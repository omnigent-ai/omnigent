"""Shared terminal-output styling for the Omnigent CLI.

This is the one place that owns the consoles, the brand palette, and the
status / structure helpers that every command should print through, so
that ``omnigent``'s output reads as one coherent product. See
``designs/CLI_CONTRACT.md`` for the full contract.

Core rule — **stdout carries data, stderr carries decoration**:

* Machine-readable output (IDs, paths, config dumps, the ``version``
  string) goes to stdout via :data:`console`, so ``omnigent … | cat``
  stays clean.
* Warnings, errors, and the brand banner go to stderr (via
  :data:`err_console` / the banner helpers) and are TTY-gated, so they
  never corrupt piped stdout.

Color is handled by rich: both consoles honor ``NO_COLOR`` and terminal
capability automatically, so callers never emit raw ANSI.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from . import wordmark

#: Brand accent — Otto's magenta-pink, shared with the mascot and banner.
ACCENT = wordmark.WORDMARK_COLOR

#: Env var that force-disables the brand banner even on a TTY. Mirrors the
#: ``OMNIGENT_NO_SPINNER`` convention in :mod:`omnigent._runner_startup`.
NO_BANNER_ENV_VAR = "OMNIGENT_NO_BANNER"

# Named styles, so call sites use semantic tokens ("omni.warning") rather
# than hard-coded colors. Semantic colors stay conventional; only the
# accent is brand-specific.
OMNIGENT_THEME = Theme(
    {
        "omni.accent": ACCENT,
        "omni.success": "green",
        "omni.warning": "yellow",
        "omni.error": "bold red",
        "omni.info": "cyan",
        "omni.muted": "dim",
    }
)

# ``highlight=False`` so rich never auto-recolors numbers / paths / URLs
# inside our messages — CLI output must be predictable. File is resolved
# lazily by rich, so these follow ``CliRunner`` / ``capsys`` stream swaps.
#: Console for stdout — data and primary output.
console = Console(theme=OMNIGENT_THEME, highlight=False)
#: Console for stderr — status, warnings, errors, and the brand banner.
err_console = Console(stderr=True, theme=OMNIGENT_THEME, highlight=False)


def show_banner(*, isatty: bool | None = None, env: dict[str, str] | None = None) -> bool:
    """
    Decide whether the brand banner / brandmark should be drawn.

    The banner is decoration, so it only shows on an interactive stderr
    and can be force-disabled with ``OMNIGENT_NO_BANNER``. Color *within*
    the banner is a separate concern handled by rich (``NO_COLOR`` simply
    renders the art in monochrome).

    :param isatty: Override for ``sys.stderr.isatty()`` (tests pass this
        to exercise both branches without a real PTY).
    :param env: Environment snapshot; defaults to ``os.environ``.
    :returns: ``True`` when the banner should be drawn.
    """
    if isatty is None:
        isatty = sys.stderr.isatty()
    if not isatty:
        return False
    env = os.environ if env is None else env
    raw = str(env.get(NO_BANNER_ENV_VAR, "")).strip().lower()
    return raw not in {"1", "true", "yes", "on"}


# ── Status helpers ────────────────────────────────────────────────────
# A consistent glyph + color per severity. ``step``/``success``/``info``
# are normal status on stdout; ``warn``/``error`` are diagnostics on
# stderr (always correct, never pollutes piped data). The message is
# appended as plain Text so it is never reinterpreted as rich markup.


def _emit(target: Console, glyph: str, style: str, message: str) -> None:
    """
    Print ``<glyph> <message>`` with *glyph* styled, *message* plain.

    :param target: Console to print to (stdout or stderr).
    :param glyph: Leading status glyph, e.g. ``"✓"``.
    :param style: Style name for the glyph, e.g. ``"omni.success"``.
    :param message: Plain message text (never parsed as markup).
    """
    line = Text()
    line.append(f"{glyph} ", style=style)
    line.append(message)
    target.print(line)


def step(message: str) -> None:
    """Print an ``==>`` progress step (accent) to stdout."""
    _emit(console, "==>", "omni.accent", message)


def success(message: str) -> None:
    """Print a ``✓`` success line (green) to stdout."""
    _emit(console, "✓", "omni.success", message)


def info(message: str) -> None:
    """Print a dim ``·`` informational line to stdout."""
    _emit(console, "·", "omni.muted", message)


def warn(message: str) -> None:
    """Print a ``!`` warning (yellow) to stderr."""
    _emit(err_console, "!", "omni.warning", message)


def error(message: str) -> None:
    """Print a ``✗`` error (red) to stderr."""
    _emit(err_console, "✗", "omni.error", message)


# ── Structure helpers ─────────────────────────────────────────────────


def header(title: str) -> None:
    """Print a bold accent section header to stdout."""
    console.print(Text(title, style="bold omni.accent"))


def kv(label: str, value: str, *, label_width: int = 10) -> None:
    """
    Print one aligned ``label   value`` row (dim label, bold value).

    :param label: Left-hand label, e.g. ``"Session"``.
    :param value: Right-hand value, e.g. ``"New session"``.
    :param label_width: Column width the label is padded to.
    """
    line = Text()
    line.append(label.ljust(label_width), style="dim")
    line.append(value, style="bold")
    console.print(line)


def rule(title: str = "") -> None:
    """Print a horizontal accent rule (optionally titled) to stdout."""
    console.rule(title, style="omni.accent")


def table(*, title: str | None = None, **kwargs: object) -> Table:
    """
    Build a :class:`rich.table.Table` pre-styled with the brand palette.

    Callers add columns/rows and then ``console.print(tbl)``. Centralizing
    construction keeps every table's header/border consistent.

    :param title: Optional table title.
    :returns: A configured (empty) ``Table``.
    """
    return Table(
        title=title,
        header_style="bold omni.accent",
        border_style="omni.muted",
        title_style="bold omni.accent",
        **kwargs,  # type: ignore[arg-type]
    )


def panel(renderable: object, *, title: str | None = None, **kwargs: object) -> Panel:
    """
    Wrap *renderable* in a :class:`rich.panel.Panel` with the brand border.

    :param renderable: Any rich renderable or string to box.
    :param title: Optional panel title.
    :returns: A configured ``Panel``.
    """
    return Panel(
        renderable,  # type: ignore[arg-type]
        title=title,
        border_style="omni.accent",
        **kwargs,  # type: ignore[arg-type]
    )


# ── Brand banner ──────────────────────────────────────────────────────


def print_landing(
    *,
    epilogue: list[tuple[str, str]] | None = None,
    gradient: bool = True,
    tagline: str | None = None,
) -> None:
    """
    Print the full Otto + wordmark lockup (the hero banner), TTY-gated.

    Drawn on stderr so it never lands in piped stdout. No-op when the
    banner is suppressed (non-TTY or ``OMNIGENT_NO_BANNER``).

    :param epilogue: Optional aligned ``(label, value)`` rows beneath the
        art (e.g. version / next-step).
    :param gradient: Fade the wordmark magenta→pink (default on for the
        hero moment); falls back to flat accent on low-color terminals.
    :param tagline: Optional dim tagline under the art.
    :returns: None.
    """
    if not show_banner():
        return
    wordmark.render_lockup(err_console, gradient=gradient, tagline=tagline, epilogue=epilogue)


def print_brandmark(subtitle: str | None = None) -> None:
    """
    Print the compact one-line brandmark (``✦ omnigent``), TTY-gated.

    For non-interactive commands that want a branded header without the
    full banner. Drawn on stderr; no-op when the banner is suppressed.

    :param subtitle: Optional dim trailing text, e.g. a version string.
    :returns: None.
    """
    if not show_banner():
        return
    wordmark.render_compact(err_console, subtitle=subtitle)
