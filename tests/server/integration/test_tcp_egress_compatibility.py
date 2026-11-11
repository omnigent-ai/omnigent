"""Reject named TCP policies before an older runtime can ignore them."""

import dataclasses
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_MCP_REGISTRY, CAP_TCP_EGRESS
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runtime import get_agent_store, get_conversation_store
from omnigent.server.auth import RESERVED_USER_LOCAL
from tests.server.helpers import build_agent_bundle, create_test_session
from tests.server.integration.test_session_host_launch import app as app
from tests.server.integration.test_session_worktree_create import _HOST_ID, _SOURCE_REPO
from tests.server.integration.test_session_worktree_create import (
    register_worktree_host as register_worktree_host,
)

pytestmark = pytest.mark.asyncio


def _tcp_bundle(scope: str) -> bytes:
    environment = {
        "type": "caller_process",
        "sandbox": {
            "type": "linux_bwrap",
            "tcp_egress": [{"name": "db", "host": "db.example.test", "port": 5432}],
        },
    }
    return build_agent_bundle(
        "tcp-agent",
        os_env=environment if scope == "root" else None,
        sub_agents=[{"name": "child", **({"os_env": environment} if scope == "child" else {})}],
        terminals={"shell": {"command": "bash", "os_env": environment}}
        if scope == "terminal"
        else None,
    )


@pytest.mark.parametrize("scope", ["root", "child", "terminal", "plain"])
@pytest.mark.parametrize("capable", [False, True])
@pytest.mark.parametrize("upload", [False, True])
async def test_create_checks_tcp_support_before_side_effects(
    client, app, register_worktree_host, monkeypatch, scope, capable, upload
):
    capture = register_worktree_host(capabilities=[CAP_TCP_EGRESS] if capable else [])
    bundle = _tcp_bundle(scope)
    source = await create_test_session(client, bundle=bundle)
    store = get_conversation_store()
    create = Mock(wraps=store.create_session_with_agent)
    monkeypatch.setattr(store, "create_session_with_agent", create)
    metadata = {"host_id": _HOST_ID, "workspace": _SOURCE_REPO}
    if upload:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
    else:
        response = await client.post(
            "/v1/sessions",
            json={"agent_id": source["agent_id"], "sub_agent_name": "child", **metadata},
        )
    if scope != "plain" and not capable:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == ErrorCode.RUNNER_CAPABILITY_MISMATCH
        assert "TCP egress support" in response.text
        create.assert_not_called()
        assert not capture.create and not capture.launches
    else:
        assert response.status_code == 201, response.text
        assert len(capture.launches) == 1


@pytest.mark.parametrize("scope", ["root", "child", "terminal"])
async def test_old_runner_cannot_bind_initialize_or_dispatch_then_recovers(client, app, scope):
    session = await create_test_session(client, bundle=_tcp_bundle(scope))
    sid = session["id"]
    connection = app.state.tunnel_registry.register(
        "old-runner",
        Mock(),
        HelloFrame(
            runner_version="same-version",
            frame_protocol_version=1,
            capabilities=[CAP_MCP_REGISTRY],
        ),
        owner=RESERVED_USER_LOCAL,
    )
    runner = AsyncMock()
    runner.post.return_value = httpx.Response(
        200, json={}, request=httpx.Request("POST", "http://runner/v1/sessions")
    )
    try:
        response = await client.patch(f"/v1/sessions/{sid}", json={"runner_id": "old-runner"})
        assert response.status_code == 503, response.text
        assert get_conversation_store().get_conversation(sid).runner_id is None
        conv = dataclasses.replace(
            get_conversation_store().get_conversation(sid), runner_id="old-runner"
        )
        initializer = app.state.runner_session_initializer
        with pytest.raises(OmnigentError, match="TCP egress support"):
            await initializer.initialize(conv, runner, timeout=1)
        runner.post.assert_not_awaited()
        from omnigent.server.routes._sessions.orchestration import (
            _dispatch_session_event_to_runner_impl,
        )
        from omnigent.server.schemas import SessionEventInput

        with pytest.raises(OmnigentError, match="TCP egress support"):
            await _dispatch_session_event_to_runner_impl(
                sid,
                conv,
                SessionEventInput(type="message", role="user", content="hello"),
                get_conversation_store(),
                runner,
                agent_name=None,
                file_store=None,
                artifact_store=None,
                runner_router=app.state.runner_router,
            )
        runner.post.assert_not_awaited()
        connection.hello.capabilities.append(CAP_TCP_EGRESS)
        assert (await initializer.initialize(conv, runner, timeout=1)).status_code == 200
        runner.post.assert_awaited_once()
        connection.hello.capabilities.remove(CAP_TCP_EGRESS)
        with pytest.raises(OmnigentError, match="TCP egress support"):
            await initializer.initialize(conv, runner, timeout=1)
        runner.post.assert_awaited_once()
    finally:
        app.state.tunnel_registry.deregister("old-runner", session=connection)


async def test_old_host_cannot_relaunch_or_admit_saved_tcp_session(
    client, app, register_worktree_host
):
    capture = register_worktree_host(capabilities=[CAP_MCP_REGISTRY])
    session = await create_test_session(client, bundle=_tcp_bundle("terminal"))
    sid = session["id"]
    store = get_conversation_store()
    store.set_host_id(sid, _HOST_ID, _SOURCE_REPO)
    store.replace_runner_id(sid, "previous-runner")
    response = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners", json={"session_id": sid, "workspace": _SOURCE_REPO}
    )
    assert response.status_code == 503, response.text
    assert store.get_conversation(sid).runner_id == "previous-runner"
    assert not capture.launches
    with pytest.raises(OmnigentError, match="TCP egress support"):
        await app.state.host_registry.admit_launch(app.state.host_registry.get(_HOST_ID), sid)
    assert not capture.launches


async def test_capable_host_does_not_mask_old_runner_on_bundle_replacement(
    client, app, register_worktree_host
):
    register_worktree_host(capabilities=[CAP_MCP_REGISTRY, CAP_TCP_EGRESS])
    session = await create_test_session(client, name="tcp-agent")
    sid = session["id"]
    store = get_conversation_store()
    store.set_host_id(sid, _HOST_ID, _SOURCE_REPO)
    store.replace_runner_id(sid, "old-runner")
    connection = app.state.tunnel_registry.register(
        "old-runner", Mock(), HelloFrame(runner_version="same-version", frame_protocol_version=1)
    )
    before = get_agent_store().get(session["agent_id"]).bundle_location
    try:
        response = await client.put(
            f"/v1/sessions/{sid}/agent",
            files={"bundle": ("agent.tar.gz", _tcp_bundle("root"))},
        )
        assert response.status_code == 503, response.text
        assert "runner does not advertise TCP egress" in response.text
        assert get_agent_store().get(session["agent_id"]).bundle_location == before
    finally:
        app.state.tunnel_registry.deregister("old-runner", session=connection)


async def test_bound_old_runner_cannot_serve_shell_or_terminal_attach(client, app, monkeypatch):
    session = await create_test_session(client, bundle=_tcp_bundle("root"))
    sid = session["id"]
    store = get_conversation_store()
    store.replace_runner_id(sid, "old-runner")
    connection = app.state.tunnel_registry.register(
        "old-runner", Mock(), HelloFrame(runner_version="old", frame_protocol_version=1)
    )
    runner = AsyncMock()
    client_factory = Mock(return_value=runner)
    router = app.state.runner_router
    from omnigent.runtime import _globals

    monkeypatch.setattr(_globals, "_runner_router", router)
    monkeypatch.setattr(router, "_client_for_runner", client_factory)
    try:
        for route in (
            lambda: router.client_for_session_resources(sid),
            lambda: router.client_for_existing_conversation(sid),
            lambda: router.client_for_conversation(conversation_id=sid, harness="claude-sdk"),
        ):
            with pytest.raises(OmnigentError, match="TCP egress support"):
                route()
        response = await client.post(
            f"/v1/sessions/{sid}/resources/environments/default/shell",
            json={"command": "echo must-not-execute"},
        )
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == ErrorCode.RUNNER_CAPABILITY_MISMATCH
        from omnigent.server.routes._sessions.helpers import _get_runner_client_impl

        with pytest.raises(OmnigentError, match="TCP egress support"):
            await _get_runner_client_impl(sid, router)
        client_factory.assert_not_called()
        runner.post.assert_not_awaited()
    finally:
        app.state.tunnel_registry.deregister("old-runner", session=connection)
