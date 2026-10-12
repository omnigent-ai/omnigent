"""Native event batches retain their source cursor until a tunnel acknowledgement."""

from __future__ import annotations

import asyncio
import gc
import json
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from omnigent.errors import ErrorCode
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


@pytest.mark.parametrize("bytes_over_limit", [-1, 0, 1])
@pytest.mark.parametrize("path_session_id", ["session-a", "session%2Dwith%2Descaped%2Didentifier"])
async def test_full_wire_frame_size_selects_http_fallback(
    bytes_over_limit: int, path_session_id: str
) -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    frames: list[EventBatchFrame] = []
    session_id = unquote(path_session_id)
    item = {
        **_ITEM,
        "data": {
            **_ITEM["data"],
            "item_data": {"role": "assistant", "content": [{"type": "output_text", "text": ""}]},
        },
    }
    frame_bytes = len(encode_frame(EventBatchFrame("0" * 32, session_id, [item])).encode())
    item["data"]["item_data"]["content"][0]["text"] = "x" * (
        256 * 1024 - frame_bytes + bytes_over_limit
    )

    async def send(text: str) -> None:
        # This is the limit enforced by the receiving server, including the envelope.
        assert len(text.encode()) <= 256 * 1024
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        frames.append(frame)
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    dispatcher.ready(send)
    async with _client(dispatcher, http_posts) as client:
        response = await asyncio.wait_for(
            client.post(f"/v1/sessions/{path_session_id}/events", json=item), timeout=2
        )
    assert response.status_code == 202
    if bytes_over_limit > 0:
        assert http_posts == [item]
        assert frames == []
    else:
        assert http_posts == []
        assert len(frames) == 1
        assert frames[0].session_id == session_id
        assert frames[0].events == [item]


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


async def test_stalled_session_leaves_a_worker_for_other_sessions() -> None:
    dispatcher = RunnerEventDispatcher()
    started = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame.session_id)
        if frame.session_id == "session-a" and not release.is_set():
            started.set()
            await release.wait()
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    dispatcher.ready(send)
    first = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
    tasks = [first]
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        second = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
        tasks.append(second)
        await asyncio.sleep(0)
        other = asyncio.create_task(dispatcher.submit("session-b", [_ITEM]))
        tasks.append(other)
        assert (await asyncio.wait_for(other, timeout=2)).applied == 1
        assert sent == ["session-a", "session-b"]
        release.set()
        assert [ack.applied for ack in await asyncio.gather(first, second)] == [1, 1]
        assert sent == ["session-a", "session-b", "session-a"]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        for worker in dispatcher._workers:
            worker.cancel()
        await asyncio.gather(*dispatcher._workers, return_exceptions=True)


async def test_cancelled_admission_does_not_send_or_block_a_later_item() -> None:
    dispatcher = RunnerEventDispatcher()
    started = asyncio.Event()
    release = asyncio.Event()
    sent: list[str] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        source_id = frame.events[0]["data"]["source_id"]
        sent.append(source_id)
        if source_id == "first":
            started.set()
            await release.wait()
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    def item(source_id: str) -> dict[str, Any]:
        return {**_ITEM, "data": {**_ITEM["data"], "source_id": source_id}}

    dispatcher.ready(send)
    first = asyncio.create_task(dispatcher.submit("session-a", [item("first")]))
    tasks = [first]
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        waiting = asyncio.create_task(dispatcher.submit("session-a", [item("cancelled")]))
        tasks.append(waiting)
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        release.set()
        assert (await asyncio.wait_for(first, timeout=2)).applied == 1
        assert (
            await asyncio.wait_for(dispatcher.submit("session-a", [item("fresh")]), 2)
        ).applied == 1
        assert sent == ["first", "fresh"]
        assert not dispatcher.has_pending
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        for worker in dispatcher._workers:
            worker.cancel()
        await asyncio.gather(*dispatcher._workers, return_exceptions=True)


async def test_cancelled_delivery_keeps_order_while_another_session_progresses() -> None:
    dispatcher = RunnerEventDispatcher()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    sent: list[str] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        source_id = frame.events[0]["data"]["source_id"]
        sent.append(source_id)
        if source_id == "first":
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    def item(source_id: str) -> dict[str, Any]:
        return {**_ITEM, "data": {**_ITEM["data"], "source_id": source_id}}

    dispatcher.ready(send)
    first = asyncio.create_task(dispatcher.submit("session-a", [item("first")]))
    tasks = [first]
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(cleaning.wait(), timeout=2)
        second = asyncio.create_task(dispatcher.submit("session-a", [item("second")]))
        tasks.append(second)
        await asyncio.sleep(0)
        ack = await asyncio.wait_for(dispatcher.submit("session-b", [item("other")]), timeout=2)
        assert ack.applied == 1
        assert sent == ["first", "other"]
        release_cleanup.set()
        assert (await asyncio.wait_for(second, timeout=2)).applied == 1
        assert sent == ["first", "other", "second"]
        assert not dispatcher.has_pending
    finally:
        release_cleanup.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        for worker in dispatcher._workers:
            worker.cancel()
        await asyncio.gather(*dispatcher._workers, return_exceptions=True)


async def test_cancelled_queue_admission_releases_session_for_later_delivery() -> None:
    dispatcher = RunnerEventDispatcher()
    release = asyncio.Event()
    started = asyncio.Event()
    sent: list[str] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame.session_id)
        if len(sent) == 2:
            started.set()
        await release.wait()
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    async def wait_for_backpressure() -> None:
        while not dispatcher._queue.full():
            await asyncio.sleep(0)

    dispatcher.ready(send)
    tasks = [asyncio.create_task(dispatcher.submit(f"session-{i}", [_ITEM])) for i in range(35)]
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(wait_for_backpressure(), timeout=2)
        # Cancel one queued item and one producer still waiting for queue space.
        for index in (2, 34):
            tasks[index].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[index]
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        for index in (2, 34):
            session_id = f"session-{index}"
            assert session_id not in sent
            assert (await asyncio.wait_for(dispatcher.submit(session_id, [_ITEM]), 2)).applied == 1
        assert not dispatcher.has_pending
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
        for worker in dispatcher._workers:
            worker.cancel()
        await asyncio.gather(*dispatcher._workers, return_exceptions=True)


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


@pytest.mark.parametrize(
    ("ack_error", "expected_status"),
    [
        # The server's wrong-runner refusal: the session is bound elsewhere.
        pytest.param(ErrorCode.FORBIDDEN, 403, id="forbidden"),
        pytest.param(ErrorCode.INVALID_INPUT, 422, id="invalid-input"),
        pytest.param("invalid session event", 422, id="free-text-rejection"),
    ],
)
async def test_non_retryable_ack_maps_to_synthetic_http_status(
    ack_error: str, expected_status: int
) -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        # Round-trip the ack through the wire codec like a real tunnel frame.
        ack = decode_frame(encode_frame(EventAckFrame(frame.id, 0, ack_error)))
        assert isinstance(ack, EventAckFrame)
        dispatcher.acknowledge(ack)

    dispatcher.ready(send)
    async with _client(dispatcher, http_posts) as client:
        response = await asyncio.wait_for(client.post(_URL, json=_ITEM), timeout=2)
    assert response.status_code == expected_status
    assert response.json() == {"detail": ack_error}
    assert http_posts == []
    assert not dispatcher.has_pending


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


async def test_acknowledged_batch_stamps_last_dispatch_at() -> None:
    dispatcher = RunnerEventDispatcher()
    loop = asyncio.get_running_loop()
    assert dispatcher.last_dispatch_at is None

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    dispatcher.ready(send)
    async with _client(dispatcher, []) as client:
        before = loop.time()
        response = await client.post(_URL, json=_ITEM)
    assert response.status_code == 202
    dispatched = dispatcher.last_dispatch_at
    assert dispatched is not None and before <= dispatched <= loop.time()


async def test_unacknowledged_batch_does_not_stamp_last_dispatch_at() -> None:
    dispatcher = RunnerEventDispatcher()

    async def send(_text: str) -> None:
        # The tunnel drops before any ACK: nothing reached the server.
        dispatcher.disconnected()

    dispatcher.ready(send)
    preview = {"type": "external_output_text_delta", "data": {"delta": "hi"}}
    async with _client(dispatcher, []) as client:
        response = await asyncio.wait_for(client.post(_URL, json=preview), timeout=3)
    assert response.status_code == 503
    assert dispatcher.last_dispatch_at is None


async def test_disconnect_during_failed_send_leaves_no_unretrieved_future_error() -> None:
    dispatcher = RunnerEventDispatcher()
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))

    async def send(_text: str) -> None:
        # The tunnel drops mid-send: the pending future fails, then the send itself fails.
        dispatcher.disconnected()
        raise OSError("socket closed")

    dispatcher.ready(send)
    delivery = asyncio.create_task(dispatcher._deliver("session-a", [_ITEM]))
    for _ in range(20):
        await asyncio.sleep(0.01)
        if delivery.done() or dispatcher._state == "disconnected":
            break
    delivery.cancel()
    await asyncio.gather(delivery, return_exceptions=True)
    del delivery  # The cancelled task's traceback would otherwise keep the future alive.
    gc.collect()
    await asyncio.sleep(0)
    assert dispatcher._pending == {}
    assert not reported
