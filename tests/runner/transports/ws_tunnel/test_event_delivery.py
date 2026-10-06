"""Native event batches retain their source cursor until a tunnel acknowledgement."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
from typing import Any

import httpx
import pytest

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


async def test_lost_ack_replays_after_tunnel_reconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher = RunnerEventDispatcher()
    http_posts: list[object] = []
    frames: list[EventBatchFrame] = []
    caplog.set_level(logging.INFO, logger="omnigent.runner.transports.ws_tunnel.event_delivery")

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
    retry_records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "runner_event_delivery_retry"
    ]
    assert retry_records
    assert retry_records[0].attributes == {
        "reason": "transport_error",
        "retry_count": 1,
        "elapsed_retry_s": 0.0,
        "batch_size": 1,
        "remaining_count": 1,
    }
    recovered_records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "runner_event_delivery_recovered"
    ]
    assert len(recovered_records) == 1
    assert recovered_records[0].session_id == "session-a"
    assert recovered_records[0].attributes["retry_count"] == 1


@pytest.mark.asyncio
async def test_retrying_session_does_not_starve_other_sessions() -> None:
    """A blocked session cannot occupy both delivery workers."""
    dispatcher = RunnerEventDispatcher()
    sent: list[EventBatchFrame] = []
    session_a_started = asyncio.Event()
    release_session_a = asyncio.Event()

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame)
        if frame.session_id == "session-a" and not release_session_a.is_set():
            session_a_started.set()
            await release_session_a.wait()
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    dispatcher.ready(send)
    item_a1 = {**_ITEM, "data": {**_ITEM["data"], "source_id": "a-1"}}
    item_a2 = {**_ITEM, "data": {**_ITEM["data"], "source_id": "a-2"}}
    item_b = {**_ITEM, "data": {**_ITEM["data"], "source_id": "b-1"}}
    first = asyncio.create_task(dispatcher.submit("session-a", [item_a1]))
    await session_a_started.wait()
    second = asyncio.create_task(dispatcher.submit("session-a", [item_a2]))
    await asyncio.sleep(0)
    other = asyncio.create_task(dispatcher.submit("session-b", [item_b]))

    try:
        await asyncio.wait_for(other, timeout=1.0)
        assert [frame.session_id for frame in sent] == ["session-a", "session-b"]
        assert not release_session_a.is_set()

        release_session_a.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
        assert [frame.session_id for frame in sent] == ["session-a", "session-b", "session-a"]
    finally:
        release_session_a.set()
        await asyncio.wait_for(asyncio.gather(first, second, other, return_exceptions=True), 1.0)
        for worker in dispatcher._workers:
            worker.cancel()
        await asyncio.gather(*dispatcher._workers, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_session_admission_does_not_leave_lock_or_block_next_item() -> None:
    """Cancelling a waiter releases admission and lets the next item through."""
    dispatcher = RunnerEventDispatcher()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    sent: list[EventBatchFrame] = []

    async def send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame)
        if len(sent) == 1:
            first_started.set()
            await release_first.wait()
        dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))

    dispatcher.ready(send)
    first = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
    await first_started.wait()
    waiting = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    release_first.set()
    await asyncio.wait_for(first, timeout=1.0)
    fresh = {**_ITEM, "data": {**_ITEM["data"], "source_id": "fresh"}}
    await asyncio.wait_for(dispatcher.submit("session-a", [fresh]), timeout=1.0)
    assert [frame.events[0]["data"]["source_id"] for frame in sent] == [
        "record-1",
        "fresh",
    ]

    await asyncio.sleep(0)
    gc.collect()
    assert "session-a" not in dispatcher._locks


async def test_cancelled_delivery_keeps_session_order_until_cleanup_finishes() -> None:
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
        await asyncio.wait_for(started.wait(), timeout=1.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(cleaning.wait(), timeout=1.0)
        second = asyncio.create_task(dispatcher.submit("session-a", [item("second")]))
        tasks.append(second)
        await asyncio.wait_for(dispatcher.submit("session-b", [item("other")]), timeout=1.0)
        assert sent == ["first", "other"]
        release_cleanup.set()
        await asyncio.wait_for(second, timeout=1.0)
        assert sent == ["first", "other", "second"]
        assert not dispatcher.has_pending
    finally:
        release_cleanup.set()
        for task in [*tasks, *dispatcher._workers]:
            task.cancel()
        await asyncio.gather(*tasks, *dispatcher._workers, return_exceptions=True)


async def test_delivery_backpressure_and_cancelled_queue_admission() -> None:
    dispatcher = RunnerEventDispatcher()
    release = asyncio.Event()
    both_started = asyncio.Event()
    active = 0
    peak_active = 0
    sent: list[str] = []

    async def send(text: str) -> None:
        nonlocal active, peak_active
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        active += 1
        peak_active = max(peak_active, active)
        sent.append(frame.session_id)
        if active == 2:
            both_started.set()
        try:
            await release.wait()
            dispatcher.acknowledge(EventAckFrame(frame.id, len(frame.events)))
        finally:
            active -= 1

    dispatcher.ready(send)
    tasks = [asyncio.create_task(dispatcher.submit(f"session-{i}", [_ITEM])) for i in range(35)]
    try:
        await asyncio.wait_for(both_started.wait(), timeout=1.0)

        async def queue_full() -> None:
            while not dispatcher._queue.full():
                await asyncio.sleep(0)

        await asyncio.wait_for(queue_full(), timeout=1.0)
        assert dispatcher._queue.qsize() == 32
        assert peak_active == 2
        # One item is queued; the last producer is waiting for queue capacity.
        for index in (2, 34):
            tasks[index].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[index]
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
        for index in (2, 34):
            assert f"session-{index}" not in sent
            await asyncio.wait_for(dispatcher.submit(f"session-{index}", [_ITEM]), timeout=1.0)
        assert peak_active == 2
        assert not dispatcher.has_pending
        await asyncio.sleep(0)
        gc.collect()
        assert not dispatcher._locks
    finally:
        release.set()
        for task in [*tasks, *dispatcher._workers]:
            task.cancel()
        await asyncio.gather(*tasks, *dispatcher._workers, return_exceptions=True)


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
