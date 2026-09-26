"""Per-service gateway authorization and policies wrap both built-in and BYO backends."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from omnigent.policies.types import PolicyResult
from omnigent.server.routes._sessions import orchestration
from omnigent.spec.types import Phase, PolicyAction
from tests.server.helpers import create_test_session
from tests.server.integration.test_mcp_registry import app as app
from tests.server.integration.test_mcp_registry import launch_registry as launch_registry
from tests.server.routes.test_sessions_mcp_proxy_policy_retry import _FixedPolicyEngine

pytestmark = pytest.mark.asyncio


@pytest.fixture
def backend(app, launch_registry, monkeypatch, request):
    tools = [
        Tool(name=name, inputSchema={"type": "object"})
        for name in ["read_ticket", "delete_ticket"]
    ]
    result = CallToolResult(content=[TextContent(type="text", text="private ticket")])
    backend = AsyncMock()
    backend.list_tools.return_value = tools
    backend.call_tool.return_value = result
    if request.param == "custom":
        app.state.mcp_gateway_backend = backend
    else:

        async def execute(service, user, state, *, tool=None, arguments=None):
            entry = launch_registry.service(service, user)
            if tool is None:
                return await backend.list_tools(entry, user)
            return await backend.call_tool(entry, user, tool, arguments)

        monkeypatch.setattr(launch_registry, "execute", execute)
    return backend


async def selected_session(client):
    session = await create_test_session(client, name="gateway-session")
    response = await client.post(
        f"/v1/sessions/{session['id']}/agent/mcp-servers",
        json={"name": "tracker", "transport": "registry"},
    )
    assert response.status_code == 200, response.text
    return session["id"]


async def rpc(client, session_id, method="tools/call", params=None, service="tracker"):
    return await client.post(
        f"/v1/mcp/{service}",
        headers={"X-Omnigent-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params if params is not None else {"name": "read_ticket", "arguments": {}},
        },
    )


def policy(monkeypatch, call_action=PolicyAction.ALLOW, result_action=PolicyAction.ALLOW):
    seen = []
    engine = _FixedPolicyEngine(PolicyResult(action=PolicyAction.ALLOW))

    async def evaluate(session_id, spec, store, conv, ctx):
        seen.append(ctx)
        if ctx.phase == Phase.TOOL_CALL:
            return engine, PolicyResult(
                action=call_action, reason="review ticket", data={"ticket_id": "approved"}
            )
        return engine, PolicyResult(
            action=result_action, reason="private data", data="redacted ticket"
        )

    monkeypatch.setattr(orchestration, "_evaluate_policy_with_fresh_engine", evaluate)
    return seen


@pytest.mark.parametrize("backend", ["default", "custom"], indirect=True)
async def test_gateway_discovery_and_request_result_transforms(client, backend, monkeypatch):
    session_id = await selected_session(client)
    initialized = await rpc(client, session_id, "initialize", {})
    assert initialized.json()["result"]["serverInfo"]["name"] == "tracker"
    listed = await rpc(client, session_id, "tools/list", {})
    assert [t["name"] for t in listed.json()["result"]["tools"]] == ["read_ticket"]
    seen = policy(monkeypatch)
    called = await rpc(client, session_id)
    assert called.json()["result"]["content"][0]["text"] == "redacted ticket"
    assert backend.call_tool.await_args.args[2:] == ("read_ticket", {"ticket_id": "approved"})
    assert [ctx.phase for ctx in seen] == [Phase.TOOL_CALL, Phase.TOOL_RESULT]
    assert all(ctx.tool_name == "tracker__read_ticket" for ctx in seen)
    assert seen[1].request_data["arguments"] == {"ticket_id": "approved"}


@pytest.mark.parametrize("backend", ["default", "custom"], indirect=True)
async def test_gateway_denied_request_never_executes(client, backend, monkeypatch):
    session_id = await selected_session(client)
    policy(monkeypatch, call_action=PolicyAction.DENY)
    denied = await rpc(client, session_id)
    assert "Denied by policy" in denied.json()["error"]["message"]
    backend.call_tool.assert_not_awaited()


@pytest.mark.parametrize("backend", ["default", "custom"], indirect=True)
@pytest.mark.parametrize("action", [PolicyAction.DENY, PolicyAction.ASK])
async def test_gateway_withholds_restricted_result(client, backend, monkeypatch, action):
    session_id = await selected_session(client)
    policy(monkeypatch, result_action=action)
    response = await rpc(client, session_id)
    assert "private ticket" not in response.text
    assert "redacted ticket" not in response.text
    assert "Result" in response.json()["result"]["content"][0]["text"]
    backend.call_tool.assert_awaited_once()


@pytest.mark.parametrize("backend", ["default", "custom"], indirect=True)
@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_gateway_approval_preserves_transformed_arguments(
    client, backend, monkeypatch, decision
):
    session_id = await selected_session(client)
    policy(monkeypatch, call_action=PolicyAction.ASK)
    initial = (await rpc(client, session_id)).json()["result"]
    assert initial["resultType"] == "input_required"
    backend.call_tool.assert_not_awaited()
    eid = next(iter(initial["inputRequests"]))
    try:
        response = await rpc(
            client,
            session_id,
            params={
                "name": "read_ticket",
                "arguments": {},
                "requestState": initial["requestState"],
                "inputResponses": {eid: {"action": decision}},
            },
        )
        if decision == "accept":
            assert response.json()["result"]["content"][0]["text"] == "redacted ticket"
            backend.call_tool.assert_awaited_once()
            assert backend.call_tool.await_args.args[3] == {"ticket_id": "approved"}
        else:
            assert "denied by user" in response.json()["error"]["message"]
            backend.call_tool.assert_not_awaited()
    finally:
        orchestration._pending_policy_ask_writes.pop(eid, None)


@pytest.mark.parametrize("backend", ["custom"], indirect=True)
async def test_gateway_requires_context_selection_and_allowed_tool(client, backend):
    session_id = await selected_session(client)
    missing = await client.post(
        "/v1/mcp/tracker", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    )
    assert missing.status_code == 422
    unselected = await rpc(client, session_id, service="other")
    assert unselected.status_code == 403
    denied = await rpc(client, session_id, params={"name": "delete_ticket"})
    assert "not allowed" in denied.json()["error"]["message"]
    backend.call_tool.assert_not_awaited()
    backend.list_tools.assert_not_awaited()


@pytest.mark.parametrize("backend", ["custom"], indirect=True)
async def test_gateway_uses_trusted_turn_actor(client, backend):
    from omnigent.runtime import get_conversation_store
    from omnigent.server.routes._sessions.common import _TURN_ACTOR_LABEL

    session_id = await selected_session(client)
    store = get_conversation_store()
    store.set_labels(session_id, {_TURN_ACTOR_LABEL: "actor@example.test"})
    called = await rpc(client, session_id)
    assert "result" in called.json(), called.text
    assert backend.call_tool.await_args.args[1] == "actor@example.test"
