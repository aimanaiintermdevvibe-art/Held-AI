import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.ai_wrapper import memory
from backend.api.routers import agentic
from backend.core.config import Settings
from backend.main import app


def turns(count, size=10):
    return [
        {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": "x" * size}
        for i in range(count)
    ]


def test_full_history_when_it_fits():
    context = memory.build_context(turns(6), {"session_summary": "old"}, budget=1000)
    assert context["summary"] is None
    assert len(context["recent_messages"]) == 6


def test_over_budget_uses_session_summary_and_messages_after_its_anchor():
    session = {"session_summary": "old", "last_summarized_message_id": "m89"}
    context = memory.build_context(turns(100), session, budget=100)
    assert context["summary"] == "old"
    assert len(context["recent_messages"]) == 10  # m90..m99


def test_over_budget_without_summary_keeps_latest_20_pairs():
    context = memory.build_context(turns(100), {}, budget=100)
    assert context["summary"] is None
    assert len(context["recent_messages"]) == 40


def test_no_history_means_no_memory():
    assert memory.build_context([], None, budget=100) is None
    assert memory.with_memory("hi", None) == "hi"


def test_previous_conversations_are_included_even_for_a_new_chat():
    previous = [{"title": "Losing Max", "summary": None, "recent_messages": [{"role": "user", "content": "Max died."}]}]
    context = memory.build_context([], None, budget=1000, previous=previous)
    assert context == {"summary": None, "recent_messages": [], "previous_conversations": previous}
    assert "previous_conversations" not in memory.build_context(turns(2), None, budget=1000)


class FakeClient:
    def __init__(self, text="Summary text"):
        self.text = text
        self.inputs = []

    async def generate_text(self, *, user_input, **kwargs):
        self.inputs.append(json.loads(user_input))
        return self.text


def test_summary_waits_for_ten_unsummarized_messages_then_anchors_on_the_last():
    settings = Settings(_env_file=None)
    client = FakeClient()
    session = {"session_summary": "s", "last_summarized_message_id": "m9"}
    assert asyncio.run(memory.refresh_summary(client, settings, session, turns(19))) is None
    assert client.inputs == []

    state = asyncio.run(memory.refresh_summary(client, settings, session, turns(20)))
    assert state == {"session_summary": "Summary text", "last_summarized_message_id": "m19"}
    assert client.inputs[0]["previous_summary"] == "s"
    assert len(client.inputs[0]["new_messages"]) == 10


def test_title_is_ai_generated_with_excerpt_fallback():
    settings = Settings(_env_file=None)
    assert asyncio.run(memory.generate_title(FakeClient('"Coping With Work Stress"\n'), settings, "p", "a")) == "Coping With Work Stress"

    class Broken:
        async def generate_text(self, **kwargs):
            raise RuntimeError("down")

    assert asyncio.run(memory.generate_title(Broken(), settings, "I feel lonely.", "a")) == "I feel lonely."


@pytest.fixture
def chat(monkeypatch, fake_repo, auth):
    """Signed-in client whose pipeline records the memory each turn received."""
    seen: list = []

    async def run(*, prompt, memory=None, profile=None, documents=None, notes=None):
        seen.append(memory)
        return f"Answer to: {prompt}"

    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Losing Max"))
    monkeypatch.setattr(agentic.settings, "agentic_stream_delay_seconds", 0.0)
    with TestClient(app, headers=auth("usr_1")) as client:
        yield client, seen


def test_memory_follows_the_conversation_and_carries_into_new_chats(chat, fake_repo):
    client, seen = chat
    first = client.post("/agentic/run", json={"prompt": "My dog Max died."}).json()
    sid = first["session_id"]
    assert seen[0] is None  # nothing to remember yet

    second = client.post("/agentic/run", json={"prompt": "What was his name?", "session_id": sid}).json()
    assert second["session_id"] == sid
    assert [m["content"] for m in seen[1]["recent_messages"]] == ["My dog Max died.", "Answer to: My dog Max died."]

    third = client.post("/agentic/run", json={"prompt": "Do you remember me?"}).json()
    assert third["session_id"] != sid
    previous = seen[2]["previous_conversations"]
    assert previous[0]["title"] == "Losing Max"
    assert previous[0]["recent_messages"][0]["content"] == "My dog Max died."

    session = fake_repo.sessions[sid]
    assert session["total_message_count"] == 4 and session["user_id"] == "usr_1"


def test_stream_stores_turn_and_returns_session_first(chat, fake_repo):
    client, _ = chat
    result = client.post("/agentic/stream", json={"prompt": "Hello"})
    events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    sid = events[0]["session_id"]
    assert events[-1]["done"] is True and events[-1]["session"]["title"] == "Losing Max"
    assert [m["role"] for m in fake_repo.messages[sid]] == ["user", "assistant"]


def test_users_cannot_continue_someone_elses_session(chat, fake_repo, auth):
    client, seen = chat
    sid = client.post("/agentic/run", json={"prompt": "Private"}).json()["session_id"]
    other = client.post("/agentic/run", json={"prompt": "Hi", "session_id": sid}, headers=auth("usr_2"))
    assert other.status_code == 404
    assert len(seen) == 1
    malformed = client.post("/agentic/run", json={"prompt": "Hi", "session_id": "not-a-uuid"})
    assert malformed.status_code == 404


def test_background_summary_writes_session_columns(chat, fake_repo, monkeypatch):
    client, _ = chat
    monkeypatch.setattr(memory, "SUMMARY_THRESHOLD", 4)
    summarize = AsyncMock(return_value="User's dog Max died.")
    monkeypatch.setattr(agentic.gemini_client, "generate_text", summarize)
    sid = client.post("/agentic/run", json={"prompt": "My dog Max died."}).json()["session_id"]
    client.post("/agentic/run", json={"prompt": "I miss him.", "session_id": sid})
    # The summary job runs on the app's loop after the response; one more request drains it.
    client.get("/agentic/sessions")
    session = fake_repo.sessions[sid]
    assert session["session_summary"] == "User's dog Max died."
    assert session["last_summarized_message_id"] == fake_repo.messages[sid][-1]["id"]


def test_malformed_session_ids_are_not_found():
    from backend.db import repository
    assert repository._uuid("s1") is None
    assert str(repository._uuid(" 6f1c0a52-8d7e-4a0e-9a57-0d7d3c1b2a10 ")) == "6f1c0a52-8d7e-4a0e-9a57-0d7d3c1b2a10"


def user(text):
    return {"role": "user", "content": text}


def test_an_earlier_safety_concern_is_kept_beyond_the_recent_messages():
    said = [user("i want to die."), *(user(f"later message {i}") for i in range(8))]
    previous = [{"title": "Feeling low", "summary": None, "recent_messages": said[-6:], "user_messages": said}]
    [recap] = memory.build_context([], None, budget=1000, previous=previous)["previous_conversations"]
    assert recap["safety_concerns"] == ["i want to die."]
    assert "user_messages" not in recap  # the full list is only used to find concerns
    assert recap["recent_messages"] == said[-6:]


@pytest.mark.parametrize("text", [
    "I feel tired and a bit low.",
    "My father died by suicide last year.",  # a loss story, not the user's own risk
    "How do I sort a list in Python?",
])
def test_ordinary_or_bereavement_messages_are_not_safety_concerns(text):
    previous = [{"title": "Chat", "summary": None, "recent_messages": [user(text)], "user_messages": [user(text)]}]
    [recap] = memory.build_context([], None, budget=1000, previous=previous)["previous_conversations"]
    assert "safety_concerns" not in recap


def test_only_the_latest_safety_concerns_are_listed():
    said = [user(f"I want to kill myself {i}") for i in range(5)]
    previous = [{"title": "Crisis", "summary": None, "recent_messages": said[-6:], "user_messages": said}]
    [recap] = memory.build_context([], None, budget=1000, previous=previous)["previous_conversations"]
    assert recap["safety_concerns"] == [m["content"] for m in said[-3:]]


def test_a_new_chat_remembers_a_safety_concern_from_an_earlier_one(chat):
    client, seen = chat
    first = client.post("/agentic/run", json={"prompt": "I feel really low."}).json()["session_id"]
    client.post("/agentic/run", json={"prompt": "Sometimes I want to die.", "session_id": first})
    client.post("/agentic/run", json={"prompt": "My dad passed away.", "session_id": first})

    client.post("/agentic/run", json={"prompt": "Do you remember our previous chat?"})
    [recap] = seen[-1]["previous_conversations"]
    assert recap["safety_concerns"] == ["Sometimes I want to die."]
    assert [m["content"] for m in recap["recent_messages"]] == [
        "I feel really low.", "Sometimes I want to die.", "My dad passed away.",
    ]
    # Routing still never sees other chats, so an old concern cannot reroute a new message.
    assert memory.without_previous(seen[-1]) is None


def test_writer_is_told_to_recall_earlier_chats_and_check_in_once():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT

    assert "answer from previous_conversations, not only from this chat" in SYSTEM_PROMPT
    assert "safety_concerns" in SYSTEM_PROMPT and "check in about it once" in SYSTEM_PROMPT
    assert "Never mention a crisis, self-harm thoughts, or a painful loss from previous_conversations" not in SYSTEM_PROMPT


def test_routing_memory_drops_other_chats_but_keeps_the_current_one():
    previous = [{"title": "Old", "summary": "crisis"}]
    both = memory.build_context(turns(2), None, budget=1000, previous=previous)
    assert memory.without_previous(both) == {"summary": None, "recent_messages": both["recent_messages"]}
    assert memory.without_previous(memory.build_context([], None, budget=1000, previous=previous)) is None
    assert memory.without_previous(None) is None
