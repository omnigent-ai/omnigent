"""RichBlockFormatter — converts stream blocks to Rich renderables.

Each block type has a ``format_*`` method. Override any method to
customize rendering. The base class provides a polished terminal
treatment with panels, syntax highlighting, and a magenta-pink
brand color scheme.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass

from omnigent_client import (
    AnyBlock,
    CompactionBlock,
    ErrorBlock,
    FileBlock,
    NativeToolBlock,
    ReasoningBlock,
    ReasoningChunk,
    ReasoningStartBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    RetryBlock,
    TextChunk,
    TextDone,
    ToolExecution,
    ToolGroup,
    ToolResultBlock,
)
from rich import box
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.markdown import Heading as _RichHeading
from rich.markdown import ListItem as _RichListItem
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from ._theme import LIGHT_THEME, TerminalTheme, get_theme
from ._tool_renderers import (
    DEFAULT_TOOL_RENDERERS,
    TerminalToolRendererRegistry,
    TerminalToolRenderTheme,
    parse_tool_output,
    prettify_tool_output,
)


@dataclass
class StreamingText:
    """Marker: the host should print this with ``end=""`` for streaming."""

    text: str


@dataclass
class StreamReplace:
    """
    Marker: atomically erase the streamed-text region and render the
    wrapped Rich renderable in its place.

    The formatter emits this at paragraph boundaries (``\\n\\n``)
    and at end-of-response so each completed paragraph is replaced
    with its rendered Markdown view. The host
    (``TerminalHost.replace_streamed_text``) responds by issuing
    one combined ANSI write — cursor-up + erase escapes followed
    by the rendered output — so the terminal repaints once. This
    keeps plain prose (whose Markdown render looks ~identical to
    the streamed raw text) from visibly flickering, while bold /
    lists / inline code / code blocks render correctly.

    :param renderable: The Rich renderable to print in place of
        the cleared lines, e.g.
        ``Padding(Markdown(para_text), (0, 1, 0, 3))``.
    """

    renderable: RenderableType


@dataclass
class StreamLive:
    """
    Marker: clear the current live region and render this in its place.

    The host tracks how many lines the live region occupies so the
    next ``StreamLive`` or ``StreamReplace`` can erase it. Unlike
    ``StreamReplace``, the host does NOT commit (reset its live
    line count to 0) — the rendered content remains in the
    "replaceable" live region.

    Used for the unstable tail of streaming text — the portion
    after the last stable markdown boundary. Re-rendered on every
    ``TextChunk`` via ``Markdown()`` so the user sees formatted
    output from the first token, with unclosed fences / inline
    styles rendered as Rich handles them (CommonMark-compliant).

    :param renderable: The Rich renderable to display in the live
        region, e.g. ``Padding(Markdown(tail_text), (0, 1, 0, 3))``.
    """

    renderable: RenderableType


# Union of items a formatter can return.
FormattedItem = RenderableType | StreamingText | StreamReplace | StreamLive


class _LeftHeading(_RichHeading):
    """Heading subclass that left-aligns instead of Rich's default center."""

    _HEADING_STYLES: dict[str, Style] = {
        "h1": Style(bold=True, italic=True, underline=True),
        "h2": Style(bold=True, underline=True),
        "h3": Style(bold=True),
        "h4": Style(bold=True, italic=True, underline=True, color="#888888"),
        "h5": Style(bold=True, underline=True, color="#888888"),
        "h6": Style(bold=True, color="#888888"),
    }

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        style = self._HEADING_STYLES.get(self.tag)
        if style is not None:
            # Replace Rich's default heading style entirely.
            # No justify — prevents underline from extending to console width.
            yield Text(self.text.plain, style=style)
        else:
            # Unknown tag — fall back to left-aligned plain text so the
            # heading content is never silently swallowed.
            text = self.text
            text.justify = "left"
            yield text


class _AsciiListItem(_RichListItem):
    """ListItem subclass that uses an ASCII ``-`` instead of Rich's ``•`` bullet."""

    def render_bullet(self, console: Console, options: ConsoleOptions) -> RenderResult:
        render_options = options.update(width=options.max_width - 3)
        lines = console.render_lines(self.elements, render_options, style=self.style)
        bullet_style = console.get_style("markdown.item.bullet", default="none")

        bullet = Segment(" - ", bullet_style)
        padding = Segment(" " * 3, bullet_style)
        new_line = Segment("\n")
        first = True
        for line in lines:
            yield bullet if first else padding
            first = False
            yield from line
            yield new_line


# Patch Rich's Markdown element map so all Markdown() instances use left headings
# and ASCII bullets in unordered lists.
Markdown.elements["heading_open"] = _LeftHeading
Markdown.elements["list_item_open"] = _AsciiListItem


class _DiamondMarkdown:
    """Renderable: styled ``◆`` prefix on the same line as Markdown content.

    Intercepts the segment stream from a ``Markdown`` renderable and
    injects a colored ``◆ `` segment before the first visible text.
    This keeps the diamond on the same line as the assistant's first
    word without losing Markdown formatting in the paragraph body.
    """

    __slots__ = ("_diamond_seg", "_md")

    def __init__(self, text: str, diamond_style: str, code_theme: str) -> None:
        self._md = Markdown(text, code_theme=code_theme)
        self._diamond_seg = Segment("◆ ", Style.parse(diamond_style))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        inserted = False
        for segment in console.render(self._md, options):
            if not inserted and segment.text and not segment.control and segment.text.strip():
                yield self._diamond_seg
                inserted = True
            yield segment
        if not inserted:
            yield self._diamond_seg


def _find_stable_markdown_boundary(text: str) -> int:
    """
    Return the char-offset up to which *text* contains only complete Markdown blocks.

    A "stable" prefix is one where every code fence is closed and the text
    ends at a paragraph boundary (blank line). Content past the returned
    offset may still be mid-paragraph or inside an unclosed code fence, so
    it should be kept in a live buffer for re-rendering.

    Copied from ``omnigent/inner/cli.py`` — the SDK cannot import from
    ``omnigent.inner``.

    :param text: The accumulated paragraph buffer text.
    :returns: 0 if no safe boundary is found; otherwise the char-offset
        of the first character after the last safe split point.
    """
    last_safe = 0
    in_fence = False
    i = 0
    n = len(text)
    while i < n:
        nl = text.find("\n", i)
        if nl == -1:
            break
        line = text[i:nl]
        stripped = line.strip()

        # Toggle code-fence state on ``` lines.
        if stripped.startswith("```"):
            in_fence = not in_fence

        # A blank line outside a fence marks a paragraph boundary.
        if stripped == "" and not in_fence:
            candidate = nl + 1
            if candidate < n:
                last_safe = candidate

        i = nl + 1

    return last_safe


def _wrap_shell_command(
    cmd: str,
    *,
    prefix_len: int = 0,
    indent: str = "    ",
    min_width: int = 60,
    fallback_width: int = 100,
) -> str:
    """Wrap a shlex-joined shell command with backslash line-continuations.

    Breaks occur at long-flag (``--flag``) boundaries; each flag plus its
    value stays on the same continuation line. Lines are kept under the
    current terminal width (or *fallback_width* if it can't be detected).

    :param cmd: The full command as a single shlex-joined string.
    :param prefix_len: Number of visible characters that precede the
        command on its first line (e.g. ``len("  Resume: ")``). Subtracted
        from the width budget for the first line so the leading label
        is accounted for.
    :param indent: Leading whitespace for continuation lines.
    :param min_width: Floor on the wrapping budget — very narrow terminals
        still get one segment per line rather than a single overflowing one.
    :param fallback_width: Used when ``shutil.get_terminal_size`` returns
        a non-positive value (headless / non-tty contexts).
    """
    width = shutil.get_terminal_size((fallback_width, 24)).columns
    if width <= 0:
        width = fallback_width
    width = max(width, min_width)

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return cmd
    if not tokens:
        return cmd

    # Group tokens into segments split at each long-flag boundary.
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok.startswith("--") and current:
            segments.append(current)
            current = [tok]
        else:
            current.append(tok)
    if current:
        segments.append(current)

    # Greedy pack: each line gets as many segments as fit. The first line's
    # budget is reduced by ``prefix_len``; continuation lines pay ``indent``.
    # Reserve 2 chars for the trailing ``" \"`` continuation marker.
    lines: list[str] = []
    line = ""
    first = True
    for seg in segments:
        piece = shlex.join(seg)
        if not line:
            line = piece if first else f"{indent}{piece}"
            continue
        budget = width - (prefix_len if first else 0) - 2
        if len(line) + 1 + len(piece) <= budget:
            line = f"{line} {piece}"
        else:
            lines.append(line)
            first = False
            line = f"{indent}{piece}"
    if line:
        lines.append(line)
    return " \\\n".join(lines)


class RichBlockFormatter:
    """Converts stream blocks to Rich renderables.

    :param accent_color: Brand accent for branding (default magenta-pink).
    :param code_theme: Pygments theme for code blocks.
    :param max_result_lines: Max lines in tool result panels.
    :param max_result_chars: Max characters in tool result panels.
        Guards against single-line monsters where the line cap doesn't
        help, e.g. JSON-stringified terminal scrollbacks where every
        newline is escaped to ``\\n`` and the entire payload is one
        physical line. Counted after JSON pretty-printing.
    :param show_agent_labels: Prefix sub-agent blocks with agent name.
    """

    def __init__(
        self,
        *,
        accent_color: str = "#F43BA6",
        code_theme: str | None = None,
        max_result_lines: int = 30,
        max_result_chars: int = 2000,
        show_agent_labels: bool = False,
        show_tool_output: bool = False,
        tool_renderers: TerminalToolRendererRegistry | None = None,
        theme: TerminalTheme | str = LIGHT_THEME,
    ) -> None:
        self.accent = accent_color
        self.max_result_lines = max_result_lines
        self.max_result_chars = max_result_chars
        self.show_agent_labels = show_agent_labels
        self.show_tool_output = show_tool_output
        self.tool_renderers = tool_renderers or DEFAULT_TOOL_RENDERERS
        self.theme = get_theme(theme) if isinstance(theme, str) else theme
        self.code_theme = code_theme or self.theme.code_theme

        # Cross-chunk text state for incremental Markdown rendering.
        # Each ``TextChunk`` appends here, then the formatter splits
        # the buffer into a committed region (stable, complete
        # Markdown blocks emitted as ``StreamReplace``) and a live
        # region (the unstable tail emitted as ``StreamLive``).
        # ``_committed_offset`` tracks how far into the buffer has
        # already been committed — only content past this offset is
        # eligible for the next commit or live render.
        # ``format_response_start`` resets both so a new turn never
        # inherits a prior turn's leftover (e.g. after cancellation
        # or an error mid-paragraph).
        self._paragraph_buffer: str = ""
        self._committed_offset: int = 0
        self._needs_diamond: bool = False

        # Tool-call dedup state. The sessions API emits a
        # ``function_call`` item twice per tool call — once at
        # dispatch (status ``in_progress`` / ``action_required``)
        # and again at completion (status ``completed``). Without
        # dedup the ``⏵ tool_name(args)`` call line renders twice.
        # ``format_tool_group`` records each ``call_id`` here and
        # skips the call line on the second occurrence.
        # ``format_response_start`` resets the set so a new turn
        # starts with a clean slate.
        self._seen_tool_call_ids: set[str] = set()

        # Derived styles.
        self._apply_theme_styles()

    def set_theme(self, theme: TerminalTheme | str) -> None:
        """Switch formatter colors and Markdown code-block theme."""
        self.theme = get_theme(theme) if isinstance(theme, str) else theme
        self.code_theme = self.theme.code_theme
        self._apply_theme_styles()

    def _apply_theme_styles(self) -> None:
        self.assistant = self.theme.assistant
        self.muted = self.theme.muted
        self.warning = self.theme.warning
        self.error = self.theme.error
        self.success = self.theme.success
        self.reasoning_style = self.theme.reasoning_style

    # ── Main dispatch ────────────────────────────────────

    def format(self, block: AnyBlock) -> list[FormattedItem]:
        """Format a block into display items."""
        items = self._dispatch(block)
        if self.show_agent_labels and block.ctx.depth > 0:
            label = Text.from_markup(f"   [{self.muted}][{block.ctx.agent}][/{self.muted}]")
            return [label, *items]
        return items

    def _dispatch(self, block: AnyBlock) -> list[FormattedItem]:
        if isinstance(block, ResponseStartBlock):
            return self.format_response_start(block)
        if isinstance(block, TextChunk):
            return self.format_text_chunk(block)
        if isinstance(block, TextDone):
            return self.format_text_done(block)
        if isinstance(block, ToolGroup):
            return self.format_tool_group(block)
        if isinstance(block, ToolResultBlock):
            return self.format_tool_result(block)
        if isinstance(block, NativeToolBlock):
            return self.format_native_tool(block)
        if isinstance(block, ReasoningStartBlock):
            return self.format_reasoning_start(block)
        if isinstance(block, ReasoningChunk):
            return self.format_reasoning_chunk(block)
        if isinstance(block, ReasoningBlock):
            return self.format_reasoning(block)
        if isinstance(block, ErrorBlock):
            return self.format_error(block)
        if isinstance(block, RetryBlock):
            return self.format_retry(block)
        if isinstance(block, CompactionBlock):
            return self.format_compaction(block)
        if isinstance(block, FileBlock):
            return self.format_file(block)
        if isinstance(block, ResponseEndBlock):
            return self.format_response_end(block)
        return []

    # ── Override points ──────────────────────────────────

    def format_response_start(self, block: ResponseStartBlock) -> list[FormattedItem]:
        # Reset the paragraph buffer and committed offset at the
        # start of each response so leftover state from a prior
        # turn (e.g. cancellation mid-paragraph, or an error
        # before TextDone) doesn't bleed into the new turn's
        # first rendered paragraph.
        self._paragraph_buffer = ""
        self._committed_offset = 0
        self._seen_tool_call_ids = set()
        self._needs_diamond = True
        return [Text("")]

    def format_text_chunk(self, block: TextChunk) -> list[FormattedItem]:
        """
        Render a text chunk incrementally via the two-region streaming model.

        Appends the chunk to ``_paragraph_buffer``, then splits into a
        committed region (stable, complete Markdown blocks up to the
        boundary returned by :func:`_find_stable_markdown_boundary`)
        and a live region (the unstable tail). Newly stable content is
        emitted as ``StreamReplace``; the tail is emitted as
        ``StreamLive`` so the host re-renders it on the next token.

        No ``StreamingText`` is emitted for text content — all text is
        rendered through ``Markdown()`` from the first token. Raw
        markdown syntax (``**bold**``, fenced code, etc.) is never
        visible to the user.

        :param block: The streaming text chunk, e.g.
            ``TextChunk(text="Hello **world")``.
        :returns: A list of ``StreamReplace`` and/or ``StreamLive``
            items in the order the host should apply them.
        """
        self._paragraph_buffer += block.text
        items: list[FormattedItem] = []
        boundary = _find_stable_markdown_boundary(self._paragraph_buffer)
        if boundary > self._committed_offset:
            stable = self._paragraph_buffer[self._committed_offset : boundary]
            if stable.strip():
                # When the diamond is pending, the first paragraph
                # needs (0,1,0,1) padding while the rest need
                # (0,1,0,3).  Split at the first blank-line boundary
                # so only the opening paragraph gets diamond styling.
                if self._needs_diamond:
                    split = stable.find("\n\n")
                    if split != -1:
                        first = stable[: split + 2]
                        rest = stable[split + 2 :]
                        items.append(self._markdown_replace(first))
                        if rest.strip():
                            items.append(self._markdown_replace(rest))
                    else:
                        items.append(self._markdown_replace(stable))
                else:
                    items.append(self._markdown_replace(stable))
            self._committed_offset = boundary
        tail = self._paragraph_buffer[self._committed_offset :]
        if tail.strip():
            items.append(self._render_tail(tail))
        return items

    def format_text_done(self, block: TextDone) -> list[FormattedItem]:
        """
        Flush remaining text at end of response.

        Commits everything left in the paragraph buffer (past
        ``_committed_offset``) as a final ``StreamReplace``. If the
        buffer is empty (response ended on a boundary, or had no text),
        emits nothing — the live region is already clear.

        :param block: The ``TextDone`` block. Unused — the trailing
            text (if any) is flushed from ``self._paragraph_buffer``,
            which prior ``format_text_chunk`` calls populated; the
            legacy ``block.has_code_blocks`` / ``block.full_text``
            fields are no longer consulted because incremental
            rendering handles fenced code blocks naturally. The
            parameter is retained because the dispatch protocol
            requires a uniform ``format_<kind>(self, block)``
            signature; ``ARG002`` is globally suppressed for
            ``sdks/**/*.py`` (see ``pyproject.toml``).
        :returns: A list with zero or one ``StreamReplace`` item for
            the trailing text.
        """
        leftover = self._paragraph_buffer[self._committed_offset :]
        self._paragraph_buffer = ""
        self._committed_offset = 0
        if leftover.strip():
            return [self._markdown_replace(leftover)]
        return []

    def format_message_done(self) -> list[FormattedItem]:
        """
        Commit in-flight streamed text and prepare for a follow-up
        message within the same response.

        Called mid-response when an assistant message item completes
        (e.g. on an ``OutputItemDone`` SSE event of type ``"message"``
        whose role is ``"assistant"``). A single response can contain
        multiple assistant messages interleaved with tool calls
        — without this flush, the next message's
        :meth:`format_text_chunk` calls would append to the prior
        message's ``_paragraph_buffer`` and the live render would
        show both messages concatenated (e.g.
        ``"Hi!I'll take a quick…"``).

        Differs from :meth:`format_text_done` (which is invoked at
        end-of-response by the ``SessionStatusEvent`` ``"idle"``
        handler) in that it also restores ``_needs_diamond`` to
        ``True`` so the next message in the same response renders
        its own ``◆`` header — matching the resume-rendering
        convention where each assistant message gets its own
        ``◆ <model>`` line.

        :returns: A list with zero or one :class:`StreamReplace`
            item for the trailing text of the just-completed
            message. Empty if the message ended on a paragraph
            boundary (nothing left to commit).
        """
        leftover = self._paragraph_buffer[self._committed_offset :]
        self._paragraph_buffer = ""
        self._committed_offset = 0
        items: list[FormattedItem] = []
        if leftover.strip():
            items.append(self._markdown_replace(leftover))
        # Restore the diamond so the next message in the same
        # response renders its own ◆ header.
        self._needs_diamond = True
        return items

    def _markdown_replace(self, paragraph_text: str) -> StreamReplace:
        """
        Wrap paragraph text in a ``StreamReplace`` whose renderable is
        the project's standard Padded Markdown panel.

        Centralized so every paragraph render uses the same padding
        (``(0, 1, 0, 3)``) and code theme — drift here means paragraphs
        rendered mid-stream wouldn't visually match the trailing
        paragraph rendered at ``format_text_done``.

        :param paragraph_text: The full paragraph text (without trailing
            ``\\n\\n``), e.g. ``"**Bold** and *italic*"``.
        :returns: A ``StreamReplace`` ready for the host to apply.
        """
        if self._needs_diamond:
            self._needs_diamond = False
            return StreamReplace(
                renderable=Padding(
                    _DiamondMarkdown(paragraph_text, self.assistant, self.code_theme),
                    (0, 1, 0, 1),
                )
            )
        # Non-diamond renders always follow prior content (the diamond
        # paragraph at minimum), so add 1 top padding to reproduce the
        # blank-line gap Rich inserts between blocks within a single
        # Markdown() render.  Without this, separate StreamReplace /
        # StreamLive renders would abut with no gap.
        return StreamReplace(
            renderable=Padding(
                Markdown(paragraph_text, code_theme=self.code_theme),
                (1, 1, 0, 3),
            )
        )

    def _render_tail(self, tail_text: str) -> StreamLive:
        """
        Wrap the unstable tail text in a ``StreamLive`` marker.

        Rich's ``Markdown()`` handles unclosed fences natively —
        markdown-it-py extends unclosed fences to EOF per the CommonMark
        spec, so no synthetic closing fence is needed.

        :param tail_text: The text after the last committed boundary,
            e.g. ``"```python\\ndef foo"`` (unclosed fence).
        :returns: A ``StreamLive`` whose renderable is a padded
            Markdown panel matching the committed-region style.
        """
        if self._needs_diamond:
            return StreamLive(
                renderable=Padding(
                    _DiamondMarkdown(tail_text, self.assistant, self.code_theme),
                    (0, 1, 0, 1),
                )
            )
        top = 1 if self._committed_offset > 0 else 0
        return StreamLive(
            renderable=Padding(
                Markdown(tail_text, code_theme=self.code_theme),
                (top, 1, 0, 3),
            )
        )

    def format_tool_group(self, block: ToolGroup) -> list[FormattedItem]:
        items: list[FormattedItem] = []
        for ex in block.executions:
            # Deduplicate by call_id. The sessions API emits a
            # ``function_call`` item twice per tool call (once at
            # dispatch, once at completion). Without this guard the
            # ``⏵ tool_name(args)`` line renders twice.
            if ex.call_id not in self._seen_tool_call_ids:
                self._seen_tool_call_ids.add(ex.call_id)
                items.append(self._tool_call_line(ex))
            if ex.output is not None and self.show_tool_output:
                items.append(self._tool_result_panel(ex))
        return items

    def format_tool_result(self, block: ToolResultBlock) -> list[FormattedItem]:
        """Render a tool result panel (no call line — already displayed)."""
        if not self.show_tool_output:
            return []
        ex = ToolExecution(
            name=block.name,
            arguments=block.arguments,
            # args_summary is not displayed for result-only panels,
            # but is required by ToolExecution.
            args_summary=block.args_summary,
            call_id=block.call_id,
            agent_name=block.agent_name,
            output=block.output,
        )
        return [self._tool_result_panel(ex)]

    def format_native_tool(self, block: NativeToolBlock) -> list[FormattedItem]:
        specialized = self.tool_renderers.render_native(block, self._tool_render_theme())
        if specialized is not None:
            return [specialized]
        return [Text.from_markup(f"   [{self.accent}]⏵ {block.label}[/{self.accent}]")]

    def format_reasoning_start(self, block: ReasoningStartBlock) -> list[FormattedItem]:
        return [
            Text.from_markup(
                f"   [{self.accent}]·[/{self.accent}] [{self.muted}]thinking…[/{self.muted}]"
            )
        ]

    def format_reasoning_chunk(self, block: ReasoningChunk) -> list[FormattedItem]:
        # Stream reasoning live (incremental, like TextChunk) so users
        # see Codex's commands / model reasoning during the tool-call
        # window instead of staring at a blank screen for 30 s. Wrap
        # in dim ANSI codes so reasoning visually distinguishes from
        # the agent's final prose, which is rendered un-styled.
        return [StreamingText(text=f"\x1b[2m{block.text}\x1b[0m")]

    def format_reasoning(self, block: ReasoningBlock) -> list[FormattedItem]:
        text = block.summary_text or block.reasoning_text
        if text.strip():
            return [self._reasoning_panel(text)]
        return []

    def format_error(self, block: ErrorBlock) -> list[FormattedItem]:
        # Assemble a body that always has SOMETHING actionable.
        # Message is the ideal content; when the server emits
        # ``response.error`` without populating it (or the client
        # drops it), fall back to ``code`` so the user sees the
        # classification instead of a blank red panel next to the
        # bare source label. Sentinel is an absolute last resort —
        # users reported a "red box with just `llm`" symptom that
        # stemmed from both fields being empty.
        src = f"[{block.source}] " if block.source else ""
        body = block.message or getattr(block, "code", "") or "(error with no details)"
        return [
            Padding(
                Panel(
                    Text(f"{src}{body}", style=self.error),
                    border_style=self.error,
                    box=box.ROUNDED,
                    padding=(0, 1),
                ),
                (0, 1, 0, 3),
            )
        ]

    def format_retry(self, block: RetryBlock) -> list[FormattedItem]:
        return [
            Text.from_markup(
                f"   [{self.warning}]↻ retrying {block.source}"
                f" ({block.attempt}/{block.max_attempts})…[/{self.warning}]"
            )
        ]

    def format_compaction(self, block: CompactionBlock) -> list[FormattedItem]:
        return [Text.from_markup(f"   [{self.muted}]◐ compacting…[/{self.muted}]")]

    def format_file(self, block: FileBlock) -> list[FormattedItem]:
        name = block.filename or block.file_id
        return [Text.from_markup(f"   [{self.success}]📎 {name}[/{self.success}]")]

    def format_response_end(self, block: ResponseEndBlock) -> list[FormattedItem]:
        if block.status == "completed":
            return []
        return [Text.from_markup(f"   [{self.warning}]{block.status}[/{self.warning}]")]

    # ── Non-block helpers ────────────────────────────────

    def welcome(self, model: str, hints: list[str] | None = None) -> FormattedItem:
        """
        The welcome banner shown at REPL start.

        :param model: Display name for the agent/model, rendered
            next to the brand, e.g. ``"coding supervisor"``.
        :param hints: Optional list of key-binding hints shown on
            the second line, in order. Each entry is a short
            human-readable label like ``"/help help"`` or
            ``"Ctrl+O debug"``. When ``None``, defaults to the
            baseline set the SDK ships (``/help help``, ``/quit exit``,
            ``Esc cancel``, ``Ctrl+C exit``). Callers that register
            extra overlays should pass the full list so the hint
            reflects what's actually bound.
        """
        if hints is None:
            hints = ["/help help", "/quit exit", "Esc cancel", "Ctrl+C exit"]
        # The lead already points at /help, so drop any redundant /help hint
        # from the joined remainder — avoids advertising help twice while
        # keeping /quit and the key hints visible.
        rest = [h for h in hints if not h.lstrip("/").lower().startswith("help")]
        hint_line = "Type a message, or /help for commands"
        if rest:
            hint_line += " · " + " · ".join(rest)
        return Panel(
            Text.from_markup(
                f"[{self.accent}]Omnigent[/{self.accent}]"
                f"  [{self.muted}]·[/{self.muted}]  [bold]{model}[/bold]\n"
                f"[{self.muted}]{hint_line}[/{self.muted}]"
            ),
            box=box.ROUNDED,
            border_style=self.accent,
            padding=(0, 1),
        )

    def user_message(
        self,
        text: str,
        attachments: list[str] | None = None,
    ) -> FormattedItem:
        """
        Format a user message with the accent marker.

        Renders ``❯ <text>`` using only foreground styles so the
        echo card adapts to whatever background the terminal
        provides. The accent ``❯`` is enough visual distinction
        from the agent's response without imposing a specific
        background color — a hardcoded dark background ``#1a1a1a``
        looked like a glaring black blob on light-themed terminals
        (iTerm light theme, ``xterm`` defaults, etc.) where the
        user's typed prompt is supposed to read normally.

        :param text: The user's message text.
        :param attachments: Optional list of attachment filenames to
            display alongside the message.
        """
        escaped = text.replace("[", "\\[").replace("]", "\\]")
        parts = f"\n [{self.accent}]❯[/{self.accent}]"
        if escaped:
            parts += f" {escaped}"
        if attachments:
            for name in attachments:
                safe = name.replace("[", "\\[").replace("]", "\\]")
                parts += f" [{self.muted}]📎 {safe}[/{self.muted}]"
        return Text.from_markup(parts)

    def steering_message(
        self,
        text: str,
        attachments: list[str] | None = None,
    ) -> FormattedItem:
        """Format a mid-stream steering message in muted style.

        Same escaping and truncation as :meth:`user_message` but
        rendered entirely in :attr:`muted` so it's visually
        subordinate to the agent's in-progress output.

        :param text: The user's steering text.
        :param attachments: Optional list of attachment filenames to
            display alongside the message.
        """
        truncated = text
        lines = text.split("\n")
        if len(lines) > 4:
            truncated = "\n".join(lines[:4]) + f"\n… {len(lines) - 4} more lines"
        escaped = truncated.replace("[", "\\[").replace("]", "\\]")
        parts = f" [{self.muted}]❯[/{self.muted}]"
        if escaped:
            parts += f" [{self.muted}]{escaped}[/{self.muted}]"
        if attachments:
            for name in attachments:
                safe = name.replace("[", "\\[").replace("]", "\\]")
                parts += f" [{self.muted}]📎 {safe}[/{self.muted}]"
        return Text.from_markup(parts)

    def goodbye(self, *, resume_hint: str | None = None) -> FormattedItem:
        """Goodbye message, optionally followed by a resume command hint.

        :param resume_hint: When set, a copy-pasteable shell command
            rendered in dim style below the "Goodbye." line, e.g.
            ``"omnigent run agent.yaml --resume conv_abc123"``.
            ``None`` (default) omits the hint.
        """
        if resume_hint is None:
            return Text.from_markup(f"\n  [{self.muted}]Goodbye.[/{self.muted}]\n")
        wrapped = _wrap_shell_command(resume_hint, prefix_len=len("  Resume: "))
        safe = wrapped.replace("[", "\\[").replace("]", "\\]")
        return Text.from_markup(
            f"\n  [{self.muted}]Goodbye.[/{self.muted}]"
            f"\n  [{self.muted}]Resume: {safe}[/{self.muted}]\n"
        )

    # ── Internal builders ────────────────────────────────

    def _tool_call_line(self, ex: ToolExecution) -> FormattedItem:
        color = self.accent
        prefix = ""
        if "." in ex.agent_name:
            prefix = f"[{self.muted}]{ex.agent_name} → [/{self.muted}]"
        args = ex.args_summary
        return Text.from_markup(f"   {prefix}[{color}]⏵ {ex.name}[/{color}][dim]({args})[/dim]")

    def _tool_result_panel(self, ex: ToolExecution) -> FormattedItem:
        raw = ex.output or ""
        parsed = parse_tool_output(raw)
        specialized = self.tool_renderers.render_tool(ex, parsed, self._tool_render_theme())
        if specialized is not None:
            return specialized
        output = prettify_tool_output(raw)
        lines = output.split("\n")
        omitted_lines = max(0, len(lines) - self.max_result_lines)
        visible = "\n".join(lines[: self.max_result_lines]) if omitted_lines else output
        omitted_chars = max(0, len(visible) - self.max_result_chars)
        if omitted_chars:
            visible = visible[: self.max_result_chars]
        notes: list[str] = []
        if omitted_lines:
            notes.append(f"{omitted_lines} more lines")
        if omitted_chars:
            notes.append(f"{omitted_chars} more chars")
        footer = f"\n[{self.muted}]… {' · '.join(notes)}[/{self.muted}]" if notes else ""
        first_line = lines[0][:80] if lines else ""
        escaped_fl = first_line.replace("[", "\\[").replace("]", "\\]")
        escaped_vis = visible.replace("[", "\\[").replace("]", "\\]")
        return Padding(
            Panel(
                Text.from_markup(f"[dim]{escaped_vis}{footer}[/dim]"),
                title=f"[dim]{escaped_fl}[/dim]",
                title_align="left",
                border_style=self.accent,
                box=box.ROUNDED,
                padding=(0, 1),
            ),
            (0, 1, 0, 3),
        )

    def _tool_render_theme(self) -> TerminalToolRenderTheme:
        return TerminalToolRenderTheme(
            accent=self.accent,
            muted=self.muted,
            warning=self.warning,
            error=self.error,
            success=self.success,
            code_theme=self.code_theme,
            max_result_lines=self.max_result_lines,
            max_result_chars=self.max_result_chars,
        )

    def _prettify_tool_output(self, raw: str) -> str:
        """Compatibility wrapper for tests/custom subclasses."""
        return prettify_tool_output(raw)

    def _reasoning_panel(self, text: str) -> FormattedItem:
        lines = text.strip().split("\n")
        if len(lines) > 8:
            preview = "\n".join(lines[-8:])
            preview = f"[{self.muted}]… {len(lines) - 8} earlier lines[/{self.muted}]\n" + preview
        else:
            preview = "\n".join(lines)
        escaped = preview.replace("[", "\\[").replace("]", "\\]")
        return Padding(
            Panel(
                Text.from_markup(f"[{self.reasoning_style}]{escaped}[/{self.reasoning_style}]"),
                title=f"[{self.muted}]thinking[/{self.muted}]",
                title_align="left",
                border_style=self.muted,
                box=box.ROUNDED,
                padding=(0, 1),
            ),
            (0, 1, 0, 3),
        )
