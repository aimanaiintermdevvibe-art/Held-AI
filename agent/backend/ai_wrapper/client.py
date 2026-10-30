import logging
import re
from typing import TYPE_CHECKING, Any, TypeVar
from time import perf_counter

from pydantic import BaseModel

from backend.ai_wrapper.usage import record_usage

if TYPE_CHECKING:
    from backend.core.config import Settings

ModelT = TypeVar("ModelT", bound=BaseModel)

logger = logging.getLogger("held_ai.gemini")

class ProviderBusyError(RuntimeError):
    """Gemini rejected the request for quota/rate limits (HTTP 429)."""

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__("Gemini rate limit reached")
        self.retry_after = retry_after

def _provider_failure(message: str, error: Exception | None) -> RuntimeError:
    """Log the real provider error and map rate limits to ProviderBusyError."""
    logger.warning("%s: %s", message, error)
    if getattr(error, "code", None) == 429:
        delay = re.search(r"retryDelay'?:\s*'?(\d+)", str(error))
        return ProviderBusyError(int(delay.group(1)) if delay else None)
    return RuntimeError(message)

class GeminiClient:
    """Lazy async wrapper around Google's native Gemini API SDK."""

    # The pipeline already has an application-level shared-key fallback. SDK
    # retries multiply this timeout across four sequential agents and can leave
    # the browser waiting for many minutes, so each candidate gets one bounded
    # transport attempt.
    MAX_REQUEST_TIMEOUT_SECONDS = 45.0

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

    def _request_candidates(
        self,
        *,
        api_key: str | None,
        thinking_level: str,
    ) -> list[tuple[str | None, str]]:
        """Return primary, low-thinking, and shared-key request candidates."""
        candidates = [(api_key, thinking_level)]
        same_key_low = (api_key, "low")
        if same_key_low != candidates[0]:
            candidates.append(same_key_low)
        shared_key = self.settings.gemini_api_key or None
        fallback = (shared_key or api_key, "low")
        if fallback != candidates[0]:
            if fallback not in candidates:
                candidates.append(fallback)
        return candidates

    @staticmethod
    def _config(
        *,
        system: str,
        thinking_level: str,
        response_model: type[BaseModel] | None = None,
    ) -> Any:
        from google.genai import types

        values: dict[str, Any] = {
            "system_instruction": system,
            "thinking_config": types.ThinkingConfig(
                thinking_level=thinking_level.upper()
            ),
        }
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
        async def request(
            resolved_api_key: str | None,
            resolved_thinking_level: str,
        ) -> Any:
            client = self._get_client(resolved_api_key)
            return await client.aio.models.generate_content(
                model=model,
                contents=user_input,
                config=self._config(
                    system=system,
                    thinking_level=resolved_thinking_level,
                    response_model=response_model,
                ),
            )

        last_error: Exception | None = None
        request_started = perf_counter()
        for resolved_key, resolved_thinking_level in self._request_candidates(
            api_key=api_key,
            thinking_level=thinking_level,
        ):
            try:
                response = await request(resolved_key, resolved_thinking_level)
                break
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
        else:
            raise _provider_failure("Gemini structured response request failed", last_error) from last_error
        record_usage(
            model,
            getattr(response, "usage_metadata", None),
            (perf_counter() - request_started) * 1000,
        )
        if isinstance(response.parsed, response_model):
            return response.parsed
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned no structured output")
        try:
            return response_model.model_validate_json(text)
        except ValueError as exc:
            raise RuntimeError("Gemini returned invalid structured output") from exc

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

        async def request(
            resolved_api_key: str | None,
            resolved_thinking_level: str,
        ) -> Any:
            client = self._get_client(resolved_api_key)
            return await client.aio.models.generate_content(
                model=model,
                contents=user_input,
                config=self._config(
                    system=system,
                    thinking_level=resolved_thinking_level,
                ),
            )

        last_error: Exception | None = None
        request_started = perf_counter()
        for resolved_key, resolved_thinking_level in self._request_candidates(
            api_key=api_key,
            thinking_level=thinking_level,
        ):
            try:
                response = await request(resolved_key, resolved_thinking_level)
                break
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = exc
        else:
            raise _provider_failure("Gemini text response request failed", last_error) from last_error
        record_usage(
            model,
            getattr(response, "usage_metadata", None),
            (perf_counter() - request_started) * 1000,
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("Gemini returned an empty response")
        return text

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