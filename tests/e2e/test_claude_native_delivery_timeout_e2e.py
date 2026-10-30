"""E2E regression: claude-native message delivery survives a transiently
unresponsive Claude Code TUI.

The failure mode this guards: a user sends a message from the Omnigent web
chat to a ``claude-native`` session; the bridge pastes the draft into Claude
Code's TUI input box and the draft visibly commits, but the TUI is
unresponsive at submit time (a CPU-starved host, a paste-coalescing burst) and
does not process the submit ``Enter`` for tens of seconds. The bridge used to
give up after a fixed 10s window and raise "The message was not delivered";
``ClaudeNativeExecutor.run_turn`` then logged ``claude-native: failed to
deliver message to harness`` and yielded an ``ExecutorError``, which the
runner surfaced to the web UI as a failed turn -- even though the committed
draft would have delivered moments later once the TUI caught up.

This test drives the REAL transport end to end: a real ``tmux`` server on a
private socket advertised through the production ``write_tmux_target``, and a
fake Claude TUI that renders the framed chat composer, visibly commits the
pasted draft, then STALLS (stops reading stdin, queueing input in the pty)
well past the legacy 10s give-up before recovering and processing the queued
submit. Delivery must succeed:

- ``inject_user_message`` -- the public bridge delivery call -- returns
  instead of raising the delivery-timeout ``RuntimeError``.
- ``ClaudeNativeExecutor.run_turn`` yields ``TurnComplete`` -- not the
  ``ExecutorError`` failed turn -- and never logs the delivery failure.

Runs with no LLM, no ``claude`` binary and no server -- only ``tmux``::

    pytest tests/e2e/test_claude_native_delivery_timeout_e2e.py -v
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    _BRIDGE_ROOT,
    _SUBMIT_VERIFY_TIMEOUT_S,
    _capture_pane,
    inject_user_message,
    write_tmux_target,
)
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux on PATH")

# The user message delivered from the web chat. Its first line is the needle
# the bridge looks for in the input box to confirm the paste committed.
_MESSAGE = "deliver me despite a slow tui"

# How long the fake TUI stays unresponsive after the draft commits. Past the
# legacy 10s fixed submit window (where delivery used to hard-fail), and clear
# of the current submit ceiling so a healthy recovery lands inside it.
_TUI_STALL_S = 13.0

# Upper bound on how long a single delivery may take: the submit ceiling plus
# slack for tmux/readiness overhead and queued-input replay. Tied to the
# ceiling constant so this bound tracks it if the window is later tuned.
_DELIVERY_BOUND_S = _SUBMIT_VERIFY_TIMEOUT_S + 5.0

# The fake Claude TUI. It renders the framed composer Claude Code shows -- a
# box rule, a prompt row, a closing rule -- so the bridge's readiness gate and
# draft-visibility poll both pass and the pasted draft is seen to land. Once
# the draft contains the expected message (argv[1]) it goes unresponsive for
# argv[2] seconds -- stdin queues in the pty, exactly like a CPU-starved TUI
# at submit time -- then recovers and processes the queued submit Enter, which
# moves the draft into the transcript and clears the input box.
_FAKE_CLAUDE_TUI = r"""
import codecs, os, sys, termios, time, tty

sys.path.insert(0, __REPO_ROOT__)
from tests.harnesses.claude_native.test_claude_native_shell_mode_draft import _InputBox

STALL_TRIGGER = sys.argv[1] if len(sys.argv) > 1 else None
STALL_S = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
INITIAL_READ_STALL_S = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    box = _InputBox("prompt", "")
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    pasted = False
    stalled = False
    read_started = False
    keys = ("\x1b[107;5u", "\x1b[117;5u", "\x1b[200~", "\x1b[201~")

    def render():
        transcript = "".join("sent: " + text + "\n" for _, text in box.submitted)
        sys.stdout.write("\x1b[2J\x1b[H" + (transcript + box.pane()).replace("\n", "\r\n"))
        cursor_row = len(transcript.splitlines()) + 1 + box.draft[:box.cursor].count("\n")
        cursor_column = len(box.draft[:box.cursor].rsplit("\n", 1)[-1]) + 2
        sys.stdout.write(f"\x1b[{cursor_row + 1};{cursor_column + 1}H")
        sys.stdout.flush()

    sys.stdout.write("\x1b[?2004h")
    render()
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            if not read_started:
                read_started = True
                time.sleep(INITIAL_READ_STALL_S)
            pending += decoder.decode(data)
            while pending:
                token = next((key for key in keys if pending.startswith(key)), None)
                if token is not None:
                    pending = pending[len(token):]
                    if token == "\x1b[200~":
                        pasted = True
                    elif token == "\x1b[201~":
                        pasted = False
                    else:
                        box.key(token)
                elif any(key.startswith(pending) for key in keys):
                    break
                else:
                    ch, pending = pending[0], pending[1:]
                    if ch == "\x03":
                        return
                    if ch in {"\x7f", "\b"} and not pasted:
                        box.key("BSpace")
                    elif ch == "\r" and not pasted:
                        if box.draft.strip():
                            box.key("Enter")
                    elif ch >= " " or (pasted and ch in "\r\n"):
                        ch = "\n" if ch == "\r" else ch
                        box.draft = box.draft[:box.cursor] + ch + box.draft[box.cursor:]
                        box.cursor += 1
                render()
                if not stalled and STALL_TRIGGER and STALL_TRIGGER in box.draft:
                    stalled = True
                    time.sleep(STALL_S)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
""".replace("__REPO_ROOT__", repr(str(Path(__file__).resolve().parents[2])))


@pytest.fixture
def stalling_claude_pane() -> Iterator[tuple[Path, str]]:
    """A claude-native bridge dir advertising a real tmux pane whose fake TUI
    commits the pasted draft, stalls past the legacy submit window, then
    accepts the queued submit.

    Yields the bridge dir and the tmux socket path. The tmux server is always
    killed on teardown, even when the test body raises.
    """
    work = Path(tempfile.mkdtemp(prefix="slowtui-"))
    # Keep the socket path short: a long path overflows the AF_UNIX limit.
    socket_path = work / "t.sock"
    tui_path = work / "fake_claude_tui.py"
    tui_path.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")

    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "claude",
            "-x",
            "80",
            "-y",
            "24",
            sys.executable,
            str(tui_path),
            _MESSAGE,
            str(_TUI_STALL_S),
            "0.9",
        ],
        check=True,
        timeout=30.0,
    )

    # The bridge validates its dir sits under the trusted claude-native root,
    # so the fixture cannot use the temp dir for it.
    bridge_dir = _BRIDGE_ROOT / f"slowtui-{uuid.uuid4().hex}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="claude")

    # Give the fake TUI a beat to paint the composer before delivery starts.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "❯" in _capture_pane(str(socket_path), "claude"):
            break
        time.sleep(0.1)

    try:
        yield bridge_dir, str(socket_path)
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )
        with contextlib.suppress(OSError):
            for child in work.iterdir():
                child.unlink()
            work.rmdir()


def test_inject_user_message_delivers_despite_stalled_submit(
    stalling_claude_pane: tuple[Path, str],
) -> None:
    """The bridge delivery call out-waits a transiently unresponsive TUI and
    the committed draft is submitted -- not abandoned as undelivered."""
    bridge_dir, socket_path = stalling_claude_pane

    start = time.monotonic()
    inject_user_message(bridge_dir, content=_MESSAGE)
    elapsed = time.monotonic() - start

    # The delivery must actually have out-waited the stall (a fixture whose
    # TUI never stalled would trivially pass) and still land within the ceiling.
    assert _TUI_STALL_S - 2.0 <= elapsed <= _DELIVERY_BOUND_S, elapsed

    # The message left the input box and landed in the transcript.
    pane = _capture_pane(socket_path, "claude")
    assert "sent: " in pane and _MESSAGE[:20] in pane, pane


async def test_run_turn_completes_despite_stalled_submit(
    stalling_claude_pane: tuple[Path, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ClaudeNativeExecutor.run_turn`` completes the turn once the stalled
    TUI catches up -- it must not log ``failed to deliver message to harness``
    or yield the ``ExecutorError`` the runner would surface to the web UI as a
    failed turn."""
    bridge_dir, _socket_path = stalling_claude_pane
    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)

    events = []
    with caplog.at_level("ERROR", logger="omnigent.inner.claude_native_executor"):
        start = time.monotonic()
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": _MESSAGE}],
            tools=[],
            system_prompt="",
            config=None,
        ):
            events.append(event)
        elapsed = time.monotonic() - start

    assert len(events) == 1, events
    assert not isinstance(events[0], ExecutorError), events[0]
    assert isinstance(events[0], TurnComplete), events[0]
    assert _TUI_STALL_S - 2.0 <= elapsed <= _DELIVERY_BOUND_S, elapsed

    assert not any(
        "failed to deliver message to harness" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]
