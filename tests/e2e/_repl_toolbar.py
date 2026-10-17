"""Wait for the REPL's idle toolbar on the rendered screen, not the raw PTY bytes.

prompt_toolkit repaints only the cells that changed, so an idle toolbar can
arrive as ``state: sl\\x1b[Ceping`` or without its unchanged ``·``; a regex over
the raw stream then never matches even though the screen reads ``· ready``.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pyte
import pytest

pexpect = pytest.importorskip("pexpect")

_READY_RE = r"·\s*ready"
_POLL_S = 0.25
_TOOLBAR_MARKER = "Ctrl+C exit"


class _ToolbarScreen:
    """``logfile_read`` tee that renders PTY output into a pyte screen.

    :param cols: Terminal width the child was spawned with, e.g. ``120``.
    :param rows: Terminal height the child was spawned with, e.g. ``40``.
    :param inner: The child's previous ``logfile_read`` (still written to), or ``None``.
    """

    def __init__(self, cols: int, rows: int, inner: Any) -> None:
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._inner = inner
        # Set by every send; cleared once the toolbar shows a non-idle state,
        # so a toolbar still idle from before the send is never mistaken for the
        # post-turn settle.
        self.awaiting_busy = False

    def write(self, data: str) -> None:
        """Render *data* and forward it to the wrapped log.

        :param data: Decoded PTY output chunk.
        """
        self._stream.feed(data)
        if self.awaiting_busy and self.idle() is False:
            self.awaiting_busy = False
        if self._inner is not None:
            self._inner.write(data)

    def flush(self) -> None:
        """Flush the wrapped log, if any."""
        if self._inner is not None:
            self._inner.flush()

    def close(self) -> None:
        """Close the wrapped log, if any."""
        if self._inner is not None:
            self._inner.close()

    def toolbar_text(self) -> str:
        """Return the rendered screen from the last toolbar row on, whitespace-collapsed.

        :returns: Toolbar text, e.g. ``"hello world · ready /help help … state: sleeping"``;
            empty when no toolbar is on screen.
        """
        rows = ["".join(row) for row in self._screen.display]
        last = max((i for i, row in enumerate(rows) if _TOOLBAR_MARKER in row), default=None)
        if last is None:
            return ""
        # The toolbar can wrap onto the next row at narrow widths.
        start = max(last - 1, 0)
        return re.sub(r"\s+", " ", "".join(rows[start : last + 2]))

    def idle(self) -> bool | None:
        """Return whether the rendered toolbar reads idle.

        :returns: ``True`` for ``state: sleeping`` (or ``· ready`` when the state
            segment is not drawn), ``False`` for any other state, ``None`` when no
            toolbar is on screen.
        """
        text = self.toolbar_text()
        if not text:
            return None
        state = text.rfind("state:")
        if state != -1:
            return text[state + len("state:") :].lstrip().startswith("sleeping")
        return re.search(_READY_RE, text) is not None


def track_toolbar(child: Any) -> _ToolbarScreen:
    """Return *child*'s toolbar screen, installing it on first use.

    Install before the first read so the screen sees every byte.

    :param child: A live ``pexpect.spawn`` child.
    :returns: The child's :class:`_ToolbarScreen`.
    """
    existing = child.logfile_read
    if isinstance(existing, _ToolbarScreen):
        return existing
    rows, cols = child.getwinsize()
    screen = _ToolbarScreen(cols, rows, existing)
    child.logfile_read = screen
    original_send = child.send

    def _send(s: Any) -> int:
        screen.awaiting_busy = True
        return original_send(s)

    child.send = _send
    return screen


def expect_idle_toolbar(child: Any, timeout: float) -> None:
    """Block until the REPL toolbar reads idle (``· ready`` / ``state: sleeping``).

    Matches the raw ``· ready`` text when it is painted whole, otherwise the
    rendered screen. ``child.before`` is left holding all output read while waiting.

    :param child: A live ``pexpect.spawn`` child.
    :param timeout: Max seconds to wait, e.g. ``45.0``.
    :raises pexpect.TIMEOUT: If the toolbar does not settle idle in time.
    """
    screen = track_toolbar(child)
    deadline = time.monotonic() + timeout
    spans: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            child.before = "".join(spans)
            raise pexpect.TIMEOUT(
                f"REPL toolbar not idle within {timeout:.0f}s; "
                f"rendered toolbar: {screen.toolbar_text()!r}"
            )
        index = child.expect([_READY_RE, pexpect.TIMEOUT], timeout=min(_POLL_S, remaining))
        spans.append(child.before)
        if index == 0 or (not screen.awaiting_busy and screen.idle()):
            child.before = "".join(spans)
            return
