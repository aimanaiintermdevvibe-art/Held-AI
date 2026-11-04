import asyncio
from types import SimpleNamespace

import pytest

from backend.ai_wrapper.client import GeminiClient, ProviderBusyError
from backend.core.config import Settings


class APIError(Exception):
    """Like google.genai's errors: the HTTP status is in .code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


class Reply:
    text = "Hello"
    parsed = None
    usage_metadata = None
    candidates: list = []


class Models:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str | None, object]] = []  # (api key, config)

    def bound(self, key):
        async def generate_content(*, model, contents, config):
            self.calls.append((key, config))
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))


@pytest.fixture
def gemini(monkeypatch):
    def make(*outcomes):
        client = GeminiClient(Settings(_env_file=None, gemini_api_key="shared"))
        client.RETRY_DELAYS_SECONDS = (0, 0)
        models = Models(outcomes)
        monkeypatch.setattr(client, "_get_client", lambda key=None: models.bound(key))
        return client, models
    return make


def ask(client, api_key=None):
    return asyncio.run(client.generate_text(
        model="gemini-3.5-flash-lite", system="s", user_input="u", thinking_level="medium", api_key=api_key,
    ))


def test_a_passing_failure_is_tried_again_with_low_thinking(gemini):
    client, models = gemini(APIError(503), Reply())
    assert ask(client) == "Hello"
    levels = [config.thinking_config.thinking_level for _, config in models.calls]
    assert [str(level).rsplit(".", 1)[-1] for level in levels] == ["MEDIUM", "LOW"]


@pytest.mark.parametrize("error", [APIError(500), APIError(504), TimeoutError()])
def test_server_errors_and_timeouts_are_tried_again(gemini, error):
    client, models = gemini(error, Reply())
    assert ask(client) == "Hello"
    assert len(models.calls) == 2


def test_it_gives_up_after_three_attempts(gemini):
    client, models = gemini(APIError(503), APIError(503), APIError(503), Reply())
    with pytest.raises(RuntimeError, match="request failed"):
        ask(client)
    assert len(models.calls) == 3


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_a_bad_request_is_not_tried_again(gemini, code):
    client, models = gemini(APIError(code), Reply())
    with pytest.raises(RuntimeError):
        ask(client)
    assert len(models.calls) == 1


def test_a_rate_limit_with_one_key_is_reported_at_once(gemini):
    client, models = gemini(APIError(429), Reply())
    with pytest.raises(ProviderBusyError):
        ask(client)
    assert len(models.calls) == 1


def test_a_rate_limited_agent_key_moves_straight_to_the_shared_key(gemini):
    client, models = gemini(APIError(429), Reply())
    assert ask(client, api_key="agent-key") == "Hello"
    assert [key for key, _ in models.calls] == ["agent-key", "shared"]


def test_every_request_has_an_output_limit(gemini):
    client, models = gemini(Reply())
    ask(client)
    assert models.calls[0][1].max_output_tokens == 8192


class Empty(Reply):
    text = None


class Garbled(Reply):
    text = '{"category": '


def test_an_empty_reply_is_tried_again(gemini):
    client, models = gemini(Empty(), Reply())
    assert ask(client) == "Hello"
    assert len(models.calls) == 2


def test_empty_replies_give_up_after_three_attempts(gemini):
    client, models = gemini(Empty(), Empty(), Empty())
    with pytest.raises(RuntimeError, match="empty response"):
        ask(client)
    assert len(models.calls) == 3


def test_an_unreadable_structured_reply_is_tried_again(gemini):
    from backend.ai_wrapper.contracts import IntentDecision

    client, models = gemini(Garbled(), SimpleNamespace(
        parsed=IntentDecision(category="GENERAL_SUPPORT", reasoning="ok"), text="", usage_metadata=None, candidates=[]))
    decision = asyncio.run(client.generate_structured(
        model="gemini-3.5-flash-lite", system="s", user_input="u", response_model=IntentDecision, thinking_level="low",
    ))
    assert decision.category == "GENERAL_SUPPORT"
    assert len(models.calls) == 2
