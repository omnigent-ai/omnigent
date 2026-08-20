"""Tests for the Omnigent brand wordmark and Otto lockup."""

from __future__ import annotations

from rich.cells import cell_len
from rich.console import Console

from omnigent.inner import wordmark
from omnigent.inner.mascots import MASCOT_ART_COLOR, MASCOT_ART_LINES


def test_wordmark_is_five_rows_of_equal_display_width() -> None:
    """The wordmark renders as five columns-aligned rows (Otto's height)."""

    assert len(wordmark.WORDMARK_LINES) == 5
    widths = {cell_len(line) for line in wordmark.WORDMARK_LINES}
    assert len(widths) == 1, f"wordmark rows misaligned: {widths}"


def test_wordmark_uses_brand_color() -> None:
    """The wordmark accent stays in sync with the mascot brand color."""

    assert wordmark.WORDMARK_COLOR == MASCOT_ART_COLOR == "#F43BA6"


def test_every_letter_in_omnigent_has_a_glyph() -> None:
    """The glyph map covers every letter rendered, and only symbols."""

    for char in "omnigent":
        assert char in wordmark._GLYPHS
    # The art is symbol-only — no letters or digits leak into the rows.
    assert all(not any(c.isalnum() for c in line) for line in wordmark.WORDMARK_LINES)


def test_lockup_lines_pair_otto_with_wordmark() -> None:
    """The lockup is Otto (5 rows) with the 5-row wordmark aligned 1:1."""

    lines = wordmark.lockup_lines()
    assert len(lines) == len(MASCOT_ART_LINES) == 5
    # Every row pairs Otto with a wordmark row; the cap and body rows carry
    # block glyphs (the final row is the all-line-art drop shadow).
    assert "█" in lines[0]
    assert "█" in lines[2]
    # Plain text form carries no ANSI escapes.
    assert all("\x1b[" not in line for line in lines)


def test_render_lockup_plain_console_has_no_ansi() -> None:
    """A no-color console renders the art in monochrome (no escapes)."""

    console = Console(no_color=True, width=120, file=_StringFile())
    wordmark.render_lockup(console)
    assert "\x1b[" not in console.file.getvalue()  # type: ignore[attr-defined]


def test_render_lockup_color_console_emits_ansi() -> None:
    """A color terminal renders the lockup with ANSI color codes."""

    console = Console(force_terminal=True, width=120, file=_StringFile())
    wordmark.render_lockup(console, gradient=True)
    assert "\x1b[" in console.file.getvalue()  # type: ignore[attr-defined]


def test_render_compact_includes_name() -> None:
    """The compact brandmark prints the product name and any subtitle."""

    console = Console(no_color=True, width=120, file=_StringFile())
    wordmark.render_compact(console, subtitle="0.4.2")
    out = console.file.getvalue()  # type: ignore[attr-defined]
    assert "omnigent" in out
    assert "0.4.2" in out
    assert "✦" in out


class _StringFile:
    """Minimal in-memory text file for capturing rich Console output."""

    def __init__(self) -> None:
        self._buf: list[str] = []

    def write(self, text: str) -> int:
        self._buf.append(text)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - rich calls this
        pass

    def getvalue(self) -> str:
        return "".join(self._buf)
