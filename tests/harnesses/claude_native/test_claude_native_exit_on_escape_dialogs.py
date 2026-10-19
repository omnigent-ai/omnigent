"""
The input-box reclaim never presses Escape on a dialog that Escape would quit.

On Claude Code 2.1.299, Escape on the startup "Settings Error" dialog exits
with status 1 and on the folder-trust question with status 0, although both
advertise "Esc to cancel". A web message that arrived while one was up used
to send that Escape and kill the session's Claude Code. The panes below are
those dialogs as Claude Code 2.1.299 draws them, with a generic path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTerminalDialog,
    inject_slash_command,
    inject_user_message,
    write_tmux_target,
)

_RULE = "─" * 100
_SETTINGS_ERROR = f"""\
{_RULE}
  Settings Error
  /home/someone/.claude/settings.json
   └ Invalid or malformed JSON
  Files with errors are skipped entirely, not just the invalid settings.
  ❯ 1. Fix with Claude
    2. Exit and fix manually
    3. Continue without these settings
  Enter to confirm · Esc to cancel
"""
_FOLDER_TRUST = f"""\
{_RULE}
 Accessing workspace:
 /home/someone/project
 Quick safety check: Is this a project you created or one you trust? (Like your own code, a
 well-known open source project, or work from your team). If not, take a moment to review what's in
 this folder first.
 Claude Code'll be able to read, edit, and execute files here.
 Security guide
 ❯ No, exit
   Yes, I trust this folder
 Enter to confirm · Esc to cancel
"""
_SETTINGS_WARNING = f"""\
{_RULE}
  Settings Warning
  /home/someone/.claude/settings.json
   └ permissions.allow: Invalid permission rule "mcp__*" was skipped
  The values listed above were skipped; the rest of the file is in effect.
  ❯ 1. Continue
    2. Fix with Claude
    3. Exit and fix manually
  Enter to confirm · Esc to cancel
"""


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
    monkeypatch.setattr(
        claude_native_bridge,
        "_claude_pane_state",
        lambda *_args: claude_native_bridge._ClaudePaneState(alive=True),
    )
    path = tmp_path / "bridge"
    write_tmux_target(path, socket_path=Path("/tmp/example/tmux.sock"), tmux_target="main")
    return path


def _show(monkeypatch: pytest.MonkeyPatch, pane: str) -> list[tuple[str, ...]]:
    sent: list[tuple[str, ...]] = []
    monkeypatch.setattr(claude_native_bridge, "_capture_pane", lambda *_args, **_kw: pane)
    monkeypatch.setattr(claude_native_bridge, "_run_tmux", lambda _sock, *args: sent.append(args))
    return sent


@pytest.mark.parametrize(
    "pane", [_SETTINGS_ERROR, _FOLDER_TRUST], ids=["settings-error", "folder-trust"]
)
def test_the_reclaim_leaves_a_dialog_escape_would_quit(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    """No key reaches the pane; the dialog is handed back as an overlay."""
    sent = _show(monkeypatch, pane)

    surface = claude_native_bridge._restore_occupied_input(
        "/tmp/example/tmux.sock", "main", bridge_dir=bridge_dir
    )

    assert surface == claude_native_bridge._OVERLAY_SURFACE
    assert sent == [], f"Escape would quit Claude Code here; sent {sent}."


def test_a_settings_warning_is_still_dismissed(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escape on the settings warning continues into the session, so it is still sent."""
    sent = _show(monkeypatch, _SETTINGS_WARNING)

    claude_native_bridge._restore_occupied_input(
        "/tmp/example/tmux.sock", "main", bridge_dir=bridge_dir
    )

    assert [args[-1] for args in sent] == ["Escape", "Escape"]


@pytest.mark.parametrize(
    "pane", [_SETTINGS_ERROR, _FOLDER_TRUST], ids=["settings-error", "folder-trust"]
)
def test_a_message_behind_such_a_dialog_is_reported_not_escaped(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    """
    The message is refused with the dialog's name and Claude Code keeps running.

    On main the reclaim's Escape quit Claude Code, and the readiness gate then
    timed out on an empty pane after about a second.
    """
    sent = _show(monkeypatch, pane)

    with pytest.raises(ClaudeTerminalDialog, match="waiting for an answer in its terminal"):
        inject_user_message(bridge_dir, content="hello from the web")

    assert sent == []


def test_a_settings_command_behind_such_a_dialog_is_refused_without_a_key(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _show(monkeypatch, _SETTINGS_ERROR)

    with pytest.raises(ClaudeTerminalDialog, match="occupied by an overlay"):
        inject_slash_command(bridge_dir, command="/effort high")

    assert sent == []


@pytest.mark.parametrize(
    ("pane", "expected"),
    [(_SETTINGS_ERROR, True), (_FOLDER_TRUST, True), (_SETTINGS_WARNING, False), ("", False)],
    ids=["settings-error", "folder-trust", "settings-warning", "empty"],
)
def test_escape_quits_claude(pane: str, expected: bool) -> None:
    assert claude_native_bridge._escape_quits_claude(pane) is expected
