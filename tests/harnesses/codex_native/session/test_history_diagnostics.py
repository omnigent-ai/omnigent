"""Pairing observations retain no tool content and reset at compaction."""

import copy
import json

from omnigent.harnesses.codex_native.history_diagnostics import history_shape_statistics


def _record(payload: dict) -> dict:
    return {"type": "response_item", "payload": payload}


def test_history_shape_is_content_free_and_preserves_records() -> None:
    records = [
        _record(
            {
                "type": "function_call",
                "call_id": "call_0",
                "name": "private-tool",
                "arguments": "private-input",
            }
        ),
        _record({"type": "message", "role": "assistant", "content": "private-answer"}),
        _record({"type": "function_call_output", "call_id": "call_0", "output": "private-output"}),
        _record({"type": "tool_search_call", "call_id": "call_0"}),
        _record({"type": "tool_search_output", "call_id": "missing-call"}),
    ]
    original = copy.deepcopy(records)
    result = history_shape_statistics(records)
    assert result["history_duplicate_call_ids_in_sample"] == 1
    assert result["history_unmatched_results_in_sample"] == 1
    assert result["history_results_after_assistant_message_in_sample"] == 1
    assert result["history_sample_complete"] is True
    assert records == original
    assert "private" not in json.dumps(result)
    assert "call_0" not in json.dumps(result)


def test_compaction_replaces_earlier_pairing_context() -> None:
    records = [
        _record({"type": "function_call", "call_id": "discarded"}),
        {
            "type": "compacted",
            "payload": {"replacement_history": [{"type": "function_call", "call_id": "retained"}]},
        },
        _record({"type": "function_call_output", "call_id": "retained"}),
    ]
    result = history_shape_statistics(records)
    assert result["history_context_items"] == 2
    assert result["history_calls_in_sample"] == 1
    assert result["history_unmatched_results_in_sample"] == 0


def test_sampling_is_explicit_and_handles_malformed_ids() -> None:
    records = [_record({"type": "message", "role": "user"}) for _ in range(5000)]
    records.append(_record({"type": "function_call_output", "call_id": {"private": "input"}}))
    result = history_shape_statistics(records)
    assert result["history_context_items"] == 5001
    assert result["history_sample_items"] == 4096
    assert result["history_sample_complete"] is False
    assert result["history_missing_call_ids_in_sample"] == 1
