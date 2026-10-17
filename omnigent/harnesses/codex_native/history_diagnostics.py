"""Content-free pairing statistics for the context written into a resume rollout."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from omnigent.util.json_types import JsonObject

_MAX_ITEMS = 4096
_CALLS = {"function_call", "tool_search_call", "custom_tool_call"}
_RESULTS = {"function_call_output", "tool_search_output", "custom_tool_call_output"}


def history_shape_statistics(records: Sequence[JsonObject]) -> dict[str, int | bool]:
    """Observe one active context, retaining only a bounded tail and opaque pairing IDs."""
    sampled: deque[object] = deque(maxlen=_MAX_ITEMS)
    total = 0
    for record in records:
        if record.get("type") == "compacted":
            payload = record.get("payload")
            replacement = payload.get("replacement_history") if isinstance(payload, dict) else None
            if isinstance(replacement, list):
                sampled.clear()
                sampled.extend(replacement)
                total = len(replacement)
        elif record.get("type") == "response_item":
            sampled.append(record.get("payload"))
            total += 1
    result: dict[str, int | bool] = {
        "history_context_items": total,
        "history_sample_items": len(sampled),
        "history_sample_complete": total <= _MAX_ITEMS,
        "history_calls_in_sample": 0,
        "history_results_in_sample": 0,
        "history_duplicate_call_ids_in_sample": 0,
        "history_unmatched_results_in_sample": 0,
        "history_missing_call_ids_in_sample": 0,
        "history_results_after_assistant_message_in_sample": 0,
    }
    seen: set[str] = set()
    pending: dict[str, int] = {}
    assistant_generation = 0
    for item in sampled:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if not isinstance(kind, str):
            continue
        if kind == "message" and item.get("role") == "assistant":
            assistant_generation += 1
        if kind not in _CALLS and kind not in _RESULTS:
            continue
        key = "history_calls_in_sample" if kind in _CALLS else "history_results_in_sample"
        result[key] += 1
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            result["history_missing_call_ids_in_sample"] += 1
            continue
        if kind in _CALLS:
            result["history_duplicate_call_ids_in_sample"] += int(call_id in seen)
            seen.add(call_id)
            pending[call_id] = assistant_generation
        else:
            generation = pending.pop(call_id, None)
            result["history_unmatched_results_in_sample"] += int(generation is None)
            result["history_results_after_assistant_message_in_sample"] += int(
                generation is not None and generation < assistant_generation
            )
    return result
