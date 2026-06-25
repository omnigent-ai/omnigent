# handlers whose signature is ``def _xxx(event: KeyPressEvent) -> None``.
# prompt-toolkit dispatches every handler with the same ``event`` positional;
# individual handlers typically ignore it. Per-handler noqas would be
# boilerplate on ~20 functions — disabling ARG001 file-wide is the right
# granularity because the genuine "dead arg" risk this rule catches does not
# apply when the signature is externally mandated.
"""TerminalHost — manages terminal I/O with a pinned input bar.

Wraps prompt_toolkit. All output goes through ``output()`` which
handles Rich rendering through the stdout proxy. Background tasks
keep the prompt visible during streaming.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import pathlib
import shlex
import sys
import textwrap
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from omnigent_client import TERMINAL_TASK_STATUSES, child_session_busy
from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, is_searching
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer, VerticalAlign
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle
from rich.console import Console
from rich.console import RenderableType as _RichRenderable
from wcwidth import wcswidth

from ._formatter import FormattedItem, StreamingText, StreamLive, StreamReplace
from ._linkify import linkify_ansi
from ._theme import LIGHT_THEME, TerminalTheme, get_theme

_log = logging.getLogger(__name__)


class _HasToolbarText(Protocol):
    """Protocol for objects that expose a ``toolbar_text()`` method.

    Used to type :attr:`TerminalHost.pipeline_counters` without
    importing the concrete :class:`PipelineCounters` from the REPL
    layer (which would create a circular dependency).
    """

    def toolbar_text(self) -> str:
        """Return a compact toolbar readout string."""
        ...


# Arc/ring characters for context-usage indicator (matching the web UI's SVG ring).
# Eight steps from empty circle to full circle — same Unicode Geometric Shapes
# block characters used by the ContextUsageBar widget in tests/scripts/tui.py.
_RING_CHARS = ("○", "◔", "◔", "◑", "◑", "◕", "◕", "◕", "●")

# Image extensions recognized for inline display.
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})

# File extensions recognized for attachment.
_FILE_EXTENSIONS = (
    frozenset(
        {
            ".pdf",
            ".txt",
            ".csv",
            ".json",
            ".md",
            ".py",
            ".js",
            ".ts",
            ".html",
            ".css",
            ".xml",
            ".yaml",
            ".yml",
            ".toml",
            ".go",
            ".rs",
            ".java",
            ".c",
            ".cpp",
            ".h",
            ".rb",
            ".sh",
            ".sql",
        }
    )
    | _IMAGE_EXTENSIONS
)


@dataclass
class PendingAttachment:
    """A file queued for upload with the next message."""

    path: str
    is_image: bool


# Pastes at or above either limit collapse to a placeholder; the
# char threshold catches single-line monsters (base64, JWTs) the
# line threshold misses.
_PASTE_LINE_THRESHOLD: int = 4
_PASTE_CHAR_THRESHOLD: int = 250


@dataclass(frozen=True)
class _PastedBlock:
    """
    A bracketed-paste payload abstracted out of the visible prompt buffer.

    :param block_id: 1-indexed ordinal, resets to ``1`` per submit.
    :param placeholder: Marker shown in the prompt, e.g.
        ``"[Pasted text #1 +22 lines]"``.
    :param content: Pasted text with line endings normalized to ``\\n``.
    """

    block_id: int
    placeholder: str
    content: str


def _normalize_paste(text: str) -> str:
    """
    Collapse ``\\r\\n`` and bare ``\\r`` to ``\\n``.

    Mirrors prompt-toolkit's default so iTerm2's CRLF payloads count
    the same as LF terminals.

    :param text: Raw paste payload, e.g. ``"line1\\r\\nline2\\r\\n"``.
    :returns: ``text`` with ``\\r\\n`` / ``\\r`` collapsed to ``\\n``.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _should_abstract_paste(text: str) -> bool:
    """
    Whether a paste crosses either abstraction threshold.

    :param text: Already-normalized paste content.
    :returns: ``True`` if the paste should be replaced with a placeholder.
    """
    if len(text) >= _PASTE_CHAR_THRESHOLD:
        return True
    # ``count("\n") + 1`` so a 4-line paste (3 separators + final line)
    # trips a threshold of 4.
    return text.count("\n") + 1 >= _PASTE_LINE_THRESHOLD


def _format_paste_placeholder(block_id: int, text: str) -> str:
    """
    Build the placeholder marker for an abstracted paste.

    Multi-line pastes report ``+M lines``; single-line monsters report
    ``+M chars``.

    :param block_id: 1-indexed paste ordinal.
    :param text: Already-normalized paste content.
    :returns: Rendered placeholder, e.g. ``"[Pasted text #1 +22 lines]"``.
    """
    line_count = text.count("\n") + 1
    if line_count >= _PASTE_LINE_THRESHOLD:
        return f"[Pasted text #{block_id} +{line_count} lines]"
    return f"[Pasted text #{block_id} +{len(text)} chars]"


# Sentinel returned by the main prompt when a registered overlay
# trigger fires. ``host.run`` detects it and launches the overlay
# app instead of dispatching to the input handler.
_OVERLAY_REQUEST_SENTINEL: str = "\x00__omnigent_ui_sdk.overlay_trigger__\x00"

# Sentinel returned by the main prompt when the user picks a sub-agent
# from the inline ``↓`` menu (sentinel + chosen session id). ``host.run``
# decodes the id and invokes ``on_subagent_select`` between prompt
# iterations — the safe context for switching sessions + re-rendering.
_SUBAGENT_SELECT_SENTINEL: str = "\x00__omnigent_ui_sdk.subagent_select__\x00"

# Braille-dot spinner frames for the "thinking…" indicator and
# the bottom-toolbar state badge. Eight frames give a smooth
# rotation at the default 10 Hz tick; matches the frame set
# that omnigent' cli.py uses so the two REPLs look identical
# while a turn is in flight.
_SPINNER_FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
# Spinner tick interval while streaming — 100 ms is fast enough
# to feel animated (10 fps) and slow enough that the invalidate
# calls don't dominate the event loop.
_SPINNER_TICK_SECONDS: float = 0.1

# Debounce window for the "N agents running" badge / spinner only. A
# sub-agent that just reached a terminal task status keeps counting as
# "running" for this many seconds so a between-turns ``completed`` blip that
# then resumes can't flicker the badge off. This NO LONGER hides the node:
# finished sub-agents stay in the ``↓`` selector indefinitely (web parity —
# see :meth:`TerminalHost._subagent_visible`), they just drop out of the
# running count once settled.
_SUBAGENT_LINGER_SECONDS: float = 3.0
# Max rows shown at once in the inline ``↓`` sub-agents menu. Longer
# trees scroll within this window so the input bar never lifts more than
# this many lines up the terminal.
_SUBAGENT_MENU_MAX_ROWS: int = 5


@dataclass
class _SubagentNode:
    """One node in the live sub-agent tree shown on the main REPL interface.

    Keyed in :attr:`TerminalHost._subagents` by ``session_id``; the tree is
    reconstructed from ``parent_id`` at render time. Fields mirror the
    ``ChildSessionSummary`` payload the server emits on
    ``session.child_session.updated`` and the
    ``GET /v1/sessions/{id}/child_sessions`` rows.
    """

    session_id: str
    parent_id: str | None = None
    agent: str | None = None
    title: str | None = None
    # ``current_task_status``: launching / in_progress / completed / failed / …
    # Running-ness is tracked via ``done_at`` (terminal-vs-not), not a live
    # ``busy`` flag — see :meth:`TerminalHost.active_subagent_count`.
    status: str | None = None
    started_at: float = 0.0  # monotonic; stamped on first sighting
    pending_elicitations: int = 0
    done_at: float | None = None  # monotonic; stamped when terminal
    # Live "session loop is running" flag from the server snapshot. Mirrors
    # ``ChildSessionSummary.busy`` and feeds the web-parity ``Working`` label.
    busy: bool = False
    # Whether the child's latest run carries a durable failure detail. Mirrors
    # the presence of ``ChildSessionSummary.last_task_error``; truthy => the
    # web-parity ``Failed`` label (outranks a stale ``completed`` status).
    last_task_error: bool = False
    # Whether the child session is closed to new user input — derived from its
    # labels / title via :func:`omnigent.session_lifecycle.is_session_closed`.
    # Sticky (a closed session never reopens); gates interactive chat: a closed
    # child is view-only because a ``message`` to it returns 409 CONFLICT.
    closed: bool = False


# Window for the two-press Ctrl+C exit: first Ctrl+C with an
# empty input arms the exit hint; a second Ctrl+C within this
# many seconds actually exits. Longer than "immediate" so a
# fat-finger double-tap doesn't surprise-exit, shorter than the
# typical user-attention span so the hint doesn't linger through
# an unrelated later Ctrl+C. Matches IPython / node REPL.
_EXIT_CONFIRM_WINDOW: float = 2.0

# Rows rendered outside the prompt input buffer by TerminalHost's inline
# prompt layout.
_PROMPT_INPUT_RESERVED_UI_ROWS: int = 2

# Extra rows to keep visible above the expanded composer. This prevents a
# large prompt from consuming every non-UI row and leaves a small amount of
# output/scrollback "bleed" on screen.
_PROMPT_INPUT_BLEED_ROWS: int = 3


@dataclass(frozen=True)
class OverlayAction:
    """
    One per-target action keybinding inside an :class:`Overlay`.

    Used for hotkeys that operate on the currently-selected
    sidebar target — e.g. "press ``o`` to attach to this
    terminal's tmux session in a new window." The handler runs
    while the overlay stays open; whatever state it changes
    propagates through the next ``builder`` call (overlays
    rebuild on a refresh tick).

    Action keys are registered alongside the overlay's
    navigation / close keys; they fire only outside search
    mode (``/``-prefixed query input). Each registered key
    must be distinct from every other ``OverlayAction.key``,
    every ``Overlay.close_keys`` entry, and the overlay's
    ``trigger`` — collisions raise at ``add_overlay`` time.

    :param key: prompt-toolkit key string, e.g. ``"o"`` or
        ``"r"``. Same syntax accepted by
        :meth:`KeyBindings.add`.
    :param label: Short label rendered in the footer hint,
        e.g. ``"attach"`` or ``"attach (read-only)"``. Keep
        terse — the footer is one line.
    :param handler: Async callable invoked when the user
        presses *key* with a sidebar selection. Receives the
        currently-selected :class:`OverlayTarget`. The host
        does not interpret the return value; the handler is
        responsible for any user-facing feedback (writing to
        stderr, raising, etc.). Exceptions inside the handler
        are caught at the host so the overlay stays usable
        instead of crashing the REPL.
    """

    key: str
    label: str
    handler: Callable[[OverlayTarget], Awaitable[None]]


@dataclass
class OverlayTarget:
    """
    One selectable row in an :class:`Overlay` sidebar.

    Used to model multi-target overlays (e.g. a debug pane that
    switches between main conversation and spawned sub-agents).
    The overlay host pairs each target with a keyboard index and
    renders them as a sidebar column with an icon + label. The
    currently-selected target is passed back to
    :attr:`Overlay.builder` on every render so the content pane
    reflects the selection.

    :param key: Stable identifier, e.g. ``"main"`` or
        ``"conv_abc123"``. Opaque to the host — the builder uses
        it to decide which data to fetch.
    :param label: Short display label rendered in the sidebar,
        e.g. ``"main"`` or ``"coder:auth"``. Wrap-to-width is
        handled by the host.
    :param icon: Optional single glyph prefix for the sidebar
        row, e.g. ``"🤖"`` for the main target, ``"👾"`` for
        spawned agents. ``None`` renders no icon prefix at all.
    """

    key: str
    label: str
    icon: str | None = None


@dataclass
class Overlay:
    """
    A fullscreen fly-out pane that overlays the pinned prompt.

    Registered on the :class:`TerminalHost` via
    :meth:`TerminalHost.add_overlay` before :meth:`TerminalHost.run`
    is called. When the user presses *trigger*, the host suspends
    the input prompt and swaps in a dedicated prompt-toolkit
    :class:`Application` whose content is whatever ``builder()``
    returns. Pressing any of *close_keys* (or *trigger* again) exits
    the overlay and returns to the input prompt.

    The host does not know anything about the overlay's meaning —
    it just runs ``builder`` to get Rich content, renders it into
    a scrollable buffer, and handles the basic keybindings. Callers
    own the fetching + formatting of the content.

    Two display modes, selected by whether *targets_builder* is
    provided. This is NOT a "dual path / fallback" pattern (see
    the one-correct-path rule) — it's an explicit API choice:
    some overlays genuinely have a single payload (e.g. a help
    screen, a command reference) while others are multi-target
    (e.g. a debug overlay switching between main / sub-agents).
    The mode is selected once at construction time; there is no
    runtime toggle, no flag to disable the sidebar on a
    per-render basis.

    * **Single pane** (``targets_builder is None``): the overlay
      shows only ``builder()``'s result in a scrollable buffer.
    * **Sidebar + content** (``targets_builder`` is supplied):
      the overlay renders a left sidebar listing every target,
      with the selected one highlighted; Tab / Shift-Tab cycle
      the selection and ``builder`` is re-invoked with the
      chosen :class:`OverlayTarget` so the content pane updates.

    :param trigger: Key binding that opens the overlay, e.g.
        ``"c-o"``. Same string syntax prompt-toolkit accepts in
        :meth:`KeyBindings.add`.
    :param builder: Async callable invoked each time the overlay
        is shown (and each time Tab switches the selected
        target). Receives the currently-selected
        :class:`OverlayTarget`, or ``None`` when the overlay has
        no sidebar. Returns either a plain string or a Rich
        renderable. A fresh call on each open guarantees the
        content reflects current state without the host having
        to invalidate caches.

        For multi-line styled content, return
        :class:`rich.console.Group` of one
        :func:`rich.text.Text.from_markup` per line — NOT a
        single :class:`Text` built from a newline-joined string.
        The Group form keeps each line as a discrete renderable
        the host's content-pane line-splitter understands; the
        joined-string form has been reported to render literal
        markup tags as visible text under certain content-pane
        widths and terminal combinations (kasey_uhlenhuth bug
        report 2026-04-28). Either way, the Group form is the
        idiom every shipped overlay builder uses — match it so
        new builders don't have to rediscover the rule by
        reading the reference implementation. Idiomatic shape::

            from rich.console import Group
            from rich.text import Text

            async def my_builder(target):
                lines = [
                    f"[bold]Title[/bold]: {target.label}",
                    f"  [dim]status[/dim]: ok",
                ]
                return Group(*(Text.from_markup(line) for line in lines))

        See ``omnigent/repl/_repl.py::_build_debug_overview``
        for the full reference implementation.
    :param title: Optional header rendered at the top of the
        pane, e.g. ``"Conversation history"``. ``None``
        suppresses the header entirely (no title bar, no
        separator under it).
    :param close_keys: Key bindings that close the overlay. The
        trigger key itself is always also treated as a close, so
        the hotkey both opens and closes the overlay by default.
    :param close_hint: Override the auto-generated footer hint
        (e.g. ``"esc  q  c-o  close"``). When ``None`` (default),
        the host derives a hint from *close_keys* + *trigger*
        using consistent short-form abbreviations. Set this to
        a literal string when the auto-generated hint is too
        verbose, ambiguous, or doesn't fit the overlay's
        purpose.
    :param targets_builder: Optional async callable that returns
        the list of :class:`OverlayTarget` entries to show in
        the sidebar. Invoked once when the overlay opens. A
        return of ``[]`` or ``None`` suppresses the sidebar,
        falling back to single-pane mode.
    :param sidebar_width: Column width (in characters) for the
        sidebar when *targets_builder* is provided. Default 24
        — matches the omnigent debug panel and fits the
        ``"type:name"`` labels the sub-agent spawn tool
        produces.
    :param actions: Per-target keybindings that act on the
        currently-selected sidebar entry, e.g. an attach
        shortcut on a terminal target. Each entry is an
        :class:`OverlayAction` with a key + label + async
        handler. Empty tuple by default (no per-target
        actions). Only applies when *targets_builder* is also
        set; without a sidebar there's nothing to act on.
    """

    trigger: str
    builder: Callable[[OverlayTarget | None], Awaitable[_RichRenderable | str]]
    title: str | None = None
    close_keys: tuple[str, ...] = ("escape", "q")
    close_hint: str | None = None
    targets_builder: Callable[[], Awaitable[list[OverlayTarget]]] | None = None
    sidebar_width: int = 24
    actions: tuple[OverlayAction, ...] = ()


# Map prompt-toolkit key names to short forms used in footer hints.
# Adding entries here is how an operator gets a different
# rendering (e.g. ``"c-c"`` → ``"^C"``) — the rule for everything
# else is the prompt-toolkit name as authored, lower-cased.
_KEY_HINT_ABBREVIATIONS: dict[str, str] = {
    "escape": "esc",
    "enter": "↵",
    "tab": "tab",
}


def _compute_sidebar_scroll_offset(
    *,
    selected_index: int,
    current_offset: int,
    visible_height: int,
) -> int:
    """
    Compute the new sidebar scroll offset that keeps
    *selected_index* inside the visible window.

    Snap policy:

    - Selection above the viewport (``selected_index <
      current_offset``) → scroll up so the selection lands on
      the first visible row.
    - Selection past the viewport (``selected_index >=
      current_offset + visible_height``) → scroll down so the
      selection lands on the last visible row.
    - Selection already inside the window → no change
      (don't gratuitously re-anchor).

    Behavior under wrap-around (``selected_index`` wraps from
    ``N-1`` to ``0`` via ``(idx + 1) % len``): ``selected_index
    = 0 < current_offset`` triggers the first branch, snapping
    the offset back to 0 — exactly what the user expects after
    Tab from the last entry. Symmetric for ``s-tab`` from
    entry 0 to ``N-1``.

    :param selected_index: Index of the currently selected
        sidebar row (0-based, < total target count).
    :param current_offset: The viewport's current top row,
        e.g. ``0`` at first paint, ``5`` after scrolling down
        5 rows.
    :param visible_height: How many rows fit in the sidebar
        viewport, e.g. ``28`` for a 30-row terminal minus 2
        rows of overlay chrome. Must be ``>= 1``.
    :returns: The new offset value. Caller writes it back to
        whatever holder maintains the viewport state.
    """
    if selected_index < current_offset:
        return selected_index
    if selected_index >= current_offset + visible_height:
        return selected_index - visible_height + 1
    return current_offset


def _abbreviate_key(key: str) -> str:
    """
    Render a prompt-toolkit key name as the short form used in
    overlay footer hints.

    Keeps the rendering consistent — ``"escape"`` always shows as
    ``"esc"``, ``"c-i"`` stays ``"c-i"`` (already short),
    everything else lower-cases. Without this normalization the
    auto-generated footer mixes long names (``"escape"``), single
    letters (``"q"``), and prompt-toolkit shorthand (``"c-i"``)
    arbitrarily.

    :param key: A prompt-toolkit key name, e.g. ``"escape"``,
        ``"q"``, ``"c-o"``.
    :returns: The short form, e.g. ``"esc"``, ``"q"``, ``"c-o"``.
    """
    return _KEY_HINT_ABBREVIATIONS.get(key.lower(), key.lower())


def _build_close_hint(overlay: Overlay) -> str:
    """
    Build the close-hint string for an overlay's footer.

    When ``overlay.close_hint`` is set, the caller's literal
    string wins. Otherwise auto-generate from *close_keys* +
    *trigger* with consistent short-form abbreviations.

    The hint is rendered as ``"<key>/<key>/<trigger> close"``
    — keys joined with ``/`` and ``"close"`` appended as the
    affordance label.

    :param overlay: The :class:`Overlay` whose footer hint to
        render.
    :returns: The close hint, ready to substitute into the
        idle footer template.
    """
    if overlay.close_hint is not None:
        return overlay.close_hint
    keys = [_abbreviate_key(k) for k in overlay.close_keys]
    trigger = _abbreviate_key(overlay.trigger)
    # ``trigger`` already closes by convention (the host treats
    # it as a close-key alongside the explicit close_keys).
    # Include it in the hint so users learn that.
    key_part = "/".join([*keys, trigger])
    return f"{key_part} close"


def _extract_file_paths(text: str) -> list[PendingAttachment]:
    """Detect file paths in pasted text (drag-and-drop).

    Terminals like iTerm2 and Kitty convert drag-and-drop into
    pasted text with file paths (possibly shell-escaped).
    Only checks whitespace-separated tokens — no shell parsing
    that could concatenate long text with filenames.
    """
    attachments: list[PendingAttachment] = []
    for token in text.split():
        token = token.strip("'\"")
        if len(token) > 512:
            continue
        if not any(token.endswith(ext) for ext in _FILE_EXTENSIONS):
            continue
        try:
            p = pathlib.Path(os.path.expanduser(token)).resolve()
        except (OSError, ValueError):
            continue
        if not p.is_file():
            continue
        is_image = p.suffix.lower() in _IMAGE_EXTENSIONS
        attachments.append(PendingAttachment(path=str(p), is_image=is_image))

    return attachments


def _strip_file_paths(text: str, attachments: list[PendingAttachment]) -> str:
    """
    Remove detected file paths from the input text.

    After extracting attachments, the raw pasted paths (possibly
    shell-escaped) should not appear in the message sent to the LLM.
    Returns the remaining text, stripped.

    :param text: The raw input line.
    :param attachments: Detected attachments with resolved paths.
    :returns: The text with file path tokens removed.
    """
    resolved_paths = {a.path for a in attachments}
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    remaining = []
    for token in tokens:
        cleaned = token.strip("'\"")
        p = pathlib.Path(os.path.expanduser(cleaned)).resolve()
        if str(p) in resolved_paths:
            continue
        remaining.append(token)
    return " ".join(remaining).strip()


def _display_width(text: str) -> int:
    """Visible width of text in terminal columns (handles CJK, emoji)."""
    w = wcswidth(text)
    return w if w >= 0 else len(text)


def _prompt_input_visual_line_count(text: str, *, columns: int, marker: str) -> int:
    """
    Estimate how many terminal rows the prompt buffer needs.

    Prompt-toolkit's ``Document.line_count`` counts only hard
    newlines. The composer also soft-wraps long logical lines, so
    capping the window to ``line_count`` forces long prompts into a
    one-row horizontally-scrolled viewport. This helper counts both
    hard lines and terminal-width wraps.

    :param text: Prompt buffer text, e.g. ``"summarize this long ..."``.
    :param columns: Current terminal width in columns, e.g. ``120``.
    :param marker: Prompt marker shown before the first line,
        e.g. ``"❯"``.
    :returns: Required visual row count, clamped to at least ``1``.
    """
    prefix_width = _display_width(f" {marker} ")
    input_width = max(1, columns - prefix_width)
    visual_lines = 0
    for line in text.split("\n"):
        width = _display_width(line)
        visual_lines += max(1, (width + input_width - 1) // input_width)
    return max(1, visual_lines)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (ValueError, OSError):
        return 80


def _term_height() -> int:
    try:
        return os.get_terminal_size().lines
    except (ValueError, OSError):
        return 24


def _prompt_input_max_rows() -> int:
    """
    Maximum visible rows available to the prompt input buffer.

    Keep TerminalHost's non-composer UI rows reserved (currently the
    separator bar and status toolbar), plus a small bleed margin above the
    composer. This lets the composer expand with the usable screen height
    instead of stopping at a fixed legacy cap, without consuming the whole
    screen.
    """
    return max(1, _term_height() - _PROMPT_INPUT_RESERVED_UI_ROWS - _PROMPT_INPUT_BLEED_ROWS)


# Rows the host doesn't get to use for scrollable output: prompt-toolkit's
# pinned input area, bottom toolbar, and safety margin for tight PTYs. The
# cursor-up + erase replace path needs the streamed line count to fit
# UNDER this ceiling — anything beyond can't be reached because the
# scrolled-off rows live in the terminal's scrollback buffer, not the
# active viewport. Keep the reserve conservative: long responses still
# render in full via StreamReplace, while printing too many live rows can
# leave duplicate raw text in scrollback.
_BOTTOM_RESERVED_ROWS: int = 11


# Idempotency guard for the CSI-u escape-sequence registrations
# below. The map mutates a prompt-toolkit module-level dict; doing
# the assignments more than once across multiple
# :class:`TerminalHost` constructions in the same process is
# harmless (same key → same value) but it's wasteful and
# pollutes import side-effects. The guard short-circuits after
# the first install.
_CSI_U_INSTALLED: bool = False


def _install_csi_u_sequences() -> None:
    """
    Register Kitty Keyboard Protocol (CSI-u) escape sequences
    with prompt-toolkit's vt100 parser.

    The Kitty Keyboard Protocol — supported by Kitty, WezTerm,
    Ghostty, recent xterm, and iTerm2 with "Report modifiers
    using CSI u" enabled — encodes keystrokes like Ctrl+C as
    ``\\x1b[3;5u`` instead of the legacy ``\\x03``. Without
    this registration, a user on one of those terminals
    presses Ctrl+C in the REPL and prompt-toolkit silently
    drops the keystroke (no binding matches the unknown
    sequence).

    Ported from the legacy non-AP mode CLI's
    ``omnigent/inner/cli.py:1186-1239`` so behavior is
    consistent across paths. Three groups:

    1. Control characters (``Ctrl+C``, ``Ctrl+D``, etc.) by
       codepoint.
    2. ``Ctrl+<letter>`` by ASCII code of the letter.
    3. Special keys (Escape, Backspace, Delete, ``Ctrl+M``,
       Shift+Enter → ``F20``, focus-in/out markers).

    Idempotent — guarded by :data:`_CSI_U_INSTALLED` so
    repeated :class:`TerminalHost` construction in the same
    process doesn't re-mutate the parser dict.

    Failure modes degrade silently: if prompt-toolkit's
    private ``ANSI_SEQUENCES`` import path moves in a future
    release, the install no-ops rather than crashing the host.
    The user just doesn't get the protocol awareness — same
    state as before this code existed.
    """
    global _CSI_U_INSTALLED
    if _CSI_U_INSTALLED:
        return
    try:
        # ``ANSI_SEQUENCES`` is a module-level mapping
        # prompt-toolkit doesn't re-export via ``__all__``; it's
        # the documented extension point for registering custom
        # escape sequences and has been stable for years.
        from prompt_toolkit.input.vt100_parser import (  # type: ignore[attr-defined]
            ANSI_SEQUENCES,
        )
        from prompt_toolkit.keys import Keys
    except ImportError:
        # Prompt-toolkit's API changed under us — degrade to
        # no-op so the host still constructs.
        _CSI_U_INSTALLED = True
        return

    # Shift+Enter → F20. Legacy maps F20 to "insert newline" so
    # power users get a multi-line input affordance on any
    # CSI-u terminal. The host's prompt session must bind
    # ``Keys.F20`` separately (see :meth:`_install_input_bindings`)
    # for the F20 key event to actually do something — without
    # the binding, the key is recognized but inert.
    ANSI_SEQUENCES["\x1b[13;2u"] = Keys.F20

    _ctrl_codepoints = {
        3: Keys.ControlC,
        4: Keys.ControlD,
        8: Keys.ControlH,
        9: Keys.ControlI,
        26: Keys.ControlZ,
    }
    for cp, key in _ctrl_codepoints.items():
        ANSI_SEQUENCES[f"\x1b[{cp};5u"] = key

    _ctrl_letter_keys = {
        "a": Keys.ControlA,
        "b": Keys.ControlB,
        "c": Keys.ControlC,
        "d": Keys.ControlD,
        "e": Keys.ControlE,
        "f": Keys.ControlF,
        "g": Keys.ControlG,
        "h": Keys.ControlH,
        "k": Keys.ControlK,
        "l": Keys.ControlL,
        "n": Keys.ControlN,
        "o": Keys.ControlO,
        "p": Keys.ControlP,
        "q": Keys.ControlQ,
        "r": Keys.ControlR,
        "s": Keys.ControlS,
        "t": Keys.ControlT,
        "u": Keys.ControlU,
        "w": Keys.ControlW,
        "y": Keys.ControlY,
    }
    for ch, key in _ctrl_letter_keys.items():
        ANSI_SEQUENCES[f"\x1b[{ord(ch)};5u"] = key

    # Other CSI-u sequences power users hit:
    ANSI_SEQUENCES["\x1b[27u"] = Keys.Escape
    ANSI_SEQUENCES["\x1b[127;5u"] = Keys.ControlH  # Ctrl+Backspace
    ANSI_SEQUENCES["\x1b[127;2u"] = Keys.Backspace  # Shift+Backspace
    ANSI_SEQUENCES["\x1b[3;2~"] = Keys.Delete  # Shift+Delete
    ANSI_SEQUENCES["\x1b[13u"] = Keys.ControlM  # plain Enter via CSI-u
    ANSI_SEQUENCES["\x1b[9u"] = Keys.ControlI  # plain Tab via CSI-u
    ANSI_SEQUENCES["\x1b[127u"] = Keys.Backspace  # plain Backspace via CSI-u

    # Xterm/iTerm2 focus-reporting sends ESC [ I when the terminal gains
    # focus and ESC [ O when it loses focus. If these are not registered,
    # prompt-toolkit sees the leading ESC as our Escape binding and then
    # inserts the printable tail ("[I" / "[O") into the prompt. Register
    # them as ignored keys so tabbing between iTerm windows/tabs is inert.
    ANSI_SEQUENCES["\x1b[I"] = Keys.Ignore  # focus in
    ANSI_SEQUENCES["\x1b[O"] = Keys.Ignore  # focus out

    _CSI_U_INSTALLED = True


class TerminalHost:
    """Terminal I/O host with a pinned input bar.

    Usage::

        async def on_input(text: str) -> None:
            ...  # process input, call host.output()

        host = TerminalHost(model_name="coder")
        async with host:
            host.output(fmt.welcome("coder"))
            await host.run(on_input)

    :param prompt_marker: Character shown before the cursor.
    :param accent_color: Color for prompt bars and marker.
    :param history_file: Path for persistent input history.
        Defaults to ``"~/.omnigent_history"`` to match the
        legacy ``omnigent run`` CLI's location
        (``omnigent/inner/cli.py:_cli_history_file_path``) so
        users who flip between legacy and Omnigent mode see the same
        ↑ / Ctrl+R recall in both. SDK consumers outside
        omnigent can override.
    :param model_name: Shown in the bottom toolbar.
    :param toolbar_hints: Right-side hint segment of the
        bottom toolbar — same shape ``welcome()`` accepts so
        callers can pass the identical list and keep the
        welcome panel and the toolbar in sync. ``None`` falls
        back to a baseline set (``"esc cancel"``,
        ``"ctrl+c exit"``).
    :param window_title: Optional terminal/tab window title set
        on enter (``__aenter__``) and cleared on exit. Mirrors
        the legacy CLI's ``OSC 0`` title set, which lets users
        distinguish multiple concurrent sessions in their
        terminal tab bar. ``None`` (default) leaves the title
        untouched. Best-effort: terminals that don't honor
        ``OSC 0`` (rare) silently ignore the escape; failures
        in :meth:`set_title` are swallowed so a host that can't
        set the title still runs.
    :param completer: Optional prompt-toolkit
        :class:`~prompt_toolkit.completion.Completer` wired into
        the input. When supplied, the popup is live (Tab / arrows
        to select, Enter to accept). ``None`` disables the popup.
        The host stays generic — callers decide what to complete.
    """

    def __init__(
        self,
        *,
        prompt_marker: str = "❯",
        accent_color: str = "#F43BA6",
        history_file: str = "~/.omnigent_history",
        model_name: str | None = None,
        toolbar_hints: list[str] | None = None,
        window_title: str | None = None,
        completer: Completer | None = None,
        theme: TerminalTheme | str = LIGHT_THEME,
    ) -> None:
        # Make Kitty Keyboard Protocol terminals (Kitty,
        # WezTerm, Ghostty, iTerm2 with CSI-u enabled,
        # recent xterm) work — without this, Ctrl+letter
        # keystrokes in those terminals are silently dropped
        # by prompt-toolkit's vt100 parser. Idempotent across
        # multiple host constructions in the same process.
        _install_csi_u_sequences()
        self._marker = prompt_marker
        self._accent = accent_color
        # Toolbar label. ``None`` = model not known yet — the
        # bottom-toolbar builder falls back to an empty segment
        # so the bar still paints. Changing this to ``""`` would
        # conflate "not set" with "user explicitly passed empty",
        # which is why the None-sentinel form is used.
        self._model: str | None = model_name
        # Toolbar hint list. Joined with `` · `` separators in
        # ``build_toolbar``. Callers should pass the same list
        # they hand to ``welcome(hints=...)`` so the bar's
        # hints match the welcome panel — drift here means
        # users see "/help help · Ctrl+O debug · ..." up top
        # but only "esc cancel · ctrl+c exit" at the bottom.
        # ``list(...)`` makes a shallow copy so external
        # mutation of the caller's list (or the module-level
        # ``WELCOME_HINTS`` constant in ``_repl``) cannot
        # silently change the bar's appearance after the host
        # is constructed.
        self._toolbar_hints: list[str] = (
            list(toolbar_hints) if toolbar_hints is not None else ["esc cancel", "ctrl+c exit"]
        )
        self._tasks: list[asyncio.Task[None]] = []
        self.theme = get_theme(theme) if isinstance(theme, str) else theme
        self._console = Console(highlight=False, theme=self.theme.rich_theme)
        self._stream_start: float | None = None
        # Live sub-agent tree shown on the main interface (state badge +
        # ``↓`` menu). Keyed by child session id; the tree is reconstructed
        # from each node's ``parent_id``. ``_subagent_root`` is the session
        # the tree is rooted at (the originally-launched "main" session).
        self._subagents: dict[str, _SubagentNode] = {}
        self._subagent_root: str | None = None
        # Bumped every time the tree is cleared (``/switch`` / ``/new`` /
        # ``/clear`` start a fresh root). A background ``seed_subagent_tree``
        # whose fetch was in flight across a clear carries the stale
        # generation and no-ops, so a slow poll can't resurrect cleared nodes
        # (or re-root the just-cleared tree onto the old session). See
        # :meth:`subagent_generation` / :meth:`clear_subagents`.
        self._subagent_generation: int = 0
        # Inline ``↓`` sub-agents menu state. Open is toggled by Down (on an
        # empty input while sub-agents are active) / Esc; index + scroll
        # drive the windowed list rendered below the toolbar (see the layout
        # patch). ``on_subagent_select`` is invoked by ``run`` with the chosen
        # session id when the user presses Enter.
        self._subagent_menu_open: bool = False
        self._subagent_menu_index: int = 0
        self._subagent_menu_scroll: int = 0
        self.on_subagent_select: Callable[[str], Awaitable[None]] | None = None
        # Returns the REPL's current active session id. Lets the host tell
        # when the user is "inside" a sub-agent (active != tree root) so
        # Left-arrow can jump back to the top-level session.
        self.active_session_id_getter: Callable[[], str | None] | None = None
        self._last_was_streaming: bool = False
        self._text_buffer: str = ""
        self._streamed_line_count: int = 0  # Lines printed from streaming text.
        self._live_line_count: int = 0  # Lines in the live (unstable tail) region.
        self.text_indent: str = "   "  # Indent for streaming text lines.
        self.on_help: Callable[[], None] | None = None  # Ctrl+H callback.
        # Ctrl+T toggle: callback invoked when the user presses Ctrl+T.
        # The REPL wires this to flip ``formatter.show_tool_output``.
        self.on_toggle_tool_output: Callable[[], None] | None = None
        self._pending_attachments: list[PendingAttachment] = []
        # Placeholders currently in the prompt buffer paired with
        # their original content. Drained on submit; cleared on cancel
        # so ``#N`` restarts at ``1`` per composition.
        self._pasted_blocks: list[_PastedBlock] = []
        # Two-press Ctrl+C exit state. First press with empty
        # input sets this to ``monotonic() + _EXIT_CONFIRM_WINDOW``;
        # a second Ctrl+C before the deadline exits, otherwise the
        # deadline is cleared and the hint drops out of the toolbar.
        # Stored as ``None`` when no confirmation is pending — the
        # ``None`` sentinel discriminates "no hint" from "hint
        # armed" unambiguously.
        self._exit_confirm_deadline: float | None = None
        # Set by handlers like /quit to exit the prompt loop cleanly.
        self._exit_requested: bool = False
        # Optional pipeline debug counters. Set by the REPL when
        # ``--debug-events`` is active; ``None`` (the default) means
        # the toolbar omits the counter segment entirely.
        self.pipeline_counters: _HasToolbarText | None = None
        # Context-window usage tracking for the toolbar ring indicator.
        # Both start as ``None``; the ring is hidden until the first
        # ``update_context_usage`` call. ``_tokens_used`` is the
        # input-token count from the most-recently-completed response;
        # ``_context_window`` is the model's total context window.
        self._tokens_used: int | None = None
        self._context_window: int | None = None

        # Overlays registered via :meth:`add_overlay`. Populated
        # before :meth:`run` is invoked; the host wires each
        # overlay's trigger into the prompt's keybindings below,
        # and indexes them by trigger so the main run loop can
        # resolve which overlay to show when the prompt exits
        # with the overlay sentinel.
        self._overlays_by_trigger: dict[str, Overlay] = {}

        style = self._make_style()

        # Retained on the instance so :meth:`add_overlay` can
        # register additional triggers after construction. prompt-
        # toolkit's ``PromptSession`` does not re-read the bindings
        # on each :meth:`prompt_async` call — it reuses the same
        # ``KeyBindings`` object — so mutating this set between
        # turns is safe and takes effect on the next prompt.
        self._style = style
        self._kb: KeyBindings = KeyBindings()

        @self._kb.add("escape")
        def _on_escape(event: object) -> None:
            # Esc first dismisses the inline ↓ sub-agents menu if it's open,
            # otherwise cancels the in-flight turn (its usual role).
            if self._subagent_menu_open:
                self._close_subagent_menu()
                return
            self.cancel()

        # Multi-line input bindings — multiline=True is set on
        # the prompt session below so the buffer accepts ``\n``,
        # but with multiline on, prompt-toolkit's default Enter
        # handler INSERTS a newline instead of submitting. We
        # invert that: plain Enter submits (the chat REPL's
        # one-shot common case) and three escape-hatch keys
        # insert a newline for power users who want multi-line
        # paste / composition:
        #
        # - ``escape enter`` works in every terminal (vim-style
        #   ``Esc`` then ``Enter``).
        # - ``c-j`` (Ctrl+J) works in every terminal — Ctrl+J
        #   IS line-feed at the byte level.
        # - ``f20`` is the Kitty Keyboard Protocol's encoding
        #   for Shift+Enter (see :func:`_install_csi_u_sequences`).
        #   Power users on Kitty / WezTerm / Ghostty / iTerm2-
        #   with-CSI-u get the muscle-memory Shift+Enter
        #   combo; on terminals without CSI-u, F20 just isn't
        #   reachable and the user falls back to one of the
        #   other two paths.
        @self._kb.add("enter", eager=True, filter=is_searching)
        def _accept_history_search(event: KeyPressEvent) -> None:
            # Ctrl+R moves focus to prompt-toolkit's search buffer.
            # Accept the matched history item and return focus to the
            # input buffer — don't submit the prompt.
            from prompt_toolkit.key_binding.bindings import (
                search as search_bindings,
            )

            search_bindings.accept_search.handler(event)

        @self._kb.add("enter", eager=True, filter=~is_searching)
        def _on_enter(event: KeyPressEvent) -> None:
            # When the inline ↓ menu is open, Enter picks the highlighted
            # sub-agent and exits with the select sentinel (``run`` switches
            # into it) instead of submitting the prompt.
            if self._subagent_menu_open:
                selected = self._selected_subagent_id()
                self._close_subagent_menu()
                if selected:
                    event.app.exit(result=_SUBAGENT_SELECT_SENTINEL + selected)
                return
            buf = event.current_buffer
            if buf.text.endswith("\\"):
                buf.text = buf.text[:-1]
                buf.cursor_position = len(buf.text)
                buf.insert_text("\n")
            else:
                buf.validate_and_handle()

        @self._kb.add("escape", "enter")
        @self._kb.add("c-j")
        @self._kb.add("f20")
        def _insert_newline(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        # Replace prompt-toolkit's default (insert full payload) with
        # the abstraction-aware path. Re-splice on submit happens in
        # ``_expand_pasted_blocks``.
        @self._kb.add(Keys.BracketedPaste)
        def _on_bracketed_paste(event: KeyPressEvent) -> None:
            to_insert = self._handle_paste_text(event.data)
            if to_insert:
                event.current_buffer.insert_text(to_insert)

        # ── Inline ↓ sub-agents menu navigation ──────────────────
        # Down on an empty input (while sub-agents are active) opens the
        # menu; while open, Up/Down move the selection. Enter (select) and
        # Esc (close) are handled in ``_on_enter`` / ``_on_escape`` above so
        # they take precedence over the submit / cancel bindings. The menu
        # is modal: it owns the arrow keys + Enter + Esc while open.
        _menu_open = Condition(lambda: self._subagent_menu_open)

        def _can_open_subagent_menu() -> bool:
            # Openable whenever ANY sub-agent node exists — active OR finished.
            # Finished children are retained in the selector (web parity) so the
            # user can revisit / chat with them, so the gesture stays available
            # after they settle, not only while something is running.
            if self._subagent_menu_open or not self.has_any_subagents():
                return False
            try:
                return get_app().current_buffer.text == ""
            except Exception:
                return False

        @self._kb.add("down", filter=Condition(_can_open_subagent_menu) & ~is_searching)
        def _open_menu(event: KeyPressEvent) -> None:
            self._open_subagent_menu()

        @self._kb.add("down", filter=_menu_open)
        def _menu_next(event: KeyPressEvent) -> None:
            self._move_subagent_selection(1)

        @self._kb.add("up", filter=_menu_open)
        def _menu_prev(event: KeyPressEvent) -> None:
            self._move_subagent_selection(-1)

        # ── Left-arrow: back to the top-level session ─────────────
        # While viewing a sub-agent, Left (on an empty input) jumps straight
        # back to the top-level "main" session — available any time you're
        # inside a sub-agent, independent of whether agents are still active.
        # Falls through to normal cursor movement when there's text to edit.
        def _can_go_back_to_main() -> bool:
            if self._subagent_menu_open or not self._is_inside_subagent():
                return False
            try:
                return get_app().current_buffer.text == ""
            except Exception:
                return False

        @self._kb.add("left", filter=Condition(_can_go_back_to_main) & ~is_searching)
        def _back_to_main(event: KeyPressEvent) -> None:
            root = self._subagent_root
            if root is not None:
                event.app.exit(result=_SUBAGENT_SELECT_SENTINEL + root)

        # Two-press Ctrl+C with clear-input semantics:
        #
        # - Input buffer has text → clear the buffer (reset the
        #   input prompt-toolkit field to empty) and disarm any
        #   pending exit confirmation. This matches IPython /
        #   node REPL / Claude Code behavior where the first
        #   Ctrl+C is "nevermind, start the line over".
        # - Input is empty AND no exit hint pending → arm the
        #   hint: set the deadline, invalidate the app so the
        #   toolbar rerenders with "Press Ctrl+C again to exit".
        # - Input is empty AND hint still in its window → raise
        #   :class:`KeyboardInterrupt` so the outer run loop's
        #   ``except (EOFError, KeyboardInterrupt): break``
        #   catches it and terminates the REPL.
        #
        # Without this binding, prompt-toolkit's default Ctrl+C
        # behavior is to raise :class:`KeyboardInterrupt` on
        # every press, which exits immediately with no buffer-
        # clear affordance.
        @self._kb.add("c-c")
        def _on_ctrl_c(event: KeyPressEvent) -> None:
            import time as _time

            buf = event.app.current_buffer
            if buf.text:
                # Clear the input field; cancel any pending exit hint.
                # Drop paste registrations too so ``#N`` restarts at 1.
                buf.reset()
                self._pasted_blocks = []
                self._exit_confirm_deadline = None
                return
            now = _time.monotonic()
            deadline = self._exit_confirm_deadline
            if deadline is not None and now < deadline:
                # Second press within the window — exit. Use
                # ``app.exit(exception=...)`` rather than a raw
                # ``raise``: a ``raise`` from inside a prompt-
                # toolkit key handler surfaces at the top-level
                # async task without going through prompt_async's
                # normal exit path, which bypasses the
                # ``erase_when_done`` teardown and leaves the
                # terminal's alt-screen / cursor state garbled.
                # ``exit(exception=KeyboardInterrupt())`` makes
                # ``prompt_async`` raise the exception cleanly,
                # which our outer run loop catches.
                event.app.exit(exception=KeyboardInterrupt())
                return
            # Empty input, no pending hint (or expired) — arm one.
            self._exit_confirm_deadline = now + _EXIT_CONFIRM_WINDOW
            event.app.invalidate()

        # Ctrl+T: toggle tool output visibility.
        @self._kb.add("c-t")
        def _on_ctrl_t(event: KeyPressEvent) -> None:
            cb = self.on_toggle_tool_output
            if cb is not None:
                cb()
                event.app.invalidate()

        # No keyboard binding for ``on_help`` — users invoke it
        # via the ``/help`` slash command instead. We tried F1
        # and Ctrl+H and both failed:
        #
        # - F1 is intercepted by some terminal emulators
        #   (iTerm2 window menus, Warp pane cycling, tmux prefix
        #   passthrough) before it reaches the running program.
        # - Ctrl+H shares its byte (0x08) with Backspace on
        #   essentially every modern terminal, and prompt-toolkit
        #   cannot reliably discriminate them at the key-parser
        #   layer — binding ``c-h`` causes ``on_help`` to fire on
        #   every Backspace, spamming the help text instead of
        #   deleting a character.
        #
        # The ``on_help`` attribute is still exposed so callers
        # can wire a slash-command handler to it and keep the
        # single render path; nothing on the SDK side triggers
        # it automatically.

        # Use prompt-toolkit's default CPR (Cursor Position
        # Report) probing. The earlier override that disabled
        # CPR (to silence a warning in tmux / SSH /
        # multiplexer setups that drop the response) had a
        # bigger cost: prompt-toolkit gates the bottom_toolbar
        # on ``renderer_height_is_known``
        # (filters/app.py:189), which becomes True only after
        # a CPR round-trip on Vt100 terminals. Disabling CPR
        # therefore makes the toolbar render condition False
        # forever and the bar never appears in real terminals.
        # The warning is annoying in CPR-less setups but rare
        # in practice; losing the toolbar is a worse UX
        # everywhere.
        _output = create_output()
        # Retained on the instance so ``__aenter__`` /
        # ``__aexit__`` can drive ``set_title`` / ``clear_title``
        # without going through the prompt-session. The same
        # output object is also handed to the ``PromptSession``
        # below — there's only one ``Output`` per host.
        self._output = _output
        self._window_title = window_title

        # ``completer=None`` and ``complete_while_typing=True`` are
        # both prompt-toolkit defaults: no completer → no popup; if
        # a caller supplies one, the popup is live as they type.
        self._prompt = PromptSession(
            history=FileHistory(os.path.expanduser(history_file)),
            style=self._style,
            erase_when_done=True,
            key_bindings=self._kb,
            output=_output,
            completer=completer,
            reserve_space_for_menu=0,
        )

        # ----------------------------------------------------------
        # Layout patches.
        #
        # prompt-toolkit's default HSplit uses JUSTIFY alignment,
        # which distributes remaining terminal height into growable
        # children — the buffer window absorbs it all, pushing the
        # bottom_toolbar to the terminal bottom.
        #
        # Fix:
        #   1. Switch the root HSplit to TOP alignment.  This adds
        #      an invisible padding Window at the bottom that absorbs
        #      remaining space, so real children stay compact.
        #   2. Cap the buffer Window's height to the actual visual
        #      input height so soft-wrapped prompts can expand, but
        #      the composer still stays compact for very long input.
        #   3. Replace the pinned bottom_toolbar with regular Windows
        #      (separator bar + status line) that flow right after
        #      the input.
        # ----------------------------------------------------------
        _ps = self._prompt
        _root = _ps.layout.container
        if isinstance(_root, HSplit):
            # (1) TOP alignment — remaining space goes to padding,
            #     not to the buffer.
            _root.align = VerticalAlign.TOP

            # (2) Find and cap the buffer Window height.
            #     Use get_children() — the standard Container API —
            #     so we traverse through Frame, ConditionalContainer,
            #     FloatContainer etc. reliably.
            def _find_buffer_window(container: object) -> Window | None:
                if isinstance(container, Window):
                    if isinstance(getattr(container, "content", None), BufferControl):
                        return container
                    return None
                for child in container.get_children():  # type: ignore[union-attr]
                    hit = _find_buffer_window(child)
                    if hit:
                        return hit
                return None

            _buf_win = _find_buffer_window(_root)
            if _buf_win is not None:
                _COMPLETION_MENU_ROWS = 8

                def _dynamic_height() -> Dimension:
                    n = _prompt_input_visual_line_count(
                        _ps.default_buffer.text,
                        columns=_term_width(),
                        marker=self._marker,
                    )
                    n = min(_prompt_input_max_rows(), n)
                    # When the completion popup is visible, request
                    # extra rows so the renderer claims enough
                    # terminal space for the menu to render below
                    # the input.  Without this the Float has no room
                    # and the popup is squeezed to 1-2 lines.
                    menu = _COMPLETION_MENU_ROWS if _ps.default_buffer.complete_state else 0
                    return Dimension(
                        min=max(1, n + menu), max=max(1, n + menu), preferred=n + menu
                    )

                _buf_win.height = _dynamic_height  # type: ignore[assignment]

            # (3) Replace the bottom_toolbar (last child) with
            #     inline separator + status windows.
            children = list(_root.children)
            if children:
                children.pop()  # remove ConditionalContainer(bottom_toolbar)
            _sep = Window(
                FormattedTextControl(lambda: FormattedText([("class:bar", "─" * _term_width())])),
                height=1,
                dont_extend_height=True,
            )
            _status = Window(
                FormattedTextControl(lambda: self.build_toolbar()),
                style="class:bottom-toolbar",
                height=1,
                dont_extend_height=True,
            )
            # Inline ↓ sub-agents menu, BELOW the toolbar so the navigable
            # list sits beneath the whole chat interface. Shown only while
            # the menu is open with rows; sized to its content (≤ row cap +
            # hint) so the input bar never lifts more than a few lines.
            _subagent_menu = ConditionalContainer(
                Window(
                    FormattedTextControl(lambda: self.build_subagent_menu()),
                    height=lambda: Dimension.exact(self._subagent_menu_line_count()),
                    dont_extend_height=True,
                ),
                filter=Condition(
                    lambda: self._subagent_menu_open and bool(self._subagent_menu_display_rows())
                ),
            )
            children.append(_sep)
            children.append(_status)
            children.append(_subagent_menu)
            _root.children = children

    def _make_style(self) -> PTStyle:
        """Build prompt-toolkit styles from the active terminal theme."""
        return PTStyle.from_dict(
            {
                "bar": self._accent,
                "prompt-marker": f"{self._accent} bold",
                "model-name": self.theme.toolbar_model,
                # Settled (done / warm-idle) sub-agent rows in the ↓ selector
                # are de-emphasized vs live ones — mirroring the web Agents
                # rail's SETTLED_STATE dimming — but kept in the list.
                "subagent-settled": "fg:ansibrightblack",
                "bottom-toolbar.key": self._accent,
                # Bottom toolbar background. ``noreverse`` prevents
                # prompt-toolkit's default fg/bg swap (which would turn
                # the magenta ``────`` accent fragments into magenta
                # BACKGROUND blocks). When the theme specifies a
                # background color, paint a subtle full-width strip;
                # otherwise just cancel the reverse and let the
                # terminal's native background show through.
                "bottom-toolbar": f"noreverse bg:{self.theme.toolbar_background}"
                if self.theme.toolbar_background
                else "noreverse",
            }
        )

    @property
    def tokens_used(self) -> int | None:
        """
        Provider-reported token count from the most recently completed
        response, or ``None`` before the first response completes.

        Set by :meth:`update_context_usage`. Use this in the ``/context``
        slash command to show the same value as the toolbar ring.

        :returns: Token count, e.g. ``8363``, or ``None``.
        """
        return self._tokens_used

    def update_context_usage(self, tokens_used: int, context_window: int) -> None:
        """
        Record current context-window usage for the toolbar ring indicator.

        Called by the REPL after each completed response with the
        server-counted input-token total. Triggers a toolbar repaint so
        the ring character and percentage reflect the latest turn.

        :param tokens_used: Input-token count from the most-recently-
            completed response, e.g. ``3100``.
        :param context_window: Model context window size in tokens,
            e.g. ``200000``.
        """
        self._tokens_used = tokens_used
        self._context_window = context_window
        with contextlib.suppress(RuntimeError):
            get_app().invalidate()

    def set_theme(self, theme: TerminalTheme | str) -> None:
        """Switch prompt/status-bar colors for future TUI renders."""
        self.theme = get_theme(theme) if isinstance(theme, str) else theme
        self._console = Console(highlight=False, theme=self.theme.rich_theme)
        self._style = self._make_style()
        # ``PromptSession`` keeps a mutable ``Application`` after first
        # render; update both slots so the next toolbar repaint uses the
        # selected palette in tests and in live terminals.
        self._prompt.style = self._style
        app = getattr(self._prompt, "app", None)
        if app is not None:
            app.style = self._style
        with contextlib.suppress(RuntimeError):
            get_app().invalidate()

    def set_model_name(self, model_name: str) -> None:
        """
        Update the toolbar agent label (and window title) at runtime.

        Used when the session's bound agent changes mid-run — e.g. an
        in-place agent switch made from another client — so the bottom
        toolbar and the terminal tab stop showing the launch-time
        agent. The toolbar re-reads the label on every repaint; the
        window title is re-emitted only when one was configured at
        construction (a host launched without a title stays untitled).

        :param model_name: New agent label shown in the bottom
            toolbar, e.g. ``"claude native ui"``.
        """
        self._model = model_name
        if self._window_title is not None:
            self._window_title = model_name
            self._try_set_window_title()
        with contextlib.suppress(RuntimeError):
            get_app().invalidate()

    def add_overlay(self, overlay: Overlay) -> None:
        """
        Register an :class:`Overlay` with this host.

        Must be called before :meth:`run`. The overlay's trigger
        key becomes a binding on the main prompt; pressing it
        exits the prompt with :data:`_OVERLAY_REQUEST_SENTINEL`,
        which :meth:`run` recognizes and dispatches to
        :meth:`_show_overlay`.

        Triggers are unique per host — registering two overlays
        against the same key is rejected so subtle "which one
        fires" bugs don't sneak in.

        :param overlay: The overlay definition. Its ``builder``
            is captured by reference; callers may update any state
            the builder closes over and the next open will pick it
            up.
        :raises ValueError: If *overlay.trigger* is already
            registered by a previous :meth:`add_overlay` call.
        """
        if overlay.trigger in self._overlays_by_trigger:
            raise ValueError(
                f"overlay trigger {overlay.trigger!r} is already registered",
            )
        # Validate action keys upfront so a misconfigured overlay
        # fails at registration rather than when the user presses
        # the conflicting key inside the overlay (where the
        # binding silently does the wrong thing — a close key
        # would close instead of running the action, etc.).
        reserved = {*overlay.close_keys, overlay.trigger}
        seen_action_keys: set[str] = set()
        for action in overlay.actions:
            if action.key in reserved:
                raise ValueError(
                    f"overlay action key {action.key!r} collides with a close "
                    f"key or trigger ({reserved!r}); pick a non-conflicting key"
                )
            if action.key in seen_action_keys:
                raise ValueError(
                    f"overlay action key {action.key!r} is registered twice; "
                    f"each action must have a unique key"
                )
            seen_action_keys.add(action.key)
        self._overlays_by_trigger[overlay.trigger] = overlay

        # When the trigger fires, tear down the current prompt by
        # exiting the app with the sentinel as the "result" value.
        # prompt-toolkit's ``PromptSession.prompt_async`` returns
        # whatever was passed to ``event.app.exit(result=...)``, so
        # the run loop sees the sentinel and opens the overlay.
        @self._kb.add(overlay.trigger)
        def _trigger(event: KeyPressEvent) -> None:
            event.app.exit(result=_OVERLAY_REQUEST_SENTINEL + overlay.trigger)

    def _try_set_window_title(self) -> None:
        """
        Drive ``Output.set_title`` for the configured window title.

        No-op when ``self._window_title`` is ``None``. Best-effort:
        a terminal that doesn't honor ``OSC 0`` (or an ``Output``
        backend without title support) shouldn't take the host
        down. The legacy CLI's ``_set_terminal_title``
        (``omnigent/inner/cli.py:2717-2723``) swallows the same
        way for the same reason; mirroring keeps behavior
        identical so a session that boots green on legacy boots
        green on Omnigent mode regardless of terminal quirks.
        """
        if self._window_title is None:
            return
        # Broad swallow matches the legacy CLI's bare ``except`` —
        # title is decorative; any failure (OSError on a closed
        # tty, AttributeError on a stub Output, anything else) must
        # not break the REPL.
        with contextlib.suppress(Exception):
            self._output.set_title(self._window_title)
            self._output.flush()

    def _try_clear_window_title(self) -> None:
        """
        Drive ``Output.clear_title`` to revert the window title.

        Mirror of :meth:`_try_set_window_title` for cleanup. Best-
        effort for the same reason — a terminal that ignored the
        set won't error on the clear, but a backend that crashed
        on set must not crash on clear either, or the host's
        teardown surfaces a swallowed-set error as a real exit
        exception.
        """
        if self._window_title is None:
            return
        with contextlib.suppress(Exception):
            self._output.clear_title()
            self._output.flush()

    async def __aenter__(self) -> TerminalHost:
        self._try_set_window_title()
        # Redirect stderr to the CLI log file so stray logging,
        # warnings, or third-party print(..., file=sys.stderr)
        # calls don't paint into the prompt-toolkit screen.
        # Restored in __aexit__.
        try:
            from omnigent.cli_diagnostics import redirect_stderr_to_log

            redirect_stderr_to_log()
        except Exception as err:
            _log.exception("Failed to redirect stderr to the CLI diagnostics log: %s", err)
        return self

    async def _drain_pending_handlers(self) -> None:
        """
        Wait for in-flight handler tasks to finish, with a bounded
        timeout, before the host cancels everything.

        Background: ``host.run`` can return prematurely if
        ``prompt_async`` raises ``EOFError`` — which prompt_toolkit
        does spuriously in some PTY setups right after processing a
        submit. If we cancelled immediately, the handler task just
        created for the user's input would die mid-request, the user
        sees nothing (not even an error), and the REPL exits as if
        nothing happened. Instead, give live handlers a bounded
        window to finish; only cancel if they exceed the budget.

        Bounded drain. 30s covers a typical LLM roundtrip; past that
        the user has likely moved on or wants to force-exit. Caller
        is responsible for ``self.cancel()`` afterward.
        """
        live = [t for t in self._tasks if not t.done()]
        if not live:
            return
        try:
            _done, pending = await asyncio.wait(live, timeout=30.0)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            # __aexit__ itself was cancelled (e.g. parent scope is
            # shutting down fast). Fall through so caller's
            # ``self.cancel()`` runs and we don't leak tasks.
            pass

    async def __aexit__(self, *exc: object) -> None:
        # Restore stderr first — everything below (drain, cancel)
        # may log or raise, and those should go to the real terminal
        # now that the TUI is tearing down.
        try:
            from omnigent.cli_diagnostics import restore_stderr

            restore_stderr()
        except Exception as err:
            _log.exception("Failed to restore stderr from the CLI diagnostics log: %s", err)
        # Clear the title before the drain so it reverts even if a
        # handler blocks for the full 30s budget below.
        self._try_clear_window_title()
        # If ``exc`` indicates a real exception bubbling up, skip
        # the drain — cancel fast so the error surfaces without
        # waiting on a now-orphaned LLM call.
        if not any(e is not None for e in exc):
            await self._drain_pending_handlers()
        self.cancel()

    @property
    def pending_attachments(self) -> list[PendingAttachment]:
        """Files queued for the next message."""
        return self._pending_attachments

    def take_attachments(self) -> list[PendingAttachment]:
        """Take and clear pending attachments."""
        attachments = self._pending_attachments
        self._pending_attachments = []
        return attachments

    def _handle_paste_text(self, raw: str) -> str:
        """
        Decide what to insert into the prompt buffer for a paste.

        Testable core of the ``Keys.BracketedPaste`` binding. Drag-and-
        drop pastes (resolvable file path in text) and short pastes
        pass through; only pastes crossing the thresholds register
        a placeholder.

        :param raw: Bracketed-paste payload from prompt-toolkit,
            e.g. ``"line1\\r\\nline2\\r\\n"``.
        :returns: Text to insert — normalized paste, placeholder
            marker, or ``""`` for empty input (caller skips insert).
        """
        text = _normalize_paste(raw)
        if not text:
            return ""
        if _extract_file_paths(text):
            return text
        if not _should_abstract_paste(text):
            return text
        return self._register_pasted_block(text).placeholder

    def _register_pasted_block(self, text: str) -> _PastedBlock:
        """
        Allocate a placeholder and append a registry entry.

        :param text: Normalized paste content.
        :returns: The newly-registered block; caller inserts
            ``block.placeholder`` into the prompt buffer.
        """
        block_id = len(self._pasted_blocks) + 1
        placeholder = _format_paste_placeholder(block_id, text)
        block = _PastedBlock(block_id=block_id, placeholder=placeholder, content=text)
        self._pasted_blocks.append(block)
        return block

    def _expand_pasted_blocks(self, text: str) -> str:
        """
        Substitute placeholder markers with original content and clear
        the registry.

        Markers edited away by the user pass through unchanged and the
        content is dropped — safer than guessing the splice point.

        :param text: The submitted prompt buffer string.
        :returns: ``text`` with each placeholder replaced by its content.
        """
        if not self._pasted_blocks:
            return text
        expanded = text
        for block in self._pasted_blocks:
            # count=1 defends against two entries aliasing the same
            # content (impossible in practice given distinct ``#N``).
            expanded = expanded.replace(block.placeholder, block.content, 1)
        self._pasted_blocks = []
        return expanded

    async def run(self, handler: Callable[..., Awaitable[None]]) -> None:
        """Run the input loop.

        Uses the alternate screen buffer so the prompt stays pinned
        at the bottom on terminal resize. Output scrolls above the
        prompt. On exit, the alternate buffer is discarded and the
        original terminal content is restored.

        Calls ``handler(text)`` as a background task for each input.
        The prompt re-renders immediately so the bar stays visible.
        If the user types while a handler is running, a new task
        starts (for steering). Escape cancels all tasks.
        """

        async def _toolbar_ticker() -> None:
            """
            Drive the spinner animation + elapsed-time counter.

            Four cadences:

            - **Streaming**: tick every
              :data:`_SPINNER_TICK_SECONDS` (100 ms, 10 fps) so
              the Braille spinner in the ``⠹ working`` line
              and the ``state: running ⠹`` badge animate
              smoothly.
            - **Busy without an active timer**: same cadence, so
              short local handlers and task-completion transitions
              still repaint the activity row promptly.
            - **Sub-agents active**: same cadence, so the
              ``state: N agents running ⠹`` badge keeps animating
              while the top-level agent is idle (parked awaiting
              its sub-agents).
            - **Ctrl+C exit hint armed**: tick every 250 ms so
              the ``Press Ctrl+C again to exit`` hint clears
              within a quarter second of its
              :data:`_EXIT_CONFIRM_WINDOW` expiring. Without
              this cadence the idle 500 ms tick would let the
              hint linger for up to that window past expiry.
            - **Idle**: 500 ms — nothing to animate, just keep
              the toolbar responsive to late ``is_busy`` flips.
            """
            while True:
                if self._stream_start is not None or self.is_busy or self.has_active_subagents():
                    if self._prompt.app:
                        self._prompt.app.invalidate()
                    await asyncio.sleep(_SPINNER_TICK_SECONDS)
                elif self._exit_confirm_deadline is not None:
                    if self._prompt.app:
                        self._prompt.app.invalidate()
                    await asyncio.sleep(0.25)
                else:
                    await asyncio.sleep(0.5)

        ticker = asyncio.create_task(_toolbar_ticker())
        try:
            # Push Kitty Keyboard Protocol mode (flag 1 =
            # disambiguate escape codes). Without this the terminal
            # never knows to send CSI-u encoded sequences — e.g.
            # Shift+Enter arrives as plain \r instead of
            # \x1b[13;2u, so the F20 binding never fires. The
            # push/pop model (\x1b[>Xu / \x1b[<u) restores the
            # previous mode on exit. Terminals that don't support
            # the protocol ignore the sequence per ECMA-48.
            self._output.write_raw("\x1b[>1u")
            self._output.flush()
            with patch_stdout(raw=True):
                while True:
                    try:
                        line = await self._read_input()
                    except (EOFError, KeyboardInterrupt):
                        break

                    if self._exit_requested:
                        break

                    if line is None:
                        continue

                    # Overlay trigger — ``add_overlay`` wired the
                    # key binding to ``exit(result=<sentinel + trigger>)``.
                    # Decode the trigger from the sentinel tail and
                    # run the overlay. When it returns, loop back to
                    # a fresh ``_read_input`` so the main prompt
                    # resumes exactly where the user left off.
                    #
                    # KeyboardInterrupt from inside the overlay
                    # (its ``c-c`` binding raises it via
                    # ``app.exit(exception=KeyboardInterrupt())``)
                    # propagates out of ``_show_overlay`` and must
                    # break the outer loop just like Ctrl+C from
                    # the main prompt — otherwise Ctrl+C inside
                    # the overlay falls through to ``continue``
                    # and the REPL keeps running.
                    if isinstance(line, str) and line.startswith(
                        _OVERLAY_REQUEST_SENTINEL,
                    ):
                        trigger = line[len(_OVERLAY_REQUEST_SENTINEL) :]
                        overlay = self._overlays_by_trigger.get(trigger)
                        if overlay is not None:
                            try:
                                await self._show_overlay(overlay)
                            except KeyboardInterrupt:
                                break
                        continue

                    # Inline ↓ sub-agents menu selection — Enter on a menu
                    # row exits with this sentinel + the chosen session id.
                    # Run the switch callback here (between prompt
                    # iterations) so it re-renders against the main prompt,
                    # not while the input app still owns the screen.
                    if isinstance(line, str) and line.startswith(
                        _SUBAGENT_SELECT_SENTINEL,
                    ):
                        session_id = line[len(_SUBAGENT_SELECT_SENTINEL) :]
                        cb = self.on_subagent_select
                        if cb is not None and session_id:
                            try:
                                await cb(session_id)
                            except Exception:
                                _log.exception("on_subagent_select callback failed")
                        continue

                    line = line.strip()

                    # Detect file paths from drag-and-drop paste.
                    attachments = _extract_file_paths(line)
                    if attachments:
                        self._pending_attachments.extend(attachments)
                        # Invalidate so the paperclip renders immediately.
                        if self._prompt.app:
                            self._prompt.app.invalidate()
                        # If ONLY file paths and no other text, queue
                        # and wait for a message.
                        att_paths = {a.path for a in attachments}
                        non_file_tokens = [
                            t
                            for t in line.split()
                            if str(pathlib.Path(os.path.expanduser(t.strip("'\""))).resolve())
                            not in att_paths
                        ]
                        if not non_file_tokens:
                            continue

                    # Detect @-mention file references.
                    from ._completer import extract_at_mentions, strip_at_mentions

                    at_attachments = extract_at_mentions(line)
                    if at_attachments:
                        self._pending_attachments.extend(at_attachments)
                        if self._prompt.app:
                            self._prompt.app.invalidate()
                        line = strip_at_mentions(line, at_attachments)
                        attachments = attachments + at_attachments

                    # Allow empty text when attachments are pending (the
                    # user dropped a file then hit Enter without typing).
                    if not line and not self._pending_attachments:
                        # Reset the paste registry so ``#N`` restarts at 1.
                        self._pasted_blocks = []
                        continue

                    # Splice paste content into the line; the abstraction
                    # is only for the visible buffer. Done AFTER file
                    # detection so embedded paths aren't auto-uploaded.
                    line = self._expand_pasted_blocks(line)

                    # Clear attachments before starting the handler.
                    files = self.take_attachments()
                    task = asyncio.create_task(handler(line, files))
                    self._tasks.append(task)
                    task.add_done_callback(self._on_handler_task_done)
        finally:
            # Pop Kitty Keyboard Protocol — restore previous mode.
            self._output.write_raw("\x1b[<u")
            self._output.flush()
            ticker.cancel()

    def _on_handler_task_done(self, task: asyncio.Task[None]) -> None:
        """
        Remove a completed input handler and repaint the prompt.

        The prompt's activity row depends on :meth:`is_busy`, which
        flips only after the task leaves ``self._tasks``. Without an
        explicit repaint request the top prompt can keep showing the
        last busy render even though the bottom toolbar has moved back
        to ``state: sleeping``.

        :param task: Completed task previously appended to
            ``self._tasks`` by :meth:`run`.
        """
        if task in self._tasks:
            self._tasks.remove(task)
        self._invalidate_prompt()

    async def _show_overlay(self, overlay: Overlay) -> None:
        """
        Run the overlay's fullscreen view until the user closes it.

        Invoked by :meth:`run` when ``_read_input`` returns the
        overlay sentinel. Builds a dedicated prompt-toolkit
        :class:`Application` whose layout is: an optional title
        line on top, a scrollable buffer holding the rendered
        content, and a footer hint line. Arrow keys / Page Up /
        Page Down / Home / End scroll the buffer; Esc, q, and the
        original trigger key close it.

        Rationale for a separate Application: the main prompt is
        a :class:`PromptSession` and not trivially augmentable with
        an alternate layout. A standalone fullscreen app — run
        sequentially, not concurrently — keeps the two layouts
        isolated. When this method returns, the caller loops back
        into the main prompt's ``_read_input``.

        :param overlay: The registered :class:`Overlay` to display.
        """
        # Enumerate sidebar targets. When ``targets_builder`` is
        # ``None`` OR returns empty, we render single-pane mode —
        # no sidebar, no Tab bindings.
        targets: list[OverlayTarget] = []
        if overlay.targets_builder is not None:
            try:
                result = await overlay.targets_builder()
            except Exception as exc:
                _log.exception("Overlay targets_builder failed")
                result = []
                targets_error: str | None = f"{type(exc).__name__}: {exc}"
            else:
                targets_error = None
            if result:
                targets = list(result)
        else:
            targets_error = None

        has_sidebar = bool(targets)
        # Mutable holders so the Tab/Shift-Tab key callbacks can
        # mutate selection/scroll state without ``nonlocal`` on
        # every handler. Single-element lists are the idiomatic
        # escape hatch in prompt-toolkit's callback style.
        selected_index = [0]
        scroll_offset = [0]
        # Sidebar viewport offset: when the target list is taller
        # than the available terminal rows, the sidebar renderer
        # slices ``targets[sidebar_scroll_offset:offset+height]``
        # so the selected row stays visible as the user
        # tab-navigates. Without this the selection marker
        # invisibly walks off the bottom of the viewport — exactly
        # the user-reported 2026-04-30 symptom where 38 terminals
        # rendered but only the first ~29 were visible and tabbing
        # past s29 left no on-screen indication of which row was
        # selected. Updated by :func:`_ensure_selection_visible`
        # called from the tab / s-tab handlers.
        sidebar_scroll_offset = [0]
        # Where the terminal's blinking cursor rests inside the
        # content pane. "top" — row 0 of the visible window (less
        # pager default, matches how all generic scroll bindings
        # leave the cursor). "bottom" — the row that renders the
        # last content line, so vim ``G`` / ``end`` land the
        # cursor ON the last line instead of at the top of the
        # last page. Used only by :func:`_get_cursor_position`.
        cursor_anchor: list[str] = ["top"]
        content_lines_holder: list[list[str]] = [[]]
        # Search state — vim / less-style ``/`` incremental
        # search over the content pane. ``search_active`` means
        # the user has pressed ``/`` and is typing a query; the
        # footer flips to a live ``/pattern (match/total)``
        # status line and printable key presses append to
        # ``search_query`` instead of firing their normal
        # bindings. ``search_origin`` remembers the pre-search
        # scroll position so Esc can revert cleanly even after
        # the incremental scroll has hopped around.
        search_active = [False]
        search_query: list[str] = [""]
        search_origin = [0]
        # Monotonically-increasing generation counter. Incremented
        # on every ``_rebuild_content`` kick-off; a stale async
        # rebuild whose generation no longer matches at completion
        # time drops its result on the floor. Without this guard,
        # two rapid Tabs (or a Tab during an in-flight periodic
        # refresh) could land out of order and clobber the newer
        # target's content with the older target's content.
        rebuild_generation = [0]
        # Signature of the last successfully-rendered content,
        # keyed by ``(target_key, content_raw)``. The 500 ms
        # refresh loop compares the next render's signature
        # against this and skips ``content_lines_holder`` /
        # ``invalidate`` when nothing changed — that's what kills
        # the flicker users saw as the turn streamed server-side
        # while the overlay was open.
        last_signature: list[tuple[str | None, str] | None] = [None]
        # The target key displayed by the current ``content_lines``
        # frame. When a Tab lands, we know to reset scroll because
        # the target changed; when the refresh loop fires on the
        # same target, scroll position is preserved.
        current_target_key: list[str | None] = [None]

        # Holder for the running Application so the Tab handlers
        # can call ``invalidate()`` without a forward reference.
        # Populated below, just before ``run_async()``.
        app_holder: list[Application[None] | None] = [None]

        async def _rebuild_content(
            *,
            force: bool = False,
            reset_scroll: bool = True,
        ) -> None:
            """
            Re-render the content pane for the current selection.

            Invoked on overlay open, every Tab/Shift-Tab, and on
            each tick of the periodic refresh loop. The builder
            runs fresh each time so it sees latest server state;
            we rebuild the rasterised line cache
            (``content_lines_holder``) so the scroll window picks
            up the new content on the next render tick.

            Flicker / race avoidance:

            1. Increments ``rebuild_generation`` at entry and
               captures the value locally; if the generation moves
               again before the async builder returns, the stale
               result is discarded.
            2. Computes a content signature and early-returns
               (no ``invalidate`` → no repaint) when it matches
               the last rendered one. The periodic refresh uses
               this to stay silent when nothing has changed on
               the server side.
            3. Only resets scroll when the target actually changed
               (or the caller explicitly asks via
               ``reset_scroll=True`` — the default for Tab).
               Periodic refreshes of the same target pass
               ``reset_scroll=False`` so the user's scroll
               position isn't yanked to the top every 500 ms.

            :param force: When ``True``, skip the signature-
                equality early-return and always write the new
                content. Used on initial open so the first frame
                paints even if the cache is somehow pre-populated.
            :param reset_scroll: When ``True``, reset scroll to 0
                whenever the target key differs from the last
                rendered one. Callers doing a periodic refresh
                of the same target should pass ``False``.
            """
            generation = rebuild_generation[0] + 1
            rebuild_generation[0] = generation
            target = targets[selected_index[0]] if has_sidebar else None
            target_key = target.key if target is not None else None
            content_raw = await self._render_overlay_content(overlay, target=target)
            # Drop stale result — a newer rebuild has started
            # since ours kicked off.
            if rebuild_generation[0] != generation:
                return
            signature = (target_key, content_raw)
            if not force and signature == last_signature[0]:
                return
            content_lines_holder[0] = content_raw.split("\n")
            if reset_scroll and current_target_key[0] != target_key:
                scroll_offset[0] = 0
                cursor_anchor[0] = "top"
            current_target_key[0] = target_key
            last_signature[0] = signature
            app = app_holder[0]
            if app is not None:
                app.invalidate()

        await _rebuild_content(force=True)

        async def _refresh_loop() -> None:
            """
            Periodically rebuild the current target's content.

            500 ms cadence — tight enough that users see turn
            updates land without manual intervention, loose enough
            that each tick's HTTP ``list_items`` round-trip doesn't
            hammer the server. Matches omnigent' overview polling
            strategy (see ``omnigent/cli.py::_refresh_loop``),
            with the interval bumped from 50 ms → 500 ms because
            Omnigent' builder crosses a real HTTP boundary
            while omnigent' builder just reads in-process state.
            """
            try:
                while True:
                    await asyncio.sleep(0.5)
                    await _rebuild_content(reset_scroll=False)
            except asyncio.CancelledError:
                return

        refresh_task: asyncio.Task[None] = asyncio.create_task(_refresh_loop())

        def _max_scroll() -> int:
            """
            Return the largest scroll offset that still leaves
            the content pane fully populated.

            Setting ``scroll_offset`` to ``len(lines) - 1`` (as
            an earlier version did) would put only the final line
            on-screen with the rest of the pane left blank. The
            correct ceiling is ``len(lines) - view_height`` so the
            last line lands on the last visible row and
            ``view_height - 1`` preceding lines fill the pane
            above it. When content is shorter than the pane,
            clamps to 0 so we never emit a negative offset.

            :returns: Clamped maximum for ``scroll_offset[0]``.
            """
            chrome = 3 + (2 if overlay.title else 0)
            view_height = max(5, _term_height() - chrome)
            lines = content_lines_holder[0]
            return max(0, len(lines) - view_height)

        def _highlight_matches(line: str, needle: str) -> str:
            """
            Wrap every case-insensitive occurrence of *needle* in
            *line* with a yellow-background + black-foreground
            ANSI span (``\\x1b[30;43m`` … ``\\x1b[39;49m``).

            Matches ``less``'s default highlight colors, which
            are more visible than plain reverse-video against
            Rich's already-styled terminal output. Uses a regex
            with ``re.IGNORECASE``; the match's exact casing is
            preserved in the replacement so ``Bash`` and
            ``BASH`` both highlight without normalising the
            user-visible text. Empty needle returns the line
            unchanged so this is safe to call unconditionally
            in the render path.

            The closing sequence resets foreground (``\\x1b[39m``)
            and background (``\\x1b[49m``) independently — using
            a single ``\\x1b[0m`` would clear every other
            attribute the Rich rendering set (bold, dim, link,
            etc.), so the rest of the line would lose its
            styling at each match.

            :param line: One content line — may already contain
                ANSI color escapes from Rich rendering; those are
                preserved, with the highlight layered on top.
            :param needle: Search query, e.g. ``"bash"``.
            :returns: The line with highlight markers inserted
                around each match.
            """
            if not needle:
                return line
            import re as _re

            pattern = _re.compile(_re.escape(needle), _re.IGNORECASE)
            return pattern.sub(
                lambda m: f"\x1b[30;43m{m.group(0)}\x1b[39;49m",
                line,
            )

        def _get_visible_ansi() -> ANSI:
            """
            Return the slice of content lines currently in view.

            Recomputed each render tick so scroll-key bindings that
            mutate ``scroll_offset`` take effect on the next frame
            without manually invalidating the app. When a search
            query is set (either during active ``/`` input or after
            Enter commit), every visible line is run through
            :func:`_highlight_matches` so occurrences render in
            reverse-video — the "yellow highlight" users expect
            from less / vim / grep pagers.
            """
            # Subtract chrome: optional title (2 lines) + divider +
            # footer (2 lines).
            chrome = 3 + (2 if overlay.title else 0)
            view_height = max(5, _term_height() - chrome)
            lines = content_lines_holder[0]
            if not lines:
                return ANSI("")
            start = max(0, min(scroll_offset[0], len(lines) - 1))
            end = start + view_height
            scroll_offset[0] = start
            visible = lines[start:end]
            needle = search_query[0]
            if needle:
                visible = [_highlight_matches(line, needle) for line in visible]
            return ANSI("\n".join(visible))

        def _get_cursor_position() -> Point:
            """
            Place the terminal cursor inside the content pane.

            The default position (0, 0) — i.e. the top-left of
            the currently visible slice — is what prompt-toolkit
            gives us without intervention. For most scroll
            bindings that's the right answer (less-style pager),
            but ``G`` / ``end`` specifically ask to "go to the
            last line," and users expect the cursor to land ON
            that last line rather than at the top of the
            page-sized window that renders it. Honoring that
            expectation means resolving the cursor row
            dynamically from ``scroll_offset``, ``view_height``,
            and the total content length — we can't hardcode a
            row because the visible window may be shorter than
            view_height when we're at EOF on a file shorter than
            the pane.

            :returns: Cursor position in the content pane's text
                coordinate space. Column is always 0.
            """
            if cursor_anchor[0] != "bottom":
                return Point(x=0, y=0)
            chrome = 3 + (2 if overlay.title else 0)
            view_height = max(5, _term_height() - chrome)
            lines = content_lines_holder[0]
            if not lines:
                return Point(x=0, y=0)
            # Number of content lines currently rendered in the
            # pane — capped by view_height when content overflows
            # the pane, capped by remaining lines when it doesn't.
            visible_count = min(view_height, max(0, len(lines) - scroll_offset[0]))
            last_row = max(0, visible_count - 1)
            return Point(x=0, y=last_row)

        def _sidebar_visible_height() -> int:
            """
            Compute how many sidebar rows fit in the current
            terminal viewport.

            Subtracts the overlay's chrome from the terminal
            height. The chrome rows are added to ``layout_children``
            below — count them in lock-step:

            * 1 row for the title bar (when ``overlay.title`` set).
            * 1 row for the separator under the title (same gate).
            * 1 row for the bottom separator above the footer
              (always present).
            * 1 row for the footer hint (always present).

            Used by :func:`_ensure_selection_visible` and
            :func:`_get_sidebar` to keep the selected target on
            screen as the user tab-navigates a long target list.
            Returns at least 1 even on absurdly small terminals so
            the sidebar always shows at least the selected row.
            Falls back to a reasonable default when ``get_app()``
            isn't available (overlay construction edge cases).

            Off-by-one bug history (2026-04-30): an earlier
            iteration subtracted only 2, missing the title +
            top-separator pair. The result on a 38-target list
            in a 32-row terminal was the snap-down landing
            selection on what _looked like_ the last row but was
            actually the second-to-last visible row, so the
            user-selected row clipped off the bottom.
            """
            try:
                rows = get_app().output.get_size().rows
            except Exception:
                rows = 24
            # Always 2: bottom separator + footer.
            chrome = 2
            if overlay.title:
                # Add 2: title bar + separator under the title.
                chrome += 2
            return max(1, rows - chrome)

        def _ensure_selection_visible() -> None:
            """
            Adjust ``sidebar_scroll_offset[0]`` so the currently
            selected target is inside the visible window.

            Called by the tab / s-tab handlers after the selection
            moves. Snaps the viewport just enough to bring the
            selection into view — selection above viewport scrolls
            the offset down to match; selection below viewport
            scrolls the offset up so the selection lands on the
            last visible row. Wraps cleanly around the
            ``(selected_index + 1) % len(targets)`` wrap-to-top
            case because the comparison is against the new
            selected_index regardless of how it changed.
            """
            if not has_sidebar:
                return
            sidebar_scroll_offset[0] = _compute_sidebar_scroll_offset(
                selected_index=selected_index[0],
                current_offset=sidebar_scroll_offset[0],
                visible_height=_sidebar_visible_height(),
            )

        def _get_sidebar() -> ANSI:
            """
            Render the sidebar column as ANSI, one row per target.

            The selected row gets a ``▸`` prefix + bold style; the
            rest use a muted color. Each row is left-padded/truncated
            to *sidebar_width* so the separator column stays aligned
            even with variable-length labels. Matches the omnigent
            debug panel's visual layout.

            Slices the target list by ``sidebar_scroll_offset``
            and the current viewport height so the selection
            always stays on screen as the user tab-navigates a
            list longer than the terminal — without this, lists
            of N > terminal-height rows let selection walk
            invisibly off the bottom edge.
            """
            if not has_sidebar:
                return ANSI("")
            visible = _sidebar_visible_height()
            offset = sidebar_scroll_offset[0]
            # Slice the visible window. The slice ALSO bounds
            # the targets-error banner: if a per-target fetch
            # failed during sidebar construction, the error row
            # eats one of the visible slots — acceptable
            # because the user needs to see WHY targets are
            # missing more than they need every visible row.
            window = targets[offset : offset + visible]
            rows: list[str] = []
            if targets_error is not None:
                rows.append(f"\x1b[31m{targets_error}\x1b[0m")
            for window_idx, t in enumerate(window):
                i = offset + window_idx
                is_selected = i == selected_index[0]
                marker = "▸" if is_selected else " "
                # ``None`` icon renders as an empty prefix so
                # the marker column still aligns with icon rows.
                # Note on emoji width: any icon with a wcswidth
                # value that disagrees with the terminal's
                # actual rendering will misalign rows by ±1
                # column. wcswidth is the source of truth for
                # both this padding AND prompt-toolkit's
                # :class:`Window` layer that contains us — so
                # we can't compensate from here, the Window
                # would just override. The fix lives at the
                # caller: pick icons whose wcswidth matches
                # terminal rendering (i.e. East-Asian-Width
                # Wide emoji like ``🤖`` / ``👾`` / ``🐚``;
                # AVOID Neutral-classified ones like ``🖥`` /
                # ``⌨`` that wcswidth says is 1 cell but
                # terminals render as 2).
                icon = f"{t.icon} " if t.icon is not None else ""
                raw_label = f"{marker}{icon}{t.label}"
                pad_len = overlay.sidebar_width - _display_width(raw_label)
                if pad_len < 0:
                    raw_label = raw_label[: overlay.sidebar_width]
                    pad_len = 0
                padded = raw_label + (" " * pad_len)
                if is_selected:
                    rows.append(f"\x1b[1m{padded}\x1b[0m")
                else:
                    rows.append(f"\x1b[38;2;106;106;106m{padded}\x1b[0m")
            return ANSI("\n".join(rows))

        # Footer hint tells the user how to close + scroll +
        # switch + search. Rendered via a callable so the footer
        # can flip to the live ``/query (N/M)`` status while the
        # user is typing a search — no separate footer Window,
        # same vertical slot, zero layout flicker on mode change.
        # ``_build_close_hint`` honors ``overlay.close_hint`` when
        # the caller wants a custom hint, and otherwise renders
        # all close keys + trigger via ``_abbreviate_key`` so the
        # hint reads consistently (no more mixed
        # ``escape``/``q``/``c-i`` notation).
        close_hint = _build_close_hint(overlay)
        # Per-target action hints render between navigation and
        # search — close to the close hint so the eye picks them
        # up alongside other "things you can press right now."
        # When no actions are registered the segment collapses
        # away entirely.
        action_hint = (
            " · ".join(f"{a.key} {a.label}" for a in overlay.actions)
            if has_sidebar and overlay.actions
            else ""
        )
        if has_sidebar:
            segments = [
                "←→/tab switch",
                "↑↓ gg/G scroll",
                "pgup/pgdn page",
                "/ search",
            ]
            if action_hint:
                segments.append(action_hint)
            segments.append(close_hint)
            idle_footer = " · ".join(segments)
        else:
            idle_footer = "↑↓ gg/G scroll · pgup/pgdn page · / search · " + close_hint

        def _find_matches(query: str) -> list[int]:
            """
            Return the line indices of every match for *query*.

            Case-insensitive substring match against the raw
            content lines (ANSI escapes stripped on the fly so
            color spans don't interrupt the pattern). Empty
            query returns an empty list — the caller decides
            whether that's "no matches" or "search not started".

            :param query: The search needle, e.g. ``"Bash"``.
            :returns: Sorted line indices where the needle
                appears, e.g. ``[3, 17, 42]``. Empty when
                *query* is empty or nothing matches.
            """
            if not query:
                return []
            import re as _re

            ansi_re = _re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
            needle = query.casefold()
            hits: list[int] = []
            for i, raw in enumerate(content_lines_holder[0]):
                stripped = ansi_re.sub("", raw)
                if needle in stripped.casefold():
                    hits.append(i)
            return hits

        def _current_match_index(hits: list[int]) -> int:
            """
            Find which 0-based match corresponds to the current
            scroll position.

            When multiple matches are on-screen, pick the one
            closest to (but not past) ``scroll_offset``. Used
            to populate the ``(N/M)`` counter in the search
            footer. Returns ``0`` when no hits.

            :param hits: List of match line indices.
            :returns: 0-based index into *hits*, clamped to
                ``[0, len(hits))``.
            """
            if not hits:
                return 0
            for idx, line in enumerate(hits):
                if line >= scroll_offset[0]:
                    return idx
            return len(hits) - 1

        def _build_footer() -> str:
            """
            Return the current footer text.

            Flips between the idle hint line and the live search
            status. Called on every render tick so mode changes
            appear immediately without a layout rebuild.
            """
            if not search_active[0]:
                return idle_footer
            hits = _find_matches(search_query[0])
            if not search_query[0]:
                return f"/{search_query[0]}_  (type to search — Enter commit · Esc cancel)"
            if hits:
                current = _current_match_index(hits) + 1
                return f"/{search_query[0]}  ({current}/{len(hits)})  · Enter commit · Esc cancel"
            return f"/{search_query[0]}  (no matches)  · Esc cancel"

        layout_children: list[Any] = []
        if overlay.title:
            layout_children.append(
                Window(
                    content=FormattedTextControl(
                        text=overlay.title,
                        focusable=False,
                    ),
                    height=1,
                    style="bold",
                ),
            )
            layout_children.append(
                Window(height=1, char="─", style="class:bar"),
            )

        content_window = Window(
            content=FormattedTextControl(
                text=_get_visible_ansi,
                focusable=True,
                get_cursor_position=_get_cursor_position,
            ),
            wrap_lines=True,
        )
        if has_sidebar:
            # Import locally so single-pane mode doesn't pay for
            # ``VSplit`` (lives in the same layout module but only
            # needed when sidebar is on).
            from prompt_toolkit.layout import VSplit

            sidebar_window = Window(
                content=FormattedTextControl(text=_get_sidebar, focusable=False),
                width=overlay.sidebar_width,
            )
            separator_window = Window(width=1, char="│", style="class:bar")
            layout_children.append(
                VSplit([sidebar_window, separator_window, content_window]),
            )
        else:
            layout_children.append(content_window)

        layout_children.append(
            Window(height=1, char="─", style="class:bar"),
        )
        layout_children.append(
            Window(
                content=FormattedTextControl(text=_build_footer, focusable=False),
                height=1,
                style=self.theme.muted,
            ),
        )

        overlay_kb = KeyBindings()
        # ``not_searching`` / ``searching`` gate every navigation
        # binding so ``/`` search mode can take over printable
        # keys + Esc + Enter + Backspace without conflicting with
        # the scroll / close / Tab handlers. prompt-toolkit's
        # :class:`Condition` is evaluated on each key press; the
        # single ``search_active`` holder is the source of truth
        # and flipping it swaps the entire key map.
        not_searching = Condition(lambda: not search_active[0])
        searching = Condition(lambda: search_active[0])

        # ``close_keys`` + the trigger itself all dismiss the
        # overlay — including re-pressing the trigger so the
        # hotkey toggles open/close. ``escape`` is overridden
        # separately below so it cancels active searches instead
        # of closing the overlay (vim/less muscle memory).
        for key in (*overlay.close_keys, overlay.trigger):
            if key == "escape":
                continue

            @overlay_kb.add(key, filter=not_searching)
            def _close(event: KeyPressEvent) -> None:
                event.app.exit()

        # Per-target action keys. Each fires the action's
        # handler against whatever target the sidebar has
        # selected; the handler is responsible for any user-
        # facing output, but exceptions are caught here so a
        # broken action can't crash the overlay (the user
        # would lose whatever scrollback they were inspecting).
        # Actions only make sense with a sidebar — without
        # targets there's nothing to operate on, so the loop
        # is a no-op when ``targets`` is empty.
        if has_sidebar:
            for action in overlay.actions:
                # Capture by default-arg so each closure binds
                # its own action; otherwise they'd all reference
                # the loop variable and the last action would
                # win on every key.
                @overlay_kb.add(action.key, filter=not_searching)
                def _run_action(
                    event: KeyPressEvent,
                    _action: OverlayAction = action,
                ) -> None:
                    if not targets:
                        return
                    target = targets[selected_index[0]]

                    async def _invoke() -> None:
                        # Overlay handler errors are swallowed at the
                        # host so the overlay stays open; handlers
                        # own their own user-facing error reporting.
                        with contextlib.suppress(Exception):
                            await _action.handler(target)

                    event.app.create_background_task(_invoke())

        # Ctrl+C in the overlay exits the whole program, matching
        # the main prompt's Ctrl+C behavior. Without this binding,
        # prompt-toolkit swallows ``c-c`` inside the overlay's
        # :class:`Application` (no default fallback when a custom
        # ``KeyBindings`` is supplied) so pressing Ctrl+C appears
        # to do nothing until the overlay is manually closed. We
        # raise :class:`KeyboardInterrupt` so the outer ``run``
        # loop's ``except (EOFError, KeyboardInterrupt): break``
        # catches it and shuts the REPL down cleanly.
        @overlay_kb.add("c-c")
        def _ctrl_c(event: KeyPressEvent) -> None:
            event.app.exit(exception=KeyboardInterrupt())

        @overlay_kb.add("up", filter=not_searching)
        def _up(event: KeyPressEvent) -> None:
            scroll_offset[0] = max(0, scroll_offset[0] - 1)
            cursor_anchor[0] = "top"

        @overlay_kb.add("down", filter=not_searching)
        def _down(event: KeyPressEvent) -> None:
            scroll_offset[0] = min(_max_scroll(), scroll_offset[0] + 1)
            cursor_anchor[0] = "top"

        @overlay_kb.add("pageup", filter=not_searching)
        @overlay_kb.add("c-b", filter=not_searching)
        def _pgup(event: KeyPressEvent) -> None:
            page = max(1, _term_height() - 3)
            scroll_offset[0] = max(0, scroll_offset[0] - page)
            cursor_anchor[0] = "top"

        @overlay_kb.add("pagedown", filter=not_searching)
        @overlay_kb.add("c-f", filter=not_searching)
        def _pgdn(event: KeyPressEvent) -> None:
            page = max(1, _term_height() - 3)
            scroll_offset[0] = min(_max_scroll(), scroll_offset[0] + page)
            cursor_anchor[0] = "top"

        @overlay_kb.add("home", filter=not_searching)
        def _home(event: KeyPressEvent) -> None:
            scroll_offset[0] = 0
            cursor_anchor[0] = "top"

        @overlay_kb.add("end", filter=not_searching)
        def _end(event: KeyPressEvent) -> None:
            # ``end`` / ``G`` both jump to EOF; anchor the cursor
            # on the last visible content row so users see it on
            # the last line rather than at the top of the last
            # page. Matches vim's ``G`` behavior at EOF.
            scroll_offset[0] = _max_scroll()
            cursor_anchor[0] = "bottom"

        # Vim-style scroll: ``G`` jumps to bottom, ``gg`` jumps
        # to top. Matches muscle memory from less / vim / man /
        # every pager the user already knows. prompt-toolkit
        # supports multi-key sequences natively — binding the
        # literal two-key sequence ``("g", "g")`` handles the
        # prefix buffer for us (second ``g`` within the default
        # key-sequence timeout fires the handler; a lone ``g``
        # followed by anything else is swallowed).
        @overlay_kb.add("G", filter=not_searching)
        def _vim_bottom(event: KeyPressEvent) -> None:
            scroll_offset[0] = _max_scroll()
            cursor_anchor[0] = "bottom"

        @overlay_kb.add("g", "g", filter=not_searching)
        def _vim_top(event: KeyPressEvent) -> None:
            scroll_offset[0] = 0
            cursor_anchor[0] = "top"

        # ── Vim / less-style incremental search ──────────────
        # ``/`` enters search mode. The footer flips to the
        # live query + match-count indicator, and printable
        # keystrokes start appending to ``search_query`` via
        # the ``<any>`` binding below. Esc cancels (restoring
        # the pre-search scroll); Enter commits (keeps current
        # match as the new scroll position).
        @overlay_kb.add("/", filter=not_searching)
        def _search_start(event: KeyPressEvent) -> None:
            search_active[0] = True
            search_query[0] = ""
            search_origin[0] = scroll_offset[0]

        # ``n`` / ``N`` cycle to the next / previous match of
        # the last committed query. Only meaningful after a
        # search has been committed — otherwise they fall
        # through to no-ops rather than silently scrolling.
        def _jump_to_match(delta: int) -> None:
            """
            Move scroll to the match *delta* steps away from
            the current cursor position.

            :param delta: ``+1`` for ``n`` (next match), ``-1``
                for ``N`` (previous match).
            """
            hits = _find_matches(search_query[0])
            if not hits:
                return
            current = _current_match_index(hits)
            # If the cursor is already ON a match, advance past
            # it; if between matches, land on the nearest one
            # in the direction of ``delta``.
            at_match = scroll_offset[0] == hits[current]
            if at_match:
                new_idx = (current + delta) % len(hits)
            elif delta > 0:
                new_idx = current % len(hits)
            else:
                new_idx = (current - 1) % len(hits)
            scroll_offset[0] = hits[new_idx]
            cursor_anchor[0] = "top"

        @overlay_kb.add("n", filter=not_searching)
        def _next_match(event: KeyPressEvent) -> None:
            _jump_to_match(+1)

        @overlay_kb.add("N", filter=not_searching)
        def _prev_match(event: KeyPressEvent) -> None:
            _jump_to_match(-1)

        # Search-mode bindings. While active, every printable
        # key appends to the query and the first match becomes
        # the new scroll position (incremental search — same
        # UX as vim's ``/``). Backspace pops a char. Enter
        # commits, Esc cancels.
        @overlay_kb.add("enter", filter=searching)
        def _search_commit(event: KeyPressEvent) -> None:
            search_active[0] = False

        @overlay_kb.add("escape", filter=searching)
        def _search_cancel(event: KeyPressEvent) -> None:
            search_active[0] = False
            search_query[0] = ""
            scroll_offset[0] = search_origin[0]
            cursor_anchor[0] = "top"

        # Esc is a three-stage "back off" key. Outside search,
        # a pending search_query means the user committed a
        # query and the pane is showing highlights — Esc clears
        # the query (drops the highlight) and keeps the overlay
        # open. Only when there's no active search AND no
        # committed query does Esc close the overlay. Matches
        # the vim/less pattern where Esc after ``/foo<Enter>``
        # is "clear this search", not "exit the pager".
        @overlay_kb.add("escape", filter=not_searching)
        def _escape_close(event: KeyPressEvent) -> None:
            if search_query[0]:
                search_query[0] = ""
                return
            event.app.exit()

        @overlay_kb.add("backspace", filter=searching)
        def _search_backspace(event: KeyPressEvent) -> None:
            if search_query[0]:
                search_query[0] = search_query[0][:-1]
            hits = _find_matches(search_query[0])
            if hits:
                scroll_offset[0] = hits[0]
                cursor_anchor[0] = "top"
            elif not search_query[0]:
                scroll_offset[0] = search_origin[0]
                cursor_anchor[0] = "top"

        @overlay_kb.add("<any>", filter=searching)
        def _search_char(event: KeyPressEvent) -> None:
            # prompt-toolkit passes the keystroke via
            # ``event.data``; for printable keys this is the
            # character itself. For non-printable keys
            # (function keys, modifiers) ``data`` is empty or
            # an escape sequence — skip those so arrow keys
            # etc. don't corrupt the query.
            ch = event.data
            if not ch or len(ch) != 1 or not ch.isprintable():
                return
            search_query[0] += ch
            hits = _find_matches(search_query[0])
            if hits:
                scroll_offset[0] = hits[0]
                cursor_anchor[0] = "top"

        if has_sidebar:

            @overlay_kb.add("tab", filter=not_searching)
            @overlay_kb.add("right", filter=not_searching)
            def _tab(event: KeyPressEvent) -> None:
                selected_index[0] = (selected_index[0] + 1) % len(targets)
                _ensure_selection_visible()
                # Invalidate immediately so the sidebar marker
                # moves on the next tick even while the async
                # rebuild is still in flight; _rebuild_content
                # calls invalidate() again when the fresh content
                # lands so the right pane repaints too.
                event.app.invalidate()
                event.app.create_background_task(_rebuild_content())

            @overlay_kb.add("s-tab", filter=not_searching)
            @overlay_kb.add("left", filter=not_searching)
            def _stab(event: KeyPressEvent) -> None:
                selected_index[0] = (selected_index[0] - 1) % len(targets)
                _ensure_selection_visible()
                event.app.invalidate()
                event.app.create_background_task(_rebuild_content())

        app: Application[None] = Application(
            layout=Layout(HSplit(layout_children)),
            key_bindings=overlay_kb,
            full_screen=True,
            mouse_support=False,
        )
        # Publish the app so _rebuild_content (which was defined
        # before the app existed so it could run once at startup)
        # can invalidate() after async fetches complete.
        app_holder[0] = app
        # ``patch_stdout`` (from the outer ``run`` context) uses
        # prompt-toolkit's stdout proxy; it cooperates with a
        # nested Application. Run to completion — returns when
        # one of the close keys calls ``event.app.exit()``.
        try:
            await app.run_async()
        finally:
            # Stop the periodic refresh no matter how the overlay
            # closed — clean exit (Esc), Ctrl+C exception, builder
            # error. Without this the loop leaks a task that keeps
            # polling ``list_items`` after the REPL moves on.
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task

    async def _render_overlay_content(
        self,
        overlay: Overlay,
        *,
        target: OverlayTarget | None,
    ) -> str:
        """
        Call *overlay.builder* and convert its output to an
        ANSI-coloured string suitable for the overlay's scrollable
        content pane.

        Accepts either a plain string or any Rich renderable. Rich
        renderables are rasterised via a temporary :class:`Console`
        with truecolor ANSI output so colors, panels, and markdown
        styling survive to the final :class:`ANSI` decode inside
        :meth:`_show_overlay`.

        Failures in the builder surface as a one-line error inside
        the overlay rather than propagating; the main prompt stays
        usable even if the overlay code has a bug.

        The console width is reduced by the sidebar width + 1 (for
        the separator column) when the overlay has a sidebar, so
        wrap-heavy renderables (tables, panels) fit the remaining
        content pane instead of bleeding under the sidebar.

        :param overlay: The registered overlay.
        :param target: Currently-selected sidebar target, or
            ``None`` when the overlay has no sidebar. Passed
            through to ``builder`` so it can render the right
            data for the selection.
        :returns: ANSI-coloured text ready to be line-split and
            windowed by the caller.
        """
        try:
            result = await overlay.builder(target)
        except Exception as exc:
            _log.exception("Overlay builder failed")
            return f"Overlay builder raised {type(exc).__name__}: {exc}"
        if isinstance(result, str):
            return result
        buf = io.StringIO()
        pane_width = _term_width()
        if overlay.targets_builder is not None:
            pane_width = max(20, pane_width - overlay.sidebar_width - 1)
        temp = Console(
            file=buf,
            force_terminal=True,
            width=pane_width,
            highlight=False,
            color_system="truecolor",
            theme=self.theme.rich_theme,
        )
        temp.print(result)
        return buf.getvalue()

    def output(self, item: FormattedItem | None) -> None:
        """Display a formatted item above the pinned prompt.

        - ``StreamingText``: printed with ``end=""`` for live streaming.
        - ``StreamReplace``: atomic clear-streamed-region + render
          (delegated to :meth:`replace_streamed_text`). Used by the
          formatter for per-paragraph Markdown re-rendering — the
          combined ANSI write avoids the "blank then re-render"
          flicker on plain prose where the rendered output looks
          nearly identical to the streamed raw text.
        - Rich renderables: rendered to ANSI via a temp console, printed.
        - ``None``: ignored.
        """
        if item is None:
            return
        if isinstance(item, StreamLive):
            self._replace_live_region(item.renderable, commit=False)
            return
        if isinstance(item, StreamReplace):
            self._replace_live_region(item.renderable, commit=True)
            return
        if isinstance(item, StreamingText):
            self._text_buffer += item.text
            # Flush complete lines (LLM-produced newlines).
            while "\n" in self._text_buffer:
                line, self._text_buffer = self._text_buffer.split("\n", 1)
                self._print_text_line(line)
            # Flush when buffer fills a terminal line. Each flushed
            # line is full-width with consistent indent — no jagged
            # short lines, no terminal word-wrap without indent.
            available = max(20, _term_width() - _display_width(self.text_indent))
            while _display_width(self._text_buffer) >= available:
                wrap_at = self._text_buffer.rfind(" ", 0, available)
                if wrap_at <= 0:
                    wrap_at = available
                line = self._text_buffer[:wrap_at]
                self._text_buffer = self._text_buffer[wrap_at:].lstrip()
                # Gated by the same viewport-ceiling rule as
                # ``_print_text_line`` — see ``_should_stream_more``
                # for why printing past the ceiling causes the
                # scrollback-duplicate-render bug.
                if not self._should_stream_more(1):
                    continue
                print(linkify_ansi(f"{self.text_indent}{line}"), flush=True)
                self._streamed_line_count += 1
            self._last_was_streaming = True
            return
        # Flush any remaining streaming text buffer (partial line).
        if self._text_buffer:
            buf = self._text_buffer
            self._text_buffer = ""
            if buf.strip():
                print(linkify_ansi(f"{self.text_indent}{buf}"), flush=True)
                self._streamed_line_count += 1
            else:
                print(flush=True)
                self._streamed_line_count += 1
        if self._last_was_streaming:
            self._last_was_streaming = False
        # Non-streaming output — reset streamed and live line counters.
        # (clear_streamed_text must be called before this if needed.)
        self._streamed_line_count = 0
        self._live_line_count = 0
        # Render Rich content to ANSI string, print through proxy.
        buf = io.StringIO()
        temp = Console(
            file=buf,
            force_terminal=True,
            width=_term_width(),
            highlight=False,
            theme=self.theme.rich_theme,
        )
        temp.print(item)
        print(linkify_ansi(buf.getvalue()), end="", flush=True)

    def _print_text_line(self, text: str) -> None:
        """Print a line of streaming text, wrapped and indented.

        Gated by :meth:`_should_stream_more`: once the cumulative
        streamed line count would exceed what the cursor-up + erase
        in ``replace_streamed_text`` can later reach, this method
        becomes a silent no-op. The line content is NOT lost — the
        formatter holds the full paragraph in its own buffer and
        will pass it through ``StreamReplace`` at end-of-paragraph
        / end-of-response. Skipping the print here is what guarantees
        ``replace_streamed_text``'s cursor-up reaches every printed
        line — preventing the scrollback-duplicate render.
        """
        if not text.strip():
            if not self._should_stream_more(1):
                return
            print(flush=True)
            self._streamed_line_count += 1
            return
        width = _term_width()
        indent = self.text_indent
        available = max(20, width - _display_width(indent))
        wrapped = textwrap.fill(
            text,
            width=available,
            initial_indent=indent,
            subsequent_indent=indent,
        )
        wrapped_line_count = wrapped.count("\n") + 1
        if not self._should_stream_more(wrapped_line_count):
            return
        self._streamed_line_count += wrapped_line_count
        print(linkify_ansi(wrapped), flush=True)

    def _should_stream_more(self, pending_lines: int = 1) -> bool:
        """
        Whether the streaming path may print another line.

        Returns ``False`` when the cumulative streamed line count
        has reached what cursor-up + erase can reach inside the
        viewport. Beyond this threshold, additional streamed lines
        would scroll into the terminal's scrollback buffer, where
        ``replace_streamed_text``'s cursor-up sequence cannot reach
        them — the original cause of the 2026-04-28 duplicate-render
        bug. By refusing to print past the threshold, we guarantee
        every streamed line is still in-viewport at replace time, so
        the markdown render reliably supersedes the streamed text
        with no leftovers.

        Practical effect for users: short responses stream live as
        before. Long responses pause streaming once they fill the
        viewport (the rest of the response renders only when the
        formatter emits the markdown ``StreamReplace``). The trade
        is partial live-streaming feedback for guaranteed
        markdown-rendered final output. The formatter's
        ``_paragraph_buffer`` holds the full text — no content is
        lost from the gating.

        :param pending_lines: Number of terminal rows the pending
            print would add.
        :returns: ``True`` if there's still room in the viewport for
            the pending streamed rows; ``False`` once the line count
            would exceed the ceiling.
        """
        ceiling = max(1, _term_height() - _BOTTOM_RESERVED_ROWS)
        return self._streamed_line_count + pending_lines <= ceiling

    def clear_streamed_text(self) -> None:
        """Clear the previously streamed text lines using ANSI escapes.

        Call this before outputting a re-render (e.g., markdown) to
        avoid showing duplicate content.

        Also discards any unflushed partial content in
        ``_text_buffer``. The streaming path holds back the tail of
        the message (the part after the last newline / wrap) until a
        terminating newline arrives or a non-streaming item triggers
        a flush. If we cleared the printed lines but left the buffer,
        the very next ``output()`` would flush that partial tail
        (printing it raw) immediately before rendering the Markdown
        panel — exactly the "raw text shows up alongside the rendered
        version" duplication this method exists to prevent. The
        Markdown panel rendered by the caller already contains the
        full text, so dropping the buffer is safe.

        Prefer :meth:`replace_streamed_text` when you have the
        replacement renderable in hand: it issues the clear and the
        render in a single ANSI write so the terminal repaints once
        (no blank-then-redraw flicker on plain prose). This method is
        kept for callers that need to clear without an immediate
        replacement (e.g. cancel paths).
        """
        total = self._streamed_line_count + self._live_line_count
        if total > 0:
            # Move cursor up and clear each line.
            for _ in range(total):
                print("\033[A\033[2K", end="", flush=True)
            self._streamed_line_count = 0
            self._live_line_count = 0
        self._text_buffer = ""
        self._last_was_streaming = False

    def replace_streamed_text(self, renderable: _RichRenderable) -> None:
        """
        Atomically erase the streamed-text region and render in its place.

        Backward-compatible wrapper around :meth:`_replace_live_region`
        with ``commit=True``. Kept for external callers (cancel paths,
        etc.) that hold a replacement renderable and need the
        clear+render+state-reset sequence in one call.

        :param renderable: The Rich renderable to display in place of
            the cleared lines, e.g.
            ``Padding(Markdown(text), (0, 1, 0, 3))``.
        """
        self._replace_live_region(renderable, commit=True)

    def _replace_live_region(
        self,
        renderable: _RichRenderable,
        *,
        commit: bool,
    ) -> None:
        """
        Clear the live + streamed regions and render ``renderable``.

        Erases ``_live_line_count + _streamed_line_count`` lines via
        cursor-up + erase, renders ``renderable`` to ANSI, writes
        everything as one atomic ``sys.stdout.write``.

        For non-commit (``StreamLive``) renders, the output is capped
        to the viewport ceiling — the same limit
        :meth:`_should_stream_more` enforces for ``StreamingText``.
        Lines that scroll into the terminal's scrollback buffer can't
        be reached by cursor-up, so an uncapped live region would
        leave stale content in scrollback on every re-render. The cap
        keeps only the tail (most-recent lines) so the user sees
        continuous progress at the bottom. Committed content
        (``StreamReplace``) is permanent and never needs clearing, so
        no cap is applied.

        :param renderable: The Rich renderable to display.
        :param commit: If ``True``, the content is committed
            permanently — ``_live_line_count`` and
            ``_streamed_line_count`` reset to 0 and
            ``_text_buffer`` / ``_last_was_streaming`` clear. If
            ``False``, the rendered content becomes the new live
            region — ``_live_line_count`` tracks its rendered
            height for subsequent replacement.
        """
        total_clear = self._live_line_count + self._streamed_line_count
        parts: list[str] = []
        if total_clear > 0:
            parts.append("\033[A\033[2K" * total_clear)
        ansi_buf = io.StringIO()
        temp = Console(
            file=ansi_buf,
            force_terminal=True,
            width=_term_width(),
            highlight=False,
            theme=self.theme.rich_theme,
        )
        temp.print(renderable)
        rendered = linkify_ansi(ansi_buf.getvalue())

        if commit:
            # Committed content is permanent — no viewport cap.
            parts.append(rendered)
            sys.stdout.write("".join(parts))
            sys.stdout.flush()
            self._live_line_count = 0
            self._streamed_line_count = 0
            self._text_buffer = ""
            self._last_was_streaming = False
        else:
            # Cap live region to viewport ceiling so cursor-up can
            # always reach every line on the next clear. Keep only
            # the tail (most-recent lines) for continuous progress.
            ceiling = max(1, _term_height() - _BOTTOM_RESERVED_ROWS)
            rendered_lines = rendered.count("\n")
            if rendered_lines > ceiling:
                # Split on newlines, keep the last `ceiling` lines
                # (plus trailing content after the last newline).
                lines = rendered.split("\n")
                rendered = "\n".join(lines[-(ceiling + 1) :])
                rendered_lines = ceiling
            parts.append(rendered)
            sys.stdout.write("".join(parts))
            sys.stdout.flush()
            self._live_line_count = rendered_lines
            self._streamed_line_count = 0
            self._text_buffer = ""
            self._last_was_streaming = False

    @property
    def is_busy(self) -> bool:
        """True if any handler task is running."""
        return any(not t.done() for t in self._tasks)

    def _is_working(self) -> bool:
        """
        Return whether the prompt should show the active work badge.

        :returns: ``True`` while a streamed turn timer is active or an
            input handler task is still running.
        """
        return self._stream_start is not None or self.is_busy

    def _invalidate_prompt(self) -> None:
        """
        Request a prompt-toolkit repaint when the host state changes.

        :returns: ``None``. The method is best-effort because tests
            and pre-run host instances may not have an active
            prompt-toolkit application yet.
        """
        app = self._prompt.app
        if app is not None:
            app.invalidate()

    def start_timer(self) -> None:
        """Start the elapsed timer shown in the toolbar.

        Idempotent: if the timer is already running, this is a
        no-op. Two call sites fire start_timer — ``on_input()``
        (immediate, before the POST) and ``_render_session_event``
        (when the SSE ``session.status=running`` arrives). Without
        the guard the second call resets the counter to zero,
        producing a visible "double stream" glitch in the toolbar.
        """
        if self._stream_start is not None:
            return
        import time as _time

        self._stream_start = _time.monotonic()
        self._invalidate_prompt()

    def stop_timer(self) -> None:
        """Stop the elapsed timer."""
        self._stream_start = None
        self._invalidate_prompt()

    # ------------------------------------------------------------------
    # Sub-agent tree — live status for the main-interface badge + ↓ menu.
    # ------------------------------------------------------------------
    @staticmethod
    def _monotonic() -> float:
        import time as _time

        return _time.monotonic()

    def upsert_subagent(
        self,
        session_id: str,
        *,
        parent_id: str | None = None,
        child: dict[str, Any] | None = None,
    ) -> None:
        """Create or merge one sub-agent node from a partial payload.

        ``child`` is a partial ``ChildSessionSummary`` dict (a status delta
        carries only what changed), so we merge **present keys only** and
        never blank a value we already know (e.g. a delta must not drop the
        label or ``parent_id``).
        """
        child = child or {}
        node = self._subagents.get(session_id)
        if node is None:
            node = _SubagentNode(session_id=session_id, started_at=self._monotonic())
            self._subagents[session_id] = node
        if parent_id is not None and parent_id != session_id:
            node.parent_id = parent_id
        agent = child.get("agent_name") or child.get("tool")
        if agent is not None:
            node.agent = agent
        if child.get("title") is not None:
            node.title = child.get("title")
        # Adopt only a REAL (non-null) task status. The live SSE
        # (``session.child_session.updated``) delivers a real
        # ``current_task_status``, but the REST poll (used for deeper tree
        # levels) hardcodes it to ``None`` — letting that null overwrite the
        # status would erase the SSE-delivered terminal state and resurrect a
        # finished sub-agent on every 2 s poll.
        incoming_status = child.get("current_task_status")
        if incoming_status is not None:
            node.status = incoming_status
        if "busy" in child:
            node.busy = bool(child.get("busy"))
        if "last_task_error" in child:
            node.last_task_error = child.get("last_task_error") is not None
        if "pending_elicitations_count" in child:
            node.pending_elicitations = int(child.get("pending_elicitations_count") or 0)
        # Closed-ness is sticky: once a child's session is closed to input it
        # never reopens, and a partial delta that omits labels must not flip it
        # back to open. Derive it from the same labels/title the server uses so
        # the selector knows the child is view-only (a ``message`` to a closed
        # session 409s). Imported lazily to keep this UI SDK importable without
        # the ``omnigent`` server package on the path.
        if not node.closed:
            from omnigent.session_lifecycle import is_session_closed

            if is_session_closed(child.get("labels"), child.get("title")):
                node.closed = True
        # ``done_at`` stamps a TERMINAL task OUTCOME (completed / failed /
        # cancelled, or a durable failure detail) — NOT a merely-not-busy loop.
        # Web parity: a child whose loop is idle between turns (``busy=False``
        # with no terminal status) is "warm/idle", still chattable, and must
        # not be mislabeled ``Done``. ``done_at`` debounces the running badge;
        # a non-terminal update clears it (a reused handle / resumed child).
        terminal = node.status in TERMINAL_TASK_STATUSES or node.last_task_error
        if terminal:
            if node.done_at is None:
                node.done_at = self._monotonic()
        else:
            node.done_at = None
        self._invalidate_prompt()

    def seed_subagent_tree(
        self, root_id: str, nodes: list[dict[str, Any]], *, generation: int | None = None
    ) -> None:
        """Replace the tree from a recursively-fetched snapshot.

        Each entry in ``nodes`` is a ``child_sessions`` row augmented with a
        ``parent_id`` (the session it was queried under).

        Retention (web parity): a child the server STILL lists stays in the
        selector regardless of whether it has finished — finished sub-agents
        remain visible (dimmed) so the user can revisit / chat with them. A
        node is dropped only when it is BOTH absent from the snapshot (the
        server stopped listing it) AND terminal (its result was delivered);
        an unsent (still-active) node that's momentarily absent — a poll racing
        a just-spawned child — is kept.

        ``generation`` guards against a clear-during-poll race: pass the value
        captured from :attr:`subagent_generation` before the fetch started, and
        a snapshot whose generation no longer matches (the tree was cleared and
        re-rooted while this fetch was in flight) is dropped wholesale rather
        than resurrecting cleared nodes onto a new root.
        """
        if generation is not None and generation != self._subagent_generation:
            return
        self._subagent_root = root_id
        seen: set[str] = {root_id}
        for row in nodes:
            sid = row.get("id")
            if not isinstance(sid, str):
                continue
            seen.add(sid)
            parent = row.get("parent_id")
            self.upsert_subagent(
                sid,
                parent_id=parent if isinstance(parent, str) else None,
                child=row,
            )
        for sid in list(self._subagents):
            if sid in seen:
                continue
            node = self._subagents[sid]
            # Unsent (still-active) sub-agents stay searchable in the tree
            # until they deliver their result, regardless of snapshot gaps.
            if node.done_at is None:
                continue
            # Terminal AND gone from the server snapshot → truly removed. Drop
            # it (no linger wait): retained nodes are the ones the server keeps
            # listing, not ones it has forgotten.
            del self._subagents[sid]
        self._invalidate_prompt()

    def _subagent_visible(self, node: _SubagentNode, now: float) -> bool:
        """Whether a node shows in the ↓ selector tree.

        Always ``True`` — finished / warm sub-agents are retained in the
        selector indefinitely (web parity), so the user can revisit or chat
        with a child after it settles. Nodes leave the selector only via
        snapshot reconciliation (the server stops listing them — see
        :meth:`seed_subagent_tree`) or :meth:`clear_subagents`. The
        ``now`` parameter is unused but kept so callers don't special-case it.
        """
        return True

    def _subagent_active(self, node: _SubagentNode, now: float) -> bool:
        """Whether a node counts toward the "N agents running" badge / spinner.

        This is the running-ness signal (NOT visibility — see
        :meth:`_subagent_visible`). A terminal node stays counted for the
        linger window so a between-turns ``completed`` blip that then resumes
        can't flicker the badge off. A non-terminal node counts while it is
        actually doing work: busy, launching, awaiting input, or mid-task with
        a non-terminal ``current_task_status`` (``in_progress`` / ``queued`` —
        a busy-flag gap between turns). A warm-idle child (loop idle, no
        in-progress task, no pending input) is retained but NOT counted as
        running — matching the web ``Idle`` state.
        """
        if node.done_at is not None:
            return (now - node.done_at) < _SUBAGENT_LINGER_SECONDS
        # The non-terminal "is this child working" decision is the canonical,
        # un-debounced predicate shared with SDK rollups (subtree_busy /
        # tree_busy) and the web SubagentsPanel; only the linger above is
        # CLI-specific. Keeping one definition stops the CLI and SDK drifting.
        return child_session_busy(
            busy=node.busy,
            current_task_status=node.status,
            pending_elicitations_count=node.pending_elicitations,
        )

    def subagent_tree(self) -> list[tuple[_SubagentNode, int]]:
        """Return the visible sub-agent nodes in pre-order with their depth.

        Depth is 1 for direct children of the root ("main") session, 2 for
        grandchildren, etc. Finished-but-lingering nodes are included; ones
        past the linger window are hidden (but kept — see
        :meth:`_subagent_visible`). Cycle- and orphan-safe.
        """
        now = self._monotonic()
        visible = sorted(
            (n for n in self._subagents.values() if self._subagent_visible(n, now)),
            key=lambda n: n.started_at,
        )
        children_of: dict[str | None, list[_SubagentNode]] = {}
        for node in visible:  # pre-sorted, so child lists stay started_at-ordered
            children_of.setdefault(node.parent_id, []).append(node)
        out: list[tuple[_SubagentNode, int]] = []
        seen: set[str] = set()

        def _walk(parent_key: str | None, depth: int) -> None:
            for node in children_of.get(parent_key, []):
                if node.session_id in seen:  # cycle guard
                    continue
                seen.add(node.session_id)
                out.append((node, depth))
                _walk(node.session_id, depth + 1)

        _walk(self._subagent_root, 1)
        # Surface any visible node not reachable from the root — an orphan
        # whose parent isn't tracked (or was hidden), or a node stranded in a
        # parent cycle — at depth 1 so no running sub-agent is silently lost.
        for node in visible:
            if node.session_id in seen:
                continue
            seen.add(node.session_id)
            out.append((node, 1))
            _walk(node.session_id, 2)
        return out

    def active_subagent_count(self) -> int:
        """Number of sub-agents currently shown as running, across the tree.

        Counts a sub-agent from launch until it has stayed terminal (delivered
        its final reply) for the whole linger window. This is deliberately NOT
        the live ``busy`` flag: ``busy`` (server ``status in (running,
        waiting)``) drops to false in the gaps between a child's turns / async
        waits and comes back, which made the ``N agents running`` badge
        flicker on/off between status heartbeats. Keying on terminal-vs-not
        plus the linger window also debounces a brief terminal blip (an
        ``idle`` between turns the server maps to ``completed``) that then
        resumes, so the badge only settles once the child is truly finished.
        """
        now = self._monotonic()
        return sum(1 for n in self._subagents.values() if self._subagent_active(n, now))

    def has_active_subagents(self) -> bool:
        """True while any sub-agent is still running (drives the badge /
        spinner). Keys on running-ness (debounced) rather than the flickering
        raw ``busy`` flag — see :meth:`active_subagent_count`. NOTE: this is
        running-ness, NOT selector availability; use :meth:`has_any_subagents`
        to gate the ↓ menu so finished children remain reachable."""
        return self.active_subagent_count() > 0

    def has_any_subagents(self) -> bool:
        """True when ANY sub-agent node exists — active OR finished. Gates the
        ↓ selector + its toolbar hint, which stay available after children
        settle so the user can revisit / chat with finished sub-agents (web
        parity with the Agents rail)."""
        return bool(self._subagents)

    @property
    def subagent_generation(self) -> int:
        """Monotonic counter bumped on every :meth:`clear_subagents`. A
        background tree fetch captures this before it starts and passes it back
        to :meth:`seed_subagent_tree`; a stale seed (the tree was cleared while
        the fetch ran) no-ops instead of resurrecting cleared nodes."""
        return self._subagent_generation

    def clear_subagents(self) -> None:
        """Drop the whole tree — called when the REPL changes which top-level
        session it owns (``/switch``, ``/clear``, ``/new``) so the prior
        session's sub-agents don't linger under the new one. Bumps
        :attr:`subagent_generation` so an in-flight tree fetch can't resurrect
        the cleared nodes (race guard)."""
        self._subagents.clear()
        self._subagent_root = None
        self._subagent_generation += 1
        self._invalidate_prompt()

    def is_subagent_chattable(self, session_id: str) -> bool:
        """Whether the user can send messages to *session_id* from the selector.

        A child is chattable while its session is open (not closed): finished
        and warm children stay chattable so the user can follow up (web-UI
        co-drive parity). A closed child is view-only — a ``message`` to it
        returns 409 CONFLICT — and unknown ids (e.g. the ``main`` row, a stale
        selection) are not chattable.
        """
        node = self._subagents.get(session_id)
        return node is not None and not node.closed

    def _is_inside_subagent(self) -> bool:
        """True when the active session is a sub-agent below the tree root —
        i.e. the user dived in and Left-arrow can return to the top."""
        getter = self.active_session_id_getter
        if getter is None or self._subagent_root is None:
            return False
        active = getter()
        return active is not None and active != self._subagent_root

    def _subagent_status_label(self, node: _SubagentNode) -> str:
        """Collapse a node to a short status word, mirroring the web UI
        ``childStatus`` precedence (``ap-web SubagentsPanel.tsx``):

        1. ``pending_elicitations > 0`` → ``Needs response`` (outranks busy: a
           sub-agent parked on an approval needs the user, not a generic badge).
        2. ``current_task_status == "launching"`` → ``Launching``.
        3. ``busy`` → ``Working``.
        4. a durable failure detail → ``Failed``.
        5. ``current_task_status`` is ``failed`` / ``cancelled`` / ``completed``
           → ``Failed`` / ``Cancelled`` / ``Done``; any other non-null status is
           shown raw.
        6. otherwise (loop idle, no task) → ``Idle`` (warm — still chattable;
           deliberately NOT ``Done``, so a between-turns idle isn't mistaken
           for a delivered result).
        """
        if node.pending_elicitations > 0:
            return "Needs response"
        if node.status == "launching":
            return "Launching"
        if node.busy:
            return "Working"
        if node.last_task_error or node.status == "failed":
            return "Failed"
        if node.status == "cancelled":
            return "Cancelled"
        if node.status == "completed":
            return "Done"
        if node.status:
            return node.status
        return "Idle"

    def subagent_menu_rows(self) -> list[tuple[str, str]]:
        """Return ``(session_id, label)`` rows for the ``↓`` sub-agents menu.

        Pre-order, depth-indented so the parent→child hierarchy is visible.
        Each label carries the indent, a ✔/✗ glyph for finished nodes, the
        display name, and the status word (mirroring the web Agents rail).
        """
        rows: list[tuple[str, str]] = []
        for node, depth in self.subagent_tree():
            indent = "  " * max(0, depth - 1)
            status = self._subagent_status_label(node)
            if status == "Done":
                glyph = "✔ "
            elif status in ("Failed", "Cancelled"):
                glyph = "✗ "
            else:
                glyph = ""
            name = node.title or node.agent or node.session_id[:12]
            label = f"{indent}{glyph}{name}  · {status}"
            rows.append((node.session_id, label))
        return rows

    # ------------------------------------------------------------------
    # Inline ``↓`` sub-agents menu — a navigable list below the toolbar.
    # ------------------------------------------------------------------
    def _subagent_menu_display_rows(self) -> list[tuple[str, str]]:
        """Rows for the inline menu: a leading ``main`` entry (when the root
        session is known) followed by the depth-indented sub-agent tree."""
        rows: list[tuple[str, str]] = []
        if self._subagent_root is not None:
            rows.append((self._subagent_root, "main"))
        rows.extend(self.subagent_menu_rows())
        return rows

    def _active_subagent_menu_index(self) -> int:
        """Index of the display row for the session currently being viewed.

        When the user has dived into a sub-agent, opening the menu should
        pre-select that sub-agent's row (so ``↓`` then ``Enter`` is a no-op and
        the highlight reflects where you actually are), not always reset to the
        ``main`` row. Falls back to 0 (``main``) when the active session is
        unknown or not present in the menu.
        """
        getter = self.active_session_id_getter
        active = getter() if getter is not None else None
        if active is None:
            return 0
        for i, (sid, _label) in enumerate(self._subagent_menu_display_rows()):
            if sid == active:
                return i
        return 0

    def _open_subagent_menu(self) -> None:
        """Open the inline menu (no-op when the tree is empty). Opens whenever
        ANY node exists — active OR finished — so finished children stay
        reachable for revisit / chat (web parity). The selection starts on the
        row for the session you're currently viewing (the dived-into sub-agent,
        or ``main`` at the top level)."""
        if not self.has_any_subagents():
            return
        self._subagent_menu_open = True
        self._subagent_menu_index = self._active_subagent_menu_index()
        self._subagent_menu_scroll = 0
        self._invalidate_prompt()

    def _close_subagent_menu(self) -> None:
        """Close the inline menu."""
        if self._subagent_menu_open:
            self._subagent_menu_open = False
            self._invalidate_prompt()

    def _move_subagent_selection(self, delta: int) -> None:
        """Move the menu selection by *delta*, wrapping around the list."""
        rows = self._subagent_menu_display_rows()
        if not rows:
            self._close_subagent_menu()
            return
        self._subagent_menu_index = (self._subagent_menu_index + delta) % len(rows)
        self._invalidate_prompt()

    def _selected_subagent_id(self) -> str | None:
        """Session id of the currently-highlighted menu row, or ``None``."""
        rows = self._subagent_menu_display_rows()
        if not rows:
            return None
        idx = max(0, min(self._subagent_menu_index, len(rows) - 1))
        return rows[idx][0]

    def _render_subagent_menu_fragments(self) -> list[tuple[str, str]]:
        """Build the windowed menu rows shown below the toolbar.

        Returns prompt-toolkit ``(style, text)`` fragments, one row each
        ending in a newline. At most :data:`_SUBAGENT_MENU_MAX_ROWS` rows
        show at once; longer trees scroll within that window as the
        selection moves, so the menu never grows more than that many lines.
        Empty when the menu is closed; auto-closes when the tree drained
        while the menu was open.
        """
        if not self._subagent_menu_open:
            return []
        rows = self._subagent_menu_display_rows()
        if not rows:
            self._subagent_menu_open = False
            return []
        idx = max(0, min(self._subagent_menu_index, len(rows) - 1))
        self._subagent_menu_index = idx
        window = _SUBAGENT_MENU_MAX_ROWS
        scroll = self._subagent_menu_scroll
        if idx < scroll:
            scroll = idx
        elif idx >= scroll + window:
            scroll = idx - window + 1
        scroll = max(0, min(scroll, max(0, len(rows) - window)))
        self._subagent_menu_scroll = scroll
        frags: list[tuple[str, str]] = []
        for offset, (sid, label) in enumerate(rows[scroll : scroll + window]):
            selected = (scroll + offset) == idx
            marker = "▸" if selected else " "
            if selected:
                style = "class:prompt-marker"
            elif self._subagent_row_settled(sid):
                # Finished / warm rows are kept but de-emphasized (web parity).
                style = "class:subagent-settled"
            else:
                style = "class:model-name"
            frags.append((style, f" {marker} {label}\n"))
        return frags

    def _subagent_row_settled(self, session_id: str) -> bool:
        """Whether a selector row is in a settled (dimmed) state — a finished
        (``Done``) or warm-idle (``Idle``) child, mirroring the web
        ``SETTLED_STATE``. The ``main`` row and unknown ids are never settled.
        """
        node = self._subagents.get(session_id)
        if node is None:
            return False
        return self._subagent_status_label(node) in ("Done", "Idle")

    def build_subagent_menu(self) -> FormattedText:
        """Render the inline menu (rows + key hint) for the Window below the
        toolbar. Empty when the menu is closed."""
        rows = self._render_subagent_menu_fragments()
        if not rows:
            return FormattedText([])
        return FormattedText([*rows, ("class:model-name", " ↑↓ select · ⏎ open · esc close")])

    def _subagent_menu_line_count(self) -> int:
        """Number of terminal lines the inline menu occupies (rows + hint),
        or 0 when closed — drives the menu Window's height."""
        rows = self._render_subagent_menu_fragments()
        return len(rows) + 1 if rows else 0

    def cancel(self) -> None:
        """Cancel all running handler tasks."""
        self.stop_timer()
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def request_exit(self) -> None:
        """Exit the prompt loop from a background handler."""
        self._exit_requested = True
        app = self._prompt.app
        if app is not None:
            app.exit(exception=EOFError())

    async def _read_input(self) -> str | None:
        # Pass ``build_prompt`` as a CALLABLE, not its result.
        # prompt-toolkit invokes the message callable on every
        # render tick; passing the already-materialised
        # ``FormattedText`` freezes the prompt at one frame so
        # the Braille spinner and any other time-varying content
        # appear static. The bottom_toolbar already goes through
        # this code path (see ``build_toolbar`` is bound, not
        # called) — now the top-of-input line does too.
        # ``multiline=True`` lets the buffer hold ``\n`` so power
        # users can paste / compose multi-line prompts. The
        # plain-Enter binding registered in __init__ overrides
        # prompt-toolkit's default "Enter inserts newline in
        # multiline mode" behavior to submit instead — newline
        # insertion goes through ``escape enter`` / ``Ctrl+J`` /
        # ``Shift+Enter`` (CSI-u) explicitly.
        return await self._prompt.prompt_async(
            self.build_prompt,
            multiline=True,
        )

    def _spinner_frame(self) -> str:
        """
        Return the Braille spinner frame for the current tick.

        Frames are indexed by ``monotonic_time * (1 / tick)`` so
        the animation is wall-clock-driven rather than
        invalidation-driven — every render reads the same frame
        for a given tick, so two consecutive render ticks within
        100 ms show the same frame and the animation doesn't
        jitter from redraws triggered by other sources.

        :returns: One glyph from :data:`_SPINNER_FRAMES`.
        """
        import time as _time

        idx = int(_time.monotonic() / _SPINNER_TICK_SECONDS) % len(_SPINNER_FRAMES)
        return _SPINNER_FRAMES[idx]

    def build_prompt(self) -> FormattedText:
        width = _term_width()
        bar = "─" * width
        parts: list[tuple[str, str]] = []
        # Auto-close the inline ↓ menu when its list has drained — it renders
        # in a Window below the toolbar (see the layout patch), so guard the
        # stale-open state here, the one render path that always runs.
        if self._subagent_menu_open and not self._subagent_menu_display_rows():
            self._subagent_menu_open = False
        # Keep this row present even when idle. prompt-toolkit's
        # prompt message can otherwise shrink from "working + bar"
        # to just "bar", leaving the prior "working" line orphaned
        # above the separator while the toolbar already says ready.
        if self._is_working():
            frame = self._spinner_frame()
            parts.append(("class:prompt-marker", f" {frame} "))
            parts.append(("class:model-name", "working\n"))
        else:
            parts.append(("", " \n"))
        parts.append(("class:bar", bar + "\n"))
        # Show pending attachments above the input.
        cwd = os.getcwd()
        for i, att in enumerate(self._pending_attachments):
            rel = os.path.relpath(att.path, cwd)
            if att.is_image:
                parts.append(("class:prompt-marker", f" [Image #{i + 1}] "))
                parts.append(("class:model-name", f"{rel}\n"))
            else:
                parts.append(("class:prompt-marker", " 📎 "))
                parts.append(("class:model-name", f"{rel}\n"))
        parts.append(("class:prompt-marker", f" {self._marker} "))
        return FormattedText(parts)

    def build_toolbar(self) -> FormattedText:
        """
        Build the bottom status toolbar.

        Layout: ``{model · state-badge} … hints``. The state
        badge reads ``state: running ⠹`` (with animated Braille
        spinner) while a handler task is running, and
        ``state: sleeping`` while idle — matching the format
        omnigent' main REPL shows at the bottom-right. A
        running stream also shows elapsed seconds in the model
        segment. The hints stay on the right.

        If a Ctrl+C exit confirmation is armed (first press of
        two-press exit), the hints segment is replaced with a
        muted gray ``Press Ctrl+C again to exit`` prompt until
        either the window expires or the user presses Ctrl+C
        again. Prompt-toolkit re-renders the toolbar on every
        spinner tick, so an expired confirmation is garbage-
        collected on the next tick without needing an extra
        timer task.
        """
        import time as _time

        n_sub = self.active_subagent_count()
        if self._stream_start is not None:
            elapsed = _time.monotonic() - self._stream_start
            status = f"streaming… {elapsed:.0f}s"
            state_badge = f"state: running {self._spinner_frame()}"
        elif self.is_busy:
            status = "streaming…"
            state_badge = f"state: running {self._spinner_frame()}"
        elif n_sub > 0:
            # The top-level agent is idle but sub-agents are still working —
            # surface that instead of the misleading ``sleeping``.
            status = "ready"
            plural = "s" if n_sub != 1 else ""
            state_badge = f"state: {n_sub} agent{plural} running {self._spinner_frame()}"
        else:
            status = "ready"
            state_badge = "state: sleeping"
        # When ``model_name`` was not supplied, drop the label
        # + separator so the toolbar just reads ``status · hints``
        # instead of a floating ``None`` or a dangling ``·``.
        parts = f" {self._model} · {status} " if self._model else f" {status} "
        # Exit-confirmation window takes over the hint segment
        # when armed. The deadline is checked-then-cleared
        # here (not via a separate timer) so the toolbar drops
        # the hint as soon as the next tick renders past the
        # deadline, with no races against the binding.
        deadline = self._exit_confirm_deadline
        now = _time.monotonic()
        if deadline is not None and now >= deadline:
            self._exit_confirm_deadline = None
            deadline = None
        if deadline is not None:
            hints = " Press Ctrl+C again to exit "
        else:
            # Match the welcome-panel hint formatting:
            # entries joined with `` · `` and padded with one
            # leading + trailing space. The hint list comes
            # from the constructor so callers control which
            # bindings appear (e.g. ``run_repl`` passes the
            # same ``WELCOME_HINTS`` list it gives to
            # ``fmt.welcome``). The ``↓ agents`` hint is shown
            # only while sub-agents are active, advertising the
            # Down-arrow gesture that opens the sub-agents menu; the
            # ``← back`` hint shows while inside a sub-agent, advertising
            # Left-arrow to return to the top-level session.
            hint_items = list(self._toolbar_hints)
            if self.has_any_subagents():
                # Sit the ``↓ agents`` hint right after the ``/help`` entry so
                # it rides with the primary hints instead of trailing the row.
                # Falls back to the end when there's no ``/help`` (e.g. a host
                # constructed with a custom hint list).
                insert_at = next(
                    (i + 1 for i, h in enumerate(hint_items) if h.startswith("/help")),
                    len(hint_items),
                )
                hint_items.insert(insert_at, "↓ agents")
            if self._is_inside_subagent():
                hint_items.append("← back")
            hints = " " + " · ".join(hint_items) + " "
        # Pipeline debug counters (--debug-events). Appended between
        # hints and the state badge so they're visible but don't
        # displace the core toolbar elements. Empty string when
        # counters are not active.
        counter_segment = ""
        if self.pipeline_counters is not None:
            counter_segment = f" │ {self.pipeline_counters.toolbar_text()} "
        # Context-window ring indicator. Shows a single Unicode arc
        # character (○◔◑◕●) followed by a compact percentage, matching
        # the SVG ring in the web UI. Hidden until the first completed
        # response supplies both token counts.
        ring_segment = ""
        if self._tokens_used is not None and self._context_window:
            pct = min(self._tokens_used / self._context_window, 1.0)
            idx = min(int(pct * (len(_RING_CHARS) - 1)), len(_RING_CHARS) - 1)
            ring_char = _RING_CHARS[idx]
            ring_segment = f" {ring_char} {pct:.0%} "
        state_segment = f" {state_badge} "
        width = _term_width()
        bar_right = max(
            0,
            width
            - 2
            - len(parts)
            - len(hints)
            - len(counter_segment)
            - len(ring_segment)
            - len(state_segment),
        )
        segments: list[tuple[str, str]] = [
            ("class:bar", "──"),
            ("class:model-name", parts),
            ("class:bottom-toolbar.key", hints),
        ]
        if counter_segment:
            segments.append(("class:model-name", counter_segment))
        if ring_segment:
            segments.append(("class:model-name", ring_segment))
        segments.append(("class:bar", "─" * bar_right))
        segments.append(("class:model-name", state_segment))
        return FormattedText(segments)
