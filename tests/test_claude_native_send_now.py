"""Queued Claude messages are steered only when the running CLI advertises support."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.harnesses.claude_native import bridge

_PRODUCTION_SEND_NOW_HINT_TIMEOUT_S = bridge._SEND_NOW_HINT_TIMEOUT_S
_CONTENT = "Stop the old task and inspect the failing test"
_SOCKET = "/tmp/example/tmux.sock"
_TARGET = "claude:0.0"
_HINT = "ctrl+x ctrl+s to send now"
_RULE = "────────────────────────────────────────────────────────────"
_MARKDOWN_MESSAGES = [
    pytest.param("Use `foo` instead of `bar`", "Use foo instead of bar", id="inline-code"),
    pytest.param("**Stop now**", "Stop now", id="bold"),
    pytest.param("# New direction", "New direction", id="heading"),
]


def _composer(draft: str = "") -> str:
    return f"{_RULE}\n❯ {draft}\n{_RULE}\n  ? for shortcuts\n"


def _queued_pane(message: str = _CONTENT, *, hint: str = _HINT) -> str:
    return (
        f"❯ Earlier request\n● Working on the earlier request\n\n❯ {message}\n"
        f"  {hint}\n\n· Thinking… (5s)\n\n" + _composer("Press up to edit queued messages")
    )


@pytest.fixture
def native_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.002)
    monkeypatch.setattr(bridge, "_SEND_NOW_HINT_TIMEOUT_S", 0.01)
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0)
    native = tmp_path / "bridge"
    bridge.write_tmux_target(native, socket_path=Path(_SOCKET), tmux_target=_TARGET)
    (native / "bridge.json").write_text('{"active_session_id": "send-now-session"}')
    (native / "context_raw.json").write_text('{"version": "2.1.275"}')
    (native / "state.json").write_text('{"last_hook_event_name": "UserPromptSubmit"}')
    return native


@dataclass
class _Terminal:
    """A tmux-facing TUI that keeps accepted messages queued until the chord arrives."""

    accepted_pane: str = field(default_factory=_queued_pane)
    swallow_first_enter: bool = False
    fail_send_now: bool = False
    on_submit: Callable[[], None] | None = None
    pane: str = field(default_factory=_composer)
    commands: list[list[str]] = field(default_factory=list)
    payloads: list[bytes] = field(default_factory=list)
    enters: int = 0
    send_now_attempts: int = 0

    def run(self, cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=self.pane, stderr="")
        self.commands.append(cmd)
        if "load-buffer" in cmd:
            self.payloads.append(Path(cmd[-1]).read_bytes())
        elif "paste-buffer" in cmd:
            self.pane = _composer("[Pasted text #1 +2 lines]")
        elif cmd[-1] == "Enter":
            self.enters += 1
            if not self.swallow_first_enter or self.enters > 1:
                self.pane = self.accepted_pane
                if self.on_submit is not None:
                    self.on_submit()
        elif cmd[-2:] == ["C-x", "C-s"]:
            assert self.enters > 0, "The send-now chord must follow the submit Enter"
            assert self.pane == self.accepted_pane, "The message must have left the draft"
            self.send_now_attempts += 1
            if self.fail_send_now:
                return SimpleNamespace(returncode=1, stdout="", stderr="tmux socket closed")
            self.pane = _composer()
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@dataclass
class _VirtualClock:
    """Track delivery waits without sleeping or changing other modules' clocks."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_queued_message_sends_now_after_a_swallowed_submit_enter_is_retried(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _Terminal(swallow_first_enter=True)
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.enters == 2
    assert terminal.send_now_attempts == 1
    assert terminal.pane == _composer()
    assert [cmd[6:] for cmd in terminal.commands if cmd[3] == "send-keys"] == [
        ["C-a"],
        ["C-k"],
        ["Enter"],
        ["Enter"],
        ["C-x", "C-s"],
    ]
    assert terminal.commands[-1] == [
        "tmux",
        "-S",
        _SOCKET,
        "send-keys",
        "-t",
        _TARGET,
        "C-x",
        "C-s",
    ]


@pytest.mark.parametrize("footer", ["", "  ❯ Hey\n"], ids=["plain", "matching-glyph-footer"])
def test_visible_hint_sends_now_on_first_accepted_capture_without_sleep(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, footer: str
) -> None:
    clock = _VirtualClock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.15)
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 1.0)
    monkeypatch.setattr(bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 3.0)
    terminal = _Terminal(accepted_pane=_queued_pane("Hey") + footer)
    entered_at: list[float] = []
    captured_at: list[float] = []
    sent_now_at: list[float] = []
    sleeps_before_enter = 0

    def timed_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal sleeps_before_enter
        if cmd[-1] == "Enter":
            entered_at.append(clock.now)
            sleeps_before_enter = len(clock.sleeps)
        elif "capture-pane" in cmd and terminal.enters:
            captured_at.append(clock.now)
        elif cmd[-2:] == ["C-x", "C-s"]:
            sent_now_at.append(clock.now)
        return terminal.run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", timed_run)

    bridge.inject_user_message(native_bridge, content="Hey")

    assert terminal.enters == 1
    assert captured_at == entered_at
    assert sent_now_at == entered_at
    assert clock.sleeps[sleeps_before_enter:] == []
    assert terminal.payloads == [b"Hey\r"]
    assert terminal.send_now_attempts == 1


@pytest.mark.parametrize(("content", "rendered"), _MARKDOWN_MESSAGES)
def test_queued_markdown_message_sends_now_without_rewriting_the_pasted_content(
    native_bridge: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    rendered: str,
) -> None:
    terminal = _Terminal(accepted_pane=_queued_pane(rendered))
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=content)

    assert terminal.payloads == [(content + "\r").encode()]
    assert terminal.enters == 1
    assert terminal.send_now_attempts == 1
    assert terminal.pane == _composer()


def test_queued_message_waits_for_send_now_hint_after_prompt_screening(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _VirtualClock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.15)
    monkeypatch.setattr(bridge, "_SEND_NOW_HINT_TIMEOUT_S", 1.0)
    terminal = _Terminal(accepted_pane=_queued_pane(hint=""))
    entered_at: list[float] = []
    captured_at: list[float] = []
    hint_visible_at: list[float] = []
    sent_now_at: list[float] = []

    def delayed_hint_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if cmd[-1] == "Enter":
            entered_at.append(clock.now)
        elif "capture-pane" in cmd and terminal.enters:
            captured_at.append(clock.now)
            # The composer clears before asynchronous prompt screening renders the hint.
            if len(captured_at) == 4:
                terminal.accepted_pane = _queued_pane()
                terminal.pane = terminal.accepted_pane
                hint_visible_at.append(clock.now)
        elif cmd[-2:] == ["C-x", "C-s"]:
            sent_now_at.append(clock.now)
        return terminal.run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", delayed_hint_run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert len(captured_at) == 4
    assert captured_at[:1] == entered_at
    assert all(0 < after - before <= 0.05 + 1e-9 for before, after in pairwise(captured_at))
    assert sent_now_at == hint_visible_at == captured_at[-1:]
    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.enters == 1
    assert terminal.send_now_attempts == 1
    assert terminal.pane == _composer()


def test_slow_prompt_screening_sends_now_on_the_first_hint_after_four_seconds(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _VirtualClock()
    monkeypatch.setattr(bridge, "time", clock)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.15)
    monkeypatch.setattr(bridge, "_SEND_NOW_HINT_TIMEOUT_S", _PRODUCTION_SEND_NOW_HINT_TIMEOUT_S)
    terminal = _Terminal(accepted_pane=_queued_pane(hint=""))
    captured_at: list[float] = []
    hint_visible_at: list[float] = []
    sent_now_at: list[float] = []

    def slow_screening_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        if "capture-pane" in cmd and terminal.enters:
            captured_at.append(clock.now)
            if clock.now >= 4.0 and not hint_visible_at:
                terminal.accepted_pane = _queued_pane()
                terminal.pane = terminal.accepted_pane
                hint_visible_at.append(clock.now)
        elif cmd[-2:] == ["C-x", "C-s"]:
            sent_now_at.append(clock.now)
        return terminal.run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", slow_screening_run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.send_now_attempts == 1
    assert captured_at[0] == 0.0
    assert 4.0 <= hint_visible_at[0] <= 4.05 + 1e-9
    assert sent_now_at == hint_visible_at == captured_at[-1:]
    assert terminal.enters == 1
    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.pane == _composer()


def test_send_now_hint_timeout_keeps_the_accepted_message_queued(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = _Terminal(accepted_pane=_queued_pane(hint=""))
    accepted_captures = 0

    def no_hint_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal accepted_captures
        if "capture-pane" in cmd and terminal.enters:
            accepted_captures += 1
        return terminal.run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", no_hint_run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert accepted_captures >= 2
    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.enters == 1
    assert terminal.send_now_attempts == 0
    assert terminal.pane == terminal.accepted_pane


@pytest.mark.parametrize("idle_state", ["SessionStart", "Stop", "StopFailure", None])
@pytest.mark.parametrize("hint_visible", [False, True], ids=["no-hint", "hint"])
def test_idle_session_does_not_wait_for_the_send_now_hint_polling_budget(
    native_bridge: Path,
    monkeypatch: pytest.MonkeyPatch,
    idle_state: str | None,
    hint_visible: bool,
) -> None:
    state_path = native_bridge / "state.json"
    if idle_state is None:
        state_path.unlink()
    else:
        state_path.write_text(f'{{"last_hook_event_name": "{idle_state}"}}')
    terminal = _Terminal(accepted_pane=_queued_pane() if hint_visible else _composer())
    accepted_captures = 0

    def idle_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal accepted_captures
        if "capture-pane" in cmd and terminal.enters:
            accepted_captures += 1
        return terminal.run(cmd, **kwargs)

    monkeypatch.setattr("subprocess.run", idle_run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    # The verified snapshot also supplies the shortcut check, without another capture.
    assert accepted_captures == 1
    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.enters == 1
    assert terminal.send_now_attempts == int(hint_visible)


@pytest.mark.parametrize(
    ("raw_context", "supported"),
    [
        ('{"version": "2.1.275"}', True),
        ('{"version": "2.1.282"}', True),
        ('{"version": "2.2.0"}', True),
        ('{"version": "2.1.274"}', False),
        ('{"version": "2.1.99"}', False),
        ('{"version": "2.1.275rc1"}', False),
        ('{"version": "not-a-version"}', False),
        ('{"version": 2.1}', False),
        ('{"version": null}', False),
        ('{"version": {"major": 2}}', False),
        ('{"model": {"version": "2.1.282"}}', False),
        ('{"version": "2.1.282"', False),
        ("[]", False),
        ("null", False),
        (None, False),
    ],
    ids=[
        "minimum",
        "newer-patch",
        "newer-minor",
        "previous",
        "numeric-order",
        "prerelease",
        "malformed",
        "number",
        "null-version",
        "object-version",
        "nested-version",
        "partial-json",
        "array",
        "null-context",
        "missing",
    ],
)
def test_send_now_requires_a_supported_running_cli_version(
    native_bridge: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_context: str | None,
    supported: bool,
) -> None:
    context_path = native_bridge / "context_raw.json"
    if raw_context is None:
        context_path.unlink()
    else:
        context_path.write_text(raw_context)
    terminal = _Terminal()
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.enters == 1
    assert terminal.send_now_attempts == int(supported)
    assert terminal.pane == (_composer() if supported else terminal.accepted_pane)


@pytest.mark.parametrize(
    "accepted_pane",
    [
        _composer(),
        _queued_pane(hint="Press up to edit queued messages"),
        _queued_pane(hint="ctrl+x ctrl+a to send now"),
        _queued_pane("An unrelated queued message"),
        _queued_pane().replace("Press up to edit queued messages", "Press up to DELETE something"),
    ],
    ids=["idle", "no-shortcut", "rebound-shortcut", "different-message", "press-up-draft"],
)
def test_supported_cli_receives_no_chord_without_this_messages_live_queue_hint(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, accepted_pane: str
) -> None:
    terminal = _Terminal(accepted_pane=accepted_pane)
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.enters == 1
    assert terminal.send_now_attempts == 0
    assert len(terminal.payloads) == 1


def test_send_now_failure_does_not_replay_an_already_queued_message(
    native_bridge: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=bridge.__name__)
    terminal = _Terminal(fail_send_now=True)
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert terminal.enters == 1
    assert terminal.send_now_attempts == 1
    assert terminal.pane == terminal.accepted_pane
    assert any(record.name == bridge.__name__ for record in caplog.records)


def test_blind_submit_does_not_promote_a_potentially_stale_queue_hint(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 0)
    terminal = _Terminal()
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.enters == 1
    assert terminal.send_now_attempts == 0
    assert terminal.payloads == [(_CONTENT + "\r").encode()]


@pytest.mark.parametrize(
    "prompt",
    [
        f"{_RULE}\nWhich test should run?\n❯ 1. Unit tests\n  2. Integration tests\n"
        f"{_RULE}\nEnter to select · ↑/↓ to navigate · Esc to cancel\n",
        "╭────────────────────────────────────╮\n"
        "│ Do you want to proceed?            │\n"
        "│ ❯ 1. Yes                          │\n"
        "│   2. No                           │\n"
        "╰────────────────────────────────────╯\n",
    ],
    ids=["question", "permission"],
)
def test_native_decision_appearing_after_submit_receives_no_send_now_chord(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    terminal = _Terminal(accepted_pane=f"❯ {_CONTENT}\n  {_HINT}\n" + prompt)
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.enters == 1
    assert terminal.send_now_attempts == 0
    assert terminal.payloads == [(_CONTENT + "\r").encode()]


def test_approval_hook_parking_after_submit_prevents_send_now(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def park_approval() -> None:
        marker = bridge.approval_wait_marker_path("send-now-session", bridge_dir=native_bridge)
        marker.parent.mkdir(exist_ok=True)
        bridge.touch_approval_wait_marker(marker)

    terminal = _Terminal(on_submit=park_approval)
    monkeypatch.setattr("subprocess.run", terminal.run)

    bridge.inject_user_message(native_bridge, content=_CONTENT)

    assert terminal.enters == 1
    assert terminal.send_now_attempts == 0
    assert terminal.payloads == [(_CONTENT + "\r").encode()]
    assert bridge.has_pending_user_prompt(native_bridge)


def test_send_now_does_not_swallow_injection_cancellation(
    native_bridge: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_: _queued_pane())

    def cancelled_send(*_args: str) -> None:
        raise bridge.ClaudeInjectionCancelled("delivery cancelled before send-now")

    monkeypatch.setattr(bridge, "_run_tmux", cancelled_send)

    with pytest.raises(bridge.ClaudeInjectionCancelled):
        bridge._send_now_if_queued(native_bridge, _SOCKET, _TARGET, needle=_CONTENT)


@pytest.mark.parametrize(
    "pane",
    [
        _queued_pane(),
        _queued_pane("Stop the old task and\n  inspect the failing test"),
        _queued_pane("[Pasted text #2 +7 lines]"),
        _queued_pane(hint="ctrl+x ctrl+s to send\n  now"),
    ],
    ids=["plain", "wrapped-message", "collapsed-paste", "wrapped-hint"],
)
def test_live_send_now_hint_follows_the_injected_queued_message(pane: str) -> None:
    assert bridge._send_now_hint_visible(pane, _CONTENT)


@pytest.mark.parametrize(("content", "rendered"), _MARKDOWN_MESSAGES)
def test_send_now_hint_matches_markdown_rendered_queue_text(content: str, rendered: str) -> None:
    assert bridge._send_now_hint_visible(_queued_pane(rendered), content)


@pytest.mark.parametrize(
    ("content", "rendered", "matches"),
    [
        ("**Stop now**", "stop now", False),
        ("**Stop now**", "Stop sometime else", False),
        ("!!!", "???", False),
        ("!!!", _CONTENT, False),
        ("???", "???", True),
        ("###", "###", True),
    ],
    ids=[
        "case-sensitive",
        "different-words",
        "different-punctuation",
        "empty-normalization",
        "exact-question-marks",
        "exact-hashes",
    ],
)
def test_queue_text_normalization_keeps_meaningful_matching_boundaries(
    content: str, rendered: str, matches: bool
) -> None:
    assert bridge._send_now_hint_visible(_queued_pane(rendered), content) is matches


@pytest.mark.parametrize(
    "placeholder",
    [
        "",
        "Press up to edit queued messages",
        "Press up to edit queued messages, Enter to send them immediately",
        "Press up to select a queued message, then Enter to edit it",
        "Press up to select a queued message to edit, or Enter to send them now",
        "Press up to select a queued message\n  to edit, or Enter to send them now",
    ],
    ids=["empty", "edit", "send", "select-edit", "select-send", "wrapped-select-send"],
)
def test_known_queue_composer_placeholders_allow_send_now(placeholder: str) -> None:
    pane = _queued_pane().replace("Press up to edit queued messages", placeholder)
    assert bridge._send_now_hint_visible(pane, _CONTENT)


def test_empty_needle_cannot_identify_a_queued_message() -> None:
    assert not bridge._send_now_hint_visible(_queued_pane(), "")


@pytest.mark.parametrize(
    "pane",
    [
        "",
        f"❯ {_CONTENT}\n  {_HINT}\n",
        f"  {_HINT}\n" + _composer(),
        f"  {_HINT}\n❯ {_CONTENT}\n" + _composer(),
        f"❯ {_CONTENT}\n● The shortcut is {_HINT}\n" + _composer(),
        f"❯ {_CONTENT}\n" + _composer() + f"  {_HINT}\n",
        _composer(f"{_CONTENT}\n  {_HINT}"),
        _queued_pane().replace("❯ Press up to edit queued messages", f"❯ {_CONTENT}"),
        _queued_pane().replace("Press up to edit queued messages", "Press up to DELETE something"),
        _queued_pane().replace("❯ Press up to edit queued messages", "! echo shell"),
        _queued_pane().replace("? for shortcuts", "Search prompts: Esc to cancel"),
        f"❯ {_CONTENT}\n  {_HINT}\n" + _queued_pane("Somebody else's queued message"),
        _queued_pane(hint="ctrl+x ctrl+a to send now"),
    ],
    ids=[
        "empty",
        "no-live-composer",
        "standalone-hint",
        "hint-before-message",
        "quoted-hint",
        "hint-below-composer",
        "hint-in-draft",
        "unsubmitted-draft",
        "press-up-draft",
        "shell-composer",
        "history-search",
        "older-queue-row",
        "rebound-shortcut",
    ],
)
def test_stale_or_unrelated_send_now_text_is_not_a_live_queue_hint(pane: str) -> None:
    assert not bridge._send_now_hint_visible(pane, _CONTENT)
