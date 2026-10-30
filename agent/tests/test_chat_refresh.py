import io
import json
import sys
import time
import urllib.error
from pathlib import Path

import httpx
import jwt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import chat
from backend.core import user_accounts
from backend.core.held_client import HeldAuthClient
from backend.core.user_accounts import UserAccountFile, user_file_for


class FakeResponse:
    def __init__(self, lines=()):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def __iter__(self):
        return iter(self.lines)

    def read(self):
        return b"".join(self.lines)


ANSWER = (b'data: {"text":"Hello"}\n', b"\n", b'data: {"done":true}\n', b"\n")
PROFILE = {
    "id": "usr_2", "email": "two@example.test", "fullName": "User Two", "username": "user_two",
    "onboardingComplete": True, "role": "CAREGIVER", "relation": "PARENT", "relationOther": None,
    "diseases": [{"id": "d1", "name": "Depression"}], "avatarUrl": None,
}


def token(expires_in=900):
    return jwt.encode({"sub": "usr_1", "exp": int(time.time()) + expires_in}, "k" * 32, algorithm="HS256")


def write_account(number, **fields):
    path = user_accounts.USERS_DIR / f"user{number}.json"  # a temp dir under the autouse fixture
    path.write_text(json.dumps({
        "label": f"User {number}", "email": f"user{number}@example.test", "password": "secret1",
        "held_api_base_url": "http://held.test/api", "user_id": "", "access_token": "", "refresh_token": "",
        **fields,
    }), encoding="utf-8")
    return path


@pytest.fixture
def held(monkeypatch):
    """The selected account's Held session, on a temporary account file and a fake Held API."""
    refreshed = token(expires_in=1000)  # distinct from the stored token
    held_requests = []

    def handler(request):
        held_requests.append(request.url.path)
        return httpx.Response(200, json={"data": {"accessToken": refreshed, "refreshToken": "refresh-1"}})

    path = write_account(1, access_token=token(), refresh_token="refresh-0")
    client = HeldAuthClient(UserAccountFile(path), httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(chat, "held", client)
    monkeypatch.setattr(chat, "CURRENT_SESSION_ID", None)
    client.refreshed_token = refreshed
    client.held_requests = held_requests
    return client


@pytest.mark.parametrize("selection, expected", [("1", "user1.json"), ("2", "user2.json"), (" a ", "user1.json"), ("B", "user2.json")])
def test_env_selection_picks_the_users_json_file(selection, expected):
    write_account(1)
    write_account(2)
    assert user_file_for(selection).name == expected


@pytest.mark.parametrize("selection", ["", "0", "user1", "-1"])
def test_invalid_selection_is_explained(selection):
    with pytest.raises(ValueError, match="HELD_ACTIVE_USER"):
        user_file_for(selection)


def test_missing_account_file_is_explained():
    with pytest.raises(FileNotFoundError, match="user3.json"):
        user_file_for("3")


def test_sign_in_logs_in_the_selected_user_and_saves_their_profile(monkeypatch):
    path = write_account(2)
    login_access = token()
    requests = []

    def handler(request):
        requests.append((request.url.path, request.headers.get("Authorization")))
        if request.url.path.endswith("/auth/login"):
            assert json.loads(request.content) == {"email": "user2@example.test", "password": "secret1"}
            return httpx.Response(200, json={"data": {"accessToken": login_access, "refreshToken": "r-1"}})
        return httpx.Response(200, json={"data": {**PROFILE, "email": "user2@example.test"}})

    real_client = HeldAuthClient
    monkeypatch.setattr(chat, "HeldAuthClient", lambda account: real_client(
        account, httpx.Client(transport=httpx.MockTransport(handler)),
    ))
    profile = chat.sign_in("2")

    assert profile == {**PROFILE, "email": "user2@example.test"}
    assert requests == [("/api/auth/login", None), ("/api/auth/me", f"Bearer {login_access}")]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert (data["access_token"], data["refresh_token"], data["user_id"]) == (login_access, "r-1", "usr_2")
    assert data["profile"]["diseases"] == ["Depression"]
    assert data["profile"]["role"] == "CAREGIVER"
    assert "avatarUrl" not in data["profile"]


def test_chat_sends_the_held_access_token(monkeypatch, held):
    access_token = held.access_token
    requests = []

    def fake_urlopen(request, timeout):
        requests.append(request)
        assert request.full_url == chat.API_URL
        assert request.get_header("Authorization") == f"Bearer {access_token}"
        assert timeout == chat.TIMEOUT_SECONDS
        return FakeResponse(lines=ANSWER)

    monkeypatch.setattr(chat.urllib.request, "urlopen", fake_urlopen)
    assert chat.ask_agent("Hi") == "Hello"
    assert len(requests) == 1
    assert held.held_requests == []


def test_rejected_token_is_refreshed_and_the_message_retried(monkeypatch, held):
    sent_tokens = []

    def fake_urlopen(request, timeout):
        sent_tokens.append(request.get_header("Authorization").removeprefix("Bearer "))
        if len(sent_tokens) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":{"code":"TOKEN_EXPIRED"}}'),
            )
        return FakeResponse(lines=ANSWER)

    monkeypatch.setattr(chat.urllib.request, "urlopen", fake_urlopen)
    old_token = held.access_token
    assert chat.ask_agent("Hi") == "Hello"
    assert sent_tokens == [old_token, held.refreshed_token]
    assert held.held_requests == ["/api/auth/refresh"]
    assert held.store.load() == (held.refreshed_token, "refresh-1")


def test_expiring_token_is_refreshed_before_sending(monkeypatch, held):
    held.store.save(token(expires_in=10), "refresh-0")
    held.access_token = held.store.load()[0]
    sent = []
    monkeypatch.setattr(chat.urllib.request, "urlopen", lambda request, timeout: (
        sent.append(request.get_header("Authorization")) or FakeResponse(lines=ANSWER)
    ))
    chat.ask_agent("Hi")
    assert sent == [f"Bearer {held.refreshed_token}"]


def test_signed_in_user_is_registered_with_the_agent(monkeypatch, held):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        return FakeResponse()

    monkeypatch.setattr(chat, "AGENT_BASE_URL", "http://agent.test")
    monkeypatch.setattr(chat.urllib.request, "urlopen", fake_urlopen)
    old_token = held.access_token
    chat.register_with_agent()
    assert calls == [
        ("POST", "http://agent.test/agentic/users/register", f"Bearer {old_token}"),
        ("POST", "http://agent.test/agentic/users/register", f"Bearer {held.refreshed_token}"),
    ]
