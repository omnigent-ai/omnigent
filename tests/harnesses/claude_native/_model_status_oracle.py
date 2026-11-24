"""Fresh same-session model status checks shared by native tests."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.status import sync_raw_status_context


def native_claude_wire_model(model: str) -> str:
    """Map the public native-1M spelling while keeping private model IDs literal."""
    return {"system.ai.claude-opus-4-8[1m]": "system.ai.claude-opus-4-8"}.get(model, model)


def _status_model_matches(
    bridge_dir: Path, expected: str, *, expected_status: str | None = None
) -> bool:
    try:
        raw = json.loads((bridge_dir / bridge.CONTEXT_RAW_FILE).read_text())
    except (OSError, ValueError):
        return False
    native_session = bridge.read_claude_session_id(bridge_dir)
    if not isinstance(raw, dict) or not native_session or raw.get("session_id") != native_session:
        return False
    sync_raw_status_context(bridge_dir, None)
    actual = bridge.read_claude_status_model(bridge_dir)
    status_model = (
        expected_status if expected_status is not None else native_claude_wire_model(expected)
    )
    if actual == expected and expected_status is None:
        return True
    if status_model != expected:
        window = raw.get("context_window")
        if (
            not expected.endswith("[1m]")
            or status_model != expected.removesuffix("[1m]")
            or not isinstance(window, dict)
            or window.get("context_window_size") != 1_000_000
        ):
            return False
    return actual == status_model
