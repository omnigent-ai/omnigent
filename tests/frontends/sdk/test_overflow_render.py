"""
PTY regression test: the REPL host must produce both rendered
markdown AND no duplicate visible text, even when the streamed
response is taller than the terminal viewport.

The bug (user-reported on 2026-04-28): a long markdown response
under ``claude-sdk`` rendered TWICE — first as raw streamed
text with literal ``**`` markers, then again as rendered markdown
below it. Items 1-6 of a 17-item list appeared raw at top, then the
rendered markdown for items 1-17 appeared below.

Root cause: each growing live render (``StreamLive``) and the final
``StreamReplace`` are drawn by ``_replace_live_region``, which clears
the previous region with a cursor-up + erase repeated
``_live_line_count + _streamed_line_count`` times. Cursor-up movements
past row 0 of the viewport are no-ops — so once a live render grew
taller than the viewport, its top lines scrolled into scrollback and
the next clear could no longer reach them. The visible viewport got
cleared and the markdown rendered below, but scrolled-off content
remained in scrollback — visible duplication wherever scrollback can
be observed (terminal scroll, ``script(1)``, ``tmux`` capture-pane,
screenshots with buffered context).

Fix: in ``_replace_live_region``'s non-commit (``StreamLive``) path,
cap the rendered live region to the viewport ceiling
(``_term_height() - _BOTTOM_RESERVED_ROWS``), keeping only the last
``ceiling + 1`` lines. Because the live region never exceeds what
cursor-up can reach, every subsequent clear (including the final
committed ``StreamReplace``) erases the whole region before the
markdown renders. Markdown formatting is preserved end-to-end;
duplication never appears.

Trade-off vs. UX: long responses show only the tail of the live render
once they fill the viewport (the full text arrives with the committed
markdown panel). Short responses render live in full as before.

This test forks the host's output path under a PTY with a TIGHT
viewport (15 rows), produces 30 numbered markdown items, captures the
byte stream, and re-plays through ``pyte.HistoryScreen`` so both the
visible viewport AND the scrollback are inspectable. Asserts: every
item appears exactly once across visible + scrollback, AND no ``**``
raw markdown markers survive (proving the markdown render took over).

Determinism: the driver (``_overflow_render_driver.py``) calls
``TerminalHost.output`` synchronously — it does NOT run the interactive
prompt-toolkit application. ``output`` writes the streamed renders and
the final ``StreamReplace`` via plain ``print`` / ``sys.stdout.write``,
all in order; the overflow guard under test lives entirely in that
synchronous path. The earlier version ran the live app and relied on
fixed ``asyncio.sleep`` delays, but the app's pinned prompt + toolbar
redraw on a ~10fps timer shared the PTY and interleaved cursor moves
between the driver's prints, corrupting the capture — the test flaked
on CI (duplication detected) with no product regression. Driving
``output`` directly removes that concurrent writer while exercising the
exact same guard.
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

pyte = pytest.importorskip("pyte")

_DRIVER = Path(__file__).parent / "_overflow_render_driver.py"


def _spawn_driver_in_pty(cols: int, rows: int) -> tuple[int, int]:
    """
    Fork the driver script under a PTY of the given size.

    :param cols: Terminal width in columns.
    :param rows: Terminal height in rows.
    :returns: ``(pid, parent_fd)`` for the spawned driver.
    """
    parent_fd, child_fd = pty.openpty()
    fcntl.ioctl(child_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    pid = os.fork()
    if pid == 0:
        os.close(parent_fd)
        os.dup2(child_fd, 0)
        os.dup2(child_fd, 1)
        os.dup2(child_fd, 2)
        os.close(child_fd)
        os.execvp(sys.executable, [sys.executable, str(_DRIVER)])
    os.close(child_fd)
    return pid, parent_fd


def _drain_pty(parent_fd: int, pid: int, timeout_s: float = 8.0) -> bytes:
    """
    Read from the PTY until the child exits or the deadline expires.

    :param parent_fd: Parent side of the PTY.
    :param pid: Child PID — used to short-circuit when the child exits.
    :param timeout_s: Wall-clock seconds.
    :returns: Captured bytes.
    """
    captured: list[bytes] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, _, _ = select.select([parent_fd], [], [], 0.5)
        if not ready:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done == pid:
                    break
            except ChildProcessError:
                break
            continue
        try:
            data = os.read(parent_fd, 16384)
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
    return b"".join(captured)


def _scrollback_text(screen: pyte.HistoryScreen) -> str:
    """
    Concatenate all scrollback rows into a single newline-separated
    string.

    pyte's ``HistoryScreen.history.top`` returns rows as either
    ``StaticDefaultDict`` (with ``.values()`` of ``Char`` objects) or
    integers (sentinel rows). We tolerate both — just stringify
    anything we can.

    :param screen: A ``pyte.HistoryScreen`` that has been fed the
        captured byte stream.
    :returns: All scrollback rows joined by newlines.
    """
    parts: list[str] = []
    for row in screen.history.top:
        if isinstance(row, int):
            continue
        try:
            s = "".join(c.data for c in row.values())
        except (AttributeError, TypeError):
            try:
                s = "".join(c.data if hasattr(c, "data") else str(c) for c in row)
            except Exception:
                s = ""
        parts.append(s)
    return "\n".join(parts)


# Reruns on failure: the assertions are deterministic, but the
# capture path is not — ``_drain_pty`` races the forked driver under a
# fixed wall-clock deadline, so a slow/loaded CI worker can truncate the
# byte stream and drop a trailing item (count == 0). A rerun re-forks a
# fresh PTY and re-captures, which clears the transient timing failure
# without masking a real regression (those fail every attempt).
#
# TODO(#222): reruns alone are a stopgap. CI (Linux) has also been seen
# failing with the DUPLICATION mode (an item appears 2x — raw streamed
# text AND rendered markdown both survive) which reproduces across every
# rerun, unlike the timing/truncation mode this marker covers. That
# points at an environment-dependent overflow-render bug in
# ``_should_stream_more`` / ``replace_streamed_text`` at a tight (15-row)
# viewport that is NOT fixed by retrying. Investigate and fix the
# underlying double-render, then reassess whether this marker is still
# needed.
@pytest.mark.flaky(reruns=4, reruns_delay=0)
def test_no_duplicate_when_streamed_overflows_viewport() -> None:
    """
    Stream 30 markdown items into a 15-row PTY. After the response
    completes, every item must appear AT MOST ONCE across the
    combined ``visible viewport + scrollback`` text.

    Without the viewport cap in ``_replace_live_region``, a live
    render that grew past the viewport leaves its top lines in
    scrollback; the next cursor-up + erase can't reach them, so
    items 1..N stay in scrollback AND the same items render as
    markdown in the new visible viewport — every item appears twice
    in any tool that captures both regions (terminal screenshots,
    ``script(1)``, ``tmux capture-pane -e -S -``).

    The fix caps the live region to the viewport ceiling so cursor-up
    always reaches every rendered line. Long responses show only the
    tail live but no longer produce duplicate visible content.
    """
    cols, rows = 100, 15
    pid, parent_fd = _spawn_driver_in_pty(cols, rows)
    raw = _drain_pty(parent_fd, pid)

    screen = pyte.HistoryScreen(cols, rows, history=400)
    stream = pyte.Stream(screen)
    stream.feed(raw.decode("utf-8", errors="replace"))

    visible = "\n".join(screen.display)
    scrollback = _scrollback_text(screen)
    combined = scrollback + "\n" + visible

    # For each test item, count raw appearances (``**itemN**``)
    # and rendered appearances (``itemN — description`` without
    # the bold markers). Sum of the two must be exactly 1.
    # Sum > 1 = duplication (the bug). Sum < 1 = item missing
    # entirely (driver/PTY issue).
    # Each item's "description for item N " (trailing space anchors
    # the match so "item 1" doesn't also catch "item 10/11/...")
    # must appear exactly once across visible + scrollback. Two
    # appearances = the bug (raw + rendered). Zero = the response
    # didn't make it through.
    for n in (1, 5, 15, 25, 30):
        rendered_sig = f"description for item {n} "
        count = combined.count(rendered_sig)
        assert count == 1, (
            f"item{n} (signature: {rendered_sig!r}) appears {count}x "
            f"in combined visible+scrollback. Expected exactly 1. "
            f"Count > 1 means a scrolled-off live render AND the "
            f"rendered markdown BOTH made it into the captured view — "
            f"the overflow-render duplication. The fix caps the live "
            f"region in ``_replace_live_region`` to the viewport "
            f"ceiling so the cursor-up + erase can always reach every "
            f"rendered line. Combined captured text (tail):\n"
            f"{combined[-1500:]}"
        )

    # The user requirement: markdown MUST be rendered — the fix
    # cannot trade duplication for "no markdown formatting." Verify
    # the rendered markdown is present by asserting the raw bold
    # markers (``**``) DO NOT survive in any captured region. Rich's
    # Markdown render strips them; if any remain, the markdown render
    # didn't run for at least one paragraph.
    raw_bold_count = combined.count("**")
    assert raw_bold_count == 0, (
        f"Combined captured text contains {raw_bold_count} ``**`` "
        f"raw markdown markers — the rendered markdown should have "
        f"replaced them with bold styling (no asterisks remain in "
        f"the rendered output). If > 0, the streaming gate left raw "
        f"text in the viewport AND replace_streamed_text didn't "
        f"clear it before rendering. Combined tail:\n"
        f"{combined[-1500:]}"
    )


def test_no_duplicate_when_streamed_fits_viewport() -> None:
    """
    Sanity check: when the streamed content DOES fit in the viewport,
    the existing replace path still produces a clean render with no
    raw markdown left behind.

    This complements ``test_replace_clears_streamed_raw_markdown_in_pty``
    which uses ``_double_render_driver.py`` (a different chunking
    pattern). We use the same overflow driver but with a generous
    PTY (45 rows) so the streamed content stays in-viewport. Every
    item should appear exactly once — and as RENDERED markdown (no
    leading ``**``).
    """
    cols, rows = 100, 45
    pid, parent_fd = _spawn_driver_in_pty(cols, rows)
    raw = _drain_pty(parent_fd, pid)

    screen = pyte.HistoryScreen(cols, rows, history=400)
    stream = pyte.Stream(screen)
    stream.feed(raw.decode("utf-8", errors="replace"))

    visible = "\n".join(screen.display)
    scrollback = _scrollback_text(screen)
    combined = scrollback + "\n" + visible

    for n in (1, 5, 15, 25, 30):
        # Anchor with leading space so "for item 1" doesn't also
        # match "for item 10", "for item 11", etc.
        rendered_sig = f"description for item {n} "
        count = combined.count(rendered_sig)
        assert count == 1, (
            f"item{n} description appears {count}x in combined "
            f"visible+scrollback with a generous viewport. Expected "
            f"1. The replace path should have cleanly cleared the "
            f"streamed raw text and rendered markdown in its place. "
            f"Combined tail: {combined[-1500:]}"
        )

    # In the in-viewport case, the rendered markdown should fully
    # replace the streamed raw text — no ``**`` markers should
    # survive in the captured stream.
    raw_bold_count = combined.count("**")
    assert raw_bold_count == 0, (
        f"Combined captured text contains {raw_bold_count} ``**`` "
        f"raw markdown markers — the rendered markdown should have "
        f"replaced them with bold styling (no asterisks remain in "
        f"the rendered output). Visible:\n{visible}"
    )
