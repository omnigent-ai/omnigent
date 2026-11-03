"""RPC lifecycle regressions for the native Codex app-server client."""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient


@pytest.mark.parametrize(
    ("outcome", "cause_type", "close_code"),
    [
        ("normal", type(None), "1000"),
        ("abrupt", ConnectionClosedError, "1006"),
        ("malformed", json.JSONDecodeError, None),
        ("reply_then_close", None, None),
    ],
)
async def test_pending_rpc_finishes_when_reader_exits(
    outcome: str, cause_type: type[BaseException] | None, close_code: str | None
) -> None:
    """A disconnected reader fails its caller with the reader's cause and close code."""

    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            if message["method"] == "initialize":
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))
            elif message["method"] == "turn/start":
                if outcome == "reply_then_close":
                    await websocket.send(
                        json.dumps({"id": message["id"], "result": {"accepted": True}})
                    )
                elif outcome == "malformed":
                    await websocket.send("invalid json")
                if outcome == "abrupt":
                    websocket.transport.abort()
                else:
                    await websocket.close()
                return

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        request = asyncio.create_task(client.request("turn/start", {"input": []}))
        try:
            done, _ = await asyncio.wait({request}, timeout=2)
            assert request in done, "RPC remained pending after app-server disconnect"
            if outcome == "reply_then_close":
                assert (await request)["result"] == {"accepted": True}
            else:
                with pytest.raises(ConnectionError, match="before responding") as raised:
                    await request
                assert isinstance(raised.value.__cause__, cause_type)
                note = "\n".join(raised.value.__notes__)
                assert "method=turn/start request_id=2 phase=response" in note
                if close_code is not None:
                    assert f"close_code={close_code}" in note
            assert client._pending_requests == {}
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            with contextlib.suppress(ValueError):
                await client.close()


async def test_initialize_disconnect_keeps_transport_cause() -> None:
    """A peer that drops the connection during initialize surfaces that close as the cause."""

    async def handle(websocket: ServerConnection) -> None:
        await websocket.recv()
        websocket.transport.abort()

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        try:
            with pytest.raises(ConnectionError, match="responding to initialize") as raised:
                await asyncio.wait_for(client.connect(), 2)
            assert isinstance(raised.value.__cause__, ConnectionClosedError)
            assert raised.value.__notes__ == [
                "Codex app-server RPC: method=initialize request_id=1"
                " phase=response close_code=1006"
            ]
        finally:
            await client.close()


@pytest.mark.parametrize("outcome", ["cancelled", "send_failure"])
async def test_abandoned_rpc_releases_pending_request(outcome: str) -> None:
    """Failed sends and cancelled callers release their pending response slot."""
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    sent = asyncio.Event()

    async def send(_raw: str) -> None:
        sent.set()
        if outcome == "send_failure":
            raise ConnectionError("connection lost during send")

    websocket.send.side_effect = send
    client._ws = websocket
    client._reader_task = asyncio.create_task(asyncio.Event().wait())
    request = asyncio.create_task(client.request("turn/start", {}))
    try:
        await asyncio.wait_for(sent.wait(), 2)
        if outcome == "cancelled":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            with pytest.raises(ConnectionError) as raised:
                await request
            assert raised.value.__notes__ == [
                "Codex app-server RPC: method=turn/start request_id=1 phase=send"
            ]
        assert client._pending_requests == {}
        assert not client._reader_task.done()
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()
