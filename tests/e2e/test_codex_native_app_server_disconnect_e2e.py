"""Codex native app-server RPC disconnect e2e tests.

User journey: a native Codex operation sends a JSON-RPC request to its
local app-server and the app-server disconnects before replying — a
clean close, an abrupt transport drop, or a malformed frame that kills
the client's reader. The awaiting operation must fail promptly instead
of hanging forever, must not resubmit the request, and abandoned
requests (cancelled, or whose send failed) must release their pending
bookkeeping without cancelling the shared reader. A response that
arrives just before the disconnect must still win.

The app-server side is a controlled loopback WebSocket server that
completes the real initialize handshake, so the real
``CodexAppServerClient`` request path runs end to end.

Usage::

    python -m pytest tests/e2e/test_codex_native_app_server_disconnect_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient

#: Seconds within which an RPC must settle once its connection is gone.
#: Generous against CI jitter, tiny against the reported indefinite hang.
_DISCONNECT_DEADLINE = 5.0

_TURN_PARAMS = {"input": "hello"}

_Responder = Callable[[ServerConnection, dict], Awaitable[None]]


def _free_port() -> int:
    """Reserve an ephemeral loopback port for the fake app-server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def _connected_client(
    respond: _Responder,
) -> AsyncIterator[tuple[CodexAppServerClient, list[dict]]]:
    """Yield a connected client and the post-handshake requests it sent.

    The loopback server answers the real initialize handshake, records
    every subsequent request envelope, and delegates each one to
    ``respond`` to drive the scenario (reply, close, abort, garbage).

    :param respond: Per-request scenario action, e.g. close the socket
        without replying.
    :returns: Async context manager yielding ``(client, received)``.
    """
    received: list[dict] = []

    async def handler(ws: ServerConnection) -> None:
        with contextlib.suppress(ConnectionClosed, OSError):
            async for raw in ws:
                message = json.loads(raw)
                method = message.get("method")
                if method == "initialize":
                    await ws.send(json.dumps({"id": message["id"], "result": {}}))
                elif method == "initialized":
                    continue
                else:
                    received.append(message)
                    await respond(ws, message)

    port = _free_port()
    async with serve(handler, "127.0.0.1", port):
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        try:
            yield client, received
        finally:
            # A crashed reader can re-raise its error from close(); the
            # scenario assertions, not teardown, decide these tests.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(), timeout=10)


async def _cancel_quietly(task: asyncio.Task) -> None:
    """Cancel ``task`` and absorb its cancellation/error."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(BaseException):
        await task


@pytest.mark.parametrize(
    "disconnect",
    ["clean_close", "transport_abort", "malformed_frame"],
)
async def test_pending_rpc_settles_when_reader_exits(disconnect: str) -> None:
    """An in-flight RPC must fail promptly once its reader is gone.

    Covers the three reported disconnect variants: the app-server closes
    cleanly without replying, drops the transport abruptly, or sends a
    malformed frame that kills the client's reader loop while the socket
    stays open. In every case the awaiting caller must get an error
    within the deadline — not wait indefinitely — release its pending
    slot, and never resubmit the request.
    """

    async def act(ws: ServerConnection, _message: dict) -> None:
        if disconnect == "clean_close":
            await ws.close()
        elif disconnect == "transport_abort":
            ws.transport.abort()
        else:
            await ws.send("this is not json")

    async with _connected_client(act) as (client, received):
        task = asyncio.ensure_future(client.request("turn/start", _TURN_PARAMS))
        try:
            done, _ = await asyncio.wait({task}, timeout=_DISCONNECT_DEADLINE)
            assert task in done, (
                f"turn/start still pending {_DISCONNECT_DEADLINE}s after the "
                f"app-server {disconnect} — the RPC hangs instead of failing"
            )
            assert task.exception() is not None, (
                "the RPC must surface a connection error, not fabricate a result"
            )
            assert client._pending_requests == {}, "the settled RPC must release its pending slot"
            assert [m.get("method") for m in received] == ["turn/start"], (
                "the RPC must not be resubmitted after the disconnect"
            )
        finally:
            await _cancel_quietly(task)


async def test_cancelled_rpc_releases_slot_and_spares_reader() -> None:
    """Cancelling an awaiting RPC frees its slot without killing the reader.

    The app-server holds the first request open; the caller gives up and
    cancels. The abandoned request must not stay in the pending map for
    the connection's lifetime, and the shared reader must keep serving
    later RPCs on the same connection.
    """

    async def respond(ws: ServerConnection, message: dict) -> None:
        if message.get("method") == "session/ping":
            await ws.send(json.dumps({"id": message["id"], "result": {"pong": True}}))

    async with _connected_client(respond) as (client, received):
        task = asyncio.ensure_future(client.request("turn/start", _TURN_PARAMS))
        deadline = asyncio.get_running_loop().time() + _DISCONNECT_DEADLINE
        while not received:
            assert asyncio.get_running_loop().time() < deadline, (
                "loopback app-server never saw turn/start"
            )
            await asyncio.sleep(0.01)
        await _cancel_quietly(task)
        assert client._pending_requests == {}, "a cancelled request must release its pending slot"
        reader = client._reader_task
        assert reader is not None and not reader.done(), (
            "cancelling one request must not cancel the shared reader"
        )
        follow_up = await asyncio.wait_for(
            client.request("session/ping", {}), _DISCONNECT_DEADLINE
        )
        assert follow_up.get("result") == {"pong": True}


async def test_failed_send_releases_pending_slot() -> None:
    """A request whose send fails must not retain pending bookkeeping.

    After the app-server replies once and closes, a further request's
    send fails on the dead connection. The error must reach the caller
    promptly and the request's pending slot must be released.
    """

    async def reply_then_close(ws: ServerConnection, message: dict) -> None:
        await ws.send(json.dumps({"id": message["id"], "result": {"ok": True}}))
        await ws.close()

    async with _connected_client(reply_then_close) as (client, _received):
        await asyncio.wait_for(client.request("turn/start", _TURN_PARAMS), _DISCONNECT_DEADLINE)
        reader = client._reader_task
        assert reader is not None
        await asyncio.wait({reader}, timeout=_DISCONNECT_DEADLINE)
        assert reader.done(), "reader should exit once the app-server closes"
        with pytest.raises(Exception) as excinfo:
            await asyncio.wait_for(client.request("turn/interrupt", {}), _DISCONNECT_DEADLINE)
        assert not isinstance(excinfo.value, TimeoutError), (
            "the failed send must raise promptly, not hang"
        )
        assert client._pending_requests == {}, (
            "a request whose send failed must release its pending slot"
        )


async def test_response_delivered_before_disconnect_still_wins() -> None:
    """A response racing a disconnect must be delivered, not discarded.

    The app-server replies to the request and immediately closes. The
    caller must receive the real response — this control already passes
    and guards the fix against over-failing settled requests.
    """

    async def reply_then_close(ws: ServerConnection, message: dict) -> None:
        await ws.send(json.dumps({"id": message["id"], "result": {"done": True}}))
        await ws.close()

    async with _connected_client(reply_then_close) as (client, _received):
        response = await asyncio.wait_for(
            client.request("turn/start", _TURN_PARAMS), _DISCONNECT_DEADLINE
        )
        assert response.get("result") == {"done": True}
        assert client._pending_requests == {}
