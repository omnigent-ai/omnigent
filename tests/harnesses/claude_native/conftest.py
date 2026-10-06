"""Shared fixtures for the Claude-native adapter tests."""

from __future__ import annotations

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge


@pytest.fixture(autouse=True)
def _no_hook_block_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make the post-submit hook-block watch a single look.

    Fake panes answer instantly, so waiting out the production window only
    slows every delivery test; the tests of the watch itself widen it again.
    """
    monkeypatch.setattr(claude_native_bridge, "_HOOK_BLOCK_WATCH_TIMEOUT_S", 0.0)
