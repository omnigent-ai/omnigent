"""Tests for the turn-persist filter (``_extract_persistent_item_from_sse``).

This is the gate that decides which streamed ``response.output_item.done`` items
become durable conversation items. It is the other half of the observed-tool-card
persistence fix: the adapter re-emits an observed tool call as a ``completed``
function_call precisely because this filter keeps ``completed`` ones and drops
``in_progress`` ones. These tests pin that contract so a change to either side
can't silently reopen the "tool cards vanish on refresh" bug.
"""

from __future__ import annotations

from omnigent.server.routes._sessions.helpers import _extract_persistent_item_from_sse


def _output_item_done(item: dict[str, object]) -> dict[str, object]:
    """Wrap an item as a ``response.output_item.done`` SSE event."""
    return {"type": "response.output_item.done", "item": item}


def test_completed_function_call_is_persisted() -> None:
    """A ``completed`` function_call becomes a durable item — this is what the
    adapter re-emits for an observed tool call so it survives reload."""
    result = _extract_persistent_item_from_sse(
        _output_item_done(
            {
                "id": "fc_1",
                "type": "function_call",
                "status": "completed",
                "name": "Ran command",
                "arguments": '{"command": "ls"}',
                "call_id": "c1",
                "agent": "resp_1",
            }
        )
    )
    assert result is not None
    assert result.type == "function_call"


def test_in_progress_function_call_is_dropped() -> None:
    """The live-only observed emission (``in_progress``) is NOT persisted.

    Persisting it would leave an orphan card whose spinner never resolves; the
    durable copy is the ``completed`` re-emission asserted above.
    """
    result = _extract_persistent_item_from_sse(
        _output_item_done(
            {
                "id": "fc_1",
                "type": "function_call",
                "status": "in_progress",
                "name": "Ran command",
                "arguments": '{"command": "ls"}',
                "call_id": "c1",
                "agent": "resp_1",
            }
        )
    )
    assert result is None


def test_function_call_output_is_persisted() -> None:
    """The paired output persists too, so the reloaded card shows its result."""
    result = _extract_persistent_item_from_sse(
        _output_item_done(
            {
                "id": "fco_1",
                "type": "function_call_output",
                "call_id": "c1",
                "output": "a.txt",
            }
        )
    )
    assert result is not None
    assert result.type == "function_call_output"
