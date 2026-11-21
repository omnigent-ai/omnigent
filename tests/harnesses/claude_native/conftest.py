"""Shared fixtures for the Claude-native harness tests."""

from __future__ import annotations

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge


@pytest.fixture(autouse=True)
def _default_to_personal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guidance tests assume a person's own host unless they set ``IS_SANDBOX=1``."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)


@pytest.fixture(autouse=True)
def _ignore_ambient_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep a developer's ``CLAUDE_CONFIG_DIR`` away from transcript reads and writes.

    Tests that need a Claude config dir set one themselves.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_one_typing_in_the_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Deliveries see no person typing in a fake pane, and read no real tmux for it.

    Tests of the typing pause set their own reader.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(claude_native_bridge, "last_human_input_at", lambda *_args: None)


@pytest.fixture(autouse=True)
def _styled_capture_is_the_plain_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A fake pane has no text attributes, so its styled capture is its plain one.

    Resolved at call time, so it follows whatever capture a test installs.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        claude_native_bridge,
        "_capture_pane_styled",
        lambda socket_path, tmux_target: claude_native_bridge._capture_pane(
            socket_path, tmux_target
        ),
    )
