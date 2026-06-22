"""Unit tests for stream transforms."""

from __future__ import annotations

import pytest
from omnigent_client._blocks import (
    BlockContext,
    ReasoningBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    TextChunk,
    TextDone,
)
from omnigent_client._transforms import (
    merge_text_across_iterations,
    only_agent,
    pipe,
    skip_blocks,
    skip_intermediate_ends,
)


async def _to_list(stream):  # type: ignore[no-untyped-def]
    return [b async for b in stream]


async def _aiter(*blocks):  # type: ignore[no-untyped-def]
    for b in blocks:
        yield b


@pytest.mark.asyncio()
async def test_skip_blocks_removes_reasoning() -> None:
    """skip_blocks(ReasoningBlock) drops reasoning blocks."""
    stream = _aiter(
        ResponseStartBlock(model="test", response_id="r1"),
        ReasoningBlock(reasoning_text="think", summary_text="sum"),
        TextChunk(text="hello"),
        TextDone(full_text="hello"),
        ResponseEndBlock(status="completed"),
    )

    result = await _to_list(skip_blocks(ReasoningBlock)(stream))
    types = [type(b).__name__ for b in result]

    assert "ReasoningBlock" not in types
    assert "TextChunk" in types
    assert "ResponseEndBlock" in types


@pytest.mark.asyncio()
async def test_skip_blocks_multiple_types() -> None:
    """skip_blocks can drop multiple types at once."""
    stream = _aiter(
        ResponseStartBlock(model="test", response_id="r1"),
        ReasoningBlock(reasoning_text="t", summary_text="s"),
        TextChunk(text="hi"),
        ResponseEndBlock(status="completed"),
    )

    result = await _to_list(skip_blocks(ReasoningBlock, TextChunk)(stream))
    types = [type(b).__name__ for b in result]

    assert types == ["ResponseStartBlock", "ResponseEndBlock"]


@pytest.mark.asyncio()
async def test_skip_intermediate_ends() -> None:
    """Only the final ResponseEndBlock survives."""
    stream = _aiter(
        ResponseStartBlock(model="test", response_id="r1"),
        ResponseEndBlock(status="completed"),  # intermediate
        TextChunk(text="hi"),
        ResponseEndBlock(status="completed"),  # final
    )

    result = await _to_list(skip_intermediate_ends()(stream))
    ends = [b for b in result if isinstance(b, ResponseEndBlock)]

    # Only the final one (no blocks after it).
    assert len(ends) == 1


@pytest.mark.asyncio()
async def test_skip_intermediate_ends_keeps_single() -> None:
    """A single ResponseEndBlock is kept."""
    stream = _aiter(
        ResponseStartBlock(model="test", response_id="r1"),
        ResponseEndBlock(status="completed"),
    )

    result = await _to_list(skip_intermediate_ends()(stream))
    ends = [b for b in result if isinstance(b, ResponseEndBlock)]
    assert len(ends) == 1


@pytest.mark.asyncio()
async def test_merge_text_across_iterations() -> None:
    """Multiple TextDone blocks merge into one."""
    stream = _aiter(
        ResponseStartBlock(model="test", response_id="r1"),
        TextDone(full_text="Part 1. "),
        TextDone(full_text="Part 2."),
        ResponseEndBlock(status="completed"),
    )

    result = await _to_list(merge_text_across_iterations()(stream))
    text_dones = [b for b in result if isinstance(b, TextDone)]

    # Merged into one.
    assert len(text_dones) == 1
    assert text_dones[0].full_text == "Part 1. Part 2."


@pytest.mark.asyncio()
async def test_merge_text_detects_code_blocks() -> None:
    """Merged text detects code blocks from any fragment."""
    stream = _aiter(
        TextDone(full_text="text before "),
        TextDone(full_text="```python\ncode\n```"),
        ResponseEndBlock(status="completed"),
    )

    result = await _to_list(merge_text_across_iterations()(stream))
    text_done = next(b for b in result if isinstance(b, TextDone))
    assert text_done.has_code_blocks


@pytest.mark.asyncio()
async def test_only_agent_filters() -> None:
    """only_agent filters to blocks from a specific agent."""
    ctx_root = BlockContext(agent="coder")
    ctx_sub = BlockContext(agent="coder.researcher")

    stream = _aiter(
        TextChunk(text="root text", ctx=ctx_root),
        TextChunk(text="sub text", ctx=ctx_sub),
        ResponseEndBlock(status="completed", ctx=ctx_root),
    )

    result = await _to_list(only_agent("coder.researcher")(stream))
    assert len(result) == 1
    assert isinstance(result[0], TextChunk)
    assert result[0].text == "sub text"


@pytest.mark.asyncio()
async def test_pipe_composes_transforms() -> None:
    """pipe() chains transforms left-to-right."""
    stream = _aiter(
        ResponseStartBlock(model="test", response_id="r1"),
        ReasoningBlock(reasoning_text="t", summary_text="s"),
        TextChunk(text="hi"),
        ResponseEndBlock(status="completed"),  # intermediate
        TextChunk(text="more"),
        ResponseEndBlock(status="completed"),  # final
    )

    result = await _to_list(
        pipe(
            stream,
            skip_blocks(ReasoningBlock),
            skip_intermediate_ends(),
        )
    )
    types = [type(b).__name__ for b in result]

    assert "ReasoningBlock" not in types
    # Only one end block.
    assert types.count("ResponseEndBlock") == 1
    # Final end is the last block.
    assert types[-1] == "ResponseEndBlock"
