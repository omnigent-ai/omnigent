"""Native event batches retain their source cursor until a tunnel acknowledgement."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omnigent.runner.transports.ws_tunnel import event_delivery as event_delivery_module
from omnigent.runner.transports.ws_tunnel.event_delivery import (
    RunnerEventDispatcher,
    TunnelEventClient,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    EventAckFrame,
    EventBatchFrame,
    EventReadyFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import _handle_tunnel_frame

_URL = "/v1/sessions/session-a/events"
_ITEM = {
    "type": "external_conversation_item",
    "data": {
        "source_id": "record-1",
        "item_type": "message",
        "item_data": {"role": "assistant", "content": []},
    },
}


def _client(dispatcher: RunnerEventDispatcher, http_posts: list[object]) -> TunnelEventClient:
    def handler(request: httpx.Request) -> httpx.Response:
        http_posts.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    return TunnelEventClient(
        event_dispatcher=dispatcher,
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    )


def _item_with_source(source_id: str) -> dict[str, Any]:
    """Build a durable event with a distinct source cursor."""
    return {
        "type": "external_conversation_item",
        "data": {
            "source_id": source_id,
            "item_type": "message",
            "item_data": {"role": "assistant", "content": []},
        },
    }


def _preview(delta: str) -> dict[str, Any]:
    """Build a best-effort text preview event."""
    return {
        "type": "external_output_text_delta",
        "data": {"delta": delta},
    }


def _event_batch_bytes(events: list[dict[str, Any]]) -> int:
    """Return the encoded size used by the tunnel admission check."""
    return len(encode_frame(EventBatchFrame("", "", events)).encode("utf-8"))


def _preview_at_batch_size(target_bytes: int) -> dict[str, Any]:
    """Build a preview whose encoded frame is exactly ``target_bytes``."""
    marker = "🙂"
    base_size = _event_batch_bytes([_preview("")])
    marker_size = _event_batch_bytes([_preview(marker)])
    marker_cost = marker_size - base_size
    assert target_bytes >= base_size + marker_cost
    delta = marker + "x" * (target_bytes - base_size - marker_cost)
    event = _preview(delta)
    assert _event_batch_bytes([event]) == target_bytes
    return event


async def _wait_event(event: asyncio.Event) -> None:
    """Wait for a test handshake without allowing a regression to hang."""
    await asyncio.wait_for(event.wait(), timeout=1)


async def _shutdown_dispatcher(dispatcher: RunnerEventDispatcher) -> None:
    """Cancel worker tasks created by a test dispatcher."""
    dispatcher.disconnected()
    workers = tuple(dispatcher._workers)
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


async def test_runner_handler_routes_ready_and_ack_to_delivery_queue() -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    sent: list[EventBatchFrame] = []

    async def noop_app(*_args: Any) -> None:
        pass

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame)
        await _handle_tunnel_frame(
            noop_app,
            encode_frame(EventAckFrame(frame.id, 1)),
            send,
            {},
            {},
            event_dispatcher=dispatcher,
        )

    await _handle_tunnel_frame(
        noop_app,
        encode_frame(EventReadyFrame()),
        send,
        {},
        {},
        event_dispatcher=dispatcher,
    )
    async with _client(dispatcher, http_posts) as client:
        response = await client.post(_URL, json=_ITEM)
    assert response.status_code == 202
    assert len(sent) == 1 and http_posts == []


async def test_acknowledged_item_uses_tunnel_not_http() -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    frames: list[EventBatchFrame] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    dispatcher.ready(send)
    async with _client(dispatcher, http_posts) as client:
        response = await client.post(_URL, json=_ITEM)
    assert response.status_code == 202
    assert frames[0].session_id == "session-a"
    assert frames[0].events == [_ITEM]
    assert http_posts == []


async def test_lost_ack_replays_after_tunnel_reconnect() -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    frames: list[EventBatchFrame] = []

    async def first_send(text: str) -> None:
        assert dispatcher.has_pending
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        # The server may already have applied it, but its ACK was lost.
        dispatcher.disconnected()
        asyncio.get_running_loop().call_soon(dispatcher.ready, second_send)

    async def second_send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        dispatcher.acknowledge(EventAckFrame(frame.id, 1))

    dispatcher.ready(first_send)
    async with _client(dispatcher, http_posts) as client:
        response = await asyncio.wait_for(client.post(_URL, json=_ITEM), timeout=2)
    assert response.status_code == 202
    assert [frame.events[0]["data"]["source_id"] for frame in frames] == ["record-1", "record-1"]
    assert http_posts == []
    assert not dispatcher.has_pending


async def test_single_item_child_array_preserves_http_acknowledgement() -> None:
    dispatcher = RunnerEventDispatcher()
    tunneled: list[str] = []
    posts: list[object] = []

    async def send(text: str) -> None:
        tunneled.append(text)

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(202, json=[{"queued": False, "item_id": "item-child"}])

    dispatcher.ready(send)
    async with TunnelEventClient(
        event_dispatcher=dispatcher,
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as client:
        response = await client.post(
            _URL,
            content=json.dumps([_ITEM]).encode(),
            headers={"content-type": "application/json"},
        )
    assert response.json() == [{"queued": False, "item_id": "item-child"}]
    assert posts == [[_ITEM]]
    assert tunneled == []


async def test_old_server_and_unacknowledged_child_batch_use_http() -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    async with _client(dispatcher, http_posts) as client:
        response = await client.post(_URL, json=_ITEM)
        child = await client.post(_URL, json=[_ITEM, _ITEM])
    assert response.status_code == child.status_code == 202
    assert http_posts == [_ITEM, [_ITEM, _ITEM]]


async def test_reconnect_to_old_server_retries_pending_item_over_http() -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    started = asyncio.Event()

    async def first_send(_text: str) -> None:
        started.set()
        dispatcher.disconnected()

    async def old_server_send(_text: str) -> None:
        raise AssertionError("old server must never receive an event batch")

    dispatcher.ready(first_send)
    async with _client(dispatcher, http_posts) as client:
        pending = asyncio.create_task(client.post(_URL, json=_ITEM))
        await started.wait()
        dispatcher.connected(old_server_send)
        response = await asyncio.wait_for(pending, timeout=2)
        # Unsupported is cached for this generation; no second negotiation delay.
        second = await asyncio.wait_for(client.post(_URL, json=_ITEM), timeout=0.35)
    assert response.status_code == second.status_code == 202
    assert http_posts == [_ITEM, _ITEM]


async def test_cancelled_delivery_does_not_send_stale_item_after_reconnect() -> None:
    dispatcher = RunnerEventDispatcher()
    sent: list[list[dict[str, Any]]] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame.events)
        dispatcher.acknowledge(EventAckFrame(frame.id, 1))

    dispatcher.ready(send)
    dispatcher.disconnected()
    abandoned = [asyncio.create_task(dispatcher.submit("session-a", [_ITEM])) for _ in range(2)]
    for _ in range(20):
        if dispatcher._queue.empty():
            break
        await asyncio.sleep(0.01)
    assert dispatcher._queue.empty()  # Both workers are waiting for the tunnel.
    for task in abandoned:
        task.cancel()
    await asyncio.gather(*abandoned, return_exceptions=True)
    dispatcher.ready(send)
    fresh = {**_ITEM, "data": {**_ITEM["data"], "source_id": "fresh-record"}}
    ack = await asyncio.wait_for(dispatcher.submit("session-a", [fresh]), timeout=1)
    assert ack.applied == 1
    assert sent == [[fresh]]


async def test_synthetic_ack_invokes_http_response_hooks() -> None:
    dispatcher = RunnerEventDispatcher()
    hooks: list[int] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        dispatcher.acknowledge(EventAckFrame(frame.id, 1))

    async def response_hook(response: httpx.Response) -> None:
        hooks.append(response.status_code)

    dispatcher.ready(send)
    async with _client(dispatcher, []) as client:
        client.event_hooks["response"].append(response_hook)
        await client.post(_URL, json=_ITEM)
    assert hooks == [202]


async def test_preview_drops_when_negotiated_tunnel_is_down() -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []

    async def send(_text: str) -> None:
        raise AssertionError("disconnected tunnel must not send")

    dispatcher.ready(send)
    dispatcher.disconnected()
    preview = {"type": "external_output_text_delta", "data": {"delta": "hi"}}
    async with _client(dispatcher, http_posts) as client:
        response = await asyncio.wait_for(client.post(_URL, json=preview), timeout=3)
    assert response.status_code == 503
    assert http_posts == []


async def test_durable_partial_retryable_ack_replays_only_unapplied_suffix() -> None:
    """A partial durable ACK retries the suffix without duplicating the prefix."""
    dispatcher = RunnerEventDispatcher()
    events = [_item_with_source("record-1"), _item_with_source("record-2")]
    frames: list[EventBatchFrame] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        if len(frames) == 1:
            dispatcher.acknowledge(EventAckFrame(frame.id, applied=1, retryable=True))
        else:
            dispatcher.acknowledge(EventAckFrame(frame.id, applied=len(frame.events)))

    dispatcher.ready(send)
    try:
        ack = await dispatcher.submit("session-a", events)
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert ack.applied == len(events)
    assert [[event["data"]["source_id"] for event in frame.events] for frame in frames] == [
        ["record-1", "record-2"],
        ["record-2"],
    ]


async def test_preview_partial_non_retryable_ack_returns_422() -> None:
    """A server rejection of a preview suffix is surfaced as a client error."""
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    events = [_preview("one"), _preview("two")]

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        dispatcher.acknowledge(
            EventAckFrame(frame.id, applied=1, error="invalid preview", retryable=False)
        )

    dispatcher.ready(send)
    try:
        async with _client(dispatcher, http_posts) as client:
            response = await client.post(_URL, json=events)
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid preview"}
    assert http_posts == []


async def test_preview_retryable_partial_ack_returns_503_without_replay() -> None:
    """Preview backpressure is transient and must not be sent over HTTP."""
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    events = [_preview("one"), _preview("two")]

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=0, retryable=True))

    dispatcher.ready(send)
    try:
        async with _client(dispatcher, http_posts) as client:
            response = await client.post(_URL, json=events)
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert response.status_code == 503
    assert http_posts == []


@pytest.mark.parametrize("applied", [-1, 2])
async def test_invalid_ack_bounds_fail_closed(applied: int) -> None:
    """An ACK outside ``[0, len(events)]`` cannot report success."""
    dispatcher = RunnerEventDispatcher()

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=applied))

    dispatcher.ready(send)
    try:
        with pytest.raises(ValueError, match="invalid event acknowledgement"):
            await dispatcher.submit("session-a", [_item_with_source("record-1")])
    finally:
        await _shutdown_dispatcher(dispatcher)


async def test_same_session_submissions_are_serialized() -> None:
    """A second turn for one session waits for the first ACK."""
    dispatcher = RunnerEventDispatcher()
    first_event = _item_with_source("record-1")
    second_event = _item_with_source("record-2")
    first_sent = asyncio.Event()
    second_submitted = asyncio.Event()
    release_first = asyncio.Event()
    second_sent = asyncio.Event()
    frames: list[EventBatchFrame] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        if frame.events == [first_event]:
            first_sent.set()
            await release_first.wait()
        else:
            second_sent.set()
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=len(frame.events)))

    async def submit_second() -> EventAckFrame:
        second_submitted.set()
        return await dispatcher.submit("session-a", [second_event])

    dispatcher.ready(send)
    tasks: list[asyncio.Task[EventAckFrame]] = []
    try:
        tasks.append(asyncio.create_task(dispatcher.submit("session-a", [first_event])))
        await _wait_event(first_sent)
        tasks.append(asyncio.create_task(submit_second()))
        await _wait_event(second_submitted)

        async def wait_for_second_to_leave_queue() -> None:
            while dispatcher._queue.qsize() > 0:
                await asyncio.sleep(0)

        # The second worker must dequeue B before this assertion. With the
        # per-session lock intact it then waits on A; removing the lock lets
        # B's send callback run and flips ``second_sent``.
        await asyncio.wait_for(wait_for_second_to_leave_queue(), timeout=1)
        await asyncio.sleep(0)
        assert not second_sent.is_set()

        release_first.set()
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _shutdown_dispatcher(dispatcher)

    assert [frame.events for frame in frames] == [[first_event], [second_event]]


async def test_different_sessions_deliver_independently() -> None:
    """A stalled session does not block another session's event delivery."""
    dispatcher = RunnerEventDispatcher()
    session_a_sent = asyncio.Event()
    release_a = asyncio.Event()
    session_b_sent = asyncio.Event()
    frames: list[EventBatchFrame] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        if frame.session_id == "session-a":
            session_a_sent.set()
            await release_a.wait()
        else:
            session_b_sent.set()
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=len(frame.events)))

    dispatcher.ready(send)
    tasks: list[asyncio.Task[EventAckFrame]] = []
    try:
        tasks.append(
            asyncio.create_task(dispatcher.submit("session-a", [_item_with_source("record-a")]))
        )
        await _wait_event(session_a_sent)
        tasks.append(
            asyncio.create_task(dispatcher.submit("session-b", [_item_with_source("record-b")]))
        )
        await _wait_event(session_b_sent)
        await asyncio.wait_for(tasks[1], timeout=1)
        assert not tasks[0].done()

        release_a.set()
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await _shutdown_dispatcher(dispatcher)

    assert [frame.session_id for frame in frames] == ["session-a", "session-b"]


async def test_reconnect_ignores_stale_ack_from_previous_generation() -> None:
    """An ACK from a dead tunnel generation cannot settle the new attempt."""
    dispatcher = RunnerEventDispatcher()
    old_batch_id: str | None = None
    new_batch_id: str | None = None
    new_sent = asyncio.Event()
    stale_ack_sent = asyncio.Event()
    release_new_send = asyncio.Event()
    send_returned = asyncio.Event()

    async def second_send(text: str) -> None:
        nonlocal new_batch_id
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        new_batch_id = frame.id
        new_sent.set()
        assert old_batch_id is not None
        dispatcher.acknowledge(EventAckFrame(old_batch_id, applied=1))
        stale_ack_sent.set()
        await release_new_send.wait()
        send_returned.set()

    async def first_send(text: str) -> None:
        nonlocal old_batch_id
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        old_batch_id = frame.id
        dispatcher.disconnected()
        dispatcher.ready(second_send)

    dispatcher.ready(first_send)
    task = asyncio.create_task(dispatcher.submit("session-a", [_item_with_source("record-1")]))
    try:
        await _wait_event(new_sent)
        await _wait_event(stale_ack_sent)
        assert not task.done()
        assert old_batch_id is not None and new_batch_id is not None
        assert old_batch_id != new_batch_id
        release_new_send.set()
        await _wait_event(send_returned)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
        dispatcher.acknowledge(EventAckFrame(new_batch_id, applied=1))
        ack = await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _shutdown_dispatcher(dispatcher)

    assert ack.applied == 1


async def test_unsupported_event_payload_uses_http_fallback() -> None:
    """Events outside the tunnel contract retain the normal HTTP path."""
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    body = {"type": "unsupported_event", "data": {"value": "kept"}}

    async def send(_text: str) -> None:
        raise AssertionError("unsupported events must not enter the tunnel")

    dispatcher.ready(send)
    try:
        async with _client(dispatcher, http_posts) as client:
            response = await client.post(_URL, json=body)
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert response.status_code == 202
    assert http_posts == [body]


async def test_event_count_limit_accepts_boundary_and_falls_back_after_it() -> None:
    """The maximum event count tunnels; one more event uses HTTP."""
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    exact_events = [_preview(str(index)) for index in range(event_delivery_module._MAX_EVENTS)]
    oversized_events = [
        _preview(str(index)) for index in range(event_delivery_module._MAX_EVENTS + 1)
    ]
    tunneled: list[list[dict[str, Any]]] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        tunneled.append(frame.events)
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=len(frame.events)))

    dispatcher.ready(send)
    try:
        async with _client(dispatcher, http_posts) as client:
            exact_response = await client.post(_URL, json=exact_events)
            oversized_response = await client.post(_URL, json=oversized_events)
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert exact_response.status_code == oversized_response.status_code == 202
    assert tunneled == [exact_events]
    assert http_posts == [oversized_events]


async def test_event_byte_limit_accepts_utf8_boundary_and_falls_back_after_it() -> None:
    """The encoded byte limit admits exactly the boundary and rejects +1."""
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    max_bytes = event_delivery_module._MAX_BATCH_BYTES
    exact_event = _preview_at_batch_size(max_bytes)
    oversized_event = _preview_at_batch_size(max_bytes + 1)
    exact_events = [exact_event]
    oversized_events = [oversized_event]
    tunneled: list[list[dict[str, Any]]] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        tunneled.append(frame.events)
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=len(frame.events)))

    dispatcher.ready(send)
    try:
        async with _client(dispatcher, http_posts) as client:
            exact_response = await client.post(_URL, json=exact_events)
            oversized_response = await client.post(_URL, json=oversized_events)
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert _event_batch_bytes(exact_events) == max_bytes
    assert _event_batch_bytes(oversized_events) == max_bytes + 1
    assert exact_response.status_code == oversized_response.status_code == 202
    assert tunneled == [exact_events]
    assert http_posts == [oversized_events]


async def test_percent_encoded_session_id_is_decoded_for_tunnel_delivery() -> None:
    """Tunnel frames carry the logical session id, not its URL encoding."""
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    session_ids: list[str] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        session_ids.append(frame.session_id)
        dispatcher.acknowledge(EventAckFrame(frame.id, applied=len(frame.events)))

    dispatcher.ready(send)
    try:
        async with _client(dispatcher, http_posts) as client:
            response = await client.post(
                "/v1/sessions/session%2Fchild/events",
                json=_preview("hi"),
            )
    finally:
        await _shutdown_dispatcher(dispatcher)

    assert response.status_code == 202
    assert session_ids == ["session/child"]
    assert http_posts == []
