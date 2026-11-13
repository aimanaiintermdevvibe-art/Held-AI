import asyncio

import httpx
import pytest

from backend.core.auth import fetch_user_profile, held_me as real_held_me
from backend.core.config import get_settings

BASE = "http://held.test/api"


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
