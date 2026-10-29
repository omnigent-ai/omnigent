"""Integration tests for the MCP proxy endpoint ``POST /v1/sessions/{id}/mcp``.

Covers JSON-RPC validation, method routing, and error paths that do
not require a live runner connection.  The MCP endpoint proxies
JSON-RPC requests to the runner's MCP servers; these tests exercise
the validation layer, requester transport, and error handling on the AP server side.
Requester cases authenticate against the real route and capture the forwarded runner request.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
which wires a real ``RunnerRouter`` with an empty tunnel registry,
so the endpoint is reachable and runner-dependent methods return the
"no runner bound" application error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp.types import CallToolResult, TextContent

from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.mcp_execution_registry import (
    MCP_OPERATION_ID_PARAM,
    McpExecutionConflict,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runtime.requester import (
    REQUESTER_FIELD,
    REQUESTER_HEADER,
    ExecutionRequester,
    InputRequester,
    ToolRequester,
    current_tool_requester,
)
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from omnigent.server.routes import sessions as sessions_mod
from omnigent.server.routes._sessions.common import _pending_policy_ask_writes
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent, create_test_session
from tests.server.integration.test_accounts_auth_e2e import _build_app, _cookie_header, _login
from tests.server.integration.test_mcp_gateway import policy
from tests.server.integration.test_session_agent_owner import _share_editor

pytestmark = pytest.mark.asyncio


# ── Helpers ───────────────────────────────────────────────


def _jsonrpc(
    method: str,
    rpc_id: int = 1,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request envelope.

    :param method: JSON-RPC method name, e.g. ``"tools/list"``.
    :param rpc_id: Request id echoed in the response, e.g. ``1``.
    :param params: Optional method parameters.
    :returns: A dict ready to POST as JSON.
    """
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


async def _post_mcp(
    client: httpx.AsyncClient,
    session_id: str,
    body: Any,
) -> httpx.Response:
    """POST to the MCP proxy endpoint with a JSON body.

    :param client: The test HTTP client.
    :param session_id: Target session id.
    :param body: JSON-serialisable request body.
    :returns: The HTTP response.
    """
    return await client.post(f"/v1/sessions/{session_id}/mcp", json=body)


async def _post_mcp_raw(
    client: httpx.AsyncClient,
    session_id: str,
    raw: str | bytes,
    content_type: str = "application/json",
) -> httpx.Response:
    """POST raw bytes to the MCP proxy endpoint.

    :param client: The test HTTP client.
    :param session_id: Target session id.
    :param raw: Raw body content.
    :param content_type: Content-Type header value.
    :returns: The HTTP response.
    """
    data = raw if isinstance(raw, bytes) else raw.encode()
    return await client.post(
        f"/v1/sessions/{session_id}/mcp",
        content=data,
        headers={"Content-Type": content_type},
    )


# ── Tests: JSON-RPC validation layer ─────────────────────


async def test_mcp_invalid_json_returns_parse_error(
    client: httpx.AsyncClient,
) -> None:
    """Malformed JSON body returns a JSON-RPC parse error (-32700)."""
    session = await create_test_session(client, name="mcp-bad-json")
    resp = await _post_mcp_raw(client, session["id"], "not valid json{{{")
    assert resp.status_code == 200  # JSON-RPC errors travel in the body
    payload = resp.json()
    assert payload["error"]["code"] == -32700
    assert "Parse error" in payload["error"]["message"]


async def test_mcp_non_object_body_returns_invalid_request(
    client: httpx.AsyncClient,
) -> None:
    """A JSON array body returns a JSON-RPC invalid request error (-32600)."""
    session = await create_test_session(client, name="mcp-non-object")
    resp = await _post_mcp(client, session["id"], [1, 2, 3])
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["code"] == -32600
    assert "expected JSON object" in payload["error"]["message"]


# ── Tests: method dispatch ────────────────────────────────


async def test_mcp_initialize_returns_capabilities(
    client: httpx.AsyncClient,
) -> None:
    """The ``initialize`` method returns protocol version and capabilities."""
    session = await create_test_session(client, name="mcp-init")
    resp = await _post_mcp(client, session["id"], _jsonrpc("initialize"))
    assert resp.status_code == 200
    payload = resp.json()
    assert "result" in payload
    result = payload["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "omnigent-mcp-proxy"
    # Echoes the request id.
    assert payload["id"] == 1


async def test_mcp_initialize_echoes_rpc_id(
    client: httpx.AsyncClient,
) -> None:
    """The response echoes the caller's JSON-RPC id."""
    session = await create_test_session(client, name="mcp-init-id")
    resp = await _post_mcp(client, session["id"], _jsonrpc("initialize", rpc_id=99))
    assert resp.status_code == 200
    assert resp.json()["id"] == 99


async def test_mcp_unknown_method_returns_method_not_found(
    client: httpx.AsyncClient,
) -> None:
    """An unknown JSON-RPC method returns -32601 (method not found)."""
    session = await create_test_session(client, name="mcp-unknown")
    resp = await _post_mcp(client, session["id"], _jsonrpc("resources/list", rpc_id=42))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 42
    assert payload["error"]["code"] == -32601
    assert "Method not found" in payload["error"]["message"]
    assert "resources/list" in payload["error"]["message"]


# ── Tests: tools/list without runner ──────────────────────


async def test_mcp_tools_list_no_runner_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """``tools/list`` with no runner bound returns a -32000 application error."""
    session = await create_test_session(client, name="mcp-list-no-runner")
    resp = await _post_mcp(client, session["id"], _jsonrpc("tools/list", rpc_id=5))
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 5
    assert payload["error"]["code"] == -32000
    assert "No runner bound" in payload["error"]["message"]


# ── Tests: tools/call validation ──────────────────────────


async def test_mcp_tools_call_missing_name_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """``tools/call`` without a ``name`` param returns an application error."""
    session = await create_test_session(client, name="mcp-call-no-name")
    resp = await _post_mcp(
        client,
        session["id"],
        _jsonrpc("tools/call", rpc_id=9, params={}),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["error"]["code"] == -32000
    assert "Missing tool name" in payload["error"]["message"]


# ── Tests: nonexistent session ────────────────────────────


async def test_mcp_nonexistent_session_returns_error(
    client: httpx.AsyncClient,
) -> None:
    """MCP on a nonexistent session returns an error.

    The session lookup (``_require_access``) runs before the JSON-RPC
    dispatch.  Without a permission store the access check is a no-op,
    so the request reaches the JSON-RPC layer.  ``tools/list`` then
    fails with "No runner bound" because the tunnel registry has no
    binding for the nonexistent session.
    """
    resp = await _post_mcp(
        client,
        "3628cfc9b00f747da23485e2534458a6",
        _jsonrpc("tools/list", rpc_id=10),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 10
    assert payload["error"]["code"] == -32000
    assert "No runner bound" in payload["error"]["message"]


@pytest.mark.parametrize(
    ("transport", "expected_status"),
    [
        ("legacy", 200),
        ("direct_owner", 200),
        ("direct_editor", 200),
        ("bound_runner", 200),
        ("unknown_origin", 200),
        ("human_header", 403),
        ("wrong_runner", 403),
        ("malformed_header", 403),
    ],
)
async def test_mcp_tools_call_protects_requester_transport(
    auth_client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    expected_status: int,
) -> None:
    """Direct callers use auth; only the bound runner may supply an envelope."""
    owner = "bryan@example.test"
    editor = "pat@example.test"
    agent = await create_test_agent(auth_client, name="mcp-requester", user=owner)
    session_id = agent["_session_id"]
    await _share_editor(auth_client, session_id, owner, editor)
    binding_token = "mcp-requester-test-runner"
    assert SqlAlchemyConversationStore(db_uri).set_runner_id(
        session_id, token_bound_runner_id(binding_token)
    )
    requester = ExecutionRequester(
        InputRequester(user_id="pat@example.test", account_generation="pat-v1")
        if transport != "unknown_origin"
        else InputRequester()
    ).snapshot()
    forged_owner = ExecutionRequester(
        InputRequester(user_id=owner, account_generation="bryan-v1")
    ).snapshot()
    direct = transport in {"direct_owner", "direct_editor"}
    caller = editor if transport == "direct_editor" else owner
    headers = {"X-Forwarded-Email": caller}
    if transport != "legacy" and not direct:
        headers[REQUESTER_HEADER] = requester.model_dump_json()
    if not direct and transport != "human_header":
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = (
            "another-runner" if transport == "wrong_runner" else binding_token
        )
    if transport == "malformed_header":
        headers[REQUESTER_HEADER] = '{"execution_id":"invalid"}'

    dispatched: list[dict[str, Any]] = []

    def execute(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{session_id}/mcp/execute"
        dispatched.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"output": "ok"}})

    arguments = {"path": "README.md", REQUESTER_FIELD: forged_owner.model_dump()}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(execute), base_url="http://runner.test"
    ) as runner:

        async def get_runner(*args):
            return runner

        monkeypatch.setattr(sessions_mod, "_get_runner_client", get_runner)
        response = await auth_client.post(
            f"/v1/sessions/{session_id}/mcp",
            headers=headers,
            json={
                **_jsonrpc(
                    "tools/call",
                    params={
                        "name": "sys_os_read",
                        "arguments": arguments,
                        REQUESTER_FIELD: forged_owner.model_dump(),
                    },
                ),
                REQUESTER_FIELD: forged_owner.model_dump(),
            },
        )
        if expected_status == 200:
            # Exercise the actual proxy header writer through server decoding too.
            saved_headers = auth_client.headers.copy()
            token = current_tool_requester.set(
                requester if transport != "legacy" and not direct else None
            )
            try:
                auth_client.headers.update(
                    {key: value for key, value in headers.items() if key != REQUESTER_HEADER}
                )
                proxy = ProxyMcpManager(session_id=session_id, ap_client=auth_client)
                assert await proxy.call_tool(None, "sys_os_read", arguments) == "ok"
            finally:
                auth_client.headers = saved_headers
                current_tool_requester.reset(token)
    assert response.status_code == expected_status, response.text
    if expected_status != 200:
        assert not dispatched
        assert (
            "Invalid requester metadata"
            if transport == "malformed_header"
            else "Requester requires the bound runner"
        ) in response.text
        return
    assert "error" not in response.json(), response.text
    assert len(dispatched) == 2
    for body in dispatched:
        params = body["params"]
        assert params["arguments"] == arguments
        assert REQUESTER_FIELD not in body
        if transport == "legacy":
            assert REQUESTER_FIELD not in params
        elif direct:
            captured = ToolRequester.model_validate(params[REQUESTER_FIELD])
            assert captured.origin.user_id == caller
            assert captured.origin.account_generation is None
            assert captured not in (requester, forged_owner)
        else:
            assert params[REQUESTER_FIELD] == requester.model_dump()


@pytest.mark.parametrize("credential", ["cookie", "bearer"])
@pytest.mark.parametrize("retained", [False, True])
async def test_direct_mcp_approval_preserves_authenticated_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential: str, retained: bool
) -> None:
    """A direct approval retry restores Pat's origin, never Bryan's session ownership."""
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.passwords import hash_password

    app = _build_app(tmp_path, monkeypatch, init_admin_password=None)
    accounts = SqlAlchemyAccountStore(f"sqlite:///{tmp_path}/test.db")
    for user in ("bryan", "pat"):
        accounts.create_user_with_password(user, hash_password("test-password-123"))
    evaluations = policy(monkeypatch, call_action=PolicyAction.ASK)
    dispatched: list[dict[str, Any]] = []

    def execute(request: httpx.Request) -> httpx.Response:
        dispatched.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"output": "ok"}})

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(execute), base_url="http://runner.test"
        ) as runner,
    ):

        async def credentials(user):
            cookies = await _login(client, user, "test-password-123")
            client.cookies.clear()
            return (
                _cookie_header(cookies)
                if credential == "cookie"
                else {"Authorization": f"Bearer {cookies['ap_session']}"}
            )

        headers = {user: await credentials(user) for user in ("bryan", "pat")}
        client.headers.update(headers["bryan"])
        agent = await create_test_agent(client, name="direct-mcp-approval")
        client.headers.clear()
        session_id = agent["_session_id"]
        shared = await client.put(
            f"/v1/sessions/{session_id}/permissions",
            headers=headers["bryan"],
            json={"user_id": "pat", "level": 2},
        )
        assert shared.status_code in (200, 204), shared.text

        async def get_runner(*args):
            return runner

        monkeypatch.setattr(sessions_mod, "_get_runner_client", get_runner)

        async def call(user, params):
            response = await client.post(
                f"/v1/sessions/{session_id}/mcp",
                headers=headers[user],
                json=_jsonrpc("tools/call", params=params),
            )
            assert response.status_code == 200, response.text
            return response.json()

        params = {"name": "sys_os_read", "arguments": {"path": "README.md"}}
        if retained:
            params[MCP_OPERATION_ID_PARAM] = "mcpop_direct_approval"
        first = await call("pat", params)
        state = json.loads(first["result"]["requestState"])
        elicitation_id = state["elicitation_id"]
        pending = _pending_policy_ask_writes[elicitation_id]
        assert pending.mcp_call_identity is not None
        saved = pending.mcp_call_identity[4]
        assert saved is not None
        assert saved.origin.user_id == "pat"
        assert saved.origin.account_generation == accounts.get_user("pat").account_generation
        assert not dispatched
        forged = ExecutionRequester(InputRequester(user_id="bryan")).snapshot().model_dump()
        # Neither the retry state nor tool arguments are an authority transport.
        state[REQUESTER_FIELD] = forged
        retry = {
            **params,
            REQUESTER_FIELD: forged,
            "requestState": json.dumps(state),
            "inputResponses": {elicitation_id: {"action": "accept"}},
        }
        try:
            denied = await call("bryan", retry)
            assert denied["error"]["message"] == "Approval does not match this call"
            assert not dispatched
            assert _pending_policy_ask_writes[elicitation_id] is pending
            if retained:
                changed = await call("pat", {**retry, MCP_OPERATION_ID_PARAM: "mcpop_changed"})
                assert changed["error"]["message"] == "Approval does not match this call"
                assert not dispatched
            approved = await call("pat", retry)
            assert "error" not in approved, approved
            assert len(dispatched) == 1
            assert dispatched[0]["params"][REQUESTER_FIELD] == saved.model_dump()
            assert dispatched[0]["params"]["arguments"] == {"ticket_id": "approved"}
            assert elicitation_id not in _pending_policy_ask_writes
            assert all(ctx.actor == {"run_as": "pat"} for ctx in evaluations)
            assert evaluations[-1].phase == Phase.TOOL_RESULT
        finally:
            _pending_policy_ask_writes.pop(elicitation_id, None)


async def test_direct_mcp_retained_operation_keeps_requester(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated HTTP calls attach to one execution; changing user conflicts."""
    owner, editor = "bryan@example.test", "pat@example.test"
    agent = await create_test_agent(auth_client, name="direct-mcp-retained", user=owner)
    session_id = agent["_session_id"]
    await _share_editor(auth_client, session_id, owner, editor)
    registry = McpExecutionRegistry()
    operation_id = "mcpop_direct_retained"
    registry.retain_operation(session_id, operation_id)
    dispatched: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []

    async def execute(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        dispatched.append(body)

        async def run():
            executed.append(body)
            return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

        operation = body["_omnigent_operation"]
        try:
            result = await registry.execute(
                session_id=session_id,
                operation_id=operation["id"],
                step=operation["step"],
                params={"method": body["method"], "params": body["params"]},
                run=run,
            )
            return httpx.Response(result.status_code, json=result.content)
        except McpExecutionConflict as exc:
            return httpx.Response(200, json={"error": {"code": -32000, "message": str(exc)}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(execute), base_url="http://runner.test"
    ) as runner:

        async def get_runner(*args):
            return runner

        monkeypatch.setattr(sessions_mod, "_get_runner_client", get_runner)
        try:
            for caller in (editor, editor, owner):
                response = await auth_client.post(
                    f"/v1/sessions/{session_id}/mcp",
                    headers={"X-Forwarded-Email": caller},
                    json=_jsonrpc(
                        "tools/call",
                        params={
                            "name": "sys_os_read",
                            "arguments": {"path": "README.md"},
                            MCP_OPERATION_ID_PARAM: operation_id,
                        },
                    ),
                )
                assert response.status_code == 200, response.text
                if caller == editor:
                    assert "error" not in response.json(), response.text
                else:
                    assert "parameters changed" in response.json()["error"]["message"]
            assert len(dispatched) == 3
            assert len(executed) == 1
            assert dispatched[0] == dispatched[1]
            captured = ToolRequester.model_validate(dispatched[0]["params"][REQUESTER_FIELD])
            assert captured.origin.user_id == editor
            assert dispatched[2]["params"][REQUESTER_FIELD]["origin"]["user_id"] == owner
        finally:
            registry.release_operation(session_id, operation_id)
            await registry.cancel_session(session_id)


@pytest.mark.parametrize("tool", ["sys_session_create", "sys_session_message", "sys_session_send"])
async def test_managed_mcp_orchestration_names_never_dispatch_local_tools(
    auth_client: httpx.AsyncClient,
    auth_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    """Names matching orchestration tools stay on the selected managed backend."""
    owner = "bryan@example.test"
    registry = McpRegistry(
        McpRegistryConfig(
            services=[
                McpService(
                    id="tracker",
                    title="Tracker",
                    url="https://example.test/mcp",
                    auth="none",
                    tools=[tool],
                )
            ]
        ),
        None,
    )
    auth_app.state.mcp_registry = registry
    calls: list[tuple[str, str, str, dict[str, Any]]] = []

    async def execute(service_id, user_id, app_state, *, tool, arguments):
        calls.append((service_id, user_id, tool, arguments))
        return CallToolResult(content=[TextContent(type="text", text="registry result")])

    async def reject_local_dispatch(*args):
        raise AssertionError("Managed MCP must not dispatch runner-local tools")

    monkeypatch.setattr(registry, "execute", execute)
    monkeypatch.setattr(sessions_mod, "_get_runner_client", reject_local_dispatch)
    agent = await create_test_agent(auth_client, name="managed-mcp-requester", user=owner)
    session_id = agent["_session_id"]
    headers = {"X-Forwarded-Email": owner}
    attached = await auth_client.post(
        f"/v1/sessions/{session_id}/agent/mcp-servers",
        headers=headers,
        json={"name": "tracker", "transport": "registry"},
    )
    assert attached.status_code == 200, attached.text
    requester = ExecutionRequester(InputRequester(user_id="pat@example.test")).snapshot()
    response = await auth_client.post(
        "/v1/mcp/tracker",
        headers={
            **headers,
            "X-Omnigent-Session-Id": session_id,
            REQUESTER_HEADER: requester.model_dump_json(),
        },
        json=_jsonrpc(
            "tools/call",
            params={
                "name": tool,
                "arguments": {"task": "example"},
                REQUESTER_FIELD: requester.model_dump(),
            },
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["content"] == [{"type": "text", "text": "registry result"}]
    assert calls == [("tracker", owner, tool, {"task": "example"})]
