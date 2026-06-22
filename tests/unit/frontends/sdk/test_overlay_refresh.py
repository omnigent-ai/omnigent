"""
E2E regression test: Overlay content auto-refreshes while the
overlay is open, without requiring a user keystroke.

The bug: before the ``_refresh_loop`` was added to
:meth:`TerminalHost._show_overlay`, the content pane only
rebuilt on overlay open and on Tab/Shift-Tab. When a turn
streamed server-side while the overlay was open, the pane sat
frozen showing pre-turn content — users had to close and re-open
the overlay (or press Tab) to see the new messages. Worse, when
they DID press Tab the intermediate render showed the old
target's content under the new target's sidebar marker,
flickering for one frame before the async rebuild completed.

The fix (``_host.py::_show_overlay``):

1. Spawn ``_refresh_loop`` as a background task on overlay open.
   Every 500 ms it calls ``_rebuild_content(reset_scroll=False)``
   so the user's scroll position isn't yanked on each tick.
2. ``_rebuild_content`` tracks a ``rebuild_generation`` counter;
   any async rebuild whose generation no longer matches at
   completion time drops its result, so two rapid triggers land
   in the correct order.
3. ``_rebuild_content`` computes a ``(target_key, content_raw)``
   signature and early-returns when it matches the last rendered
   frame — a quiet server means no ``invalidate``, no repaint,
   no flicker.
4. Scroll only resets when the target key actually changed
   (Tab) or the caller explicitly asks. Refresh ticks on the
   same target preserve scroll.
5. The refresh task is cancelled in a ``try/finally`` around
   ``app.run_async()`` so Esc / Ctrl+C / builder-exception all
   stop polling cleanly.

This test drives the :mod:`_overlay_refresh_driver` whose builder
bumps a counter on every call, emitting
``OVERLAY_TICK_{n}_XYZZY``. Opening the overlay paints ``tick=1``;
after ~600 ms (one refresh-loop tick with headroom) ``tick=2``
must appear without the test pressing any key. Without the fix,
the second sentinel never shows up and the test times out.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pexpect
import pyte
import pytest


def _wait_for_screen(
    child: pexpect.spawn,
    screen: pyte.Screen,
    stream: pyte.Stream,
    substring: str,
    timeout: float,
) -> str:
    """
    Drain the child's PTY stream into a pyte terminal emulator
    until *substring* appears anywhere on the emulated display,
    or *timeout* elapses. See the Tab-refresh test for the full
    rationale on why pyte is used instead of naive ANSI
    stripping.

    :param child: Live pexpect child.
    :param screen: pyte screen to update.
    :param stream: pyte stream feeding *screen*.
    :param substring: Plain-text sentinel to look for.
    :param timeout: Max seconds to wait.
    :returns: The emulated display (rows joined by newlines) at
        the moment of match.
    :raises pexpect.TIMEOUT: When *substring* never shows up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = child.read_nonblocking(size=4096, timeout=0.1)
        except pexpect.TIMEOUT:
            continue
        stream.feed(chunk)
        rendered = "\n".join(screen.display)
        if substring in rendered:
            return rendered
    rendered = "\n".join(screen.display)
    raise pexpect.TIMEOUT(
        f"did not see {substring!r} within {timeout}s. Rendered screen:\n{rendered}",
    )


_DRIVER = Path(__file__).parent / "_overlay_refresh_driver.py"
_TERM = "xterm-256color"
_ROWS = 40
_COLS = 120

_BOOT_TIMEOUT = 10.0
# First tick lands on overlay open — same window as the
# Tab-refresh test, bounded by the builder runtime (near-zero
# in this driver) plus one render cycle.
_FIRST_TICK_TIMEOUT = 3.0
# Second tick must land within one refresh interval + a render
# cycle. The interval is 500 ms; 2.5 s covers the interval plus
# a generous slop for CI schedulers.
_SECOND_TICK_TIMEOUT = 2.5
_SHUTDOWN_TIMEOUT = 5.0


@pytest.fixture
def overlay_refresh_child() -> pexpect.spawn:
    """
    Spawn the auto-refresh driver under a PTY.

    :returns: A live pexpect child. The fixture tears down by
        sending Ctrl+C then Ctrl+D and force-closing so a hung
        child never blocks the test suite.
    """
    child = pexpect.spawn(
        sys.executable,
        [str(_DRIVER)],
        env={"TERM": _TERM, "LINES": str(_ROWS), "COLUMNS": str(_COLS)},
        encoding="utf-8",
        timeout=_BOOT_TIMEOUT,
        dimensions=(_ROWS, _COLS),
    )
    yield child
    if not child.closed:
        child.sendcontrol("c")
        child.sendcontrol("d")
        with contextlib.suppress(pexpect.TIMEOUT):
            child.expect(pexpect.EOF, timeout=_SHUTDOWN_TIMEOUT)
        child.close(force=True)


def test_overlay_auto_refreshes_without_user_action(
    overlay_refresh_child: pexpect.spawn,
) -> None:
    """
    Opening Ctrl+O must produce a follow-up render after ~500 ms
    even with zero additional user input.

    Steps:

    1. Wait for the host prompt to appear.
    2. Press Ctrl+O → first builder call returns
       ``OVERLAY_TICK_1_XYZZY``; assert it appears on-screen.
    3. WITHOUT any further input, wait for
       ``OVERLAY_TICK_2_XYZZY`` (the periodic refresh's second
       call) to appear.

    **What breaks if this fails:**

    - ``_refresh_loop`` stops being spawned as a background task
      in ``_show_overlay`` (the tick-2 sentinel never renders
      because the builder never runs again after open).
    - The 500 ms sleep interval is increased beyond the 2.5 s
      timeout budget (should be a deliberate change; bump the
      timeout AND update the test docstring).
    - The signature early-return regression treats
      ``(main, content_v1)`` and ``(main, content_v2)`` as
      equal and skips the repaint even though content differs —
      would keep tick-1 sentinel on screen.
    - The ``refresh_task.cancel()`` in the ``finally`` block is
      missing AND the overlay closes fast enough that the task
      leaks — the test would pass but the fixture's shutdown
      path might hang on the dangling task.

    :param overlay_refresh_child: Fresh PTY-spawned driver from
        the fixture.
    """
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.Stream(screen)

    # 1. Wait for the input marker — the host is ready.
    _wait_for_screen(
        overlay_refresh_child,
        screen,
        stream,
        "❯",
        timeout=_BOOT_TIMEOUT,
    )

    # 2. Open the overlay; first tick lands immediately.
    overlay_refresh_child.sendcontrol("o")
    _wait_for_screen(
        overlay_refresh_child,
        screen,
        stream,
        "OVERLAY_TICK_1_XYZZY",
        timeout=_FIRST_TICK_TIMEOUT,
    )

    # 3. Do NOT press any keys. The refresh loop must fire on
    #    its own and produce tick 2 within the budget.
    _wait_for_screen(
        overlay_refresh_child,
        screen,
        stream,
        "OVERLAY_TICK_2_XYZZY",
        timeout=_SECOND_TICK_TIMEOUT,
    )
