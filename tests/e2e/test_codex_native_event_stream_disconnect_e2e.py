"""Codex native app-server event-stream disconnect e2e tests.

User journey: a native Codex session listens for app-server events —
the forwarder's event loop and thread-startup discovery both iterate
``CodexAppServerClient.iter_events()`` — and the app-server connection
goes away: a clean close, an error close, an abrupt transport drop, or
a malformed frame that kills the client's reader. Pending RPCs already
fail promptly; the waiting event consumers must also wake promptly
(terminate or receive a disconnect error) instead of waiting forever
for events that can no longer arrive. Notifications buffered before
the disconnect must still be delivered, and consumers must likewise
wake when the client itself is closed.

The app-server side is a controlled loopback WebSocket server that
completes the real initialize handshake, so the real
``CodexAppServerClient`` event path runs end to end.

Usage::

    python -m pytest tests/e2e/test_codex_native_event_stream_disconnect_e2e.py -v
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
from omnigent.harnesses.codex_native.forwarder import wait_for_thread_started

#: Seconds within which a waiting event consumer must settle once its
#: connection is gone. Generous against CI jitter, tiny against the
#: reported indefinite hang.
_DISCONNECT_DEADLINE = 5.0

_Scenario = Callable[[ServerConnection], Awaitable[None]]


def _free_port() -> int:
    """Reserve an ephemeral loopback port for the fake app-server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def _connected_client(
    on_disconnect_probe: _Scenario,
) -> AsyncIterator[CodexAppServerClient]:
    """Yield a client connected to a scenario-driven loopback app-server.

    The loopback server answers the real initialize handshake and runs
    ``on_disconnect_probe`` (close, abort, garbage, notify-then-close)
    when the client sends a ``probe/disconnect`` notification, so each
    test controls exactly when and how the connection dies.

    :param on_disconnect_probe: Scenario action driving the disconnect.
    :returns: Async context manager yielding the connected client.
    """

    async def handler(ws: ServerConnection) -> None:
        with contextlib.suppress(ConnectionClosed, OSError):
            async for raw in ws:
                message = json.loads(raw)
                method = message.get("method")
                if method == "initialize":
                    await ws.send(json.dumps({"id": message["id"], "result": {}}))
                elif method == "probe/disconnect":
                    await on_disconnect_probe(ws)
                    return

    port = _free_port()
    async with serve(handler, "127.0.0.1", port):
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        try:
            yield client
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


async def _reader_gone(client: CodexAppServerClient) -> None:
    """Wait until the client's reader task has exited."""
    reader = client._reader_task
    assert reader is not None, "connected client must have a reader task"
    await asyncio.wait({reader}, timeout=_DISCONNECT_DEADLINE)
    assert reader.done(), "loopback disconnect never stopped the reader"


@pytest.mark.parametrize(
    "disconnect",
    ["clean_close", "error_close", "transport_abort", "malformed_frame"],
)
async def test_waiting_event_consumer_settles_when_reader_exits(disconnect: str) -> None:
    """A waiting event consumer must wake once its reader is gone.

    Covers the reported close codes 1000 and 1011 plus the reader-failure
    variants: an abrupt transport drop, and a malformed frame that kills
    the reader loop while the socket stays open. In every case the
    consumer blocked in ``iter_events()`` must settle within the deadline
    — terminate or get a disconnect error — instead of waiting forever
    for events that can no longer arrive.
    """

    async def act(ws: ServerConnection) -> None:
        if disconnect == "clean_close":
            await ws.close(code=1000, reason="local disconnect probe")
        elif disconnect == "error_close":
            await ws.close(code=1011, reason="local disconnect probe")
        elif disconnect == "transport_abort":
            ws.transport.abort()
        else:
            await ws.send("this is not json")

    async with _connected_client(act) as client:
        events = client.iter_events()
        consumer = asyncio.ensure_future(anext(events))
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            await _reader_gone(client)
            done, _ = await asyncio.wait({consumer}, timeout=_DISCONNECT_DEADLINE)
            assert consumer in done, (
                f"event consumer still blocked {_DISCONNECT_DEADLINE}s after the "
                f"app-server {disconnect} — iter_events() hangs instead of ending"
            )
            assert consumer.exception() is not None, (
                "the consumer must terminate or get a disconnect error, "
                "not a fabricated notification"
            )
        finally:
            await _cancel_quietly(consumer)
            await events.aclose()


async def test_buffered_notification_delivered_then_stream_ends() -> None:
    """Notifications queued before the disconnect survive; then the stream ends.

    The app-server emits one notification and immediately closes. The
    consumer must still receive that buffered notification — the fix must
    not drop queued events — and its next wait must settle instead of
    blocking forever on the dead connection.
    """

    async def notify_then_close(ws: ServerConnection) -> None:
        await ws.send(json.dumps({"method": "thread/event", "params": {"seq": 1}}))
        await ws.close(code=1000, reason="local disconnect probe")

    async with _connected_client(notify_then_close) as client:
        events = client.iter_events()
        first = asyncio.ensure_future(anext(events))
        follow_up: asyncio.Task | None = None
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            done, _ = await asyncio.wait({first}, timeout=_DISCONNECT_DEADLINE)
            assert first in done, "the notification buffered before the close was never delivered"
            assert (await first).get("method") == "thread/event"
            await _reader_gone(client)
            follow_up = asyncio.ensure_future(anext(events))
            done, _ = await asyncio.wait({follow_up}, timeout=_DISCONNECT_DEADLINE)
            assert follow_up in done, (
                f"event consumer still blocked {_DISCONNECT_DEADLINE}s after draining "
                "the buffered notification — iter_events() hangs instead of ending"
            )
            assert follow_up.exception() is not None, (
                "the drained stream must terminate or raise, not fabricate a notification"
            )
        finally:
            await _cancel_quietly(first)
            if follow_up is not None:
                await _cancel_quietly(follow_up)
            await events.aclose()


async def test_wait_for_thread_started_settles_after_disconnect() -> None:
    """No-deadline thread discovery must fail once the event stream is dead.

    ``wait_for_thread_started(client, timeout=None)`` is the production
    no-deadline path host-spawned runner auto-create uses while a possible
    interactive sign-in is pending. After the app-server disconnects it
    must reach its documented event-stream-ended failure promptly, not
    wait forever for a ``thread/started`` that can no longer arrive.
    """

    async def close_cleanly(ws: ServerConnection) -> None:
        await ws.close(code=1000, reason="local disconnect probe")

    async with _connected_client(close_cleanly) as client:
        waiter = asyncio.ensure_future(wait_for_thread_started(client, timeout=None))
        try:
            await asyncio.sleep(0)
            await client.notify("probe/disconnect")
            await _reader_gone(client)
            done, _ = await asyncio.wait({waiter}, timeout=_DISCONNECT_DEADLINE)
            assert waiter in done, (
                f"wait_for_thread_started(timeout=None) still blocked "
                f"{_DISCONNECT_DEADLINE}s after the app-server disconnect"
            )
            assert waiter.exception() is not None, (
                "thread discovery on a dead stream must fail, not return a thread id"
            )
        finally:
            await _cancel_quietly(waiter)


async def test_waiting_event_consumer_settles_on_client_close() -> None:
    """Explicitly closing the client must wake a waiting event consumer.

    Session teardown calls ``client.close()`` while the forwarder may
    still be blocked in ``iter_events()``. The consumer must settle
    within the deadline; callers should not have to hunt down and cancel
    every consumer themselves.
    """

    async def never_disconnects(_ws: ServerConnection) -> None:
        return

    async with _connected_client(never_disconnects) as client:
        events = client.iter_events()
        consumer = asyncio.ensure_future(anext(events))
        try:
            await asyncio.sleep(0)
            await asyncio.wait_for(client.close(), timeout=10)
            done, _ = await asyncio.wait({consumer}, timeout=_DISCONNECT_DEADLINE)
            assert consumer in done, (
                f"event consumer still blocked {_DISCONNECT_DEADLINE}s after "
                "client.close() — teardown leaves consumers hanging"
            )
            assert consumer.exception() is not None, (
                "a closed client's consumer must terminate or raise, "
                "not fabricate a notification"
            )
        finally:
            await _cancel_quietly(consumer)
            await events.aclose()
