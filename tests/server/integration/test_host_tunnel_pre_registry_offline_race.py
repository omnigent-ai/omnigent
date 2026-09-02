"""Race test: pre-registry offline cleanup must be generation-safe.

When a host connection persists its row (``upsert_on_connect``) but fails
before ``HostRegistry.register()``, the tunnel handler marks the row
offline so no ghost-online host is left behind. That cleanup runs without
a generation guard: the registered-connection paths gate the offline write
on ``HostRegistry.deregister(host_id, conn=conn)``, but the pre-registry
path has no equivalent token. If the host reconnects while the failed
handler is still sending its error frame, the stale cleanup overwrites the
new connection's valid online row — the reconnected host then reads as
offline everywhere the durable row is trusted (the host picker, the
``host_is_live`` launch gate) even though its tunnel is live and its ping
loop keeps heartbeating (heartbeats never restore ``status``).

The test drives the A-fails / B-connects / A-cleans-up ordering
deterministically: it holds the failed handler A between its error frame
and its offline cleanup, completes a full reconnect B inside that window,
then lets A finish — and asserts B's row is still online while its tunnel
is registered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket

import omnigent.server.routes.host_tunnel as tunnel_mod
from omnigent.host.frames import HostConnectionErrorFrame, decode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.stores.host_store import HostStore, host_is_live
from tests.server.integration.test_host_tunnel_route import (
    _connect_route,
    _make_hello,
    _wait_registered,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "1444b179a19322377dcc75cf7fcd1bd2"
_TUNNEL_PATH = f"/v1/hosts/{_HOST_ID}/tunnel"


async def test_stale_pre_registry_cleanup_cannot_offline_newer_connection(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded connection's pre-registry cleanup must not clobber a reconnect.

    Ordering (each step waits for the previous to settle, so the race is
    deterministic rather than scheduler-dependent):

    1. Connection A sends hello. Its ``HostRegistry.register`` raises after
       ``upsert_on_connect`` already persisted the row online.
    2. A is held inside its error path — after sending the connection-error
       frame, before its offline cleanup — while connection B completes a
       full reconnect and becomes the current registered online host.
    3. A's cleanup then runs. A is superseded, so the host row must stay
       online: flipping it offline strands a live, registered host as
       "offline" for every reader of the durable row until the tunnel
       bounces.
    """
    registry = HostRegistry()
    store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, store), prefix="/v1")

    # First register call (connection A) fails after the upsert persisted the
    # row; later calls (connection B's reconnect) register normally.
    real_register = registry.register
    register_calls = {"count": 0}

    def _register_first_fails(*args: Any, **kwargs: Any) -> HostConnection:
        register_calls["count"] += 1
        if register_calls["count"] == 1:
            raise RuntimeError("registry unavailable (injected)")
        return real_register(*args, **kwargs)

    monkeypatch.setattr(registry, "register", _register_first_fails)

    # Hold the failed handler between its error frame and its cleanup so the
    # reconnect deterministically lands inside the race window. The real
    # error frame is still sent first, preserving the handler's semantics.
    reconnect_done = asyncio.Event()
    real_send_error = tunnel_mod._send_connection_error

    async def _send_error_then_hold(
        ws: WebSocket,
        *,
        stage: str,
        error: str,
        retryable: bool = False,
    ) -> None:
        await real_send_error(ws, stage=stage, error=error, retryable=retryable)
        await reconnect_done.wait()

    monkeypatch.setattr(tunnel_mod, "_send_connection_error", _send_error_then_hold)

    comm_a = await _connect_route(app, _TUNNEL_PATH)
    try:
        await comm_a.send_input({"type": "websocket.receive", "text": _make_hello()})

        # A's error frame on the wire means it persisted the row, failed
        # registration, and is now parked just before its offline cleanup.
        sent = await comm_a.receive_output(timeout=2.0)
        assert sent["type"] == "websocket.send"
        error_frame = decode_host_frame(sent["text"])
        assert error_frame == HostConnectionErrorFrame(
            stage="registry",
            error="registry unavailable (injected)",
            retryable=True,
        )

        host = store.get_host(_HOST_ID)
        assert host is not None and host.status == "online", (
            "connection A should have persisted the host online before failing"
        )

        # Connection B reconnects inside A's window and becomes the current
        # registered online host.
        comm_b = await _connect_route(app, _TUNNEL_PATH)
        try:
            await comm_b.send_input({"type": "websocket.receive", "text": _make_hello()})
            await asyncio.wait_for(_wait_registered(registry, _HOST_ID), timeout=2.0)
            host = store.get_host(_HOST_ID)
            assert host is not None and host.status == "online", (
                "connection B's upsert should have the host online"
            )

            # Release A: its stale cleanup runs to completion (the handler
            # closes the socket and finishes only after any offline write).
            reconnect_done.set()
            closed = await comm_a.receive_output(timeout=2.0)
            assert closed["type"] == "websocket.close"
            await comm_a.wait(timeout=2.0)

            # B is still the live, registered connection...
            assert registry.get(_HOST_ID) is not None, (
                "the reconnected host must still be registered"
            )

            # ...so its durable row must still be online. A stale offline
            # write here makes every reader of the durable row (the host
            # picker, the host_is_live launch gate) treat a connected host
            # as offline until its tunnel bounces.
            host = store.get_host(_HOST_ID)
            assert host is not None
            assert host.status == "online", (
                "stale pre-registry cleanup from a superseded connection "
                "must not mark the newer registered connection offline"
            )
            assert host_is_live(host), "the reconnected host must still be live"
        finally:
            await comm_b.send_input({"type": "websocket.disconnect", "code": 1000})
    finally:
        # Unblock A's parked coroutine even when an assertion fails early.
        reconnect_done.set()
