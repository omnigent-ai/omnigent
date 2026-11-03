"""Server integration test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_claude_prompt_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the claude-native model-change prompt wait off a real tmux pane.

    The handler waits for the pane's prompt before typing. Server integration
    tests mock inject_slash_command, so wait_for_input_ready has no pane and
    must be stubbed.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.wait_for_input_ready", lambda *_a, **_k: None
    )
