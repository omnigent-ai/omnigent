"""Render a pexpect child's terminal output so tests can wait on what a user sees."""

from __future__ import annotations

import time
from collections.abc import Callable

import pexpect
import pyte


class TerminalScreen:
    """Feed everything a pexpect child prints into a ``pyte`` screen.

    prompt-toolkit repaints only changed cells and skips unchanged ones with
    cursor moves, so visible text need not appear contiguously in the raw
    stream. Waiting on the rendered screen does not depend on which frames
    were painted.
    """

    def __init__(self, child: pexpect.spawn) -> None:
        """Attach to *child* before it prints, so the screen sees every byte.

        :param child: A text-mode ``pexpect.spawn`` (``encoding`` set) whose
            ``logfile_read`` is unused.
        """
        if child.logfile_read is not None:
            raise ValueError("TerminalScreen needs the child's unused logfile_read")
        rows, cols = child.getwinsize()
        self._child = child
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        child.logfile_read = self

    def write(self, data: str) -> None:
        """Render a chunk pexpect read, including chunks ``expect()`` consumed."""
        self._stream.feed(data)

    def flush(self) -> None:
        """Satisfy pexpect's log-file protocol; rendering needs no flush."""

    def text(self) -> str:
        """Return the visible screen, one line per row, without trailing spaces."""
        return "\n".join(line.rstrip() for line in self._screen.display)

    def wait_for(self, predicate: Callable[[str], bool], *, timeout: float, what: str) -> str:
        """Read output until *predicate* accepts the rendered screen.

        :param predicate: Called with :meth:`text` after each read.
        :param timeout: Seconds to wait, e.g. ``30.0``.
        :param what: Description used in the failure message, e.g.
            ``"the first turn to finish"``.
        :returns: The screen text that satisfied *predicate*.
        :raises TimeoutError: If *timeout* elapses first; the message holds the screen.
        :raises EOFError: If the child exits first; the message holds the screen.
        """
        deadline = time.monotonic() + timeout
        while not predicate(screen := self.text()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Screen did not show {what} within {timeout:g}s:\n{screen}")
            try:
                self._child.read_nonblocking(size=65536, timeout=min(0.1, remaining))
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF as error:
                raise EOFError(
                    f"Process exited before the screen showed {what}:\n{screen}"
                ) from error
        return screen
