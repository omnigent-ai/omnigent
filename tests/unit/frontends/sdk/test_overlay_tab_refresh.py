"""
E2E regression test: Overlay Tab repaints the content pane
without requiring a follow-up keystroke.

The bug: pressing Tab in the Ctrl+O debug overlay updated the
selected sidebar index and scheduled ``_rebuild_content`` as a
background task, but neither the handler nor the rebuild called
``app.invalidate()``. prompt-toolkit's :class:`Application` only
re-renders in response to (a) a key press, or (b) an explicit
invalidate. So Tab moved the selection, the rebuild finished
fetching content, and then the content pane kept showing the
previous target's data — until the user pressed an arrow key,
which triggered a render tick and the fresh lines finally
appeared.

The fix (``_host.py``):

1. The Tab / Shift-Tab handlers call ``event.app.invalidate()``
   immediately after mutating ``selected_index`` so the sidebar
   marker moves on the next tick, even while the async rebuild
   is still in flight.
2. ``_rebuild_content`` holds a reference to the running
   ``Application`` via ``app_holder`` and calls
   ``app.invalidate()`` right after writing the fresh content
   lines into ``content_lines_holder`` so the content pane
   repaints as soon as the fetch completes.

This test drives a minimal :class:`TerminalHost` with a static
2-target overlay (``tests/frontends/sdk/_overlay_tab_driver.py``)
and asserts that pressing Tab — and nothing else after it —
reveals the sub target's sentinel content within a short window.
Without the fix, the second ``expect`` would time out because
the new content never gets painted.
"""

from __future__ import annotations

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
    until *substring* appears somewhere on the emulated screen,
    or *timeout* elapses.

    Naive ANSI-stripping does NOT work here. prompt-toolkit diff-
    renders the content pane when the overlay target switches —
    ``MAIN`` and ``SUB`` sentinels share character positions, so
    the renderer uses ``\\x1b[C`` (cursor forward) to skip
    positions where the characters happen to match, writing only
    the differing glyphs. Stripping the escape without simulating
    the cursor movement drops characters that were "inherited"
    from the previous frame, producing a corrupted stripped
    output.

    :class:`pyte.Screen` + :class:`pyte.Stream` is a real
    terminal emulator: it applies cursor movement, line wrap,
    clears, etc. Reading :attr:`pyte.Screen.display` after
    feeding the full byte stream gives the rendered text the
    way the user actually sees it on-screen, which is what the
    test is asserting about.

    :param child: Live pexpect child — must be non-blocking-
        readable (all pexpect children are).
    :param screen: The pyte screen to update. Persisted across
        calls within one test so partial renders accumulate.
    :param stream: The pyte stream feeding *screen*.
    :param substring: Plain-text sentinel to look for anywhere
        in the emulated display grid, e.g.
        ``"OVERLAY_CONTENT_FOR_SUB_TARGET_XYZZY"``.
    :param timeout: Max seconds to wait for the sentinel to
        land on-screen. Each read uses a short 100 ms timeout
        so polling cadence is ~10 Hz.
    :returns: The full emulated display (all rows joined by
        newlines) at the moment of match.
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


_DRIVER = Path(__file__).parent / "_overlay_tab_driver.py"
# Same sentinels the driver emits. Kept in sync by import rather
# than re-declaration to avoid drift — any future rename of the
# sentinels in the driver fails the ``_MAIN_SENTINEL`` import.
sys.path.insert(0, str(_DRIVER.parent))
# mypy can't resolve this — the driver isn't a proper package
# module, it's only importable after the sys.path insert above.
# That's intentional: the driver is a throwaway script the pexpect
# subprocess executes, and the only thing the test itself needs
# from it is the two sentinel strings.
import contextlib  # noqa: E402 — must come after the sys.path insert above so mypy picks up the driver module reliably

from _overlay_tab_driver import (  # type: ignore[import-not-found]  # noqa: E402
    _MAIN_SENTINEL,
    _SUB_SENTINEL,
)

_TERM = "xterm-256color"
_ROWS = 40
_COLS = 120

# PTY boot: importing omnigent_ui_sdk + booting a prompt-toolkit
# Application takes a second or two on a cold Python process.
_BOOT_TIMEOUT = 10.0
# Post-keystroke expects: prompt-toolkit's redraw cycle is bounded
# by the 50ms key-input poller + content builder runtime. 3s is
# generous — the pre-fix failure mode is an infinite hang, not a
# slow render.
_RENDER_TIMEOUT = 3.0
# Shutdown: Ctrl+D → app.exit() → asyncio loop drain.
_SHUTDOWN_TIMEOUT = 5.0


@pytest.fixture
def overlay_driver_child() -> pexpect.spawn:
    """
    Spawn the overlay-test driver under a PTY.

    :returns: A live pexpect child running the driver. The fixture
        tears down by sending Ctrl+D then force-closing so a hung
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


def test_overlay_tab_repaints_without_followup_keystroke(
    overlay_driver_child: pexpect.spawn,
) -> None:
    """
    Pressing Tab after Ctrl+O must repaint the content pane
    without any additional keystroke.

    Steps:

    1. Wait for the host to boot (marker prompt visible).
    2. Press Ctrl+O → main target's sentinel appears.
    3. Press Tab *only* — no arrow keys, no follow-up input.
    4. Assert sub target's sentinel appears within
       :data:`_RENDER_TIMEOUT`.

    **What breaks if this fails:**

    - Tab handler in ``_host.py::_show_overlay`` stops calling
      ``event.app.invalidate()`` after mutating
      ``selected_index`` (sidebar marker would move but content
      pane wouldn't repaint until a render tick is triggered).
    - ``_rebuild_content`` stops calling ``app.invalidate()``
      after the async content fetch completes (the new content
      sits in the line cache but prompt-toolkit doesn't know to
      read it).
    - ``app_holder`` stops being populated before
      ``app.run_async()`` (``_rebuild_content``'s
      ``invalidate()`` call silently becomes a no-op).
    - The rebuild coroutine is run via ``asyncio.create_task``
      outside prompt-toolkit's event loop (the invalidate would
      fire but on the wrong app context).

    :param overlay_driver_child: Fresh PTY-spawned driver from
        the fixture — independent per test so parallel runs
        don't share state.
    """
    # Fresh pyte screen per test so bytes from a prior test can't
    # leak into this screen's display.
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.Stream(screen)

    # 1. Boot — wait for the welcome panel's model label to land
    #    on-screen. The driver sets ``model_name="test"`` so
    #    that's the anchor.
    _wait_for_screen(
        overlay_driver_child,
        screen,
        stream,
        "❯",
        timeout=_BOOT_TIMEOUT,
    )

    # 2. Open the overlay. Default selection is index 0 → main.
    overlay_driver_child.sendcontrol("o")
    _wait_for_screen(
        overlay_driver_child,
        screen,
        stream,
        _MAIN_SENTINEL,
        timeout=_RENDER_TIMEOUT,
    )

    # 3. Tab once. Send ONLY the Tab byte — no subsequent keys.
    #    prompt-toolkit binds ``tab`` to the literal 0x09, which
    #    is what ``\t`` becomes over the PTY.
    overlay_driver_child.send("\t")

    # 4. Wait for the sub sentinel. Before the fix, this hung —
    #    ``selected_index`` moved but the content pane never
    #    repainted because neither invalidate() nor a fresh
    #    render tick fired.
    _wait_for_screen(
        overlay_driver_child,
        screen,
        stream,
        _SUB_SENTINEL,
        timeout=_RENDER_TIMEOUT,
    )


def test_overlay_shift_tab_repaints_without_followup_keystroke(
    overlay_driver_child: pexpect.spawn,
) -> None:
    """
    Shift-Tab must repaint too — not just Tab.

    Same regression shape as Tab, since both handlers share the
    same ``_rebuild_content`` + ``invalidate`` contract. A
    separate test guards against a partial fix that only updates
    one of the two handlers.

    :param overlay_driver_child: Fresh PTY-spawned driver from
        the fixture.
    """
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.Stream(screen)
    _wait_for_screen(
        overlay_driver_child,
        screen,
        stream,
        "❯",
        timeout=_BOOT_TIMEOUT,
    )
    overlay_driver_child.sendcontrol("o")
    _wait_for_screen(
        overlay_driver_child,
        screen,
        stream,
        _MAIN_SENTINEL,
        timeout=_RENDER_TIMEOUT,
    )
    # prompt-toolkit's ``s-tab`` binding matches the CSI Z
    # escape (``\x1b[Z``), which is the standard Shift-Tab
    # sequence on xterm-compatible terminals. Sending the raw
    # bytes is more reliable than ``child.send`` of a key name.
    overlay_driver_child.send("\x1b[Z")
    _wait_for_screen(
        overlay_driver_child,
        screen,
        stream,
        _SUB_SENTINEL,
        timeout=_RENDER_TIMEOUT,
    )
