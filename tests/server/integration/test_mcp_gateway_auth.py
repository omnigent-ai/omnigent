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


@pytest.mark.parametrize("route", ["general", "legacy", "ordinary"])
@pytest.mark.parametrize("phase", ["tool_call", "tool_result"])
async def test_editor_turn_uses_editor_policy_and_runner_credential(
    auth_app, auth_client, monkeypatch, route, phase
):
    import httpx
    from mcp.types import CallToolResult, TextContent

    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runtime import get_conversation_store
    from omnigent.server.routes import sessions as sessions_mod
    from omnigent.server.routes._sessions.common import _TURN_ACTOR_LABEL
    from tests.server.integration.test_session_agent_owner import _share_editor

    owner, editor = "alice@example.test", "bob@example.test"
    expression = (
        f'event.type == "{phase}" && event.context.actor.run_as != "{owner}" '
        '? {"result": "DENY", "reason": "owner-only tool policy"} : {"result": "ALLOW"}'
    )
    agent = await create_test_agent(
        auth_client,
        name="policy-attribution",
        user=owner,
        guardrails={
            "policies": {
                "owner_only": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.policies.builtins.cel.cel_policy",
                        "arguments": {"expression": expression},
                    },
                }
            }
        },
    )
    sid = agent["_session_id"]
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
    backend = AsyncMock()
    backend.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text="owner-only-result")]
    )
    auth_app.state.mcp_gateway_backend = backend
    attach = await auth_client.post(
        f"/v1/sessions/{sid}/agent/mcp-servers",
        headers={"X-Forwarded-Email": owner},
        json={"name": "tracker", "transport": "registry"},
    )
    assert attach.status_code == 200, attach.text
    await _share_editor(auth_client, sid, owner, editor)
    binding = "test-runner-binding-for-policy"
    store = get_conversation_store()
    store.set_runner_id(sid, token_bound_runner_id(binding))
    executions = []

    async def runner_response(request):
        if request.url.path.endswith("/mcp/execute"):
            executions.append(request)
            return httpx.Response(200, json={"result": {"output": "owner-only-result"}})
        return httpx.Response(202, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_response), base_url="http://runner.test"
    ) as runner:
        monkeypatch.setattr(sessions_mod, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", AsyncMock())
        event = await auth_client.post(
            f"/v1/sessions/{sid}/events",
            headers={"X-Forwarded-Email": editor},
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Use the tool"}],
                },
            },
        )
        assert event.status_code == 202, event.text
        assert store.get_conversation(sid).labels[_TURN_ACTOR_LABEL] == editor
        path = "/v1/mcp/tracker" if route == "general" else f"/v1/sessions/{sid}/mcp"
        name = {
            "general": "read_ticket",
            "legacy": "tracker__read_ticket",
            "ordinary": "sys_os_shell",
        }[route]
        response = await auth_client.post(
            path,
            headers={
                "X-Forwarded-Email": owner,
                "X-Omnigent-Session-Id": sid,
                RUNNER_TUNNEL_TOKEN_HEADER: binding,
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            },
        )
    assert "owner-only-result" not in response.text, response.text
    assert "owner-only tool policy" in response.text, response.text
    expected_calls = int(phase == "tool_result")
    if route == "ordinary":
        assert len(executions) == expected_calls
    else:
        assert backend.call_tool.await_count == expected_calls
        if expected_calls:
            assert backend.call_tool.await_args.args[1] == owner

    if route == "ordinary":
        return

    # A direct editor cannot use a stale label or an unbound proof as policy identity.
    store.set_labels(sid, {_TURN_ACTOR_LABEL: owner})
    for proof in () if route == "ordinary" else ("", "a-different-runner-token"):
        backend.call_tool.reset_mock()
        executions.clear()
        direct = await auth_client.post(
            path,
            headers={
                "X-Forwarded-Email": editor,
                "X-Omnigent-Session-Id": sid,
                RUNNER_TUNNEL_TOKEN_HEADER: proof,
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": {},
                    "actor": {"run_as": owner},
                },
            },
        )
        assert "owner-only-result" not in direct.text, direct.text
        assert "owner-only tool policy" in direct.text, direct.text
        if route != "ordinary" and expected_calls:
            assert backend.call_tool.await_args.args[1] == editor


@pytest.mark.parametrize("selected", [False, True])
async def test_template_env_expansion_is_snapshotted_but_uploads_stay_literal(
    auth_app, auth_client, tmp_path, monkeypatch, selected
):
    import json

    import yaml

    from omnigent.runtime import get_agent_store, get_conversation_store
    from omnigent.server.routes._sessions.helpers import _load_agent_spec_for_session
    from omnigent.server.routes.session_mcp_servers import _tar_gz_dir
    from omnigent.spec import extract_safe, load
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from tests.server.helpers import build_agent_bundle

    monkeypatch.setenv("MCP_TEMPLATE_TEST_TOKEN", "synthetic-template-token")
    root = tmp_path / "template"
    extract_safe(build_agent_bundle("env-template"), root)
    mcp = root / "tools" / "mcp"
    mcp.mkdir(parents=True)
    (mcp / "existing.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "existing",
                "transport": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${MCP_TEMPLATE_TEST_TOKEN}"},
            }
        )
    )
    child = root / "agents" / "nested"
    extract_safe(build_agent_bundle("nested"), child)
    child_mcp = child / "tools" / "mcp"
    child_mcp.mkdir(parents=True)
    (child_mcp / "existing.yaml").write_bytes((mcp / "existing.yaml").read_bytes())
    bundle = _tar_gz_dir(root)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    artifacts.put("env-template/bundle", bundle)
    agents = get_agent_store()
    template = agents.create(
        "1234567890abcdef1234567890abcdef", "env-template", "env-template/bundle"
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
    selection = ["tracker"] if selected else []
    for trusted in (True, False):
        if trusted:
            response = await auth_client.post(
                "/v1/sessions",
                headers=headers,
                json={"agent_id": template.id, "mcp_registry_services": selection},
            )
        else:
            response = await auth_client.post(
                "/v1/sessions",
                headers=headers,
                data={"metadata": json.dumps({"mcp_registry_services": selection})},
                files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            )
        assert response.status_code == 201, response.text
        sid = response.json().get("id") or response.json()["session_id"]
        conv = get_conversation_store().get_conversation(sid)
        spec = _load_agent_spec_for_session(conv, agents)
        expected = (
            "Bearer synthetic-template-token" if trusted else "Bearer ${MCP_TEMPLATE_TEST_TOKEN}"
        )
        assert (
            next(c for c in spec.mcp_servers if c.name == "existing").headers["Authorization"]
            == expected
        )
        assert spec.sub_agents[0].mcp_servers[0].headers["Authorization"] == expected
        if selected or not trusted:
            copied = artifacts.get(agents.get(conv.agent_id).bundle_location)
            runner_spec = load(copied, dest=tmp_path / f"runner-{trusted}", expand_env=False)
            assert (
                next(c for c in runner_spec.mcp_servers if c.name == "existing").headers[
                    "Authorization"
                ]
                == expected
            )
            assert runner_spec.sub_agents[0].mcp_servers[0].headers["Authorization"] == expected
    assert artifacts.get(template.bundle_location) == bundle
