"""
Interactive stderr/stdin picker for ``--resume``.

A keyboard selector for picking a saved conversation: arrow keys
move the highlighted row, Enter resumes it, and q/Esc cancels.
Conversations come from Omnigent' conversation store.

Two entry points share one rendering loop:

- :func:`pick_conversation` — pure picker. Caller already has the
  conversation list (e.g. fetched via SDK or store).
- :func:`pick_conversation_from_sdk` — convenience that drives the
  SDK fetch + filter for the chat REPL path.

The picker writes to ``stderr`` rather than ``stdout`` so a parent
process piping the agent's stdout stream still gets clean output —
matching the legacy picker's stream choice. Reads come from
``stdin``. Interactive terminals use prompt-toolkit so key decoding,
resize handling, and redraw diffing are owned by the same TUI stack as
the REPL; non-TTY callers keep a simple line-buffered fallback for
scripted compatibility.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Protocol

from omnigent._terminal_picker_theme import (
    PICKER_ACCENT as _ACCENT,
)
from omnigent._terminal_picker_theme import (
    PICKER_MUTED as _MUTED,
)

# Wrapper label sentinel — single source of truth in
# ``omnigent._wrapper_labels``. Imported here so the picker module
# stays decoupled from the heavy ``claude_native`` import graph (tmux
# / websocket code) while still rendering the right badge.
from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
)

# Client-side persistent launch state. The picker reads this per row
# to render workspace metadata for native wrapper sessions. Decoupled
# from the heavy wrapper import graphs; these state modules have no
# tmux / websocket dependencies.
from omnigent.claude_native_state import read_launch_state as _read_claude_launch_state
from omnigent.codex_native_state import read_launch_state as _read_codex_launch_state
from omnigent.native_coding_agents import (
    CODEX_NATIVE_CODING_AGENT,
    native_coding_agent_for_wrapper_label,
)

# Page size for the paginated picker.
# Small enough that a 24-line terminal shows the whole page; big enough
# that a typical user finds their conversation in the first page.
_PAGE_SIZE = 10

_CANCEL_TOKEN = "cancel"

# Input tokens. Lowercased before comparison. Enter maps to
# :data:`_SELECT_TOKEN` in both TTY and line-buffered fallback paths
# so the highlighted row is the only resume action.
_QUIT_TOKENS: frozenset[str] = frozenset({"q", "quit", _CANCEL_TOKEN})
_NEXT_TOKENS: frozenset[str] = frozenset({"n", "next", ">"})
_PREV_TOKENS: frozenset[str] = frozenset({"p", "prev", "previous", "<"})
_UP_TOKENS: frozenset[str] = frozenset({"up"})
_DOWN_TOKENS: frozenset[str] = frozenset({"down"})
_SELECT_TOKEN = "select"

# Maximum characters of preview text shown per row. Wider would
# crowd out titles on standard 80-col terminals; narrower
# would truncate so aggressively the preview stops being useful.
_PREVIEW_DISPLAY_CHARS = 60

# Cap on how many conversations we pre-fetch previews for. Most
# users have far fewer than this; users with thousands fall back to
# blank previews on rows past the cap (still selectable, just no
# preview hint). Lazy per-page fetch would be
# stricter but adds an async-from-sync bridge complication; pre-
# fetching all is the simpler choice while limits stay sane.
_PREVIEW_PREFETCH_CAP = 100
_METADATA_SEPARATOR = " · "

# Role symbols shared with the rest of the REPL surface so the
# preview row reads as the same visual language as the chat.
# ``❯`` mirrors :meth:`omnigent_ui_sdk.RichBlockFormatter.\
# user_message`'s prefix; ``◆`` is the SDK formatter's default
# assistant glyph.
_USER_GLYPH = "❯"
_ASSISTANT_GLYPH = "◆"


@dataclass(frozen=True)
class _Preview:
    """
    The latest message preview from one conversation.

    :param role: ``"user"`` or ``"assistant"`` — drives the
        glyph the picker renders before the text. Other roles
        (``"system"``, ``"tool"``) shouldn't surface here in
        practice; treat them like the assistant for rendering.
    :param text: The latest message's plain-text content,
        already collapsed to a single line and truncated to
        :data:`_PREVIEW_DISPLAY_CHARS` with a trailing ``…``
        when the original was longer. The picker does not
        re-truncate; this is the final display string.
    """

    role: str
    text: str


@dataclass(frozen=True)
class _PageRowRender:
    """
    Rendered line data for one resume-picker list item.

    :param lines: Rich-renderable lines printed for one conversation.
    """

    lines: list[Any]


class _ConversationRow(Protocol):
    """
    Minimal shape the picker reads off each conversation row.

    Both the SDK's :class:`omnigent_client.types.Conversation`
    and the store's
    :class:`omnigent.entities.conversation.Conversation`
    satisfy this without inheriting from it — the Protocol exists
    purely to document what the picker depends on, so a future
    refactor that drops one of those fields breaks here loudly
    instead of confusing the renderer.

    :param id: Conversation identifier, e.g. ``"conv_abc123"``.
    :param title: Optional human-set title; ``None`` when the
        user hasn't named it yet.
    :param created_at: Creation time as seconds since epoch.
        Both row sources expose this as :class:`int`.
    :param labels: Session-scoped labels (read for the Runtime
        metadata badge — the ``omnigent.wrapper`` key identifies
        wrapper-style sessions like claude-native). Empty dict
        when the row has no labels; ``None`` is tolerated for
        callers (legacy fakes in tests) that do not surface
        labels on the row. :func:`_runtime_badge` treats both as
        "no wrapper" and falls through to ``[chat]``.
    """

    id: str
    title: str | None
    created_at: int
    labels: dict[str, str] | None


def _runtime_badge(row: _ConversationRow) -> str:
    """
    Compute the runtime metadata label string for one picker row.

    The badge surfaces which Omnigent wrapper owns the session so a
    cross-agent picker can be skimmed at a glance. For now we
    distinguish terminal-native wrappers; other agents render as
    ``[chat]`` because the picker
    drives the chat REPL when the row is selected.

    Returned as a literal string with brackets. Callers that hand it
    to Rich for rendering must use a ``Text`` node rather than Rich
    markup, otherwise Rich treats ``[claude]`` / ``[chat]`` as style
    tags and renders nothing.

    :param row: One conversation row from the SDK / store list.
    :returns: Native runtime badge, or ``"[chat]"`` otherwise.
        Reads ``row.labels`` defensively so legacy fakes (test rows
        without a ``labels`` attribute) fall through to the default
        without raising.
    """
    labels = getattr(row, "labels", None)
    if isinstance(labels, dict):
        native_agent = native_coding_agent_for_wrapper_label(
            labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        )
        if native_agent is not None:
            return f"[{native_agent.key}]"
    return "[chat]"


def pick_conversation(
    conversations: list[_ConversationRow],
    *,
    agent_name: str,
    previews: dict[str, _Preview | None] | None = None,
    show_runtime: bool = False,
    show_workspace: bool = False,
    out: IO[str] | None = None,
    in_: IO[str] | None = None,
) -> str | None:
    """
    Run the interactive picker over a pre-fetched conversation list.

    :param conversations: Conversations to choose from, already
        ordered (typically newest-first). Each item must expose
        ``id`` (str), ``title`` (str | None), and ``created_at``
        (int seconds since epoch). Both SDK ``Conversation`` and
        the store's ``Conversation`` entity satisfy this.
    :param agent_name: Agent label shown in the header,
        e.g. ``"resume_test"``. Pure cosmetic; doesn't filter.
    :param previews: Optional ``{conversation_id: _Preview | None}``
        map showing the latest message in each conversation —
        rendered as a preview line inside each list item. ``None`` for
        a conversation (or a missing key) means "no preview to
        show" and a muted placeholder is rendered for that row. The
        helpers :func:`pick_conversation_from_sdk` /
        :func:`pick_conversation_from_store` build the dict via
        :func:`_collect_previews_async` /
        :func:`_collect_previews_sync` before invoking this
        function; callers driving the pure picker can pass ``None``
        and preview lines are omitted entirely.
    :param show_runtime: When ``True``, render runtime metadata
        showing ``[claude]`` for claude-native conversations
        and ``[chat]`` for everything else. Used by the cross-agent
        picker (``omnigent resume``) where a user is choosing
        across multiple wrappers and needs to see which runtime each
        row belongs to. Per-agent pickers leave this off because the
        runtime is implicit from the agent.
    :param show_workspace: When ``True``, render workspace metadata
        between the timestamp and conversation id showing the cwd
        each session was launched from (read per row from the
        wrapper's client-side persistent state, not from the
        conversation row). Rows with no recorded workspace omit that
        metadata segment. Rows whose recorded workspace differs from
        the current cwd are flagged with a ``↪ cd`` suffix so the
        user sees at a glance which picks will trigger the
        resume-time chdir prompt. Used by the claude-native wrapper
        picker because Claude Code's ``--resume`` requires the cwd
        to match the original; other pickers leave this off.
    :param out: Output stream for the rendered picker. Default
        ``sys.stderr``. Override in tests with a ``StringIO``.
    :param in_: Input stream for user keystrokes. Default
        ``sys.stdin``. Override in tests with a ``StringIO``.
    :returns: The selected ``conversation_id``, or ``None`` when
        the user cancels (q / Esc / EOF).
    """
    out_stream: IO[str] = out if out is not None else sys.stderr
    in_stream: IO[str] = in_ if in_ is not None else sys.stdin

    if not conversations:
        _print_empty(agent_name, out_stream)
        return None

    if _is_tty(in_stream):
        return _pick_conversation_prompt_toolkit(
            conversations,
            agent_name=agent_name,
            previews=previews,
            show_runtime=show_runtime,
            show_workspace=show_workspace,
            out=out_stream,
            in_=in_stream,
        )
    return _pick_conversation_line_buffered(
        conversations,
        agent_name=agent_name,
        previews=previews,
        show_runtime=show_runtime,
        show_workspace=show_workspace,
        out=out_stream,
        in_=in_stream,
    )


def _pick_conversation_line_buffered(
    conversations: list[_ConversationRow],
    *,
    agent_name: str,
    previews: dict[str, _Preview | None] | None,
    show_runtime: bool,
    show_workspace: bool,
    out: IO[str],
    in_: IO[str],
) -> str | None:
    """
    Run the resume picker with line-buffered input.

    This path is for scripted / redirected stdin where prompt-toolkit
    cannot enter terminal mode. It keeps legacy numeric selection
    compatibility while sharing the same list rendering as the
    interactive path.

    :param conversations: Conversations to choose from.
    :param agent_name: Agent label shown in the header,
        e.g. ``"resume_test"``.
    :param previews: Optional preview map used for preview lines.
    :param show_runtime: Whether runtime metadata is present.
    :param show_workspace: Whether workspace metadata is present.
    :param out: Output stream for rendered pages.
    :param in_: Line-buffered input stream.
    :returns: Selected conversation id, or ``None`` on cancel / EOF.
    """
    selected_index = 0
    page_start = _page_start_for_selection(selected_index)
    while True:
        page = conversations[page_start : page_start + _PAGE_SIZE]
        _print_page(
            page,
            page_start,
            selected_index,
            len(conversations),
            agent_name,
            out,
            previews,
            show_runtime=show_runtime,
            show_workspace=show_workspace,
        )
        choice = _read_line_choice(in_)
        if choice is None or choice in _QUIT_TOKENS:
            return None
        if choice == _SELECT_TOKEN:
            return str(conversations[selected_index].id)
        if choice in _UP_TOKENS:
            next_selected_index = max(0, selected_index - 1)
            next_page_start = _page_start_for_selection(next_selected_index)
            if next_selected_index == selected_index:
                continue
            selected_index = next_selected_index
            page_start = next_page_start
            continue
        if choice in _DOWN_TOKENS:
            next_selected_index = min(len(conversations) - 1, selected_index + 1)
            next_page_start = _page_start_for_selection(next_selected_index)
            if next_selected_index == selected_index:
                continue
            selected_index = next_selected_index
            page_start = next_page_start
            continue
        if choice in _NEXT_TOKENS:
            last_page_start = ((len(conversations) - 1) // _PAGE_SIZE) * _PAGE_SIZE
            page_start = min(page_start + _PAGE_SIZE, last_page_start)
            selected_index = page_start
            continue
        if choice in _PREV_TOKENS:
            page_start = max(0, page_start - _PAGE_SIZE)
            selected_index = page_start
            continue
        try:
            absolute_index = int(choice) - 1
        except ValueError:
            _print_invalid(out)
            continue
        # Numeric selection is retained only for scripted / non-TTY
        # callers that still feed row numbers. The prompt-toolkit TTY
        # path does not use digit choices; interactive users get one
        # visible path: highlight a row and press Enter.
        page_end = page_start + len(page)
        if page_start <= absolute_index < page_end:
            return str(conversations[absolute_index].id)
        _print_invalid(out)


@dataclass
class _PromptToolkitPickerState:
    """
    Mutable state owned by the prompt-toolkit resume picker.

    :param conversations: Full ordered conversation list.
    :param agent_name: Agent label shown in the header,
        e.g. ``"resume_test"``.
    :param previews: Optional preview map used for preview lines.
    :param show_runtime: Whether runtime metadata is present.
    :param show_workspace: Whether workspace metadata is present.
    :param selected_index: Zero-based selected conversation index.
    """

    conversations: list[_ConversationRow]
    agent_name: str
    previews: dict[str, _Preview | None] | None
    show_runtime: bool
    show_workspace: bool
    selected_index: int = 0

    @property
    def page_start(self) -> int:
        """
        Current page offset for :attr:`selected_index`.

        :returns: Zero-based page start, always a multiple of
            :data:`_PAGE_SIZE`.
        """
        return _page_start_for_selection(self.selected_index)

    @property
    def page(self) -> list[_ConversationRow]:
        """
        Visible conversation slice for the current selection.

        :returns: Conversations on the current page.
        """
        start = self.page_start
        return self.conversations[start : start + _PAGE_SIZE]

    def move_selection(self, delta: int) -> None:
        """
        Move the highlighted row by *delta*.

        :param delta: Signed row delta, e.g. ``1`` for Down and
            ``-1`` for Up.
        :returns: None.
        """
        last_index = len(self.conversations) - 1
        self.selected_index = min(last_index, max(0, self.selected_index + delta))

    def next_page(self) -> None:
        """
        Move selection to the first row on the next page.

        :returns: None.
        """
        last_page_start = ((len(self.conversations) - 1) // _PAGE_SIZE) * _PAGE_SIZE
        self.selected_index = min(self.page_start + _PAGE_SIZE, last_page_start)

    def previous_page(self) -> None:
        """
        Move selection to the first row on the previous page.

        :returns: None.
        """
        self.selected_index = max(0, self.page_start - _PAGE_SIZE)

    def selected_id(self) -> str:
        """
        Return the currently highlighted conversation id.

        :returns: Conversation id, e.g. ``"conv_abc123"``.
        """
        return str(self.conversations[self.selected_index].id)


def _pick_conversation_prompt_toolkit(
    conversations: list[_ConversationRow],
    *,
    agent_name: str,
    previews: dict[str, _Preview | None] | None,
    show_runtime: bool,
    show_workspace: bool,
    out: IO[str],
    in_: IO[str],
) -> str | None:
    """
    Run the interactive TTY picker using prompt-toolkit.

    prompt-toolkit owns key decoding and rendering diffing here; this
    module only supplies state and formatted fragments. That avoids
    hand-written terminal escape sequences while keeping arrow-key
    movement responsive.

    :param conversations: Conversations to choose from.
    :param agent_name: Agent label shown in the header,
        e.g. ``"resume_test"``.
    :param previews: Optional preview map used for preview lines.
    :param show_runtime: Whether runtime metadata is present.
    :param show_workspace: Whether workspace metadata is present.
    :param out: Output stream for the prompt-toolkit renderer.
    :param in_: Input stream for keypresses.
    :returns: Selected conversation id, or ``None`` when cancelled.
    :raises KeyboardInterrupt: Propagated when the user presses
        Ctrl+C.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.input.defaults import create_input
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output.defaults import create_output

    state = _PromptToolkitPickerState(
        conversations=conversations,
        agent_name=agent_name,
        previews=previews,
        show_runtime=show_runtime,
        show_workspace=show_workspace,
    )
    control = FormattedTextControl(lambda: _prompt_toolkit_fragments(state), focusable=True)
    key_bindings = _prompt_toolkit_key_bindings(state)
    app: Application[str | None] = Application(
        layout=Layout(Window(content=control, wrap_lines=True, always_hide_cursor=True)),
        key_bindings=key_bindings,
        style=_prompt_toolkit_style(),
        include_default_pygments_style=False,
        full_screen=False,
        erase_when_done=False,
        input=create_input(stdin=in_),
        output=create_output(stdout=out),
    )
    return app.run(
        handle_sigint=False,
        set_exception_handler=False,
        in_thread=_has_running_event_loop(),
    )


def _has_running_event_loop() -> bool:
    """
    Return whether the current thread is already running asyncio.

    prompt-toolkit's synchronous :meth:`Application.run` calls
    :func:`asyncio.run` by default, which raises inside the async SDK
    resume path after session rows have been fetched. In that case we
    ask prompt-toolkit to host its application loop in a worker thread
    while this synchronous picker waits for the selected result.

    :returns: ``True`` when :func:`asyncio.get_running_loop` finds an
        active loop in this thread, otherwise ``False``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _prompt_toolkit_key_bindings(state: _PromptToolkitPickerState) -> Any:
    """
    Build keybindings for the prompt-toolkit picker.

    :param state: Mutable picker state.
    :returns: A :class:`prompt_toolkit.key_binding.KeyBindings`
        instance.
    """
    from prompt_toolkit.key_binding import KeyBindings

    key_bindings = KeyBindings()

    @key_bindings.add("up")
    def _move_up(event: Any) -> None:
        """
        Move the picker selection one row upward.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.move_selection(-1)
        event.app.invalidate()

    @key_bindings.add("down")
    def _move_down(event: Any) -> None:
        """
        Move the picker selection one row downward.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.move_selection(1)
        event.app.invalidate()

    @key_bindings.add("n")
    @key_bindings.add("right")
    def _next_page(event: Any) -> None:
        """
        Move the picker selection to the next page.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.next_page()
        event.app.invalidate()

    @key_bindings.add("p")
    @key_bindings.add("left")
    def _previous_page(event: Any) -> None:
        """
        Move the picker selection to the previous page.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        state.previous_page()
        event.app.invalidate()

    @key_bindings.add("enter")
    def _select(event: Any) -> None:
        """
        Resume the currently highlighted conversation.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(result=state.selected_id())

    @key_bindings.add("q")
    @key_bindings.add("escape")
    @key_bindings.add("c-d")
    def _cancel(event: Any) -> None:
        """
        Cancel the picker without selecting a conversation.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(result=None)

    @key_bindings.add("c-c")
    def _interrupt(event: Any) -> None:
        """
        Propagate Ctrl+C as :class:`KeyboardInterrupt`.

        :param event: prompt-toolkit key event.
        :returns: None.
        """
        event.app.exit(exception=KeyboardInterrupt)

    return key_bindings


def _prompt_toolkit_style() -> Any:
    """
    Build prompt-toolkit style classes for the picker.

    :returns: A :class:`prompt_toolkit.styles.Style` instance.
    """
    from prompt_toolkit.styles import Style

    return Style.from_dict(
        {
            "accent": _ACCENT,
            "accent-bold": f"{_ACCENT} bold",
            "muted": _MUTED,
            "title": "bold",
            "selected-title": f"{_ACCENT} bold",
            "untitled": _MUTED,
        }
    )


def _prompt_toolkit_fragments(state: _PromptToolkitPickerState) -> list[tuple[str, str]]:
    """
    Render picker state as prompt-toolkit formatted text fragments.

    :param state: Mutable picker state.
    :returns: ``(style, text)`` fragments for
        :class:`prompt_toolkit.layout.controls.FormattedTextControl`.
    """
    fragments: list[tuple[str, str]] = []
    _append_prompt_toolkit_header(fragments, state)
    current_cwd = Path.cwd().resolve() if state.show_workspace else None
    page_start = state.page_start
    page = state.page
    for offset, conv in enumerate(page):
        _append_prompt_toolkit_item(
            fragments,
            conv,
            absolute_index=page_start + offset,
            selected_index=state.selected_index,
            previews=state.previews,
            show_runtime=state.show_runtime,
            show_workspace=state.show_workspace,
            current_cwd=current_cwd,
            is_last=offset == len(page) - 1,
        )
    _append_prompt_toolkit_footer(fragments)
    return fragments


def _append_prompt_toolkit_header(
    fragments: list[tuple[str, str]],
    state: _PromptToolkitPickerState,
) -> None:
    """
    Append the picker header to prompt-toolkit fragments.

    :param fragments: Fragment list being built.
    :param state: Mutable picker state.
    :returns: None.
    """
    page_start = state.page_start
    page_len = len(state.page)
    total = len(state.conversations)
    page_no = page_start // _PAGE_SIZE + 1
    page_count = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    last_index = page_start + page_len
    fragments.extend(
        [
            ("class:accent", "Resume"),
            ("class:muted", "  ·  "),
            ("class:title", state.agent_name),
            ("class:muted", f"  · page {page_no}/{page_count}, "),
            ("class:muted", f"showing {page_start + 1}-{last_index} of {total}\n"),
        ]
    )


def _append_prompt_toolkit_item(
    fragments: list[tuple[str, str]],
    conv: _ConversationRow,
    *,
    absolute_index: int,
    selected_index: int,
    previews: dict[str, _Preview | None] | None,
    show_runtime: bool,
    show_workspace: bool,
    current_cwd: Path | None,
    is_last: bool,
) -> None:
    """
    Append one list item to prompt-toolkit fragments.

    :param fragments: Fragment list being built.
    :param conv: Conversation row being rendered.
    :param absolute_index: Zero-based index in the full conversation list.
    :param selected_index: Zero-based selected conversation index.
    :param previews: Optional preview map used for preview lines.
    :param show_runtime: Whether runtime metadata is present.
    :param show_workspace: Whether workspace metadata is present.
    :param current_cwd: Resolved current cwd when
        ``show_workspace=True``; otherwise ``None``.
    :param is_last: Whether this is the last visible item.
    :returns: None.
    """
    selected = absolute_index == selected_index
    if selected:
        title_style = "class:selected-title" if conv.title else "class:untitled"
        fragments.extend(
            [
                ("class:accent-bold", "> "),
                ("class:accent-bold", f"{absolute_index + 1}. "),
                (title_style, conv.title or "(untitled)"),
                ("", "\n"),
            ]
        )
    else:
        title_style = "" if conv.title else "class:untitled"
        fragments.extend(
            [
                ("", "  "),
                ("class:muted", f"{absolute_index + 1}. "),
                (title_style, conv.title or "(untitled)"),
                ("", "\n"),
            ]
        )
    _append_prompt_toolkit_metadata(
        fragments,
        conv,
        show_runtime=show_runtime,
        show_workspace=show_workspace,
        current_cwd=current_cwd,
    )
    if previews is not None:
        _append_prompt_toolkit_preview(fragments, previews.get(conv.id))
    if not is_last:
        fragments.append(("", "\n"))


def _append_prompt_toolkit_metadata(
    fragments: list[tuple[str, str]],
    conv: _ConversationRow,
    *,
    show_runtime: bool,
    show_workspace: bool,
    current_cwd: Path | None,
) -> None:
    """
    Append one prompt-toolkit metadata line.

    :param fragments: Fragment list being built.
    :param conv: Conversation row being rendered.
    :param show_runtime: Whether runtime metadata is present.
    :param show_workspace: Whether workspace metadata is present.
    :param current_cwd: Resolved current cwd when
        ``show_workspace=True``; otherwise ``None``.
    :returns: None.
    """
    fragments.append(("class:muted", f"    {_format_when(conv.created_at)}"))
    if show_workspace:
        assert current_cwd is not None
        workspace = _workspace_metadata(conv, current_cwd=current_cwd)
        if workspace is not None:
            working_directory, requires_cd = workspace
            fragments.extend(
                [
                    ("class:muted", _METADATA_SEPARATOR),
                    ("class:muted", working_directory),
                ]
            )
            if requires_cd:
                fragments.append(("class:accent", "  ↪\xa0cd"))
    fragments.extend(
        [
            ("class:muted", _METADATA_SEPARATOR),
            ("class:muted", str(conv.id)),
        ]
    )
    if show_runtime:
        fragments.extend(
            [
                ("class:muted", _METADATA_SEPARATOR),
                ("class:muted", _runtime_badge(conv)),
            ]
        )
    fragments.append(("class:muted", "\n"))


def _append_prompt_toolkit_preview(
    fragments: list[tuple[str, str]],
    preview: _Preview | None,
) -> None:
    """
    Append one prompt-toolkit preview line.

    :param fragments: Fragment list being built.
    :param preview: Latest-message preview, or ``None``.
    :returns: None.
    """
    if preview is None:
        fragments.append(("class:muted", "    …\n"))
        return
    glyph = _USER_GLYPH if preview.role == "user" else _ASSISTANT_GLYPH
    fragments.extend(
        [
            ("", "    "),
            ("class:accent", glyph),
            ("class:muted", f"  {preview.text}\n"),
        ]
    )


def _append_prompt_toolkit_footer(fragments: list[tuple[str, str]]) -> None:
    """
    Append the prompt-toolkit picker footer.

    :param fragments: Fragment list being built.
    :returns: None.
    """
    fragments.extend(
        [
            ("class:muted", "  Keys:  "),
            ("class:accent-bold", "↑"),
            ("class:muted", "/"),
            ("class:accent-bold", "↓"),
            ("class:muted", " move  ·  "),
            ("class:accent-bold", "Enter"),
            ("class:muted", " resume  ·  "),
            ("class:accent-bold", "n"),
            ("class:muted", "/"),
            ("class:accent-bold", "p"),
            ("class:muted", " page  ·  "),
            ("class:accent-bold", "q"),
            ("class:muted", "/"),
            ("class:accent-bold", "Esc"),
            ("class:muted", " cancel\n"),
        ]
    )


def _page_start_for_selection(selected_index: int) -> int:
    """
    Return the page offset that contains *selected_index*.

    The picker stores selection as an absolute index so moving
    across a page boundary is just ``selected_index += 1``. This
    helper converts that absolute selection into the current page's
    first row before each render.

    :param selected_index: Zero-based index into the full
        conversation list, e.g. ``10`` for the first row on page 2.
    :returns: Zero-based page offset, always a multiple of
        :data:`_PAGE_SIZE`.
    """
    return (selected_index // _PAGE_SIZE) * _PAGE_SIZE


async def pick_conversation_from_sdk(
    # ``Any`` to avoid coupling the repl package to the client's load order.
    client: Any,
    *,
    agent_name: str,
    agent_id: str | None = None,
    agent_name_filter: str | None = None,
    out: IO[str] | None = None,
    in_: IO[str] | None = None,
) -> str | None:
    """Fetch sessions via ``/v1/sessions`` and run the picker.

    Sessions API filters by direct ``conversation.agent_id`` (not via
    ``task.agent_id``) so wrapper sessions without task rows still
    appear; also enforces ``has_agent_id`` and ``accessible_by`` server-side.

    :param agent_id: Scope to this agent; ``None`` lists across agents.
    :param agent_name_filter: Scope to sessions whose bound agent row
        has this name. Used for session-scoped agents that share a
        YAML name but intentionally do not share ``agent_id``.
    """
    convos = await client.sessions.list(
        limit=200,
        agent_id=agent_id,
        agent_name=agent_name_filter,
        order="desc",
    )
    previews = await _collect_previews_async(client, convos)
    return pick_conversation(convos, agent_name=agent_name, previews=previews, out=out, in_=in_)


async def pick_conversation_by_wrapper_label_from_sdk(
    client: Any,
    *,
    wrapper_value: str,
    agent_name: str,
    out: IO[str] | None = None,
    in_: IO[str] | None = None,
) -> str | None:
    """Picker scoped to one wrapper kind (``omnigent.wrapper=<value>``).

    Wrapper invocations (claude-native today) upload a fresh agent
    bundle per session, so ``agents.get_by_name`` returns no canonical
    record — agent-id filtering can't be used. List every session the
    caller can see and filter by the wrapper label client-side.

    Renders workspace metadata so the user can see which cwd each
    session was launched from -- claude --resume requires cwd parity
    with the original session, and the row-level hint prepares the
    user for the chdir prompt the wrapper raises after they pick.
    The cwd comes from the wrapper's client-side persistent state
    (``~/.omnigent/claude-native/``); sessions created on a
    different machine will show as having no recorded cwd."""
    all_convos = await client.sessions.list(limit=200, agent_id=None, order="desc")
    convos = [
        c
        for c in all_convos
        if getattr(c, "labels", None)
        and c.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY) == wrapper_value
    ]
    previews = await _collect_previews_async(client, convos)
    return pick_conversation(
        convos,
        agent_name=agent_name,
        previews=previews,
        show_workspace=True,
        out=out,
        in_=in_,
    )


async def pick_conversation_cross_agent_from_sdk(
    client: Any,
    *,
    out: IO[str] | None = None,
    in_: IO[str] | None = None,
) -> str | None:
    """Cross-agent variant: lists every session the caller can see
    via ``/v1/sessions`` and renders runtime metadata for
    ``omnigent resume``'s runtime-dispatch UX."""
    convos = await client.sessions.list(limit=200, agent_id=None, order="desc")
    previews = await _collect_previews_async(client, convos)
    # Header label is intentionally generic — "resume" describes the
    # action, not a single agent. Without overriding the legacy
    # ``agent_name`` argument the header would print as "None" or an
    # empty string.
    return pick_conversation(
        convos,
        agent_name="all runtimes",
        previews=previews,
        show_runtime=True,
        out=out,
        in_=in_,
    )


def pick_conversation_from_store(
    # ``Any`` rather than the concrete store types so the picker
    # stays decoupled from the SqlAlchemy implementations — only
    # the ``ConversationStore`` abstract methods used here
    # (``list_conversations``) actually matter, and importing the
    # abstract base here would still add a non-trivial dependency
    # surface for a typing-only win.
    conv_store: Any,
    *,
    agent_name: str,
    out: IO[str] | None = None,
    in_: IO[str] | None = None,
) -> str | None:
    """
    Sync sibling for the one-shot ``-p`` path. Lists conversations
    directly from the store, scoped by the bound agent row's name.

    The one-shot path doesn't have an SDK client connected (the
    in-process ASGI app is reached via raw httpx + ASGITransport),
    so we read through the stores directly. Name scoping is important
    for session-scoped agents: every multipart upload gets a distinct
    ``agent_id`` while preserving the user-authored YAML name for
    resume lookup.

    :param conv_store: Conversation store for the list query.
    :param agent_name: Agent's registered name from the YAML.
    :param out: Output stream override (tests).
    :param in_: Input stream override (tests).
    :returns: Selected conversation_id, or ``None`` on cancel /
        empty list / unknown agent.
    """
    out_stream: IO[str] = out if out is not None else sys.stderr
    page = conv_store.list_conversations(
        agent_name=agent_name,
        has_agent_id=True,
        limit=200,
        sort_by="updated_at",
        order="desc",
    )
    previews = _collect_previews_sync(conv_store, page.data)
    return pick_conversation(
        page.data,
        agent_name=agent_name,
        previews=previews,
        out=out_stream,
        in_=in_,
    )


def _print_page(
    page: list[_ConversationRow],
    page_start: int,
    selected_index: int,
    total: int,
    agent_name: str,
    out: IO[str],
    previews: dict[str, _Preview | None] | None = None,
    *,
    show_runtime: bool = False,
    show_workspace: bool = False,
) -> None:
    """
    Render one page of the picker as a keyboard-selectable list.

    :param page: Slice of conversations to display.
    :param page_start: Zero-based offset of the first conversation
        on this page (used to compute ``"showing X-Y of Z"``).
    :param selected_index: Zero-based index into the full
        conversation list. The corresponding row is highlighted and
        marked with ``">"``.
    :param total: Total number of conversations across all pages.
    :param agent_name: Agent label for the header,
        e.g. ``"resume_test"``.
    :param out: Output stream. ``sys.stderr`` in production,
        ``StringIO`` in tests.
    :param previews: Optional preview map. When present, the page
        renders one preview under each list item.
    :param show_runtime: When ``True``, include runtime metadata.
    :param show_workspace: When ``True``, include launch-workspace
        metadata and compare recorded cwd against current cwd.
    :returns: None.
    """
    console = _make_console(out)
    console.print(_page_header_text(page_start, len(page), total, agent_name))
    current_cwd = Path.cwd().resolve() if show_workspace else None
    for offset, conv in enumerate(page):
        rendered = _list_item_lines(
            conv,
            absolute_index=page_start + offset,
            selected_index=selected_index,
            previews=previews,
            show_runtime=show_runtime,
            show_workspace=show_workspace,
            current_cwd=current_cwd,
        )
        _print_list_item(console, rendered, is_last=offset == len(page) - 1)
    _print_page_footer(console)


def _page_header_text(
    page_start: int,
    page_len: int,
    total: int,
    agent_name: str,
) -> Any:
    """
    Build the list header for one resume-picker page.

    :param page_start: Zero-based offset of the first row on the
        page, e.g. ``10`` for page 2.
    :param page_len: Number of rows on the page.
    :param total: Total number of conversations across all pages.
    :param agent_name: Agent label shown in the header,
        e.g. ``"resume_test"``.
    :returns: A :class:`rich.text.Text` header.
    """
    from rich.text import Text

    page_no = page_start // _PAGE_SIZE + 1
    page_count = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    last_index = page_start + page_len
    return Text.from_markup(
        f"[{_ACCENT}]Resume[/]  [{_MUTED}]·[/]  [bold]{_escape_for_markup(agent_name)}[/]"
        f"  [{_MUTED}]· page {page_no}/{page_count}, "
        f"showing {page_start + 1}-{last_index} of {total}[/]"
    )


def _list_item_lines(
    conv: _ConversationRow,
    *,
    absolute_index: int,
    selected_index: int,
    previews: dict[str, _Preview | None] | None,
    show_runtime: bool,
    show_workspace: bool,
    current_cwd: Path | None,
) -> _PageRowRender:
    """
    Build the rendered lines for one resume-picker list item.

    :param conv: Conversation row being rendered.
    :param absolute_index: Zero-based index in the full conversation list.
    :param selected_index: Zero-based selected conversation index.
    :param previews: Optional preview map used for preview lines.
    :param show_runtime: Whether runtime metadata is present.
    :param show_workspace: Whether workspace metadata is present.
    :param current_cwd: Resolved current cwd when
        ``show_workspace=True``; otherwise ``None``.
    :returns: Rendered item lines and selected state.
    """
    selected = absolute_index == selected_index
    lines = [
        _list_item_title(conv, absolute_index=absolute_index, selected=selected),
        _list_item_metadata(
            conv,
            show_runtime=show_runtime,
            show_workspace=show_workspace,
            current_cwd=current_cwd,
        ),
    ]
    if previews is not None:
        lines.append(_list_item_preview(previews.get(conv.id)))
    return _PageRowRender(lines=lines)


def _list_item_title(conv: _ConversationRow, *, absolute_index: int, selected: bool) -> Any:
    """
    Build the primary title line for a resume-picker list item.

    :param conv: Conversation row being rendered.
    :param absolute_index: Zero-based index in the full conversation list.
    :param selected: Whether the row is the highlighted selection.
    :returns: A :class:`rich.text.Text` title line.
    """
    from rich.text import Text

    text = Text()
    if selected:
        text.append("> ", style=f"bold {_ACCENT}")
        text.append(f"{absolute_index + 1}. ", style=f"bold {_ACCENT}")
        title_style = f"bold {_ACCENT}" if conv.title else f"bold {_MUTED}"
        text.append(conv.title or "(untitled)", style=title_style)
        return text

    text.append("  ")
    text.append(f"{absolute_index + 1}. ", style=_MUTED)
    text.append(conv.title or "(untitled)", style=None if conv.title else _MUTED)
    return text


def _list_item_metadata(
    conv: _ConversationRow,
    *,
    show_runtime: bool,
    show_workspace: bool,
    current_cwd: Path | None,
) -> Any:
    """
    Build the metadata line for a resume-picker list item.

    :param conv: Conversation row being rendered.
    :param show_runtime: Whether to include runtime metadata.
    :param show_workspace: Whether to include recorded workspace
        metadata when available.
    :param current_cwd: Resolved current cwd when
        ``show_workspace=True``; otherwise ``None``.
    :returns: A :class:`rich.text.Text` metadata line.
    """
    from rich.text import Text

    text = Text("    ", style=_MUTED)
    text.append(_format_when(conv.created_at), style=_MUTED)
    if show_workspace:
        assert current_cwd is not None
        workspace = _render_workspace_cell(conv, current_cwd=current_cwd)
        if workspace is not None:
            text.append(_METADATA_SEPARATOR, style=_MUTED)
            text.append_text(workspace)
    text.append(_METADATA_SEPARATOR, style=_MUTED)
    text.append(str(conv.id), style=_MUTED)
    if show_runtime:
        text.append(_METADATA_SEPARATOR, style=_MUTED)
        text.append(_runtime_badge(conv), style=_MUTED)
    return text


def _list_item_preview(preview: _Preview | None) -> Any:
    """
    Build the preview line for a resume-picker list item.

    :param preview: Latest-message preview, or ``None``.
    :returns: A :class:`rich.text.Text` preview line.
    """
    from rich.text import Text

    text = _render_preview_cell(preview)
    return Text("    ") + text


def _print_list_item(console: Any, rendered: _PageRowRender, *, is_last: bool) -> None:
    """
    Print one rendered resume-picker list item.

    :param console: Rich console returned by :func:`_make_console`.
    :param rendered: Rendered item lines from :func:`_list_item_lines`.
    :param is_last: Whether this is the last item on the page.
    """
    for line in rendered.lines:
        console.print(line)
    if not is_last:
        console.print()


def _print_page_footer(console: Any) -> None:
    """
    Print the resume-picker keybinding footer and trailing spacer.

    :param console: Rich console returned by :func:`_make_console`.
    """
    from rich.text import Text

    console.print(
        Text.from_markup(
            f"  [{_MUTED}]Keys:[/]  "
            f"[bold {_ACCENT}]↑[/]/[bold {_ACCENT}]↓[/] move  "
            f"[{_MUTED}]·[/]  "
            f"[bold {_ACCENT}]Enter[/] resume  "
            f"[{_MUTED}]·[/]  "
            f"[bold {_ACCENT}]n[/]/[bold {_ACCENT}]p[/] page  "
            f"[{_MUTED}]·[/]  "
            f"[bold {_ACCENT}]q[/]/[bold {_ACCENT}]Esc[/] cancel"
        )
    )
    console.print()


def _print_empty(agent_name: str, out: IO[str]) -> None:
    """
    Render the "no prior conversations" notice.

    Replaces the bare ``print(...)`` call so the empty-list case
    matches the page render's visual language. Tests assert on
    the substring ``"No prior conversations"`` — preserve it
    exactly.

    :param agent_name: Agent label for the message,
        e.g. ``"resume_test"``.
    :param out: Output stream override.
    """
    from rich.text import Text

    console = _make_console(out)
    console.print(
        Text.from_markup(
            f"  [{_MUTED}]No prior conversations for agent[/] [bold]{agent_name}[/][{_MUTED}].[/]"
        )
    )


def _print_invalid(out: IO[str]) -> None:
    """
    Render the "invalid selection" notice between picker re-prompts.

    Tests assert on the substring ``"Invalid selection."`` —
    preserve it exactly.

    :param out: Output stream override.
    """
    from rich.text import Text

    console = _make_console(out)
    console.print(Text.from_markup(f"  [{_ACCENT}]Invalid selection.[/]  [{_MUTED}]Try again.[/]"))


def _make_console(out: IO[str]) -> Any:
    """
    Build a :class:`rich.console.Console` aimed at *out*.

    Centralized so the page render, empty-list message, and
    invalid-selection notice all create their consoles the
    same way. Rich's defaults handle the tty-vs-StringIO
    branching: real stderr gets ANSI styling, in-memory
    streams get plain text. ``soft_wrap=True`` prevents Rich from
    injecting hard newlines into redirected transcripts; terminals
    still wrap long lines visually.

    :param out: Output stream override.
    :returns: A :class:`rich.console.Console` writing to *out*.
    """
    from rich.console import Console

    return Console(file=out, highlight=False, soft_wrap=True)


def _render_preview_cell(preview: _Preview | None) -> Any:
    """
    Render a :class:`_Preview` as a list-item preview line.

    Renders as ``"<glyph> <text>"`` where the glyph reflects the
    role: ``❯`` for user, ``◆`` for assistant. Glyphs are styled
    in accent color; the text body is muted so the list item's
    title stays the eye's first stop. ``None`` (no preview
    available — fetch failed, conversation is empty, or it
    fell past the prefetch cap) renders as a single muted ``…``
    so the line doesn't collapse to blank-and-look-broken.

    :param preview: The preview to render, or ``None``.
    :returns: A :class:`rich.text.Text` ready to print in a list item.
    """
    from rich.text import Text

    if preview is None:
        return Text("…", style=_MUTED)
    glyph = _USER_GLYPH if preview.role == "user" else _ASSISTANT_GLYPH
    return Text.from_markup(
        f"[{_ACCENT}]{glyph}[/]  [{_MUTED}]{_escape_for_markup(preview.text)}[/]"
    )


def _render_workspace_cell(row: _ConversationRow, *, current_cwd: Path) -> Any | None:
    """
    Render the workspace metadata value for one picker row.

    Looks up the wrapper's client-side persistent launch state for
    *row.id* via the matching native state module.
    When state is present, renders the recorded path in muted gray
    and appends a ``↪ cd`` flag in accent color if it differs from
    *current_cwd* (resolved). When state is absent -- legacy session
    pre-dating this tracking, session created on a different
    machine, or a non-wrapper session that never set it -- returns
    ``None`` so the picker omits the workspace metadata segment.

    The flag uses ``↪`` ("return / leftwards arrow with hook") +
    a no-break space + ``cd`` so the badge stays on one visual
    unit when the line wraps. Picked deliberately over ``[cd]``
    so Rich's markup parser doesn't try to interpret it as a
    style tag (we'd otherwise need :func:`_escape_for_markup`).

    Reads happen once per row at render time. With the default
    :data:`_PREVIEW_PREFETCH_CAP` of 100 conversations the picker
    issues at most 100 small JSON reads from
    ``~/.omnigent/claude-native/<hash>/launch.json`` -- fast on
    local disk. We deliberately do not async / parallelize these
    because the picker is single-threaded and a stat+read of a
    sub-200-byte file is microseconds on any reasonable storage.

    :param row: One conversation row from the SDK / store list.
    :param current_cwd: The wrapper's current working directory,
        already resolved, captured once per page render.
    :returns: A :class:`rich.text.Text` ready to print in a list item,
        or ``None`` when no workspace was recorded.
    """
    from rich.text import Text

    workspace = _workspace_metadata(row, current_cwd=current_cwd)
    if workspace is None:
        return None
    working_directory, requires_cd = workspace
    if not requires_cd:
        return Text(working_directory, style=_MUTED)
    return Text.from_markup(
        f"[{_MUTED}]{_escape_for_markup(working_directory)}[/]  [{_ACCENT}]↪\xa0cd[/]"
    )


def _workspace_metadata(row: _ConversationRow, *, current_cwd: Path) -> tuple[str, bool] | None:
    """
    Read the recorded workspace for one conversation.

    :param row: One conversation row from the SDK / store list.
    :param current_cwd: The wrapper's current working directory,
        already resolved.
    :returns: ``(working_directory, requires_cd)`` when recorded, or
        ``None`` when no workspace state exists.
    """
    state = _launch_state_for_row(row)
    if state is None:
        return None
    working_directory = state.working_directory
    # Resolve so a recorded ``/repo`` and a current ``/home/me/repo``
    # (symlink) don't falsely flag as different. ``Path.resolve()``
    # doesn't raise on non-existent paths, so this is safe even when
    # the recorded directory has since been deleted -- the
    # downstream chdir-prompt path handles that case loudly.
    recorded = Path(working_directory).resolve()
    return working_directory, recorded != current_cwd


def _launch_state_for_row(row: _ConversationRow) -> Any | None:
    """
    Read native launch state using the row's wrapper label.

    :param row: One conversation row from the SDK / store list.
    :returns: Native launch state object with ``working_directory``,
        or ``None`` when the row has no supported wrapper state.
    """
    labels = getattr(row, "labels", None)
    if not isinstance(labels, dict):
        return None
    wrapper = labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    if wrapper == _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE:
        return _read_claude_launch_state(row.id)
    if wrapper == CODEX_NATIVE_CODING_AGENT.wrapper_label:
        return _read_codex_launch_state(row.id)
    return None


def _escape_for_markup(text: str) -> str:
    """
    Escape ``[`` so :func:`rich.text.Text.from_markup` doesn't
    interpret user-text bracket sequences as style tags.

    Without this, a preview text containing ``"[bold]"`` would
    re-enter Rich's parser and either throw or apply unintended
    styling. ``\\[`` is Rich's escape for ``[``; the rest of the
    text passes through untouched.

    :param text: User-visible text from the preview.
    :returns: The text with ``[`` characters escaped.
    """
    return text.replace("[", r"\[")


async def _collect_previews_async(
    client: Any,
    conversations: list[_ConversationRow],
) -> dict[str, _Preview | None]:
    """
    Fetch the latest message for each conversation in parallel.

    Concurrent ``list_items(limit=10, order="desc")`` calls
    fanned out via :func:`asyncio.gather` so the picker's
    initial render isn't gated on N round-trips serially. The
    fetch is bounded by :data:`_PREVIEW_PREFETCH_CAP` because a
    user with thousands of conversations doesn't want the picker
    to stall while it warms up — the cap covers typical use
    while keeping the worst case bounded.

    Per-conversation failures (HTTP errors, missing items)
    surface as ``None`` in the returned dict instead of bubbling
    up; a preview is a UX nicety, not a correctness requirement,
    and one bad row shouldn't kill the picker.

    :param client: The :class:`omnigent_client.OmnigentClient`.
    :param conversations: Full conversation list (will be capped
        internally by :data:`_PREVIEW_PREFETCH_CAP`).
    :returns: A ``{conversation_id: _Preview | None}`` map. Keys
        beyond the cap simply don't appear; the picker treats a
        missing key the same as ``None``.
    """

    async def fetch_one(conv: _ConversationRow) -> tuple[str, _Preview | None]:
        try:
            items = await client.sessions.list_items(conv.id, limit=10, order="desc")
        except Exception:  # noqa: BLE001 — preview is best-effort, swallow per-conv errors
            return conv.id, None
        return conv.id, _last_message_preview_from_dicts(items)

    capped = conversations[:_PREVIEW_PREFETCH_CAP]
    pairs = await asyncio.gather(*(fetch_one(c) for c in capped))
    return dict(pairs)


def _collect_previews_sync(
    conv_store: Any,
    conversations: list[_ConversationRow],
) -> dict[str, _Preview | None]:
    """
    Sync sibling for the one-shot ``-p`` path.

    Sequential rather than parallel — same ``conv_store`` instance
    can't safely be hit from multiple threads here without a
    deeper guarantee about the SqlAlchemy session policy, and the
    in-process store is fast enough that a serial loop over
    :data:`_PREVIEW_PREFETCH_CAP` rows finishes well under a
    second on local SQLite. If users ever hit the cap and notice
    latency, we move to a thread-pool fan-out — but only with
    measurements showing it's needed.

    :param conv_store: The conversation store.
    :param conversations: Full conversation list (will be capped
        internally by :data:`_PREVIEW_PREFETCH_CAP`).
    :returns: A ``{conversation_id: _Preview | None}`` map; same
        contract as :func:`_collect_previews_async`.
    """
    out: dict[str, _Preview | None] = {}
    for conv in conversations[:_PREVIEW_PREFETCH_CAP]:
        try:
            page = conv_store.list_items(conversation_id=conv.id, limit=10, order="desc")
        except Exception:  # noqa: BLE001 — preview is best-effort, swallow per-conv errors
            out[conv.id] = None
            continue
        out[conv.id] = _last_message_preview_from_entities(page.data)
    return out


def _last_message_preview_from_dicts(
    items: list[dict[str, Any]],
) -> _Preview | None:
    """
    Extract the latest message preview from API-shape item dicts.

    The SDK's ``client.sessions.list_items`` returns dicts
    in the API-flattened shape (``{"type": "message", "role":
    "...", "content": [...]}``). Walks the list newest-to-oldest
    (the caller fetched with ``order="desc"``) looking for the
    first non-meta ``message`` item with extractable text.

    :param items: Items from the SDK call (already in
        ``order="desc"`` so ``items[0]`` is the most recent).
    :returns: A :class:`_Preview`, or ``None`` when no message
        item with text is found in the slice (e.g. the
        conversation has only tool calls, or is empty).
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        if item.get("is_meta") is True:
            continue
        role = item.get("role")
        if not isinstance(role, str) or not role:
            continue
        text = _extract_text_from_content_blocks(item.get("content"))
        if text:
            return _Preview(role=role, text=text)
    return None


def _last_message_preview_from_entities(items: list[Any]) -> _Preview | None:
    """
    Extract the latest message preview from store entity items.

    The store's ``list_items`` returns
    :class:`omnigent.entities.ConversationItem` objects whose
    ``data`` carries the typed ``MessageData`` for message rows.
    Same walk as :func:`_last_message_preview_from_dicts` but on
    the entity attribute path.

    :param items: Items from the store call (already in
        ``order="desc"``).
    :returns: A :class:`_Preview`, or ``None`` (same contract).
    """
    for item in items:
        if getattr(item, "type", None) != "message":
            continue
        data = getattr(item, "data", None)
        if getattr(data, "is_meta", False):
            continue
        role = getattr(data, "role", None)
        content = getattr(data, "content", None)
        if not isinstance(role, str) or not role:
            continue
        text = _extract_text_from_content_blocks(content)
        if text:
            return _Preview(role=role, text=text)
    return None


def _extract_text_from_content_blocks(content: Any) -> str:
    """
    Reduce a Responses-API message ``content`` to one preview line.

    Pulls the ``text`` field off ``input_text`` / ``output_text``
    blocks (and any other block carrying a string ``text``),
    collapses whitespace to single spaces, and truncates to
    :data:`_PREVIEW_DISPLAY_CHARS` with a trailing ``…`` when the
    original was longer. Image / file blocks are dropped — the
    preview line is text-only.

    :param content: A list of typed content blocks, a string,
        or ``None``. Anything else returns ``""``.
    :returns: The collapsed + truncated preview text. Empty
        string means "no extractable text" (caller treats as
        no preview for this item).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                value = block.get("text")
            else:
                value = getattr(block, "text", None)
            if isinstance(value, str):
                parts.append(value)
        text = " ".join(parts)
    else:
        return ""
    # Collapse whitespace so a multi-line message renders as a
    # single tidy preview line — newlines and tabs become single
    # spaces, runs of spaces become one space.
    compact = " ".join(text.split())
    if not compact:
        return ""
    if len(compact) <= _PREVIEW_DISPLAY_CHARS:
        return compact
    return compact[: _PREVIEW_DISPLAY_CHARS - 1] + "…"


def _format_when(created_at: int) -> str:
    """
    Format *created_at* as a compact human-readable label.

    Picker rows benefit from the relative form ("5m ago",
    "3h ago", "2d ago") for recent items because that's
    how users actually think about which conversation they
    want; older items fall back to ``"Mon DD HH:MM"`` (local
    time) which is unambiguous without the visual noise of a
    full ISO timestamp. The legacy picker showed UTC
    ``"YYYY-MM-DD HH:MM"`` for everything, which is
    technically more precise but harder to scan.

    :param created_at: Unix epoch seconds (server-side
        ``Conversation.created_at``).
    :returns: Compact label, e.g. ``"5m ago"``, ``"3h ago"``,
        ``"2d ago"``, or ``"Apr 27 14:30"``.
    """
    now = int(time.time())
    delta = now - created_at
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 7 * 86400:
        return f"{delta // 86400}d ago"
    return datetime.fromtimestamp(created_at).strftime("%b %d %H:%M")


def _read_line_choice(in_: IO[str]) -> str | None:
    """
    Read one line-buffered picker choice.

    Blank lines map to Enter/select so scripted fallback input follows
    the same action semantics as the prompt-toolkit TTY path.

    :param in_: Input stream.
    :returns: Lowercased + stripped input, :data:`_SELECT_TOKEN`
        for a blank line, or ``None`` on EOF.
    """
    line = in_.readline()
    if not line:
        return None
    choice = line.strip().lower()
    return _SELECT_TOKEN if choice == "" else choice


def _is_tty(in_: IO[str]) -> bool:
    """
    Predicate: is *in_* an interactive terminal?

    The prompt-toolkit path needs both ``isatty()`` truth and a real
    file descriptor (``fileno()``). ``StringIO`` has neither; a pipe
    has ``fileno()`` but not ``isatty()``.

    :param in_: Input stream.
    :returns: ``True`` only when both checks pass.
    """
    isatty = getattr(in_, "isatty", None)
    fileno = getattr(in_, "fileno", None)
    if isatty is None or fileno is None:
        return False
    try:
        return bool(isatty()) and isinstance(fileno(), int)
    except (OSError, ValueError):
        return False
