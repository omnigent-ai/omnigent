"""
PTY regression test: the REPL's host must clear streamed raw markdown
when emitting a rendered replacement.

The bug this guards against: a markdown response (intro paragraph
followed by a table) is streamed chunk-by-chunk through
:meth:`TerminalHost.output`, then a :class:`StreamReplace` triggers
:meth:`TerminalHost.replace_streamed_text` to clear the streamed
lines and write the rendered Markdown panel. If the clear-and-render
path is wrong (e.g. bypasses the StdoutProxy's worker-thread
serialization with the streaming writes), the cursor escapes fire
before the streamed text reaches the terminal — so nothing gets
cleared, and both the raw streamed lines and the rendered Markdown
panel end up visible at the same time. The user-reported
"REPL is still double rendering" symptom on 2026-04-27.

This test runs the host in a forked PTY (so prompt-toolkit's
``patch_stdout`` and ``run_in_terminal`` machinery are exercised
exactly as in the REPL), captures the byte stream the host wrote,
re-plays it through ``pyte`` to inspect the final on-screen state,
and asserts that no raw markdown markers (``| Name |`` / pipe-tables)
remain visible. A regression that re-introduces the bypass-the-proxy
path (or otherwise breaks the streaming/replace serialization) shows
the raw markdown alongside the rendered table — this assertion fails.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

# pyte is a terminal emulator we use to render the captured byte
# stream into a final screen state, the same way the user's terminal
# would. Without it we'd be eyeballing raw escape sequences which is
# misleading (the captured stream contains the streamed text *and* the
# clear-and-render escapes; you can't tell which won by reading bytes).
pyte = pytest.importorskip("pyte")

_DRIVER = Path(__file__).parent / "_double_render_driver.py"


def _spawn_driver_in_pty(cols: int, rows: int) -> tuple[int, int]:
    """
    Fork the driver script under a PTY of the given size.

    :param cols: Terminal width in columns.
    :param rows: Terminal height in rows.
    :returns: ``(pid, parent_fd)`` — the child's pid and the parent's
        side of the PTY (read this to capture what the driver wrote).
    """
    parent_fd, child_fd = pty.openpty()
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(child_fd, termios.TIOCSWINSZ, winsize)

    pid = os.fork()
    if pid == 0:
        # Child: dup the slave PTY to stdin/stdout/stderr and exec the driver.
        os.close(parent_fd)
        os.dup2(child_fd, 0)
        os.dup2(child_fd, 1)
        os.dup2(child_fd, 2)
        os.close(child_fd)
        os.execvp(sys.executable, [sys.executable, str(_DRIVER)])

    os.close(child_fd)
    return pid, parent_fd


def _drain_pty(parent_fd: int, pid: int, timeout_s: float = 8.0) -> str:
    """
    Read from the PTY parent fd until the child exits or the deadline
    expires. Decodes the captured bytes as UTF-8.

    :param parent_fd: The parent side of the PTY.
    :param pid: The child's pid (used to detect termination).
    :param timeout_s: Maximum wall-clock seconds to drain.
    :returns: Decoded captured stream.
    """
    captured: list[bytes] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, _, _ = select.select([parent_fd], [], [], 0.5)
        if not ready:
            try:
                done_pid, _ = os.waitpid(pid, os.WNOHANG)
                if done_pid == pid:
                    break
            except ChildProcessError:
                break
            continue
        try:
            data = os.read(parent_fd, 8192)
            if not data:
                break
            captured.append(data)
        except OSError:
            break
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, 9)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
    os.close(parent_fd)
    return b"".join(captured).decode("utf-8", errors="replace")


def test_replace_clears_streamed_raw_markdown_in_pty() -> None:
    """
    Drive the host through the streamed-then-replaced flow inside a
    real PTY. Assert that the final on-screen state shows the rendered
    table BUT NOT any raw pipe-table markdown.

    Without the fix to :meth:`TerminalHost.replace_streamed_text`
    (or with a regression that bypasses ``sys.stdout`` for the
    replace payload), the streamed raw lines remain visible above
    the rendered output and the ``| Name | Notes |`` raw row is
    still on screen — this assertion catches that loud.
    """
    cols, rows = 100, 40
    pid, parent_fd = _spawn_driver_in_pty(cols, rows)
    raw = _drain_pty(parent_fd, pid)

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(raw)
    display = list(screen.display)

    raw_markdown_visible = any("| Name | Notes |" in line for line in display)
    rendered_table_visible = any(
        "Name" in line and "Notes" in line and "|" not in line for line in display
    )

    assert not raw_markdown_visible, (
        "Raw markdown ``| Name | Notes |`` is still visible on screen "
        "after the StreamReplace fired — the clear+render didn't take "
        "effect. Likely cause: ``replace_streamed_text`` is bypassing "
        "the StdoutProxy worker queue (e.g. via a direct "
        "``run_in_terminal`` call), so the cursor escapes race the "
        "streaming writes and end up clearing empty rows while the "
        "streamed raw lines slip in afterward. See "
        "``_host.py::replace_streamed_text`` for why we route through "
        "``sys.stdout`` instead. Final screen state:\n"
        + "\n".join(f"{i:2}: {line.rstrip()}" for i, line in enumerate(display))
    )
    assert rendered_table_visible, (
        "Rendered Markdown table is missing from the final screen — "
        "the StreamReplace either didn't fire or its rendered output "
        "landed somewhere unreachable. Check that the formatter is "
        "still emitting StreamReplace at the trailing-paragraph "
        "TextDone path. Final screen state:\n"
        + "\n".join(f"{i:2}: {line.rstrip()}" for i, line in enumerate(display))
    )
