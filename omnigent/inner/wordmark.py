"""The Omnigent brand wordmark and Otto lockup for CLI output.

A bold "ANSI-Shadow" block-letter ``omnigent`` wordmark — the canonical
figlet font with one duplicate body row dropped (5 rows), so every letter
stays legible and it sits exactly as tall as the Otto-the-starfish mascot
from :mod:`omnigent.inner.mascots`, which it pairs with 1:1.

This module owns the *art* and its rendering onto a caller-supplied
:class:`rich.console.Console`. The decision of *whether* to draw the
banner (TTY gating, ``OMNIGENT_NO_BANNER``) lives one layer up in
:mod:`omnigent.inner.ui`, which is the only module that should be imported
by command code. Keeping the gate out of here avoids a circular import
(``ui`` imports ``wordmark``) and keeps the art unit-testable in isolation.

The brand color is Otto's magenta-pink ``#F43BA6`` (see
:data:`omnigent.inner.mascots.MASCOT_ART_COLOR`); the optional gradient
fades it toward a lighter pink across the wordmark columns.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from .mascots import MASCOT_ART_COL_WIDTH, MASCOT_ART_COLOR, MASCOT_ART_LINES

# Flat brand accent — kept in sync with the mascot/banner border so the
# wordmark, Otto, and the interactive REPL box all read as one color.
WORDMARK_COLOR = MASCOT_ART_COLOR

# Gradient endpoints (magenta → soft pink). Used only when a caller asks
# for ``gradient=True`` and the terminal supports enough colors; rich
# downgrades or drops the color automatically otherwise.
_GRADIENT_START = (0xF4, 0x3B, 0xA6)  # #F43BA6
_GRADIENT_END = (0xFF, 0x9F, 0xD6)  # #FF9FD6

# Two-space gutter between Otto and the wordmark in the lockup.
_GAP = "  "
# Left indent applied to every printed row so the banner doesn't hug the
# terminal edge (matches the installer's two-space banner indent).
_INDENT = "  "

# Per-letter "ANSI-Shadow" glyphs — the canonical figlet font (as used by
# NeonX and TAAG) with a single near-duplicate body row dropped, leaving 5
# rows. This keeps the full-height, fully-legible letterforms while sitting
# exactly as tall as Otto (5 rows), so the lockup pairs 1:1 with no unpaired
# rows. Stored as a glyph map rather than a frozen multi-line blob so the
# wordmark is regenerable and a missing letter fails loud at import. Each
# glyph's rows are equal display width so columns stay aligned when letters
# are concatenated.
_GLYPH_ROWS = 5
_GLYPHS: dict[str, tuple[str, ...]] = {
    "o": (" ██████╗ ", "██╔═══██╗", "██║   ██║", "╚██████╔╝", " ╚═════╝ "),
    "m": ("███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"),
    "n": ("███╗   ██╗", "████╗  ██║", "██╔██╗ ██║", "██║ ╚████║", "╚═╝  ╚═══╝"),
    "i": ("██╗", "██║", "██║", "██║", "╚═╝"),
    "g": (" ██████╗ ", "██╔════╝ ", "██║  ███╗", "╚██████╔╝", " ╚═════╝ "),
    "e": ("███████╗", "██╔════╝", "█████╗  ", "███████╗", "╚══════╝"),
    "t": ("████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ╚═╝   "),
}

_WORDMARK_TEXT = "omnigent"


def _build_wordmark(word: str) -> tuple[str, ...]:
    """
    Concatenate per-letter glyphs into the wordmark rows.

    :param word: The text to render; every character must have a glyph
        in :data:`_GLYPHS`, e.g. ``"omnigent"``.
    :returns: The wordmark rows (top cap · identity · bottom · shadow).
    """
    rows = ["" for _ in range(_GLYPH_ROWS)]
    for char in word:
        glyph = _GLYPHS[char]
        for i in range(_GLYPH_ROWS):
            rows[i] += glyph[i]
    return tuple(rows)


#: The rows of the ``omnigent`` wordmark, as plain (uncolored) text.
WORDMARK_LINES: tuple[str, ...] = _build_wordmark(_WORDMARK_TEXT)

# Which Otto row each wordmark row sits on. Otto and the wordmark are both
# five rows tall, so they pair 1:1 — no unpaired rows on either side.
_WORDMARK_ROW_FOR_OTTO_ROW = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4}


def wordmark_lines() -> list[str]:
    """
    Return the wordmark on its own (no mascot), as plain text rows.

    :returns: The wordmark rows, e.g. for embedding in a doc or a bash
        banner.
    """
    return list(WORDMARK_LINES)


def lockup_lines() -> list[str]:
    """
    Return the Otto + wordmark lockup as plain text rows (no color).

    Otto sits on the left (5 rows × :data:`MASCOT_ART_COL_WIDTH` cells)
    with the 5-row wordmark aligned 1:1 beside it. Trailing whitespace is
    stripped so the plain form is clean for snapshots and docs.

    :returns: Five rows of the composed lockup.
    """
    out: list[str] = []
    for i, art in enumerate(MASCOT_ART_LINES):
        pad = " " * (MASCOT_ART_COL_WIDTH - cell_len(art))
        wm_index = _WORDMARK_ROW_FOR_OTTO_ROW.get(i)
        wm = WORDMARK_LINES[wm_index] if wm_index is not None else ""
        out.append(f"{_INDENT}{art}{pad}{_GAP}{wm}".rstrip())
    return out


def _blend(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> str:
    """
    Linearly interpolate two RGB triples into a ``#RRGGBB`` hex string.

    :param start: RGB at ``t == 0``, e.g. ``(244, 59, 166)``.
    :param end: RGB at ``t == 1``.
    :param t: Position in ``[0, 1]``.
    :returns: Hex color, e.g. ``"#f43ba6"``.
    """
    r = round(start[0] + (end[0] - start[0]) * t)
    g = round(start[1] + (end[1] - start[1]) * t)
    b = round(start[2] + (end[2] - start[2]) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _wordmark_row_text(row: str, total_width: int, *, gradient: bool) -> Text:
    """
    Render one wordmark row as a styled :class:`rich.text.Text`.

    :param row: The plain wordmark row.
    :param total_width: Full wordmark width, used as the gradient span so
        every row shares the same per-column color ramp.
    :param gradient: When ``True``, fade each column from magenta to pink;
        otherwise paint the whole row the flat brand accent.
    :returns: A styled ``Text`` for the row.
    """
    if not gradient:
        return Text(row, style=WORDMARK_COLOR)
    text = Text()
    span = max(1, total_width - 1)
    for column, char in enumerate(row):
        if char == " ":
            text.append(" ")
            continue
        color = _blend(_GRADIENT_START, _GRADIENT_END, column / span)
        text.append(char, style=color)
    return text


def render_lockup(
    console: Console,
    *,
    gradient: bool = False,
    tagline: str | None = None,
    epilogue: list[tuple[str, str]] | None = None,
) -> None:
    """
    Print the Otto + wordmark lockup to *console*.

    Color is applied via rich styles, so the console's own color settings
    (NO_COLOR, terminal capability) decide whether color actually renders;
    a no-color console prints the same art in monochrome.

    :param console: Destination console (typically ``ui.err_console``).
    :param gradient: Fade the wordmark magenta→pink instead of flat accent.
    :param tagline: Optional dim line printed under the lockup, e.g.
        ``"all your agents, one cli"``.
    :param epilogue: Optional aligned label/value rows printed beneath the
        art, e.g. ``[("Version", "0.4.2"), ("Next", "omnigent setup")]``.
    :returns: None.
    """
    total_width = max(len(line) for line in WORDMARK_LINES)
    console.print()
    for i, art in enumerate(MASCOT_ART_LINES):
        pad = " " * (MASCOT_ART_COL_WIDTH - cell_len(art))
        line = Text(_INDENT)
        line.append(f"{art}{pad}", style=WORDMARK_COLOR)
        wm_index = _WORDMARK_ROW_FOR_OTTO_ROW.get(i)
        if wm_index is not None:
            line.append(_GAP)
            line.append_text(
                _wordmark_row_text(WORDMARK_LINES[wm_index], total_width, gradient=gradient)
            )
        console.print(line)
    if tagline:
        console.print(Text(f"{_INDENT}{tagline}", style="dim"))
    if epilogue:
        console.print()
        _print_epilogue(console, epilogue)
    console.print()


def _print_epilogue(console: Console, rows: list[tuple[str, str]]) -> None:
    """
    Print aligned ``label   value`` rows (dim label, bold value).

    :param console: Destination console.
    :param rows: ``(label, value)`` pairs; labels are left-padded to a
        common width so the values line up.
    :returns: None.
    """
    label_width = max(cell_len(label) for label, _ in rows) + 3
    for label, value in rows:
        line = Text(_INDENT)
        line.append(label.ljust(label_width), style="dim")
        line.append(value, style="bold")
        console.print(line)


def render_compact(console: Console, *, subtitle: str | None = None) -> None:
    """
    Print the one-line brandmark: ``✦ omnigent  <subtitle>``.

    Used as a lightweight branded header on non-interactive commands that
    don't warrant the full lockup (``version``, ``status``, ``upgrade``…).

    :param console: Destination console (typically ``ui.err_console``).
    :param subtitle: Optional dim trailing text, e.g. a version string.
    :returns: None.
    """
    line = Text(_INDENT)
    line.append("✦ ", style=WORDMARK_COLOR)
    line.append("omnigent", style=f"bold {WORDMARK_COLOR}")
    if subtitle:
        line.append(f"  {subtitle}", style="dim")
    console.print(line)
