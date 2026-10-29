"""RPC lifecycle regressions for the native Codex app-server client."""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient


@pytest.mark.parametrize(
    "user_agent,expected",
    [
        ("omnigent/0.154.0 (Linux)", True),
        ("omnigent/0.163.0 (Linux)", True),
        ("omnigent-probe/0.154.0 (Linux) xterm-256color (omnigent; 0.1)", True),
        ("omnigent-probe/0.163.0 (Linux) xterm-256color (omnigent; 0.1)", True),
        ("omnigent-probe/0.142.0 (Linux) xterm-256color (omnigent; 0.1)", False),
        ("omnigent/0.129.0 (Linux)", False),
        ("omnigent/0.142.0 (Linux)", False),
        ("omnigent/0.153.9 (Linux)", False),
        ("other-client/0.163.0 (Linux)", False),
        ("unknown", False),
        (None, False),
        (12, False),
    ],
)
async def test_delivery_identity_support_comes_from_the_connected_server(
    user_agent: object, expected: bool
) -> None:
    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            if message["method"] == "initialize":
                await websocket.send(
                    json.dumps({"id": message["id"], "result": {"userAgent": user_agent}})
                )

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        try:
            await client.connect()
            assert client.supports_user_message_ids is expected
        finally:
            await client.close()


@pytest.mark.parametrize("outcome", ["normal", "abrupt", "malformed", "reply_then_close"])
async def test_pending_rpc_finishes_when_reader_exits(outcome: str) -> None:
    """A disconnected reader cannot leave its caller awaiting a response forever."""

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
                with pytest.raises(ConnectionError, match="before responding"):
                    await request
            assert client._pending_requests == {}
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            with contextlib.suppress(ValueError):
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
            with pytest.raises(ConnectionError):
                await request
        assert client._pending_requests == {}
        assert not client._reader_task.done()
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()
