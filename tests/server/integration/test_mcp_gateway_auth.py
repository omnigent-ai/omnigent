"""The general gateway retains authenticated session permissions."""

from unittest.mock import AsyncMock

import pytest
from mcp.types import Tool

from omnigent.server.auth import delegated_path_allowed
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from tests.server.helpers import create_test_agent
from tests.server.integration.test_session_agent_owner import auth_app as auth_app
from tests.server.integration.test_session_agent_owner import auth_client as auth_client


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
