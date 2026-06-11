"""Startup banner builder for the Omnigent REPL and onboarding wizard.

Migrated from the legacy ``omnigent.inner.cli`` module during the
``--no-sessions-api`` sunset so that the Omnigent REPL and onboarding wizard can
continue rendering the mascot-art welcome box without pulling in
the legacy CLI.
"""

from __future__ import annotations

from dataclasses import dataclass

from .mascots import (
    MASCOT_ART_COL_WIDTH,
    MASCOT_ART_COLOR,
    random_mascot_lines,
)

_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_PROMPT_GRAY = "\033[38;5;244m"

_CLI_WELCOME_HINT_LINE = "ctrl-c: cancel · ctrl-d: quit"


def _ansi_truecolor_fg(hex_rgb: str) -> str:
    """``#RRGGBB`` → ANSI 24-bit foreground; invalid input falls back to gray."""
    if len(hex_rgb) != 7 or hex_rgb[0] != "#":
        return _PROMPT_GRAY
    try:
        r = int(hex_rgb[1:3], 16)
        g = int(hex_rgb[3:5], 16)
        b = int(hex_rgb[5:7], 16)
    except ValueError:
        return _PROMPT_GRAY
    return f"\033[38;2;{r};{g};{b}m"


def _display_width(text: str) -> int:
    """Terminal display width of *text*, counting VS16 emoji as 2 cells.

    :func:`rich.cells.cell_len` under-counts an emoji forced to its wide
    presentation by a trailing VARIATION SELECTOR-16 (U+FE0F) — e.g. the
    cli-config ``⚙️`` glyph — as a single cell, while modern terminals
    render it as two. Adding one cell per VS16 makes the banner box border
    line up under such a glyph instead of drifting one column right.

    :param text: The string to measure, e.g. ``"⚙️ my-gateway"``.
    :returns: The estimated terminal column width, e.g. ``13``.
    """
    from rich.cells import cell_len

    return cell_len(text) + text.count("\N{VARIATION SELECTOR-16}")


@dataclass(frozen=True)
class RenderedLines:
    """Paired plain / ANSI rendering of a block of REPL output."""

    plain: str
    ansi: str


@dataclass(frozen=True)
class BannerLine:
    """One text row inside the startup banner box.

    :param text: The row's text. Width is measured with
        :func:`rich.cells.cell_len`, so emoji / wide glyphs (e.g. a
        ``🧱 Databricks`` credential label) pad and align correctly.
    :param dim: Render the row dim (``True``, for metadata rows like the
        model + credential, folder, or hint) or bold (``False``, used
        for the title row). Only the title row passes ``False``.
    """

    text: str
    dim: bool


def startup_banner_strings(
    agent_label: str,
    *,
    hint_line: str | None = None,
    info_lines: list[BannerLine] | None = None,
    mascot_lines: list[str] | tuple[str, ...] | None = None,
    art_color: str = MASCOT_ART_COLOR,
) -> RenderedLines:
    """
    Build the startup banner box as both plain and ANSI strings.

    The box shows the mascot art to the left and one bold title row
    (*agent_label*) plus any number of dim info rows to the right —
    so the REPL can surface the working folder, model, credential, and
    a one-line agent summary inside the same box (Claude-Code-style
    header). The box always renders at least as many rows as the mascot
    has, padding with blank rows so the full mascot shows even when
    there is only a title.

    :param agent_label: Bold title row, e.g. the agent name ``"polly"``.
    :param hint_line: Dimmed hint shown as the single info row when
        *info_lines* is not given. ``None`` uses the default hint;
        ``""`` omits the hint row (back-compat with the bare-banner
        callers). Ignored when *info_lines* is provided.
    :param info_lines: Explicit dim info rows shown beneath the title,
        e.g. ``[BannerLine("multi-agent orchestrator", dim=True),
        BannerLine("claude-sonnet-4-6 · Subscription", dim=True),
        BannerLine("~/omnigent", dim=True)]``. ``None`` falls
        back to the *hint_line* behavior.
    :param mascot_lines: Mascot art to show to the left of the
        text. ``None`` picks a random mascot.
    :param art_color: Hex color for the mascot + box border, e.g.
        ``"#F43BA6"``.
    :returns: The box as paired plain / ANSI strings.
    """
    mascot = list(mascot_lines or random_mascot_lines())
    left_pad = " "
    art_w = MASCOT_ART_COL_WIDTH
    gap = 2

    # Row 0 is the bold title; the rest are dim info rows. When no explicit
    # info rows are given, fall back to the single (optional) hint row.
    rows: list[BannerLine] = [BannerLine(agent_label, dim=False)]
    if info_lines is not None:
        rows.extend(info_lines)
    elif hint_line != "":
        rows.append(BannerLine(hint_line or _CLI_WELCOME_HINT_LINE, dim=True))
    # Pad with blank rows so every mascot row has a content row to sit on —
    # otherwise a single-row banner would clip the mascot's lower half.
    while len(rows) < len(mascot):
        rows.append(BannerLine("", dim=True))

    text_w = max(_display_width(r.text) for r in rows)

    def _art_cell(i: int) -> str:
        """Return the mascot art for row *i*, padded/truncated to art_w cells."""
        line = mascot[i] if i < len(mascot) else ""
        pad = art_w - _display_width(line)
        if pad >= 0:
            return line + " " * pad
        return line[:art_w]

    def _content_line(i: int, row: BannerLine) -> RenderedLines:
        """Render one box row (mascot cell + gap + text), plain + ANSI."""
        pad_right = text_w - _display_width(row.text)
        art = _art_cell(i)
        content = f"{left_pad}{art}{' ' * gap}{row.text}{' ' * pad_right}"
        plain = f"│ {content} │"
        text_prefix = _DIM if row.dim else _BOLD
        ansi_content = (
            f"{_ansi_truecolor_fg(art_color)}{left_pad}{art}{_RESET}"
            f"{' ' * gap}"
            f"{text_prefix}{row.text}{_RESET}"
            f"{' ' * pad_right}"
        )
        ansi = (
            f"{_ansi_truecolor_fg(art_color)}│{_RESET} "
            f"{ansi_content}"
            f" {_ansi_truecolor_fg(art_color)}│{_RESET}"
        )
        return RenderedLines(plain=plain, ansi=ansi)

    content_width = len(left_pad) + art_w + gap + text_w
    top = "╭" + ("─" * (content_width + 2)) + "╮"
    bottom = "╰" + ("─" * (content_width + 2)) + "╯"
    top_ansi = f"{_ansi_truecolor_fg(art_color)}{top}{_RESET}"
    bottom_ansi = f"{_ansi_truecolor_fg(art_color)}{bottom}{_RESET}"
    body = [_content_line(i, row) for i, row in enumerate(rows)]
    plain_rows = [top, *(b.plain for b in body), bottom]
    ansi_rows = [top_ansi, *(b.ansi for b in body), bottom_ansi]
    return RenderedLines(plain="\n".join(plain_rows), ansi="\n".join(ansi_rows))
