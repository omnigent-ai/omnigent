"""Same-session identity assertions used by native model acceptance checks."""

import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from tests.harnesses.claude_native._model_status_oracle import _status_model_matches
from tests.harnesses.claude_native._model_switch_support import (
    _session_model_bridge,
    _SessionModelComposer,
)


@pytest.mark.parametrize(
    ("expected", "actual", "reported_session", "expected_status", "window_size", "matches"),
    [
        (
            "system.ai.claude-opus-4-8[1m]",
            "system.ai.claude-opus-4-8",
            "native-session",
            None,
            1_000_000,
            True,
        ),
        (
            "system.ai.claude-opus-4-8[1m]",
            "system.ai.claude-opus-4-8[1m]",
            "native-session",
            None,
            1_000_000,
            True,
        ),
        (
            "system.ai.claude-opus-4-8",
            "system.ai.claude-opus-4-8[1m]",
            "native-session",
            None,
            1_000_000,
            False,
        ),
        (
            "system.ai.claude-opus-5[1m]",
            "system.ai.claude-opus-5",
            "native-session",
            None,
            1_000_000,
            False,
        ),
        ("private-model[1m]", "private-model", "native-session", None, 1_000_000, False),
        ("private-model[1m]", "private-model[1m]", "native-session", None, 1_000_000, True),
        (
            "system.ai.claude-opus-4-8-Private[1m]",
            "system.ai.claude-opus-4-8-Private",
            "native-session",
            None,
            1_000_000,
            False,
        ),
        (
            "system.ai.claude-opus-4-8[1m]",
            "system.ai.claude-opus-5",
            "native-session",
            None,
            1_000_000,
            False,
        ),
        (
            "system.ai.claude-opus-4-8[1m]",
            "system.ai.claude-opus-4-8",
            None,
            None,
            1_000_000,
            False,
        ),
        (
            "system.ai.claude-opus-4-8[1m]",
            "system.ai.claude-opus-4-8",
            "earlier-session",
            None,
            1_000_000,
            False,
        ),
        (
            "system.ai.claude-opus-5[1m]",
            "system.ai.claude-opus-5",
            "native-session",
            "system.ai.claude-opus-5",
            1_000_000,
            True,
        ),
        ("private-model[1m]", "private-model", "native-session", "private-model", 1_000_000, True),
        (
            "system.ai.claude-opus-5[1m]",
            "system.ai.claude-opus-5",
            "native-session",
            "system.ai.claude-opus-5",
            200_000,
            False,
        ),
        (
            "system.ai.claude-opus-5[1m]",
            "system.ai.claude-opus-5",
            "native-session",
            "system.ai.claude-opus-5",
            None,
            False,
        ),
        (
            "system.ai.claude-opus-5[1m]",
            "system.ai.claude-opus-4-8",
            "native-session",
            "system.ai.claude-opus-4-8",
            1_000_000,
            False,
        ),
        (
            "private-Model[1m]",
            "private-model",
            "native-session",
            "private-model",
            1_000_000,
            False,
        ),
        (
            "system.ai.claude-opus-5[1m]",
            "system.ai.claude-opus-5",
            "earlier-session",
            "system.ai.claude-opus-5",
            1_000_000,
            False,
        ),
        (
            "system.ai.glm-5-3",
            "system.ai.glm-5-3",
            "native-session",
            "system.ai.glm-5-3",
            200_000,
            True,
        ),
        (
            "private-model",
            "private-model[1m]",
            "native-session",
            "private-model[1m]",
            1_000_000,
            False,
        ),
    ],
    ids=[
        "documented-native-context-spelling",
        "exact-context-spelling",
        "context-is-not-added-to-a-plain-request",
        "other-public-generation-is-literal",
        "private-context-is-literal",
        "exact-private-context",
        "public-looking-private-context-is-literal",
        "wrong-generation",
        "missing-status-session",
        "different-status-session",
        "declared-other-generation-context-spelling",
        "declared-private-context-spelling",
        "declared-context-needs-million-token-window",
        "declared-context-needs-window-evidence",
        "declared-spelling-cannot-change-generation",
        "declared-spelling-cannot-change-private-case",
        "declared-spelling-needs-the-same-native-session",
        "declared-plain-spelling-remains-literal",
        "declared-spelling-cannot-add-context",
    ],
)
def test_managed_model_status_check_preserves_exact_context_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected: str,
    actual: str,
    reported_session: str | None,
    expected_status: str | None,
    window_size: int | None,
    matches: bool,
) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)
    composer = _SessionModelComposer()
    composer.model = actual
    bridge_dir = _session_model_bridge(tmp_path, composer)
    raw_path = bridge_dir / claude_native_bridge.CONTEXT_RAW_FILE
    raw = json.loads(raw_path.read_text())
    if reported_session is None:
        raw.pop("session_id")
    else:
        raw["session_id"] = reported_session
    if window_size is None:
        raw.pop("context_window", None)
    else:
        raw["context_window"] = {"context_window_size": window_size}
    raw_path.write_text(json.dumps(raw))

    assert _status_model_matches(bridge_dir, expected, expected_status=expected_status) is matches
