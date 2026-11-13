"""Reject named TCP policies before an older runtime can ignore them."""

import asyncio
import dataclasses
import json
import threading
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_MCP_REGISTRY, CAP_TCP_EGRESS
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TcpEgressRoute, TerminalEnvSpec
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runtime import get_agent_store, get_conversation_store
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.runtime_compatibility import uses_tcp_egress
from omnigent.spec.types import AgentSpec
from tests.server.helpers import build_agent_bundle, create_test_session, echo_runner_client
from tests.server.integration.test_mcp_registry import launch_registry as launch_registry
from tests.server.integration.test_session_host_launch import app as app
from tests.server.integration.test_session_worktree_create import _HOST_ID, _SOURCE_REPO
from tests.server.integration.test_session_worktree_create import (
    register_worktree_host as register_worktree_host,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("terminal", [False, True])
async def test_tcp_requirement_detection_includes_explicit_terminals(nested, terminal):
    target = AgentSpec(spec_version=1)
    spec = AgentSpec(spec_version=1, sub_agents=[target]) if nested else target
    assert not uses_tcp_egress(spec)
    sandbox = OSEnvSandboxSpec(
        type="linux_bwrap", tcp_egress=[TcpEgressRoute("db", "db.example.test", 5432)]
    )
    environment = OSEnvSpec(sandbox=sandbox)
    if terminal:
        target.terminals = {"shell": TerminalEnvSpec(command="bash", os_env=environment)}
    else:
        target.os_env = environment
    assert uses_tcp_egress(spec)
    sandbox.tcp_egress = None
    assert not uses_tcp_egress(spec)


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


@pytest.mark.parametrize(
    ("feature", "scope", "capable"),
    [
        (feature, scope, capable)
        for feature, scope in [
            ("tcp", "root"),
            ("tcp", "child"),
            ("tcp", "terminal"),
            ("mcp", "root"),
        ]
        for capable in (False, True)
    ]
    + [("tcp", "plain", False)],
)
@pytest.mark.parametrize("upload", [False, True])
async def test_create_checks_runtime_support_before_side_effects(
    client,
    app,
    launch_registry,
    register_worktree_host,
    monkeypatch,
    feature,
    scope,
    capable,
    upload,
):
    capability = CAP_TCP_EGRESS if feature == "tcp" else CAP_MCP_REGISTRY
    capture = register_worktree_host(capabilities=[capability] if capable else [])
    bundle = _tcp_bundle(scope if feature == "tcp" else "plain")
    source = await create_test_session(client, bundle=bundle)
    store = get_conversation_store()
    creates = []
    for method in ("create_session_with_agent", "create_conversation"):
        create = Mock(wraps=getattr(store, method))
        monkeypatch.setattr(store, method, create)
        creates.append(create)
    metadata = {"host_id": _HOST_ID, "workspace": _SOURCE_REPO}
    if feature == "mcp":
        metadata["mcp_registry_services"] = ["tracker"] if scope != "plain" else []
    if not upload:
        metadata["git"] = {"branch_name": "runtime-check"}
    if upload:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
    else:
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": source["agent_id"],
                **({"sub_agent_name": "child"} if feature == "tcp" else {}),
                **metadata,
            },
        )
    if scope != "plain" and not capable:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == ErrorCode.RUNNER_CAPABILITY_MISMATCH
        assert (
            "TCP egress support" if feature == "tcp" else "MCP registry support"
        ) in response.text
        if feature == "mcp":
            assert "tracker" in response.text
            assert "restart the host and session runner" in response.text
        for create in creates:
            create.assert_not_called()
        assert not capture.create and not capture.launches
    else:
        assert response.status_code == 201, response.text
        assert len(capture.launches) == 1


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("ancestor_host", [False, True])
async def test_hostless_child_checks_inherited_runtime_before_persistence(
    client, app, register_worktree_host, monkeypatch, upload, ancestor_host
):
    capture = register_worktree_host(capabilities=[CAP_MCP_REGISTRY, CAP_TCP_EGRESS])
    source = await create_test_session(client, bundle=_tcp_bundle("root"))
    store = get_conversation_store()
    store.set_host_id(source["id"], _HOST_ID, _SOURCE_REPO)
    store.replace_runner_id(source["id"], "parent-runner")
    parent_id = source["id"]
    if ancestor_host:
        parent = store.create_conversation(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            agent_id=source["agent_id"],
            runner_id="parent-runner",
        )
        parent_id = parent.id
    creates = []
    for method in ("create_session_with_agent", "create_conversation"):
        create = Mock(wraps=getattr(store, method))
        monkeypatch.setattr(store, method, create)
        creates.append(create)

    async def create_child():
        metadata = {"parent_session_id": parent_id}
        if upload:
            return await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps(metadata)},
                files={"bundle": ("agent.tar.gz", _tcp_bundle("root"), "application/gzip")},
            )
        return await client.post("/v1/sessions", json={"agent_id": source["agent_id"], **metadata})

    loop_thread = threading.get_ident()

    def remote_host(host_id):
        assert threading.get_ident() != loop_thread
        return host_id == _HOST_ID

    with monkeypatch.context() as remote:
        remote.setattr(app.state.host_registry, "get", Mock(return_value=None))
        ownership = Mock(side_effect=remote_host)
        remote.setattr(app.state.runner_router, "host_is_on_another_replica", ownership)
        response = await create_child()
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == ErrorCode.WRONG_REPLICA
        assert error["slice_key"] == _HOST_ID
        ownership.assert_called_once_with(_HOST_ID)
        for create in creates:
            create.assert_not_called()
    connection = app.state.tunnel_registry.register(
        "parent-runner",
        Mock(),
        HelloFrame(
            runner_version="same-version",
            frame_protocol_version=1,
            capabilities=[CAP_MCP_REGISTRY],
        ),
        owner=RESERVED_USER_LOCAL,
    )
    try:
        response = await create_child()
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == ErrorCode.RUNNER_CAPABILITY_MISMATCH
        for create in creates:
            create.assert_not_called()
        async with echo_runner_client() as runner:
            monkeypatch.setattr(app.state.runner_router, "_client_for_runner", lambda _: runner)
            connection.hello.capabilities.append(CAP_TCP_EGRESS)
            response = await create_child()
        assert response.status_code == 201, response.text
        child_id = response.json()["session_id" if upload else "id"]
        child = store.get_conversation(child_id)
        assert child.runner_id == "parent-runner"
        assert child.host_id is None
        assert not capture.create and not capture.launches
    finally:
        app.state.tunnel_registry.deregister("parent-runner", session=connection)


@pytest.mark.parametrize(
    ("feature", "scope"), [("tcp", "root"), ("tcp", "child"), ("tcp", "terminal"), ("mcp", "root")]
)
async def test_old_runner_cannot_bind_initialize_or_dispatch_then_recovers(
    client, app, launch_registry, feature, scope
):
    capability = CAP_TCP_EGRESS if feature == "tcp" else CAP_MCP_REGISTRY
    message = "TCP egress support" if feature == "tcp" else "MCP registry support"
    session = await create_test_session(
        client, bundle=_tcp_bundle(scope if feature == "tcp" else "plain")
    )
    if feature == "mcp":
        response = await client.post(
            f"/v1/sessions/{session['id']}/agent/mcp-servers",
            json={"name": "tracker", "transport": "registry"},
        )
        assert response.status_code == 200, response.text
    sid = session["id"]
    connection = app.state.tunnel_registry.register(
        "old-runner",
        Mock(),
        HelloFrame(
            runner_version="same-version",
            frame_protocol_version=1,
            capabilities=[CAP_MCP_REGISTRY] if feature == "tcp" else [],
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
        with pytest.raises(OmnigentError, match=message):
            await initializer.initialize(conv, runner, timeout=1)
        runner.post.assert_not_awaited()
        from omnigent.server.routes._sessions.orchestration import (
            _dispatch_session_event_to_runner_impl,
        )
        from omnigent.server.schemas import SessionEventInput

        with pytest.raises(OmnigentError, match=message):
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
        connection.hello.capabilities.append(capability)
        assert (await initializer.initialize(conv, runner, timeout=1)).status_code == 200
        runner.post.assert_awaited_once()
        connection.hello.capabilities.remove(capability)
        with pytest.raises(OmnigentError, match=message):
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
        from omnigent.server._runner_ws_tunnel import (
            make_direct_attach_resolver,
            make_tunnel_ws_factory,
        )

        direct = make_direct_attach_resolver(router, app.state.tunnel_registry)
        assert await asyncio.to_thread(direct, sid) is None
        tunnel = make_tunnel_ws_factory(router, app.state.tunnel_registry)
        with pytest.raises(RuntimeError, match="TCP egress support"):
            await asyncio.to_thread(tunnel, f"/v1/sessions/{sid}/resources/terminals/shell/attach")
        client_factory.assert_not_called()
        runner.post.assert_not_awaited()
    finally:
        app.state.tunnel_registry.deregister("old-runner", session=connection)


async def test_plain_agent_requirement_reads_in_resource_helpers_run_off_loop(
    client, app, monkeypatch
):
    from omnigent.runtime import _globals
    from omnigent.server.routes._sessions.helpers import (
        _get_runner_client_for_resource_access_impl,
        _get_runner_client_impl,
    )

    session = await create_test_session(client, bundle=_tcp_bundle("plain"))
    sid = session["id"]
    get_conversation_store().replace_runner_id(sid, "pre-tcp")
    connection = app.state.tunnel_registry.register(
        "pre-tcp",
        Mock(),
        HelloFrame(
            runner_version="old", frame_protocol_version=1, capabilities=[CAP_MCP_REGISTRY]
        ),
    )
    router = app.state.runner_router
    monkeypatch.setattr(_globals, "_runner_router", router)
    loop_thread = threading.get_ident()
    reads = {"get": [], "load": []}

    def track(call, name):
        def tracked(*args, **kwargs):
            reads[name].append(threading.get_ident())
            return call(*args, **kwargs)

        return tracked

    for owner, name in ((app.state.agent_store, "get"), (router._agent_cache, "load")):
        monkeypatch.setattr(owner, name, track(getattr(owner, name), name))
    try:
        assert await _get_runner_client_impl(sid, router) is not None
        assert await _get_runner_client_for_resource_access_impl(sid) is not None
        assert all(
            threads and all(t != loop_thread for t in threads) for threads in reads.values()
        )
        failure = OmnigentError("invalid saved spec", code=ErrorCode.INVALID_INPUT)
        monkeypatch.setattr(router._agent_cache, "load", Mock(side_effect=failure))
        with pytest.raises(OmnigentError) as error:
            await _get_runner_client_impl(sid, router)
        assert error.value.code == failure.code and error.value.__cause__ is failure
        assert await _get_runner_client_impl(sid, router, for_control=True) is not None
    finally:
        app.state.tunnel_registry.deregister("pre-tcp", session=connection)
