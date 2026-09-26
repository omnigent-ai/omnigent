"""A connected service runs through the session gateway and existing policy path."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from omnigent.server.mcp_registry import (
    McpRegistry,
    McpRegistryConfig,
    McpService,
    McpUpstreamError,
)
from tests.server.helpers import build_agent_bundle, create_test_session

pytestmark = pytest.mark.asyncio


@pytest.fixture
def app(runtime_init, db_uri, tmp_path):
    from omnigent.runtime import get_agent_cache, get_agent_store, get_conversation_store
    from omnigent.server.app import create_app
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    return create_app(
        agent_store=get_agent_store(),
        conversation_store=get_conversation_store(),
        agent_cache=get_agent_cache(),
        artifact_store=LocalArtifactStore(str(tmp_path / "artifacts")),
        file_store=SqlAlchemyFileStore(db_uri),
    )


async def test_registry_tools_are_attached_discovered_and_executed_on_server(
    client, app, monkeypatch
):
    registry = McpRegistry(
        McpRegistryConfig(
            services=[
                McpService(
                    id="tracker",
                    title="Tracker",
                    url="https://example.test/mcp",
                    auth="none",
                    tools=["read_ticket"],
                )
            ]
        ),
        None,
    )
    app.state.mcp_registry = registry
    execute = AsyncMock(
        side_effect=[
            [
                Tool(
                    name="read_ticket",
                    inputSchema={
                        "type": "object",
                        "properties": {"ticket_id": {"type": "string"}},
                    },
                )
            ],
            CallToolResult(content=[TextContent(type="text", text="Ticket TEST-123: ready")]),
        ]
    )
    monkeypatch.setattr(registry, "execute", execute)
    session = await create_test_session(client, name="registry-agent")
    session_id = session["id"]
    attached = await client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        json={"name": "tracker", "transport": "registry"},
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["url"] is None
    listed = await client.post(
        f"/v1/sessions/{session_id}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    assert listed.status_code == 200, listed.text
    assert "result" in listed.json(), listed.text
    assert "tracker__read_ticket" in [t["name"] for t in listed.json()["result"]["tools"]]
    called = await client.post(
        f"/v1/sessions/{session_id}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tracker__read_ticket", "arguments": {"ticket_id": "TEST-123"}},
        },
    )
    assert called.json()["result"]["content"][0]["text"] == "Ticket TEST-123: ready", called.text
    assert execute.await_args.kwargs == {
        "tool": "read_ticket",
        "arguments": {"ticket_id": "TEST-123"},
    }
    # A catalog reference is all the runner receives; no endpoint or provider token.
    agent = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent.json()["mcp_servers"][0]["transport"] == "registry"
    assert "example.test" not in agent.text

    # A normal call must still stop at the existing policy gate.
    from omnigent.policies.types import PolicyResult
    from omnigent.server.routes import sessions as sessions_mod
    from omnigent.spec.types import PolicyAction
    from tests.server.routes.test_sessions_mcp_proxy_policy_retry import (
        _engine_factory_expecting_preload,
        _FixedPolicyEngine,
    )

    engine = _FixedPolicyEngine(PolicyResult(action=PolicyAction.DENY, reason="Operator blocked"))
    monkeypatch.setattr(
        sessions_mod, "_build_policy_engine_from_spec", _engine_factory_expecting_preload(engine)
    )
    execute.reset_mock()
    denied = await client.post(
        f"/v1/sessions/{session_id}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "tracker__read_ticket", "arguments": {"ticket_id": "TEST-123"}},
        },
    )
    assert "Operator blocked" in denied.json()["error"]["message"]
    execute.assert_not_awaited()


@pytest.fixture
def launch_registry(app):
    app.state.mcp_registry = McpRegistry(
        McpRegistryConfig(
            services=[
                McpService(
                    id=name,
                    title=name,
                    url="https://example.test/mcp",
                    auth="none",
                    tools=["read_ticket"],
                    allowed_users=[] if name == "restricted" else None,
                )
                for name in ["tracker", "other", "restricted"]
            ]
        ),
        None,
    )
    return app.state.mcp_registry


@pytest.mark.parametrize("upload", [False, True])
async def test_launch_selection_is_present_before_sandbox_launch(
    client, app, launch_registry, monkeypatch, upload
):
    from omnigent.runtime import get_agent_store, get_conversation_store
    from omnigent.server.managed_hosts import ManagedLaunchTracker, parse_sandbox_config
    from omnigent.server.routes.sessions import routes_core

    source = await create_test_session(client, name="launch-template")
    source_agent = get_agent_store().get(source["agent_id"])
    before_location = source_agent.bundle_location
    launched = []
    launch_finished = asyncio.Event()

    async def launch(*, session_id, agent_id, **kwargs):
        conv = get_conversation_store().get_conversation(session_id)
        assert conv.agent_id == agent_id
        assert agent_id != source["agent_id"]
        agent = get_agent_store().get(agent_id)
        assert agent.session_id == session_id
        summary = await client.get(f"/v1/sessions/{session_id}/agent")
        servers = summary.json()["mcp_servers"]
        assert [(s["name"], s["transport"]) for s in servers] == [("tracker", "registry")]
        assert "example.test" not in summary.text
        launched.append(session_id)
        launch_finished.set()

    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    app.state.sandbox_config = parse_sandbox_config(
        {"provider": "modal", "server_url": "https://managed.example.test"}
    )
    app.state.host_store = object()
    app.state.managed_launches = ManagedLaunchTracker()
    metadata = {"host_type": "managed", "mcp_registry_services": ["tracker", "tracker"]}
    if upload:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle("launch-template"),
                    "application/gzip",
                )
            },
        )
    else:
        response = await client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], **metadata}
        )
    assert response.status_code == 201, response.text
    await asyncio.wait_for(launch_finished.wait(), timeout=5)
    assert len(launched) == 1
    assert get_agent_store().get(source["agent_id"]).bundle_location == before_location
    original = await client.get(f"/v1/sessions/{source['id']}/agent")
    assert original.json()["mcp_servers"] == []
    plain = await client.post("/v1/sessions", json={"agent_id": source["agent_id"]})
    assert plain.status_code == 201, plain.text
    assert plain.json()["agent_id"] == source["agent_id"]


@pytest.mark.parametrize("upload", [False, True])
@pytest.mark.parametrize(
    "service,status", [("missing", 403), ("restricted", 403), ("../invalid", 422)]
)
async def test_invalid_launch_selection_creates_no_session(
    client, launch_registry, monkeypatch, upload, service, status
):
    from omnigent.runtime import get_conversation_store
    from omnigent.server.routes.sessions import routes_core

    source = await create_test_session(client)
    launch = AsyncMock()
    create = Mock(side_effect=AssertionError("selection must be rejected before persistence"))
    monkeypatch.setattr(routes_core, "_run_managed_launch", launch)
    monkeypatch.setattr(get_conversation_store(), "create_session_with_agent", create)
    metadata = {"mcp_registry_services": [service]}
    if upload:
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": ("agent.tar.gz", build_agent_bundle("launch"), "application/gzip")},
        )
    else:
        response = await client.post(
            "/v1/sessions", json={"agent_id": source["agent_id"], **metadata}
        )
    expected = 400 if upload and status == 422 else status
    assert response.status_code == expected, response.text
    create.assert_not_called()
    launch.assert_not_awaited()


@pytest.mark.parametrize("allowed", [[], ["read_ticket"]])
async def test_launch_selection_preserves_existing_registry_tool_restrictions(
    client, launch_registry, tmp_path, allowed
):
    import yaml

    from omnigent.runtime import get_agent_cache, get_agent_store
    from omnigent.server.routes.session_mcp_servers import _tar_gz_dir
    from omnigent.spec import extract_safe

    root = tmp_path / "source"
    extract_safe(build_agent_bundle("restricted-tools"), root)
    mcp_dir = root / "tools" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "tracker.yaml").write_text(
        yaml.safe_dump({"name": "tracker", "transport": "registry", "tools": allowed})
    )
    uploaded = await client.post(
        "/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", _tar_gz_dir(root), "application/gzip")},
    )
    assert uploaded.status_code == 201, uploaded.text
    source = uploaded.json()
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": source["agent_id"], "mcp_registry_services": ["tracker", "other"]},
    )
    assert response.status_code == 201, response.text
    summary = await client.get(f"/v1/sessions/{response.json()['id']}/agent")
    assert {s["name"] for s in summary.json()["mcp_servers"]} == {"tracker", "other"}
    agent = get_agent_store().get(response.json()["agent_id"])
    spec = get_agent_cache().load(agent.id, agent.bundle_location, expand_env=False).spec
    assert next(s for s in spec.mcp_servers if s.name == "tracker").tools == allowed


async def test_launch_selection_does_not_replace_custom_mcp(client, launch_registry):
    source = await create_test_session(client)
    attached = await client.post(
        f"/v1/sessions/{source['id']}/agent/mcp-servers",
        json={"name": "tracker", "transport": "http", "url": "https://custom.example/mcp"},
    )
    assert attached.status_code == 200, attached.text
    response = await client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "mcp_registry_services": ["tracker"]}
    )
    assert response.status_code == 409, response.text


async def test_gateway_reports_upstream_permission_failure(
    client, launch_registry, monkeypatch, caplog
):
    source = await create_test_session(client)
    response = await client.post(
        "/v1/sessions", json={"agent_id": source["agent_id"], "mcp_registry_services": ["tracker"]}
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    execute = AsyncMock(
        side_effect=McpUpstreamError(
            "MCP service denied access (HTTP 403).", kind="http", status_code=403
        )
    )
    monkeypatch.setattr(launch_registry, "execute", execute)
    called = await client.post(
        f"/v1/sessions/{session_id}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "tracker__read_ticket", "arguments": {}},
        },
    )
    assert "HTTP 403" in called.json()["error"]["message"], called.text
    assert "not retried" in called.json()["error"]["message"]
    execute.assert_awaited_once()
    assert f"session={session_id} failure=http http_status=403" in caplog.text


async def test_launch_selection_preserves_initial_metadata(client, launch_registry):
    source = await create_test_session(client, name="registry-metadata")
    metadata = {
        "agent_id": source["agent_id"],
        "mcp_registry_services": ["tracker"],
        "labels": {"review_context": "prototype"},
        "reasoning_effort": "low",
        "model_override": "gpt-4o-mini",
        "cost_control_mode_override": "off",
        "subagent_routing_override": "off",
        "harness_override": "openai-agents",
    }
    response = await client.post("/v1/sessions", json=metadata)
    assert response.status_code == 201, response.text
    session = response.json()
    from omnigent.runtime import get_conversation_store

    stored = get_conversation_store().get_conversation(session["id"])
    assert stored is not None
    assert stored.labels["review_context"] == "prototype"
    for field in (
        "reasoning_effort",
        "model_override",
        "cost_control_mode_override",
        "subagent_routing_override",
        "harness_override",
    ):
        assert getattr(stored, field) == metadata[field]
