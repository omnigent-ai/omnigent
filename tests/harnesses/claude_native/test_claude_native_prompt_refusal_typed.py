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
from omnigent.harnesses.claude_native.bridge import ClaudeUserPromptPending, write_tmux_target

_RULE = "──────────────────────────────"
_PERMISSION_PROMPT = (
    "  Bash command\n"
    "    rm -rf build\n"
    "  Do you want to proceed?\n"
    "  ❯ 1. Yes\n"
    "    2. Yes, and don't ask again for rm commands\n"
    "    3. No, and tell Claude what to do differently (esc)\n"
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
