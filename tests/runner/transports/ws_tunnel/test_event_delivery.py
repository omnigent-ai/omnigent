"""Native event batches retain their source cursor until a tunnel acknowledgement."""

from __future__ import annotations

import asyncio
import gc
import json
from typing import Any

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


async def test_superseded_disconnect_keeps_live_tunnel_and_pending_ack() -> None:
    """The shared dispatcher ignores a superseded generation's disconnect.

    Make-before-break opens a replacement while the old socket drains. When the
    old socket finally closes, its disconnect must not null the live send or
    fail an ack in flight on the replacement generation.
    """
    dispatcher = RunnerEventDispatcher()
    sent: list[EventBatchFrame] = []
    release_ack = asyncio.Event()
    ack_tasks: list[asyncio.Task[None]] = []

    async def old_send(_text: str) -> None:
        raise AssertionError("the superseded tunnel must not send")

    async def live_send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        sent.append(frame)

        async def _ack_when_released() -> None:
            await release_ack.wait()
            dispatcher.acknowledge(EventAckFrame(frame.id, 1))

        ack_tasks.append(asyncio.create_task(_ack_when_released()))

    old_generation = dispatcher.connected(old_send)
    dispatcher.ready(old_send)
    # The replacement supersedes the old generation while it is still draining.
    dispatcher.connected(live_send)
    dispatcher.ready(live_send)

    submit = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert sent, "the live tunnel never received the batch"

    dispatcher.disconnected(old_generation)
    assert not submit.done(), "a superseded disconnect wrongly failed the live ack"

    release_ack.set()
    ack = await asyncio.wait_for(submit, timeout=1)
    await asyncio.gather(*ack_tasks)
    assert ack.applied == 1
    assert len(sent) == 1


async def test_current_generation_disconnect_tears_down() -> None:
    dispatcher = RunnerEventDispatcher()

    async def send(_text: str) -> None:
        raise AssertionError("disconnected tunnel must not send")

    generation = dispatcher.connected(send)
    dispatcher.ready(send)
    dispatcher.disconnected(generation)
    assert dispatcher._state == "disconnected"
    assert dispatcher._send is None


async def test_superseded_pending_retransmits_on_live_socket_without_timeout() -> None:
    """A superseded disconnect retransmits that generation's in-flight batch now.

    Make-before-break sends a batch on the old socket, then the replacement goes
    live. When the old socket finally closes, its unacknowledged batch must be
    retransmitted on the live socket at once, not after the 30s delivery timeout.
    """
    dispatcher = RunnerEventDispatcher()
    old_sent: list[EventBatchFrame] = []
    live_sent: list[EventBatchFrame] = []
    old_received = asyncio.Event()

    async def old_send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        old_sent.append(frame)
        old_received.set()
        # The old socket is superseded before it acknowledges, then it closes.

    async def live_send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        live_sent.append(frame)
        dispatcher.acknowledge(EventAckFrame(frame.id, 1))

    old_generation = dispatcher.connected(old_send)
    dispatcher.ready(old_send)

    submit = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
    await asyncio.wait_for(old_received.wait(), timeout=1)
    assert len(old_sent) == 1

    # The replacement supersedes the old generation; then the old socket closes.
    dispatcher.connected(live_send)
    dispatcher.ready(live_send)
    dispatcher.disconnected(old_generation)

    ack = await asyncio.wait_for(submit, timeout=2)
    assert ack.applied == 1
    assert len(old_sent) == 1, "the superseded socket must not be reused"
    assert len(live_sent) >= 1, "the batch was not retransmitted on the live socket"


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


async def test_stale_ready_from_superseded_generation_does_not_enable_delivery() -> None:
    """A superseded generation's late ``event.ready`` leaves the replacement negotiating.

    Make-before-break can deliver the old socket's ready frame after the
    replacement has already connected. It must not repoint delivery at the
    closing socket; batches wait for the replacement's own ready frame.
    """
    dispatcher = RunnerEventDispatcher()
    old_sent: list[EventBatchFrame] = []
    live_sent: list[EventBatchFrame] = []

    async def old_send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        old_sent.append(frame)

    async def live_send(text: str) -> None:
        frame = decode_frame(text)
        assert isinstance(frame, EventBatchFrame)
        live_sent.append(frame)
        dispatcher.acknowledge(EventAckFrame(frame.id, 1))

    old_generation = dispatcher.connected(old_send)
    live_generation = dispatcher.connected(live_send)
    dispatcher.ready(old_send, old_generation)

    submit = asyncio.create_task(dispatcher.submit("session-a", [_ITEM]))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not submit.done(), "a superseded generation's ready frame enabled delivery"
    assert old_sent == [] and live_sent == []

    dispatcher.ready(live_send, live_generation)
    ack = await asyncio.wait_for(submit, timeout=2)
    assert ack.applied == 1
    assert old_sent == [], "the superseded socket must not carry batches"
    assert len(live_sent) == 1
