"""
A shell-mode draft left in Claude Code's input box no longer blocks web input.

Claude Code takes its input box back from ``!`` shell mode on Escape only while
the shell draft is empty; a typed command keeps the box in shell mode. The
bridge clears such a command with Ctrl+U, which Claude Code applies to the line
and then, at the start of the line, to the mode. The fake input box below
reproduces that behaviour, including multi-line edits, of Claude Code 2.1.310.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    inject_slash_command,
    inject_user_message,
    write_tmux_target,
)

_RULE = "──────────────────────────────"
_CTRL_U = "\x1b[117;5u"
_CTRL_K = "\x1b[107;5u"


class _InputBox:
    """
    Claude Code's input box as far as shell mode is concerned.

    :param mode: ``"bash"`` for shell mode, ``"prompt"`` for chat input.
    :param draft: Text in the box.
    :param cursor: Cursor offset; ``None`` places it at the end.
    """

    def __init__(self, mode: str, draft: str, *, cursor: int | None = None) -> None:
        self.mode = mode
        self.draft = draft
        self.cursor = len(draft) if cursor is None else cursor
        self.submitted: list[tuple[str, str]] = []

    def pane(self) -> str:
        glyph = "!" if self.mode == "bash" else "❯"
        footer = "  ! for shell mode" if self.mode == "bash" else "  ? for shortcuts"
        first, *rest = self.draft.split("\n")
        rows = [f"{glyph} {first}", *(f"  {row}" for row in rest)]
        return "\n".join([_RULE, *rows, _RULE, footer]) + "\n"

    def key(self, key: str) -> None:
        if key in (_CTRL_U, "C-u"):
            # Clears the line; at the start of the line it leaves shell mode.
            if self.cursor == 0:
                self.mode = "prompt"
            else:
                start = self.draft.rfind("\n", 0, self.cursor) + 1
                if start == self.cursor and self.cursor:
                    start -= 1
                self.draft = self.draft[:start] + self.draft[self.cursor :]
                self.cursor = start
        elif key == _CTRL_K:
            end = self.draft.find("\n", self.cursor)
            end = len(self.draft) if end == -1 else end
            if end == self.cursor and end < len(self.draft):
                end += 1
            self.draft = self.draft[: self.cursor] + self.draft[end:]
        elif key == "Escape":
            # Shell mode ends only with the cursor at the start of an empty draft.
            if self.mode == "bash" and not self.draft:
                self.mode = "prompt"
        elif key == "\x1b[97;5u":
            self.cursor = self.draft.rfind("\n", 0, self.cursor) + 1
        elif key == "Space":
            self.draft = self.draft[: self.cursor] + " " + self.draft[self.cursor :]
            self.cursor += 1
        elif key == "Enter":
            self.submitted.append((self.mode, self.draft.strip()))
            self.draft = ""
            self.cursor = 0


def _patch_tmux(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    box: _InputBox,
    *,
    content: str,
) -> tuple[Path, list[list[str]]]:
    """
    Route the bridge's tmux calls to *box*; a paste deposits *content*.

    :returns: The bridge dir and the recorded tmux argv lists.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/example/tmux.sock"), tmux_target="main")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=box.pane(), stderr="")
        calls.append(cmd)
        if "paste-buffer" in cmd:
            box.draft = content
            box.cursor = len(content)
        elif "send-keys" in cmd:
            if "-l" in cmd and cmd[-1].startswith("/"):
                box.draft = cmd[-1]
                box.cursor = len(box.draft)
            else:
                for key in re.findall(r"\x1b\[\d+;5u|[^\x1b]+", cmd[-1]):
                    box.key(key)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    return bridge_dir, calls


def _keys(calls: list[list[str]]) -> list[str]:
    return [cmd[-1] for cmd in calls if "send-keys" in cmd]


def test_a_shell_mode_draft_is_cleared_and_the_message_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A typed shell command is cleared, never run, and the message is sent.

    On main the message was refused ("occupied by shell mode") after two
    Escapes the shell draft ignores.
    """
    box = _InputBox("bash", "git status --short")
    bridge_dir, calls = _patch_tmux(monkeypatch, tmp_path, box, content="hello from the web")

    inject_user_message(bridge_dir, content="hello from the web")

    keys = _keys(calls)
    assert "Escape" not in keys, f"Escape cannot clear a shell draft; sent {keys}."
    assert box.submitted == [("prompt", "hello from the web")]


def test_an_empty_shell_mode_box_is_still_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing typed after ``!``, Escape alone hands the box back, as before."""
    box = _InputBox("bash", "")
    bridge_dir, calls = _patch_tmux(monkeypatch, tmp_path, box, content="hi")

    inject_user_message(bridge_dir, content="hi")

    keys = _keys(calls)
    assert keys[0] == "Escape"
    assert _CTRL_U not in keys
    assert box.submitted == [("prompt", "hi")]


@pytest.mark.parametrize("draft", ["one\ntwo\nthree", "\none\ntwo"])
@pytest.mark.parametrize("cursor", [0, 6, None], ids=["start", "middle", "end"])
def test_a_multiline_shell_draft_is_cleared_before_web_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft: str, cursor: int | None
) -> None:
    """Line edits reclaim shell drafts without executing any leftover command."""
    box = _InputBox("bash", draft, cursor=cursor)
    bridge_dir, _calls = _patch_tmux(monkeypatch, tmp_path, box, content="hello from the web")

    inject_user_message(bridge_dir, content="hello from the web")

    assert box.submitted == [("prompt", "hello from the web")]


def test_a_settings_command_clears_a_shell_mode_draft_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/model`` and ``/effort`` path shares the reclaim, so it is not refused either."""
    box = _InputBox("bash", "ls -la")
    bridge_dir, _calls = _patch_tmux(monkeypatch, tmp_path, box, content="")

    inject_slash_command(bridge_dir, command="/effort high")

    assert box.submitted == [("prompt", "/effort high")]


@pytest.mark.parametrize(
    ("row", "footer", "expected"),
    [
        ("! git status", "  ! for shell mode", True),
        ("!\xa0git status", "  ! for shell mode", True),
        ("!", "  ! for shell mode", False),
        ("❯ git status", "  ? for shortcuts", False),
    ],
    ids=["shell-draft", "nbsp-after-glyph", "empty-shell-box", "chat-draft"],
)
def test_shell_mode_draft(row: str, footer: str, expected: bool) -> None:
    """Only a shell-mode row carrying a command counts as a shell draft."""
    pane = f"{_RULE}\n{row}\n{_RULE}\n{footer}\n"
    assert claude_native_bridge._shell_mode_draft(pane) is expected


def test_shell_mode_draft_ignores_a_pane_without_an_input_box() -> None:
    """An overlay over the box (no composer row) is left to the Escape path."""
    assert claude_native_bridge._shell_mode_draft("  Search prompts: git\n") is False
