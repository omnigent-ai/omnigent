"""Runner event ingestion across a real server tunnel and a lost ACK."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
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
