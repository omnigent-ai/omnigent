"""Authenticated host evidence and acknowledgment ordering across route boundaries."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostShutdownFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.shutdown import ShutdownIntent
from omnigent.server import shutdown_attribution
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.integration.test_host_tunnel_route import _websocket_scope

pytestmark = pytest.mark.asyncio
_HOST = "a" * 32
_RUNNER = "shutdown-route-runner"


class _Auth(AuthProvider):
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


def _hello() -> HostHelloFrame:
    return HostHelloFrame(
        version="test",
        frame_protocol_version=1,
        name="test",
        runners=[_RUNNER],
        process_id="process",
        connection_id="host-connection",
    )


def _intent() -> ShutdownIntent:
    return ShutdownIntent(
        reason="user_stopped_host",
        action="host_stop",
        initiator="local_cli",
        initiator_user_id="spoofed-user",
        host_id=_HOST,
        host_process_id="process",
        host_connection_id="host-connection",
    )


@pytest.mark.parametrize("user", ["owner", "different-user", None])
async def test_http_shutdown_requires_owner_and_uses_verified_actor(
    db_uri: str, user: str | None, app: FastAPI
) -> None:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST, name="test", user_id="owner")
    conv = store.create_conversation(host_id=_HOST, workspace="/tmp", runner_id=_RUNNER)
    await shutdown_attribution.begin_connection(conv.id, _RUNNER, "runner-connection", store)
    registry.register(_HOST, AsyncMock(), _hello(), owner="owner")
    route_app = FastAPI(exception_handlers={OmnigentError: app.exception_handlers[OmnigentError]})
    route_app.include_router(
        create_hosts_router(registry, host_store, store, auth_provider=_Auth()), prefix="/v1"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(route_app), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/v1/hosts/{_HOST}/shutdown",
                json=_intent().model_dump(),
                headers={"x-test-user": user} if user else {},
            )
        assert response.status_code == (200 if user == "owner" else 403 if user else 401)
        evidence = await shutdown_attribution.matching_shutdown(conv.id, store)
        if user == "owner":
            assert evidence is not None
            assert evidence.intent.initiator_user_id == "owner"
        else:
            assert evidence is None
    finally:
        shutdown_attribution.session_scopes.pop(conv.id, None)
        shutdown_attribution.session_shutdowns.pop(conv.id, None)


async def test_host_ack_waits_for_scoped_persistence(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="omnigent.server.routes.host_tunnel")
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(host_id=_HOST, workspace="/tmp", runner_id=_RUNNER)
    await shutdown_attribution.begin_connection(conv.id, _RUNNER, "runner-connection", store)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry,
            host_store,
            conversation_store=store,
            auth_provider=_Auth(),
            local_single_user=False,
        ),
        prefix="/v1",
    )
    scope = _websocket_scope(f"/v1/hosts/{_HOST}/tunnel")
    scope["headers"] = [(b"x-test-user", b"owner")]
    communicator = ApplicationCommunicator(app, scope)
    entered, release = threading.Event(), threading.Event()
    compare = store.compare_shutdown_state

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return compare(*args, **kwargs)

    monkeypatch.setattr(store, "compare_shutdown_state", blocked_write)
    try:
        await communicator.send_input({"type": "websocket.connect"})
        assert (await communicator.receive_output(timeout=5))["type"] == "websocket.accept"
        await communicator.send_input(
            {"type": "websocket.receive", "text": encode_host_frame(_hello())}
        )
        intent = _intent()
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": encode_host_frame(HostShutdownFrame(intent, [_RUNNER])),
            }
        )
        ack = asyncio.create_task(communicator.receive_output(timeout=5))
        assert await asyncio.to_thread(entered.wait, 5)
        assert not ack.done(), "acknowledgment must follow evidence persistence"
        from omnigent.host.frames import HostRunnerStatusResultFrame
        from omnigent.runner.transports.ws_tunnel.frames import PongFrame, encode_frame

        conn = registry.get(_HOST)
        assert conn is not None
        worker = conn.shutdown_task
        reply = asyncio.get_running_loop().create_future()
        conn.pending_runner_status["control-reply"] = reply
        await communicator.send_input(
            {"type": "websocket.receive", "text": encode_frame(PongFrame(ts=1))}
        )
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": encode_host_frame(HostShutdownFrame(intent, [_RUNNER])),
            }
        )
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": encode_host_frame(HostRunnerStatusResultFrame("control-reply", "alive")),
            }
        )
        assert await asyncio.wait_for(reply, 1) == {"status": "alive"}
        assert any("tunnel keepalive: pong" in row.message for row in caplog.records)
        assert conn.shutdown_task is worker, "duplicates must not create unbounded workers"
        assert not ack.done(), "control replies must not acknowledge uncommitted intent"
        release.set()
        output = await ack
        frame = decode_host_frame(output["text"])
        assert frame.shutdown_id == intent.shutdown_id
        evidence = await shutdown_attribution.matching_shutdown(conv.id, store)
        assert evidence is not None
        assert evidence.intent.initiator_user_id is None
    finally:
        release.set()
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=5)
        shutdown_attribution.session_scopes.pop(conv.id, None)
        shutdown_attribution.session_shutdowns.pop(conv.id, None)


@pytest.mark.parametrize("blocked_method", ["get_conversation", "get_shutdown_state"])
async def test_http_shutdown_rejects_replacement_during_session_lookup(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, blocked_method: str
) -> None:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    host_store.upsert_on_connect(host_id=_HOST, name="test", user_id="owner")
    conv = store.create_conversation(host_id=_HOST, workspace="/tmp", runner_id=_RUNNER)
    await shutdown_attribution.begin_connection(conv.id, _RUNNER, "old-runner-connection", store)
    registry.register(_HOST, AsyncMock(), _hello(), owner="owner")
    route_app = FastAPI()
    route_app.include_router(
        create_hosts_router(registry, host_store, store, auth_provider=_Auth()), prefix="/v1"
    )
    entered, release = threading.Event(), threading.Event()
    lookup = getattr(store, blocked_method)

    def blocked_lookup(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return lookup(*args, **kwargs)

    monkeypatch.setattr(store, blocked_method, blocked_lookup)
    request = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(route_app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(
                client.post(
                    f"/v1/hosts/{_HOST}/shutdown",
                    json=_intent().model_dump(),
                    headers={"x-test-user": "owner"},
                )
            )
            assert await asyncio.to_thread(entered.wait, 5)
            registry.register(
                _HOST,
                AsyncMock(),
                replace(_hello(), connection_id="replacement-host-connection"),
                owner="owner",
            )
            monkeypatch.setattr(store, blocked_method, lookup)
            current = await shutdown_attribution.begin_connection(
                conv.id, _RUNNER, "replacement-runner-connection", store
            )
            assert current is not None
            release.set()
            response = await request
            assert response.status_code == 409, response.text
            state = store.get_shutdown_state(conv.id)
            assert state["scope"] == current.model_dump_json()
            assert not state["intent"]
            assert await shutdown_attribution.matching_shutdown(conv.id, store) is None
    finally:
        release.set()
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
        shutdown_attribution.session_scopes.pop(conv.id, None)
        shutdown_attribution.session_shutdowns.pop(conv.id, None)


async def test_host_disconnect_cancels_its_shutdown_worker(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = HostRegistry()
    entered, canceled = asyncio.Event(), asyncio.Event()

    async def pending_record(*_args, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    monkeypatch.setattr(shutdown_attribution, "record_host_shutdown", pending_record)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry, HostStore(db_uri), conversation_store=SqlAlchemyConversationStore(db_uri)
        ),
        prefix="/v1",
    )
    communicator = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST}/tunnel"))
    try:
        await communicator.send_input({"type": "websocket.connect"})
        assert (await communicator.receive_output(timeout=5))["type"] == "websocket.accept"
        await communicator.send_input(
            {"type": "websocket.receive", "text": encode_host_frame(_hello())}
        )
        await communicator.send_input(
            {
                "type": "websocket.receive",
                "text": encode_host_frame(HostShutdownFrame(_intent(), [_RUNNER])),
            }
        )
        await asyncio.wait_for(entered.wait(), 5)
        conn = registry.get(_HOST)
        assert conn is not None and conn.shutdown_task is not None
        worker = conn.shutdown_task
        await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        await communicator.wait(timeout=5)
        assert canceled.is_set() and worker.cancelled()
        assert conn.shutdown_task is None
        assert registry.get(_HOST) is None
    finally:
        communicator.stop()


@pytest.mark.parametrize("response_id", [None, "stopped-turn", "new-turn"])
async def test_external_running_invalidates_stop_only_for_a_new_response(
    client: httpx.AsyncClient, db_uri: str, response_id: str | None
) -> None:
    from omnigent.runtime import session_stream
    from omnigent.server import session_live_state
    from omnigent.server.routes._sessions.common import _session_status_cache

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id=_RUNNER)
    await shutdown_attribution.begin_connection(conv.id, _RUNNER, "connection", store)
    scope = await shutdown_attribution.advance_lifecycle(conv.id, store, "stopped-turn")
    store.set_session_live_status(conv.id, "running")
    _session_status_cache[conv.id] = "running"
    evidence = await shutdown_attribution.record_session_shutdown(
        store.get_conversation(conv.id),
        ShutdownIntent(
            reason="user_stopped_session", action="stop_session", initiator="authenticated_user"
        ),
        store,
    )
    assert evidence is not None
    try:
        data = {"status": "running"}
        if response_id is not None:
            data["response_id"] = response_id
        response = await client.post(
            f"/v1/sessions/{conv.id}/events",
            json={"type": "external_session_status", "data": data},
        )
        assert response.status_code == 202, response.text
        assert response.json() == {"queued": False}
        current = await shutdown_attribution.matching_shutdown(conv.id, store)
        if response_id == "new-turn":
            assert current is None
            assert shutdown_attribution.session_scopes[conv.id] != scope
        else:
            assert current == evidence
            assert shutdown_attribution.session_scopes[conv.id] == scope
    finally:
        await session_live_state.drain_pending_writes()
        shutdown_attribution.session_scopes.pop(conv.id, None)
        shutdown_attribution.session_shutdowns.pop(conv.id, None)
        _session_status_cache.pop(conv.id, None)
        session_stream.close(conv.id)
