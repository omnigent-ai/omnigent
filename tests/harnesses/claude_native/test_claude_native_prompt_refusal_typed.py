"""
Which pending-prompt refusals let the executor hold the message for an answer.

A refusal raised before any keystroke reaches the pane carries
``nothing_typed=True``; the executor may retry it unchanged once the prompt is
answered. A refusal after the paste must not, because the draft is already in
the input box.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTerminalDialog,
    ClaudeUserPromptPending,
    write_tmux_target,
)

_RULE = "──────────────────────────────"
_PERMISSION_PROMPT = (
    "  Bash command\n"
    "    rm -rf build\n"
    "  Do you want to proceed?\n"
    "  ❯ 1. Yes\n"
    "    2. Yes, and don't ask again for rm commands\n"
    "    3. No, and tell Claude what to do differently (esc)\n"
)


_SETTINGS_ERROR_DIALOG = (
    f"{_RULE}\n"
    "  Settings Error\n"
    "  /home/someone/.claude/settings.json\n"
    "   └ Invalid or malformed JSON\n"
    "  ❯ 1. Fix with Claude\n"
    "    2. Exit and fix manually\n"
    "    3. Continue without these settings\n"
    "  Enter to confirm · Esc to cancel\n"
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "time", _Clock())
    monkeypatch.setattr(claude_native_bridge, "_run_tmux", lambda *_args: None)
    path = tmp_path / "bridge"
    write_tmux_target(path, socket_path=Path("/tmp/example/tmux.sock"), tmux_target="main")
    return path


def _show(monkeypatch: pytest.MonkeyPatch, pane: str) -> None:
    monkeypatch.setattr(claude_native_bridge, "_capture_pane", lambda *_args, **_kw: pane)


def test_the_occupied_input_check_refuses_before_typing(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _show(monkeypatch, _PERMISSION_PROMPT)

    with pytest.raises(ClaudeUserPromptPending) as raised:
        claude_native_bridge._restore_occupied_input(
            "/tmp/example/tmux.sock", "main", bridge_dir=bridge_dir
        )

    assert raised.value.nothing_typed is True


def test_the_readiness_gate_refuses_before_typing(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _show(monkeypatch, _PERMISSION_PROMPT)

    with pytest.raises(ClaudeUserPromptPending) as raised:
        claude_native_bridge._wait_for_claude_prompt_ready(
            "/tmp/example/tmux.sock", "main", timeout_s=1.0, bridge_dir=bridge_dir
        )

    assert raised.value.nothing_typed is True


def test_the_pre_paste_check_refuses_before_typing(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", lambda _dir: True)

    with pytest.raises(ClaudeUserPromptPending) as raised:
        claude_native_bridge._paste_and_submit(
            bridge_dir, "/tmp/example/tmux.sock", "main", text="hi\n", needle="hi"
        )

    assert raised.value.nothing_typed is True


def test_a_prompt_after_the_paste_is_not_retryable(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draft is in the box by then; a retry could append to it."""
    _show(monkeypatch, f"{_RULE}\n❯ hi\n{_RULE}\n  ? for shortcuts\n")
    checks = iter([False, True])
    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", lambda _dir: next(checks))

    with pytest.raises(ClaudeUserPromptPending) as raised:
        claude_native_bridge._paste_and_submit(
            bridge_dir, "/tmp/example/tmux.sock", "main", text="hi\n", needle="hi"
        )

    assert raised.value.nothing_typed is False


def test_the_readiness_gate_dialog_refusal_awaits_an_answer(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dialog only the terminal shows is held by the executor until it closes."""
    _show(monkeypatch, _SETTINGS_ERROR_DIALOG)

    with pytest.raises(ClaudeTerminalDialog, match="Settings Error") as raised:
        claude_native_bridge._wait_for_claude_prompt_ready(
            "/tmp/example/tmux.sock", "main", timeout_s=1.0, bridge_dir=bridge_dir
        )

    assert raised.value.awaits_answer is True


def test_an_occupied_input_box_refusal_awaits_no_answer(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell mode is no dialog; nothing the person answers would free the box."""
    _show(monkeypatch, f"{_RULE}\n! git status --short\n{_RULE}\n  ! for shell mode\n")

    with pytest.raises(ClaudeTerminalDialog, match="occupied by shell mode") as raised:
        claude_native_bridge.inject_user_message(bridge_dir, content="hi")

    assert raised.value.awaits_answer is False


@pytest.mark.parametrize(
    ("pane", "expected"),
    [
        (_SETTINGS_ERROR_DIALOG, True),
        (f"{_RULE}\n❯ \n{_RULE}\n  ? for shortcuts\n", False),
        ("", False),
    ],
    ids=["dialog", "composer", "torn-capture"],
)
def test_terminal_dialog_pending_reads_the_pane(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, pane: str, expected: bool
) -> None:
    _show(monkeypatch, pane)

    assert claude_native_bridge.terminal_dialog_pending(bridge_dir) is expected


def test_terminal_dialog_pending_without_a_pane_is_false(tmp_path: Path) -> None:
    assert claude_native_bridge.terminal_dialog_pending(tmp_path / "missing") is False


def test_the_settings_pre_check_refuses_before_typing(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A routed ``/model`` refused up front can be retried as is once the prompt is answered."""
    _show(monkeypatch, f"{_RULE}\n❯ \n{_RULE}\n  ? for shortcuts\n")
    monkeypatch.setattr(claude_native_bridge, "has_pending_user_prompt", lambda _dir: True)

    with pytest.raises(ClaudeUserPromptPending, match="before changing settings") as raised:
        claude_native_bridge.inject_slash_command(bridge_dir, command="/model opus")

    assert raised.value.nothing_typed is True
