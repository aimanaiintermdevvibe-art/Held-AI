import asyncio
import json
import threading
import time

import httpx
import jwt
import pytest

from backend.core.auth import fetch_user_profile, held_me as real_held_me
from backend.core.config import get_settings
from backend.core.held_client import HeldApiError, HeldAuthClient
from backend.core.user_accounts import UserAccountFile

BASE = "http://held.test/api"


def token(sub="usr_1", expires_in=900):
    return jwt.encode({"sub": sub, "exp": int(time.time()) + expires_in}, "k" * 32, algorithm="HS256")


def ok(data, status_code=200):
    return httpx.Response(status_code, json={"success": True, "statusCode": status_code, "message": "ok", "data": data})


def error(status_code, code):
    return httpx.Response(status_code, json={
        "success": False, "statusCode": status_code, "message": f"failed: {code}", "code": code,
    })


class FakeHeld:
    """Records requests and answers them from a handler, like the Held API."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[tuple[str, str, dict | None]] = []
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.headers.get("content-type") == "application/json" else None
        with self.lock:
            self.requests.append((request.method, request.url.path.removeprefix("/api"), body))
        return self.handler(request, body)

    def paths(self):
        return [path for _, path, _ in self.requests]


def write_account(path, **fields):
    data = {
        "label": "User 1", "email": "user@example.test", "password": "secret1",
        "held_api_base_url": BASE, "user_id": "usr_1", "access_token": "", "refresh_token": "refresh-0",
        **fields,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def account(tmp_path):
    return write_account(tmp_path / "user1.json")


def make_client(path, handler):
    fake = handler if isinstance(handler, FakeHeld) else FakeHeld(handler)
    client = HeldAuthClient(UserAccountFile(path), httpx.Client(transport=httpx.MockTransport(fake)))
    return client, fake


def stored(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_refresh_rotates_and_persists_the_pair_in_the_users_file(account):
    new_access = token()

    def handler(request, body):
        assert request.url == f"{BASE}/auth/refresh"
        assert body == {"refreshToken": "refresh-0"}
        return ok({"accessToken": new_access, "refreshToken": "refresh-1"})

    client, held = make_client(account, handler)
    assert client.get_valid_token() == new_access
    assert held.paths() == ["/auth/refresh"]
    data = stored(account)
    assert (data["access_token"], data["refresh_token"]) == (new_access, "refresh-1")
    assert (data["email"], data["password"], data["label"]) == ("user@example.test", "secret1", "User 1")
    assert "tokens_updated_at" in data


def test_valid_access_token_is_used_without_a_refresh(tmp_path):
    current = token()
    path = write_account(tmp_path / "user1.json", access_token=current)
    client, held = make_client(path, lambda *_: pytest.fail("no request expected"))
    assert client.get_valid_token() == current
    assert held.requests == []


def test_account_without_tokens_logs_in_with_its_own_credentials(tmp_path):
    path = write_account(tmp_path / "user2.json", email="two@example.test", password="pw-2", refresh_token="")
    login_access = token("usr_2")

    def handler(request, body):
        assert body == {"email": "two@example.test", "password": "pw-2"}
        return ok({"accessToken": login_access, "refreshToken": "login-refresh", "user": {"id": "usr_2"}})

    client, held = make_client(path, handler)
    assert client.get_valid_token() == login_access
    assert held.paths() == ["/auth/login"]
    assert (stored(path)["access_token"], stored(path)["refresh_token"]) == (login_access, "login-refresh")


def test_each_user_keeps_its_own_token_pair(tmp_path):
    one = write_account(tmp_path / "user1.json", refresh_token="one-refresh")
    two = write_account(tmp_path / "user2.json", email="two@example.test", refresh_token="two-refresh")
    one_access = token()
    client, _ = make_client(one, lambda *_: ok({"accessToken": one_access, "refreshToken": "one-rotated"}))
    client.get_valid_token()
    assert stored(one)["refresh_token"] == "one-rotated"
    assert stored(two)["refresh_token"] == "two-refresh"


def test_rotated_refresh_token_is_used_on_next_exchange(account):
    pairs = [(token(expires_in=30), "refresh-1"), (token(), "refresh-2")]

    def handler(request, body):
        access, refresh = pairs.pop(0)
        return ok({"accessToken": access, "refreshToken": refresh})

    client, held = make_client(account, handler)
    client.refresh()
    client.refresh()
    assert [body for _, _, body in held.requests] == [{"refreshToken": "refresh-0"}, {"refreshToken": "refresh-1"}]
    assert stored(account)["refresh_token"] == "refresh-2"


def test_a_new_run_continues_from_the_rotated_pair(account):
    first_access = token(expires_in=30)  # about to expire, so the next run must refresh
    second_access = token()

    def handler(request, body):
        if body == {"refreshToken": "refresh-0"}:
            return ok({"accessToken": first_access, "refreshToken": "refresh-1"})
        assert body == {"refreshToken": "refresh-1"}, "a used refresh token was replayed"
        return ok({"accessToken": second_access, "refreshToken": "refresh-2"})

    first_run, _ = make_client(account, handler)
    first_run.get_valid_token()
    second_run, _ = make_client(account, handler)  # e.g. the next `python chat.py`
    assert second_run.get_valid_token() == second_access


def test_rotation_by_another_process_is_picked_up_instead_of_refreshing_again(account):
    rotated = token()
    a, held_a = make_client(account, lambda *_: ok({"accessToken": rotated, "refreshToken": "refresh-1"}))
    b, held_b = make_client(account, lambda *_: pytest.fail("B must reuse A's rotated pair"))

    assert a.get_valid_token() == rotated
    assert b.get_valid_token() == rotated  # B still had refresh-0 in memory
    assert held_a.paths() == ["/auth/refresh"]
    assert held_b.requests == []


def test_concurrent_clients_refresh_only_once(account):
    rotated = token()

    def handler(request, body):
        time.sleep(0.2)  # a slow refresh, so the other threads wait on the lock
        return ok({"accessToken": rotated, "refreshToken": "refresh-1"})

    fake = FakeHeld(handler)
    clients = [make_client(account, fake)[0] for _ in range(3)]
    results = []
    threads = [threading.Thread(target=lambda c=c: results.append(c.get_valid_token())) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == [rotated] * 3
    assert fake.paths() == ["/auth/refresh"]


def test_rejected_refresh_token_falls_back_to_login(account):
    login_access = token()

    def handler(request, body):
        if request.url.path.endswith("/auth/refresh"):
            return error(401, "REFRESH_TOKEN_REUSED")
        return ok({"accessToken": login_access, "refreshToken": "login-refresh"})

    client, held = make_client(account, handler)
    assert client.get_valid_token() == login_access
    assert held.paths() == ["/auth/refresh", "/auth/login"]
    assert (stored(account)["access_token"], stored(account)["refresh_token"]) == (login_access, "login-refresh")


def test_rejected_refresh_without_credentials_clears_the_dead_tokens(tmp_path):
    path = write_account(tmp_path / "user1.json", password="")
    client, _ = make_client(path, lambda *_: error(401, "REFRESH_TOKEN_EXPIRED"))
    assert client.get_valid_token() == ""
    assert (stored(path)["access_token"], stored(path)["refresh_token"]) == ("", "")


def test_expired_request_is_refreshed_and_retried_once(tmp_path):
    old_access, new_access = token(expires_in=800), token(expires_in=900)
    path = write_account(tmp_path / "user1.json", access_token=old_access)

    def handler(request, body):
        if request.url.path.endswith("/auth/refresh"):
            return ok({"accessToken": new_access, "refreshToken": "refresh-1"})
        if request.headers["Authorization"] == f"Bearer {old_access}":
            return error(401, "UNAUTHORIZED")  # revoked before its exp claim
        return ok({"id": "usr_1", "role": "SELF"})

    client, held = make_client(path, handler)
    assert client.me() == {"id": "usr_1", "role": "SELF"}
    assert held.paths() == ["/auth/me", "/auth/refresh", "/auth/me"]


def test_api_errors_carry_the_held_code(tmp_path):
    path = write_account(tmp_path / "user1.json", refresh_token="")
    client, _ = make_client(path, lambda *_: error(401, "INVALID_CREDENTIALS"))
    with pytest.raises(HeldApiError) as caught:
        client.get_valid_token()
    assert (caught.value.status_code, caught.value.code) == (401, "INVALID_CREDENTIALS")


def test_saved_tokens_of_another_account_are_replaced_by_a_login(tmp_path):
    foreign_access, own_access = token("usr_other"), token("usr_1", expires_in=901)
    path = write_account(tmp_path / "user1.json", access_token=foreign_access, refresh_token="foreign-refresh")

    def handler(request, body):
        if request.url.path.endswith("/auth/login"):
            assert body == {"email": "user@example.test", "password": "secret1"}
            return ok({"accessToken": own_access, "refreshToken": "own-refresh"})
        if request.headers["Authorization"] == f"Bearer {foreign_access}":
            return ok({"id": "usr_other", "email": "someone.else@example.test"})
        return ok({"id": "usr_1", "email": "User@Example.test"})

    client, held = make_client(path, handler)
    assert client.sign_in()["id"] == "usr_1"
    assert held.paths() == ["/auth/me", "/auth/login", "/auth/me"]
    assert (stored(path)["access_token"], stored(path)["refresh_token"]) == (own_access, "own-refresh")


def test_held_api_url_comes_from_the_account_file(tmp_path):
    path = write_account(tmp_path / "user1.json", held_api_base_url="http://other-held.test/api/")
    client, _ = make_client(path, lambda *_: pytest.fail("no request expected"))
    assert client.base_url == "http://other-held.test/api"


# -- server-side profile lookup -------------------------------------------------

class FakeAsyncClient:
    response = None
    requests: list = []

    def __init__(self, **_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def get(self, url, headers):
        FakeAsyncClient.requests.append((url, headers))
        return FakeAsyncClient.response


@pytest.fixture
def held_me(monkeypatch):
    monkeypatch.setattr(get_settings(), "held_api_base_url", BASE)
    monkeypatch.setattr("backend.core.auth.held_me", real_held_me)  # the HTTP layer below is faked instead
    monkeypatch.setattr("backend.core.auth.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.requests = []
    return FakeAsyncClient


def test_profile_comes_from_auth_me_with_the_callers_token(held_me):
    held_me.response = httpx.Response(
        200, json={"data": {"id": "usr_1", "role": "CAREGIVER", "relation": "PARENT", "diseases": []}},
        request=httpx.Request("GET", f"{BASE}/auth/me"),
    )
    profile = asyncio.run(fetch_user_profile("caller-token", "usr_1"))
    assert profile == {"id": "usr_1", "role": "CAREGIVER", "relation": "PARENT", "diseases": []}
    assert held_me.requests == [(f"{BASE}/auth/me", {"Authorization": "Bearer caller-token"})]


def test_profile_lookup_failure_is_not_fatal(held_me):
    held_me.response = httpx.Response(401, json={}, request=httpx.Request("GET", f"{BASE}/auth/me"))
    assert asyncio.run(fetch_user_profile("caller-token", "usr_1")) == {}


def test_profile_for_a_different_user_is_ignored(held_me):
    held_me.response = httpx.Response(
        200, json={"data": {"id": "someone-else", "role": "SELF"}},
        request=httpx.Request("GET", f"{BASE}/auth/me"),
    )
    assert asyncio.run(fetch_user_profile("caller-token", "usr_1")) == {}
