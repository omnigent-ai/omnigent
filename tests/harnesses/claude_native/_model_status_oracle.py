"""Fresh same-session model status checks shared by native tests."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.status import sync_raw_status_context


def _status_model_matches(bridge_dir: Path, expected: str) -> bool:
    try:
        raw = json.loads((bridge_dir / bridge.CONTEXT_RAW_FILE).read_text())
    except (OSError, ValueError):
        return False
    native_session = bridge.read_claude_session_id(bridge_dir)
    if not isinstance(raw, dict) or not native_session or raw.get("session_id") != native_session:
        return False
    sync_raw_status_context(bridge_dir, None)
    # Native-1M Opus 4.8 omits this public suffix; every other identity stays literal.
    actual = bridge.read_claude_status_model(bridge_dir)
    return actual == expected or (
        expected == "system.ai.claude-opus-4-8[1m]" and actual == "system.ai.claude-opus-4-8"
    )
