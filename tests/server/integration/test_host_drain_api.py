"""Host drain (launch fence) API: start/status/undrain, close-ack, compat."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import Mock

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_LAUNCH_DRAIN,
    HOST_DRAINING_ERROR_CODE,
    HostCreateWorktreeFrame,
    HostDrainStatusFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostSetDrainFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.budgets import budget

pytestmark = pytest.mark.asyncio

_HOST_ID = "5d1f0b8e0d6a4f6c9a3e2b1c0d9e8f7a"
_DRAIN = f"/v1/hosts/{_HOST_ID}/drain"


def _scope(path: str) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


@pytest.fixture()
def drain_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    registry.launch_authorizer = host_store.admit_launch
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(create_hosts_router(registry, host_store, conv_store), prefix="/v1")

    @app.exception_handler(OmnigentError)
    async def _omnigent_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return app, registry, host_store, conv_store


async def _connect(
    app: FastAPI, registry: HostRegistry, *, supports_drain: bool = True
) -> ApplicationCommunicator:
    """Connect a fake host; ``supports_drain=False`` models an older host build."""
    previous = registry.get(_HOST_ID)
    comm = ApplicationCommunicator(app, _scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=budget(1.0))
    assert accepted["type"] == "websocket.accept"
    hello = HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="drain-laptop",
        capabilities=[CAP_LAUNCH_DRAIN] if supports_drain else [],
    )
    await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(hello)})
    while registry.get(_HOST_ID) is None or registry.get(_HOST_ID) is previous:
        await asyncio.sleep(0.01)
    return comm


async def _next_frame(comm: ApplicationCommunicator, kind: type[Any], timeout: float = 2.0) -> Any:
    """Return the next host-bound frame of *kind*, skipping keepalive pings."""
    for _ in range(40):
        output = await comm.receive_output(timeout=budget(timeout))
        if output["type"] != "websocket.send":
            continue
        try:
            frame = decode_host_frame(output["text"])
        except ValueError:
            continue
        if isinstance(frame, kind):
            return frame
    raise AssertionError(f"host never received a {kind.__name__}")


async def _report(comm: ApplicationCommunicator, frame: HostDrainStatusFrame) -> None:
    await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(frame)})


async def _status(app: FastAPI, method: str = "GET") -> dict[str, Any]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.request(method, _DRAIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _wait_status(app: FastAPI, key: str, value: object) -> dict[str, Any]:
    return await _wait_until(app, lambda body: body[key] == value)


async def _wait_until(app: FastAPI, predicate: Any) -> dict[str, Any]:
    async def _poll() -> dict[str, Any]:
        while True:
            body = await _status(app)
            if predicate(body):
                return body
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(_poll(), budget(3.0))


async def _close(comm: ApplicationCommunicator) -> None:
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    await comm.wait(timeout=budget(1.0))


async def test_drain_full_cycle_reports_safe_only_after_generation_ack(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, registry, _store, _convs = drain_app
    comm = await _connect(app, registry)
    try:
        assert (await _status(app))["state"] == "active"

        started = await _status(app, "POST")
        assert started["state"] == "draining"
        assert started["generation"] == 1
        assert started["safe_to_restart"] is False
        assert await _next_frame(comm, HostSetDrainFrame) == HostSetDrainFrame(1, True)

        # A not-yet-complete report (a spawn still in flight) is surfaced, not acked.
        await _report(comm, HostDrainStatusFrame(1, True, 1, 2, False))
        body = await _wait_until(app, lambda b: b["host_report"] is not None)
        assert body["host_report"]["pending_launches"] == 1
        assert body["host_report"]["complete"] is False
        assert body["safe_to_restart"] is False

        # An ack for a different generation is ignored.
        await _report(comm, HostDrainStatusFrame(0, True, 0, 2, True))
        await _report(comm, HostDrainStatusFrame(1, True, 0, 2, True))
        drained = await _wait_status(app, "safe_to_restart", True)
        assert drained["state"] == "drained"
        assert drained["acked_generation"] == 1
        assert drained["host_report"]["running_runners"] == 2, "running sessions keep running"

        undrained = await _status(app, "DELETE")
        assert undrained["state"] == "active"
        assert undrained["safe_to_restart"] is False
        assert undrained["generation"] == 2
        assert await _next_frame(comm, HostSetDrainFrame) == HostSetDrainFrame(2, False)
    finally:
        await _close(comm)


async def test_old_host_without_drain_support_is_never_safe(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """An old build cannot be fenced host-side, so it must read unsupported."""
    app, registry, _store, _convs = drain_app
    comm = await _connect(app, registry, supports_drain=False)
    try:
        body = await _status(app, "POST")
        assert body["state"] == "unsupported"
        assert body["drain_support"] == "unsupported"
        assert body["safe_to_restart"] is False
        # Even a (forged) complete report cannot make it safe.
        await _report(comm, HostDrainStatusFrame(1, True, 0, 0, True))
        await asyncio.sleep(0.1)
        assert (await _status(app))["safe_to_restart"] is False
        assert registry.get(_HOST_ID).drain_generation_sent == 0  # type: ignore[union-attr]
    finally:
        await _close(comm)


async def test_reconnect_resends_fence_and_requires_a_fresh_ack(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, registry, _store, _convs = drain_app
    comm = await _connect(app, registry)
    await _status(app, "POST")
    await _next_frame(comm, HostSetDrainFrame)
    await _report(comm, HostDrainStatusFrame(1, True, 0, 0, True))
    await _wait_status(app, "safe_to_restart", True)
    await _close(comm)

    comm = await _connect(app, registry)
    try:
        # The fence is the first frame the new connection carries.
        assert await _next_frame(comm, HostSetDrainFrame) == HostSetDrainFrame(1, True)
        body = await _status(app)
        assert body["draining"] is True
        assert body["safe_to_restart"] is False, "a new connection must re-acknowledge"
        await _report(comm, HostDrainStatusFrame(1, True, 0, 0, True))
        await _wait_status(app, "safe_to_restart", True)
    finally:
        await _close(comm)


async def test_drain_started_while_offline_reaches_host_on_connect(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, registry, store, _convs = drain_app
    await _close(await _connect(app, registry))
    store.start_drain(_HOST_ID, None)

    comm = await _connect(app, registry)
    try:
        assert await _next_frame(comm, HostSetDrainFrame) == HostSetDrainFrame(1, True)
    finally:
        await _close(comm)


async def test_drain_committed_on_another_replica_reaches_host_via_keepalive(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.server.routes.host_tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "PING_INTERVAL_S", 0.02)
    monkeypatch.setattr(tunnel_mod, "PING_MISS_THRESHOLD", 100_000)
    app, registry, store, _convs = drain_app
    comm = await _connect(app, registry)
    try:
        store.start_drain(_HOST_ID, None)  # as if written by another replica
        assert await _next_frame(comm, HostSetDrainFrame) == HostSetDrainFrame(1, True)
    finally:
        await _close(comm)


async def test_launch_runner_rejected_before_any_side_effect(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated launch route refuses before worktree creation or binding."""
    app, registry, _store, convs = drain_app
    comm = await _connect(app, registry)
    conv = convs.create_conversation(agent_id=None)
    send = Mock(wraps=registry.send_text)
    monkeypatch.setattr(registry, "send_text", send)
    try:
        await _status(app, "POST")
        await _next_frame(comm, HostSetDrainFrame)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/v1/hosts/{_HOST_ID}/runners",
                json={
                    "session_id": conv.id,
                    "workspace": "/tmp/repo",
                    "git": {"branch_name": "feature/x"},
                },
            )
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == HOST_DRAINING_ERROR_CODE
        sent = [decode_host_frame(call.args[1]) for call in send.call_args_list]
        assert not [
            f for f in sent if isinstance(f, (HostLaunchRunnerFrame, HostCreateWorktreeFrame))
        ]
        saved = convs.get_conversation(conv.id)
        assert saved is not None and saved.runner_id is None and saved.host_id is None
    finally:
        await _close(comm)


async def test_host_side_refusal_after_admission_maps_to_host_draining(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A launch admitted just before the drain, refused by the host, fails cleanly."""
    app, registry, _store, convs = drain_app
    comm = await _connect(app, registry)
    conv = convs.create_conversation(agent_id=None)

    async def _refuse() -> None:
        frame = await _next_frame(comm, HostLaunchRunnerFrame)
        result = HostLaunchRunnerResultFrame(
            request_id=frame.request_id,
            status="failed",
            error="host is draining",
            error_code=HOST_DRAINING_ERROR_CODE,
        )
        await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(result)})

    responder = asyncio.create_task(_refuse())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/v1/hosts/{_HOST_ID}/runners",
                json={"session_id": conv.id, "workspace": "/tmp/repo"},
            )
        await responder
        assert resp.status_code == 503, resp.text
        assert resp.json()["error"]["code"] == HOST_DRAINING_ERROR_CODE
        saved = convs.get_conversation(conv.id)
        assert saved is not None and saved.runner_id is None, "binding must be rolled back"
    finally:
        await _close(comm)


async def test_drain_unknown_host_is_404(
    drain_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, _registry, _store, _convs = drain_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method in ("GET", "POST", "DELETE"):
            resp = await client.request(method, _DRAIN)
            assert resp.status_code == 404, (method, resp.text)
