"""Unit tests for BlockStream — mock events → blocks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from omnigent_client._blocks import (
    ReasoningBlock,
    ReasoningChunk,
    ReasoningStartBlock,
    TextChunk,
    TextDone,
    ToolGroup,
    ToolResultBlock,
)
from omnigent_client._events import (
    MessageDone,
    ReasoningDelta,
    ReasoningStarted,
    ReasoningSummaryDelta,
    ResponseCompleted,
    ResponseCreated,
    ResponseInProgress,
    TextDelta,
    ToolCall,
    ToolResult,
)
from omnigent_client._stream import BlockStream
from omnigent_client._types import Response


def _make_response(
    response_id: str = "resp_1",
    status: str = "completed",
    model: str = "test-agent",
) -> Response:
    """Create a minimal Response for testing."""
    return Response(
        id=response_id,
        status=status,
        model=model,
    )


class FakeSession:
    """Fake session that yields pre-defined events."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def send(
        self,
        input: Any,
        *,
        files: Any = None,
    ) -> AsyncIterator[Any]:
        for event in self._events:
            yield event


@pytest.fixture()
def block_stream() -> BlockStream:
    return BlockStream(text_flush_threshold=10)


@pytest.mark.asyncio()
async def test_simple_text_response(block_stream: BlockStream) -> None:
    """Simple text → ResponseStart, TextChunks, TextDone, ResponseEnd."""
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            ResponseInProgress(response=_make_response(status="in_progress")),
            TextDelta(delta="Hello "),
            TextDelta(delta="world!"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    types = [type(b).__name__ for b in blocks]

    assert "ResponseStartBlock" in types
    assert "TextDone" in types
    assert "ResponseEndBlock" in types

    # Verify TextDone has the full text.
    text_done = next(b for b in blocks if isinstance(b, TextDone))
    assert text_done.full_text == "Hello world!"
    assert not text_done.has_code_blocks


@pytest.mark.asyncio()
async def test_message_done_content_without_deltas_renders_text(
    block_stream: BlockStream,
) -> None:
    """
    Completed message items render as ``TextChunk`` + ``TextDone``
    without preceding deltas.

    Native terminal bridges such as Claude mirror completed transcript
    items through ``response.output_item.done``; the reducer must
    emit a ``TextChunk`` so consumers that forward only ``TextChunk``
    text (``Session._stream_chunks`` backing ``Session.query(...,
    stream=True)``) still see the assistant message.
    """
    session = FakeSession(
        [
            MessageDone(content=[{"type": "output_text", "text": "hello from transcript"}]),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]

    text_chunks = [b for b in blocks if isinstance(b, TextChunk)]
    text_dones = [b for b in blocks if isinstance(b, TextDone)]

    assert len(text_chunks) == 1
    assert "".join(c.text for c in text_chunks) == "hello from transcript"
    assert len(text_dones) == 1
    assert text_dones[0].full_text == "hello from transcript"
    assert not text_dones[0].has_code_blocks


@pytest.mark.asyncio()
async def test_message_done_without_deltas_yields_text_chunk(
    block_stream: BlockStream,
) -> None:
    """
    Regression test for the review finding at ``_stream.py:492``.

    When ``MessageDone`` arrives with content but no preceding text
    deltas (the claude-native transcript-mirror path), the reducer
    must synthesize a ``TextChunk`` carrying the full message text
    before the closing ``TextDone``. Without the chunk,
    ``Session.query(..., stream=True)`` yields nothing because the
    user-facing generator only forwards ``TextChunk`` text.

    Uses multi-block content to also exercise the concatenation in
    ``_output_text_from_message_content``.
    """
    session = FakeSession(
        [
            MessageDone(
                content=[
                    {"type": "output_text", "text": "foo "},
                    {"type": "output_text", "text": "bar"},
                ],
            ),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]

    text_chunks = [b for b in blocks if isinstance(b, TextChunk)]
    assert len(text_chunks) == 1
    assert "".join(c.text for c in text_chunks) == "foo bar"

    text_dones = [b for b in blocks if isinstance(b, TextDone)]
    assert len(text_dones) == 1
    assert text_dones[0].full_text == "foo bar"

    # TextChunk must precede TextDone — chunk announces, done closes.
    chunk_idx = next(i for i, b in enumerate(blocks) if isinstance(b, TextChunk))
    done_idx = next(i for i, b in enumerate(blocks) if isinstance(b, TextDone))
    assert chunk_idx < done_idx


@pytest.mark.asyncio()
async def test_text_with_code_blocks(block_stream: BlockStream) -> None:
    """Code fences in text → has_code_blocks=True."""
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            TextDelta(delta="```python\nprint('hi')\n```"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    text_done = next(b for b in blocks if isinstance(b, TextDone))
    assert text_done.has_code_blocks


@pytest.mark.asyncio()
async def test_reasoning_streams_chunks_live(block_stream: BlockStream) -> None:
    """
    Reasoning deltas must surface as :class:`ReasoningChunk` blocks
    while reasoning is in progress so the TUI can render live
    progress (e.g. Codex commands) instead of waiting until the
    section ends to dump a single panel.

    Contract: when chunks fire, the trailing :class:`ReasoningBlock`
    is suppressed — emitting both would make renderers show the same
    text twice (once streaming, once as a panel).
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            ReasoningStarted(),
            ReasoningDelta(delta="Let me think...\n"),
            ReasoningSummaryDelta(delta="Summary here\n"),
            TextDelta(delta="Answer"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    types = [type(b).__name__ for b in blocks]

    # Start indicator and at least one streamed chunk.
    assert "ReasoningStartBlock" in types
    chunk_texts = [b.text for b in blocks if isinstance(b, ReasoningChunk)]
    assert chunk_texts, (
        f"No ReasoningChunk emitted — reasoning would be invisible "
        f"during the section. Got: {types}"
    )
    # Both delta sources must reach the consumer (the executor maps
    # Codex events to ReasoningSummaryDelta; LLM-native reasoning
    # comes through ReasoningDelta). Concatenated chunk text must
    # contain content from both.
    joined = "".join(chunk_texts)
    assert "Let me think" in joined, (
        f"ReasoningDelta payload missing from chunks. Joined: {joined!r}"
    )
    assert "Summary here" in joined, (
        f"ReasoningSummaryDelta payload missing from chunks. Joined: {joined!r}"
    )

    # ReasoningBlock must be suppressed — chunks already covered it.
    assert "ReasoningBlock" not in types, (
        f"ReasoningBlock leaked alongside chunks; renderers would "
        f"show the same text twice. Got: {types}"
    )


@pytest.mark.asyncio()
async def test_consecutive_reasoning_blocks_are_separated(
    block_stream: BlockStream,
) -> None:
    """
    Summarized thinking arrives as several thinking blocks (each a fresh
    ``ReasoningStarted``). A separator must be inserted between them, else
    consecutive thought items render run together ("…item one.Item two").
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            ReasoningStarted(),
            ReasoningDelta(delta="First thought."),
            ReasoningStarted(),
            ReasoningDelta(delta="Second thought."),
            TextDelta(delta="Answer"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    joined = "".join(b.text for b in blocks if isinstance(b, ReasoningChunk))
    assert "First thought.\n\nSecond thought." in joined, joined
    assert "First thought.Second thought." not in joined, joined


@pytest.mark.asyncio()
async def test_reasoning_started_without_deltas_emits_block(
    block_stream: BlockStream,
) -> None:
    """
    Edge case: ``ReasoningStarted`` arrives but no deltas follow
    before the section closes. With no chunks to stream, the
    :class:`ReasoningBlock` must still fire so non-streaming
    renderers know reasoning happened (even if empty).
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            ReasoningStarted(),
            # No deltas — straight to text.
            TextDelta(delta="Direct answer"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    types = [type(b).__name__ for b in blocks]

    assert "ReasoningStartBlock" in types
    assert "ReasoningChunk" not in types
    # Block fires because no chunks did.
    assert "ReasoningBlock" in types
    block = next(b for b in blocks if isinstance(b, ReasoningBlock))
    assert block.reasoning_text == ""
    assert block.summary_text == ""


@pytest.mark.asyncio()
async def test_interleaved_text_reasoning_text_closes_each_section(
    block_stream: BlockStream,
) -> None:
    """
    Claude interleaved-thinking emits think→speak→think→speak inside ONE
    response with no tool call between. Reasoning must close the open
    text section (symmetric with ``TextDelta`` closing reasoning), else
    the pre-reasoning text orphans and the final ``TextDone`` concatenates
    both segments. Mirrors blockStream.test.ts.
    """
    t1 = "First answer here."
    t2 = "Second answer here."
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            ReasoningStarted(),
            ReasoningDelta(delta="plan it"),
            TextDelta(delta=t1),
            ReasoningStarted(),
            ReasoningDelta(delta="continue"),
            TextDelta(delta=t2),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    dones = [b for b in blocks if isinstance(b, TextDone)]
    assert [d.full_text for d in dones] == [t1, t2], (
        f"expected two separate text sections, got {[d.full_text for d in dones]}"
    )

    # The second reasoning start must land between the two closed texts.
    first_done = blocks.index(dones[0])
    second_done = blocks.index(dones[1])
    reasoning_starts = [i for i, b in enumerate(blocks) if isinstance(b, ReasoningStartBlock)]
    assert any(first_done < i < second_done for i in reasoning_starts), (
        f"reasoning_start not between the two texts; types={[type(b).__name__ for b in blocks]}"
    )


@pytest.mark.asyncio()
async def test_reasoning_delta_without_started_emits_implicit_start(
    block_stream: BlockStream,
) -> None:
    """
    Codex events arrive as bridged ``ReasoningSummaryDelta`` with
    no preceding ``ReasoningStarted`` (the executor maps directly
    from ``codex/event`` to deltas). The block stream must
    synthesize a :class:`ReasoningStartBlock` on the first delta
    so the formatter still gets its "thinking…" anchor.
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            # No ReasoningStarted — straight into a delta.
            ReasoningSummaryDelta(delta="$ ls /tmp\n"),
            TextDelta(delta="Result"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]

    start_idx = next(
        (i for i, b in enumerate(blocks) if isinstance(b, ReasoningStartBlock)),
        None,
    )
    chunk_idx = next(
        (i for i, b in enumerate(blocks) if isinstance(b, ReasoningChunk)),
        None,
    )
    assert start_idx is not None, (
        "Implicit ReasoningStartBlock missing — Codex-bridged deltas "
        "would arrive without a section header in the TUI."
    )
    assert chunk_idx is not None, "ReasoningChunk missing for the bridged delta."
    assert start_idx < chunk_idx, "Start block must precede the first chunk."


@pytest.mark.asyncio()
async def test_tool_group_with_results(block_stream: BlockStream) -> None:
    """ToolCall + ToolResult + next ResponseCreated → ToolGroup with output."""
    session = FakeSession(
        [
            ResponseCreated(response=_make_response(response_id="resp_1")),
            ToolCall(
                name="Read",
                arguments={"file_path": "/tmp/f"},
                call_id="c1",
                status="completed",
                agent_name="coder",
            ),
            ResponseCompleted(response=_make_response(response_id="resp_1")),
            # Client SDK yields ToolResult between iterations:
            ToolResult(call_id="c1", output="file content"),
            # Next iteration:
            ResponseCreated(response=_make_response(response_id="resp_2")),
            TextDelta(delta="Done"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response(response_id="resp_2")),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    tool_groups = [b for b in blocks if isinstance(b, ToolGroup)]

    # First ToolGroup: emitted immediately with output=None (call line).
    assert len(tool_groups) >= 1
    assert tool_groups[0].executions[0].name == "Read"

    # ToolResultBlock: emitted when result arrives.
    results = [b for b in blocks if isinstance(b, ToolResultBlock)]
    assert len(results) == 1
    assert results[0].output == "file content"


@pytest.mark.asyncio()
async def test_tool_result_arguments_override_pending_call_arguments(
    block_stream: BlockStream,
) -> None:
    """function_call_output arguments are preserved for result-only rendering."""
    session = FakeSession(
        [
            ResponseCreated(response=_make_response(response_id="resp_1")),
            ToolCall(
                name="sys_os_edit",
                arguments={},
                call_id="c1",
                status="action_required",
                agent_name="coder",
            ),
            ToolResult(
                call_id="c1",
                output='{"path":"/tmp/f.py","replacements":1,"bytes_written":20}',
                arguments={
                    "path": "/tmp/f.py",
                    "oldText": "print('old')\n",
                    "newText": "print('new')\n",
                },
            ),
            ResponseCompleted(response=_make_response(response_id="resp_1")),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]

    result = next(b for b in blocks if isinstance(b, ToolResultBlock))
    assert result.arguments["oldText"] == "print('old')\n"
    assert result.arguments["newText"] == "print('new')\n"


@pytest.mark.asyncio()
async def test_delayed_tool_result_after_text_uses_retained_call_metadata(
    block_stream: BlockStream,
) -> None:
    """
    A ``ToolResult`` that arrives after assistant text must still render.

    ``TextDelta`` clears ``pending_tools`` so completed tool panels
    don't bunch at the end of a turn. The reducer keeps a separate
    call-id metadata table so a later ``ToolResult`` can still use the
    original tool name and arguments instead of being dropped.
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response(response_id="resp_1")),
            ToolCall(
                name="Read",
                arguments={"file_path": "/tmp/f"},
                call_id="c1",
                status="completed",
                agent_name="coder",
            ),
            TextDelta(delta="Continuing while the tool is still running."),
            ToolResult(call_id="c1", output="late file content"),
            ResponseCompleted(response=_make_response(response_id="resp_1")),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]

    tool_groups = [b for b in blocks if isinstance(b, ToolGroup)]
    # One call card: the later text delta must not synthesize a duplicate call.
    assert len(tool_groups) == 1
    assert tool_groups[0].executions[0].name == "Read"

    results = [b for b in blocks if isinstance(b, ToolResultBlock)]
    # One result panel: before metadata retention this was zero.
    assert len(results) == 1
    assert results[0].call_id == "c1"
    assert results[0].name == "Read"
    assert results[0].output == "late file content"


@pytest.mark.asyncio()
async def test_block_context_agent_name(block_stream: BlockStream) -> None:
    """Blocks carry the agent name from the response."""
    session = FakeSession(
        [
            ResponseCreated(response=_make_response(model="my-agent")),
            TextDelta(delta="hi"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response(model="my-agent")),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]

    for block in blocks:
        assert block.ctx.agent == "my-agent"


@pytest.mark.asyncio()
async def test_text_chunk_flushing(block_stream: BlockStream) -> None:
    """Text chunks flush on newlines and word boundaries."""
    # block_stream has threshold=10
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            TextDelta(delta="short\nline two is longer than threshold characters"),
            MessageDone(content=[]),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    chunks = [b for b in blocks if isinstance(b, TextChunk)]

    # At least one chunk from the newline split.
    assert len(chunks) >= 1
    # First chunk should be "short\n" (from the newline).
    assert chunks[0].text == "short\n"


@pytest.mark.asyncio()
async def test_empty_response(block_stream: BlockStream) -> None:
    """Response with no text or tools → just start + end blocks."""
    session = FakeSession(
        [
            ResponseCreated(response=_make_response()),
            ResponseCompleted(response=_make_response()),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    types = [type(b).__name__ for b in blocks]

    assert types == ["ResponseStartBlock", "ResponseEndBlock"]


@pytest.mark.asyncio()
async def test_tool_call_dedupe_by_call_id_under_mcp_path(
    block_stream: BlockStream,
) -> None:
    """
    Two ``ToolCall`` events with the same ``call_id`` yield only
    ONE ``ToolGroup`` — the second occurrence is deduped.

    Why this matters: under the claude-sdk harness's MCP path,
    a single logical tool call surfaces as TWO ``ToolCall``
    events with correlated ``call_id``s — an inline observed
    event (``status="completed"``) emitted as the inner SDK
    parses the ``tool_use`` block, and a post-stream
    action_required event emitted when the SDK invokes the
    MCP-server handler. The adapter
    (``omnigent/runtime/harnesses/_executor_adapter.py``)
    threads the SDK's ``tool_use_id`` through both so they
    share a ``call_id``; this dedup is what keeps the REPL from
    rendering ``⏵ tool_name`` twice.

    Without the dedup, the user sees the same call line printed
    twice (the 2026-04-28 duplicate-render bug). This test
    pins the contract at the SDK-client boundary so a future
    refactor can't silently revive the duplicate render.
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response(response_id="resp_1")),
            # Inline observed event — fires as the inner SDK
            # parses the tool_use block, BEFORE the SDK invokes
            # the MCP handler. status="completed" matches the
            # adapter's _OBSERVED_TOOL_CALL_STATUS.
            ToolCall(
                name="sys_terminal_launch",
                arguments={"terminal": "shell", "session": "probe"},
                call_id="tool_use_xyz",
                status="completed",
                agent_name="agent",
            ),
            # Post-stream action_required event — fires when the
            # SDK's MCP handler chains through ctx.dispatch_tool.
            # Same call_id (correlated via the adapter's
            # ``_pending_mcp_call_ids`` queue).
            ToolCall(
                name="sys_terminal_launch",
                arguments={"terminal": "shell", "session": "probe"},
                call_id="tool_use_xyz",
                status="action_required",
                agent_name="agent",
            ),
            ToolResult(call_id="tool_use_xyz", output='{"ok": true}'),
            ResponseCompleted(response=_make_response(response_id="resp_1")),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    tool_groups = [b for b in blocks if isinstance(b, ToolGroup)]

    # Exactly one ToolGroup — the second ToolCall (same call_id)
    # was deduped. If this fails with len == 2, the dedup at
    # ``_stream.py``'s ToolCall handler regressed and the REPL
    # would render ``⏵ sys_terminal_launch`` twice.
    assert len(tool_groups) == 1, (
        f"Expected exactly 1 ToolGroup after dedup; got "
        f"{len(tool_groups)}: {tool_groups!r}. If 2, the "
        f"call_id-based dedup in ``_stream.py``'s ToolCall "
        f"branch is broken — the REPL will render the same "
        f"tool call twice."
    )
    assert tool_groups[0].executions[0].name == "sys_terminal_launch"
    assert tool_groups[0].executions[0].call_id == "tool_use_xyz"


@pytest.mark.asyncio()
async def test_tool_call_distinct_call_ids_yield_separate_groups(
    block_stream: BlockStream,
) -> None:
    """
    Two ``ToolCall`` events with DIFFERENT ``call_id``s yield
    TWO separate ``ToolGroup``s — the dedup is keyed on
    ``call_id``, not on ``(name, args)``.

    Why this matters: an LLM can legitimately invoke the same
    tool twice with the same arguments (e.g. retrying a transient
    failure). Each invocation is a distinct logical call with its
    own ``call_id``; collapsing them by ``(name, args)`` would
    lose the second call's render and confuse the user. This
    test pins the contract that dedup ONLY fires when ``call_id``
    matches.
    """
    session = FakeSession(
        [
            ResponseCreated(response=_make_response(response_id="resp_1")),
            ToolCall(
                name="Read",
                arguments={"path": "/tmp/x"},
                call_id="call_a",
                status="completed",
                agent_name="agent",
            ),
            ToolCall(
                name="Read",
                arguments={"path": "/tmp/x"},
                call_id="call_b",
                status="completed",
                agent_name="agent",
            ),
            ResponseCompleted(response=_make_response(response_id="resp_1")),
        ]
    )

    blocks = [b async for b in block_stream.stream(session, "test")]  # type: ignore[arg-type]
    tool_groups = [b for b in blocks if isinstance(b, ToolGroup)]

    assert len(tool_groups) == 2, (
        f"Two ToolCalls with distinct call_ids should yield 2 "
        f"ToolGroups; got {len(tool_groups)}. If 1, the dedup "
        f"is incorrectly collapsing by (name, args) instead of "
        f"by call_id — the second logical tool call would be "
        f"silently lost from the user's view."
    )
