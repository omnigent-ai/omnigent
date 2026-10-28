"""Preserve Codex request boundaries for catalog cost estimates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from omnigent.llms import context_window
from omnigent.llms.context_window import ModelPricing

TokenCounts = tuple[int, int, int]


def token_counts(value: object) -> TokenCounts | None:
    """Read complete inclusive input, cached input and output counters."""
    if not isinstance(value, dict):
        return None
    counts = tuple(value.get(key) for key in ("inputTokens", "cachedInputTokens", "outputTokens"))
    if any(type(count) is not int or count < 0 for count in counts):
        return None
    input_tokens, cached, output = cast(TokenCounts, counts)
    if cached > input_tokens:
        return None
    return input_tokens, cached, output


def usage_from_counts(counts: TokenCounts) -> dict[str, int]:
    """Normalize inclusive Codex input to additive billing buckets."""
    return {
        "input_tokens": counts[0] - counts[1],
        "cache_read_input_tokens": counts[1],
        "output_tokens": counts[2],
    }


def request_usage_delta(
    total: TokenCounts,
    last: TokenCounts | None,
    baseline: TokenCounts,
) -> list[dict[str, int]]:
    """Split counter growth into a verified request and an unclassified remainder.

    Gaps and historical totals retain flat estimates. On a counter reset only
    the reported latest request is attributable; absent that, return no estimate.
    Equal counters are duplicate notifications, even when ``last`` is present.
    """
    if total == baseline:
        return []
    delta = (total[0] - baseline[0], total[1] - baseline[1], total[2] - baseline[2])
    if min(delta) < 0:
        if last is None:
            return []
        delta = last
    # Compare non-cached input separately too: a cache split change is not a request.
    normalized = usage_from_counts(delta)
    if min(normalized.values()) < 0:
        return []
    if last is None:
        return [normalized]
    request = usage_from_counts(last)
    remainder = {key: normalized[key] - value for key, value in request.items()}
    if min(remainder.values()) < 0:
        return [normalized]
    request["request_input_tokens"] = last[0]
    return ([remainder] if any(remainder.values()) else []) + [request]


def compute_itemized_cost(
    usage: dict[str, Any],
    pricing: ModelPricing,
    requests: object,
) -> float:
    """Price verified requests, leaving unclassified aggregate tokens at base rates.

    Ignore malformed or over-attributed itemization rather than accepting negative
    remainders or charging token buckets twice.
    """
    base = context_window.compute_llm_cost(usage, pricing)
    if not isinstance(requests, list):
        return base
    remaining = {
        key: usage.get(key, 0) or 0
        for key in ("input_tokens", "cache_read_input_tokens", "output_tokens")
    }
    adjustment = 0.0
    for request in requests:
        if not isinstance(request, dict):
            return base
        values = {key: request.get(key) for key in remaining}
        if any(type(value) is not int or value < 0 for value in values.values()):
            return base
        values = cast(dict[str, int], values)
        inclusive = request.get("request_input_tokens")
        if (
            type(inclusive) is not int
            or inclusive != values["input_tokens"] + values["cache_read_input_tokens"]
        ):
            return base
        for key, value in values.items():
            remaining[key] -= value
        if min(remaining.values()) < 0:
            return base
        adjustment += context_window.compute_llm_cost(
            {**values, "request_input_tokens": inclusive}, pricing
        ) - context_window.compute_llm_cost(values, pricing)
    return base + adjustment


def native_cost_with_requests(
    data: dict[str, Any],
    current: dict[str, Any],
    old_usage: dict[str, int],
    old_cost: float,
    pricing: ModelPricing,
    pricing_for_model: Callable[[str], ModelPricing | None] | None = None,
) -> float:
    """Consume retry-safe native request snapshots, persisting their checkpoint.

    The coalescer sends every request since its last successful post. Persisted
    source sequence numbers make retries harmless, including counter resets.
    A new forwarder starts from persisted counters, not from zero.
    """
    base = context_window.compute_llm_cost(current, pricing)
    adjustment = float(current.get("codex_cost_adjustment", 0.0))
    # The flat estimate of the reported totals is a floor, so no snapshot can
    # leave a negative adjustment that freezes cost below its flat growth.
    fallback = max(old_cost, base, base + adjustment)
    updates = data.get("codex_pricing_updates")
    source = data.get("codex_pricing_source")
    if not isinstance(updates, list) or not isinstance(source, str):
        prior = token_counts(current.get("codex_pricing_total"))
        reported = {
            "inputTokens": current.get("input_tokens", 0)
            + current.get("cache_read_input_tokens", 0),
            "cachedInputTokens": current.get("cache_read_input_tokens", 0),
            "outputTokens": current.get("output_tokens", 0),
        }
        counts = token_counts(reported)
        if counts is not None and (
            prior is None or all(new >= old for new, old in zip(counts, prior, strict=True))
        ):
            current["codex_pricing_total"] = reported
        return fallback
    checkpoints = dict(current.get("codex_pricing_sequences", {}))
    sequence = checkpoints.get(source, 0)
    baseline = (
        old_usage.get("input_tokens", 0) + old_usage.get("cache_read_input_tokens", 0),
        old_usage.get("cache_read_input_tokens", 0),
        old_usage.get("output_tokens", 0),
    )
    baseline = token_counts(current.get("codex_pricing_total")) or baseline
    cost = old_cost
    for update in updates:
        if not isinstance(update, dict):
            continue
        next_sequence = update.get("sequence")
        total = token_counts(update.get("total"))
        if type(next_sequence) is not int or next_sequence <= sequence or total is None:
            continue
        last = token_counts(update.get("last"))
        model = update.get("model")
        request_pricing = (
            pricing_for_model(model) if isinstance(model, str) and pricing_for_model else pricing
        )
        if request_pricing is None:
            continue
        parts = (
            request_usage_delta(last, last, (0, 0, 0))
            if update.get("counter_reset") is True and last is not None
            else request_usage_delta(total, last, baseline)
        )
        cost += sum(context_window.compute_llm_cost(part, request_pricing) for part in parts)
        baseline = total
        sequence = next_sequence
    final_total = (
        int(current.get("input_tokens", 0)) + int(current.get("cache_read_input_tokens", 0)),
        int(current.get("cache_read_input_tokens", 0)),
        int(current.get("output_tokens", 0)),
    )
    if all(new >= old for new, old in zip(final_total, baseline, strict=True)):
        cost += sum(
            context_window.compute_llm_cost(part, pricing)
            for part in request_usage_delta(final_total, None, baseline)
        )
        baseline = final_total
    # Same floor as the legacy path: an overstated snapshot total can skip
    # request charges, but never hold cost below the reported tokens' flat price.
    cost = max(old_cost, cost, base)
    checkpoints[source] = sequence
    current["codex_pricing_sequences"] = checkpoints
    current["codex_pricing_total"] = dict(
        zip(("inputTokens", "cachedInputTokens", "outputTokens"), baseline, strict=True)
    )
    # Retain known request adjustments across legacy/incomplete future snapshots.
    current["codex_cost_adjustment"] = cost - context_window.compute_llm_cost(
        usage_from_counts(baseline), pricing
    )
    return cost
