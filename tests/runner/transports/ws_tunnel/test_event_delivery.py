"""Native event batches retain their source cursor until a tunnel acknowledgement."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

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
