"""Delivery into a Claude terminal whose footer holds keyboard focus."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import inject_user_message, write_tmux_target

_CLEAR_ONE_ROW_COMPOSER = "\x1b[107;5u" * 2 + "\x1b[117;5u" * 2
_AGENTS = ("main", "general-purpose", "general-purpose")
_RULE = "─" * 60
_PANEL_KEYS = ("Down", "Up", "Enter")


class _Clock:
    """Time that passes only while the bridge sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _FakeClaudeFooter:
    """
    Claude Code 2.1.290's footer focus, as observed against the real TUI.

    Pressing Down in an empty input box moves focus to the background-shells
    item, then into the agent list. While the footer holds focus a paste is
    lost and Enter acts on the footer. Space hands focus back to the input
    box and, there or in the footer, types a space into it.

    Enter on an agent-list row shows that agent, and a message submitted while
    a subagent is shown goes to it (``subagent_inbox``) instead of main.

    After a panel key, captures can lag (``stale_captures`` frames drawn before
    the key) or tear (``torn``: ``"empty"``, ``"partial"`` without the agent
    panel, ``"panel"`` with the whole input box but no agent panel for three
    captures, or ``"spinner"`` with only a busy line), and keys in ``swallowed``
    are dropped once.
    """

    focus: str = "input"
    selected: int = 0
    viewed: int = 0
    draft: str = ""
    submitted: list[str] = field(default_factory=list)
    subagent_inbox: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    view_switch_ignored: bool = False
    stale_captures: int = 0
    torn: str = ""
    swallowed: list[str] = field(default_factory=list)
    clock: _Clock | None = None
    pressed_at: list[tuple[str, float]] = field(default_factory=list)
    queued_captures: list[str] = field(default_factory=list)

    def capture(self) -> str:
        """
        Return the next ``capture-pane`` result: a lagging or torn frame, else the pane.

        :returns: Captured pane text.
        """
        return self.queued_captures.pop(0) if self.queued_captures else self.pane()

    def pane(self) -> str:
        """
        Render the pane the way ``capture-pane -p`` returns it.

        :returns: Transcript, framed input box, status rows and agent list.
        """
        list_focused = self.focus == "list"
        rows = []
        for idx, name in enumerate(_AGENTS):
            glyph = "●" if idx == self.viewed else "◯"
            lead = "❯ " if list_focused and idx == self.selected else "  "
            task = "" if idx == 0 else f"  Sleep and monitor {idx}"
            rows.append(f"{lead}{glyph} {name}{task}")
        hint = "  ↑/↓ to select" if list_focused else "  ⏵⏵ auto mode on · 2 shells"
        top_rule, composer = _RULE, self.draft
        if self.viewed:
            title = f" Sleep and monitor {self.viewed} ─"
            top_rule = "─" * (len(_RULE) - len(title)) + title
            composer = self.draft or f"Message @{_AGENTS[self.viewed]}…"
        return "\n".join(
            [
                "● I started 2 general-purpose agents in the background.",
                top_rule,
                f"❯ {composer}",
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
        if self.clock is not None:
            self.pressed_at.append((key, self.clock.now))
        if key in self.swallowed:
            self.swallowed.remove(key)
            return
        before = self.pane()
        navigating = key in _PANEL_KEYS and bool(self.viewed or self.focus != "input")
        self._apply(key)
        if navigating:
            self.queued_captures += [before] * self.stale_captures
            if self.torn == "empty":
                self.queued_captures.append("")
            elif self.torn == "partial":
                self.queued_captures.append("\n".join(self.pane().splitlines()[:3]))
            elif self.torn == "panel":
                self.queued_captures += ["\n".join(self.pane().splitlines()[:5])] * 3
            elif self.torn == "spinner":
                self.queued_captures.append("✻ Exploring… (esc to interrupt)")

    def _apply(self, key: str) -> None:
        if self.focus != "input":
            if key == "Space":
                self.focus = "input"
                self.draft += " "
            elif self.focus == "shells" and key in ("Up", "Down"):
                self.focus, self.selected = ("input" if key == "Up" else "list"), 0
            elif self.focus == "list" and key == "Up":
                if self.selected:
                    self.selected -= 1
                else:
                    self.focus = "input"
            elif self.focus == "list" and key == "Down":
                self.selected = min(self.selected + 1, len(_AGENTS) - 1)
            elif self.focus == "list" and key == "Enter" and not self.view_switch_ignored:
                self.viewed = self.selected
            return
        if key == "Space":
            self.draft += " "
        elif key == _CLEAR_ONE_ROW_COMPOSER:
            self.draft = ""
        elif key == "Enter":
            if self.draft.strip():
                inbox = self.subagent_inbox if self.viewed else self.submitted
                inbox.append(self.draft.strip())
            self.draft = ""
        elif key == "Down":
            if not self.draft:
                self.focus = "shells"
        elif key not in ("Escape", "Up"):
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
            return SimpleNamespace(returncode=0, stdout=tui.capture(), stderr="")
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
    assert tui.keys[:2] == ["Space", _CLEAR_ONE_ROW_COMPOSER]


def test_a_slash_command_reaches_claude_while_the_footer_holds_focus(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings change from the web UI is refocused the same way as a message."""
    tui = _FakeClaudeFooter(focus="list")
    _drive(monkeypatch, tui)

    claude_native_bridge.inject_slash_command(bridge_dir, command="/effort high")

    assert tui.submitted == ["/effort high"]
    assert tui.keys[:2] == ["Space", _CLEAR_ONE_ROW_COMPOSER]


@pytest.mark.parametrize(
    ("focus", "selected", "draft"),
    [
        ("input", 0, ""),
        ("input", 0, "half-typed note"),
        ("shells", 0, ""),
        ("list", 1, ""),
        ("list", 2, ""),
        ("list", 0, ""),
    ],
    ids=[
        "input-box",
        "draft-in-subagent-box",
        "shells-item",
        "pointer-on-shown-subagent",
        "pointer-on-another-subagent",
        "pointer-on-main",
    ],
)
@pytest.mark.parametrize(
    "torn",
    ["", "empty", "partial", "panel", "spinner"],
    ids=[
        "whole-frames",
        "empty-capture-after-keys",
        "capture-without-panel-after-keys",
        "whole-input-box-without-panel-after-keys",
        "capture-without-input-box-after-keys",
    ],
)
def test_a_message_reaches_main_while_the_terminal_shows_a_subagent(
    focus: str,
    selected: int,
    draft: str,
    torn: str,
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A message typed while the terminal shows a subagent went to that subagent.

    The web UI's chat is the main conversation, so delivery walks the agent
    panel back to main first, without Escape or ``x``, which would interrupt
    or stop the subagent. A torn capture mid-walk is not taken for main.
    """
    tui = _FakeClaudeFooter(focus=focus, selected=selected, viewed=1, draft=draft, torn=torn)
    _drive(monkeypatch, tui)

    inject_user_message(bridge_dir, content="what did the explorer find?")

    assert tui.submitted == ["what did the explorer find?"]
    assert tui.subagent_inbox == []
    assert tui.viewed == 0
    assert "Escape" not in tui.keys
    assert "x" not in tui.keys


@pytest.mark.parametrize(
    "panel_less_captures", [0, 2], ids=["whole-frames-after", "input-box-without-panel-after"]
)
def test_an_empty_first_capture_does_not_skip_the_switch_to_main(
    panel_less_captures: int, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The readiness gate finds the subagent's view the torn first capture hid, and
    frames that then drop the agent panel are not taken for main.
    """
    tui = _FakeClaudeFooter(viewed=1)
    panel_less = "\n".join(tui.pane().splitlines()[:5])
    tui.queued_captures = ["", tui.pane(), *[panel_less] * panel_less_captures]
    _drive(monkeypatch, tui)

    inject_user_message(bridge_dir, content="what did the explorer find?")

    assert tui.submitted == ["what did the explorer find?"]
    assert tui.subagent_inbox == []
    assert tui.viewed == 0
    assert "Escape" not in tui.keys


def test_a_slash_command_reaches_main_while_the_terminal_shows_a_subagent(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings change from the web UI applies to main, not the shown subagent."""
    tui = _FakeClaudeFooter(viewed=1)
    _drive(monkeypatch, tui)

    claude_native_bridge.inject_slash_command(bridge_dir, command="/effort high")

    assert tui.submitted == ["/effort high"]
    assert tui.subagent_inbox == []


def test_panel_keys_wait_for_the_screen_to_show_their_effect(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A frame drawn before a key landed does not draw another key past main, and a
    key the TUI dropped is pressed again only after the retry interval.
    """
    clock = _Clock()
    monkeypatch.setattr(claude_native_bridge, "time", clock)
    tui = _FakeClaudeFooter(
        focus="list", selected=2, viewed=1, stale_captures=1, swallowed=["Up"], clock=clock
    )
    _drive(monkeypatch, tui)

    inject_user_message(bridge_dir, content="what did the explorer find?")

    assert tui.submitted == ["what did the explorer find?"]
    assert tui.subagent_inbox == []
    panel_keys = [(key, at) for key, at in tui.pressed_at if key in _PANEL_KEYS]
    # The dropped Up, its retry, one more Up to main, Enter on main, then the submit.
    assert [key for key, _ in panel_keys] == ["Up", "Up", "Up", "Enter", "Enter"]
    assert panel_keys[1][1] - panel_keys[0][1] >= claude_native_bridge._AGENT_PANEL_STEP_RETRY_S


@pytest.mark.parametrize(
    "first_captures", [[], [""]], ids=["whole-first-capture", "empty-first-capture"]
)
def test_a_subagent_view_that_will_not_close_is_refused_before_typing(
    first_captures: list[str], bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When main cannot be shown again, nothing is typed into the subagent's box."""
    monkeypatch.setattr(claude_native_bridge, "_OCCUPIED_INPUT_DISMISS_TIMEOUT_S", 0.3)
    tui = _FakeClaudeFooter(
        focus="list",
        selected=1,
        viewed=1,
        view_switch_ignored=True,
        queued_captures=list(first_captures),
    )
    _drive(monkeypatch, tui)

    with pytest.raises(claude_native_bridge.ClaudeInputBoxOccupied, match="a subagent's view"):
        inject_user_message(bridge_dir, content="what did the explorer find?")

    assert tui.subagent_inbox == []
    assert tui.draft == ""


def test_a_subagent_view_seen_at_the_readiness_deadline_holds_the_message(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt timeout would reap a healthy terminal; an occupied box holds the message."""
    clock = _Clock()
    monkeypatch.setattr(claude_native_bridge, "time", clock)
    tui = _FakeClaudeFooter(viewed=1, queued_captures=[""])
    _drive(monkeypatch, tui)
    render = tui.capture

    def capture_returning_after_the_deadline() -> str:
        frame = render()
        clock.now += claude_native_bridge._TMUX_READY_SLOW_BOOT_TIMEOUT_S
        return frame

    monkeypatch.setattr(tui, "capture", capture_returning_after_the_deadline)

    with pytest.raises(claude_native_bridge.ClaudeInputBoxOccupied, match="a subagent's view"):
        inject_user_message(bridge_dir, content="what did the explorer find?")

    assert tui.subagent_inbox == []
    assert tui.submitted == []
    assert tui.draft == ""
