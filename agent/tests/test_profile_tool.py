"""The profile tool: read once per chat, through the tool layer, only what picks the mode."""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.ai_wrapper.profile import user_context
from backend.api.routers import agentic
from backend.db import repository
from backend.main import app
from backend.tools import user_profile
from backend.tools.user_profile import UserProfileTool, mode_fields

DB_PROFILE = {"id": "usr_1", "role": "CAREGIVER", "relation": "PARENT", "relationOther": None,
              "onboardingComplete": True, "email": "one@example.test", "fullName": "One",
              "diseases": [{"id": "d1", "name": "Dementia"}]}


def test_only_the_mode_fields_are_returned():
    assert mode_fields(DB_PROFILE) == {"role": "CAREGIVER", "relation": "PARENT", "onboardingComplete": True,
                                       "diseases": [{"name": "Dementia"}]}
    assert mode_fields({}) is None and mode_fields(None) is None
    assert user_context(mode_fields(DB_PROFILE))["mode"] == "caregiver_diagnosed"


def test_the_tool_is_bound_to_the_token_user_and_logs_no_conditions(monkeypatch):
    asked = []

    async def get_user_profile(user_id):
        asked.append(user_id)
        return dict(DB_PROFILE)

    monkeypatch.setattr(repository, "get_user_profile", get_user_profile)
    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    capture = Capture()
    user_profile.logger.addHandler(capture)
    try:
        assert asyncio.run(UserProfileTool("usr_1").run())["role"] == "CAREGIVER"
    finally:
        user_profile.logger.removeHandler(capture)
    assert asked == ["usr_1"]
    assert "conditions=1" in lines[0] and "Dementia" not in lines[0]
    with pytest.raises(ValueError):
        UserProfileTool("")


@pytest.fixture
def chats(fake_repo, auth, monkeypatch):
    """Chats as usr_1, recording the profile each reply was written with and each database read."""
    seen, reads = [], []

    async def run(*, prompt, memory=None, profile=None, notes=None, recall=None, note_tools=None, is_note=False):
        seen.append(profile)
        return "Reply"

    async def get_user_profile(user_id):
        reads.append(user_id)
        found = fake_repo.profiles.get(user_id)
        if isinstance(found, Exception):
            raise found
        return dict(found) if found else None

    held = AsyncMock(return_value={"id": "usr_1", "role": "SELF", "diseases": []})
    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    monkeypatch.setattr(agentic, "fetch_user_profile", held)
    monkeypatch.setattr(repository, "get_user_profile", get_user_profile)
    with TestClient(app, headers=auth("usr_1")) as client:
        yield client, seen, reads, held


def test_the_profile_is_read_once_per_chat(chats, fake_repo):
    client, seen, reads, held = chats
    fake_repo.profiles["usr_1"] = dict(DB_PROFILE)
    sid = client.post("/agentic/run", json={"prompt": "Hi"}).json()["session_id"]
    client.post("/agentic/run", json={"prompt": "Mum forgot my name today", "session_id": sid})
    client.post("/agentic/stream", json={"prompt": "Thanks", "session_id": sid})
    assert reads == ["usr_1"]  # one read for the whole chat
    assert [user_context(p)["mode"] for p in seen] == ["caregiver_diagnosed"] * 3
    assert fake_repo.chat_profiles[sid]["role"] == "CAREGIVER" and "email" not in fake_repo.chat_profiles[sid]
    held.assert_not_awaited()
    client.post("/agentic/run", json={"prompt": "New topic"})
    assert reads == ["usr_1", "usr_1"]  # a new chat reads it afresh


def test_a_profile_change_applies_from_the_next_new_chat(chats, fake_repo):
    client, seen, reads, _ = chats
    fake_repo.profiles["usr_1"] = dict(DB_PROFILE)
    sid = client.post("/agentic/run", json={"prompt": "Hi"}).json()["session_id"]
    fake_repo.profiles["usr_1"] = {**DB_PROFILE, "diseases": []}  # the user edits their profile
    client.post("/agentic/run", json={"prompt": "Still here", "session_id": sid})
    client.post("/agentic/run", json={"prompt": "A new chat"})
    assert [user_context(p)["mode"] for p in seen] == ["caregiver_diagnosed", "caregiver_diagnosed", "caregiver_undiagnosed"]


def test_the_kept_profile_is_never_shown_to_the_app(chats, fake_repo):
    client, *_ = chats
    fake_repo.profiles["usr_1"] = dict(DB_PROFILE)
    data = client.post("/agentic/run", json={"prompt": "Hi"}).json()
    listed = client.get("/agentic/sessions").json()
    sessions = listed if isinstance(listed, list) else listed.get("sessions", [])
    for session in [data["session"], *sessions]:
        assert "memory" not in (session.get("metadata") or {}) and "profile" not in (session.get("metadata") or {})


def test_an_older_chat_without_a_kept_profile_reads_it_once(chats, fake_repo):
    client, seen, reads, _ = chats
    fake_repo.profiles["usr_1"] = dict(DB_PROFILE)
    sid = client.post("/agentic/run", json={"prompt": "Hi"}).json()["session_id"]
    fake_repo.chat_profiles.pop(sid)  # a chat from before profiles were kept
    client.post("/agentic/run", json={"prompt": "Again", "session_id": sid})
    client.post("/agentic/run", json={"prompt": "And again", "session_id": sid})
    assert reads == ["usr_1", "usr_1"] and sid in fake_repo.chat_profiles


def test_helds_api_is_the_fallback_and_an_outage_is_not_kept(chats, fake_repo):
    client, seen, reads, held = chats
    fake_repo.profiles["usr_1"] = RuntimeError("database down")
    sid = client.post("/agentic/run", json={"prompt": "Hi"}).json()["session_id"]
    assert user_context(seen[0])["mode"] == "myself_undiagnosed" and fake_repo.chat_profiles[sid]["role"] == "SELF"
    held.return_value = {}  # Held unreachable too
    other = client.post("/agentic/run", json={"prompt": "New"}).json()["session_id"]
    assert other not in fake_repo.chat_profiles  # nothing kept, so the next message tries again
