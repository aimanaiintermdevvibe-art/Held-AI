"""The four reply modes, picked from the user's onboarding answers in the Held database."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.ai_wrapper.profile import user_context
from backend.api.routers import agentic
from backend.main import app


def profile(role, diseases=(), relation=None, other=None):
    """A profile as backend.db.repository.get_user_profile returns it."""
    return {"id": "usr_1", "role": role, "relation": relation, "relationOther": other, "onboardingComplete": True,
            "diseases": [{"id": f"d{i}", "name": name} for i, name in enumerate(diseases)]}


@pytest.mark.parametrize("row, mode, extra", [
    (profile("SELF", ["Anxiety Disorder"]), "myself_diagnosed", {"user_diagnosed_conditions": ["Anxiety Disorder"]}),
    (profile("SELF"), "myself_undiagnosed", {}),
    (profile("CAREGIVER", ["Dementia"], "PARENT"), "caregiver_diagnosed",
     {"cares_for": "parent", "cared_for_person_diagnosed_conditions": ["Dementia"]}),
    (profile("CAREGIVER", [], "OTHER", "my aunt"), "caregiver_undiagnosed", {"cares_for": "my aunt"}),
])
def test_each_database_profile_picks_its_mode(row, mode, extra):
    context = user_context(row)
    assert context["mode"] == mode
    for key, value in extra.items():
        assert context[key] == value


@pytest.mark.parametrize("row", [profile(None), profile("ADMIN"), None])
def test_users_without_onboarding_get_no_mode(row):
    assert user_context(row) is None


@pytest.fixture
def seen_profiles(fake_repo, auth, monkeypatch):
    """Run chats as usr_1 and record the profile each reply was written with."""
    seen = []

    async def run(*, prompt, memory=None, profile=None, notes=None, recall=None, note_tools=None, is_note=False):
        seen.append(profile)
        return "Answer"

    held_lookup = AsyncMock(return_value={"id": "usr_1", "role": "SELF", "diseases": []})
    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    monkeypatch.setattr(agentic, "fetch_user_profile", held_lookup)
    with TestClient(app, headers=auth("usr_1")) as client:
        yield client, seen, held_lookup


def test_the_profile_comes_from_the_database(seen_profiles, fake_repo):
    client, seen, held_lookup = seen_profiles
    fake_repo.profiles["usr_1"] = profile("CAREGIVER", ["Dementia"], "PARENT")
    assert client.post("/agentic/run", json={"prompt": "Hi"}).status_code == 200
    assert user_context(seen[0])["mode"] == "caregiver_diagnosed"
    held_lookup.assert_not_awaited()  # no call to Held's API


@pytest.mark.parametrize("db_answer", [None, PermissionError("no access to the disease tables")])
def test_helds_api_is_the_fallback(seen_profiles, fake_repo, db_answer):
    client, seen, held_lookup = seen_profiles
    if db_answer is not None:
        fake_repo.profiles["usr_1"] = db_answer
    assert client.post("/agentic/run", json={"prompt": "Hi"}).status_code == 200
    held_lookup.assert_awaited_once()
    assert user_context(seen[0])["mode"] == "myself_undiagnosed"
