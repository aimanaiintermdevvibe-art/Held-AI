import asyncio
import json
from datetime import datetime, timedelta, timezone
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


def test_a_short_chat_is_sent_whole_without_its_summary():
    context = memory.build_context(turns(6), {"session_summary": "old", "total_message_count": 6})
    assert context["summary"] is None
    assert len(context["recent_messages"]) == 6


def test_a_long_chat_sends_its_summary_and_the_latest_20_messages():
    session = {"session_summary": "old", "last_summarized_message_id": "m89", "total_message_count": 100}
    context = memory.build_context(turns(100)[-20:], session)
    assert context["summary"] == "old"
    assert [m["content"] for m in context["recent_messages"]] == [m["content"] for m in turns(100)[-20:]]
    # Given more than 20 messages, only the latest 20 are sent.
    assert len(memory.build_context(turns(100), session)["recent_messages"]) == 20


def test_a_long_chat_without_summary_keeps_the_latest_20_messages():
    context = memory.build_context(turns(100), {})
    assert context["summary"] is None
    assert len(context["recent_messages"]) == 20


def test_a_very_long_message_keeps_its_start_and_end():
    text = "start " + "x" * 10_000 + " end"
    [kept] = memory.build_context([{"role": "user", "content": text}])["recent_messages"]
    assert kept["content"].startswith("start ") and kept["content"].endswith(" end")
    assert len(kept["content"]) < memory.MESSAGE_CHAR_LIMIT + 20


def test_no_history_means_no_memory():
    assert memory.build_context([], None) is None
    assert memory.with_memory("hi", None) == "hi"


def test_previous_conversations_are_included_even_for_a_new_chat():
    previous = [{"title": "Losing Max", "summary": None, "recent_messages": [{"role": "user", "content": "Max died."}]}]
    context = memory.build_context([], None, previous=previous)
    assert context == {"summary": None, "recent_messages": [], "previous_conversations": previous}
    assert "previous_conversations" not in memory.build_context(turns(2), None)


def test_later_replies_get_the_short_user_memory_instead():
    context = memory.build_context(turns(2), None, earlier="They have a dog called Max.")
    assert context["earlier_conversations"] == "They have a dog called Max."
    assert "previous_conversations" not in context
    assert memory.build_context([], None, earlier="They have a dog.")["earlier_conversations"] == "They have a dog."


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
    [recap] = memory.build_context([], None, previous=previous)["previous_conversations"]
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
    [recap] = memory.build_context([], None, previous=previous)["previous_conversations"]
    assert "safety_concerns" not in recap


def test_only_the_latest_safety_concerns_are_listed():
    said = [user(f"I want to kill myself {i}") for i in range(5)]
    previous = [{"title": "Crisis", "summary": None, "recent_messages": said[-6:], "user_messages": said}]
    [recap] = memory.build_context([], None, previous=previous)["previous_conversations"]
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
    assert memory.current_chat(seen[-1]) is None


def test_safety_concerns_reach_only_the_first_reply_of_a_chat():
    said = [user("I want to die")]
    previous = [{"title": "Crisis", "summary": None, "recent_messages": said, "user_messages": said}]
    [first] = memory.build_context([], None, previous=previous)["previous_conversations"]
    [later] = memory.build_context(turns(2), None, previous=previous)["previous_conversations"]
    assert first["safety_concerns"] == ["I want to die"]
    assert "safety_concerns" not in later
    assert later["recent_messages"] == []  # the writer cannot find it in the recap either


def test_a_later_turn_in_a_chat_gets_no_safety_concern(chat):
    client, seen = chat
    client.post("/agentic/run", json={"prompt": "Sometimes I want to die."})
    sid = client.post("/agentic/run", json={"prompt": "Hello"}).json()["session_id"]
    assert "safety_concerns" in seen[-1]["previous_conversations"][0]
    client.post("/agentic/run", json={"prompt": "I'm stressed", "session_id": sid})
    assert "previous_conversations" not in seen[-1]
    assert "want to die" not in json.dumps(seen[-1])


@pytest.mark.parametrize("days_ago, raised", [(0, True), (5, True), (7, False), (30, False)])
def test_safety_concerns_older_than_six_days_are_not_raised(days_ago, raised):
    sent = datetime.now(timezone.utc) - timedelta(days=days_ago)
    said = [{**user("I want to die"), "created_at": sent.isoformat()}]
    previous = [{"title": "Crisis", "summary": None, "recent_messages": said, "user_messages": said}]
    [recap] = memory.build_context([], None, previous=previous)["previous_conversations"]
    assert ("safety_concerns" in recap) is raised
    assert (recap["recent_messages"] == said) is raised


def test_hiding_a_concern_keeps_the_rest_of_the_recap():
    said = [user("I want to die"), user("I got a new cat called Marigold.")]
    summary = "They spoke of dying. They got a cat."
    previous = [{"title": "Chat", "summary": summary, "recent_messages": said, "user_messages": said}]
    [recap] = memory.build_context(turns(2), None, previous=previous)["previous_conversations"]
    assert recap["recent_messages"] == [said[1]]
    assert recap["summary"] == "They got a cat."
    [first] = memory.build_context([], None, previous=previous)["previous_conversations"]
    assert first["summary"] == summary


CHECKED_IN = {"recent_messages": [{"role": "assistant", "content": "I want to check in: **are you safe right now?**"}]}


@pytest.mark.parametrize("draft, memory_so_far, repeats", [
    ("Are you safe right now?", CHECKED_IN, True),
    ("You shared thoughts of dying before, so I wanted to ask.", CHECKED_IN, True),
    ("Work sounds exhausting this week.", CHECKED_IN, False),
    ("This is a safe place to talk.", CHECKED_IN, False),
    ("Are you safe right now?", {"recent_messages": [{"role": "assistant", "content": "Hello."}]}, False),
    ("Are you safe right now?", None, False),
])
def test_a_repeated_safety_check_in_is_detected(draft, memory_so_far, repeats):
    assert memory.repeats_safety_check_in(draft, memory_so_far) is repeats


def test_a_past_tense_disclosure_counts_as_a_safety_concern():
    said = [user("i wanted to die")]
    previous = [{"title": "Chat", "summary": None, "recent_messages": said, "user_messages": said}]
    [first] = memory.build_context([], None, previous=previous)["previous_conversations"]
    [later] = memory.build_context(turns(2), None, previous=previous)["previous_conversations"]
    assert first["safety_concerns"] == ["i wanted to die"]
    assert later["recent_messages"] == []


def test_a_summary_cannot_raise_a_concern_the_safety_check_never_flagged():
    said = [user("Thanks for listening.")]
    previous = [{"title": "Chat", "summary": "They asked about safety.", "recent_messages": said, "user_messages": said}]
    [recap] = memory.build_context([], None, previous=previous)["previous_conversations"]
    assert recap["summary"] is None and "safety_concerns" not in recap


def test_writer_is_told_to_leave_past_hardships_alone_unless_the_user_raises_them():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT

    assert "Past hardships are background, not topics." in SYSTEM_PROMPT
    assert "only when they help with the latest message" not in SYSTEM_PROMPT


def test_writer_is_told_to_recall_earlier_chats_and_check_in_once():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT

    assert "answer from previous_conversations or earlier_conversations, not only from this chat" in SYSTEM_PROMPT
    assert "safety_concerns" in SYSTEM_PROMPT and "check in about it once" in SYSTEM_PROMPT
    assert "Never mention a crisis, self-harm thoughts, or a painful loss from previous_conversations" not in SYSTEM_PROMPT


def test_routing_memory_drops_other_chats_but_keeps_the_current_one():
    previous = [{"title": "Old", "summary": "crisis"}]
    both = memory.build_context(turns(2), None, previous=previous)
    assert memory.current_chat(both) == {"summary": None, "recent_messages": both["recent_messages"]}
    assert memory.current_chat(memory.build_context([], None, previous=previous)) is None
    assert memory.current_chat(memory.build_context(turns(2), None, earlier="They have a cat.")) == {
        "summary": None, "recent_messages": both["recent_messages"],
    }
    assert memory.current_chat(None) is None
    assert len(memory.current_chat(memory.build_context(turns(10)), 6)["recent_messages"]) == 6


def test_a_long_chats_summary_also_stops_a_repeated_check_in():
    long_chat = {"summary": "They felt low. The assistant already checked on the user's safety.", "recent_messages": []}
    assert memory.repeats_safety_check_in("Are you safe right now?", long_chat) is True
    assert memory.repeats_safety_check_in("Work sounds hard.", long_chat) is False
    assert "checked on the user's safety" in memory.SUMMARY_PROMPT


def test_the_auditor_sees_the_users_side_only():
    context = memory.build_context(turns(6), None, earlier="They have a cat.")
    view = memory.audit_view(context)
    assert [m["role"] for m in view["recent_messages"]] == ["user"] * 3
    assert view["earlier_conversations"] == "They have a cat."
    assert memory.audit_view(None) is None


@pytest.mark.parametrize("session, known_count, pending", [
    ({"last_summarized_message_id": "m5", "total_message_count": 12}, 12, 6),  # anchor in hand
    ({"last_summarized_message_id": None, "total_message_count": 4}, 4, 4),     # short chat, all in hand
    ({"last_summarized_message_id": None, "total_message_count": 30}, 22, None),  # older ones not loaded
    ({"last_summarized_message_id": "old", "total_message_count": 40}, 22, None),  # anchor not loaded
])
def test_unsummarized_messages_come_from_the_messages_in_hand(session, known_count, pending):
    found = memory.unsummarized(session, turns(known_count))
    assert (None if found is None else len(found)) == pending
    assert memory.summary_due(session, turns(known_count)) is (pending is None or pending >= memory.SUMMARY_THRESHOLD)


def test_the_user_memory_leaves_out_risk_talk():
    client = FakeClient("They lost their dog Max. They spoke about suicide. Walks help them.")
    said = [user("I want to die"), user("My dog Max died.")]
    previous = [{"title": "Max", "summary": None, "recent_messages": said, "user_messages": said}]
    remembered = asyncio.run(memory.build_user_memory(client, Settings(_env_file=None), previous))
    assert remembered == "They lost their dog Max. Walks help them."
    # A concern is never handed to the memory writer in the first place.
    assert "want to die" not in json.dumps(client.inputs)
    assert asyncio.run(memory.build_user_memory(client, Settings(_env_file=None), [])) is None


def test_each_turn_reads_and_writes_the_chat_once(chat, fake_repo):
    client, _ = chat
    sid = client.post("/agentic/run", json={"prompt": "Hello"}).json()["session_id"]
    assert fake_repo.ops == ["create_chat_session", "get_previous_conversations", "save_turn"]

    fake_repo.ops.clear()
    client.post("/agentic/run", json={"prompt": "Work is hard.", "session_id": sid})
    client.get("/agentic/sessions")  # lets any background job finish
    # One read (session + latest messages + user memory) and one write (both messages and the count).
    assert fake_repo.ops == ["open_chat", "save_turn"]


def test_a_new_chat_builds_the_user_memory_that_later_replies_use(chat, fake_repo, monkeypatch):
    client, seen = chat
    monkeypatch.setattr(agentic.gemini_client, "generate_text", AsyncMock(return_value="Their dog Max died."))
    client.post("/agentic/run", json={"prompt": "My dog Max died."})

    sid = client.post("/agentic/run", json={"prompt": "Hi again"}).json()["session_id"]
    assert seen[-1]["previous_conversations"][0]["recent_messages"][0]["content"] == "My dog Max died."
    client.get("/agentic/sessions")  # lets the background job finish
    assert fake_repo.user_memory == {"usr_1": "Their dog Max died."}

    client.post("/agentic/run", json={"prompt": "Do you remember Max?", "session_id": sid})
    assert seen[-1]["earlier_conversations"] == "Their dog Max died."
    assert "previous_conversations" not in seen[-1]


def test_a_summary_that_fell_behind_reads_the_messages_it_needs(chat, fake_repo, monkeypatch):
    client, _ = chat
    monkeypatch.setattr(agentic, "RECENT_MESSAGES", 2)
    sid = client.post("/agentic/run", json={"prompt": "one"}).json()["session_id"]
    for prompt in ("two", "three", "four", "five"):
        client.post("/agentic/run", json={"prompt": prompt, "session_id": sid})
    # Every summary attempt so far failed (no Gemini in tests), so ten messages wait.
    assert fake_repo.sessions[sid]["last_summarized_message_id"] is None

    monkeypatch.setattr(agentic.gemini_client, "generate_text", AsyncMock(return_value="Counting turns."))
    fake_repo.ops.clear()
    client.post("/agentic/run", json={"prompt": "six", "session_id": sid})
    client.get("/agentic/sessions")
    assert fake_repo.ops == ["open_chat", "save_turn", "get_chat_session", "get_messages_after", "save_session_summary"]
    session = fake_repo.sessions[sid]
    assert session["session_summary"] == "Counting turns."
    assert session["last_summarized_message_id"] == fake_repo.messages[sid][-1]["id"]


def test_the_router_also_sees_the_users_earlier_messages():
    history = turns(20)  # m0 (user) .. m19 (assistant)
    view = memory.routing_view(memory.build_context(history))
    contents = [m["content"] for m in view["recent_messages"]]
    expected = [m["content"] for m in history[:14] if m["role"] == "user"] + [m["content"] for m in history[14:]]
    assert contents == expected
    assert len(view["recent_messages"]) == 7 + 6
    assert memory.routing_view(None) is None


def test_the_summary_keeps_facts_and_advice_already_given_apart():
    for part in ("Facts:", "Feelings and goals:", "Already suggested:", "checked on the user's safety"):
        assert part in memory.SUMMARY_PROMPT


ASKED_WHO = {"role": "assistant", "content": "**Is there someone nearby who can be with you right now?**"}
WHO_QUESTION = "Is there anyone who can be with you right now?"


@pytest.mark.parametrize("draft, recent, prompt, summary, answered", [
    (WHO_QUESTION, [ASKED_WHO, user("yes my friend is with me")], "I still feel like doing it.", None, "yes my friend is with me"),
    (WHO_QUESTION, [ASKED_WHO, user("yes")], "I still want to.", None, "yes"),
    (WHO_QUESTION, [user("I'm alone tonight")], "I can't do this", None, "I'm alone tonight"),
    (WHO_QUESTION, [ASKED_WHO], "she is sitting right next to me", None, "she is sitting right next to me"),
    (WHO_QUESTION, [ASKED_WHO], "no", None, "no"),
    (WHO_QUESTION, [], "I still feel it.", "Facts: none. Safety answers: their sister is with them at home.", "Safety answers: their sister is with them at home."),
    (WHO_QUESTION, [user("please stay with me")], "I feel empty.", None, None),
    (WHO_QUESTION, [user("I feel empty")], "I want to end it.", None, None),
    ("With your friend beside you, could you tell them how tonight feels?", [user("my friend is with me")], "ok", None, None),
    (WHO_QUESTION, [], "I want to end my life.", None, None),
])
def test_a_crisis_reply_may_not_reask_who_is_with_the_user(draft, recent, prompt, summary, answered):
    assert memory.reasks_who_is_with_them(draft, {"recent_messages": recent, "summary": summary}, prompt) == answered


def test_safety_answers_are_kept_in_the_summary_and_hidden_from_later_chats():
    assert "Safety answers:" in memory.SUMMARY_PROMPT
    assert memory._without_safety_talk("They felt low. Safety answers: their friend is with them.") == "They felt low."


def test_writer_is_told_not_to_reask_answered_safety_questions():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT

    assert "Never ask again about something they have already answered" in SYSTEM_PROMPT
    assert "never reuse an earlier closing" in SYSTEM_PROMPT
    assert "and whether someone can be with them. Do not reuse" not in SYSTEM_PROMPT  # the old, unconditional ask


def test_no_agent_is_shown_a_gender_the_user_never_gave():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT as WRITER
    from backend.ai_wrapper.agents.agent4_compliance_auditor.prompt import SYSTEM_PROMPT as AUDITOR

    assert "Never assume a detail the user has not given, such as a person's gender" in WRITER
    assert "could you tell them how heavy tonight feels" in WRITER and "tell her how heavy" not in WRITER
    assert "a gender, name or other detail about a person that the user never gave" in AUDITOR


def _said(text, minutes_ago):
    from datetime import datetime, timedelta, timezone
    return {"role": "user", "content": text, "created_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()}


def test_a_concern_is_raised_once_for_the_whole_account():
    crisis = _said("I am going to kill myself.", 60)
    crisis_chat = {"title": "Crisis", "summary": None, "recent_messages": [crisis], "user_messages": [crisis]}
    # The first new chat after the crisis raises it and then remembers how far its check-in went.
    [first] = memory.build_context([], None, previous=[crisis_chat])["previous_conversations"]
    assert first["safety_concerns"] == ["I am going to kill myself."]
    until = memory.newest_concern_to_raise([crisis_chat])
    assert until == crisis["created_at"]
    # Later new chats see that mark on the chat that checked in, and raise nothing.
    checked = {"title": "Work", "summary": None, "recent_messages": [], "user_messages": [], "safety_raised_until": until}
    later = memory.build_context([], None, previous=[checked, crisis_chat])["previous_conversations"]
    assert all("safety_concerns" not in r for r in later)
    assert all("safety_raised_until" not in r for r in later)  # internal, never shown to the agents
    assert memory.newest_concern_to_raise([checked, crisis_chat]) is None


def test_a_new_concern_after_the_check_in_is_raised_again():
    old, new = _said("I want to die", 120), _said("I want to end my life", 5)
    checked = {"title": "Work", "summary": None, "recent_messages": [], "user_messages": [], "safety_raised_until": old["created_at"]}
    chats = [{"title": "Again", "summary": None, "recent_messages": [new], "user_messages": [new]}, checked,
             {"title": "First", "summary": None, "recent_messages": [old], "user_messages": [old]}]
    recaps = memory.build_context([], None, previous=chats)["previous_conversations"]
    assert recaps[0]["safety_concerns"] == ["I want to end my life"] and "safety_concerns" not in recaps[2]
    assert memory.newest_concern_to_raise(chats) == new["created_at"]


@pytest.mark.parametrize("reply, asks", [
    ("Last time you shared thoughts of dying. **Are you safe right now?**", True),
    ("I want to check in on how you are feeling and whether you are safe.", True),
    ("Night shifts are brutal. **What helps you unwind?**", False),
])
def test_a_check_in_is_recognised(reply, asks):
    assert memory.asks_about_safety(reply) is asks


def test_writer_asks_about_ambiguous_words_and_bolds_crisis_questions():
    from backend.ai_wrapper.agents.agent3_empathy_synthesizer.prompt import SYSTEM_PROMPT

    assert '"I feel high"' in SYSTEM_PROMPT and "never assume drug use or a crisis" in SYSTEM_PROMPT
    assert "Bold every question you ask, as in all replies." in SYSTEM_PROMPT

