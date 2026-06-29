"""Aggregate per-conversation ``session_usage`` blobs into a usage summary.

Each conversation persists a ``session_usage`` dict with flat token counters,
an optional ``total_cost_usd`` (present only when the turns were priced), and a
nested ``by_model`` breakdown (see ``sessions.py`` usage write path). This
module folds many such blobs into one summary for the usage dashboard.

Pure and dependency-free, so it is trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# The five token counters stored on each usage blob (and each by_model bucket).
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _coerce_int(value: Any) -> int:
    """Best-effort int coercion; malformed persisted values count as 0."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_cost(value: Any) -> float | None:
    """Coerce a cost value to float, or ``None`` if malformed/absent."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def aggregate_usage(usages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold conversation ``session_usage`` blobs into a single summary.

    :param usages: Per-conversation ``session_usage`` dicts (empty/None skipped).
    :returns: ``{"totals": {...tokens, total_cost_usd?}, "by_model":
        {model: {...tokens, total_cost_usd?}}}``. ``total_cost_usd`` is included
        only when at least one contributing blob/bucket was priced, mirroring
        the "priced ⟺ key present" contract.
    """
    totals = dict.fromkeys(_TOKEN_KEYS, 0)
    total_cost = 0.0
    totals_priced = False
    # model -> {token counters, _cost, _priced}
    models: dict[str, dict[str, Any]] = {}

    for usage in usages:
        if not usage:
            continue
        for key in _TOKEN_KEYS:
            totals[key] += _coerce_int(usage.get(key))
        if "total_cost_usd" in usage:
            cost = _coerce_cost(usage.get("total_cost_usd"))
            if cost is not None:
                total_cost += cost
                totals_priced = True

        by_model = usage.get("by_model") or {}
        if isinstance(by_model, Mapping):
            for model, bucket in by_model.items():
                if not isinstance(bucket, Mapping):
                    continue
                acc = models.setdefault(
                    str(model),
                    {**dict.fromkeys(_TOKEN_KEYS, 0), "_cost": 0.0, "_priced": False},
                )
                for key in _TOKEN_KEYS:
                    acc[key] += _coerce_int(bucket.get(key))
                if "total_cost_usd" in bucket:
                    cost = _coerce_cost(bucket.get("total_cost_usd"))
                    if cost is not None:
                        acc["_cost"] += cost
                        acc["_priced"] = True

    out_totals: dict[str, Any] = dict(totals)
    if totals_priced:
        out_totals["total_cost_usd"] = round(total_cost, 6)

    out_by_model: dict[str, Any] = {}
    for model, acc in models.items():
        entry: dict[str, Any] = {key: acc[key] for key in _TOKEN_KEYS}
        if acc["_priced"]:
            entry["total_cost_usd"] = round(acc["_cost"], 6)
        out_by_model[model] = entry

    return {"totals": out_totals, "by_model": out_by_model}
