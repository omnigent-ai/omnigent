"""The general gateway retains authenticated session permissions."""

from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from omnigent.server.auth import delegated_path_allowed
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from tests.server.helpers import create_test_agent
from tests.server.integration.test_session_agent_owner import auth_app as auth_app
from tests.server.integration.test_session_agent_owner import (
    auth_client as auth_client,
)  # pytest fixture re-export


@pytest.fixture(autouse=True)
def multi_user(monkeypatch):
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)


@pytest.mark.asyncio
async def test_gateway_checks_session_permissions_before_backend(
    auth_app, auth_client, monkeypatch
):
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
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
    auth_app.state.mcp_registry = registry
    backend = AsyncMock()
    backend.list_tools.return_value = [Tool(name="read_ticket", inputSchema={"type": "object"})]
    auth_app.state.mcp_gateway_backend = backend
    agent = await create_test_agent(auth_client, name="gateway-owner", user="alice@example.test")
    sid = agent["_session_id"]
    attached = await auth_client.post(
        f"/v1/sessions/{sid}/agent/mcp-servers",
        headers={"X-Forwarded-Email": "alice@example.test"},
        json={"name": "tracker", "transport": "registry"},
    )
    assert attached.status_code == 200, attached.text
    for identity, status in [(None, 401), ("bob@example.test", 404), ("alice@example.test", 200)]:
        headers = {"X-Omnigent-Session-Id": sid}
        if identity:
            headers["X-Forwarded-Email"] = identity
        result = await auth_client.post(
            "/v1/mcp/tracker",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert result.status_code == status, result.text
        if status != 200:
            backend.list_tools.assert_not_awaited()
    backend.list_tools.assert_awaited_once()
    assert backend.list_tools.await_args.args[1] == "alice@example.test"


def test_delegated_runner_credentials_allow_gateway_only():
    assert delegated_path_allowed("/v1/mcp/tracker")
    assert not delegated_path_allowed("/v1/mcp-registry/services/tracker/connection")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("entitled", [False, True])
async def test_shared_editor_uses_own_registry_identity(
    auth_app, auth_client, monkeypatch, legacy, entitled
):
    from mcp.types import CallToolResult, TextContent

    from omnigent.server.routes._sessions.common import _TURN_ACTOR_LABEL
    from tests.server.integration.test_session_agent_owner import _share_editor

    alice, bob = "alice@example.test", "bob@example.test"
    service = McpService(
        id="tracker",
        title="Tracker",
        url="https://example.test/mcp",
        auth="none",
        tools=["read_ticket"],
        allowed_users=[alice, bob] if entitled else [alice],
    )
    auth_app.state.mcp_registry = McpRegistry(McpRegistryConfig(services=[service]), None)
    backend = AsyncMock()
    backend.list_tools.return_value = [Tool(name="read_ticket", inputSchema={"type": "object"})]
    backend.call_tool.return_value = CallToolResult(content=[TextContent(type="text", text="ok")])
    auth_app.state.mcp_gateway_backend = backend
    agent = await create_test_agent(auth_client, name="shared-mcp", user=alice)
    sid = agent["_session_id"]
    attached = await auth_client.post(
        f"/v1/sessions/{sid}/agent/mcp-servers",
        headers={"X-Forwarded-Email": alice},
        json={"name": "tracker", "transport": "registry"},
    )
    assert attached.status_code == 200, attached.text
    await _share_editor(auth_client, sid, alice, bob)
    from omnigent.runtime import get_conversation_store

    get_conversation_store().set_labels(sid, {_TURN_ACTOR_LABEL: alice})
    path = f"/v1/sessions/{sid}/mcp" if legacy else "/v1/mcp/tracker"
    headers = {"X-Forwarded-Email": bob, "X-Omnigent-Session-Id": sid}
    listed = await auth_client.post(
        path, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    called = await auth_client.post(
        path,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "tracker__read_ticket" if legacy else "read_ticket",
                "arguments": {},
            },
        },
    )
    if entitled:
        assert listed.status_code == called.status_code == 200
        assert "result" in called.json(), called.text
        assert backend.list_tools.await_args.args[1] == bob
        assert backend.call_tool.await_args.args[1] == bob
    else:
        backend.list_tools.assert_not_awaited()
        backend.call_tool.assert_not_awaited()
        assert called.status_code == 403 or "unavailable" in called.json()["error"]["message"]


@pytest.mark.asyncio
async def test_registry_selection_preserves_trusted_template_provenance(
    auth_app, auth_client, tmp_path
):
    from tests.server.test_bundles import _single_file_yaml_bundle

    bundle = _single_file_yaml_bundle(
        "name: callable-template\nprompt: Use the square root tool.\n"
        "executor:\n  harness: claude-sdk\ntools:\n"
        "  square_root:\n    type: function\n    description: Square root\n"
        "    callable: math.sqrt\n    parameters:\n      type: object\n"
        "      properties:\n        x:\n          type: number\n"
    )
    from omnigent.runtime import get_agent_store
    from omnigent.stores.artifact_store.local import LocalArtifactStore

    LocalArtifactStore(str(tmp_path / "artifacts")).put("trusted-template/bundle", bundle)
    agent = get_agent_store().create(
        "1234567890abcdef1234567890abcdef", "callable-template", "trusted-template/bundle"
    )
    auth_app.state.mcp_registry = McpRegistry(
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
    headers = {"X-Forwarded-Email": "alice@example.test"}
    for selection in ([], ["tracker"]):
        response = await auth_client.post(
            "/v1/sessions",
            headers=headers,
            json={
                "agent_id": agent.id,
                "mcp_registry_services": selection,
            },
        )
        assert response.status_code == 201, response.text
    # User uploads must still go through the untrusted-bundle boundary.
    upload = await auth_client.post(
        "/v1/sessions",
        headers=headers,
        data={"metadata": '{"mcp_registry_services": ["tracker"]}'},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert upload.status_code == 400, upload.text
    assert "callable" in upload.text
