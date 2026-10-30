import asyncio
import json

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.api.routers import agentic
from backend.ai_wrapper.contracts import AuditDecision, ClinicalAnalysis, GriefAnalysis, IntentDecision
from backend.ai_wrapper.orchestrator import (
    CRISIS_FALLBACK_RESPONSE,
    OFF_TOPIC_FALLBACK_RESPONSE,
    SAFE_RESPONSE,
    AgenticPipeline,
)
from backend.ai_wrapper.supervisor.schemas import ExecutionPlan
from backend.core.auth import GUEST_PROFILE
from backend.core.config import Settings, get_settings
from backend.db import repository
from backend.main import app


class FakeProvider:
    def __init__(self, *, route="GENERAL_SUPPORT", intent="CLINICAL_QUERY", fail=False, compliant=True):
        self.route = route
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
        if response_model is ExecutionPlan:
            return ExecutionPlan(intent=self.route)
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
        if payload["mode"] == "off_topic":
            return "Model answer for: " + payload["message"]
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
    "chatting_for": "someone they care for",
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
        "/agentic/sessions",
        "/agentic/sessions/{session_id}",
        "/agentic/sessions/{session_id}/messages",
        "/agentic/users",
        "/agentic/diseases",
        "/agentic/admin/overview",
    }
    assert expected_paths.issubset(set(paths))
    assert set(paths["/agentic/run"]) == {"post"}
    assert set(paths["/agentic/stream"]) == {"post"}
    assert set(paths["/agentic/sessions"]) == {"get"}
    assert set(paths["/agentic/sessions/{session_id}"]) == {"delete"}


@pytest.mark.parametrize("path", [
    "/health", "/docs", "/redoc", "/openapi.json", "/auth/me",
    "/agentic/capabilities", "/agentic/conversations", "/agentic/grief-calendar/entries",
])
def test_removed_endpoints(api, path):
    client, _ = api
    assert client.get(path).status_code == 404


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
    provider.route = "CLINICAL_QUERY"
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


def test_off_topic_answers_ignore_the_user_mode(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    assert client.post("/agentic/run", json={"prompt": "How do I sort a list in Python?"}).status_code == 200
    assert "user_context" not in provider.payloads[-1]


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


@pytest.mark.parametrize("path", [
    "/agentic/sessions", "/agentic/sessions/private-session/messages",
    "/agentic/users", "/agentic/admin/overview",
])
def test_private_routes_still_require_auth(api, path):
    client, _ = api
    client.headers.pop("Authorization")
    result = client.get(path)
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "TOKEN_MISSING"


@pytest.mark.parametrize("payload", [
    {}, {"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 10001},
    {"prompt": 123}, {"prompt": ["hello"]},
    {"prompt": "hello", "save_to_calendar": True}, {"message": "hello"},
])
def test_invalid_requests(api, payload):
    client, provider = api
    assert client.post("/agentic/run", json=payload).status_code == 422
    assert provider.calls == []


@pytest.fixture
def guest_api(monkeypatch, fake_repo):
    """A client sending no token to a deployment running with AUTH_DISABLED."""
    provider = FakeProvider()
    monkeypatch.setattr(get_settings(), "auth_disabled", True)
    monkeypatch.setattr(agentic, "pipeline", AgenticPipeline(provider, Settings(_env_file=None)))
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    with TestClient(app) as client:
        yield client, provider


def test_tokens_are_required_unless_auth_is_explicitly_disabled():
    assert Settings(_env_file=None).auth_disabled is False


@pytest.mark.parametrize("path", ["/agentic/run", "/agentic/stream"])
def test_disabled_auth_answers_without_a_token_as_the_guest(guest_api, fake_repo, profile_lookup, path):
    client, provider = guest_api

    result = client.post(path, json={"prompt": "I feel lonely."})

    assert result.status_code == 200
    assert "audited" in result.text
    assert [s["user_id"] for s in fake_repo.sessions.values()] == [GUEST_PROFILE["id"]]
    assert "user_context" not in provider.payloads[-1]
    profile_lookup.assert_not_awaited()


def test_disabled_auth_adds_the_guest_only_once(guest_api, fake_repo):
    client, _ = guest_api
    assert client.post("/agentic/run", json={"prompt": "Hello"}).status_code == 200
    assert client.post("/agentic/run", json={"prompt": "Hello again"}).status_code == 200
    assert fake_repo.created_from_profiles == [GUEST_PROFILE]


@pytest.mark.parametrize("authorization", ["Bearer invalid-token", "Bearer ", "Basic invalid-token"])
def test_disabled_auth_ignores_any_token_sent(guest_api, authorization):
    client, _ = guest_api
    assert client.get("/agentic/sessions", headers={"Authorization": authorization}).status_code == 200


@pytest.mark.parametrize("path", ["/agentic/users", "/agentic/users/usr_1", "/agentic/admin/overview"])
@pytest.mark.parametrize("guest_role", ["USER", "ADMIN"])
def test_disabled_auth_keeps_admin_routes_closed(guest_api, fake_repo, path, guest_role):
    client, _ = guest_api
    fake_repo.users[GUEST_PROFILE["id"]] = guest_role
    assert client.get(path).status_code == 403


def test_disabled_auth_hides_other_users_sessions(guest_api, fake_repo):
    client, _ = guest_api
    private = asyncio.run(fake_repo.create_chat_session(user_id="usr_1", title="Private"))["id"]

    assert client.get("/agentic/sessions").json() == []
    assert client.get(f"/agentic/sessions/{private}/messages").status_code == 404
    assert client.post("/agentic/run", json={"prompt": "Hi", "session_id": private}).status_code == 404
    assert client.delete(f"/agentic/sessions/{private}").status_code == 404
    assert private in fake_repo.sessions


def test_disabled_auth_reports_an_unavailable_database(guest_api, monkeypatch):
    client, _ = guest_api
    monkeypatch.setattr(repository, "get_auth_user", AsyncMock(side_effect=RuntimeError("Database is unavailable.")))
    result = client.post("/agentic/run", json={"prompt": "Hi"})
    assert result.status_code == 503
    assert result.json() == {"detail": "The guest user is unavailable."}


def test_reasoning_route(api):
    client, provider = api
    provider.route = "CLINICAL_QUERY"
    assert client.post("/agentic/run", json={"prompt": "I feel anxious."}).status_code == 200
    assert provider.calls == [IntentDecision, ExecutionPlan, ClinicalAnalysis, "synthesis", AuditDecision]


def test_crisis_precedes_off_topic_and_reports_outage(api):
    client, provider = api
    provider.fail = True
    result = client.post("/agentic/run", json={"prompt": "I want to kill myself; write python."})
    assert result.status_code == 503
    assert provider.calls == ["synthesis"]


def test_off_topic_is_answered_by_the_model(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    result = client.post("/agentic/run", json={"prompt": "Write python code."})
    assert result.status_code == 200
    assert result.json()["response"] == "Model answer for: Write python code. [audited]"
    assert provider.calls == [IntentDecision, "synthesis", AuditDecision]


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
    assert "usage" not in text
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
    provider.route = "GRIEF"
    result = client.post("/agentic/run", json={"prompt": "My father died by suicide last year and I miss him."})
    assert result.status_code == 200
    assert provider.calls == [IntentDecision, ExecutionPlan, GriefAnalysis, "synthesis", AuditDecision]
    assert ClinicalAnalysis not in provider.calls
    assert provider.payloads[-1]["mode"] == "grief"
    assert "pgd_criteria_status" not in provider.payloads[-1]["analysis"]
    assert provider.payloads[-1]["analysis"]["analysis_mode"] == "grief"


@pytest.mark.parametrize("planned", ["CLINICAL_QUERY", "GENERAL_SUPPORT", "OFF_TOPIC"])
def test_grief_is_never_downgraded_by_the_supervisor(api, planned):
    client, provider = api
    provider.intent = "GRIEF_WORKBOOK"
    provider.route = planned
    assert client.post("/agentic/run", json={"prompt": "I lost my mum in May."}).status_code == 200
    assert ClinicalAnalysis not in provider.calls
    assert GriefAnalysis in provider.calls
    assert provider.payloads[-1]["mode"] == "grief"


def test_supervisor_can_still_escalate_grief_to_crisis(api):
    client, provider = api
    provider.intent = "GRIEF_WORKBOOK"
    provider.route = "CRISIS"
    assert client.post("/agentic/run", json={"prompt": "I lost my mum in May."}).status_code == 200
    assert GriefAnalysis not in provider.calls
    assert provider.payloads[-1]["mode"] == "crisis"


def test_suicide_loss_story_is_not_an_instant_crisis(api):
    client, provider = api
    provider.intent = "GRIEF_WORKBOOK"
    provider.route = "GRIEF"
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
    for text in (SAFE_RESPONSE, OFF_TOPIC_FALLBACK_RESPONSE, CRISIS_FALLBACK_RESPONSE):
        assert "can't reliably" not in text and "don't want to guess" not in text
    assert "findahelpline.com" in CRISIS_FALLBACK_RESPONSE
    assert "I'm still here" in SAFE_RESPONSE


def test_crisis_that_fails_audit_gets_a_caring_reply_not_an_error(api):
    client, provider = api
    provider.compliant = False
    result = client.post("/agentic/run", json={"prompt": "I want to kill myself."})
    assert result.status_code == 200
    assert result.json()["response"] == CRISIS_FALLBACK_RESPONSE


def test_off_topic_audit_failure_uses_the_plain_fallback(api):
    client, provider = api
    provider.intent = "OFF_TOPIC"
    provider.compliant = False
    assert client.post("/agentic/run", json={"prompt": "Write python code."}).json()["response"] == OFF_TOPIC_FALLBACK_RESPONSE


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
