"""Mixed-version deployments fail before handing registry specs to old runtimes."""

import dataclasses
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_MCP_REGISTRY
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runtime import get_agent_store, get_conversation_store
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.mcp_compatibility import registry_services, require_registry_mcp_runtime
from omnigent.spec.types import AgentSpec, MCPServerConfig
from tests.server.helpers import build_agent_bundle, create_test_session
from tests.server.integration.test_mcp_registry import launch_registry as launch_registry
from tests.server.integration.test_session_host_launch import app as app
from tests.server.integration.test_session_worktree_create import (
    _HOST_ID,
    _SOURCE_REPO,
)
from tests.server.integration.test_session_worktree_create import (
    register_worktree_host as register_worktree_host,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize("capable,selected", [(False, True), (False, False), (True, True)])
async def test_create_checks_host_before_persistence(
    client, app, launch_registry, register_worktree_host, monkeypatch, upload, capable, selected
):
    capture = register_worktree_host(capabilities=[CAP_MCP_REGISTRY] if capable else [])
    source = await create_test_session(client)
    store = get_conversation_store()
    create = Mock(wraps=store.create_session_with_agent)
    monkeypatch.setattr(store, "create_session_with_agent", create)
    metadata = {
        "host_id": _HOST_ID,
        "workspace": _SOURCE_REPO,
        "mcp_registry_services": ["tracker"] if selected else [],
    }
    if not upload:
        metadata["git"] = {"branch_name": "registry-check"}
    if upload:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": ("agent.tar.gz", build_agent_bundle("new"), "application/gzip")},
        )
    else:
        response = await client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], **metadata}
        )
    if selected and not capable:
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == ErrorCode.RUNNER_CAPABILITY_MISMATCH
        assert "restart the host and session runner" in response.text
        assert "tracker" in response.text
        create.assert_not_called()
        assert not capture.create and not capture.launches
    else:
        assert response.status_code == 201, response.text
        assert len(capture.launches) == 1


async def test_reconnect_refuses_before_changing_binding(
    client, app, launch_registry, register_worktree_host
):
    capture = register_worktree_host(capabilities=[])
    session = await create_test_session(client)
    sid = session["id"]
    attached = await client.post(
        f"/v1/sessions/{sid}/agent/mcp-servers",
        json={"name": "tracker", "transport": "registry"},
    )
    assert attached.status_code == 200
    store = get_conversation_store()
    store.set_host_id(sid, _HOST_ID, _SOURCE_REPO)
    store.replace_runner_id(sid, "previous-runner")
    response = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners", json={"session_id": sid, "workspace": _SOURCE_REPO}
    )
    assert response.status_code == 503, response.text
    assert "MCP registry support" in response.text
    assert store.get_conversation(sid).runner_id == "previous-runner"
    assert not capture.launches

    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_impl

    with pytest.raises(OmnigentError, match="MCP registry support"):
        await _launch_runner_on_host_impl(
            store.get_conversation(sid),
            store,
            app.state.host_registry,
            app.state.host_registry.get(_HOST_ID),
        )
    assert store.get_conversation(sid).runner_id == "previous-runner"
    assert not capture.launches


@pytest.mark.parametrize("method", ["post", "put"])
async def test_new_host_does_not_mask_old_runner_when_editing(
    client, app, launch_registry, register_worktree_host, method
):
    register_worktree_host(capabilities=[CAP_MCP_REGISTRY])
    session = await create_test_session(client)
    sid = session["id"]
    url = f"/v1/sessions/{sid}/agent/mcp-servers"
    if method == "put":
        response = await client.post(url, json={"name": "tracker", "transport": "registry"})
        assert response.status_code == 200
    store = get_conversation_store()
    store.set_host_id(sid, _HOST_ID, _SOURCE_REPO)
    store.replace_runner_id(sid, "old-runner")
    connection = app.state.tunnel_registry.register(
        "old-runner",
        Mock(),
        HelloFrame(runner_version="same-version", frame_protocol_version=1),
        owner=RESERVED_USER_LOCAL,
    )
    before = get_agent_store().get(session["agent_id"]).bundle_location
    response = await getattr(client, method)(
        url + ("/tracker" if method == "put" else ""),
        json={"name": "tracker", "transport": "registry"},
    )
    assert response.status_code == 503, response.text
    assert "runner does not advertise" in response.text
    assert get_agent_store().get(session["agent_id"]).bundle_location == before
    app.state.tunnel_registry.deregister("old-runner", session=connection)


async def test_cli_rebind_and_initialization_reject_old_runner_then_recover(
    client, app, launch_registry
):
    session = await create_test_session(client)
    sid = session["id"]
    assert (
        await client.post(
            f"/v1/sessions/{sid}/agent/mcp-servers",
            json={"name": "tracker", "transport": "registry"},
        )
    ).status_code == 200
    connection = app.state.tunnel_registry.register(
        "old-runner",
        Mock(),
        HelloFrame(runner_version="same-version", frame_protocol_version=1),
        owner=RESERVED_USER_LOCAL,
    )
    response = await client.patch(f"/v1/sessions/{sid}", json={"runner_id": "old-runner"})
    assert response.status_code == 503, response.text
    assert get_conversation_store().get_conversation(sid).runner_id is None
    conv = dataclasses.replace(
        get_conversation_store().get_conversation(sid), runner_id="old-runner"
    )
    runner = AsyncMock()
    runner.post.return_value = httpx.Response(
        200, json={}, request=httpx.Request("POST", "http://runner/v1/sessions")
    )
    initializer = app.state.runner_session_initializer
    with pytest.raises(OmnigentError, match="MCP registry support"):
        await initializer.initialize(conv, runner, timeout=1)
    runner.post.assert_not_awaited()
    from omnigent.server.routes._sessions.orchestration import (
        _dispatch_session_event_to_runner_impl,
    )
    from omnigent.server.schemas import SessionEventInput

    with pytest.raises(OmnigentError, match="MCP registry support"):
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
    # Identical version strings: the negotiated feature, not semver, decides support.
    connection.hello.capabilities.append(CAP_MCP_REGISTRY)
    response = await initializer.initialize(conv, runner, timeout=1)
    assert response.status_code == 200
    runner.post.assert_awaited_once()
    app.state.tunnel_registry.deregister("old-runner", session=connection)


@pytest.mark.parametrize("relaunch", [False, True])
async def test_managed_incompatible_host_settles_launch_and_cleans_up(
    client, app, launch_registry, register_worktree_host, monkeypatch, relaunch
):
    from omnigent.server.managed_hosts import (
        ManagedHostLaunch,
        ManagedLaunchTracker,
        parse_sandbox_config,
    )
    from omnigent.server.routes._sessions import orchestration

    capture = register_worktree_host(capabilities=[])
    session = await create_test_session(client)
    sid = session["id"]
    assert (
        await client.post(
            f"/v1/sessions/{sid}/agent/mcp-servers",
            json={"name": "tracker", "transport": "registry"},
        )
    ).status_code == 200
    tracker = ManagedLaunchTracker()
    tracker.begin(sid)
    terminate = AsyncMock()
    monkeypatch.setattr("omnigent.server.managed_hosts.terminate_managed_host", terminate)
    await orchestration._bind_and_launch_managed_runner(
        session_id=sid,
        managed=ManagedHostLaunch(_HOST_ID, _SOURCE_REPO),
        sandbox_config=parse_sandbox_config(
            {"provider": "modal", "server_url": "https://example.test"}
        ),
        tracker=tracker,
        conversation_store=get_conversation_store(),
        host_store=app.state.host_store,
        host_registry=app.state.host_registry,
        tunnel_registry=app.state.tunnel_registry,
        relaunch_host=app.state.host_store.get_host(_HOST_ID) if relaunch else None,
    )
    state = tracker.get(sid)
    assert state.settled.is_set()
    assert "MCP registry support" in state.error
    assert not capture.launches
    assert get_conversation_store().get_conversation(sid).runner_id is None
    assert terminate.await_count == (0 if relaunch else 1)


async def test_registry_checks_include_nested_agents_and_preserve_replica_routing():
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
    from omnigent.server.host_registry import HostRegistry

    spec = AgentSpec(
        spec_version=1,
        name="root",
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="child",
                mcp_servers=[MCPServerConfig(name="tracker", transport="registry")],
            )
        ],
    )
    router = Mock()
    router.host_is_on_another_replica.return_value = True
    with pytest.raises(OmnigentError) as error:
        require_registry_mcp_runtime(
            registry_services(spec),
            host_id="remote-host",
            runner_id="remote-runner",
            host_registry=HostRegistry(),
            tunnel_registry=TunnelRegistry(),
            runner_router=router,
        )
    assert error.value.code == ErrorCode.WRONG_REPLICA
    for transport in ["http", "stdio"]:
        spec.mcp_servers = [MCPServerConfig(name="custom", transport=transport)]
        spec.sub_agents = []
        assert not registry_services(spec)


async def test_bundle_edit_is_guarded_and_removing_registry_service_is_allowed(
    client, app, launch_registry, register_worktree_host
):
    register_worktree_host(capabilities=[])
    session = await create_test_session(client)
    sid = session["id"]
    url = f"/v1/sessions/{sid}/agent"
    assert (
        await client.post(f"{url}/mcp-servers", json={"name": "tracker", "transport": "registry"})
    ).status_code == 200
    bundle = await client.get(f"{url}/contents")
    assert (await client.delete(f"{url}/mcp-servers/tracker")).status_code == 204
    get_conversation_store().set_host_id(sid, _HOST_ID, _SOURCE_REPO)
    before = get_agent_store().get(session["agent_id"]).bundle_location
    response = await client.put(url, files={"bundle": ("agent.tar.gz", bundle.content)})
    assert response.status_code == 503, response.text
    assert get_agent_store().get(session["agent_id"]).bundle_location == before
    connection = app.state.host_registry.get(_HOST_ID)
    connection.hello.capabilities.append(CAP_MCP_REGISTRY)
    response = await client.put(url, files={"bundle": ("agent.tar.gz", bundle.content)})
    assert response.status_code == 200, response.text
    connection.hello.capabilities.clear()
    assert (await client.delete(f"{url}/mcp-servers/tracker")).status_code == 204
    assert (await client.get(url)).json()["mcp_servers"] == []


async def test_guards_use_app_stores_without_runtime_globals(
    client, app, launch_registry, register_worktree_host, monkeypatch
):
    from omnigent.runtime import _globals

    register_worktree_host(capabilities=[])
    plain = await create_test_session(client, name="plain-agent")
    session = await create_test_session(client)
    sid = session["id"]
    assert (
        await client.post(
            f"/v1/sessions/{sid}/agent/mcp-servers",
            json={"name": "tracker", "transport": "registry"},
        )
    ).status_code == 200
    store = get_conversation_store()
    for item in [plain, session]:
        store.set_host_id(item["id"], _HOST_ID, _SOURCE_REPO)
        store.replace_runner_id(item["id"], "old-runner")
    connection = app.state.tunnel_registry.register(
        "old-runner", Mock(), HelloFrame(runner_version="old", frame_protocol_version=1)
    )
    try:
        # Custom deployments may have no runtime singleton or a different store.
        for global_store in [None, Mock(get=Mock(return_value=None))]:
            monkeypatch.setattr(_globals, "_agent_store", global_store)
            monkeypatch.setattr(_globals, "_agent_cache", None)
            host = app.state.host_registry.get(_HOST_ID)
            await app.state.host_registry.admit_launch(host, plain["id"])
            app.state.runner_router.require_mcp_registry_support(
                store.get_conversation(plain["id"])
            )
            with pytest.raises(OmnigentError, match="MCP registry support"):
                await app.state.host_registry.admit_launch(host, sid)
            with pytest.raises(OmnigentError, match="MCP registry support"):
                app.state.runner_router.require_mcp_registry_support(store.get_conversation(sid))
    finally:
        app.state.tunnel_registry.deregister("old-runner", session=connection)


async def test_native_ensure_rechecks_reconnected_runner(
    client, app, launch_registry, monkeypatch
):
    from omnigent.server.routes._sessions.orchestration import _ensure_native_terminal_ready

    session = await create_test_session(client)
    sid = session["id"]
    assert (
        await client.post(
            f"/v1/sessions/{sid}/agent/mcp-servers",
            json={"name": "tracker", "transport": "registry"},
        )
    ).status_code == 200
    conv = dataclasses.replace(
        get_conversation_store().get_conversation(sid),
        runner_id="reconnecting-runner",
        labels={"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"},
    )
    connection = app.state.tunnel_registry.register(
        conv.runner_id,
        Mock(),
        HelloFrame(
            runner_version="same-version",
            frame_protocol_version=1,
            capabilities=[CAP_MCP_REGISTRY],
        ),
    )

    async def reconnect(*args, **kwargs):
        connection.hello.capabilities.clear()
        return True

    monkeypatch.setattr(app.state.runner_router, "wait_for_runner", reconnect)
    runner = AsyncMock()
    runner.post.side_effect = ConnectionError("tunnel closed before request completed")
    try:
        with pytest.raises(OmnigentError, match="MCP registry support"):
            await _ensure_native_terminal_ready(
                runner, sid, conv, runner_router=app.state.runner_router
            )
        runner.post.assert_awaited_once()
    finally:
        app.state.tunnel_registry.deregister(conv.runner_id, session=connection)
