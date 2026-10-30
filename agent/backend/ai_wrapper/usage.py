from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

# Gemini Developer API standard paid-tier text rates, USD per 1M tokens.
# Unknown models remain unpriced instead of receiving a misleading estimate.
PRICING = {
    "gemini-3.5-flash-lite": {"input": 0.30, "cached_input": 0.03, "output": 2.50},
}
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
_current_usage: ContextVar["UsageTracker | None"] = ContextVar("current_usage", default=None)


@dataclass
class UsageTracker:
    calls: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=perf_counter)

    def record(self, model: str, metadata: Any, duration_ms: float) -> None:
        prompt = getattr(metadata, "prompt_token_count", None)
        output = getattr(metadata, "candidates_token_count", None)
        thoughts = getattr(metadata, "thoughts_token_count", None)
        cached = getattr(metadata, "cached_content_token_count", None)
        if metadata is not None:
            thoughts = thoughts or 0
            cached = cached or 0
        total = getattr(metadata, "total_token_count", None)
        rates = PRICING.get(model)
        estimated_cost = None
        if rates is not None and prompt is not None and output is not None and thoughts is not None:
            cached_billable = min(cached or 0, prompt)
            uncached = prompt - cached_billable
            estimated_cost = (
                uncached * rates["input"]
                + cached_billable * rates["cached_input"]
                + (output + thoughts) * rates["output"]
            ) / 1_000_000
        self.calls.append({
            "model": model,
            "input_tokens": prompt,
            "output_tokens": output,
            "thinking_tokens": thoughts,
            "cached_input_tokens": cached,
            "total_tokens": total,
            "estimated_cost_usd": estimated_cost,
            "duration_ms": round(duration_ms, 2),
        })

    def as_dict(self) -> dict[str, Any]:
        calls = self.calls
        cost_available = all(
            call["estimated_cost_usd"] is not None for call in calls
        )
        return {
            "provider": "Google Gemini Developer API",
            "pricing_tier": "standard paid tier estimate; free tier and account discounts are not included",
            "pricing_source": PRICING_SOURCE,
            "duration_ms": round((perf_counter() - self.started_at) * 1000, 2),
            "request_count": len(calls),
            "input_tokens": _sum(calls, "input_tokens"),
            "output_tokens": _sum(calls, "output_tokens"),
            "thinking_tokens": _sum(calls, "thinking_tokens"),
            "cached_input_tokens": _sum(calls, "cached_input_tokens"),
            "total_tokens": _sum(calls, "total_tokens"),
            "estimated_cost_usd": (
                round(sum(call["estimated_cost_usd"] for call in calls), 10)
                if cost_available else None
            ),
            "calls": calls,
        }


def _sum(calls: list[dict[str, Any]], key: str) -> int | None:
    values = [call[key] for call in calls]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def begin_usage(tracker: UsageTracker):
    return _current_usage.set(tracker)

def end_usage(token) -> None:
    _current_usage.reset(token)

def record_usage(model: str, metadata: Any, duration_ms: float) -> None:
    tracker = _current_usage.get()
    if tracker is not None:
        tracker.record(model, metadata, duration_ms)