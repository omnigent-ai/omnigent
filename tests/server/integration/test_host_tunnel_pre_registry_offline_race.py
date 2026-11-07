"""Host offline-cleanup races across host types and simulated replicas."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, WebSocket

import omnigent.server.routes.host_tunnel as tunnel_mod
from omnigent.db.utils import now_epoch
from omnigent.host.frames import HostConnectionErrorFrame, decode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.host_store import HostStore, host_is_live
from tests.budgets import budget
from tests.server.integration.test_host_tunnel_route import (
    _make_hello,
    _managed_scope,
    _wait_registered,
    _websocket_scope,
)

pytestmark = pytest.mark.asyncio

_HOST_ID = "1444b179a19322377dcc75cf7fcd1bd2"
_TUNNEL_PATH = f"/v1/hosts/{_HOST_ID}/tunnel"
_MANAGED_TOKEN = "managed-race-token"


async def _connect(target: FastAPI, *, managed: bool = False) -> ApplicationCommunicator:
    scope = (
        _managed_scope(_TUNNEL_PATH, _MANAGED_TOKEN) if managed else _websocket_scope(_TUNNEL_PATH)
    )
    communicator = ApplicationCommunicator(target, scope)
    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=budget(2.0))
    assert accepted["type"] == "websocket.accept"
    return communicator


@pytest.mark.parametrize("managed", [False, True], ids=["external", "managed"])
@pytest.mark.parametrize("separate_replica", [False, True], ids=["same-replica", "cross-replica"])
async def test_stale_pre_registry_cleanup_cannot_offline_newer_connection(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    managed: bool,
    separate_replica: bool,
) -> None:
    """A failed connection's cleanup must not offline a newer registered host."""
    registry = HostRegistry()
    store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(tunnel_mod.create_host_tunnel_router(registry, store), prefix="/v1")
    if managed:
        store.register_managed_host(
            host_id=_HOST_ID,
            name="managed-laptop",
            user_id="alice@example.com",
            token=_MANAGED_TOKEN,
            provider="modal",
            sandbox_id="race-sandbox",
            token_expires_at=now_epoch() + 3600,
        )
    successor_registry = HostRegistry() if separate_replica else registry
    successor_store = HostStore(db_uri) if separate_replica else store
    successor_app = FastAPI() if separate_replica else app
    if separate_replica:
        successor_app.include_router(
            tunnel_mod.create_host_tunnel_router(successor_registry, successor_store),
            prefix="/v1",
        )

    # Fail A after its durable upsert.
    real_register = registry.register
    register_calls = {"count": 0}

    def _register_first_fails(*args: Any, **kwargs: Any) -> HostConnection:
        register_calls["count"] += 1
        if register_calls["count"] == 1:
            raise RuntimeError("registry unavailable (injected)")
        return real_register(*args, **kwargs)

    monkeypatch.setattr(registry, "register", _register_first_fails)

    # Pause A after its error frame and before its conditional offline write.
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

    comm_a = await _connect(app, managed=managed)
    a_finished = False
    try:
        await comm_a.send_input({"type": "websocket.receive", "text": _make_hello()})

        # The error frame confirms A is parked after its upsert.
        sent = await comm_a.receive_output(timeout=budget(2.0))
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

        comm_b = await _connect(successor_app, managed=managed)
        try:
            await comm_b.send_input({"type": "websocket.receive", "text": _make_hello()})
            await asyncio.wait_for(
                _wait_registered(successor_registry, _HOST_ID), timeout=budget(2.0)
            )
            host = store.get_host(_HOST_ID)
            assert host is not None and host.status == "online", (
                "connection B's upsert should have the host online"
            )

            # Release A only after B is registered.
            reconnect_done.set()
            closed = await comm_a.receive_output(timeout=budget(2.0))
            assert closed["type"] == "websocket.close"
            await comm_a.wait(timeout=budget(2.0))
            a_finished = True

            assert successor_registry.get(_HOST_ID) is not None, (
                "the reconnected host must still be registered"
            )

            host = store.get_host(_HOST_ID)
            assert host is not None
            assert host.status == "online", (
                "stale pre-registry cleanup from a superseded connection "
                "must not mark the newer registered connection offline"
            )
            assert host_is_live(host), "the reconnected host must still be live"
        finally:
            await comm_b.send_input({"type": "websocket.disconnect", "code": 1000})
            await comm_b.wait(timeout=budget(2.0))
    finally:
        # Unblock A if an earlier assertion fails, then let its handler finish.
        reconnect_done.set()
        if not a_finished:
            await comm_a.send_input({"type": "websocket.disconnect", "code": 1000})
            with contextlib.suppress(asyncio.TimeoutError):
                await comm_a.wait(timeout=budget(2.0))


async def test_stale_registered_cleanup_cannot_offline_newer_connection_on_other_replica(
    db_uri: str,
) -> None:
    """A registered connection's disconnect must not offline a newer connection elsewhere."""
    first_registry, second_registry = HostRegistry(), HostRegistry()
    first_store, second_store = HostStore(db_uri), HostStore(db_uri)
    first_app, second_app = FastAPI(), FastAPI()
    first_app.include_router(
        tunnel_mod.create_host_tunnel_router(first_registry, first_store), prefix="/v1"
    )
    second_app.include_router(
        tunnel_mod.create_host_tunnel_router(second_registry, second_store), prefix="/v1"
    )

    comm_a = await _connect(first_app)
    a_finished = False
    try:
        await comm_a.send_input(
            {"type": "websocket.receive", "text": _make_hello(connection_id="conn-a")}
        )
        await asyncio.wait_for(_wait_registered(first_registry, _HOST_ID), timeout=budget(2.0))

        # The host reconnects through another replica while the first still holds A.
        comm_b = await _connect(second_app)
        try:
            await comm_b.send_input(
                {"type": "websocket.receive", "text": _make_hello(connection_id="conn-b")}
            )
            await asyncio.wait_for(
                _wait_registered(second_registry, _HOST_ID), timeout=budget(2.0)
            )
            assert first_registry.get(_HOST_ID) is not None

            await comm_a.send_input({"type": "websocket.disconnect", "code": 1000})
            await comm_a.wait(timeout=budget(2.0))
            a_finished = True
            assert first_registry.get(_HOST_ID) is None

            host = first_store.get_host(_HOST_ID)
            assert host is not None
            assert host.status == "online", (
                "a superseded connection's disconnect on another replica "
                "must not mark the live host offline"
            )
            assert host_is_live(host), "the reconnected host must still be live"
        finally:
            await comm_b.send_input({"type": "websocket.disconnect", "code": 1000})
            await comm_b.wait(timeout=budget(2.0))
    finally:
        if not a_finished:
            await comm_a.send_input({"type": "websocket.disconnect", "code": 1000})
            with contextlib.suppress(asyncio.TimeoutError):
                await comm_a.wait(timeout=budget(2.0))
