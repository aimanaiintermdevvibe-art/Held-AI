"""Held access tokens: verified locally with JWT_SECRET, or confirmed with Held. The token's
user must already exist in the Held database; this service never adds users."""
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.api.routers import agentic
from backend.core.auth import HeldUnavailable
from backend.main import app
from backend.tools.user_profile import mode_fields

TEST_SECRET = "test-secret"  # the agent's JWT_SECRET under the auth fixture
HELD_ONLY_SECRET = "a-held-signing-key-this-agent-does-not-have"
PROFILE = {"id": "usr_1", "email": "one@example.test", "role": "CAREGIVER", "relation": "PARTNER", "diseases": []}


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
    assert client.get("/agentic/sessions", headers=bearer("usr_1", TEST_SECRET)).status_code == 200
    held_me.assert_not_awaited()


def test_held_confirms_a_token_signed_with_another_key(client, held_me):
    held_me.return_value = PROFILE
    headers = bearer("usr_1")
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    held_me.assert_awaited_once_with(headers["Authorization"].removeprefix("Bearer "))


@pytest.mark.parametrize("secret", [TEST_SECRET, HELD_ONLY_SECRET])
def test_a_user_missing_from_the_database_is_rejected_not_added(client, held_me, fake_repo, secret):
    held_me.return_value = {**PROFILE, "id": "usr_new"}
    res = client.get("/agentic/sessions", headers=bearer("usr_new", secret))
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "USER_NOT_FOUND"
    assert "usr_new" not in fake_repo.users


@pytest.mark.parametrize("held_answer", [None, {**PROFILE, "id": "someone-else"}])
def test_token_held_does_not_confirm_is_invalid(client, held_me, held_answer):
    held_me.return_value = held_answer
    res = client.get("/agentic/sessions", headers=bearer("usr_1"))
    assert res.status_code == 401
    assert res.json()["detail"]["code"] == "TOKEN_INVALID"


def test_expired_token_is_reported_as_expired_without_asking_held(client, held_me):
    res = client.get("/agentic/sessions", headers=bearer("usr_1", expires_in=-5))
    assert res.json()["detail"]["code"] == "TOKEN_EXPIRED"
    held_me.assert_not_awaited()


def test_a_confirmed_token_is_not_rechecked_for_a_minute(client, held_me):
    held_me.return_value = PROFILE
    headers = bearer("usr_1")
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    held_me.assert_awaited_once()

    from backend.core import auth
    token = headers["Authorization"].removeprefix("Bearer ")
    _, user_id, profile = auth._confirmed_tokens[token]
    auth._confirmed_tokens[token] = (0.0, user_id, profile)  # a minute later
    assert client.get("/agentic/sessions", headers=headers).status_code == 200
    assert held_me.await_count == 2


def test_a_rejected_token_is_not_remembered(client, held_me):
    headers = bearer("usr_1")
    assert client.get("/agentic/sessions", headers=headers).status_code == 401
    assert client.get("/agentic/sessions", headers=headers).status_code == 401
    assert held_me.await_count == 2


def test_held_outage_is_a_temporary_failure(client, held_me):
    held_me.side_effect = HeldUnavailable("connection refused")
    assert client.get("/agentic/sessions", headers=bearer("usr_1")).status_code == 503


def test_the_confirming_profile_is_reused_for_the_reply(client, held_me, monkeypatch):
    held_me.return_value = PROFILE
    lookup = AsyncMock(return_value={})
    seen = []

    async def run(*, prompt, memory=None, profile=None, notes=None, recall=None, note_tools=None, is_note=False):
        seen.append(profile)
        return "Answer"

    monkeypatch.setattr(agentic, "fetch_user_profile", lookup)
    monkeypatch.setattr(agentic.pipeline, "run", run)
    monkeypatch.setattr(agentic, "generate_title", AsyncMock(return_value="Title"))
    assert client.post("/agentic/run", json={"prompt": "Hi"}, headers=bearer("usr_1")).status_code == 200
    assert seen == [mode_fields(PROFILE)]  # only what picks the mode reaches the agents
    lookup.assert_not_awaited()
    held_me.assert_awaited_once()


def test_there_is_no_registration_endpoint(client):
    assert client.post("/agentic/users/register", headers=bearer("usr_1", TEST_SECRET)).status_code in (404, 405)
