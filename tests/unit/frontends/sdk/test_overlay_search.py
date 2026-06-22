"""
E2E test: ``/`` search in the Ctrl+O overlay scrolls the content
pane to the first match and shows a live ``/query (N/M)`` status
in the footer.

Less / vim pagers have trained every developer to hit ``/`` when
they want to find something in a scrollable buffer. This test
drives the UI SDK's overlay through that exact interaction:

1. Open the overlay → the content pane shows the top of a 100+
   line block. A distinctive decoy string is visible; the
   search needle is scrolled off screen, far below.
2. Press ``/`` → footer flips from the idle hint to the
   search-prompt status line.
3. Type the needle (in uppercase, to exercise case-insensitive
   matching) → the content pane scrolls to the matching line.
4. Press Enter → footer returns to the idle hint; the content
   pane stays at the match line (search was COMMITTED, not
   cancelled).

**What breaks if this fails:**

- The ``/`` binding in ``_host.py::_show_overlay`` stops
  setting ``search_active[0] = True`` — the footer never flips
  and no printable keys get captured into ``search_query``.
- The ``<any>`` binding's ``searching`` filter regresses so
  it fires outside search mode too (would swallow every
  keystroke in the idle overlay) OR fails to fire inside
  search mode (the query buffer never grows).
- ``_find_matches`` drops its case-insensitive comparison —
  uppercase query against lowercase content would return no
  hits and the scroll wouldn't move.
- The search-mode binding for Enter stops clearing
  ``search_active[0]`` — the overlay stays in search mode
  forever, with the printable-char handler eating every
  keystroke.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pexpect
import pyte
import pytest

sys.path.insert(0, str(Path(__file__).parent))
# mypy can't resolve this — the driver isn't a proper package
# module, it's only importable after the sys.path insert above.
import contextlib

from _overlay_search_driver import _DECOY, _NEEDLE  # type: ignore[import-not-found]

_DRIVER = Path(__file__).parent / "_overlay_search_driver.py"
_TERM = "xterm-256color"
_ROWS = 40
_COLS = 120

_BOOT_TIMEOUT = 10.0
_RENDER_TIMEOUT = 3.0
_SHUTDOWN_TIMEOUT = 5.0


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
    :returns: The emulated display at the moment of match.
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


@pytest.fixture
def overlay_search_child() -> pexpect.spawn:
    """
    Spawn the search-test driver under a PTY.

    :returns: A live pexpect child. Torn down by sending Ctrl+C
        and Ctrl+D so a hung child never blocks the suite.
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


def test_slash_search_scrolls_to_match(
    overlay_search_child: pexpect.spawn,
) -> None:
    """
    Typing ``/<needle>`` in the overlay must scroll the content
    pane to the first matching line.

    :param overlay_search_child: Fresh PTY-spawned driver from
        the fixture.
    """
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.Stream(screen)

    # 1. Wait for the host prompt.
    _wait_for_screen(
        overlay_search_child,
        screen,
        stream,
        "❯",
        timeout=_BOOT_TIMEOUT,
    )

    # 2. Open the overlay; top-of-content decoy is visible.
    overlay_search_child.sendcontrol("o")
    _wait_for_screen(
        overlay_search_child,
        screen,
        stream,
        _DECOY,
        timeout=_RENDER_TIMEOUT,
    )

    # 3. Press ``/`` — footer should flip to search mode. The
    #    "Enter commit" sentinel in the footer is the signal
    #    that _search_start actually fired and the footer
    #    callable picked up the mode flip.
    overlay_search_child.send("/")
    _wait_for_screen(
        overlay_search_child,
        screen,
        stream,
        "Enter commit",
        timeout=_RENDER_TIMEOUT,
    )

    # 4. Type the needle in UPPERCASE to exercise case-insensitive
    #    matching. As each character lands, the incremental
    #    search re-finds + re-scrolls; by the time the full
    #    needle is typed the match line must be on-screen.
    overlay_search_child.send(_NEEDLE.upper())
    _wait_for_screen(
        overlay_search_child,
        screen,
        stream,
        _NEEDLE,
        timeout=_RENDER_TIMEOUT,
    )

    # 5. Press Enter to commit. Footer should return to the
    #    idle hint (contains ``gg/G``). The needle stays on
    #    screen because commit keeps the scroll position.
    overlay_search_child.send("\r")
    rendered = _wait_for_screen(
        overlay_search_child,
        screen,
        stream,
        "gg/G",
        timeout=_RENDER_TIMEOUT,
    )
    assert _NEEDLE in rendered, (
        f"After Enter commit, needle {_NEEDLE!r} should still be on-screen "
        f"(commit preserves scroll). Rendered:\n{rendered}"
    )
