import asyncio
import logging
import random
import re
from typing import TYPE_CHECKING, Any, Callable, TypeVar
from time import perf_counter

from pydantic import BaseModel

from backend.ai_wrapper.usage import record_usage

if TYPE_CHECKING:
    from backend.core.config import Settings

ModelT = TypeVar("ModelT", bound=BaseModel)
ParsedT = TypeVar("ParsedT")

logger = logging.getLogger("held_ai.gemini")


class _NoOutput(Exception):
    """Gemini answered, but with nothing usable (empty, malformed or unparseable)."""

class ProviderBusyError(RuntimeError):
    """Gemini rejected the request for quota/rate limits (HTTP 429)."""

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("Gemini rate limit reached")
        self.retry_after = retry_after

# Server-side and network failures that usually pass within seconds (for example Gemini's
# 503 "model overloaded"). Bad requests, auth errors and rate limits are not retried.
_TRANSIENT_STATUS = {408, 500, 502, 503, 504}


def _status(error: Exception) -> int | None:
    code = getattr(error, "code", None)
    return code if isinstance(code, int) else None


def _is_transient(error: Exception) -> bool:
    status = _status(error)
    if status is not None:
        return status in _TRANSIENT_STATUS
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with google-genai
        httpx = None
    if httpx is not None and isinstance(error, httpx.TransportError):
        return True  # timeouts, dropped connections
    return isinstance(error, (TimeoutError, ConnectionError))


def _provider_failure(message: str, error: Exception | None) -> RuntimeError:
    """Log the real provider error and map rate limits to ProviderBusyError."""
    logger.warning("%s: %s", message, error)
    if getattr(error, "code", None) == 429:
        delay = re.search(r"retryDelay'?:\s*'?(\d+)", str(error))
        return ProviderBusyError(int(delay.group(1)) if delay else None)
    return RuntimeError(message)


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    return getattr(reason, "name", None) or (str(reason) if reason else None)


class GeminiClient:
    """Lazy async wrapper around Google's native Gemini API SDK."""

    # The pipeline already has an application-level shared-key fallback. SDK
    # retries multiply this timeout across four sequential agents and can leave
    # the browser waiting for many minutes, so each candidate gets one bounded
    # transport attempt.
    MAX_REQUEST_TIMEOUT_SECONDS = 45.0
    # A request that fails for a passing reason (a 5xx such as "model overloaded", a timeout)
    # is tried again after a short pause, at most MAX_ATTEMPTS times in all, with low thinking
    # from the second attempt on and finally the shared key, when the agent has its own. A rate
    # limit is tried again only with another key; other errors are not tried again.
    MAX_ATTEMPTS = 3
    RETRY_DELAYS_SECONDS = (0.5, 1.5)

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self._clients: dict[str, Any] = {}

    def _get_client(self, api_key: str | None = None) -> Any:
        resolved_key = api_key or self.settings.gemini_api_key
        if not resolved_key:
            raise RuntimeError("No Gemini API key is configured for this agent")
        if resolved_key not in self._clients:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise RuntimeError("The google-genai package is not installed") from exc
            self._clients[resolved_key] = genai.Client(
                api_key=resolved_key,
                http_options=types.HttpOptions(
                    timeout=int(
                        min(
                            self.settings.gemini_timeout_seconds,
                            self.MAX_REQUEST_TIMEOUT_SECONDS,
                        )
                        * 1000
                    ),
                    retry_options=types.HttpRetryOptions(
                        attempts=1,
                    ),
                ),
            )
        return self._clients[resolved_key]

    def _attempts(self, *, api_key: str | None, thinking_level: str) -> list[tuple[str | None, str]]:
        """(api key, thinking level) for each attempt: as asked first, then low thinking, then
        the shared key, repeating the last one to make up MAX_ATTEMPTS."""
        shared_key = self.settings.gemini_api_key or None
        own_key = api_key or shared_key
        plan = list(dict.fromkeys([(own_key, thinking_level), (own_key, "low"), (shared_key or own_key, "low")]))
        plan += [plan[-1]] * (self.MAX_ATTEMPTS - len(plan))
        return plan[:self.MAX_ATTEMPTS]

    async def _generate(
        self,
        *,
        label: str,
        model: str,
        system: str,
        user_input: str,
        thinking_level: str,
        api_key: str | None,
        response_model: type[BaseModel] | None = None,
        parse: Callable[[Any], ParsedT],
    ) -> ParsedT:
        """One generate_content call, tried again on a transient failure, with each attempt's
        token use recorded. A rate limit is tried again only with a different key. parse turns
        the response into the result, raising _NoOutput when it is empty or unreadable (Gemini
        sometimes answers with a malformed response); that is tried again too."""
        attempts = self._attempts(api_key=api_key, thinking_level=thinking_level)
        last_error: Exception | None = None
        index: int | None = 0
        retries = 0
        while index is not None:
            key, level = attempts[index]
            later = range(index + 1, len(attempts))
            started = perf_counter()
            try:
                client = self._get_client(key)
                response = await client.aio.models.generate_content(
                    model=model,
                    contents=user_input,
                    config=self._config(
                        system=system,
                        thinking_level=level,
                        response_model=response_model,
                        max_output_tokens=self.settings.gemini_max_output_tokens,
                    ),
                )
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
                if _status(exc) == 429:  # only another key can help
                    index = next((i for i in later if attempts[i][0] != key), None)
                elif _is_transient(exc):
                    index = later[0] if later else None
                else:
                    index = None
                if index is None:
                    raise _provider_failure(f"{label} request failed", exc) from exc
                reason = _status(exc) or type(exc).__name__
            else:
                record_usage(model, getattr(response, "usage_metadata", None), (perf_counter() - started) * 1000)
                finish = _finish_reason(response)
                if finish == "MAX_TOKENS":
                    logger.warning("%s stopped at the %s-token output limit", label, self.settings.gemini_max_output_tokens)
                try:
                    return parse(response)
                except _NoOutput as exc:
                    if not later:
                        raise RuntimeError(str(exc)) from exc
                    index, reason = later[0], f"{exc}, finish reason {finish}"
            logger.warning("%s failed (%s); trying again", label, reason)
            delay = self.RETRY_DELAYS_SECONDS[min(retries, len(self.RETRY_DELAYS_SECONDS) - 1)]
            retries += 1
            await asyncio.sleep(delay * (0.75 + random.random() / 2))
        raise _provider_failure(f"{label} request failed", last_error)  # pragma: no cover - loop returns or raises

    @staticmethod
    def _config(
        *,
        system: str,
        thinking_level: str,
        response_model: type[BaseModel] | None = None,
        max_output_tokens: int | None = None,
    ) -> Any:
        from google.genai import types

        values: dict[str, Any] = {
            "system_instruction": system,
            "thinking_config": types.ThinkingConfig(
                thinking_level=thinking_level.upper()
            ),
        }
        if max_output_tokens:
            # Thinking counts toward this limit too; it only stops a runaway reply.
            values["max_output_tokens"] = max_output_tokens
        if response_model is not None:
            # The SDK's response_schema dialect rejects additionalProperties.
            # Keep strict extra-field validation in Pydantic after generation.
            def provider_schema(value):
                if isinstance(value, dict):
                    return {key: provider_schema(item) for key, item in value.items() if key != "additionalProperties"}
                if isinstance(value, list):
                    return [provider_schema(item) for item in value]
                return value

            values.update(
                response_mime_type="application/json",
                response_schema=provider_schema(response_model.model_json_schema()),
            )
        return types.GenerateContentConfig(**values)

    async def generate_structured(
        self,
        *,
        model: str,
        system: str,
        user_input: str,
        response_model: type[ModelT],
        thinking_level: str,
        api_key: str | None = None,
    ) -> ModelT:
        def parse(response: Any) -> ModelT:
            if isinstance(response.parsed, response_model):
                return response.parsed
            text = (response.text or "").strip()
            if not text:
                raise _NoOutput("Gemini returned no structured output")
            try:
                return response_model.model_validate_json(text)
            except ValueError as exc:
                raise _NoOutput("Gemini returned invalid structured output") from exc

        return await self._generate(
            label="Gemini structured response",
            model=model,
            system=system,
            user_input=user_input,
            thinking_level=thinking_level,
            api_key=api_key,
            response_model=response_model,
            parse=parse,
        )

    async def generate_text(
        self,
        *,
        model: str,
        system: str,
        user_input: str,
        thinking_level: str,
        verbosity: str = "medium",
        api_key: str | None = None,
    ) -> str:
        del verbosity

        def parse(response: Any) -> str:
            text = (response.text or "").strip()
            if not text:
                raise _NoOutput("Gemini returned an empty response")
            return text

        return await self._generate(
            label="Gemini text response",
            model=model,
            system=system,
            user_input=user_input,
            thinking_level=thinking_level,
            api_key=api_key,
            parse=parse,
        )

    EMBED_BATCH_SIZE = 100  # Gemini's limit for texts in one embedding request

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        task_type: str,
        dimensions: int,
    ) -> list[list[float]]:
        """One embedding per text, in order. task_type is RETRIEVAL_DOCUMENT for stored
        chunks and RETRIEVAL_QUERY for search queries."""
        from google.genai import types

        client = self._get_client()
        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=dimensions)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.EMBED_BATCH_SIZE):
            batch = texts[start:start + self.EMBED_BATCH_SIZE]
            try:
                response = await client.aio.models.embed_content(model=model, contents=batch, config=config)
            except RuntimeError:
                raise
            except Exception as exc:
                raise _provider_failure("Gemini embedding request failed", exc) from exc
            values = [list(e.values or []) for e in response.embeddings or []]
            if len(values) != len(batch) or any(len(v) != dimensions for v in values):
                raise RuntimeError("Gemini returned an unexpected number or size of embeddings")
            vectors.extend(values)
        return vectors

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aio.aclose()
        self._clients.clear()