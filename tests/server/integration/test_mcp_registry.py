"""A connected service runs through the session gateway and existing policy path."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from tests.server.helpers import create_test_session

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
