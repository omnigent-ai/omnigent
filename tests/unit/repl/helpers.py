"""Shared test helpers for ``tests/repl/``.

Utilities used by more than one command-test module live here so they
don't get copy-pasted into every file.
"""

from __future__ import annotations

from rich.console import Console


class CapturingHost:
    """
    Minimal host stub that records Rich renderables as plain text.

    Used by slash-command tests that need to assert on rendered output
    without spinning up a real terminal. Each ``output()`` call is
    flushed through a recording :class:`rich.console.Console` and
    appended to ``lines``; ``text`` joins them for easy substring checks.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.console = Console(record=True, width=120)

    def output(self, renderable: object, *, soft_wrap: bool = False) -> None:
        """
        Record a renderable as plain text.

        :param renderable: Any Rich-renderable object.
        :param soft_wrap: Ignored — present to match the real
            :meth:`TerminalHost.output` signature.
        """
        self.console.print(renderable)
        self.lines.append(self.console.export_text(clear=True))

    @property
    def text(self) -> str:
        """
        All captured output joined into a single string.

        :returns: Concatenated plain-text output from every
            ``output()`` call so far.
        """
        return "".join(self.lines)
