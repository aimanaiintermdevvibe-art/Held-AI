"""Held access tokens the agent cannot verify locally, and Held users new to the agent's database."""
import time
from datetime import datetime
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.api.routers import agentic
from backend.core.auth import HeldUnavailable
from backend.db.repository import user_values_from_profile
from backend.main import app

TEST_SECRET = "test-secret"  # the agent's JWT_SECRET under the auth fixture
HELD_ONLY_SECRET = "a-held-signing-key-this-agent-does-not-have"
NEW_USER = {
    "id": "usr_new", "email": "new@example.test", "fullName": "New User", "authProvider": "EMAIL",
    "provider": None, "providerId": None, "emailVerified": True, "onboardingComplete": True,
    "role": "CAREGIVER", "relation": "PARTNER", "relationOther": None, "username": "new_user",
    "dob": "1990-05-01", "pseudoName": None, "diseases": [{"id": "d1", "name": "Depression"}],
}


def bearer(sub, secret=HELD_ONLY_SECRET, expires_in=900):
    claims = {"sub": sub, "role": "SELF", "iat": int(time.time()), "exp": int(time.time()) + expires_in}
    return {"Authorization": f"Bearer {jwt.encode(claims, secret, algorithm='HS256')}"}


@pytest.fixture
def held_me(monkeypatch):
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr("backend.core.auth.held_me", lookup)
    return lookup


@pytest.fixture
def client(fake_repo, auth, held_me):  # auth sets the agent's JWT_SECRET to TEST_SECRET
    with TestClient(app) as test_client:
        yield test_client


def test_token_signed_with_the_shared_secret_needs_no_held_call(client, held_me):
    res = client.post("/agentic/users/register", headers=bearer("usr_1", TEST_SECRET))
    assert res.status_code == 200
    assert res.json()["id"] == "usr_1"
    held_me.assert_not_awaited()


def test_held_confirms_a_token_signed_with_another_key(client, held_me, fake_repo):
    held_me.return_value = {**NEW_USER, "id": "usr_1"}
    headers = bearer("usr_1")
    assert client.post("/agentic/users/register", headers=headers).status_code == 200
    held_me.assert_awaited_once_with(headers["Authorization"].removeprefix("Bearer "))
    assert fake_repo.created_from_profiles == []  # usr_1 already exists


def test_registration_adds_a_new_held_user_to_the_database(client, held_me, fake_repo):
    held_me.return_value = NEW_USER
    res = client.post("/agentic/users/register", headers=bearer("usr_new"))
    assert res.status_code == 200
    assert res.json()["id"] == "usr_new"
    assert fake_repo.created_from_profiles == [NEW_USER]
    assert fake_repo.users["usr_new"] == "CAREGIVER"

    # The next request finds the user and does not add them again.
    assert client.get("/agentic/sessions", headers=bearer("usr_new")).status_code == 200
    assert len(fake_repo.created_from_profiles) == 1


def test_new_user_with_a_locally_verified_token_is_added_from_their_profile(client, held_me, fake_repo):
    held_me.return_value = NEW_USER
    assert client.post("/agentic/users/register", headers=bearer("usr_new", TEST_SECRET)).status_code == 200
    assert fake_repo.created_from_profiles == [NEW_USER]


def test_unknown_user_without_a_held_profile_is_rejected(client, fake_repo):
    res = client.post("/agentic/users/register", headers=bearer("usr_ghost", TEST_SECRET))
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "USER_NOT_FOUND"
    assert fake_repo.created_from_profiles == []


@pytest.mark.parametrize("held_answer", [None, {**NEW_USER, "id": "someone-else"}])
def test_token_held_does_not_confirm_is_invalid(client, held_me, held_answer):
    held_me.return_value = held_answer
    res = client.post("/agentic/users/register", headers=bearer("usr_new"))
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "TOKEN_INVALID"


def test_expired_token_is_reported_as_expired_without_asking_held(client, held_me):
    res = client.post("/agentic/users/register", headers=bearer("usr_1", expires_in=-5))
    assert res.json()["detail"]["code"] == "TOKEN_EXPIRED"
    held_me.assert_not_awaited()


def test_a_confirmed_token_is_not_rechecked_for_a_minute(client, held_me, monkeypatch):
    held_me.return_value = {**NEW_USER, "id": "usr_1"}
    headers = bearer("usr_1")
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    held_me.assert_awaited_once()

    from backend.core import auth
    expires, user_id, profile = auth._confirmed_tokens[headers["Authorization"].removeprefix("Bearer ")]
    auth._confirmed_tokens[headers["Authorization"].removeprefix("Bearer ")] = (0.0, user_id, profile)  # a minute later
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    assert held_me.await_count == 2


def test_a_rejected_token_is_not_remembered(client, held_me):
    headers = bearer("usr_1")
    assert client.get("/agentic/sessions", headers=headers).status_code == 401
    assert client.get("/agentic/sessions", headers=headers).status_code == 401
    assert held_me.await_count == 2


def test_held_outage_is_a_temporary_failure(client, held_me):
    held_me.side_effect = HeldUnavailable("connection refused")
    assert client.post("/agentic/users/register", headers=bearer("usr_1")).status_code == 503


def test_the_confirming_profile_is_reused_for_the_reply(client, held_me, monkeypatch):
    held_me.return_value = {**NEW_USER, "id": "usr_1"}
    lookup = AsyncMock(return_value={})
    seen = []

    async def run(*, prompt, memory=None, profile=None, documents=None, notes=None):
        seen.append(profile)
        return "Answer"

    monkeypatch.setattr(agentic, "fetch_user_profile", lookup)
    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    assert client.post("/agentic/run", json={"prompt": "Hi"}, headers=bearer("usr_1")).status_code == 200
    assert seen == [{**NEW_USER, "id": "usr_1"}]
    lookup.assert_not_awaited()
    held_me.assert_awaited_once()


def test_held_profile_maps_to_this_databases_columns():
    values = user_values_from_profile(NEW_USER)
    assert values == {
        "id": "usr_new", "email": "new@example.test", "fullName": "New User", "authProvider": "EMAIL",
        "provider": None, "providerId": None, "emailVerified": True, "onboardingComplete": True,
        "role": "CAREGIVER", "dob": datetime(1990, 5, 1), "username": "new_user", "pseudoName": None,
        "relation": "SPOUSE", "relationOther": None,
    }


@pytest.mark.parametrize("held_role, stored", [("SELF", "USER"), ("CAREGIVER", "CAREGIVER"), ("ADMIN", None), (None, None)])
def test_held_roles_never_grant_admin(held_role, stored):
    assert user_values_from_profile({**NEW_USER, "role": held_role})["role"] == stored


def test_other_relation_and_oauth_provider_are_kept_together():
    values = user_values_from_profile({
        **NEW_USER, "relation": "OTHER", "relationOther": "Neighbour", "authProvider": "GOOGLE",
        "provider": "GOOGLE", "providerId": "g-123", "dob": "2001-02-03T04:05:06.000Z",
    })
    assert (values["relation"], values["relationOther"]) == ("OTHER", "Neighbour")
    assert (values["authProvider"], values["provider"], values["providerId"]) == ("GOOGLE", "GOOGLE", "g-123")
    assert values["dob"] == datetime(2001, 2, 3, 4, 5, 6)
    unpaired = user_values_from_profile({**NEW_USER, "provider": "GOOGLE", "providerId": None})
    assert (unpaired["provider"], unpaired["providerId"]) == (None, None)
