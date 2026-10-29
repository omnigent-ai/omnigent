"""Client tests for Codex app server."""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import cast
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatus,
)

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)
from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
from omnigent.harnesses.codex_native.requester import CodexRequesterRegistry
from omnigent.harnesses.codex_native.terminal_proxy import (
    TOKEN_ENV,
    CodexTerminalProxy,
    RegisteredTerminalInput,
)
from omnigent.runtime.requester import InputRequester


@pytest.mark.parametrize("method", ["turn/start", "turn/steer"])
@pytest.mark.parametrize("outcome", ["accepted", "refused", "lost", "registration_failed"])
async def test_terminal_proxy_registers_before_delivery_and_preserves_display_ids(method, outcome):
    registered_id = "a" * 32
    refuse = AsyncMock()
    register = AsyncMock(return_value=RegisteredTerminalInput(registered_id, refuse))
    if outcome == "registration_failed":
        register.side_effect = RuntimeError("Harness unavailable")
    received = []

    async def handle(websocket):
        async for raw in websocket:
            message = json.loads(raw)
            received.append(message)
            register.assert_awaited_once()
            assert message["params"]["clientUserMessageId"] == registered_id
            if outcome == "lost":
                await websocket.close()
            elif outcome == "refused":
                await websocket.send(json.dumps({"id": message["id"], "error": {"code": -32600}}))
            else:
                await websocket.send(
                    json.dumps({"id": message["id"], "result": {"turnId": "turn"}})
                )
                await websocket.send(
                    json.dumps(
                        {
                            "method": "item/started",
                            "params": {
                                "item": {
                                    "type": "userMessage",
                                    "clientId": registered_id,
                                    "content": [],
                                }
                            },
                        }
                    )
                )

    async with serve(handle, "127.0.0.1", 0) as provider:
        proxy = CodexTerminalProxy(
            f"ws://127.0.0.1:{provider.sockets[0].getsockname()[1]}", register
        )
        url = await proxy.start()
        try:
            with pytest.raises(InvalidStatus):
                async with connect(url):
                    pytest.fail("Unauthenticated terminal connected")
            register.assert_not_awaited()
            async with connect(
                url, additional_headers={"Authorization": f"Bearer {proxy.environment[TOKEN_ENV]}"}
            ) as terminal:
                params = {"threadId": "thread", "clientUserMessageId": "display-id", "input": []}
                if method == "turn/steer":
                    params["expectedTurnId"] = "turn"
                await terminal.send(json.dumps({"id": 7, "method": method, "params": params}))
                if outcome == "lost":
                    with pytest.raises(ConnectionClosed):
                        await asyncio.wait_for(terminal.recv(), timeout=3)
                else:
                    response = json.loads(await asyncio.wait_for(terminal.recv(), timeout=3))
                    assert response["id"] == 7
                    if outcome == "accepted":
                        event = json.loads(await asyncio.wait_for(terminal.recv(), timeout=3))
                        assert event["params"]["item"]["clientId"] == "display-id"
                    else:
                        assert "error" in response
            register.assert_awaited_once_with("thread", "turn" if method == "turn/steer" else None)
            if outcome == "refused":
                refuse.assert_awaited_once_with()
                assert registered_id not in proxy._display_ids
            else:
                refuse.assert_not_awaited()
            assert len(received) == (0 if outcome == "registration_failed" else 1)
        finally:
            await proxy.close()


async def test_terminal_proxy_retention_keeps_fresh_authority_and_old_calls_restricted(
    monkeypatch,
):
    monkeypatch.setattr(CodexRequesterRegistry, "_MAX_RECORDS", 2)
    monkeypatch.setattr(CodexTerminalProxy, "_MAX_DISPLAY_IDS", 2)
    registry = CodexRequesterRegistry()
    owner = InputRequester(user_id="owner", account_generation="one")
    origin = owner
    turns = []

    async def register(thread_id, expected_turn_id):
        client_id = registry.begin(origin, thread_id, expected_turn_id)

        async def refuse():
            registry.finish(client_id, refused=True)

        return RegisteredTerminalInput(client_id, refuse)

    async def handle(websocket):
        async for raw in websocket:
            message = json.loads(raw)
            method = message.get("method")
            if method in ("turn/start", "turn/steer"):
                item = {
                    "type": "userMessage",
                    "clientId": message["params"]["clientUserMessageId"],
                }
                if method == "turn/start":
                    turns.append({"id": str(message["id"]), "items": []})
                turns[-1]["items"].append(item)
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))
                await websocket.send(
                    json.dumps({"method": "item/started", "params": {"item": item}})
                )
            elif method == "thread/read":
                await websocket.send(
                    json.dumps(
                        {
                            "id": message["id"],
                            "result": {
                                "thread": {"id": "thread", "sessionId": "session", "turns": turns}
                            },
                        }
                    )
                )
            elif "id" in message:
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))

    async with serve(handle, "127.0.0.1", 0) as provider:
        upstream = f"ws://127.0.0.1:{provider.sockets[0].getsockname()[1]}"
        proxy = CodexTerminalProxy(upstream, register)
        url = await proxy.start()
        client = CodexAppServerClient(ws_url=upstream)
        try:
            await client.connect()
            async with connect(
                url, additional_headers={"Authorization": f"Bearer {proxy.environment[TOKEN_ENV]}"}
            ) as terminal:
                for index in range(8):
                    origin = InputRequester(user_id="collaborator") if index == 1 else owner
                    method = "turn/steer" if index == 1 else "turn/start"
                    params = {"threadId": "thread", "clientUserMessageId": f"display-{index}"}
                    if index == 1:
                        params["expectedTurnId"] = "0"
                    await terminal.send(
                        json.dumps({"id": index, "method": method, "params": params})
                    )
                    response = json.loads(await asyncio.wait_for(terminal.recv(), timeout=3))
                    assert "error" not in response
                    event = json.loads(await asyncio.wait_for(terminal.recv(), timeout=3))
                    assert event["params"]["item"]["clientId"] == f"display-{index}"
                    history = await client.request(
                        "thread/read", {"threadId": "thread", "includeTurns": True}
                    )
                    identity = CodexExecutionIdentity(
                        "session", "thread", "0" if index == 1 else str(index)
                    )
                    requester = registry.snapshot(identity, history, revision=registry.revision)
                    assert requester is not None
                    assert requester.origin.user_id == (None if index == 1 else "owner")
                    assert len(registry._submissions) <= 2
                    assert len(registry._executions) <= 2
                    assert len(proxy._display_ids) <= 2

                # Replayed history may lose an old display alias, never acquire a fresh one.
                await terminal.send(
                    json.dumps({"id": "history", "method": "thread/read", "params": {}})
                )
                replay = json.loads(await asyncio.wait_for(terminal.recv(), timeout=3))
                replayed_turns = replay["result"]["thread"]["turns"]
                assert (
                    replayed_turns[0]["items"][0]["clientId"] == turns[0]["items"][0]["clientId"]
                )
                assert replayed_turns[-1]["items"][0]["clientId"] == "display-7"
                delayed = registry.snapshot(
                    CodexExecutionIdentity("session", "thread", "0"),
                    history,
                    revision=registry.revision,
                )
                assert delayed is None or delayed.origin.user_id is None
        finally:
            await client.close()
            await proxy.close()
        assert proxy._display_ids == {}


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


async def test_client_tracks_threads_it_started_or_resumed() -> None:
    """Only successful thread starts and resumes make the connection receive notifications."""
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)
    replies: list[dict[str, object]] = [
        {"error": {"code": -32600, "message": "thread not found"}},
        {"result": {"thread": {"id": "thread_read"}}},
        {"result": {"thread": {"id": "thread_started"}}},
        {"result": {}},
    ]

    async def reply(raw: str) -> None:
        request_id = json.loads(raw)["id"]
        client._pending_requests.pop(request_id).set_result({"id": request_id, **replies.pop(0)})

    websocket.send.side_effect = reply
    with pytest.raises(CodexAppServerResponseError):
        await client.request("thread/resume", {"threadId": "thread_missing"})
    await client.request("thread/read", {"threadId": "thread_read"})
    await client.request("thread/start", {})
    await client.request("thread/resume", {"threadId": "thread_resumed"})

    assert not client.is_subscribed_to_thread("thread_missing")
    assert not client.is_subscribed_to_thread("thread_read")
    assert client.is_subscribed_to_thread("thread_started")
    assert client.is_subscribed_to_thread("thread_resumed")


async def test_reconnect_forgets_threads_subscribed_on_the_previous_connection() -> None:
    """A new connection receives no thread notifications until it starts or resumes one."""

    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            if "id" in message:
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        try:
            await client.connect()
            await client.request("thread/resume", {"threadId": "thread_123"})
            assert client.is_subscribed_to_thread("thread_123")
            await client.close()
            await client.connect()
            assert not client.is_subscribed_to_thread("thread_123")
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
