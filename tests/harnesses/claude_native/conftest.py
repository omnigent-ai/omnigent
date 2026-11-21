"""Shared fixtures for the Claude-native harness tests."""

from __future__ import annotations

from pathlib import Path

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
def _no_keybindings_of_the_developer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Deliveries read no developer's Claude keybindings; tests of them write their own.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory, where no keybindings file exists.
    :returns: None.
    """
    missing = tmp_path / "claude-config" / "keybindings.json"
    monkeypatch.setattr(claude_native_bridge, "_session_keybindings_path", lambda *_args: missing)


@pytest.fixture(autouse=True)
def _styled_capture_follows_a_fake_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A fake pane has no text attributes, so its styled capture is its plain one.

    Resolved at call time: a test that installs a fake capture gets it styled
    that way, and a test on real tmux keeps the real styled capture.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    real_capture = claude_native_bridge._capture_pane
    real_styled_capture = claude_native_bridge._capture_pane_styled

    def styled_capture(socket_path: str, tmux_target: str) -> str:
        capture = claude_native_bridge._capture_pane
        if capture is real_capture:
            return real_styled_capture(socket_path, tmux_target)
        return capture(socket_path, tmux_target)

    monkeypatch.setattr(claude_native_bridge, "_capture_pane_styled", styled_capture)
