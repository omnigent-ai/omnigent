"""Full rendering pipeline tests: block → formatter → host → stdout.

Tests the actual production path: real block dataclasses →
``RichBlockFormatter.format()`` → ``TerminalHost.output()`` → captured
stdout. Verifies that content traverses the entire pipeline and appears
correctly in the terminal output.
"""

from __future__ import annotations

import sys

import pytest
from omnigent_client._blocks import (
    BlockContext,
    ErrorBlock,
    ReasoningBlock,
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
)
from omnigent_ui_sdk.terminal._host import TerminalHost

# ── Helpers ─────────────────────────────────────────────────────


def _capture_pipeline(
    host: TerminalHost,
    fmt: RichBlockFormatter,
    blocks: list[object],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Feed blocks through formatter → host → captured stdout.

    ``host.output()`` uses both ``sys.stdout.write`` (for
    StreamLive/StreamReplace) and ``print()`` (which delegates to
    ``sys.stdout.write``). By hooking ``sys.stdout.write`` we capture
    both paths in a single list.

    :returns: Concatenated stdout output.
    """
    writes: list[str] = []

    def _record_write(data: str) -> int:
        writes.append(data)
        return len(data)

    monkeypatch.setattr(sys.stdout, "write", _record_write)
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)
    for block in blocks:
        for item in fmt.format(block):
            host.output(item)
    return "".join(writes)


# ── Pipeline tests ──────────────────────────────────────────────


def test_pipeline_response_start_resets_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ResponseStartBlock`` resets formatter state for a new turn.

    The formatter no longer renders the model name inline — it only
    resets paragraph buffer / committed offset / diamond flag.
    """
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    # Dirty the formatter state so we can verify the reset.
    fmt._paragraph_buffer = "leftover"
    fmt._committed_offset = 42
    _capture_pipeline(
        host, fmt, [ResponseStartBlock(model="coder", response_id="r1")], monkeypatch
    )
    assert fmt._paragraph_buffer == "", (
        f"Expected empty paragraph buffer after ResponseStartBlock, got: {fmt._paragraph_buffer!r}"
    )
    assert fmt._committed_offset == 0, (
        f"Expected committed_offset == 0 after ResponseStartBlock, got: {fmt._committed_offset}"
    )


def test_pipeline_text_renders_markdown_not_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TextChunk(text="Hello **world**\\n\\n")`` → "world" present, no literal ``**``."""
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    # Use a complete paragraph (ending with \n\n) + TextDone to flush.
    output = _capture_pipeline(
        host,
        fmt,
        [
            ResponseStartBlock(model="test", response_id="r1"),
            TextChunk(text="Hello **world**\n\n"),
            TextDone(full_text="Hello **world**\n\n"),
        ],
        monkeypatch,
    )
    # "world" should be present (the content survived rendering).
    assert "world" in output, (
        f"Expected 'world' in rendered output, got: {output!r}. Content was lost in the pipeline."
    )
    # Literal "**" should NOT survive — Rich's Markdown renderer
    # converts it to bold ANSI sequences.
    assert "**world**" not in output, (
        f"Found literal '**world**' in output — Markdown was not rendered. "
        f"The formatter should convert bold syntax to ANSI bold. "
        f"Output: {output!r}"
    )


def test_pipeline_multi_paragraph_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four TextChunks: paragraph boundary in the middle triggers commit.

    Verifies that StreamReplace is emitted for the committed paragraph
    and StreamLive for the unstable tail. The formatter's internal
    ``_committed_offset`` advances correctly.

    Note: a trailing ``\\n\\n`` at the END of a chunk does NOT trigger
    StreamReplace because ``_find_stable_markdown_boundary`` requires
    content AFTER the boundary (``candidate < n``). The commit only
    fires when the next chunk adds content past the boundary.
    """
    fmt = RichBlockFormatter()

    # Chunk 1: first paragraph ending with \n\n — no commit yet
    # because there's no content after the boundary.
    items1 = fmt.format(TextChunk(text="Paragraph one.\n\n"))
    # Chunk 1 only gets a StreamLive (the whole buffer is still "live").
    assert any(isinstance(item, StreamLive) for item in items1), (
        f"Expected StreamLive in chunk 1, got {[type(i).__name__ for i in items1]}."
    )

    # Chunk 2: content after the boundary → triggers StreamReplace
    # for committed paragraph one + StreamLive for the new tail.
    items2 = fmt.format(TextChunk(text="Paragraph two "))
    has_replace_2 = any(isinstance(item, StreamReplace) for item in items2)
    assert has_replace_2, (
        f"Expected StreamReplace in chunk 2 (content after boundary commits "
        f"paragraph one), got {[type(i).__name__ for i in items2]}."
    )
    has_live_2 = any(isinstance(item, StreamLive) for item in items2)
    assert has_live_2, (
        f"Expected StreamLive in chunk 2 (unstable tail 'Paragraph two '), "
        f"got {[type(i).__name__ for i in items2]}."
    )

    # Chunk 3: continuation — only StreamLive (no new boundary).
    items3 = fmt.format(TextChunk(text="continues."))
    has_live_3 = any(isinstance(item, StreamLive) for item in items3)
    assert has_live_3, f"Expected StreamLive in chunk 3, got {[type(i).__name__ for i in items3]}."

    # The committed offset should be past paragraph one.
    assert fmt._committed_offset > 0, (
        f"Expected _committed_offset > 0 after committing paragraph one, "
        f"got {fmt._committed_offset}. If 0, the boundary was not detected."
    )


def test_pipeline_text_done_flushes_trailing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TextChunk (no boundary) then TextDone → trailing text appears in stdout."""
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    output = _capture_pipeline(
        host,
        fmt,
        [
            ResponseStartBlock(model="test", response_id="r1"),
            TextChunk(text="trailing text without boundary"),
            TextDone(full_text="trailing text without boundary"),
        ],
        monkeypatch,
    )
    # The trailing text must appear in the output via TextDone's
    # StreamReplace flush — it was never committed by a paragraph
    # boundary.
    assert "trailing" in output, (
        f"Expected 'trailing' in pipeline output after TextDone flush, "
        f"got: {output!r}. TextDone should commit leftover buffer content."
    )


def test_pipeline_tool_group_renders_call_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ToolGroup with execution → stdout contains tool name and output."""
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter(show_tool_output=True)
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="Read",
                arguments={"file_path": "/tmp/test.py"},
                args_summary="test.py",
                call_id="c1",
                agent_name="coder",
                executed_by="server",
                output="file contents here",
            ),
        ]
    )
    output = _capture_pipeline(host, fmt, [group], monkeypatch)
    # Tool name should appear in the rendered output.
    assert "Read" in output, f"Expected tool name 'Read' in pipeline output, got: {output!r}."
    # Tool output should appear in the result panel.
    assert "file contents here" in output, (
        f"Expected tool output 'file contents here' in pipeline output, "
        f"got: {output!r}. The tool result panel was not rendered."
    )


def test_pipeline_error_block_renders_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ErrorBlock → stdout contains error message and source."""
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    output = _capture_pipeline(
        host,
        fmt,
        [ErrorBlock(message="something went wrong", source="llm")],
        monkeypatch,
    )
    assert "something went wrong" in output, (
        f"Expected error message in pipeline output, got: {output!r}."
    )
    assert "llm" in output, f"Expected error source 'llm' in pipeline output, got: {output!r}."


def test_pipeline_retry_block_renders_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RetryBlock → stdout contains attempt/max fraction."""
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    output = _capture_pipeline(
        host,
        fmt,
        [RetryBlock(source="tool", attempt=2, max_attempts=3, delay_seconds=1.0)],
        monkeypatch,
    )
    # "2/3" is the attempt fraction rendered by the formatter.
    assert "2/3" in output, f"Expected '2/3' (attempt/max) in pipeline output, got: {output!r}."


def test_pipeline_reasoning_renders_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ReasoningBlock → stdout contains summary text."""
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    output = _capture_pipeline(
        host,
        fmt,
        [ReasoningBlock(reasoning_text="deep analysis", summary_text="analyzed it")],
        monkeypatch,
    )
    # The formatter uses summary_text if available, otherwise reasoning_text.
    assert "analyzed it" in output, (
        f"Expected 'analyzed it' (summary_text) in pipeline output, got: {output!r}."
    )


def test_pipeline_full_turn_golden_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete turn: ResponseStart → TextChunks → ToolGroup → TextDone → ResponseEnd.

    Verifies all content is present in the final output and no raw
    markdown syntax survives the rendering pipeline.
    """
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    blocks = [
        ResponseStartBlock(model="golden-agent", response_id="r1"),
        TextChunk(text="Here is the **answer**.\n\n"),
        ToolGroup(
            executions=[
                ToolExecution(
                    name="Bash",
                    arguments={"command": "echo hi"},
                    args_summary="echo hi",
                    call_id="c1",
                    agent_name="golden-agent",
                    executed_by="server",
                    output="hi",
                ),
            ]
        ),
        TextChunk(text="Done reviewing."),
        TextDone(full_text="Here is the **answer**.\n\nDone reviewing."),
        ResponseEndBlock(status="completed"),
    ]
    output = _capture_pipeline(host, fmt, blocks, monkeypatch)

    # Text content from TextChunks.
    assert "answer" in output, f"Expected 'answer' from TextChunk. Got: {output!r}"
    # No raw markdown bold syntax.
    assert "**answer**" not in output, (
        f"Literal '**answer**' survived — Markdown was not rendered. Got: {output!r}"
    )
    # Tool name from ToolGroup.
    assert "Bash" in output, f"Expected tool name 'Bash'. Got: {output!r}"
    # Tool output.
    assert "hi" in output, f"Expected tool output 'hi'. Got: {output!r}"
    # Trailing text from final TextDone.
    assert "Done reviewing" in output, (
        f"Expected 'Done reviewing' from TextDone flush. Got: {output!r}"
    )


def test_pipeline_sub_agent_label_for_depth_gt_zero() -> None:
    """Block with ``depth > 0`` and ``show_agent_labels=True`` → formatter prepends label.

    The formatter adds a Rich Text label as the first item. Because
    Rich's ``from_markup`` interprets ``[coder.researcher]`` as a
    style tag (not literal text), the agent name lives in the Rich
    Text's internal spans, not in ``.plain``. We verify the label is
    present in the formatter output's ``repr`` — matching the pattern
    from the existing ``test_show_agent_labels_for_sub_agents``.
    """
    fmt = RichBlockFormatter(show_agent_labels=True)
    block = TextChunk(
        text="sub-agent output",
        ctx=BlockContext(depth=1, agent="coder.researcher"),
    )
    items = fmt.format(block)
    # Should have 2 items: the agent label + the StreamLive.
    assert len(items) == 2, (
        f"Expected 2 items (label + StreamLive) for depth > 0, got {len(items)}."
    )
    # The label contains the agent name in its markup spans.
    label_repr = repr(items[0])
    assert "coder.researcher" in label_repr, (
        f"Expected 'coder.researcher' in the label's repr when "
        f"show_agent_labels=True and depth > 0. Got: {label_repr!r}"
    )
    # The second item is the actual text content.
    assert isinstance(items[1], StreamLive), (
        f"Expected StreamLive as second item, got {type(items[1]).__name__}."
    )


# ── Hyperlink (OSC 8) pipeline tests ───────────────────────────

# OSC 8 escape components — duplicated deliberately so the test
# fails loudly if the module's wire format drifts.
_OSC_OPEN = "\x1b]8;;"
_OSC_CLOSE = "\x1b\\"


def _osc8_wrap(url: str) -> str:
    """Expected OSC 8 byte sequence for a URL."""
    return f"{_OSC_OPEN}{url}{_OSC_CLOSE}{url}{_OSC_OPEN}{_OSC_CLOSE}"


def test_pipeline_streaming_text_linkifies_urls_on_newline_flush(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """URLs in ``StreamingText`` are linkified when flushed via a newline.

    The host's ``output(StreamingText(...))`` flushes complete lines
    through ``_print_text_line`` which calls ``linkify_ansi``. This
    tests the newline-flush path (line 2372 in _host.py).

    Failure mode: ``linkify_ansi`` removed from ``_print_text_line``
    → URLs in streamed agent prose stop being ⌘-clickable.
    """
    host = TerminalHost(model_name="test")
    host.output(StreamingText(text="Visit https://example.com ok\n"))
    captured = capsys.readouterr()
    # The OSC 8 wrapper must appear around the URL.
    assert _osc8_wrap("https://example.com") in captured.out, (
        f"Expected OSC 8 hyperlink in streamed text flushed by newline, "
        f"got: {captured.out!r}. The linkify_ansi call in _print_text_line "
        f"may have been removed."
    )


def test_pipeline_streaming_text_linkifies_urls_on_word_wrap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """URLs in ``StreamingText`` are linkified when flushed via word-wrap.

    When the buffer exceeds ``_term_width() - indent_width``, the host
    word-wraps and prints. The ``linkify_ansi`` call on the wrap path
    (line 2312 in _host.py) must fire.

    Failure mode: ``linkify_ansi`` removed from the word-wrap flush
    → long lines containing URLs lose clickability.
    """
    host = TerminalHost(model_name="test")
    # Force a narrow terminal so the text wraps.
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host._term_width", lambda: 40)
    # Build text longer than 40 cols with a URL near the start.
    text = "See https://example.com " + "x" * 60
    host.output(StreamingText(text=text))
    captured = capsys.readouterr()
    assert _osc8_wrap("https://example.com") in captured.out, (
        f"Expected OSC 8 hyperlink in word-wrapped streaming text, "
        f"got: {captured.out!r}. The linkify_ansi call on the wrap path "
        f"may have been removed."
    )


def test_pipeline_stream_replace_linkifies_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URLs in ``StreamReplace`` content are linkified.

    ``_replace_live_region`` renders the renderable to ANSI via a temp
    Console and passes the result through ``linkify_ansi`` before
    writing to stdout. This covers the commit path used by the
    formatter's paragraph-boundary and TextDone flushes.

    Failure mode: ``linkify_ansi`` removed from ``_replace_live_region``
    → rendered Markdown paragraphs containing URLs lose clickability.
    """
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter()
    # Build a response with a URL in the text, ending with TextDone
    # to force a StreamReplace flush.
    writes: list[str] = []
    monkeypatch.setattr(sys.stdout, "write", lambda d: (writes.append(d), len(d))[1])
    monkeypatch.setattr(sys.stdout, "flush", lambda: None)

    for item in fmt.format(ResponseStartBlock(model="test", response_id="r1")):
        host.output(item)
    for item in fmt.format(TextChunk(text="Go to https://example.com/path now")):
        host.output(item)
    for item in fmt.format(TextDone(full_text="Go to https://example.com/path now")):
        host.output(item)

    combined = "".join(writes)
    assert _osc8_wrap("https://example.com/path") in combined, (
        f"Expected OSC 8 hyperlink in StreamReplace output, "
        f"got: {combined!r}. The linkify_ansi call in "
        f"_replace_live_region may have been removed."
    )


def test_pipeline_tool_output_url_linkified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URLs in tool result panels are linkified through the Rich render path.

    Tool results are rendered as Rich Panels via the non-streaming
    ``output()`` path, which calls ``linkify_ansi`` on the
    Console-rendered ANSI. This is the most common source of
    clickable URLs in practice — agents frequently emit URLs in
    tool output (search results, API responses, file paths).

    Failure mode: ``linkify_ansi`` removed from the Rich-renderable
    branch of ``output()`` → URLs in tool panels stop being clickable.
    """
    host = TerminalHost(model_name="test")
    fmt = RichBlockFormatter(show_tool_output=True)
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="WebSearch",
                arguments={"query": "test"},
                args_summary="test",
                call_id="c1",
                agent_name="coder",
                executed_by="server",
                output="Result: https://docs.example.com/api",
            ),
        ]
    )
    output = _capture_pipeline(host, fmt, [group], monkeypatch)
    # The OSC 8 opener encodes the URL as the hyperlink target.
    # Rich may interleave SGR color codes between the OSC 8 opener
    # and the display text, so we check for the OSC 8 opener
    # (which carries the URL) and the closer separately.
    assert f"{_OSC_OPEN}https://docs.example.com/api" in output, (
        f"Expected OSC 8 opener with URL in tool result panel, "
        f"got: {output!r}. The linkify_ansi call in the Rich-renderable "
        f"branch of output() may have been removed."
    )
    # The OSC 8 closer must also be present to terminate the link.
    assert f"{_OSC_OPEN}{_OSC_CLOSE}" in output, (
        f"Expected OSC 8 closer in tool result panel, got: {output!r}."
    )
