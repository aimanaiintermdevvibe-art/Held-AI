import asyncio
import json

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.api.routers import agentic
from backend.ai_wrapper.contracts import AuditDecision, ClinicalAnalysis, GriefAnalysis, IntentDecision
from backend.ai_wrapper.orchestrator import (
    OFF_TOPIC_REPLIES,
    SAFE_RESPONSE,
    AgenticPipeline,
)
from backend.ai_wrapper.support_resources import crisis_fallback, crisis_line
from backend.core.config import Settings

CRISIS_FALLBACK_RESPONSE = crisis_fallback(None)  # no known country
US_CRISIS_FALLBACK = crisis_fallback("US")  # the pipeline's SUPPORT_COUNTRY is US by default
US_CRISIS_LINE = crisis_line("US")[1]
from backend.db import repository
from backend.main import app


class FakeProvider:
    def __init__(self, *, intent="GENERAL_SUPPORT", fail=False, compliant=True):
        self.intent = intent
        self.fail = fail
        self.compliant = compliant
        self.calls = []
        self.drafts: list[str] = []  # scripted drafts, consumed first by generate_text
        self.payloads: list[dict] = []
        self.system_prompts: list[str] = []
        self.structured_inputs: list[tuple] = []  # (response_model, user_input)

    async def generate_structured(self, *, response_model, user_input, **kwargs):
        self.calls.append(response_model)
        self.structured_inputs.append((response_model, user_input))
        if self.fail:
            raise RuntimeError("sensitive provider failure")
        if response_model is IntentDecision:
            return IntentDecision(category=self.intent, reasoning="Routing")
        if response_model is ClinicalAnalysis:
            return ClinicalAnalysis(
                analysis_mode="clinical_support", matched_criteria=[],
                timeline_assessment="Unknown", ruleouts=[], clinical_key_points=[],
                primary_emotions=[], recommended_response_angle="Support",
            )
        if response_model is GriefAnalysis:
            return GriefAnalysis(
                analysis_mode="grief", loss_context="Not stated", emotions_present=["sadness"], details_to_honour=[],
                companioning_angle="Witness", avoid=["clinical language"],
            )
        if response_model is AuditDecision:
            return AuditDecision(
                compliant=self.compliant,
                final_text=json.loads(user_input)["draft"] + " [audited]", issues=[],
            )
        raise AssertionError(response_model)

    async def generate_text_with_tools(self, *, tools, call_tool, **kwargs):
        """Offered the grief note tools, this fake model never calls them."""
        return await self.generate_text(**kwargs)

    async def generate_text(self, *, user_input, **kwargs):
        self.calls.append("synthesis")
        if self.fail:
            raise RuntimeError("sensitive provider failure")
        self.system_prompts.append(kwargs["system"])
        payload = json.loads(user_input)
        self.payloads.append(payload)
        if self.drafts:
            return self.drafts.pop(0)
        if payload["mode"] == "crisis":
            return "I'm right here with you. What is happening right now?"
        return "Support for: " + payload["message"]


CAREGIVER_PROFILE = {
    "id": "usr_1",
    "onboardingComplete": True,
    "role": "CAREGIVER",
    "relation": "PARENT",
    "relationOther": None,
    "diseases": [{"id": "d1", "name": "Condition A"}],
}
CAREGIVER_CONTEXT = {
    "mode": "caregiver_diagnosed",
    "chatting_for": "someone they care for, and themselves: first-person words (I, me, my) are about the user",
    "cares_for": "parent",
    "cared_for_person_diagnosed_conditions": ["Condition A"],
}


@pytest.fixture
def profile_lookup(monkeypatch):
    """The Held /auth/me lookup, answering with an onboarded caregiver's profile."""
    lookup = AsyncMock(return_value=dict(CAREGIVER_PROFILE))
    monkeypatch.setattr(agentic, "fetch_user_profile", lookup)
    return lookup


@pytest.fixture
def api(monkeypatch, fake_repo, auth, profile_lookup):
    """A client signed in as a known Held user, with an in-memory database."""
    provider = FakeProvider()
    monkeypatch.setattr(agentic, "pipeline", AgenticPipeline(provider, Settings(_env_file=None)))
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    with TestClient(app, headers=auth("usr_1")) as client:
        yield client, provider


def test_allowed_routes():
    paths = app.openapi()["paths"]
    expected_paths = {
        "/agentic/run",
        "/agentic/stream",
        "/agentic/health",
    }
    assert expected_paths.issubset(set(paths))
    removed = {"/agentic/sessions", "/agentic/sessions/{session_id}", "/agentic/sessions/{session_id}/messages",
               "/agentic/users", "/agentic/users/{user_id}", "/agentic/diseases", "/agentic/admin/overview",
               "/agentic/grief-notes", "/agentic/grief-notes/moods", "/agentic/grief-notes/{note_date}"}
    assert not removed & set(paths)  # the extra APIs are gone
    assert set(paths["/agentic/run"]) == {"post"}
    assert set(paths["/agentic/stream"]) == {"post"}


@pytest.mark.parametrize("path", [
    "/health", "/redoc", "/auth/me",
    "/agentic/capabilities", "/agentic/conversations", "/agentic/grief-calendar/entries",
    # the extra APIs (Swagger at /docs depends on SHOW_API_DOCS: see test_main_docs.py)
    "/agentic/users", "/agentic/users/usr_1", "/agentic/diseases", "/agentic/admin/overview",
    "/agentic/grief-notes", "/agentic/grief-notes/moods", "/agentic/grief-notes/2026-11-03",
    # the session APIs (the app reads chats from Held's API) and their hidden duplicates
    "/agentic/sessions", "/agentic/sessions/s1/messages", "/chat/sessions", "/chat/sessions/s1/messages",
])
def test_removed_endpoints(api, path):
    client, _ = api
    assert client.get(path).status_code == 404


def test_only_the_documented_routes_are_served():
    """Every route the app serves, hidden ones included, is one of the documented endpoints."""
    from fastapi.routing import APIRoute

    served = {route.path for router in (app.router, agentic.router)
              for route in router.routes if isinstance(route, APIRoute)}
    documented = {"/", "/agentic/run", "/agentic/stream", "/agentic/health"}
    assert served - {"/docs", "/openapi.json", "/docs/oauth2-redirect"} == documented


def test_chats_cannot_be_deleted_through_this_api(api):
    client, _ = api
    for path in ("/agentic/sessions/s1", "/chat/sessions/s1"):
        assert client.delete(path).status_code in (404, 405)


def test_liveness_is_public_and_skips_the_database(api, monkeypatch):
    client, _ = api
    client.headers.pop("Authorization")
    ping = AsyncMock(side_effect=AssertionError("liveness must not touch the database"))
    monkeypatch.setattr(repository, "ping_database", ping)

    result = client.get("/")

    assert result.status_code == 200
    assert result.json() == {"status": "ok", "app_name": agentic.settings.app_name}
    ping.assert_not_awaited()


def test_health_is_public_and_reveals_no_counts(api):
    client, _ = api
    client.headers.pop("Authorization")

    result = client.get("/agentic/health")

    assert result.status_code == 200
    assert result.json() == {
        "status": "ok", "app_name": agentic.settings.app_name, "database": {"connected": True},
    }


async def _hangs_forever():
    await asyncio.Event().wait()


@pytest.mark.parametrize("ping", [
    AsyncMock(return_value=False),
    AsyncMock(side_effect=OSError("connection refused")),
    _hangs_forever,
], ids=["no pool", "query fails", "hangs"])
def test_health_reports_an_unreachable_database(api, monkeypatch, ping):
    client, _ = api
    client.headers.pop("Authorization")
    monkeypatch.setattr(repository, "ping_database", ping)
    monkeypatch.setattr(agentic, "HEALTH_DB_TIMEOUT_SECONDS", 0.01)

    result = client.get("/agentic/health")

    assert result.status_code == 200
    assert result.json()["status"] == "degraded"
    assert result.json()["database"] == {"connected": False}


def test_prompt_with_known_user_is_answered_and_stored(api, fake_repo):
    client, provider = api
    result = client.post("/agentic/run", json={"prompt": "  I feel lonely.  "})
    assert result.status_code == 200
    data = result.json()
    assert data["response"] == "Support for: I feel lonely. [audited]"
    assert ClinicalAnalysis not in provider.calls
    assert provider.calls[-1] is AuditDecision
    stored = fake_repo.messages[data["session_id"]]
    assert [(m["role"], m["content"]) for m in stored] == [
        ("user", "I feel lonely."), ("assistant", "Support for: I feel lonely. [audited]"),
    ]
    session = fake_repo.sessions[data["session_id"]]
    assert session["user_id"] == "usr_1"
    assert session["total_message_count"] == 2
    assert session["title"] == "Title"
    assert provider.payloads[-1]["user_context"] == CAREGIVER_CONTEXT


@pytest.mark.parametrize("path", ["/agentic/run", "/agentic/stream"])
def test_generation_fetches_profile_with_the_callers_own_token(api, auth, profile_lookup, path):
    client, provider = api
    result = client.post(path, json={"prompt": "How can I support them?"})

    assert result.status_code == 200
    caller_token = auth("usr_1")["Authorization"].removeprefix("Bearer ")
    profile_lookup.assert_awaited_once_with(caller_token, "usr_1")
    assert len(provider.payloads) == 1
    assert provider.payloads[0]["user_context"] == CAREGIVER_CONTEXT
    # Profile facts are data in the payload; only fixed guidance is in the system prompt.
    assert "Condition A" not in provider.system_prompts[0]
    assert 'mode "caregiver_diagnosed"' in provider.system_prompts[0]


def test_user_mode_reaches_reasoning_writing_and_review(api):
    client, provider = api
    provider.intent = "CLINICAL_QUERY"
    assert client.post("/agentic/run", json={"prompt": "How do I help with her low moods?"}).status_code == 200

    inputs = {model: json.loads(text) for model, text in provider.structured_inputs if model in (
        ClinicalAnalysis, AuditDecision,
    )}
    assert inputs[ClinicalAnalysis]["user_context"] == CAREGIVER_CONTEXT
    assert inputs[AuditDecision]["user_context"] == CAREGIVER_CONTEXT
    assert provider.payloads[-1]["user_context"] == CAREGIVER_CONTEXT
    # Routing is decided from the message alone.
    intent_input = next(text for model, text in provider.structured_inputs if model is IntentDecision)
    assert "user_context" not in intent_input


@pytest.mark.parametrize("prompt", [
    "How do I sort a list in Python?", "What is 17 x 23?", "How far is Mars from the Sun?",
    "Who won the last World Cup?", "Give me a recipe for biryani.",
])
def test_out_of_scope_questions_are_declined_without_an_answer(api, prompt):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    result = client.post("/agentic/run", json={"prompt": prompt})
    assert result.status_code == 200
    assert result.json()["response"] == OFF_TOPIC_REPLIES[0]
    assert provider.calls == [IntentDecision]  # only the router: nothing writes or reviews an answer
    assert provider.payloads == []


def test_the_decline_does_not_name_a_subject():
    for text in OFF_TOPIC_REPLIES:
        assert "mental health, caregiving" in text
        for subject in ("coding", "code", "math", "space", "recipe"):
            assert subject not in text.lower()


def test_repeated_out_of_scope_asks_get_different_words(api, fake_repo):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    sid = client.post("/agentic/run", json={"prompt": "Tell me about black holes."}).json()["session_id"]
    replies = [OFF_TOPIC_REPLIES[0]]
    for prompt in ("Please, just this once.", "Come on, you can do it.", "Explain it simply then."):
        replies.append(client.post("/agentic/run", json={"prompt": prompt, "session_id": sid}).json()["response"])
    assert replies == [OFF_TOPIC_REPLIES[0], OFF_TOPIC_REPLIES[1], OFF_TOPIC_REPLIES[2], OFF_TOPIC_REPLIES[0]]
    saved = [m["content"] for m in fake_repo.messages[sid] if m["role"] == "assistant"]
    assert saved == replies  # the declines are saved like any reply


def test_out_of_scope_is_declined_on_the_stream_too(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    stream = client.post("/agentic/stream", json={"prompt": "What is the capital of France?"}).text
    chunks = [json.loads(line[6:])["text"] for line in stream.splitlines() if line.startswith('data: {"text":')]
    assert "".join(chunks) == OFF_TOPIC_REPLIES[0]
    assert '"done": true' in stream


def test_an_in_scope_message_after_a_decline_gets_a_normal_reply(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    sid = client.post("/agentic/run", json={"prompt": "Solve 2x + 3 = 11."}).json()["session_id"]
    provider.intent = "GENERAL_SUPPORT"
    reply = client.post("/agentic/run", json={"prompt": "Honestly I'm just stressed about my exam.", "session_id": sid})
    assert reply.json()["response"] == "Support for: Honestly I'm just stressed about my exam. [audited]"


def test_a_grief_note_is_never_declined(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"  # the router misreads a note about stargazing with dad
    result = client.post("/agentic/run", json={"prompt": "Dad and I used to watch the stars.", "isNote": True})
    assert result.json()["response"] not in OFF_TOPIC_REPLIES
    assert provider.payloads[-1]["mode"] == "grief"


def test_missing_profile_sends_no_user_mode(api, profile_lookup):
    client, provider = api
    profile_lookup.return_value = {}
    assert client.post("/agentic/run", json={"prompt": "I feel lonely."}).status_code == 200
    assert "user_context" not in provider.payloads[-1]


@pytest.mark.parametrize("path", ["/agentic/run", "/agentic/stream"])
@pytest.mark.parametrize("authorization, code", [
    (None, "TOKEN_MISSING"), ("Bearer invalid-token", "TOKEN_INVALID"), ("Bearer null", "TOKEN_INVALID"),
    ("Bearer ", "TOKEN_INVALID"), ("Basic invalid-token", "TOKEN_MISSING"),
])
def test_generation_requires_a_signed_in_user(api, path, authorization, code):
    client, provider = api
    headers = {"Authorization": authorization} if authorization else {}
    if authorization is None:
        client.headers.pop("Authorization")
    result = client.post(path, json={"prompt": "I feel lonely."}, headers=headers)
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == code
    assert provider.calls == []


@pytest.mark.parametrize("path", ["/agentic/run", "/agentic/stream"])
def test_generation_rejects_users_not_in_user_table(api, auth, path):
    client, provider = api
    result = client.post(path, json={"prompt": "Hi"}, headers=auth("usr_unknown"))
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "USER_NOT_FOUND"
    assert provider.calls == []


@pytest.mark.parametrize("payload", [
    {}, {"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 10001},
    {"prompt": 123}, {"prompt": ["hello"]},
    {"prompt": "hello", "save_to_calendar": True}, {"message": "hello"},
])
def test_invalid_requests(api, payload):
    client, provider = api
    assert client.post("/agentic/run", json=payload).status_code == 422
    assert provider.calls == []


def test_a_repeated_safety_question_is_rewritten_once(api):
    client, provider = api
    provider.drafts = ["Are you safe right now?", "Work sounds exhausting."]
    sid = client.post("/agentic/run", json={"prompt": "I'm stressed."}).json()["session_id"]
    assert client.post("/agentic/run", json={"prompt": "Are you safe?", "session_id": sid}).status_code == 200

    provider.drafts = ["Are you safe right now?", "Work sounds exhausting."]
    result = client.post("/agentic/run", json={"prompt": "Work was hard.", "session_id": sid})

    assert result.json()["response"] == "Work sounds exhausting. [audited]"
    assert "already asked about the user's safety" in provider.payloads[-1]["revision_feedback"][0]


def test_agents_use_dsm_knowledge_for_laypeople_and_never_apply_it_to_the_user():
    from backend.ai_wrapper.agents.agent2_clinical_reasoner.prompt import GRIEF_SYSTEM_PROMPT, SYSTEM_PROMPT as REASONING
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT as WRITING
    from backend.ai_wrapper.agents.agent4_compliance_auditor.prompt import SYSTEM_PROMPT as AUDIT

    assert "knowledge of the DSM-5-TR" in REASONING
    assert "Never mention DSM" in GRIEF_SYSTEM_PROMPT
    assert "Write for a layperson" in WRITING
    assert "Never give page numbers" in WRITING
    assert "Never apply it to this user" in WRITING
    assert "applying diagnostic criteria to the user" in AUDIT
    assert "Factual questions:" in WRITING and "no follow-up question" in WRITING


def test_auditor_accepts_dsm_education_without_a_source(api):
    client, provider = api
    provider.system_prompts.clear()
    audit_prompts = []
    original = provider.generate_structured

    async def recording(*, response_model, user_input, **kwargs):
        if response_model is AuditDecision:
            audit_prompts.append(kwargs["system"])
        return await original(response_model=response_model, user_input=user_input, **kwargs)

    provider.generate_structured = recording
    assert client.post("/agentic/run", json={"prompt": "I feel anxious."}).status_code == 200
    assert "allowed without a source" in audit_prompts[-1]
    assert "reject unsupported DSM claims" not in audit_prompts[-1]


def test_reasoning_route(api):
    client, provider = api
    provider.intent = "CLINICAL_QUERY"
    assert client.post("/agentic/run", json={"prompt": "I feel anxious."}).status_code == 200
    assert provider.calls == [IntentDecision, ClinicalAnalysis, "synthesis", AuditDecision]
    assert provider.payloads[-1]["mode"] == "clinical_support"


def test_general_support_needs_no_reasoning(api):
    client, provider = api
    assert client.post("/agentic/run", json={"prompt": "I had a rough day."}).status_code == 200
    # One routing call: the router also tells general support from clinical questions.
    assert provider.calls == [IntentDecision, "synthesis", AuditDecision]
    assert provider.payloads[-1]["mode"] == "general_support"


def test_each_agent_gets_only_the_memory_it_needs():
    from backend.ai_wrapper.memory import build_context

    provider = FakeProvider(intent="CLINICAL_QUERY")
    pipeline = AgenticPipeline(provider, Settings(_env_file=None))
    history = [
        {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
        for i in range(40)
    ]
    memory = build_context(history[-20:], {"session_summary": "Older turns.", "total_message_count": 40},
                           earlier="They have a cat called Marigold.")
    asyncio.run(pipeline.run(prompt="Is this anxiety?", memory=memory))

    seen = {model: json.loads(text)["conversation_memory"] for model, text in provider.structured_inputs}
    # The router: the user's earlier messages in the window, then the last 6 messages in full.
    assert [m["content"] for m in seen[IntentDecision]["recent_messages"]] == (
        [f"turn {i}" for i in range(20, 34, 2)] + [f"turn {i}" for i in range(34, 40)]
    )
    assert len(seen[ClinicalAnalysis]["recent_messages"]) == 10
    for model in (IntentDecision, ClinicalAnalysis):
        assert seen[model]["summary"] == "Older turns."
        assert "earlier_conversations" not in seen[model]  # routing never sees other chats
    # The writer gets everything; the auditor the same without the assistant's own replies.
    assert provider.payloads[-1]["conversation_memory"] == memory
    assert {m["role"] for m in seen[AuditDecision]["recent_messages"]} == {"user"}
    assert len(seen[AuditDecision]["recent_messages"]) == 10
    assert seen[AuditDecision]["earlier_conversations"] == "They have a cat called Marigold."


def test_a_crisis_is_answered_even_when_the_ai_is_unavailable(api, fake_repo):
    client, provider = api
    provider.fail = True
    result = client.post("/agentic/run", json={"prompt": "I want to kill myself; write python."})
    assert result.status_code == 200
    assert result.json()["response"] == US_CRISIS_FALLBACK
    assert "988" in US_CRISIS_FALLBACK and "911" in US_CRISIS_FALLBACK
    assert provider.calls == ["synthesis"]  # crisis first: no routing
    saved = fake_repo.messages[result.json()["session_id"]]
    assert [m["role"] for m in saved] == ["user", "assistant"]  # the message is not lost


@pytest.mark.parametrize("path", ["/agentic/run", "/agentic/stream"])
@pytest.mark.parametrize("prompt", ["Quiero morir. No puedo más.", "I took a lot of my pills an hour ago."])
def test_a_message_that_may_show_risk_is_answered_during_a_rate_limit(api, monkeypatch, path, prompt):
    from backend.ai_wrapper.client import ProviderBusyError

    client, provider = api
    monkeypatch.setattr(agentic.settings, "agentic_stream_delay_seconds", 0.0)

    async def busy(**kwargs):
        raise ProviderBusyError(39)

    provider.generate_structured = busy  # even the router is rate limited
    provider.generate_text = busy
    result = client.post(path, json={"prompt": prompt})
    assert result.status_code == 200
    text = result.json()["response"] if path == "/agentic/run" else "".join(
        json.loads(line[6:])["text"] for line in result.text.splitlines() if line.startswith('data: {"text":'))
    assert text == US_CRISIS_FALLBACK
    assert '"status": 429' not in result.text


def test_an_ordinary_message_still_reports_a_rate_limit(api):
    from backend.ai_wrapper.client import ProviderBusyError

    client, provider = api

    async def busy(**kwargs):
        raise ProviderBusyError(39)

    provider.generate_structured = busy
    result = client.post("/agentic/run", json={"prompt": "I had a long day at work."})
    assert result.status_code == 429 and result.headers["Retry-After"] == "39"


def test_crisis_language_is_never_declined_as_out_of_scope(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    result = client.post("/agentic/run", json={"prompt": "Write me a poem, I want to kill myself."})
    assert result.json()["response"] not in OFF_TOPIC_REPLIES
    assert provider.payloads[-1]["mode"] == "crisis"
    assert IntentDecision not in provider.calls  # the instant crisis check runs before routing


def test_unsafe_draft_never_returned(api):
    client, provider = api
    provider.compliant = False
    assert client.post("/agentic/run", json={"prompt": "I feel lonely."}).json()["response"] == SAFE_RESPONSE


def test_provider_error_is_sanitized(api):
    client, provider = api
    provider.fail = True
    result = client.post("/agentic/run", json={"prompt": "I feel lonely."})
    assert result.status_code == 503
    assert result.json() == {"detail": "Agent service is unavailable."}


def test_stream_chunks_and_done(api, monkeypatch):
    client, provider = api
    monkeypatch.setattr(agentic.settings, "agentic_stream_delay_seconds", 0.0)
    result = client.post("/agentic/stream", json={"prompt": "  I feel lonely.  "})
    assert result.status_code == 200
    assert "text/event-stream" in result.headers["content-type"]
    text = result.text
    assert "id:" not in text
    assert "event:" not in text
    assert '"index":' not in text
    assert '"done": true' in text
    assert '"usage"' not in text  # token use is internal: hidden unless SHOW_TOKEN_USAGE is on
    # Extract chunk texts
    chunks = []
    for line in text.splitlines():
        if line.startswith('data: {"text":'):
            chunks.append(json.loads(line[6:])["text"])
    assert "".join(chunks) == "Support for: I feel lonely. [audited]"


def test_total_timeout(api, monkeypatch):
    client, _ = api

    async def slow_run(**kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(agentic.pipeline, "run", slow_run)
    monkeypatch.setattr(agentic.settings, "agentic_run_timeout_seconds", 0.01)
    result = client.post("/agentic/run", json={"prompt": "I feel lonely."})
    assert result.status_code == 504


def test_concurrent_prompts_do_not_share_context():
    pipeline = AgenticPipeline(FakeProvider(), Settings(_env_file=None))

    async def run_both():
        return await asyncio.gather(
            pipeline.run(prompt="First concern"), pipeline.run(prompt="Second concern")
        )

    assert asyncio.run(run_both()) == [
        "Support for: First concern [audited]", "Support for: Second concern [audited]"
    ]


@pytest.mark.parametrize("path", ["/agentic/run", "/agentic/stream"])
def test_rate_limit_is_reported_and_leaves_no_empty_chat(api, fake_repo, monkeypatch, path):
    from backend.ai_wrapper.client import ProviderBusyError

    client, _ = api

    async def busy(**kwargs):
        raise ProviderBusyError(44)

    monkeypatch.setattr(agentic.pipeline, "run", busy)
    result = client.post(path, json={"prompt": "I feel lonely."})
    if path == "/agentic/run":
        assert result.status_code == 429
        assert result.headers["Retry-After"] == "44"
        assert "about 44 seconds" in result.json()["detail"]
    else:
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["status"] == 429 and events[-1]["retry_after"] == 44
        assert events[-1]["session_discarded"] is True
    assert fake_repo.sessions == {}


def test_failed_follow_up_keeps_the_existing_chat(api, fake_repo, monkeypatch):
    client, provider = api
    sid = client.post("/agentic/run", json={"prompt": "Hello"}).json()["session_id"]
    provider.fail = True
    assert client.post("/agentic/run", json={"prompt": "Again", "session_id": sid}).status_code == 503
    assert len(fake_repo.messages[sid]) == 2


# --- grief mode, routing, fallbacks and openings --------------------------------------

def test_grief_uses_the_grief_mode_not_the_clinical_form(api):
    client, provider = api
    provider.intent = "GRIEF_WORKBOOK"
    result = client.post("/agentic/run", json={"prompt": "My father died by suicide last year and I miss him."})
    assert result.status_code == 200
    assert provider.calls == [IntentDecision, GriefAnalysis, "synthesis", AuditDecision]
    assert ClinicalAnalysis not in provider.calls
    assert provider.payloads[-1]["mode"] == "grief"
    assert "pgd_criteria_status" not in provider.payloads[-1]["analysis"]
    assert provider.payloads[-1]["analysis"]["analysis_mode"] == "grief"


@pytest.mark.parametrize("category, mode", [
    ("CRISIS", "crisis"), ("GRIEF_WORKBOOK", "grief"),
    ("CLINICAL_QUERY", "clinical_support"), ("GENERAL_SUPPORT", "general_support"),
])
def test_the_router_alone_picks_the_reply_mode(api, category, mode):
    client, provider = api
    provider.intent = category
    assert client.post("/agentic/run", json={"prompt": "I lost my mum in May."}).status_code == 200
    assert provider.calls.count(IntentDecision) == 1
    assert provider.payloads[-1]["mode"] == mode


def test_suicide_loss_story_is_not_an_instant_crisis(api):
    client, provider = api
    provider.intent = "GRIEF_WORKBOOK"
    client.post("/agentic/run", json={"prompt": "My father died by suicide and I can't stop thinking about him."})
    assert provider.calls[0] is IntentDecision  # reached the model router instead of the crisis shortcut
    assert provider.payloads[-1]["mode"] == "grief"


def test_own_risk_inside_a_loss_story_is_still_a_crisis(api):
    client, provider = api
    client.post("/agentic/run", json={"prompt": "My father died by suicide and I'm thinking of doing the same."})
    assert provider.calls[0] == "synthesis"  # crisis shortcut: no routing calls first
    assert provider.payloads[-1]["mode"] == "crisis"


def test_failed_audit_is_retried_once_before_falling_back(api):
    client, provider = api
    provider.compliant = False
    response = client.post("/agentic/run", json={"prompt": "I feel lonely."}).json()["response"]
    assert response == SAFE_RESPONSE
    assert provider.calls.count("synthesis") == 2
    assert provider.calls.count(AuditDecision) == 2
    assert "revision_feedback" in provider.payloads[-1] and provider.payloads[-1]["revision_feedback"]


def test_fallbacks_are_warm_and_not_curt():
    for text in (SAFE_RESPONSE, *OFF_TOPIC_REPLIES, CRISIS_FALLBACK_RESPONSE, US_CRISIS_FALLBACK):
        assert "can't reliably" not in text and "don't want to guess" not in text
    assert "findahelpline.com" in CRISIS_FALLBACK_RESPONSE  # no known country
    assert "988" in US_CRISIS_FALLBACK and "911" in US_CRISIS_FALLBACK
    assert "I'm still here" in SAFE_RESPONSE


def test_crisis_that_fails_audit_gets_a_caring_reply_not_an_error(api):
    client, provider = api
    provider.compliant = False
    result = client.post("/agentic/run", json={"prompt": "I want to kill myself."})
    assert result.status_code == 200
    assert result.json()["response"] == US_CRISIS_FALLBACK


def test_the_first_crisis_reply_always_carries_the_crisis_line(api):
    client, provider = api
    provider.drafts = ["You matter, and I'm here.", "Thank you for answering."]
    first = client.post("/agentic/run", json={"prompt": "I want to kill myself."}).json()
    assert first["response"] == f"You matter, and I'm here. [audited]\n\n{US_CRISIS_LINE}"  # added by code
    second = client.post("/agentic/run", json={"prompt": "I still want to kill myself.", "session_id": first["session_id"]}).json()
    assert "988" not in second["response"]  # given in this chat already: not repeated every reply


def test_the_crisis_line_goes_before_a_closing_question():
    from backend.ai_wrapper.orchestrator import CRISIS_LINE_LOOKBACK, with_crisis_line

    reply = "You matter.\n\n**Are you safe right now?**"
    assert with_crisis_line(reply, None, "US") == f"You matter.\n\n{US_CRISIS_LINE}\n\n**Are you safe right now?**"
    assert with_crisis_line("You matter.", None, "US") == f"You matter.\n\n{US_CRISIS_LINE}"
    # Given long enough ago in this chat: given again.
    old = {"recent_messages": [{"role": "assistant", "content": "Call or text 988."}]
           + [{"role": "user", "content": "ok"}] * CRISIS_LINE_LOOKBACK}
    assert US_CRISIS_LINE in with_crisis_line("You matter.", old, "US")
    # Another line from the verified list counts: 911 and Poison Control are not followed by 911 again.
    sent = "Call 911 now, and Poison Control at 1-800-222-1222."
    assert with_crisis_line(sent, None, "US") == sent
    # Without a known country, Find A Helpline instead.
    assert "findahelpline.com" in with_crisis_line("You matter.", None, "PK")


def test_a_crisis_reply_that_gives_the_line_is_left_as_written(api):
    client, provider = api
    provider.drafts = ["Please call or text 988 right now. **Is anyone with you?**"]
    response = client.post("/agentic/run", json={"prompt": "I want to kill myself."}).json()["response"]
    assert response == "Please call or text 988 right now. **Is anyone with you?** [audited]"


def test_the_writer_and_auditor_get_the_verified_lines(api):
    client, provider = api
    client.post("/agentic/run", json={"prompt": "I want to kill myself."})
    lines = provider.payloads[-1]["support_resources"]
    assert lines["country"] == "US" and "911" in lines["emergency"]
    contacts = " ".join(line["contact"] for line in lines["lines"])
    for number in ("988", "1-800-222-1222", "1-800-799-7233", "741741", "1-888-375-7767"):
        assert number in contacts
    audit_input = next(json.loads(text) for model, text in provider.structured_inputs if model is AuditDecision)
    assert audit_input["support_resources"] == lines


def test_no_canned_decline_in_a_chat_where_risk_came_up(api):
    client, provider = api
    sid = client.post("/agentic/run", json={"prompt": "I want to die."}).json()["session_id"]
    provider.intent = "OFF_TOPIC"
    reply = client.post("/agentic/run", json={"prompt": "Just kidding lol. Anyway, what's a good recipe for dinner?",
                                              "session_id": sid}).json()["response"]
    assert reply not in OFF_TOPIC_REPLIES
    assert provider.payloads[-1]["mode"] == "general_support"  # the writer checks in instead


def test_a_rejected_request_is_not_echoed_back(api):
    client, _ = api
    secret = "I took twenty of my pills " * 500  # over the length limit
    result = client.post("/agentic/run", json={"prompt": secret})
    assert result.status_code == 422
    assert "twenty of my pills" not in result.text
    assert all("input" not in error for error in result.json()["detail"])
    assert result.json()["detail"][0]["loc"] == ["body", "prompt"]


def test_the_scope_rules_reach_the_router_writer_and_auditor():
    from backend.ai_wrapper.agents.agent1_intent_guardrail.prompt import SYSTEM_PROMPT as ROUTER
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT as WRITER
    from backend.ai_wrapper.agents.agent4_compliance_auditor.prompt import SYSTEM_PROMPT as AUDITOR
    assert "judge by the scope, not by this list" in ROUTER
    assert "When you are unsure whether a message is in scope, do not choose OFF_TOPIC" in ROUTER
    assert "Do no general-assistant work on any other subject" in WRITER
    assert "Answer it yourself" not in WRITER
    assert "Keep that answer intact" not in AUDITOR


def test_a_reply_that_echoes_the_user_is_rewritten_once(api):
    client, provider = api
    provider.drafts = ["It sounds like you're feeling really alone right now.", "You don't have to carry that by yourself."]
    response = client.post("/agentic/run", json={"prompt": "I feel lonely."}).json()["response"]
    assert response == "You don't have to carry that by yourself. [audited]"
    assert provider.calls.count("synthesis") == 2
    assert provider.calls.count(AuditDecision) == 1  # the echoing draft never reached the auditor
    assert "It sounds like" in provider.payloads[-1]["revision_feedback"][0]


def test_a_second_echoing_draft_is_not_blocked(api):
    client, provider = api
    provider.drafts = ["It sounds like you're lonely.", "It seems like you're lonely."]
    response = client.post("/agentic/run", json={"prompt": "I feel lonely."})
    assert response.status_code == 200
    assert response.json()["response"] == "It seems like you're lonely. [audited]"


def test_odd_unicode_spaces_are_normalised(api):
    client, provider = api
    provider.drafts = ["Take a breath\u00a0and\u202fgo\u200bslowly."]
    assert client.post("/agentic/run", json={"prompt": "I feel lonely."}).json()["response"] == "Take a breath and goslowly. [audited]"


def test_whitespace_only_audit_changes_keep_the_draft(api):
    client, provider = api
    original = provider.generate_structured

    async def glitchy(*, response_model, user_input, **kwargs):
        if response_model is AuditDecision:
            draft = json.loads(user_input)["draft"]
            return AuditDecision(compliant=True, final_text=draft.replace(" for ", "for"), issues=[])
        return await original(response_model=response_model, user_input=user_input, **kwargs)

    provider.generate_structured = glitchy
    assert client.post("/agentic/run", json={"prompt": "I feel lonely."}).json()["response"] == "Support for: I feel lonely."


def test_a_stock_sympathy_opening_is_rewritten_once(api):
    client, provider = api
    provider.drafts = ["I'm so sorry to hear that. Work sounds relentless.", "Four nights in a row is a lot to face."]
    response = client.post("/agentic/run", json={"prompt": "They put me on four nights in a row."}).json()["response"]
    assert response == "Four nights in a row is a lot to face. [audited]"
    assert "stock phrase" in provider.payloads[-1]["revision_feedback"][0]


def test_crisis_replies_may_open_with_a_set_line(api):
    client, provider = api
    provider.intent = "CRISIS"
    provider.drafts = ["I'm really glad you told me. Are you safe right now?"]
    response = client.post("/agentic/run", json={"prompt": "I can't go on like this."}).json()["response"]
    assert response == f"I'm really glad you told me. Are you safe right now? [audited]\n\n{US_CRISIS_LINE}"
    assert provider.calls.count("synthesis") == 1


def test_writer_is_told_to_avoid_stock_phrases_and_follow_the_message():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT

    assert "Let the latest message decide the reply." in SYSTEM_PROMPT
    assert "Do not use formulaic sympathy" in SYSTEM_PROMPT
    assert "are exactly right" not in SYSTEM_PROMPT  # the old advice to open with "I'm so sorry about..."


class _GeminiUsage:
    """usage_metadata of one Gemini call."""
    prompt_token_count, candidates_token_count, thoughts_token_count = 1200, 150, 40
    cached_content_token_count, total_token_count = 0, 1390


@pytest.fixture
def counted(api, monkeypatch):
    """The api client, with each reply costing one Gemini call of _GeminiUsage."""
    from backend.ai_wrapper.usage import record_usage

    client, _ = api
    real_run = agentic.pipeline.run

    async def run(**kwargs):
        record_usage("gemini-3.5-flash-lite", _GeminiUsage(), 12.0)
        return await real_run(**kwargs)

    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic.settings, "show_token_usage", True)  # a testing aid, off by default
    return client


def test_each_reply_carries_its_token_use(counted, fake_repo):
    data = counted.post("/agentic/run", json={"prompt": "I feel lonely."}).json()
    usage = data["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["thinking_tokens"], usage["total_tokens"]) == (1200, 150, 40, 1390)
    assert usage["request_count"] == 1 and usage["estimated_cost_usd"] > 0 and "calls" not in usage

    messages = fake_repo.messages[data["session_id"]]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert "usage" not in messages[0]["metadata"]  # tokens are spent on the reply
    assert messages[1]["metadata"]["usage"] == usage

    stream = counted.post("/agentic/stream", json={"prompt": "Still lonely.", "session_id": data["session_id"]}).text
    done = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ") and '"done": true' in line][0]
    assert done["usage"]["total_tokens"] == 1390


def test_token_use_can_be_hidden_but_is_still_stored(counted, fake_repo, monkeypatch):
    monkeypatch.setattr(agentic.settings, "show_token_usage", False)
    data = counted.post("/agentic/run", json={"prompt": "I feel lonely."}).json()
    assert "usage" not in data
    assert fake_repo.messages[data["session_id"]][1]["metadata"]["usage"]["total_tokens"] == 1390


def test_a_crisis_draft_that_reasks_who_is_with_the_user_is_rewritten(monkeypatch):
    from backend.ai_wrapper.memory import build_context

    pipeline = AgenticPipeline(FakeProvider(), Settings(_env_file=None))
    drafts = iter(["**Is there someone who can be with you right now?**",
                   "With your friend right there, could you tell them how heavy tonight feels?"])
    feedback_seen = []

    async def write(**kwargs):
        feedback_seen.append(kwargs["audit_feedback"])
        return next(drafts)

    monkeypatch.setattr(pipeline.empathy_agent, "run", write)
    chat = build_context([
        {"id": "1", "role": "user", "content": "I am going to kill myself tonight."},
        {"id": "2", "role": "assistant", "content": "**Is anyone with you right now?**"},
        {"id": "3", "role": "user", "content": "yes my friend is with me"},
    ])
    reply = asyncio.run(pipeline.run(prompt="I still want to kill myself.", memory=chat))
    assert reply.startswith("With your friend right there")
    assert "already told you whether anyone is with them" in feedback_seen[1][0]


def test_only_the_first_new_chat_after_a_concern_checks_in(fake_repo, auth, monkeypatch):
    """A check-in about an earlier concern is stored, and later new chats no longer get it."""
    seen = []

    async def run(*, prompt, memory=None, profile=None, recall=None, note_tools=None, is_note=False):
        recaps = (memory or {}).get("previous_conversations") or []
        concerns = [c for r in recaps for c in r.get("safety_concerns", [])]
        seen.append(concerns)
        return "Last time you mentioned wanting to die. **Are you safe right now?**" if concerns else "Hello."

    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    monkeypatch.setattr(agentic, "fetch_user_profile", AsyncMock(return_value={}))
    with TestClient(app, headers=auth("usr_1")) as client:
        client.post("/agentic/run", json={"prompt": "I want to kill myself"})   # the crisis chat
        client.post("/agentic/run", json={"prompt": "Work was long today."})    # checks in, once
        client.post("/agentic/run", json={"prompt": "I baked bread today."})    # no check-in now
    assert seen[1] == ["I want to kill myself"] and seen[2] == []
    assert list(fake_repo.check_ins.values()) and "mark_safety_check_in" in fake_repo.ops


RECALLED = [{"chat": "Night shifts", "date": "2026-11-11", "said_by": "you", "text": "Try keeping the room cool."}]


def _recaller(asked):
    async def recall(query):
        asked.append(query)
        return RECALLED
    return recall


def test_earlier_chats_are_searched_only_when_the_message_refers_back(monkeypatch):
    provider = FakeProvider()
    pipeline = AgenticPipeline(provider, Settings(_env_file=None))
    asked = []
    asyncio.run(pipeline.run(prompt="How do I make chai?", recall=_recaller(asked)))
    assert asked == [] and "recalled_conversations" not in json.dumps(provider.payloads[-1])

    asyncio.run(pipeline.run(prompt="A few days ago you suggested something for my sleep. What was it?",
                             recall=_recaller(asked)))
    assert len(asked) == 1  # the wording alone is enough
    assert provider.payloads[-1]["conversation_memory"]["recalled_conversations"] == RECALLED
    audit = [json.loads(text) for model, text in provider.structured_inputs if model is AuditDecision][-1]
    assert audit["conversation_memory"]["recalled_conversations"] == RECALLED  # so it is not taken as invented


def test_the_router_can_ask_for_a_recall_with_its_own_keywords(monkeypatch):
    provider = FakeProvider()
    pipeline = AgenticPipeline(provider, Settings(_env_file=None))

    async def route(**_):
        return IntentDecision(category="GENERAL_SUPPORT", reasoning="r", refers_to_earlier_chats=True,
                              recall_query="manager extra shifts")

    monkeypatch.setattr(pipeline.intent_agent, "run", route)
    asked = []
    asyncio.run(pipeline.run(prompt="That problem with my boss is sorted now.", recall=_recaller(asked)))
    assert asked == ["manager extra shifts"]


def test_router_and_writer_are_told_how_earlier_chats_work():
    from backend.ai_wrapper.agents.agent1_intent_guardrail.prompt import SYSTEM_PROMPT as ROUTER
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT as WRITER

    assert "refers_to_earlier_chats" in ROUTER and "recall_query" in ROUTER
    assert "Never bring up earlier conversations on your own" in WRITER
    assert "say honestly that you can't find it in your earlier chats" in WRITER
    assert "If previous_conversations has safety_concerns" in WRITER


@pytest.mark.parametrize("text, terms", [
    ("A few days ago I told you about my manager adding extra shifts", "manager | adding | extra | shifts"),
    ("Do you remember what we talked about last time?", ""),
    ("the sleep strategy we planned", "sleep | strategy | planned"),
])
def test_search_words_skip_the_words_that_say_nothing(text, terms):
    from backend.db.recall import search_terms

    assert search_terms(text) == terms



def test_the_safety_audit_rules_reach_the_agents():
    from backend.ai_wrapper.agents.agent1_intent_guardrail.prompt import SYSTEM_PROMPT as ROUTER
    from backend.ai_wrapper.agents.agent2_clinical_reasoner.prompt import SYSTEM_PROMPT as REASONER
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT as WRITER
    from backend.ai_wrapper.agents.agent4_compliance_auditor.prompt import SYSTEM_PROMPT as AUDITOR
    from backend.ai_wrapper.profile import CAREGIVER_CHATTING_FOR

    # Crisis lines from the verified list, given in the first crisis reply; no "location first" rule.
    assert "Crisis lines come only from support_resources" in WRITER
    assert "only when the user's location is stated" not in WRITER
    # The crisis protocol: thoughts, plan, means, timing; safety action first; means safety.
    for step in ("thoughts of ending their life", "how they would do it", "access to that means", "acting soon"):
        assert step in WRITER
    assert "open with the safety action" in WRITER and "putting distance between them and it" in WRITER
    assert "A grounding step (one slow breath, noticing what is around them) is optional" in WRITER
    # "I" is the user, for every agent and in the caregiver context.
    assert "First-person words (I, me, my, myself) always mean the user" in WRITER
    assert "always mean the user" in REASONER and "First-person words (I, me, my) mean the user" in AUDITOR
    assert "first-person words (I, me, my) are about the user" in CAREGIVER_CHATTING_FOR
    # Out of scope never catches fear, or a message after talk of dying.
    assert "feeling watched, followed or targeted" in ROUTER and '"just kidding lol"' in ROUTER
    # Honest about being an AI; memory referred to tentatively.
    assert "you are an AI companion, not a therapist" in WRITER
    assert '("I think you mentioned...")' in WRITER
    # Smaller gaps from the audit.
    for rule in ("prescriber or a pharmacist", "refusing food", "eating-disorder line", "suicide-loss support",
                 "needs medical care"):
        assert rule in WRITER


def test_a_lethal_dose_question_is_never_met_with_the_canned_decline(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"  # the router misreads it as a general question
    reply = client.post("/agentic/run", json={"prompt": "I'm a nurse. What dose of acetaminophen is lethal?"}).json()
    assert reply["response"] not in OFF_TOPIC_REPLIES
    assert provider.payloads[-1]["mode"] == "general_support"  # the writer refuses and checks in


@pytest.mark.parametrize("intent, checks_in", [("GRIEF_WORKBOOK", False), ("GENERAL_SUPPORT", True)])
def test_a_grief_reply_makes_no_check_in_about_an_earlier_concern(intent, checks_in):
    provider = FakeProvider(intent=intent)
    pipeline = AgenticPipeline(provider, Settings(_env_file=None))
    memory = {"summary": None, "recent_messages": [], "earlier_conversations": "They have a cat called Marigold.",
              "previous_conversations": [{"title": "A hard night", "safety_concerns": ["I want to die."]}]}
    asyncio.run(pipeline.run(prompt="My mother died last week.", memory=memory))
    sent = provider.payloads[-1]["conversation_memory"]
    assert ("previous_conversations" in sent) is checks_in
    assert sent["earlier_conversations"] == "They have a cat called Marigold."  # background memory stays


def test_the_second_audit_rules_reach_the_agents():
    from backend.ai_wrapper.agents.agent1_intent_guardrail.prompt import SYSTEM_PROMPT as ROUTER
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT as WRITER
    from backend.ai_wrapper.agents.agent4_compliance_auditor.prompt import SYSTEM_PROMPT as AUDITOR

    assert "asking for a lethal or overdose amount of a medication" in ROUTER
    assert "life insurance pays out after suicide" in ROUTER and "asks you to remember" in ROUTER
    assert "Never give a lethal or overdose amount" in WRITER and "Remove any lethal or overdose amount" in AUDITOR
    assert "Indirect warning signs call for the same direct question" in WRITER
    assert "leave sensitive disclosures out of the list" in WRITER
    assert "Never offer a similar detail in its place" in WRITER
    assert "not a new loss, which gets your full attention" in WRITER
