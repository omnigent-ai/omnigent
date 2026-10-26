"""Client tests for Codex app server."""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import cast
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from omnigent.harnesses.codex_native.diagnostics import collect_codex_rpc_failure_diagnostics


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (-32600, "no active turn to steer", True),
        (-32600, "no active turn to interrupt", True),
        (-32600, " NO ACTIVE TURN TO INTERRUPT ", True),
        (-32600, "expected active turn id `turn_a` but found `turn_b`", True),
        (-32600, "expected active turn id turn_a but found turn_b", True),
        (-32600, "thread not found", False),
        (-32600, "invalid turn id", False),
        (-32600, "expected active turn id", False),
        (-32600, "cannot steer a review turn", False),
        (-32603, "no active turn to interrupt", False),
        (-32603, "expected active turn id turn_a but found turn_b", False),
        (-32600, None, False),
    ],
)
def test_is_stale_active_turn_error(code: int, message: str | None, expected: bool) -> None:
    error = CodexAppServerResponseError({"code": code, "message": message})
    assert app_server.is_stale_active_turn_error(error) is expected


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (-32600, "no active turn to steer", True),
        (-32600, "no active turn to interrupt", True),
        (-32600, " NO ACTIVE TURN TO INTERRUPT ", True),
        (-32600, "expected active turn id turn_a but found turn_b", False),
        (-32600, "expected active turn id `turn_a` but found `turn_b`", False),
        (-32600, "thread not found", False),
        (-32603, "no active turn to interrupt", False),
        (-32600, None, False),
    ],
)
def test_is_no_active_turn_error(code: int, message: str | None, expected: bool) -> None:
    """Only a genuinely-ended turn qualifies; a superseded turn does not."""
    error = CodexAppServerResponseError({"code": code, "message": message})
    assert app_server.is_no_active_turn_error(error) is expected


@pytest.mark.parametrize("method", ["turn/start", "turn/steer"])
async def test_rejected_request_traceback_identifies_rpc(method: str) -> None:
    """RPC errors keep their structured payload and add only request identity."""
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)
    error = {"code": -32600, "message": "invalid turn id"}

    async def reject_request(raw: str) -> None:
        envelope = json.loads(raw)
        request_id = envelope["id"]
        client._pending_requests.pop(request_id).set_result({"id": request_id, "error": error})

    websocket.send.side_effect = reject_request
    params = {"input": [{"text": "private prompt"}]}
    with pytest.raises(CodexAppServerResponseError) as caught:
        await client.request(method, params)

    exc = caught.value
    assert exc.error is error
    assert exc.code == -32600
    assert exc.message == "invalid turn id"
    assert str(exc) == str(error)
    assert exc.__notes__ == [f"Codex app-server RPC: method={method} request_id=1"]
    rendered = "".join(traceback.format_exception(exc))
    assert exc.__notes__[0] in rendered
    assert "private prompt" not in rendered


@pytest.mark.parametrize("failed_method", ["initialize", "turn/start"])
async def test_disconnected_request_preserves_receiver_cause_and_phase(failed_method: str) -> None:
    async def peer(socket: ServerConnection) -> None:
        async for raw in socket:
            message = json.loads(raw)
            if message["method"] == failed_method:
                await socket.close(code=1011, reason="private upstream detail")
                return
            if "id" in message:
                await socket.send(json.dumps({"id": message["id"], "result": {}}))

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        try:
            with pytest.raises(ConnectionError) as caught:
                async with asyncio.timeout(5):
                    await client.connect()
                    await client.request("turn/start", {"input": "private input"})
            assert isinstance(caught.value.__cause__, ConnectionClosedError)
            assert caught.value.__notes__ == [
                f"Codex app-server RPC: method={failed_method} "
                f"request_id={1 if failed_method == 'initialize' else 2}"
            ]
            snapshot = collect_codex_rpc_failure_diagnostics(client, caught.value)
            assert snapshot["rpc_connection_phase"] == (
                "initialize" if failed_method == "initialize" else "ready"
            )
            assert snapshot["rpc_reader_state"] == "failed"
            assert snapshot["rpc_reader_error_type"] == "ConnectionClosedError"
            assert snapshot["rpc_close_received_code"] == 1011
            assert snapshot["rpc_error_type_1"] == "ConnectionClosedError"
            assert "private" not in str(snapshot)
            assert client._pending_requests == {}
        finally:
            await client.close()


@pytest.mark.parametrize(
    "reader_error", [ConnectionClosedError(None, None), ConnectionClosedOK(None, None)]
)
async def test_client_close_cleans_up_after_reader_disconnect(reader_error: Exception) -> None:
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)

    async def fail_reader() -> None:
        raise reader_error

    reader = asyncio.create_task(fail_reader())
    client._reader_task = reader
    pending = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = pending
    await asyncio.sleep(0)
    assert reader.done()

    await client.close()

    assert pending.cancelled()
    assert client._pending_requests == {}
    assert client._reader_task is None
    assert client._ws is None
    websocket.close.assert_awaited_once()
    await client.close()
    websocket.close.assert_awaited_once()


async def test_client_close_preserves_reader_bug_after_cleanup() -> None:
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)

    async def fail_reader() -> None:
        raise ValueError("invalid event payload")

    reader = asyncio.create_task(fail_reader())
    client._reader_task = reader
    pending = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = pending
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="invalid event payload"):
        await client.close()

    assert pending.cancelled()
    assert client._pending_requests == {}
    assert client._reader_task is None
    assert client._ws is None
    websocket.close.assert_awaited_once()


async def test_client_close_clears_state_when_websocket_close_fails() -> None:
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    websocket.close.side_effect = ValueError("close failed")
    client._ws = cast(ClientConnection, websocket)
    reader = asyncio.create_task(asyncio.sleep(60))
    client._reader_task = reader
    pending = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = pending

    with pytest.raises(ValueError, match="close failed"):
        await client.close()

    assert reader.cancelled()
    assert pending.cancelled()
    assert client._pending_requests == {}
    assert client._reader_task is None
    assert client._ws is None
