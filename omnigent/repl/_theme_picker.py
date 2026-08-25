"""Startup theme picker and shared preview builders.

Two entry points:

- :func:`startup_theme_picker` — interactive arrow-key menu shown on
  first launch (before the REPL's prompt-toolkit Application starts).
  Uses raw termios for keypress reading and OSC 11 detection for
  default selection.  Persists the choice to ``~/.omnigent/config.yaml``.

- The ``/theme`` slash command in ``_repl.py`` uses
  :func:`_build_preview` and :func:`build_theme_confirmation` to
  render the preview panel and confirmation line via ``host.output()``,
  integrating cleanly with prompt-toolkit (no alternate screen buffer,
  no nested Application).

Shared helpers:

- :func:`_build_dark_preview` / :func:`_build_light_preview` build
  Rich ``Panel`` previews used by both paths.
- :func:`_render_theme_picker` assembles the menu + preview into a
  single ANSI string for the startup picker.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from typing import IO, Literal

from omnigent_ui_sdk.terminal._config import update_user_config
from omnigent_ui_sdk.terminal._theme import (
    DARK_THEME,
    LIGHT_THEME,
    TerminalTheme,
    TerminalThemeName,
)
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Accent and muted match the REPL palette defaults so the picker
# looks part of the same surface.
_ACCENT = "#F43BA6"
_MUTED = "#6a6a6a"

# Menu items in display order.
_ITEMS: list[tuple[TerminalThemeName, str]] = [
    ("dark", "dark mode"),
    ("light", "light mode"),
]


# ── Shared preview builders ───────────────────────────────────


def _build_dark_preview(width: int = 52) -> Panel:
    """Build a Rich Panel previewing the dark theme palette.

    :param width: Panel width in columns, e.g. ``52``.
    :returns: A :class:`rich.panel.Panel` showing sample dark-theme
        output.
    """
    body = Text.from_markup(
        f"  [{DARK_THEME.assistant}]assistant>[/{DARK_THEME.assistant}] "
        f"Hello, I can help with that.\n"
        f'  [{DARK_THEME.muted}]⏵ read_file(path="src/main.py")[/{DARK_THEME.muted}]\n'
        f"  [{DARK_THEME.success}]✓ tool completed[/{DARK_THEME.success}]  "
        f"[{DARK_THEME.warning}]⚠ retry[/{DARK_THEME.warning}]  "
        f"[{DARK_THEME.error}]✗ error[/{DARK_THEME.error}]\n"
        f"  [{DARK_THEME.reasoning_style}]thinking step-by-step…[/{DARK_THEME.reasoning_style}]"
    )
    return Panel(
        body,
        title=Text.from_markup(f"[bold]dark[/bold] [{_MUTED}]preview[/{_MUTED}]"),
        title_align="left",
        border_style=_ACCENT,
        width=width,
        padding=(0, 1),
    )


def _build_light_preview(width: int = 52) -> Panel:
    """Build a Rich Panel previewing the light theme palette.

    :param width: Panel width in columns, e.g. ``52``.
    :returns: A :class:`rich.panel.Panel` showing sample light-theme
        output.
    """
    body = Text.from_markup(
        f"  [{LIGHT_THEME.assistant}]assistant>[/{LIGHT_THEME.assistant}] "
        f"Hello, I can help with that.\n"
        f'  [{LIGHT_THEME.muted}]⏵ read_file(path="src/main.py")[/{LIGHT_THEME.muted}]\n'
        f"  [{LIGHT_THEME.success}]✓ tool completed[/{LIGHT_THEME.success}]  "
        f"[{LIGHT_THEME.warning}]⚠ retry[/{LIGHT_THEME.warning}]  "
        f"[{LIGHT_THEME.error}]✗ error[/{LIGHT_THEME.error}]\n"
        f"  [{LIGHT_THEME.reasoning_style}]thinking step-by-step…[/{LIGHT_THEME.reasoning_style}]"
    )
    return Panel(
        body,
        title=Text.from_markup(f"[bold]light[/bold] [{_MUTED}]preview[/{_MUTED}]"),
        title_align="left",
        border_style=_ACCENT,
        width=width,
        padding=(0, 1),
    )


def _build_preview(name: TerminalThemeName, width: int = 52) -> Panel:
    """Dispatch to the correct preview builder.

    :param name: Theme name, ``"dark"`` or ``"light"``.
    :param width: Panel width in columns.
    :returns: A :class:`rich.panel.Panel` for the requested theme.
    """
    if name == "dark":
        return _build_dark_preview(width)
    return _build_light_preview(width)


# ── Startup picker rendering ─────────────────────────────────


def _render_theme_picker(
    selected: int,
    *,
    width: int = 60,
) -> str:
    """Render the menu + preview to an ANSI string.

    Used by the startup picker's raw-termios loop.  The caller writes
    this string to stdout after clearing the picker region.

    :param selected: Zero-based index into :data:`_ITEMS` for the
        currently highlighted item.
    :param width: Terminal width for rendering.
    :returns: ANSI-styled string ready for ``stdout.write()``.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=width, highlight=False)

    # Header.
    console.print(
        Text.from_markup(f"\n  [{_ACCENT}]Choose your theme[/{_ACCENT}]"),
    )
    console.print()

    # Menu items.
    for i, (_name, label) in enumerate(_ITEMS):
        if i == selected:
            console.print(Text.from_markup(f"    [bold {_ACCENT}]❯ {label}[/]"))
        else:
            console.print(Text.from_markup(f"    [dim]  {label}[/dim]"))

    console.print()

    # Preview panel for the selected theme only.
    preview_width = min(52, width - 4)
    preview = _build_preview(_ITEMS[selected][0], width=preview_width)
    # Indent the preview panel.
    console.print(preview, justify="left")

    # Footer hints.
    console.print()
    console.print(
        Text.from_markup(
            f"  [{_MUTED}]↑/↓ navigate  ·  Enter confirm  ·  Esc accept current[/{_MUTED}]"
        ),
    )

    return buf.getvalue()


# ── OSC 11 background detection ──────────────────────────────


def _detect_terminal_background() -> Literal["dark", "light"] | None:
    """Probe the terminal's background color via OSC 11.

    Sends ``\\033]11;?\\033\\\\`` and reads the response.  Most modern
    terminals (iTerm2, Kitty, WezTerm, Ghostty, GNOME Terminal, Alacritty)
    reply with ``\\033]11;rgb:RRRR/GGGG/BBBB\\033\\\\``.  We parse the
    RGB components to compute perceived luminance and classify as
    dark (< 0.5) or light (>= 0.5).

    Returns ``None`` when detection fails (timeout, non-tty, tmux
    passthrough not supported, etc.).

    :returns: ``"dark"``, ``"light"``, or ``None`` on failure.
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None

    try:
        import select
        import termios
        import tty
    except ModuleNotFoundError:
        # Windows has no termios or tty support, even when its terminal
        # streams report that they are TTYs.
        return None

    fd = sys.stdin.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        return None

    try:
        tty.setraw(fd)
        # Send OSC 11 query.
        os.write(sys.stdout.fileno(), b"\033]11;?\033\\")

        # Wait for response with a short timeout (200ms).
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            return None

        # Read response bytes (max 64 chars is plenty for the response).
        response = b""
        while True:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            response += chunk
            # Check for terminator.
            if b"\033\\" in response or b"\x07" in response:
                break

        return _parse_osc11_response(response.decode("utf-8", errors="replace"))
    except (OSError, termios.error):
        return None
    finally:
        with contextlib.suppress(termios.error):
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _parse_osc11_response(response: str) -> Literal["dark", "light"] | None:
    """Parse an OSC 11 response into a dark/light classification.

    Expected format: ``\\033]11;rgb:RRRR/GGGG/BBBB\\033\\\\``
    where each component is a hex value (2 or 4 hex digits).

    :param response: Raw response string from the terminal.
    :returns: ``"dark"`` or ``"light"`` based on perceived luminance,
        or ``None`` if the response cannot be parsed.
    """
    # Find the rgb: part.
    idx = response.find("rgb:")
    if idx < 0:
        return None

    # Extract the rgb:RRRR/GGGG/BBBB portion.
    rgb_start = idx + 4
    # Find end — terminated by ESC, BEL, or end of string.
    rgb_end = len(response)
    for term in ("\033", "\x07"):
        pos = response.find(term, rgb_start)
        if pos >= 0 and pos < rgb_end:
            rgb_end = pos

    rgb_str = response[rgb_start:rgb_end].strip()
    parts = rgb_str.split("/")
    if len(parts) != 3:
        return None

    try:
        # Components can be 2 or 4 hex digits; normalize to 0–1 range.
        values = []
        for part in parts:
            part = part.strip()
            val = int(part, 16)
            if len(part) <= 2:
                values.append(val / 255.0)
            else:
                values.append(val / 65535.0)
        r, g, b = values
    except (ValueError, IndexError):
        return None

    # Perceived luminance (ITU-R BT.601).
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "dark" if luminance < 0.5 else "light"


# ── Startup picker (raw termios) ─────────────────────────────


def startup_theme_picker(
    *,
    out: IO[str] | None = None,
) -> TerminalTheme:
    """Show an interactive theme picker on first launch.

    Uses raw termios for keypress reading (prompt-toolkit is not
    running yet).  Detects the terminal's background via OSC 11 to
    pre-select the right default.  Persists the choice to config.

    :param out: Output stream override (for testing). Defaults to
        ``sys.stdout``.
    :returns: The selected :class:`TerminalTheme`.
    """
    out_stream = out if out is not None else sys.stdout
    term_width = _term_width()

    # Pre-select based on terminal background detection.
    detected = _detect_terminal_background()
    selected = 0 if detected == "dark" else 1  # dark=0, light=1

    if not sys.stdin.isatty():
        # Non-interactive — use detected or default to light.
        theme = DARK_THEME if detected == "dark" else LIGHT_THEME
        update_user_config(theme=theme.name)
        return theme

    try:
        import termios
        import tty
    except ModuleNotFoundError:
        # Raw terminal mode is unavailable on Windows. Use the same
        # persisted fallback as other non-interactive terminals.
        theme = DARK_THEME if detected == "dark" else LIGHT_THEME
        update_user_config(theme=theme.name)
        return theme

    fd = sys.stdin.fileno()
    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        # Can't enter raw mode — fall back to detected/default.
        theme = DARK_THEME if detected == "dark" else LIGHT_THEME
        update_user_config(theme=theme.name)
        return theme

    # Track the number of lines last drawn so the next redraw can
    # move the cursor back to overwrite the previous frame.
    prev_lines = [0]

    try:
        # Draw initial picker.
        _redraw_picker(out_stream, selected, term_width, prev_lines)

        tty.setcbreak(fd)
        while True:
            c = _read_raw_byte(fd)
            if c is None:
                break

            if c == "\x03":
                # Ctrl-C: accept current selection.
                break
            if c == "\x1b":
                # Could be Escape alone or start of arrow sequence.
                next_c = _read_raw_byte_timeout(fd, timeout=0.05)
                if next_c is None:
                    # Bare Escape — accept current.
                    break
                if next_c == "[":
                    arrow = _read_raw_byte_timeout(fd, timeout=0.05)
                    if arrow == "A":  # Up
                        selected = (selected - 1) % len(_ITEMS)
                        _redraw_picker(out_stream, selected, term_width, prev_lines)
                    elif arrow == "B":  # Down
                        selected = (selected + 1) % len(_ITEMS)
                        _redraw_picker(out_stream, selected, term_width, prev_lines)
                    # Ignore other sequences.
                continue

            if c in ("\r", "\n"):
                # Enter — confirm.
                break
            if c in ("k", "K"):
                # vi-style up.
                selected = (selected - 1) % len(_ITEMS)
                _redraw_picker(out_stream, selected, term_width, prev_lines)
            elif c in ("j", "J"):
                # vi-style down.
                selected = (selected + 1) % len(_ITEMS)
                _redraw_picker(out_stream, selected, term_width, prev_lines)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    # Clear the picker from the screen.
    _clear_picker(out_stream, prev_lines[0])

    theme = DARK_THEME if _ITEMS[selected][0] == "dark" else LIGHT_THEME
    update_user_config(theme=theme.name)
    return theme


def _redraw_picker(
    out: IO[str],
    selected: int,
    width: int,
    prev_lines: list[int],
) -> None:
    """Clear the picker region and redraw.

    :param out: Output stream.
    :param selected: Currently highlighted item index.
    :param width: Terminal width.
    :param prev_lines: Single-element list tracking how many lines the
        previous render occupied.  Updated in place so the next call
        can erase the right number of lines.
    """
    rendered = _render_theme_picker(selected, width=width)
    line_count = rendered.count("\n")
    # Move up to overwrite the previous frame if one was drawn.
    if prev_lines[0] > 0:
        out.write(f"\033[{prev_lines[0]}A")
    out.write("\033[J")  # Clear from cursor to end of screen.
    out.write(rendered)
    out.flush()
    prev_lines[0] = line_count


def _clear_picker(out: IO[str], line_count: int) -> None:
    """Erase the picker output from the terminal.

    :param out: Output stream.
    :param line_count: Number of lines the last render occupied.
    """
    if line_count <= 0:
        return
    # Move up and clear so the banner starts on a clean screen.
    out.write(f"\033[{line_count}A\033[J")
    out.flush()


def _read_raw_byte(fd: int) -> str | None:
    """Read a single byte from raw fd, blocking.

    :param fd: File descriptor.
    :returns: Single character, or ``None`` on EOF.
    """
    data = os.read(fd, 1)
    if not data:
        return None
    return data.decode("utf-8", errors="replace")


def _read_raw_byte_timeout(fd: int, *, timeout: float = 0.05) -> str | None:
    """Read a single byte with a short timeout.

    :param fd: File descriptor.
    :param timeout: Seconds to wait, e.g. ``0.05``.
    :returns: Single character, or ``None`` on timeout/EOF.
    """
    import select

    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    return _read_raw_byte(fd)


def _term_width() -> int:
    """Return the current terminal width, clamped to a sane minimum.

    :returns: Terminal columns, at least ``40``.
    """
    try:
        return max(40, os.get_terminal_size().columns)
    except (OSError, ValueError):
        return 80


# ── REPL /theme integration ──────────────────────────────────


def build_theme_confirmation(
    theme: TerminalTheme,
) -> Text:
    """Build the ``❯ <theme> mode (saved)`` confirmation line.

    :param theme: The theme that was just applied.
    :returns: A :class:`rich.text.Text` for ``host.output()``.
    """
    return Text.from_markup(
        f"  [{_ACCENT}]❯[/{_ACCENT}] [bold]{theme.name}[/bold] [{_MUTED}]mode (saved)[/{_MUTED}]"
    )
