"""The terminal-screen helper waits on rendered text, not on raw PTY bytes."""

from __future__ import annotations

import io
import sys

import pexpect
import pytest

from tests._helpers.terminal_screen import TerminalScreen

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pexpect PTYs need POSIX")


def _spawn(script: str) -> pexpect.spawn:
    return pexpect.spawn(
        sys.executable, ["-c", script], encoding="utf-8", dimensions=(5, 40), timeout=10
    )


def test_partial_repaint_is_matched_on_the_rendered_screen() -> None:
    # Like prompt-toolkit, repaint only the changed word and skip the space
    # with a cursor move, so the raw stream never holds the visible line.
    child = _spawn(
        "import sys, time\n"
        "sys.stdout.write('state: running'); sys.stdout.flush(); time.sleep(0.2)\n"
        "sys.stdout.write('\\r\\x1b[6C\\x1b[Csleeping'); sys.stdout.flush(); time.sleep(5)\n"
    )
    raw = io.StringIO()
    screen = TerminalScreen(child)
    original_write = screen.write
    screen.write = lambda data: (raw.write(data), original_write(data))[1]  # type: ignore[method-assign]
    try:
        text = screen.wait_for(lambda s: "state: sleeping" in s, timeout=5, what="the idle state")
    finally:
        child.close(force=True)
    assert text.splitlines()[0] == "state: sleeping"
    assert "state: sleeping" not in raw.getvalue()


def test_output_consumed_by_expect_is_still_rendered() -> None:
    child = _spawn(
        "import sys, time\n"
        "print('first line', flush=True); time.sleep(0.3)\n"
        "print('second line', flush=True); time.sleep(5)\n"
    )
    screen = TerminalScreen(child)
    try:
        child.expect("first line")
        text = screen.wait_for(lambda s: "second line" in s, timeout=5, what="the second line")
    finally:
        child.close(force=True)
    assert text.splitlines()[:2] == ["first line", "second line"]


def test_timeout_reports_the_screen() -> None:
    child = _spawn("import time\nprint('still working', flush=True)\ntime.sleep(5)\n")
    screen = TerminalScreen(child)
    try:
        with pytest.raises(TimeoutError, match=r"(?s)never shown within 0\.5s.*still working"):
            screen.wait_for(lambda s: "done" in s, timeout=0.5, what="never shown")
    finally:
        child.close(force=True)


def test_exit_reports_the_screen() -> None:
    child = _spawn("print('goodbye', flush=True)\n")
    screen = TerminalScreen(child)
    try:
        with pytest.raises(EOFError, match=r"(?s)exited before the screen showed done.*goodbye"):
            screen.wait_for(lambda s: "done" in s, timeout=5, what="done")
    finally:
        child.close(force=True)


def test_refuses_a_child_whose_log_is_in_use() -> None:
    child = _spawn("import time\ntime.sleep(5)\n")
    child.logfile_read = io.StringIO()
    try:
        with pytest.raises(ValueError, match="unused logfile_read"):
            TerminalScreen(child)
    finally:
        child.close(force=True)
