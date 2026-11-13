"""Selected registry services use the general gateway from SDK and native runners."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.runner import pending_approvals
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.spec.types import AgentSpec, MCPServerConfig

pytestmark = pytest.mark.asyncio


def spec():
    return AgentSpec(
        spec_version=1,
        name="gateway",
        mcp_servers=[MCPServerConfig(name="tracker", transport="registry")],
    )


async def test_registry_discovery_and_fresh_native_manager_use_service_route():
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/mcp/tracker"
        assert request.headers["X-Omnigent-Session-Id"] == "session-test"
        if body["method"] == "tools/list":
            result = {"tools": [{"name": "read_ticket", "inputSchema": {"type": "object"}}]}
        else:
            assert body["params"]["name"] == "read_ticket"
            result = {"content": [{"type": "text", "text": "ticket"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        manager = ProxyMcpManager("session-test", client)
        discovered = await manager.schemas_for(spec())
        assert discovered.tool_names == {"tracker__read_ticket"}
        assert await manager.call_tool(None, "tracker__read_ticket", {}) == "ticket"
        fresh = ProxyMcpManager("session-test", client)
        assert await fresh.call_tool(spec(), "tracker__read_ticket", {}) == "ticket"
    assert len(calls) == 3


async def test_registry_transport_failure_is_not_replayed():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadError("connection closed", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="not retried"):
            await ProxyMcpManager("session-test", client).call_tool(
                spec(), "tracker__read_ticket", {}
            )
    assert len(calls) == 1


async def test_registry_approval_retry_keeps_route_and_context(monkeypatch):
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/mcp/tracker"
        assert request.headers["X-Omnigent-Session-Id"] == "session-test"
        assert body["params"]["name"] == "read_ticket"
        if len(calls) == 1:
            result = {
                "resultType": "input_required",
                "requestState": "opaque",
                "inputRequests": {
                    "approval": {"method": "elicitation/create", "params": {"message": "Approve"}}
                },
            }
        else:
            assert body["params"]["requestState"] == "opaque"
            result = {"content": [{"type": "text", "text": "approved ticket"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(
        pending_approvals,
        "wait_for_user_verdict",
        AsyncMock(return_value=pending_approvals.Verdict(approved=True)),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        assert (
            await ProxyMcpManager("session-test", client).call_tool(
                spec(), "tracker__read_ticket", {}
            )
            == "approved ticket"
        )
    assert len(calls) == 2
