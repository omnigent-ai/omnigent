"""
SDK SSE-parser coverage for the async-dispatch protocol.

Two parser surfaces this file pins:

1. ``response.client_task.cancel`` — the server emits this when an
   in-flight ``kind="client_tool"`` task is cancelled (direct or
   via parent-cancel propagation). The SDK must surface a typed
   :class:`ClientTaskCancel` event so consumers can cancel their
   local asyncio task; otherwise the cancelled tool body keeps
   running and wastes compute.
2. The function_call → function_call_output handshake under
   async dispatch — the LLM dispatches a tool, the server emits
   the ``_AsyncToolHandle`` JSON inline as the FCO output, and
   downstream consumers parse it to extract ``task_id``. The
   parser additions for the async-dispatch protocol must not
   regress that path.

Historical note: this file used to also pin the SDK's
``build_tool_handler`` schema-injection behavior for
``@tool(synchronous=False)`` — that decorator was removed once
``sys_call_async`` shipped (step 11 of the harness contract), and
the corresponding SDK schema injection went with it. The remaining
SSE parser coverage stays here because no other test file owns it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from omnigent_client._events import ClientTaskCancel, ToolCall, ToolResult
from omnigent_client._sse import parse_sse_stream


async def _bytes(*frames: bytes) -> AsyncIterator[bytes]:
    """
    Yield each frame as a discrete chunk (mimics httpx streaming).

    :param frames: One or more raw SSE byte frames to feed into
        :func:`parse_sse_stream`. Each frame is yielded as its
        own chunk so the parser sees realistic
        ``aiter_bytes()`` boundaries instead of one giant
        concatenated buffer.
    :yields: Each frame's bytes verbatim, in argument order.
    """
    for frame in frames:
        yield frame


@pytest.mark.asyncio
async def test_sse_parser_emits_client_task_cancel() -> None:
    """
    The server emits ``response.client_task.cancel`` when an
    in-flight ``kind="client_tool"`` task is cancelled (direct
    or via parent-cancel propagation). The SDK must surface this
    as a typed :class:`ClientTaskCancel` so consumers can cancel
    their local asyncio task. If the parser silently drops the
    event, the client would keep running the cancelled tool body
    indefinitely and waste compute / hold resources.
    """
    frame = (
        b"event: response.client_task.cancel\n"
        b'data: {"task_id": "task_abc123", "type": "response.client_task.cancel"}\n'
        b"\n"
    )

    events = []
    async for event in parse_sse_stream(_bytes(frame)):
        events.append(event)

    # Exactly one event — the parser must not produce duplicates.
    assert len(events) == 1, (
        f"Expected exactly one ClientTaskCancel event from the SSE "
        f"frame; got {len(events)}: {events!r}"
    )
    assert isinstance(events[0], ClientTaskCancel), (
        f"Expected ClientTaskCancel, got {type(events[0]).__name__}"
    )
    assert events[0].task_id == "task_abc123"


@pytest.mark.asyncio
async def test_sse_parser_drops_client_task_cancel_without_task_id() -> None:
    """
    A malformed ``response.client_task.cancel`` (no ``task_id``
    or empty string) must be dropped — emitting a
    :class:`ClientTaskCancel` with an empty ``task_id`` would
    cause the consumer to no-op silently or, worse, cancel the
    wrong local task if the consumer falls back on positional
    matching. Catches a server-side regression where the cancel
    payload loses its task_id.
    """
    frame = (
        b"event: response.client_task.cancel\n"
        b'data: {"type": "response.client_task.cancel"}\n'  # no task_id
        b"\n"
    )

    events = []
    async for event in parse_sse_stream(_bytes(frame)):
        events.append(event)

    assert events == [], f"Malformed cancel frame must be dropped; got {events!r}"


@pytest.mark.asyncio
async def test_sse_parser_terminates_on_bare_done_sentinel() -> None:
    """
    The server's terminal sentinel is a bare ``data: [DONE]`` with NO
    preceding ``event:`` line (see ``_stream_live_events``). The parser must
    detect it regardless of the current event and stop — emitting no events
    and ignoring anything after it.

    Regression guard: the ``[DONE]`` check used to be gated behind a non-null
    ``current_event``, so a bare sentinel was never recognized. That made a
    deliberate server close indistinguishable from a transport drop, which
    (for the web client's reconnect loop) means treating every clean close as
    droppable and re-subscribing forever.
    """
    frames = (
        b"data: [DONE]\n\n",
        # Must be ignored — the parser returns at the sentinel.
        b"event: response.output_text.delta\n"
        b'data: {"delta": "ghost", "type": "response.output_text.delta"}\n\n',
    )

    events = []
    async for event in parse_sse_stream(_bytes(*frames)):
        events.append(event)

    # Zero events: the bare [DONE] terminated parsing before the post-sentinel
    # delta. A non-empty list means [DONE] wasn't detected without an
    # ``event:`` line (the prior bug).
    assert events == [], f"Bare [DONE] must terminate parsing; got {events!r}"


# ── Sanity: existing event shapes still parse ──────────────


@pytest.mark.asyncio
async def test_sse_parser_unchanged_for_function_call_output() -> None:
    """
    The async-dispatch protocol piggybacks on the existing
    ``response.output_item.done`` event for ``function_call``
    and ``function_call_output`` items — the handle JSON
    arrives as the ``output`` field on a normal FCO. This test
    proves the parser additions did not regress that path: a
    typical async-dispatch sequence (function_call →
    function_call_output with handle JSON) must still produce
    :class:`ToolCall` + :class:`ToolResult` events.
    """
    frames = (
        # function_call (the LLM's call to the async tool)
        (
            b"event: response.output_item.done\n"
            b'data: {"item": {"type": "function_call", '
            b'"name": "_async_long_compute", '
            b'"arguments": "{\\"n\\": 5}", '
            b'"call_id": "call_abc", '
            b'"status": "completed", '
            b'"model": "test-agent"}}\n'
            b"\n"
        ),
        # function_call_output (the handle JSON the server emits inline)
        (
            b"event: response.output_item.done\n"
            b'data: {"item": {"type": "function_call_output", '
            b'"call_id": "call_abc", '
            b'"output": "{\\"task_id\\": \\"task_xyz\\", '
            b'\\"kind\\": \\"client_tool\\"}"}}\n'
            b"\n"
        ),
    )

    events = []
    async for event in parse_sse_stream(_bytes(*frames)):
        events.append(event)

    assert len(events) == 2, f"Expected ToolCall + ToolResult; got {len(events)}: {events!r}"
    call_event = events[0]
    result_event = events[1]
    assert isinstance(call_event, ToolCall)
    assert call_event.name == "_async_long_compute"
    assert call_event.call_id == "call_abc"
    assert call_event.arguments == {"n": 5}

    assert isinstance(result_event, ToolResult)
    assert result_event.call_id == "call_abc"
    # The handle JSON arrives verbatim as the FCO output —
    # downstream consumers parse it to extract task_id.
    assert "task_xyz" in result_event.output
    assert "client_tool" in result_event.output
