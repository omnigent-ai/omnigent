"""Regression test: reconnect must reap sessions deleted while the tunnel was down.

A session deleted while this runner's tunnel is down takes the server's offline
path, which skips runner-side cleanup that nothing replays on reconnect, so its
restart-forever transcript forwarder and native server survive. The catch-up
scan now probes every session that still holds a forwarder or a native server
and, on a definitive 404, fully reaps it with the delete route's teardown; a
transient status, a transport failure, or a still-live (200) session is left
untouched.
"""

from __future__ import annotations

import uuid

import httpx
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner import subagent_routing as routing
from omnigent.runner import subagent_work as sw
from omnigent.runner.native import orchestration as orch
from tests.runner.native_forwarder_leak_helpers import (
    FakeAppServer,
    drain_forwarder,
    register_forwarder,
    register_router,
)


def _make_app(handler) -> tuple[FastAPI, httpx.AsyncClient]:
    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    )
    app = create_runner_app(server_client=server_client)
    return app, server_client


async def test_reconnect_keeps_forwarder_on_transient_error() -> None:
    session_id = f"conv_transient_{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        # A transient server error is not proof the session was deleted; an
        # ordinary recoverable failure must leave the forwarder running.
        return httpx.Response(503, json={"error": "unavailable"})

    app, server_client = _make_app(handler)
    task = register_forwarder(session_id)
    try:
        await app.state.catch_up_scan()

        assert not task.cancelled()
        assert session_id in orch._AUTO_FORWARDER_TASKS
    finally:
        await server_client.aclose()
        await drain_forwarder(session_id, task)


async def test_reconnect_keeps_forwarder_on_transport_error() -> None:
    session_id = f"conv_transport_{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        # The tunnel is flapping: the probe itself fails to connect. That is
        # not a 404, so the session's deletion is unproven and the forwarder
        # must survive rather than be reaped on a transport error.
        if request.url.path == f"/v1/sessions/{session_id}":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"id": session_id, "agent_id": "agent_x"})

    app, server_client = _make_app(handler)
    task = register_forwarder(session_id)
    try:
        await app.state.catch_up_scan()

        assert not task.cancelled()
        assert session_id in orch._AUTO_FORWARDER_TASKS
    finally:
        await server_client.aclose()
        await drain_forwarder(session_id, task)


async def test_reconnect_fully_reaps_session_deleted_offline() -> None:
    deleted = f"conv_deleted_{uuid.uuid4().hex}"
    alive = f"conv_alive_{uuid.uuid4().hex}"
    grandchild = f"conv_grand_{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.rsplit("/", 1)[-1]
        if session_id == deleted:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"id": session_id, "agent_id": "agent_x"})

    app, server_client = _make_app(handler)
    deleted_task = register_forwarder(deleted)
    alive_task = register_forwarder(alive)

    # A native server neither forwarder adopted, a live routing endpoint, and
    # runner-local spawn-family state must all be torn down for the deleted
    # session but left for the live one.
    deleted_server = FakeAppServer()
    alive_server = FakeAppServer()
    orch._AUTO_CODEX_APP_SERVERS[deleted] = deleted_server  # type: ignore[assignment]
    orch._AUTO_CODEX_APP_SERVERS[alive] = alive_server  # type: ignore[assignment]
    deleted_router = register_router(deleted)
    alive_router = register_router(alive)
    sw.register_subagent_work(
        parent_session_id=deleted,
        child_session_id=grandchild,
        agent="leaf",
        title="leaf",
        wrapper_label="claude-code-native-ui",
    )

    cleaned: list[str] = []
    registry = app.state.session_resource_registry
    original_cleanup = registry.cleanup_session

    async def _recording_cleanup(session_id: str) -> None:
        cleaned.append(session_id)
        await original_cleanup(session_id)

    registry.cleanup_session = _recording_cleanup  # type: ignore[method-assign]

    try:
        await app.state.catch_up_scan()

        # The deleted session is reaped fully, not just its forwarder: the
        # orphaned native server is closed, its routing endpoint shut down, and
        # its panes/env and spawn-family state dropped, or it leaks them.
        assert deleted_task.cancelled()
        assert deleted not in orch._AUTO_FORWARDER_TASKS
        assert deleted_server.closed, "native server leaked for a session deleted offline"
        assert deleted not in orch._AUTO_CODEX_APP_SERVERS
        assert deleted_router.closed, "routing endpoint leaked for a session deleted offline"
        assert deleted not in routing._session_routers
        assert deleted in cleaned, "deleted session did not get per-session resource cleanup"
        assert sw.list_subagent_work(deleted) == []
        assert sw.get_subagent_work(grandchild) is None

        # The still-live session keeps its forwarder, native server, router, state.
        assert not alive_task.cancelled()
        assert alive in orch._AUTO_FORWARDER_TASKS
        assert not alive_server.closed
        assert not alive_router.closed
        assert alive not in cleaned
    finally:
        registry.cleanup_session = original_cleanup  # type: ignore[method-assign]
        orch._AUTO_CODEX_APP_SERVERS.pop(deleted, None)
        orch._AUTO_CODEX_APP_SERVERS.pop(alive, None)
        routing._session_routers.pop(deleted, None)
        routing._session_routers.pop(alive, None)
        sw.unregister_subagent_work_for_session(deleted)
        sw.unregister_child_session(grandchild)
        await server_client.aclose()
        await drain_forwarder(deleted, deleted_task)
        await drain_forwarder(alive, alive_task)


async def test_reconnect_reaps_server_only_session_deleted_offline() -> None:
    # A native server can outlive its forwarder (the forwarder crashed out of
    # its registry, or never adopted the server), so a session deleted offline
    # may hold only a server. The scan must still enumerate and reap it.
    codex_only = f"conv_codex_{uuid.uuid4().hex}"
    opencode_only = f"conv_opencode_{uuid.uuid4().hex}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    app, server_client = _make_app(handler)
    codex_server = FakeAppServer()
    opencode_server = FakeAppServer()
    orch._AUTO_CODEX_APP_SERVERS[codex_only] = codex_server  # type: ignore[assignment]
    orch._AUTO_OPENCODE_SERVERS[opencode_only] = opencode_server  # type: ignore[assignment]

    try:
        await app.state.catch_up_scan()

        assert codex_server.closed, "codex server with no forwarder leaked after reconnect"
        assert codex_only not in orch._AUTO_CODEX_APP_SERVERS
        assert opencode_server.closed, "opencode server with no forwarder leaked after reconnect"
        assert opencode_only not in orch._AUTO_OPENCODE_SERVERS
    finally:
        orch._AUTO_CODEX_APP_SERVERS.pop(codex_only, None)
        orch._AUTO_OPENCODE_SERVERS.pop(opencode_only, None)
        await server_client.aclose()
