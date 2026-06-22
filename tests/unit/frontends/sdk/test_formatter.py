"""Unit tests for RichBlockFormatter."""

from __future__ import annotations

import io
import json

import pytest
from omnigent_client._blocks import (
    BlockContext,
    CompactionBlock,
    ErrorBlock,
    FileBlock,
    ReasoningBlock,
    ReasoningStartBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    RetryBlock,
    TextChunk,
    TextDone,
    ToolExecution,
    ToolGroup,
)
from omnigent_ui_sdk.terminal._formatter import (
    RichBlockFormatter,
    StreamingText,
    StreamLive,
    StreamReplace,
    _LeftHeading,
)
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.style import Style
from rich.text import Text


def test_format_response_start() -> None:
    """ResponseStartBlock → newline, sets diamond flag for first text chunk."""
    fmt = RichBlockFormatter()
    items = fmt.format(ResponseStartBlock(model="coder", response_id="r1"))
    assert len(items) == 1
    # Should be a blank newline separator — the ◆ diamond is deferred
    # until the first text chunk so it appears on the same line as the
    # assistant's response.
    assert isinstance(items[0], Text)
    assert fmt._needs_diamond is True


def test_format_text_chunk_returns_stream_live() -> None:
    """TextChunk → StreamLive marker (rendered Markdown, not raw text)."""
    fmt = RichBlockFormatter()
    items = fmt.format(TextChunk(text="hello "))
    # A single StreamLive for the unstable tail — no StreamingText for
    # text content in the new two-region model.
    assert len(items) == 1, (
        f"Expected 1 item (StreamLive for the unstable tail), got {len(items)}: {items!r}."
    )
    assert isinstance(items[0], StreamLive), (
        f"Expected StreamLive (incremental Markdown render), got "
        f"{type(items[0]).__name__}. If StreamingText, the formatter "
        f"is using the old raw-text streaming path."
    )


def test_format_text_done_empty_buffer_returns_nothing() -> None:
    """
    TextDone with an empty paragraph buffer returns no items.

    In the two-region model, all text is rendered through Markdown
    from the first token (via ``StreamLive``). When the buffer is
    empty at ``TextDone`` (response ended on a boundary, or had no
    text), there's nothing left to commit — the live region was
    already cleared by the last ``StreamReplace``.
    """
    fmt = RichBlockFormatter()
    items = fmt.format(TextDone(full_text="just text", has_code_blocks=False))
    assert items == [], (
        f"Expected empty list when paragraph buffer is empty at TextDone, got {items!r}."
    )


def test_text_done_flushes_buffered_paragraph_as_stream_replace() -> None:
    """
    A trailing paragraph (no closing ``\\n\\n``) streamed via TextChunks
    is committed at TextDone via :class:`StreamReplace`, which the host
    interprets as "atomically clear the live region and render this
    Rich renderable in its place, committed".

    The legacy ``TextDone.has_code_blocks`` field is ignored — the
    formatter renders every paragraph through the same Markdown panel
    path because incremental rendering handles fenced code blocks
    naturally.
    """
    fmt = RichBlockFormatter()
    # Stream a paragraph with a fenced code block. The formatter
    # accumulates this into ``_paragraph_buffer``.
    fmt.format(TextChunk(text="```python\nprint('hi')\n```"))

    # End-of-response — the uncommitted buffer contents should be
    # emitted as a StreamReplace (commit).
    items = fmt.format(TextDone(full_text="```python\nprint('hi')\n```", has_code_blocks=True))
    assert len(items) == 1, (
        f"Expected exactly one item from format_text_done with non-empty "
        f"buffer, got {len(items)}: {items!r}. The contract is: TextDone "
        f"flushes the trailing text as a single StreamReplace."
    )
    assert isinstance(items[0], StreamReplace), (
        f"Expected StreamReplace (atomic commit) for the trailing "
        f"paragraph, got {type(items[0]).__name__}. If StreamLive, the "
        f"formatter didn't commit the remaining text at end-of-response."
    )


def test_paragraph_boundary_emits_stream_replace_mid_chunk() -> None:
    """
    A ``\\n\\n`` boundary inside a single chunk causes the formatter to
    emit a ``StreamReplace`` for the just-completed paragraph (committed)
    followed by a ``StreamLive`` for the unstable tail.

    In the two-region model: stable prefix → ``StreamReplace``,
    unstable tail → ``StreamLive``. No raw ``StreamingText`` is
    emitted for text content.
    """
    fmt = RichBlockFormatter()
    items = fmt.format(TextChunk(text="Para 1.\n\nPara 2 starts"))

    # Two items: StreamReplace for the committed "Para 1." paragraph,
    # StreamLive for the unstable "Para 2 starts" tail.
    assert len(items) == 2, (
        f"Expected 2 items (StreamReplace for committed para, StreamLive "
        f"for tail), got {len(items)}: {items!r}."
    )
    assert isinstance(items[0], StreamReplace), (
        f"First item must be StreamReplace (committed stable paragraph), "
        f"got {type(items[0]).__name__}."
    )
    assert isinstance(items[1], StreamLive), (
        f"Second item must be StreamLive (unstable tail), got {type(items[1]).__name__}."
    )


def test_paragraph_boundary_spanning_two_chunks() -> None:
    """
    A paragraph boundary split across chunks (``...\\n`` then ``\\n...``)
    is correctly recognized: the first chunk gets only a ``StreamLive``
    (unstable — no boundary yet), and the second chunk emits a
    ``StreamReplace`` (commits the paragraph) + ``StreamLive`` (new tail).

    This exercises the boundary detection when the ``\\n\\n`` straddles
    the chunk boundary.
    """
    fmt = RichBlockFormatter()
    items1 = fmt.format(TextChunk(text="Para 1.\n"))
    items2 = fmt.format(TextChunk(text="\nPara 2"))

    # First chunk: no boundary yet → just a StreamLive for the tail.
    assert len(items1) == 1, (
        f"Expected 1 item from the first chunk (no boundary yet), got {len(items1)}: {items1!r}."
    )
    assert isinstance(items1[0], StreamLive), (
        f"Expected first-chunk item to be StreamLive (unstable tail), "
        f"got {type(items1[0]).__name__}."
    )

    # Second chunk closes the boundary: StreamReplace (commits para 1)
    # + StreamLive (new tail "Para 2").
    assert len(items2) == 2, (
        f"Expected 2 items for the boundary-closing chunk "
        f"(StreamReplace + StreamLive), got {len(items2)}: {items2!r}."
    )
    assert isinstance(items2[0], StreamReplace), (
        f"Expected items2[0] to be StreamReplace (committed paragraph), "
        f"got {type(items2[0]).__name__}."
    )
    assert isinstance(items2[1], StreamLive), (
        f"Expected items2[1] to be StreamLive (unstable tail 'Para 2'), "
        f"got {type(items2[1]).__name__}."
    )


def test_format_message_done_flushes_buffer_and_restores_diamond() -> None:
    """
    ``format_message_done`` commits the in-flight tail as a
    ``StreamReplace``, clears ``_paragraph_buffer`` /
    ``_committed_offset``, and restores ``_needs_diamond`` to
    ``True`` so a follow-up message in the same response renders
    its own ``◆`` header.

    If the leftover is not committed, a follow-up
    :meth:`format_text_chunk` would render it again concatenated
    with the new text — the duplicated-text bug. If
    ``_needs_diamond`` were not restored, the follow-up message
    would render without a diamond header, breaking the
    "one ◆ per message" convention shared with the resume
    renderer.
    """
    fmt = RichBlockFormatter()
    fmt.format(ResponseStartBlock(model="coder", response_id="r1"))
    # Stream a single-paragraph message (no ``\\n\\n``), so the
    # whole text stays in the unstable tail (StreamLive).
    fmt.format(TextChunk(text="Hi! What would you like to work on?"))

    items = fmt.format_message_done()

    # The trailing tail is committed exactly once: one StreamReplace
    # with the full message text. If 0, the leftover was dropped
    # (message 1's prose would visually vanish). If >1, the flush
    # path duplicated the commit.
    assert len(items) == 1, (
        f"Expected 1 StreamReplace from format_message_done with a "
        f"non-empty buffer, got {len(items)}: {items!r}."
    )
    assert isinstance(items[0], StreamReplace), (
        f"Expected StreamReplace (atomic commit) for the trailing "
        f"text, got {type(items[0]).__name__}. If StreamLive, the "
        f"flush did not commit the tail."
    )
    # Buffer and offset reset so the next format_text_chunk starts
    # from an empty buffer.
    assert fmt._paragraph_buffer == "", (
        f"Expected _paragraph_buffer reset to empty string, got "
        f"{fmt._paragraph_buffer!r}. If non-empty, the next "
        f"message's deltas would append to this and render the "
        f"two messages concatenated."
    )
    assert fmt._committed_offset == 0, (
        f"Expected _committed_offset reset to 0, got {fmt._committed_offset}."
    )
    # The diamond was consumed when ``_markdown_replace`` produced
    # the commit; ``format_message_done`` restores it so message 2
    # renders its own ◆.
    assert fmt._needs_diamond is True, (
        "Expected _needs_diamond restored to True after "
        "format_message_done. If False, message 2 in the same "
        "response would render without a ◆ header."
    )


def test_format_message_done_empty_buffer_only_restores_diamond() -> None:
    """
    When the paragraph buffer is empty (message ended on a
    boundary, or had no text), ``format_message_done`` returns
    no items but still restores ``_needs_diamond``.
    """
    fmt = RichBlockFormatter()
    fmt.format(ResponseStartBlock(model="coder", response_id="r1"))
    # No TextChunks → buffer is empty. The previous response start
    # already set _needs_diamond True; consume it manually to
    # mimic a prior committed paragraph having already cleared it.
    fmt._needs_diamond = False

    items = fmt.format_message_done()

    assert items == [], f"Expected no items when buffer is empty, got {items!r}."
    assert fmt._needs_diamond is True, (
        "Expected _needs_diamond restored even when there is no leftover to commit."
    )


def test_streamed_message_then_message_done_then_streamed_message_isolated() -> None:
    """
    Reproduces the duplicated-text bug: within one response, a
    second streamed message must not render the prior message's
    text concatenated with its own.

    Event sequence (single response, two assistant messages with
    tool calls between them):
    1. ``ResponseStartBlock``
    2. Stream "Hi! What would you like to work on?" as TextChunks
    3. ``format_message_done()`` (assistant message done)
    4. Stream "I'll take a quick inventory." as TextChunks

    Without ``format_message_done`` between messages, step 4
    appends to the prior message's ``_paragraph_buffer`` and the
    StreamLive renderable contains both messages' text. With the
    fix, step 4's StreamLive contains only message 2's text.
    """
    fmt = RichBlockFormatter()
    fmt.format(ResponseStartBlock(model="coder", response_id="r1"))

    # Message 1 — single paragraph, no ``\\n\\n``, so stays in tail.
    fmt.format(TextChunk(text="Hi! What would you like to work on?"))

    # Message 1 done → flush the tail, reset buffer, restore diamond.
    flush_items = fmt.format_message_done()
    rendered_flush = _render(flush_items[0].renderable)
    # Sanity: the committed paragraph contains exactly message 1's text.
    assert "Hi! What would you like to work on?" in rendered_flush, (
        f"Expected message 1's text committed in the flush, got {rendered_flush!r}."
    )

    # Message 2 — first delta arrives after tool calls.
    items = fmt.format(TextChunk(text="I'll take a quick inventory."))

    # Exactly one StreamLive for the new tail.
    assert len(items) == 1, (
        f"Expected 1 StreamLive for message 2's tail, got {len(items)}: {items!r}."
    )
    assert isinstance(items[0], StreamLive), (
        f"Expected StreamLive for the unstable tail, got {type(items[0]).__name__}."
    )

    rendered = _render(items[0].renderable)
    # The bug: rendered would contain "Hi! What would you like to
    # work on?" concatenated with "I'll take a quick inventory."
    # This assertion proves message 2 rendered in isolation.
    assert "Hi!" not in rendered, (
        f"Message 2's StreamLive must not contain message 1's text. "
        f"If 'Hi!' appears, format_message_done did not reset the "
        f"paragraph buffer and the two messages are rendering "
        f"concatenated. Got: {rendered!r}."
    )
    assert "I'll take a quick inventory" in rendered, (
        f"Message 2's StreamLive must contain message 2's text. Got: {rendered!r}."
    )


def test_format_response_start_resets_paragraph_buffer() -> None:
    """
    A new ``ResponseStartBlock`` resets the paragraph buffer and
    committed offset so leftover state from a prior turn (e.g.
    cancellation mid-paragraph, or an error before TextDone) doesn't
    bleed into the new turn's first rendered paragraph.
    """
    fmt = RichBlockFormatter()
    # Plant leftover content (simulates a cancelled prior turn).
    fmt.format(TextChunk(text="leftover from prior turn"))

    # Start a new response.
    fmt.format(ResponseStartBlock(model="coder", response_id="r2"))

    # The next TextDone should return nothing — buffer was reset.
    items = fmt.format(TextDone(full_text="", has_code_blocks=False))
    assert items == [], (
        f"Expected empty list after ResponseStartBlock reset the buffer, "
        f"got {items!r}. If non-empty, the buffer was not reset and the "
        f"prior turn's content leaked into this response's TextDone."
    )


def test_format_tool_group() -> None:
    """ToolGroup → tool call line + result panel per execution."""
    fmt = RichBlockFormatter(show_tool_output=True)
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="Read",
                arguments={"file_path": "/tmp/f"},
                args_summary="f",
                call_id="c1",
                agent_name="coder",
                executed_by="client",
                output="content",
            ),
        ]
    )
    items = fmt.format(group)
    # At least 2 items: tool call line + result panel.
    assert len(items) >= 2


def test_format_tool_group_no_output() -> None:
    """ToolGroup with no output → only the tool call line."""
    fmt = RichBlockFormatter()
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="Glob",
                arguments={"pattern": "*"},
                args_summary="*",
                call_id="c1",
                agent_name="coder",
                executed_by="server",
                output=None,
            ),
        ]
    )
    items = fmt.format(group)
    # Only the tool call line, no result panel.
    assert len(items) == 1


def test_duplicate_tool_call_renders_call_line_once() -> None:
    """
    Regression: sessions API emits ``function_call`` twice per tool —
    once at dispatch (``in_progress``) and again at completion
    (``completed``).  Both produce a ``ToolGroup`` with the same
    ``call_id``. The formatter must render the ``⏵`` call line only
    on the first occurrence; the second should be suppressed.

    Reproduces the bug observed in the debug log at
    ``~/.omnigent/debug/events-fresh-1778791015.jsonl``: every tool
    call's ``⏵ tool_name(args)`` line appeared twice in the TUI.
    """
    fmt = RichBlockFormatter()
    call_id = "call_dIeDFiBbJixbbeQqsYzvIjeE"

    # First event: function_call with status "in_progress".
    group1 = ToolGroup(
        executions=[
            ToolExecution(
                name="sys_terminal_launch",
                arguments={"terminal": "zsh"},
                args_summary="zsh",
                call_id=call_id,
                agent_name="coder",
                output=None,
            ),
        ],
    )
    items1 = fmt.format_tool_group(group1)

    # The call line renders for the first occurrence.
    # 1 item = the ⏵ call line (output is None → no result panel).
    assert len(items1) == 1, (
        f"Expected 1 item (call line) on first format_tool_group, "
        f"got {len(items1)}: {items1!r}. If 0, the call line was "
        f"incorrectly suppressed on the first occurrence."
    )

    # Second event: same call_id, now status "completed".
    group2 = ToolGroup(
        executions=[
            ToolExecution(
                name="sys_terminal_launch",
                arguments={"terminal": "zsh"},
                args_summary="zsh",
                call_id=call_id,
                agent_name="coder",
                output=None,
            ),
        ],
    )
    items2 = fmt.format_tool_group(group2)

    # The call line must NOT render again — the duplicate is suppressed.
    assert items2 == [], (
        f"Expected empty list on second format_tool_group with the same "
        f"call_id (duplicate suppression), got {len(items2)} item(s): "
        f"{items2!r}. If 1, the ⏵ call line rendered twice for the same "
        f"tool call — this is the sessions-API duplicate bug."
    )


def test_duplicate_tool_call_still_renders_result_panel() -> None:
    """
    When a duplicate ``ToolGroup`` arrives with ``output`` populated
    (e.g. the ``completed`` event carries the output), the call line
    is suppressed but the result panel still renders.
    """
    fmt = RichBlockFormatter(show_tool_output=True)
    call_id = "call_abc123"

    # First occurrence: call line, no output.
    fmt.format_tool_group(
        ToolGroup(
            executions=[
                ToolExecution(
                    name="Read",
                    arguments={"file_path": "/tmp/f"},
                    args_summary="f",
                    call_id=call_id,
                    agent_name="coder",
                    output=None,
                ),
            ],
        )
    )

    # Second occurrence: same call_id, but now with output.
    items = fmt.format_tool_group(
        ToolGroup(
            executions=[
                ToolExecution(
                    name="Read",
                    arguments={"file_path": "/tmp/f"},
                    args_summary="f",
                    call_id=call_id,
                    agent_name="coder",
                    output="file contents here",
                ),
            ],
        )
    )

    # Call line is suppressed (already rendered), but the result panel
    # still appears because it's the first time output is non-None.
    assert len(items) == 1, (
        f"Expected 1 item (result panel only, call line suppressed) on "
        f"duplicate ToolGroup with output, got {len(items)}: {items!r}. "
        f"If 0, the result panel was incorrectly suppressed. "
        f"If 2, the call line rendered again."
    )


def test_tool_call_dedup_resets_on_new_response() -> None:
    """
    ``format_response_start`` clears the seen-call-id set so a
    new turn can reuse call_ids without suppression.  This
    matters for conversation resume where the same call_id may
    appear in both the history replay and the live stream.
    """
    fmt = RichBlockFormatter()
    call_id = "call_reused"

    # First turn: render the call line.
    fmt.format_tool_group(
        ToolGroup(
            executions=[
                ToolExecution(
                    name="Glob",
                    arguments={"pattern": "*"},
                    args_summary="*",
                    call_id=call_id,
                    agent_name="coder",
                    output=None,
                ),
            ],
        )
    )

    # Start a new response — the dedup set must reset.
    fmt.format_response_start(ResponseStartBlock(model="coder", response_id="r2"))

    # Same call_id in the new turn should render normally.
    items = fmt.format_tool_group(
        ToolGroup(
            executions=[
                ToolExecution(
                    name="Glob",
                    arguments={"pattern": "*"},
                    args_summary="*",
                    call_id=call_id,
                    agent_name="coder",
                    output=None,
                ),
            ],
        )
    )

    assert len(items) == 1, (
        f"Expected 1 item (call line) after response-start reset, "
        f"got {len(items)}: {items!r}. If 0, the dedup set was not "
        f"cleared by format_response_start and the call_id from the "
        f"prior turn leaked through."
    )


def test_format_reasoning_start() -> None:
    """ReasoningStartBlock → thinking indicator."""
    fmt = RichBlockFormatter()
    items = fmt.format(ReasoningStartBlock())
    assert len(items) == 1
    assert "thinking" in str(items[0]).lower()


def test_format_reasoning_with_text() -> None:
    """ReasoningBlock with text → panel."""
    fmt = RichBlockFormatter()
    items = fmt.format(
        ReasoningBlock(
            reasoning_text="deep thoughts here",
            summary_text="",
        )
    )
    assert len(items) == 1  # The panel.


def test_format_reasoning_empty() -> None:
    """ReasoningBlock with no text → nothing."""
    fmt = RichBlockFormatter()
    items = fmt.format(ReasoningBlock(reasoning_text="", summary_text=""))
    assert items == []


def test_format_error() -> None:
    """ErrorBlock → error panel."""
    fmt = RichBlockFormatter()
    items = fmt.format(ErrorBlock(message="something broke", source="llm"))
    assert len(items) == 1


def test_format_retry() -> None:
    """RetryBlock → retry indicator."""
    fmt = RichBlockFormatter()
    items = fmt.format(
        RetryBlock(
            source="tool",
            attempt=2,
            max_attempts=3,
            delay_seconds=1.5,
        )
    )
    assert len(items) == 1
    assert "retrying" in str(items[0]).lower()


def test_format_compaction() -> None:
    """CompactionBlock → compacting indicator."""
    fmt = RichBlockFormatter()
    items = fmt.format(CompactionBlock())
    assert len(items) == 1
    assert "compacting" in str(items[0]).lower()


def test_format_file() -> None:
    """FileBlock → file indicator."""
    fmt = RichBlockFormatter()
    items = fmt.format(FileBlock(file_id="f1", filename="photo.png"))
    assert len(items) == 1
    assert "photo.png" in str(items[0])


def test_format_response_end_completed() -> None:
    """Completed response → nothing (no status message)."""
    fmt = RichBlockFormatter()
    items = fmt.format(ResponseEndBlock(status="completed"))
    assert items == []


def test_format_response_end_failed() -> None:
    """Failed response → status message."""
    fmt = RichBlockFormatter()
    items = fmt.format(ResponseEndBlock(status="failed"))
    assert len(items) == 1
    assert "failed" in str(items[0]).lower()


def test_show_agent_labels_for_sub_agents() -> None:
    """show_agent_labels=True adds agent name for sub-agent blocks."""

    fmt = RichBlockFormatter(show_agent_labels=True)
    block = TextChunk(
        text="sub-agent text",
        ctx=BlockContext(agent="coder.researcher", depth=1),
    )
    items = fmt.format(block)
    # Should have the agent label + the StreamLive.
    assert len(items) == 2
    # Rich Text.plain gives the visible text; markup has the agent name.
    label_text = items[0].plain if hasattr(items[0], "plain") else str(items[0])
    assert "researcher" in label_text or "coder.researcher" in repr(items[0])


def test_show_agent_labels_not_for_root() -> None:
    """show_agent_labels=True doesn't add label for root agent."""

    fmt = RichBlockFormatter(show_agent_labels=True)
    block = TextChunk(
        text="root text",
        ctx=BlockContext(agent="coder", depth=0),
    )
    items = fmt.format(block)
    # Just the StreamLive, no label.
    assert len(items) == 1


def test_custom_accent_color() -> None:
    """Custom accent color is used."""
    fmt = RichBlockFormatter(accent_color="#ff0000")
    assert fmt.accent == "#ff0000"


def test_welcome_message() -> None:
    """welcome() returns a renderable with the model name."""
    fmt = RichBlockFormatter()
    item = fmt.welcome("my-agent")
    assert item is not None


def test_user_message() -> None:
    """user_message() returns a renderable."""
    fmt = RichBlockFormatter()
    item = fmt.user_message("hello world")
    assert item is not None


def test_user_message_preserves_all_lines() -> None:
    """Long user messages are rendered in full."""
    fmt = RichBlockFormatter()
    long_text = "line1\nline2\nline3\nline4\nline5\nline6"
    item = fmt.user_message(long_text)
    rendered = str(item)
    assert "more lines" not in rendered
    assert "line1" in rendered
    assert "line6" in rendered


def test_user_message_uses_no_explicit_background_color() -> None:
    """
    The user-message echo card must not paint an explicit
    background color on the typed text — a hardcoded dark
    background looks like a glaring black blob on
    light-themed terminals (iTerm light theme, ``xterm``
    defaults). Foreground-only styling adapts to whatever
    background the terminal provides.

    Pre-fix the formatter wrapped the message in
    ``[on #1a1a1a]…[/on #1a1a1a]`` Rich markup, which
    Rich resolves into an ANSI 48;2;<r>;<g>;<b> background
    sequence on the rendered :class:`Text`. This test pins
    the absence of any background color span so a future
    refactor can't reintroduce the regression silently.

    Render via a real :class:`rich.console.Console` so the
    assertion catches markup that resolves into a styled
    span at render time even if the source string changes
    shape.
    """
    import io

    from rich.console import Console

    fmt = RichBlockFormatter()
    item = fmt.user_message("hello world")

    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=80,
        legacy_windows=False,
    )
    console.print(item)
    output = buf.getvalue()

    # ANSI background-color sequence: ESC[48;2;...m sets a
    # 24-bit (truecolor) background; ESC[48;5;...m is the
    # 256-color background; ESC[40-47;100-107m are the named
    # background colors. None should appear for an unstyled
    # echo card. ``\x1b[7m`` (reverse-video) is also a way to
    # paint a background — explicitly check that's absent too.
    assert "\x1b[48;2;" not in output, (
        f"User-message echo card paints an RGB background. "
        f"Pre-fix this was ``[on #1a1a1a]…[/on #1a1a1a]`` and looked "
        f"like a black blob on light terminals.\nrender:\n{output!r}"
    )
    assert "\x1b[48;5;" not in output, (
        f"User-message echo card paints a 256-color background.\nrender:\n{output!r}"
    )
    assert "\x1b[7m" not in output, (
        f"User-message echo card uses reverse video — would still "
        f"flip the terminal's background under the typed text.\n"
        f"render:\n{output!r}"
    )
    # Sanity: the typed text DOES appear in the render —
    # otherwise the absence-of-bg assertion above would pass
    # vacuously.
    assert "hello world" in output


def test_subclass_override() -> None:
    """Subclassing and overriding one method works."""

    class CustomFormatter(RichBlockFormatter):
        def format_error(self, block: ErrorBlock) -> list:  # type: ignore[override]
            return [StreamingText(text=f"CUSTOM ERROR: {block.message}")]

    fmt = CustomFormatter()
    items = fmt.format(ErrorBlock(message="test error", source="llm"))
    assert len(items) == 1
    assert isinstance(items[0], StreamingText)
    assert "CUSTOM ERROR" in items[0].text


# ── tool result truncation + JSON pretty-print ───────────────


def _render(item: object, *, width: int = 200) -> str:
    """
    Render a Rich item through a real Console and return the captured
    text. We use ``color_system=None`` so the assertions match plain
    characters, not ANSI escapes.
    """
    import io

    from rich.console import Console

    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        color_system=None,
        width=width,
        legacy_windows=False,
    )
    console.print(item)
    return buf.getvalue()


def _tool_group_with_output(output: str) -> ToolGroup:
    """Build a single-execution ToolGroup with the given output."""
    return ToolGroup(
        executions=[
            ToolExecution(
                name="sys_terminal_read",
                arguments={},
                args_summary="",
                call_id="c1",
                agent_name="coder",
                executed_by="server",
                output=output,
            ),
        ]
    )


def test_tool_result_long_single_line_truncated_by_chars() -> None:
    """
    A single physical line longer than ``max_result_chars`` is
    truncated and the panel shows a "… N more chars" footer.
    Reproduces the user-reported case where a JSON-stringified
    terminal scrollback was one giant line, defeating the line cap.
    """
    fmt = RichBlockFormatter(max_result_chars=100, max_result_lines=30, show_tool_output=True)
    blob = "x" * 500  # one line, 500 chars
    items = fmt.format(_tool_group_with_output(blob))
    # Tool call line + result panel.
    assert len(items) == 2
    rendered = _render(items[1])
    # 500 chars - 100 cap = 400 omitted. Footer shows that exact count
    # so a regression in the cap math fails loudly.
    assert "400 more chars" in rendered, (
        f"expected '400 more chars' footer (500 input - 100 cap), got:\n{rendered!r}"
    )
    # The full 500 x's must NOT all appear — the truncation is what's
    # under test. If 400 of them leaked through, the cap is bypassed.
    assert "x" * 200 not in rendered


def test_tool_result_many_lines_truncated_by_lines() -> None:
    """
    Multi-line output exceeding ``max_result_lines`` is line-truncated
    and the panel shows a "… N more lines" footer.
    """
    fmt = RichBlockFormatter(max_result_lines=5, max_result_chars=10000, show_tool_output=True)
    body = "\n".join(f"line-{i}" for i in range(20))
    items = fmt.format(_tool_group_with_output(body))
    rendered = _render(items[1])
    # 20 lines - 5 cap = 15 omitted.
    assert "15 more lines" in rendered, f"expected '15 more lines' footer, got:\n{rendered!r}"
    # The first 5 lines must be visible.
    for i in range(5):
        assert f"line-{i}" in rendered
    # Lines past the cap must NOT leak through.
    assert "line-10" not in rendered


def test_tool_result_short_output_not_truncated() -> None:
    """Outputs under both caps render in full with no '… N more' footer."""
    fmt = RichBlockFormatter(max_result_lines=30, max_result_chars=2000, show_tool_output=True)
    items = fmt.format(_tool_group_with_output("hello world"))
    rendered = _render(items[1])
    assert "hello world" in rendered
    # No truncation footer of either flavor.
    assert "more lines" not in rendered
    assert "more chars" not in rendered


def test_tool_result_json_object_pretty_printed() -> None:
    """
    A JSON object output is pretty-printed: separate keys land on
    separate lines (so the line-cap can do useful work), and non-ASCII
    characters render as themselves rather than ``\\uXXXX`` escapes.
    Mirrors the user-reported ``sys_terminal_read`` payload where
    ``─`` (U+2500) showed up as ``\\u2500``.
    """
    fmt = RichBlockFormatter(max_result_lines=30, max_result_chars=2000, show_tool_output=True)
    # ensure_ascii=True is Python's json.dumps default — this is
    # exactly what the producer-side serializer emits.
    raw = json.dumps({"terminal": "zsh:r-3", "border": "─" * 4})
    assert "\\u2500" in raw  # sanity: producer escapes the box char
    items = fmt.format(_tool_group_with_output(raw))
    rendered = _render(items[1])
    # The literal Unicode escape must NOT appear post-format.
    assert "\\u2500" not in rendered, (
        f"box-drawing char rendered as ASCII escape — JSON pretty-print "
        f"didn't run or used ensure_ascii=True. rendered:\n{rendered!r}"
    )
    # The actual U+2500 character must render.
    assert "─" in rendered
    # Pretty-print broke the object onto multiple lines — the key
    # quoting proves we're looking at indented JSON, not the raw blob.
    assert '"terminal"' in rendered
    assert '"border"' in rendered


def test_tool_result_non_json_passes_through() -> None:
    """Plain text that doesn't start with { or [ is not pretty-printed."""
    fmt = RichBlockFormatter(max_result_lines=30, max_result_chars=2000, show_tool_output=True)
    items = fmt.format(_tool_group_with_output("plain output, no JSON here"))
    rendered = _render(items[1])
    assert "plain output, no JSON here" in rendered


def test_tool_result_invalid_json_passes_through() -> None:
    """A string that looks like JSON but doesn't parse renders raw."""
    fmt = RichBlockFormatter(max_result_lines=30, max_result_chars=2000, show_tool_output=True)
    raw = '{"unterminated": "string'
    items = fmt.format(_tool_group_with_output(raw))
    rendered = _render(items[1])
    # Raw text appears verbatim — we did NOT raise or substitute.
    assert "unterminated" in rendered


def test_tool_result_json_scalar_passes_through() -> None:
    """A bare JSON number/string (scalar) is not reformatted."""
    fmt = RichBlockFormatter(show_tool_output=True)
    items = fmt.format(_tool_group_with_output("42"))
    rendered = _render(items[1])
    assert "42" in rendered
    # Scalars don't trigger pretty-printing — there's no structure to
    # expand. We rely on the helper's ``isinstance(parsed, (dict, list))``
    # guard. The footer must be absent for such a tiny input.
    assert "more lines" not in rendered
    assert "more chars" not in rendered


def test_tool_result_both_caps_apply() -> None:
    """
    When both caps trip (many lines AND the survivor still exceeds
    char cap), the footer mentions both, in lines-then-chars order.
    """
    # 50 lines of 100 chars each. Line cap=10 keeps 10 lines × 100 chars =
    # ~1000 chars (plus 9 newlines). Char cap=100 then trims to 100.
    fmt = RichBlockFormatter(max_result_lines=10, max_result_chars=100, show_tool_output=True)
    body = "\n".join("a" * 100 for _ in range(50))
    items = fmt.format(_tool_group_with_output(body))
    rendered = _render(items[1])
    # 50 - 10 = 40 lines omitted by the line cap.
    assert "40 more lines" in rendered
    # Chars omitted depends on what's left after the line cap; the
    # exact number is brittle to whitespace, but "more chars" must
    # appear because the survivor exceeds 100 chars.
    assert "more chars" in rendered


# ── Two-region streaming model tests ─────────────────────────


def test_text_chunk_code_fence_emits_stream_live() -> None:
    """
    An unclosed code fence emits a ``StreamLive`` whose renderable
    is a ``Markdown`` that Rich renders as a proper code block.

    Rich's markdown-it-py extends unclosed fences to EOF per the
    CommonMark spec, so no synthetic closing fence is needed.
    Verifies that the live region contains rendered code-block
    content (syntax-highlighted), not raw markdown fence syntax.
    """
    fmt = RichBlockFormatter()
    items = fmt.format(TextChunk(text="```python\ndef foo():\n    pass"))
    # Single StreamLive for the unstable tail (no boundary to commit).
    assert len(items) == 1, (
        f"Expected 1 StreamLive for unclosed code fence, got {len(items)}: {items!r}."
    )
    assert isinstance(items[0], StreamLive), (
        f"Expected StreamLive for unclosed code fence tail, got {type(items[0]).__name__}."
    )
    # Render the live region through Rich and verify the code
    # content appears (proving Markdown() handled the unclosed fence).
    rendered = _render(items[0].renderable)
    assert "def foo" in rendered, (
        f"Expected the code content 'def foo' in the rendered StreamLive, "
        f"got: {rendered!r}. If absent, Rich's Markdown() didn't handle "
        f"the unclosed fence."
    )


def test_incremental_stable_prefix() -> None:
    """
    Progressive commits as boundaries are found: each ``\\n\\n`` commits
    a new stable block while the tail remains live.

    Verifies the ``_committed_offset`` tracks correctly across multiple
    chunks with boundaries.
    """
    fmt = RichBlockFormatter()

    # First paragraph complete.
    items1 = fmt.format(TextChunk(text="Para 1.\n\nPara 2 start"))
    # StreamReplace for committed "Para 1." + StreamLive for "Para 2 start".
    assert len(items1) == 2, f"Expected 2 items, got {len(items1)}: {items1!r}"
    assert isinstance(items1[0], StreamReplace)
    assert isinstance(items1[1], StreamLive)

    # Second paragraph completes.
    items2 = fmt.format(TextChunk(text=" end.\n\nPara 3"))
    # StreamReplace for committed "Para 2 start end." + StreamLive for "Para 3".
    assert len(items2) == 2, f"Expected 2 items, got {len(items2)}: {items2!r}"
    assert isinstance(items2[0], StreamReplace), (
        f"Expected StreamReplace for second committed paragraph, got {type(items2[0]).__name__}."
    )
    assert isinstance(items2[1], StreamLive), (
        f"Expected StreamLive for 'Para 3' tail, got {type(items2[1]).__name__}."
    )


def test_response_start_resets_committed_offset() -> None:
    """
    ``format_response_start`` resets ``_committed_offset`` to 0 so
    a new turn starts with a clean slate. Without the reset, the
    offset from a prior turn would cause the new turn's first
    boundary detection to be offset into stale buffer positions.
    """
    fmt = RichBlockFormatter()

    # Simulate a partial prior turn.
    fmt.format(TextChunk(text="Prior para.\n\nLeftover tail"))
    # _committed_offset is now > 0 from the boundary in the prior turn.

    # Start a new response — must reset offset.
    fmt.format(ResponseStartBlock(model="coder", response_id="r3"))

    # New turn text should start fresh.
    items = fmt.format(TextChunk(text="Fresh para.\n\nSecond"))
    # StreamReplace for "Fresh para." + StreamLive for "Second".
    assert len(items) == 2, (
        f"Expected 2 items (committed para + live tail) from fresh turn, "
        f"got {len(items)}: {items!r}. If only StreamLive, the committed "
        f"offset from the prior turn leaked through the reset."
    )
    assert isinstance(items[0], StreamReplace), (
        f"Expected StreamReplace for committed paragraph in fresh turn, "
        f"got {type(items[0]).__name__}. If missing, _committed_offset "
        f"was not reset by format_response_start."
    )


# ── _LeftHeading style tests ─────────────────────────────


def _render_heading(level: int) -> str:
    """Render a Markdown heading at the given level through a real Console.

    Returns the raw ANSI output so tests can inspect style sequences.
    Uses ``force_terminal=True`` + ``truecolor`` so Rich emits real
    ANSI escapes rather than plain text.
    """
    hashes = "#" * level
    md = Markdown(f"{hashes} Heading Text")
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=80,
        legacy_windows=False,
    )
    console.print(md)
    return buf.getvalue()


@pytest.mark.parametrize(
    "level",
    [1, 2, 3, 4, 5, 6],
    ids=["h1", "h2", "h3", "h4", "h5", "h6"],
)
def test_left_heading_renders_text(level: int) -> None:
    """Every heading level (h1–h6) renders its text content.

    The _LeftHeading patch on Markdown.elements means all Markdown()
    instances use our custom heading class. If a heading level is
    missing from _HEADING_STYLES, the text silently vanishes.
    """
    output = _render_heading(level)
    assert "Heading Text" in output, (
        f"h{level} heading text missing from render. If the tag is not "
        f"in _HEADING_STYLES, __rich_console__ yields nothing and the "
        f"heading silently disappears.\nrender:\n{output!r}"
    )


@pytest.mark.parametrize(
    "level",
    [1, 2, 3, 4, 5, 6],
    ids=["h1", "h2", "h3", "h4", "h5", "h6"],
)
def test_left_heading_is_left_aligned(level: int) -> None:
    """Headings must be left-aligned, not Rich's default center.

    The _LeftHeading class overrides __rich_console__ to set
    justify="left" and reconstruct the Text with no justify, which
    prevents center-alignment and stops underline from extending to
    the console width.
    """
    output = _render_heading(level)
    # "Heading Text" is 12 chars. In an 80-col console, center-alignment
    # would place ~34 leading spaces. Left-alignment means the text
    # starts within the first few columns (possibly after a newline
    # from Rich's Markdown block spacing).
    for line in output.splitlines():
        # Find the line with the actual heading text.
        # Strip ANSI escapes for the whitespace check.
        plain = _strip_ansi(line)
        if "Heading Text" in plain:
            leading = len(plain) - len(plain.lstrip())
            assert leading < 10, (
                f"h{level} heading has {leading} leading spaces — "
                f"looks center-aligned. _LeftHeading should left-align.\n"
                f"line: {line!r}"
            )
            break
    else:
        pytest.fail(f"h{level}: 'Heading Text' not found on any line")


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_left_heading_h1_is_bold() -> None:
    """h1 must be bold (part of its defined style: bold+italic+underline)."""
    output = _render_heading(1)
    # ANSI bold: ESC[1m
    assert "\x1b[1m" in output or "\x1b[1;" in output, (
        f"h1 heading missing bold ANSI sequence. Expected bold+italic+underline.\n"
        f"render:\n{output!r}"
    )


def test_left_heading_h3_is_bold_no_underline() -> None:
    """h3 style is bold-only — no underline, no italic."""
    output = _render_heading(3)
    # Bold present.
    assert "\x1b[1m" in output or "\x1b[1;" in output, f"h3 missing bold. render:\n{output!r}"
    # Underline (ESC[4m) should NOT appear for h3.
    # Only check lines containing the heading text to avoid false
    # positives from Rich chrome.
    for line in output.splitlines():
        if "Heading Text" in line:
            assert "\x1b[4m" not in line and "\x1b[4;" not in line, (
                f"h3 should not be underlined. line:\n{line!r}"
            )


@pytest.mark.parametrize("level", [4, 5, 6], ids=["h4", "h5", "h6"])
def test_left_heading_gray_levels_have_color(level: int) -> None:
    """h4–h6 are styled with #888888 (gray). The ANSI output should
    contain an RGB color sequence for that gray.
    """
    output = _render_heading(level)
    # #888888 = RGB(136, 136, 136). Rich emits this as:
    # ESC[38;2;136;136;136m  (foreground truecolor)
    assert "38;2;136;136;136" in output, (
        f"h{level} missing #888888 foreground color. Expected "
        f"truecolor sequence 38;2;136;136;136.\nrender:\n{output!r}"
    )


def test_left_heading_styles_dict_covers_all_levels() -> None:
    """_HEADING_STYLES has entries for h1–h6. If one is missing,
    the heading silently vanishes (the method yields nothing for
    unknown tags).
    """
    for tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        assert tag in _LeftHeading._HEADING_STYLES, (
            f"{tag!r} missing from _HEADING_STYLES — headings at this "
            f"level would silently disappear in rendered Markdown."
        )


def test_left_heading_styles_are_all_style_instances() -> None:
    """Every entry in _HEADING_STYLES must be a rich.style.Style."""
    for tag, style in _LeftHeading._HEADING_STYLES.items():
        assert isinstance(style, Style), (
            f"_HEADING_STYLES[{tag!r}] is {type(style).__name__}, expected Style."
        )


# ── Diamond paragraph splitting tests ────────────────────


def test_diamond_split_multi_paragraph() -> None:
    """When diamond is pending and stable text contains \\n\\n,
    the first paragraph gets diamond styling (Padding (0,1,0,1))
    and subsequent paragraphs get standard styling (Padding (1,1,0,3)).

    Without the split, all paragraphs in the first stable chunk
    would be rendered as a single diamond-styled block with wrong
    padding.
    """
    fmt = RichBlockFormatter()
    fmt.format(ResponseStartBlock(model="test", response_id="r1"))

    # Stream two paragraphs separated by \n\n in one chunk.
    items = fmt.format(TextChunk(text="First paragraph.\n\nSecond paragraph.\n\nTail"))

    # The boundary detector finds two \n\n boundaries, producing two
    # committed paragraphs + one live tail.
    # With diamond splitting: first para gets diamond, second gets regular.
    replaces = [it for it in items if isinstance(it, StreamReplace)]
    lives = [it for it in items if isinstance(it, StreamLive)]

    # At least 2 StreamReplace items: diamond para + regular para.
    assert len(replaces) >= 2, (
        f"Expected at least 2 StreamReplace items (diamond + regular), "
        f"got {len(replaces)}. If 1, the diamond split didn't fire — "
        f"both paragraphs were rendered as a single block."
    )

    # First replace: diamond — Padding with top=0, left=1.
    first_pad = replaces[0].renderable
    assert isinstance(first_pad, Padding), (
        f"First StreamReplace should wrap a Padding, got {type(first_pad).__name__}."
    )
    # Diamond layout: (top=0, right=1, bottom=0, left=1).
    assert (first_pad.top, first_pad.right, first_pad.bottom, first_pad.left) == (0, 1, 0, 1), (
        f"Diamond paragraph should have (0,1,0,1) padding, got "
        f"({first_pad.top},{first_pad.right},{first_pad.bottom},{first_pad.left})."
    )

    # Second replace: regular — Padding with top=1, left=3.
    second_pad = replaces[1].renderable
    assert isinstance(second_pad, Padding), (
        f"Second StreamReplace should wrap a Padding, got {type(second_pad).__name__}."
    )
    assert (second_pad.top, second_pad.right, second_pad.bottom, second_pad.left) == (
        1,
        1,
        0,
        3,
    ), (
        f"Non-diamond paragraph should have (1,1,0,3) padding, got "
        f"({second_pad.top},{second_pad.right},{second_pad.bottom},{second_pad.left}). "
        f"The 1-top reproduces the inter-block gap."
    )

    # Tail is live.
    assert len(lives) == 1, f"Expected 1 StreamLive for tail, got {len(lives)}."


def test_diamond_no_split_single_paragraph() -> None:
    """When diamond is pending and stable text has no \\n\\n, the
    entire text gets diamond styling — no splitting occurs.
    """
    fmt = RichBlockFormatter()
    fmt.format(ResponseStartBlock(model="test", response_id="r1"))

    # Stream a single paragraph that completes (followed by text that
    # creates a boundary), but with no internal \n\n.
    items = fmt.format(TextChunk(text="Single paragraph.\n\nTail"))

    replaces = [it for it in items if isinstance(it, StreamReplace)]
    assert len(replaces) == 1, f"Expected 1 StreamReplace (diamond para), got {len(replaces)}."

    pad = replaces[0].renderable
    assert isinstance(pad, Padding)
    # Diamond padding: (top=0, right=1, bottom=0, left=1).
    assert (pad.top, pad.right, pad.bottom, pad.left) == (0, 1, 0, 1), (
        f"Single diamond paragraph should have (0,1,0,1) padding, "
        f"got ({pad.top},{pad.right},{pad.bottom},{pad.left})."
    )


def test_diamond_consumed_after_first_paragraph() -> None:
    """The diamond flag is consumed by the first _markdown_replace call.
    Subsequent paragraphs in the same response must NOT get diamond
    styling even if they arrive in later chunks.
    """
    fmt = RichBlockFormatter()
    fmt.format(ResponseStartBlock(model="test", response_id="r1"))

    # First chunk: diamond paragraph committed. Text after \n\n is
    # needed so _find_stable_markdown_boundary detects the boundary
    # (it requires content after the blank line).
    items1 = fmt.format(TextChunk(text="Diamond para.\n\nbridge"))
    # Verify diamond was consumed: first replace has diamond padding.
    replaces1 = [it for it in items1 if isinstance(it, StreamReplace)]
    assert len(replaces1) == 1, f"Expected 1 StreamReplace (diamond commit), got {len(replaces1)}."
    assert replaces1[0].renderable.top == 0, "First commit should be diamond (top=0)."

    # Second chunk: regular paragraph — diamond already consumed.
    items2 = fmt.format(TextChunk(text=" text.\n\nMore"))

    replaces2 = [it for it in items2 if isinstance(it, StreamReplace)]
    for r in replaces2:
        pad = r.renderable
        assert isinstance(pad, Padding)
        # All should be regular (1, 1, 0, 3) — never diamond (0, 1, 0, 1).
        actual = (pad.top, pad.right, pad.bottom, pad.left)
        assert actual == (1, 1, 0, 3), (
            f"Post-diamond paragraph should have (1,1,0,3) padding, "
            f"got {actual}. If (0,1,0,1), the diamond flag was not "
            f"consumed after the first paragraph."
        )


# ── Padding / inter-block gap tests ──────────────────────


def test_non_diamond_replace_has_top_padding() -> None:
    """_markdown_replace for non-diamond paragraphs uses (1,1,0,3)
    padding — the 1-top reproduces the blank-line gap Rich inserts
    between blocks within a single Markdown() render.

    Without top=1, separate StreamReplace renders would abut with
    no visual gap.
    """
    fmt = RichBlockFormatter()
    # Start a response to set diamond, then consume it.
    fmt.format(ResponseStartBlock(model="test", response_id="r1"))
    # Include text after \n\n so the boundary is detected and diamond
    # is consumed on the first commit.
    fmt.format(TextChunk(text="Diamond.\n\nbridge"))

    # Now commit a non-diamond paragraph.
    items = fmt.format(TextChunk(text=" text.\n\nTail"))
    replaces = [it for it in items if isinstance(it, StreamReplace)]
    assert len(replaces) >= 1, (
        f"Expected at least 1 StreamReplace for non-diamond commit, got {len(replaces)}."
    )

    pad = replaces[0].renderable
    assert isinstance(pad, Padding)
    assert pad.top == 1, (
        f"Non-diamond StreamReplace should have top=1 padding, "
        f"got top={pad.top}. Without this, paragraphs abut "
        f"with no visual gap between them."
    )


def test_tail_padding_zero_when_no_committed_content() -> None:
    """When no content has been committed yet (_committed_offset == 0),
    the StreamLive tail has top=0 padding. This is the first piece of
    content in the response — no gap above needed.
    """
    fmt = RichBlockFormatter()
    # No ResponseStartBlock — just stream text directly.
    # _committed_offset starts at 0.
    items = fmt.format(TextChunk(text="First words"))

    assert len(items) == 1
    assert isinstance(items[0], StreamLive)

    pad = items[0].renderable
    assert isinstance(pad, Padding)
    assert pad.top == 0, (
        f"Tail with no committed content above should have top=0, "
        f"got top={pad.top}. Top padding here would add an "
        f"unwanted gap at the start of the response."
    )


def test_tail_padding_one_when_committed_content_exists() -> None:
    """After content has been committed (_committed_offset > 0),
    the StreamLive tail has top=1 padding — matching the inter-block
    gap that non-diamond StreamReplace uses.
    """
    fmt = RichBlockFormatter()
    # Commit a paragraph to advance _committed_offset.
    fmt.format(TextChunk(text="First para.\n\nTail start"))

    # The tail from the above call already has committed_offset > 0.
    # Stream more text to get a fresh tail render.
    items = fmt.format(TextChunk(text=" more tail"))

    assert len(items) == 1, f"Expected 1 StreamLive for tail, got {len(items)}: {items!r}."
    assert isinstance(items[0], StreamLive)

    pad = items[0].renderable
    assert isinstance(pad, Padding)
    assert pad.top == 1, (
        f"Tail after committed content should have top=1, got "
        f"top={pad.top}. Without this, the live tail abuts the "
        f"committed paragraph with no gap."
    )
