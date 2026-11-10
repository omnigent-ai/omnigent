"""Model-input conversion preserves saved native delivery uncertainty."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.runner.session_history import build_session_history


@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.parametrize("mirrored", [False, True])
def test_unconfirmed_input_never_enters_runner_history(compacted: bool, mirrored: bool) -> None:
    original: dict[str, Any] = {
        "id": "original",
        "type": "message",
        "role": "user",
        "status": "incomplete",
        "response_id": "native_input_unconfirmed",
        "content": [{"type": "input_text", "text": "saved original"}],
    }
    items: list[dict[str, object]] = [original]
    if mirrored:
        items.append(
            {**original, "id": "native", "status": "completed", "response_id": "native_turn"}
        )
    if compacted:
        items = [{"type": "compaction", "compacted_messages": items}]
    history = build_session_history(
        _background_tasks=set(),
        _last_server_item_id={},
        _persist_cancellation_items=AsyncMock(),
        _session_histories={},
        _session_spec_cache={},
        server_client=AsyncMock(),
    )
    converted = history.convert_raw_items_to_input(items)
    assert len(converted) == int(mirrored)
    if mirrored:
        assert converted[0]["content"] == original["content"]
    assert original["response_id"] == "native_input_unconfirmed"
