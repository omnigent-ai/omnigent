"""Composable stream transforms for stream blocks.

Each transform is an async generator function that wraps a block
stream. Compose with ``pipe()``:

    stream = pipe(
        block_stream.stream(session, text),
        skip_blocks(ReasoningBlock),
        skip_intermediate_ends(),
    )
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from ._blocks import (
    AnyBlock,
    ResponseEndBlock,
    TextDone,
)

StreamTransform = Callable[[AsyncIterator[AnyBlock]], AsyncIterator[AnyBlock]]


def pipe(stream: AsyncIterator[AnyBlock], *transforms: StreamTransform) -> AsyncIterator[AnyBlock]:
    """Compose transforms left-to-right.

    ``pipe(stream, t1, t2)`` is equivalent to ``t2(t1(stream))``.
    """
    for t in transforms:
        stream = t(stream)
    return stream


def skip_blocks(*types: type) -> StreamTransform:
    """Drop blocks of the given types.

    Usage::

        stream = skip_blocks(ReasoningBlock)(renderer.stream(...))
    """

    def _transform(stream: AsyncIterator[AnyBlock]) -> AsyncIterator[AnyBlock]:
        async def _inner() -> AsyncIterator[AnyBlock]:
            async for block in stream:
                if not isinstance(block, tuple(types)):
                    yield block

        return _inner()

    return _transform


def skip_intermediate_ends() -> StreamTransform:
    """Suppress ``ResponseEndBlock`` events from tool loop iterations.

    Only yields the final ``ResponseEndBlock`` — the one not followed
    by another block from the same turn.
    """

    def _transform(stream: AsyncIterator[AnyBlock]) -> AsyncIterator[AnyBlock]:
        async def _inner() -> AsyncIterator[AnyBlock]:
            pending_end: ResponseEndBlock | None = None
            async for block in stream:
                if isinstance(block, ResponseEndBlock):
                    # Buffer it — might not be the last one.
                    pending_end = block
                else:
                    # A non-end block arrived, so the buffered end
                    # was intermediate. Drop it.
                    pending_end = None
                    yield block
            # Stream ended. The buffered end IS the final one.
            if pending_end is not None:
                yield pending_end

        return _inner()

    return _transform


def merge_text_across_iterations() -> StreamTransform:
    """Merge ``TextDone`` blocks across tool loop iterations.

    When the tool loop runs N iterations, each may produce a
    ``TextDone``. This merges them into a single ``TextDone`` at
    the end. ``TextChunk`` blocks pass through unchanged.
    """

    def _transform(stream: AsyncIterator[AnyBlock]) -> AsyncIterator[AnyBlock]:
        async def _inner() -> AsyncIterator[AnyBlock]:
            accumulated = ""
            async for block in stream:
                if isinstance(block, TextDone):
                    accumulated += block.full_text
                elif isinstance(block, ResponseEndBlock):
                    if accumulated:
                        yield TextDone(
                            full_text=accumulated,
                            has_code_blocks="```" in accumulated,
                            ctx=block.ctx,
                        )
                        accumulated = ""
                    yield block
                else:
                    yield block
            # Edge case: stream ends without ResponseEndBlock.
            if accumulated:
                yield TextDone(
                    full_text=accumulated,
                    has_code_blocks="```" in accumulated,
                )

        return _inner()

    return _transform


def only_agent(agent_name: str | None) -> StreamTransform:
    """Filter to blocks from a specific agent.

    Pass ``None`` to include all agents (no filtering).

    Usage::

        stream = only_agent("coder.researcher")(renderer.stream(...))

    :param agent_name: Agent name to filter by, or ``None`` for all.
    """

    def _transform(stream: AsyncIterator[AnyBlock]) -> AsyncIterator[AnyBlock]:
        async def _inner() -> AsyncIterator[AnyBlock]:
            async for block in stream:
                if agent_name is None or block.ctx.agent == agent_name:
                    yield block

        return _inner()

    return _transform
