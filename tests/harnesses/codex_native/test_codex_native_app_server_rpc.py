"""RPC lifecycle regressions for the native Codex app-server client."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    list_codex_model_options,
)


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


_MODEL_PAGES = (
    [{"id": "gpt-5.5-account", "isDefault": True}, {"id": "gpt-5.2-account"}],
    [{"id": "gpt-6-astra-account"}, {"id": "gpt-5.6-sol-account"}],
)


@contextlib.asynccontextmanager
async def _connected_model_list_client(
    stall_on_page: int,
) -> AsyncIterator[CodexAppServerClient]:
    """Connect to an app-server serving ``model/list`` in two cursor pages;
    ``stall_on_page`` (1 or 2) withholds that page's reply with the socket open.
    """

    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            method = message.get("method")
            if method == "initialize":
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))
            elif method == "model/list":
                page = 1 if message["params"].get("cursor") is None else 2
                if page == stall_on_page:
                    continue
                result = {
                    "data": _MODEL_PAGES[page - 1],
                    "nextCursor": "page2" if page == 1 else None,
                }
                await websocket.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        try:
            yield client
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), timeout=5)


async def test_paginated_model_list_drains_every_page() -> None:
    """Pagination over a real transport returns the rows of every page in order."""
    async with _connected_model_list_client(stall_on_page=0) as client:
        rows = await asyncio.wait_for(list_codex_model_options(client), timeout=10)
    assert [row["id"] for row in rows] == [row["id"] for page in _MODEL_PAGES for row in page]


@pytest.mark.parametrize("stall_on_page", [1, 2], ids=["first-request", "later-page"])
async def test_unbounded_model_list_waits_out_a_stalled_app_server(stall_on_page: int) -> None:
    """Without an opt-in budget a stalled ``model/list`` keeps the caller waiting."""
    async with _connected_model_list_client(stall_on_page) as client:
        listing = asyncio.create_task(list_codex_model_options(client))
        try:
            done, _ = await asyncio.wait({listing}, timeout=1)
            assert not done, "bare model/list returned although the app-server never replied"
        finally:
            listing.cancel()
            await asyncio.gather(listing, return_exceptions=True)


@pytest.mark.parametrize("stall_on_page", [1, 2], ids=["first-request", "later-page"])
async def test_model_list_budget_covers_every_page_and_discards_partial_rows(
    stall_on_page: int,
) -> None:
    """The end-to-end budget fires whichever page stalls and returns no partial rows."""
    async with _connected_model_list_client(stall_on_page) as client:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(list_codex_model_options(client, timeout_s=0.5), timeout=10)
        elapsed = time.monotonic() - started
        assert elapsed < 5, f"model/list deadline did not fire; raised after {elapsed:.1f}s"
        assert client._pending_requests == {}
