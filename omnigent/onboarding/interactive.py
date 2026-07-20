"""Theme-picker-styled interactive selectors for the onboarding CLI.

This module is the shared home for the generic, REPL-theme-picker-styled
selection primitives used by ``omnigent setup --no-internal-beta`` (and any
future configure noun). The look reproduces
:mod:`omnigent.repl._theme_picker`:

- a bold accent header line,
- one row per option (selected → ``❯  <label>`` in bold accent, others
  normal weight; non-selectable sub-lines dim italic and indented beneath),
- a muted footer hint line (``↑/↓ move  ·  Enter select  ·  Esc back``),
- a raw-termios keypress loop with in-place redraw (move up, clear,
  reprint), and
- a non-TTY numbered fallback so pipes / CI / tests work.

The raw-termios reading and redraw mechanics are deliberately ported
from :mod:`omnigent.repl._theme_picker` rather than imported: that
module exposes only private helpers, and this module is the generic,
reusable version. The duplication is intentional for now — a future
cleanup can converge ``_theme_picker._render_theme_picker`` onto
:func:`select`. That convergence is out of scope here;
``_theme_picker.py`` is left unchanged.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Callable
from typing import Protocol, cast

import click
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

# Reuse the REPL theme picker's palette verbatim so the selector is
# visually identical to ``_theme_picker.py`` (``_ACCENT`` / ``_MUTED``).
ACCENT = "#F43BA6"
MUTED = "#6a6a6a"

# Shared Rich console for all onboarding interactive output. A module
# singleton so all callers render through one surface.
console = Console()


class _TermUIWithHiddenPrompt(Protocol):
    hidden_prompt_func: Callable[[str], str]


def clear_screen() -> None:
    """Clear the terminal (screen + scrollback) and home the cursor.

    The :func:`select` picker only erases the lines *it* rendered
    (``\\033[<n>A\\033[J``); it cannot erase output printed above its frame.
    A flow that shells out to a noisy subprocess (e.g. the Databricks
    ``+ Add`` path running ``databricks auth login`` + ``ucode configure``)
    therefore leaves that output on screen, and the next in-place menu
    redraw collides with it. Callers bracket such a takeover with this full
    clear so the picker re-renders on a clean buffer.

    No-op when stdout is not a TTY (pipes / CI / the numbered-fallback
    tests), so captured output stays free of escape sequences.

    :returns: None. Side effect: writes the clear sequence to stdout.
    """
    if not sys.stdout.isatty():
        return
    # 2J clears the visible screen, 3J clears the scrollback buffer, H homes
    # the cursor — together they wipe the leaked subprocess output.
    sys.stdout.write("\033[2J\033[3J\033[H")
    sys.stdout.flush()


def _truncate_cells(text: str, max_cells: int) -> str:
    """Truncate *text* to a terminal-cell budget, adding an ellipsis if needed."""
    if cell_len(text) <= max_cells:
        return text
    ellipsis = "…"
    budget = max(0, max_cells - cell_len(ellipsis))
    out: list[str] = []
    used = 0
    for ch in text:
        width = cell_len(ch)
        if used + width > budget:
            break
        out.append(ch)
        used += width
    return "".join(out) + ellipsis


def _render_menu(
    title: str,
    options: list[str],
    selected: int,
    *,
    descriptions: list[str] | None,
    width: int,
    selectable: list[bool],
    status: str | None = None,
    max_visible: int | None = None,
    window_start: int = 0,
    compact: bool = False,
) -> str:
    """Render the menu frame to an ANSI string for the termios redraw.

    A bold accent header, one row per option (selected → bold accent with the
    ``❯`` pointer, others normal weight aligned under it), an optional dim
    description line for the selected option (the generic analogue of the theme
    preview), and a muted footer of key hints.

    Non-selectable rows (``selectable[i]`` is ``False``) render as dim italic
    sub-lines indented beneath the preceding choice (e.g. a harness's
    ``default: …`` summary) with a blank line after — no ``❯`` pointer, and
    ↑/↓ skip them so the cursor only lands on real choices.

    :param title: The header shown above the options, e.g.
        ``"What kind of provider?"``.
    :param options: The row labels, e.g. ``["Claude", "  key", "Quit"]``.
    :param selected: Zero-based index of the highlighted (selectable) row.
    :param descriptions: Optional per-option description strings (same
        length as *options*); the selected option's description renders
        as a dim line under the menu. ``None`` to omit.
    :param width: Terminal width for rendering, e.g. ``80``.
    :param selectable: Parallel to *options*; ``False`` marks a row as a
        non-selectable section header/separator.
    :param status: Optional transient status line (e.g. ``"✓ added X"``)
        rendered green above the title. Being part of the frame, it is
        erased with the frame on ``clear_on_exit`` — so a re-rendering loop
        shows only the latest action's result, never an accumulating stack.
    :returns: An ANSI-styled string ready for ``stdout.write()``.
    """
    buf = io.StringIO()
    render_console = Console(file=buf, force_terminal=True, width=width, highlight=False)

    render_console.print()
    if status:
        render_console.print(Text(f"  {status}", style="bold green"))
        render_console.print()
    render_console.print(Text(f"  {title}", style=f"bold {ACCENT}"))
    if not compact:
        # The compact overview hugs the title to the list (Hermes-style); other
        # menus keep a blank line below the title for breathing room.
        render_console.print()

    # Optional scrolling viewport: when *max_visible* is set and the list is
    # longer, render only ``options[window_start : window_start + max_visible]``
    # (the caller keeps the selected row inside this window) plus dim "N more"
    # markers, so a long flat list fits one screen instead of overflowing and
    # flickering. ``None`` (the default) renders every row, unchanged.
    n_options = len(options)
    if max_visible is not None and n_options > max_visible:
        win_start = max(0, min(window_start, n_options - max_visible))
        win_end = win_start + max_visible
    else:
        win_start, win_end = 0, n_options
    if win_start > 0:
        render_console.print(Text.from_markup(f"       [{MUTED}]↑ {win_start} more[/]"))

    last_choice = -1  # index of the most recent selectable (group-owning) row
    for i in range(win_start, win_end):
        label = options[i]
        if not selectable[i]:
            # Sub-line(s) under the preceding choice (a harness's default +
            # "+N more" summary): indented, no pointer. ↑/↓ skip them. Their
            # styling follows the cursor — the label's own emphasis (e.g. a
            # bold-green default) shows only under the SELECTED choice; under an
            # unselected choice the same sub-line is muted to dim so the
            # highlight tracks where you are.
            if last_choice == selected:
                render_console.print(Text.from_markup(f"        {label}"))
            else:
                plain = Text.from_markup(label).plain  # drop the label's own color
                render_console.print(Text(f"        {plain}", style="dim"))
            # One blank line after the LAST sub-line of a group (next row is a
            # real choice, or the menu ends) — so consecutive sub-lines stay
            # together and the gap separates one choice's block from the next.
            if i + 1 >= len(options) or selectable[i + 1]:
                render_console.print()
        elif i == selected:
            # Highlighted choice: bold accent with the ❯ pointer.
            last_choice = i
            render_console.print(Text.from_markup(f"    [bold {ACCENT}]❯  {label}[/]"))
        else:
            # Unselected choice: normal weight (readable), aligned under the
            # pointer so the column doesn't shift as the cursor moves.
            last_choice = i
            render_console.print(Text.from_markup(f"       {label}"))

    if win_end < n_options:
        render_console.print(Text.from_markup(f"       [{MUTED}]↓ {n_options - win_end} more[/]"))

    if descriptions is not None and descriptions[selected]:
        render_console.print()
        description = descriptions[selected]
        if compact:
            description = _truncate_cells(description, max(8, width - 4))
        render_console.print(Text(f"    {description}", style="dim italic"))

    render_console.print()
    # The compact overview is a top-level menu (Esc exits setup), so it shows a
    # navigate/select/exit hint in the spirit of other modern CLIs; nested menus
    # keep the "Esc back" wording, where Esc returns rather than exits.
    hint = (
        "↑/↓ nav  ·  Enter select  ·  Esc exit"
        if compact
        else "↑/↓ move  ·  Enter select  ·  Esc back"
    )
    render_console.print(Text.from_markup(f"  [{MUTED}]{hint}[/]"))

    return buf.getvalue()


def _normalize_selectable(options: list[str], selectable: list[bool] | None) -> list[bool]:
    """Validate/expand the *selectable* mask for a menu.

    :param options: The row labels.
    :param selectable: Parallel ``bool`` mask, or ``None`` to mean every
        row is selectable (today's behavior).
    :returns: A concrete mask, one ``bool`` per option.
    :raises ValueError: If *selectable* length differs from *options*, or
        no row is selectable.
    """
    if selectable is None:
        return [True] * len(options)
    if len(selectable) != len(options):
        raise ValueError("selectable must match options length")
    if not any(selectable):
        raise ValueError("select() requires at least one selectable row")
    return list(selectable)


def _step_selectable(selectable: list[bool], start: int, step: int) -> int:
    """Return the next selectable index from *start* moving by *step*, wrapping.

    Skips non-selectable (header/separator) rows so ↑/↓ glide over them.

    :param selectable: The per-row selectable mask (at least one ``True``).
    :param start: The current index.
    :param step: ``-1`` for up, ``+1`` for down.
    :returns: The next selectable index (``start`` if it is the only one).
    """
    count = len(selectable)
    i = start
    for _ in range(count):
        i = (i + step) % count
        if selectable[i]:
            return i
    return start


def _first_selectable(selectable: list[bool], preferred: int) -> int:
    """Return *preferred* if selectable, else the first selectable index.

    :param selectable: The per-row selectable mask (at least one ``True``).
    :param preferred: The caller's requested default index.
    :returns: A selectable index to start the cursor on.
    """
    if 0 <= preferred < len(selectable) and selectable[preferred]:
        return preferred
    return next(i for i, ok in enumerate(selectable) if ok)


def _select_fallback(
    title: str,
    options: list[str],
    *,
    default: int,
    selectable: list[bool],
) -> int:
    """Non-TTY numbered fallback for :func:`select`.

    Gives pipes / CI / tests a numbered prompt instead of a raw-termios
    loop they cannot drive. Non-selectable rows are printed as plain
    section labels (no number); only selectable rows are numbered, and the
    returned value is the original index into *options*.

    :param title: The header shown above the numbered list, e.g.
        ``"Which provider?"``.
    :param options: The row labels (may include header/separator rows).
    :param default: Zero-based index (into *options*) pre-selected; the
        prompt default is its position among the numbered rows.
    :param selectable: Parallel mask; ``False`` rows are unnumbered labels.
    :returns: The chosen zero-based index into *options*.
    """
    console.print(f"  [{ACCENT}]{title}[/]")
    # Map each printed number → original options index; print headers as
    # plain labels so the grouping is still visible in the fallback.
    number_to_index: list[int] = []
    for i, label in enumerate(options):
        if selectable[i]:
            number_to_index.append(i)
            console.print(f"  {len(number_to_index)}. {label}")
        else:
            console.print(f"  {label}")
    console.print(f"  [{MUTED}](q to go back)[/{MUTED}]")
    console.print()
    default_number = number_to_index.index(default) + 1 if default in number_to_index else 1
    while True:
        raw = str(click.prompt("Choice", default=str(default_number)))
        # "q" is the fallback's abort, mirroring Esc on the TTY path — it
        # returns -1 so callers go back / cancel without a dedicated menu row.
        if raw.strip().lower() == "q":
            return -1
        try:
            number = int(raw)
            if 1 <= number <= len(number_to_index):
                return number_to_index[number - 1]
        except ValueError:
            pass
        console.print("  [red]Invalid selection.[/red]")


def _term_width() -> int:
    """Return the terminal width clamped to a sane minimum.

    :returns: Terminal columns, at least ``40``; ``80`` when the size
        cannot be determined.
    """
    try:
        return max(40, os.get_terminal_size().columns)
    except (OSError, ValueError):
        return 80


def select(
    title: str,
    options: list[str],
    *,
    descriptions: list[str] | None = None,
    default: int = 0,
    selectable: list[bool] | None = None,
    clear_on_exit: bool = False,
    status: str | None = None,
    max_visible: int | None = None,
    compact: bool = False,
) -> int:
    """Show a theme-picker-styled arrow-key menu and return the choice.

    On a TTY this draws the menu via raw termios (accent ``❯`` pointer,
    dimmed others, footer hints) and redraws in place on ↑/↓. Enter
    confirms the highlighted option. Esc (or Ctrl-C / Ctrl-D) **aborts**
    and returns ``-1`` so the caller can cancel / go back; callers must
    check for ``< 0`` before indexing ``options``.

    On a non-TTY (pipe / CI / test), falls back to a numbered prompt
    (:func:`_select_fallback`).

    Pass *selectable* to render a grouped tree in one view: rows whose
    mask is ``False`` are non-selectable section headers/separators that
    ↑/↓ skip over (e.g. ``"Claude"`` / ``"Codex"`` labels above their
    provider rows). With no mask, every row is selectable (today's flat
    menu).

    :param title: The header shown above the options, e.g.
        ``"What do you want to do?"``.
    :param options: The row labels, e.g. ``["Add a provider",
        "Quit"]``. Must be non-empty.
    :param descriptions: Optional per-option descriptions (same length
        as *options*); the selected option's description renders as a
        dim line under the menu. ``None`` to omit.
    :param default: Zero-based index of the initially highlighted
        option, e.g. ``0``. If it points at a non-selectable row, the
        cursor starts on the first selectable row instead.
    :param selectable: Optional parallel ``bool`` mask; ``False`` marks a
        row as a non-selectable header/separator. ``None`` → all rows
        selectable.
    :param clear_on_exit: When ``True`` (TTY only), erase the rendered menu
        frame on return instead of leaving it in the scrollback — so a
        multi-step interactive loop (re-rendering the menu after each
        action) doesn't pile up stale frames. No-op on the numbered
        fallback (nothing to erase).
    :param status: Optional transient status line shown green above the
        title (part of the frame, so it clears with ``clear_on_exit``).
        Pass the prior action's result so a re-rendering loop shows only
        the latest, never an accumulating stack. No-op on the fallback.
    :param max_visible: Optional cap on visible rows. When set and the list
        is longer, the menu shows a scrolling viewport that follows the
        cursor (with "N more" markers) so a long flat list fits one screen
        instead of overflowing and flickering. ``None`` renders every row.
        No-op on the numbered fallback.
    :param compact: When ``True`` (TTY only), render the dense top-level
        overview layout: the title hugs the list (no blank line below it) and
        the footer reads ``nav · select · Esc exit`` (Esc exits rather
        than goes back). Intended for the setup harness overview. No-op on the
        numbered fallback.
    :returns: The chosen zero-based index into *options* (always a
        selectable row), or ``-1`` when the user aborts — Esc / Ctrl-C /
        Ctrl-D on the TTY, or ``q`` on the numbered fallback.
    :raises ValueError: If *options* is empty, *descriptions* or
        *selectable* length differs from *options*, or no row is
        selectable.
    """
    if not options:
        raise ValueError("select() requires at least one option")
    if descriptions is not None and len(descriptions) != len(options):
        raise ValueError("descriptions must match options length")
    mask = _normalize_selectable(options, selectable)

    if not sys.stdin.isatty():
        return _select_fallback(title, options, default=default, selectable=mask)

    import termios
    import tty

    fd = sys.stdin.fileno()
    selected = _first_selectable(mask, default)
    cancelled = False
    width = _term_width()
    # Single-element list tracks how many lines the previous frame
    # occupied so the next redraw can move up and overwrite it
    # (the ``_theme_picker`` redraw idiom).
    prev_lines = [0]
    # Scrolling-viewport start index (mutable for the redraw closure); only
    # used when ``max_visible`` bounds a long list.
    window_start = [0]

    def _redraw() -> None:
        """Clear the prior frame region and reprint the menu in place."""
        if max_visible is not None and len(options) > max_visible:
            # Keep the selected row inside the [start, start+max_visible) window,
            # scrolling the window just enough to follow the cursor.
            if selected < window_start[0]:
                window_start[0] = selected
            elif selected >= window_start[0] + max_visible:
                window_start[0] = selected - max_visible + 1
            window_start[0] = max(0, min(window_start[0], len(options) - max_visible))
        rendered = _render_menu(
            title,
            options,
            selected,
            descriptions=descriptions,
            width=width,
            selectable=mask,
            status=status,
            max_visible=max_visible,
            window_start=window_start[0],
            compact=compact,
        )
        if prev_lines[0] > 0:
            sys.stdout.write(f"\033[{prev_lines[0]}A")
        sys.stdout.write("\033[J")  # Clear from cursor to end of screen.
        sys.stdout.write(rendered)
        sys.stdout.flush()
        prev_lines[0] = rendered.count("\n")

    try:
        old_attrs = termios.tcgetattr(fd)
    except termios.error:
        # Cannot enter raw mode — degrade to the numbered fallback.
        return _select_fallback(title, options, default=default, selectable=mask)

    try:
        _redraw()
        tty.setcbreak(fd)
        while True:
            ch = os.read(fd, 1)
            if not ch:
                break
            if ch in (b"\x03", b"\x04"):
                # Ctrl-C / Ctrl-D — abort the menu.
                cancelled = True
                break
            if ch == b"\x1b":
                # Escape alone, or the start of an arrow sequence.
                import select as _select

                if _select.select([fd], [], [], 0.05)[0]:
                    nxt = os.read(fd, 1)
                    if nxt == b"[":
                        arrow = os.read(fd, 1)
                        if arrow == b"A":  # Up
                            selected = _step_selectable(mask, selected, -1)
                            _redraw()
                        elif arrow == b"B":  # Down
                            selected = _step_selectable(mask, selected, +1)
                            _redraw()
                    # Ignore other escape sequences.
                    continue
                # Bare Escape — abort the menu.
                cancelled = True
                break
            if ch in (b"\r", b"\n"):
                break
            if ch in (b"k", b"K"):  # vi-style up
                selected = _step_selectable(mask, selected, -1)
                _redraw()
            elif ch in (b"j", b"J"):  # vi-style down
                selected = _step_selectable(mask, selected, +1)
                _redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    if clear_on_exit and prev_lines[0] > 0:
        # Erase the rendered frame so a re-rendering loop doesn't leave a
        # trail of stale menus in the scrollback: move up over the frame and
        # clear from there to the end of screen.
        sys.stdout.write(f"\033[{prev_lines[0]}A\033[J")
        sys.stdout.flush()

    return -1 if cancelled else selected


def prompt_text(
    label: str,
    *,
    default: str | None = None,
    hide_input: bool = False,
) -> str:
    """Prompt for a free-text value in the onboarding selector style.

    On a TTY this prints an accent label line, then reads the value via
    ``click.prompt`` (which handles ``hide_input`` masking and the
    ``default`` echo). Hidden prompts also print muted metadata-only
    feedback so users can tell a pasted secret registered without seeing
    the secret. On a non-TTY the same ``click.prompt`` path runs, so pipes
    / tests behave identically.

    :param label: The prompt label, e.g. ``"anthropic API key"``.
    :param default: Pre-filled default returned on an empty enter, or
        ``None`` to require a value.
    :param hide_input: When ``True``, mask the typed value (for secrets
        like API keys).
    :returns: The entered (or default) string.
    """
    if sys.stdin.isatty():
        console.print(f"  [{ACCENT}]{label}[/]")
        prompt_label = "  ❯"
    else:
        prompt_label = label

    if not hide_input:
        return str(
            click.prompt(
                prompt_label,
                default=default,
                hide_input=False,
                show_default=default is not None,
            )
        )

    console.print(f"  [{MUTED}](input hidden; paste your key, then press Enter)[/{MUTED}]")
    received_hidden_input = False
    termui = cast(_TermUIWithHiddenPrompt, click.termui)
    hidden_prompt_func = termui.hidden_prompt_func

    def _record_hidden_input(text: str) -> str:
        nonlocal received_hidden_input
        value = hidden_prompt_func(text)
        if value:
            received_hidden_input = True
        return value

    termui.hidden_prompt_func = _record_hidden_input
    try:
        value = str(
            click.prompt(
                prompt_label,
                default=default,
                hide_input=True,
                show_default=False,
            )
        )
    finally:
        termui.hidden_prompt_func = hidden_prompt_func

    if received_hidden_input and value:
        count = len(value)
        unit = "character" if count == 1 else "characters"
        console.print(f"  [{MUTED}]✓ received ({count} {unit})[/{MUTED}]")

    return value
