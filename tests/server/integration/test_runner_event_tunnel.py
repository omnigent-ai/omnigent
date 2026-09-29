"""Runner event ingestion across a real server tunnel and a lost ACK."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.runner.transports.ws_tunnel.event_delivery import (
    RunnerEventDispatcher,
    TunnelEventClient,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    EVENT_INGEST_CAPABILITY,
    EventAckFrame,
    EventReadyFrame,
    HelloFrame,
    decode_frame,
    encode_frame,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.budgets import budget
from tests.server.helpers import create_test_agent

_RUNNER_ID = "runner-event-ingress"
_ITEM = {
    "type": "external_conversation_item",
    "data": {
        "source_id": "native-record-1",
        "item_type": "message",
        "response_id": "response-1",
        "item_data": {
            "role": "assistant",
            "agent": "claude-native-ui",
            "content": [{"type": "output_text", "text": "recovered after restart"}],
        },
    },
}


async def _connect(app: FastAPI) -> ApplicationCommunicator:
    path = f"/v1/runners/{_RUNNER_ID}/tunnel"
    comm = ApplicationCommunicator(
        app,
        {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
        },
    )
    await comm.send_input({"type": "websocket.connect"})
    assert (await comm.receive_output(timeout=budget(2.0)))["type"] == "websocket.accept"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_frame(
                HelloFrame(
                    runner_version="test",
                    frame_protocol_version=1,
                    capabilities=[EVENT_INGEST_CAPABILITY],
                )
            ),
        }
    )
    ready = await comm.receive_output(timeout=budget(2.0))
    assert isinstance(decode_frame(ready["text"]), EventReadyFrame)
    return comm


async def _read_ack(comm: ApplicationCommunicator) -> EventAckFrame:
    # Runner recovery RPCs share this socket with event acknowledgements.
    for _ in range(10):
        frame = decode_frame((await comm.receive_output(timeout=budget(3.0)))["text"])
        if isinstance(frame, EventAckFrame):
            return frame
    raise AssertionError("server never acknowledged the event batch")


async def _close(comm: ApplicationCommunicator) -> None:
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    with contextlib.suppress(asyncio.TimeoutError):
        await comm.wait(timeout=budget(2.0))


async def test_lost_ack_replays_without_duplicate_item(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert SqlAlchemyConversationStore(db_uri).set_runner_id(session_id, _RUNNER_ID)

    dispatcher = RunnerEventDispatcher()
    first = await _connect(app)
    first_open = True
    second: ApplicationCommunicator | None = None
    posting: asyncio.Task[httpx.Response] | None = None
    try:

        async def first_send(text: str) -> None:
            await first.send_input({"type": "websocket.receive", "text": text})

        dispatcher.ready(first_send)
        async with TunnelEventClient(
            base_url="http://server",
            event_dispatcher=dispatcher,
            transport=httpx.MockTransport(lambda _: httpx.Response(599)),
        ) as forwarder:
            posting = asyncio.create_task(
                forwarder.post(f"/v1/sessions/{session_id}/events", json=_ITEM)
            )
            ack = await _read_ack(first)
            assert ack.applied == 1
            # Server committed the item; the connection dropped before the
            # runner received the acknowledgement.
            dispatcher.disconnected()
            await _close(first)
            first_open = False

            second = await _connect(app)

            async def second_send(text: str) -> None:
                assert second is not None
                await second.send_input({"type": "websocket.receive", "text": text})

            dispatcher.ready(second_send)
            replay_ack = await _read_ack(second)
            assert replay_ack.applied == 1
            dispatcher.acknowledge(replay_ack)
            response = await asyncio.wait_for(posting, timeout=budget(3.0))
            assert response.status_code == 202
    finally:
        dispatcher.disconnected()
        if posting is not None:
            if not posting.done():
                posting.cancel()
            await asyncio.gather(posting, return_exceptions=True)
        try:
            if second is not None:
                await _close(second)
        finally:
            if first_open:
                await _close(first)

    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert [item["content"][0]["text"] for item in items] == ["recovered after restart"]


@pytest.mark.timeout(180)
async def test_numbered_items_survive_tunnel_disconnect_stages(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Queue before ready, disconnect mid-run, and lose an applied ACK under load."""
    agent = await create_test_agent(client)
    session_ids: list[str] = []
    store = SqlAlchemyConversationStore(db_uri)
    for _ in range(4):
        created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert created.status_code == 201
        session_id = created.json()["id"]
        assert store.set_runner_id(session_id, _RUNNER_ID)
        session_ids.append(session_id)

    dispatcher = RunnerEventDispatcher()
    received_acks = 0

    async def produce(part: int) -> None:
        session_id = session_ids[part]
        for number in range(part * 250 + 1, (part + 1) * 250 + 1):
            event = {
                "type": "external_conversation_item",
                "data": {
                    "source_id": f"number-{number}",
                    "item_type": "message",
                    "response_id": f"response-{number}",
                    "item_data": {
                        "role": "assistant",
                        "agent": "claude-native-ui",
                        "content": [{"type": "output_text", "text": str(number)}],
                    },
                },
            }
            ack = await dispatcher.submit(session_id, [event])
            assert ack.applied == 1, f"number {number} was not accepted: {ack!r}"
            await asyncio.sleep(0.005)

    producers = [asyncio.create_task(produce(part)) for part in range(4)]
    await asyncio.sleep(0)
    assert dispatcher.has_pending  # Started before the first tunnel was ready.
    current: ApplicationCommunicator | None = None
    pump_task: asyncio.Task[None] | None = None

    async def connect_and_pump(stop_after: int | None = None, lose_ack: bool = False) -> None:
        nonlocal current, pump_task, received_acks
        current = await _connect(app)
        comm = current

        async def send(text: str) -> None:
            await comm.send_input({"type": "websocket.receive", "text": text})

        dispatcher.ready(send)

        async def pump() -> None:
            nonlocal received_acks
            while True:
                message = await comm.receive_output(timeout=budget(30.0))
                if message.get("type") != "websocket.send":
                    continue
                frame = decode_frame(message["text"])
                if not isinstance(frame, EventAckFrame):
                    continue  # Runner recovery RPCs also share the tunnel.
                if frame.applied != 1:
                    dispatcher.acknowledge(frame)
                    continue  # Busy responses do not advance the source.
                received_acks += 1
                if received_acks == stop_after:
                    if not lose_ack:
                        dispatcher.acknowledge(frame)
                    dispatcher.disconnected()
                    return
                dispatcher.acknowledge(frame)

        pump_task = asyncio.create_task(pump())
        if stop_after is not None:
            await asyncio.wait_for(pump_task, timeout=budget(120.0))
            await _close(comm)
            current = None
            pump_task = None

    try:
        await connect_and_pump(stop_after=400)
        await connect_and_pump(stop_after=700, lose_ack=True)
        await connect_and_pump()
        await asyncio.wait_for(asyncio.gather(*producers), timeout=budget(120.0))
    finally:
        dispatcher.disconnected()
        for producer in producers:
            if not producer.done():
                producer.cancel()
        await asyncio.gather(*producers, return_exceptions=True)
        if pump_task is not None:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
        if current is not None:
            await _close(current)

    numbers: list[int] = []
    for session_id in session_ids:
        items = await client.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000})
        items.raise_for_status()
        numbers.extend(int(item["content"][0]["text"]) for item in items.json()["data"])
    assert len(numbers) == 1000
    assert sorted(numbers) == list(range(1, 1001))
