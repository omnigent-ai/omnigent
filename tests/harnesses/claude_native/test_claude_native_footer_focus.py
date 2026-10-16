"""Delivery into a Claude terminal whose footer holds keyboard focus."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import inject_user_message, write_tmux_target

_CLEAR_LINE_KEYS = ("\x1b[97;5u", "\x1b[107;5u")
_AGENTS = ("main", "general-purpose", "general-purpose")
_RULE = "─" * 60


@dataclass
class _FakeClaudeFooter:
    """
    Claude Code 2.1.290's footer focus, as observed against the real TUI.

    Pressing Down in an empty input box moves focus to the background-shells
    item, then into the agent list. While the footer holds focus a paste is
    lost and Enter acts on the footer. Space hands focus back to the input
    box and, there or in the footer, types a space into it.
    """

    focus: str = "input"
    selected: int = 0
    draft: str = ""
    submitted: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    def pane(self) -> str:
        """
        Render the pane the way ``capture-pane -p`` returns it.

        :returns: Transcript, framed input box, status rows and agent list.
        """
        list_focused = self.focus == "list"
        rows = []
        for idx, name in enumerate(_AGENTS):
            glyph = "●" if idx == 0 else "◯"
            lead = "❯ " if list_focused and idx == self.selected else "  "
            task = "" if idx == 0 else f"  Sleep and monitor {idx}"
            rows.append(f"{lead}{glyph} {name}{task}")
        hint = "  ↑/↓ to select" if list_focused else "  ⏵⏵ auto mode on · 2 shells"
        return "\n".join(
            [
                "● I started 2 general-purpose agents in the background.",
                _RULE,
                f"❯ {self.draft}",
                _RULE,
                "  Opus │ medium │ 39.1k/1M $0.32 │ git:master",
                hint,
                *rows,
            ]
        )

    def press(self, key: str) -> None:
        """
        Apply one tmux key the way the TUI handles it.

        :param key: A tmux key name, a CSI-u sequence, or literal typed text.
        """
        self.keys.append(key)
        if self.focus != "input":
            if key == "Space":
                self.focus = "input"
                self.draft += " "
            return
        if key == "Space":
            self.draft += " "
        elif key in _CLEAR_LINE_KEYS or key == "C-u":
            self.draft = ""
        elif key == "Enter":
            if self.draft.strip():
                self.submitted.append(self.draft.strip())
            self.draft = ""
        elif key not in ("Escape", "Up", "Down"):
            self.draft += key

    def paste(self, text: str) -> None:
        """
        Deposit a bracketed paste into the input box, if it holds focus.

        :param text: The pasted text, e.g. ``"hello\\n"``.
        """
        if self.focus == "input":
            self.draft += text


def _drive(monkeypatch: pytest.MonkeyPatch, tui: _FakeClaudeFooter) -> None:
    """
    Route the bridge's tmux calls to *tui*.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tui: The fake terminal to drive.
    """
    buffer = {"text": ""}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui.pane(), stderr="")
        if "load-buffer" in cmd:
            buffer["text"] = Path(cmd[-1]).read_bytes().decode().replace("\r", "\n")
        elif "paste-buffer" in cmd:
            tui.paste(buffer["text"])
        elif "send-keys" in cmd:
            tui.press(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Advertise a tmux target for a bridge rooted under *tmp_path*.

    :returns: The bridge directory.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    path = tmp_path / "bridge"
    write_tmux_target(path, socket_path=Path("/tmp/example/tmux.sock"), tmux_target="claude:0.0")
    return path


@pytest.mark.parametrize(
    ("focus", "selected"),
    [("shells", 0), ("list", 0), ("list", 1)],
    ids=["shells-item", "agent-list-on-main", "agent-list-on-sub-agent"],
)
def test_a_message_reaches_claude_while_the_footer_holds_focus(
    focus: str, selected: int, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Focus left in the footer used to swallow the paste and the submit Enter.

    A person who arrowed down from the input box (to look at the shells or the
    agent list) and went back to the web UI saw the message never send. Space
    returns focus to the input box before the clear, without an Escape that
    would interrupt a running turn.
    """
    tui = _FakeClaudeFooter(focus=focus, selected=selected)
    _drive(monkeypatch, tui)

    inject_user_message(bridge_dir, content="yes 3am covers us-west-2")

    assert tui.submitted == ["yes 3am covers us-west-2"]
    assert tui.keys[0] == "Space"
    assert "Escape" not in tui.keys


def test_the_refocus_space_never_reaches_the_message(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the input box already focused, the typed space is cleared before the paste."""
    tui = _FakeClaudeFooter()
    _drive(monkeypatch, tui)

    inject_user_message(bridge_dir, content="hello")

    assert tui.submitted == ["hello"]
    assert tui.keys[:3] == ["Space", *_CLEAR_LINE_KEYS]


def test_a_slash_command_reaches_claude_while_the_footer_holds_focus(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings change from the web UI is refocused the same way as a message."""
    tui = _FakeClaudeFooter(focus="list")
    _drive(monkeypatch, tui)

    claude_native_bridge.inject_slash_command(bridge_dir, command="/effort high")

    assert tui.submitted == ["/effort high"]
    assert tui.keys[:2] == ["Space", "C-u"]
