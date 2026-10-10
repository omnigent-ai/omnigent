"""Unit tests for :class:`ProxyMcpManager`.

Covers:
- ``schemas_for`` short-circuit on empty ``mcp_servers``
- ``schemas_for`` happy path: JSON-RPC response → ``McpSchemasResult``
- ``inputSchema`` normalization (None, missing-properties)
- ``schemas_for`` soft errors: HTTP 500 and RPC error body → ``failures`` dict
- ``call_tool`` happy path: text content extracted from result
- ``call_tool`` isError=True → JSON error string (not raised)
- ``call_tool`` -32000 RPC error → JSON error string (soft error, not raised)
- ``call_tool`` non-32000 RPC error → raises RuntimeError
- ``call_tool`` network failure → raises RuntimeError
- ``call_tool`` request lost between runner and server (gateway 502/504,
  dropped connection) → re-sends the retained operation without replaying it
- ``call_tool`` re-send budget and reconnect window after long requests and
  approval waits
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.runner import mcp_execution_registry as mcp_execution_registry_mod
from omnigent.runner import pending_approvals
from omnigent.runner import proxy_mcp_manager as proxy_mcp_manager_mod
from omnigent.runner.mcp_execution_registry import (
    MCP_OPERATION_ID_PARAM,
    RUNNER_MCP_EXECUTION_DETACHED_CODE,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.spec.types import AgentSpec, MCPServerConfig

# ── Helpers ────────────────────────────────────────────────────────────────


@dataclass
class _Call:
    """A single captured HTTP call made through the stub transport.

    :param url: The request URL path, e.g. ``"/v1/sessions/conv_1/mcp"``.
    :param body: The parsed JSON body of the request.
    """

    url: str
    body: dict[str, Any]


class _StubTransport(httpx.AsyncBaseTransport):
    """httpx async transport backed by a list of scripted responses.

    Each call to ``handle_async_request`` pops and returns the next
    response from the queue and records the request in ``calls``.

    :param responses: Pre-scripted :class:`httpx.Response` objects, in
        the order they will be returned.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        """Create the stub transport with a list of canned responses.

        :param responses: The responses to return in order.
        """
        self._responses = list(responses)
        self.calls: list[_Call] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Return the next scripted response and record the request.

        :param request: The outgoing request.
        :returns: The next response in the queue.
        :raises IndexError: If the queue is exhausted (test setup error).
        """
        body = json.loads(request.content)
        self.calls.append(_Call(url=str(request.url), body=body))
        return self._responses.pop(0)


def _json_resp(data: dict[str, Any], status: int = 200) -> httpx.Response:
    """Build an httpx.Response with a JSON body.

    :param data: The JSON body dict.
    :param status: The HTTP status code; defaults to 200.
    :returns: A :class:`httpx.Response` with the encoded body.
    """
    return httpx.Response(
        status_code=status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(data).encode(),
    )


def _make_spec(*names: str) -> AgentSpec:
    """Build an AgentSpec with one HTTP MCPServerConfig per name.

    :param names: Server names, e.g. ``"github"``, ``"jira"``.
    :returns: :class:`AgentSpec` with ``mcp_servers`` populated.
    """
    return AgentSpec(
        spec_version=1,
        name="test-agent",
        mcp_servers=[
            MCPServerConfig(name=n, transport="http", url=f"http://mcp/{n}") for n in names
        ],
    )


def _empty_spec() -> AgentSpec:
    """Build an AgentSpec with no MCP servers.

    :returns: :class:`AgentSpec` with an empty ``mcp_servers`` list.
    """
    return AgentSpec(spec_version=1, name="test-agent")


def _make_manager(transport: _StubTransport) -> ProxyMcpManager:
    """Build a ProxyMcpManager backed by the stub transport.

    :param transport: The stub transport to use for the httpx client.
    :returns: A :class:`ProxyMcpManager` bound to session ``"conv_test"``.
    """
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    return ProxyMcpManager(session_id="conv_test", ap_client=client)


# ── schemas_for ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schemas_for_empty_spec_returns_empty_without_network() -> None:
    """``schemas_for`` must return empty result without HTTP call when spec has no MCP servers.

    Failure means the proxy hits the network (or crashes) on specs that
    declare no MCP servers — agents without MCP tools should never trigger
    MCP proxy requests.
    """
    transport = _StubTransport([])  # no responses queued — would raise if called
    manager = _make_manager(transport)

    result = await manager.schemas_for(_empty_spec())

    assert result == McpSchemasResult(schemas=[], tool_names=set(), failures={}), (
        "Empty spec must return empty McpSchemasResult without calling the proxy"
    )
    assert transport.calls == [], "No HTTP request should be sent when mcp_servers is empty"


@pytest.mark.asyncio
async def test_schemas_for_happy_path_parses_tools() -> None:
    """``schemas_for`` must parse a JSON-RPC tools/list response into McpSchemasResult.

    Failure means ProxyMcpManager's response parsing is broken — the harness
    would see no tools and never dispatch any MCP tool calls.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "github__search",
                        "description": "Search GitHub",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                    {
                        "name": "github__create_issue",
                        "description": "Create an issue",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                        },
                    },
                ]
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.tool_names == {"github__search", "github__create_issue"}, (
        "Both tool names must appear in tool_names set"
    )
    assert result.failures == {}, "No failures expected on a clean response"
    assert len(result.schemas) == 2, "One schema per tool must be returned"

    search_schema = next(s for s in result.schemas if s["name"] == "github__search")
    assert search_schema["type"] == "function"
    assert search_schema["description"] == "Search GitHub"
    assert search_schema["parameters"]["properties"]["query"] == {"type": "string"}, (
        "inputSchema.properties must be forwarded as parameters.properties"
    )

    # Verify the request body was well-formed JSON-RPC 2.0
    assert len(transport.calls) == 1, "Exactly one HTTP call should be made"
    call = transport.calls[0]
    assert call.body["method"] == "tools/list"
    assert call.body["jsonrpc"] == "2.0"
    assert "/v1/sessions/conv_test/mcp" in call.url


@pytest.mark.asyncio
async def test_schemas_for_normalizes_null_input_schema() -> None:
    """A tool with ``inputSchema: null`` must normalize to ``{type: object, properties: {}}``.

    Failure means tools without an inputSchema crash the LLM provider
    call with a missing-properties validation error.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [{"name": "gh__ping", "description": "Ping", "inputSchema": None}]
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("gh"))

    assert result.schemas[0]["parameters"] == {"type": "object", "properties": {}}, (
        "null inputSchema must normalize to object with empty properties"
    )


@pytest.mark.asyncio
async def test_schemas_for_injects_empty_properties_when_missing() -> None:
    """An object inputSchema without ``properties`` must get ``properties: {}`` injected.

    Failure means some LLM providers reject the schema (required key missing)
    for tools that accept no parameters.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "gh__noop",
                        "description": "No-op",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("gh"))

    params = result.schemas[0]["parameters"]
    assert params["type"] == "object"
    assert params["properties"] == {}, "Missing properties key must be injected as empty dict"


@pytest.mark.asyncio
async def test_schemas_for_http_error_returns_failure() -> None:
    """An HTTP 500 from the proxy must surface as a failure, not raise.

    Failure means an Omnigent server error crashes the harness instead of surfacing
    as a graceful tool-unavailable message to the LLM.
    """
    transport = _StubTransport([httpx.Response(status_code=500)])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.schemas == [], "No schemas on proxy error"
    assert result.tool_names == set(), "No tool names on proxy error"
    assert "proxy" in result.failures, "Error must surface in failures['proxy']"


@pytest.mark.asyncio
async def test_schemas_for_rpc_error_body_returns_failure() -> None:
    """A JSON-RPC error body from the proxy must surface as failures, not raise.

    Failure means RPC protocol errors (e.g. from an MCP pool miss) crash
    the harness instead of returning a graceful empty-tools result.
    """
    rpc_error = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
    )
    transport = _StubTransport([rpc_error])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.schemas == []
    assert "proxy" in result.failures
    assert "-32601" in result.failures["proxy"], (
        "Error code must appear in the failure message for diagnostics"
    )
    assert "Method not found" in result.failures["proxy"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rpc_body", "expected_message"),
    [
        ({"jsonrpc": "2.0", "id": 1, "error": []}, "non-object RPC error"),
        ({"jsonrpc": "2.0", "id": 1, "result": []}, "non-object tools/list result"),
    ],
)
async def test_schemas_for_malformed_rpc_objects_return_failure(
    rpc_body: dict[str, Any],
    expected_message: str,
) -> None:
    """Malformed JSON-RPC objects surface as schema failures rather than empty success."""
    transport = _StubTransport([_json_resp(rpc_body)])
    manager = _make_manager(transport)

    result = await manager.schemas_for(_make_spec("github"))

    assert result.schemas == []
    assert result.tool_names == set()
    assert expected_message in result.failures["proxy"]


# ── call_tool ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_happy_path_returns_text() -> None:
    """``call_tool`` must extract and return text content from a successful result.

    Failure means MCP tool results are dropped and the LLM sees empty responses,
    breaking the agent's ability to act on tool output.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {"type": "text", "text": "Found 3 results"},
                    {"type": "text", "text": "Page 1 of 1"},
                ],
                "isError": False,
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)
    spec = _make_spec("github")

    output = await manager.call_tool(spec, "github__search", {"query": "asyncio"})

    assert output == "Found 3 results\nPage 1 of 1", (
        "Multiple text blocks must be joined with newline"
    )
    # Verify the request was well-formed
    call = transport.calls[0]
    assert call.body["method"] == "tools/call"
    assert call.body["params"]["name"] == "github__search"
    assert call.body["params"]["arguments"] == {"query": "asyncio"}


def _input_required(elicitation_id: str, request_state: str, rpc_id: int) -> httpx.Response:
    """Build the ``input_required`` result the server returns for an ASK gate.

    :param elicitation_id: Server-minted approval id, e.g. ``"elicit_1"``.
    :param request_state: Opaque state the retry must echo back.
    :param rpc_id: JSON-RPC id of the request being answered.
    :returns: A JSON-RPC response carrying the elicitation.
    """
    return _json_resp(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    elicitation_id: {
                        "method": "elicitation/create",
                        "params": {
                            "message": "Approve shell command?",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"approved": {"type": "boolean"}},
                                "required": ["approved"],
                            },
                        },
                    }
                },
                "requestState": request_state,
            },
        }
    )


async def _wait_until_registered(elicitation_id: str) -> None:
    """Yield until ``call_tool`` parks on the approval for ``elicitation_id``."""
    for _ in range(1000):
        if pending_approvals.has_pending_elicitation(elicitation_id):
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"approval {elicitation_id} was never registered")


class _FakeClock:
    """Replacement for ``monotonic`` whose time moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_call_tool_recreates_approval_after_server_reconnect() -> None:
    """A reconnect discards stale requestState and repeats the original call."""
    old_elicitation = "elicit_old_server"
    new_elicitation = "elicit_new_server"

    transport = _StubTransport(
        [
            _input_required(old_elicitation, "old-state", 1),
            _input_required(new_elicitation, "new-state", 2),
            _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {
                        "content": [{"type": "text", "text": "approval-resumed"}],
                        "isError": False,
                    },
                }
            ),
        ]
    )
    manager = _make_manager(transport)
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(
        manager.call_tool(_make_spec("github"), "sys_os_shell", {"command": "printf ok"})
    )

    try:
        await _wait_until_registered(old_elicitation)
        assert pending_approvals.notify_server_reconnect() == 1
        await _wait_until_registered(new_elicitation)
        assert pending_approvals.resolve(new_elicitation, approved=True)

        assert await task == "approval-resumed"
    finally:
        if not task.done():
            task.cancel()
        pending_approvals.reset_for_tests()

    assert [call.body["id"] for call in transport.calls] == [1, 2, 3]
    first_params, replay_params, retry_params = [call.body["params"] for call in transport.calls]
    assert replay_params == first_params
    assert "requestState" not in replay_params
    assert retry_params["requestState"] == "new-state"
    assert new_elicitation in retry_params["inputResponses"]
    assert old_elicitation not in retry_params["inputResponses"]


@pytest.mark.asyncio
async def test_call_tool_reattaches_expired_execution_after_server_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconnect wait pins completed work past its normal retention window."""
    clock = 0.0
    monkeypatch.setattr(mcp_execution_registry_mod, "_COMPLETED_TTL_S", 1.0)
    monkeypatch.setattr(mcp_execution_registry_mod, "monotonic", lambda: clock)
    registry = McpExecutionRegistry()

    class _DetachThenSucceedTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))
            operation_id = body["params"][MCP_OPERATION_ID_PARAM]

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(
                    status_code=200,
                    content={"result": {"output": "retained"}},
                )

            await registry.execute(
                session_id="conv_test",
                operation_id=operation_id,
                step="initial",
                params={"name": "github__deploy", "arguments": {}},
                run=_retained_work,
            )
            if len(self.calls) == 1:
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
                            "message": "Runner MCP execution detached.",
                        },
                    }
                )
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "reattached"}],
                        "isError": False,
                    },
                }
            )

    transport = _DetachThenSucceedTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(manager.call_tool(None, "github__deploy", {}))
    try:
        for _ in range(1000):
            if pending_approvals.has_reconnect_waiters():
                break
            await asyncio.sleep(0.001)
        assert pending_approvals.has_reconnect_waiters()
        clock = 2.0
        pending_approvals.notify_server_reconnect()

        assert await task == "reattached"
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    first_params, retry_params = [call.body["params"] for call in transport.calls]
    assert retry_params == first_params
    assert first_params[MCP_OPERATION_ID_PARAM].startswith("mcpop_")


class _DropFirstRequestTransport(httpx.AsyncBaseTransport):
    """Server stand-in whose first ``tools/call`` is lost on the way back.

    Every request starts or reattaches to the retained runner execution the
    way ``/mcp/execute`` does, so a test can show the tool ran exactly once.

    :param registry: The runner-side registry shared with the manager.
    :param first_failure: Builds the status response to return, or the
        transport error to raise, for the first request only.
    """

    def __init__(
        self,
        registry: McpExecutionRegistry,
        first_failure: Callable[[], httpx.Response | Exception],
    ) -> None:
        self._registry = registry
        self._first_failure = first_failure
        self.calls: list[_Call] = []
        self.external_invocations = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(_Call(url=str(request.url), body=body))

        async def _retained_work() -> McpExecutionResult:
            self.external_invocations += 1
            return McpExecutionResult(status_code=200, content={"result": {"output": "done"}})

        await self._registry.execute(
            session_id="conv_test",
            operation_id=body["params"][MCP_OPERATION_ID_PARAM],
            step="initial",
            params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
            run=_retained_work,
        )
        if len(self.calls) == 1:
            failure = self._first_failure()
            if isinstance(failure, Exception):
                raise failure
            return failure
        return _json_resp(
            {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "content": [{"type": "text", "text": "poll finished"}],
                    "isError": False,
                },
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_failure",
    [
        pytest.param(lambda: httpx.Response(502, text="502 Bad Gateway"), id="gateway-502"),
        pytest.param(
            lambda: httpx.Response(503, text="503 Service Unavailable"), id="gateway-503"
        ),
        pytest.param(lambda: httpx.Response(504, text="504 Gateway Timeout"), id="gateway-504"),
        pytest.param(
            lambda: httpx.RemoteProtocolError("Server disconnected without sending a response."),
            id="connection-dropped",
        ),
    ],
)
async def test_call_tool_reattaches_after_request_drops_while_server_stays_up(
    monkeypatch: pytest.MonkeyPatch,
    first_failure: Callable[[], httpx.Response | Exception],
) -> None:
    """A gateway cutoff or dropped connection must not lose a running command's result.

    No server restart happens, so the call re-sends the retained operation
    without waiting for a new server generation, and the runner-side registry
    hands back the single execution instead of running the tool again.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    # Keep a wait-for-reconnect failure short instead of hanging the run.
    monkeypatch.setattr(proxy_mcp_manager_mod, "_SERVER_RECONNECT_WAIT_S", 0.2)
    registry = McpExecutionRegistry()
    transport = _DropFirstRequestTransport(registry, first_failure)
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await manager.call_tool(
            None, "sys_os_shell", {"command": "sleep 400; gh pr checks", "timeout": 600}
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert result == "poll finished"
    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    first_params, retry_params = [call.body["params"] for call in transport.calls]
    assert retry_params == first_params


@pytest.mark.asyncio
async def test_call_tool_reattaches_after_long_request_detaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel drop after minutes of execution still gets the full reconnect window.

    The detached reply lands long after the request was sent, so the time the
    tool spent running must not count against the wait for a replacement server.
    """
    clock = _FakeClock()
    monkeypatch.setattr(proxy_mcp_manager_mod, "monotonic", clock)
    registry = McpExecutionRegistry()

    def _detach_after_long_run() -> httpx.Response:
        clock.now += proxy_mcp_manager_mod._SERVER_RECONNECT_WAIT_S + 60.0
        pending_approvals.notify_server_reconnect()
        return _json_resp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
                    "message": "Runner MCP execution detached.",
                },
            }
        )

    transport = _DropFirstRequestTransport(registry, _detach_after_long_run)
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await manager.call_tool(
            None, "sys_os_shell", {"command": "sleep 400; gh pr checks", "timeout": 600}
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert result == "poll finished"
    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    first_params, retry_params = [call.body["params"] for call in transport.calls]
    assert retry_params == first_params


@pytest.mark.asyncio
async def test_call_tool_waits_for_rebind_before_resending_detached_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a detached reply the runner re-sends only once its tunnel has rebound.

    A server without the detached-while-unbound reply answers an early re-send
    with the terminal ``No runner bound`` error, which would lose the result.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    registry = McpExecutionRegistry()
    rebound = False

    class _LegacyServerTransport(httpx.AsyncBaseTransport):
        """Reports the execution detached, then refuses calls until the tunnel rebinds."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))
            if len(self.calls) > 1 and not rebound:
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32000, "message": "No runner bound for session"},
                    }
                )

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="initial",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            if len(self.calls) == 1:
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
                            "message": "Runner MCP execution detached.",
                        },
                    }
                )
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "rebound"}],
                        "isError": False,
                    },
                }
            )

    transport = _LegacyServerTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(
        manager.call_tool(None, "sys_os_shell", {"command": "sleep 400; echo ok", "timeout": 600})
    )
    try:
        for _ in range(1000):
            if pending_approvals.has_reconnect_waiters():
                break
            await asyncio.sleep(0.001)
        assert pending_approvals.has_reconnect_waiters()
        # Long enough for a polling runner to re-send and hit the legacy error.
        await asyncio.sleep(0.05)
        assert len(transport.calls) == 1, "the runner must not re-send before its tunnel rebinds"
        rebound = True
        pending_approvals.notify_server_reconnect()

        assert await task == "rebound"
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    first_params, retry_params = [call.body["params"] for call in transport.calls]
    assert retry_params == first_params


@pytest.mark.asyncio
async def test_call_tool_waits_for_rebind_after_legacy_unbound_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy server's ``No runner bound`` reply is a rebind signal, not a loss.

    A dropped request leaves this runner holding the retained execution. A server
    without the detached-while-unbound reply answers the early re-send with the
    terminal ``No runner bound`` error. The runner must read that as the tunnel
    still being unbound and wait for the rebind, not hand the harness that error
    and discard the retained result.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    registry = McpExecutionRegistry()
    rebound = False

    class _LegacyUnboundAfterDropTransport(httpx.AsyncBaseTransport):
        """Loses the first request, then refuses with the legacy error until rebind."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))
            if len(self.calls) == 2 and not rebound:
                # The server's tunnel to this runner has not rebound yet, so it
                # cannot reattach and answers with the legacy error.
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": -32000,
                            "message": "No runner bound for session 'conv_test'",
                        },
                    }
                )

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="initial",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            if len(self.calls) == 1:
                # The retained execution keeps running; the response is lost.
                raise httpx.ReadError("connection reset mid-execution")
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "rebound"}],
                        "isError": False,
                    },
                }
            )

    transport = _LegacyUnboundAfterDropTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(
        manager.call_tool(None, "sys_os_shell", {"command": "sleep 400; echo ok", "timeout": 600})
    )
    try:
        for _ in range(1000):
            if len(transport.calls) >= 2 and pending_approvals.has_reconnect_waiters():
                break
            await asyncio.sleep(0.001)
        assert len(transport.calls) == 2
        assert pending_approvals.has_reconnect_waiters()
        # The legacy error must not make the runner hammer the server.
        await asyncio.sleep(0.05)
        assert len(transport.calls) == 2, "the runner must not re-send before its tunnel rebinds"
        rebound = True
        pending_approvals.notify_server_reconnect()

        assert await task == "rebound"
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2, 3]
    params_seen = [call.body["params"] for call in transport.calls]
    assert params_seen[0] == params_seen[1] == params_seen[2]


@pytest.mark.asyncio
async def test_call_tool_resets_rebind_window_when_long_run_detaches_after_short_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long re-run that detaches gets a fresh rebind window despite an earlier drop.

    A brief transport blip starts the reconnect-wait streak. The re-sent request
    then runs for minutes before the tunnel drops and the server reports it
    detached. The minutes it ran must not count against the window, so the call
    waits for the rebind instead of failing immediately with no replacement.
    """
    clock = _FakeClock()
    monkeypatch.setattr(proxy_mcp_manager_mod, "monotonic", clock)
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    registry = McpExecutionRegistry()

    class _ShortDropThenLongDetachTransport(httpx.AsyncBaseTransport):
        """Blips on the first request, then runs long and detaches on the re-send."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))
            if len(self.calls) == 2:
                # The re-sent request runs for minutes, then the tunnel drops and
                # the server reports the retained execution detached.
                clock.now += proxy_mcp_manager_mod._SERVER_RECONNECT_WAIT_S + 280.0
                pending_approvals.notify_server_reconnect()
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
                            "message": "Runner MCP execution detached.",
                        },
                    }
                )

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="initial",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            if len(self.calls) == 1:
                # A brief blip loses the response moments after it was sent.
                raise httpx.ReadError("brief drop")
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "resumed"}],
                        "isError": False,
                    },
                }
            )

    transport = _ShortDropThenLongDetachTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await manager.call_tool(
            None, "sys_os_shell", {"command": "sleep 400; gh pr checks", "timeout": 600}
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert result == "resumed"
    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2, 3]


@pytest.mark.asyncio
async def test_call_tool_resets_rebind_window_on_each_successful_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tunnel that keeps rebinding never exhausts the reconnect window.

    Each short detach-then-rebind cycle proves the runner became reachable
    again, so the window restarts from the latest rebind. Without the reset a
    long run of brief flaps would fail the call even though every rebind
    succeeded; here the window would run out after the sixth detach.
    """
    clock = _FakeClock()
    monkeypatch.setattr(proxy_mcp_manager_mod, "monotonic", clock)
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    registry = McpExecutionRegistry()
    cycle_s = 20.0
    detach_cycles = 9

    class _FlappingRebindTransport(httpx.AsyncBaseTransport):
        """Rebinds then detaches on every re-send until the last call succeeds."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="initial",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            if len(self.calls) <= detach_cycles:
                # The tunnel rebinds right away, then drops again before the
                # re-send reaches the runner; each flap stays under the long-run
                # threshold so only a reset keeps the window alive.
                clock.now += cycle_s
                pending_approvals.notify_server_reconnect()
                return _json_resp(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {
                            "code": RUNNER_MCP_EXECUTION_DETACHED_CODE,
                            "message": "Runner MCP execution detached.",
                        },
                    }
                )
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "resumed"}],
                        "isError": False,
                    },
                }
            )

    transport = _FlappingRebindTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await manager.call_tool(
            None, "sys_os_shell", {"command": "sleep 400; gh pr checks", "timeout": 600}
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert result == "resumed"
    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert len(transport.calls) == detach_cycles + 1


@pytest.mark.asyncio
async def test_call_tool_resets_rebind_window_on_each_rebind_after_transport_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transport-loss retry window also restarts on each successful rebind.

    Quick transport failures start the reconnect streak; the tunnel reconnects
    near the original deadline and more quick failures follow. The window must
    restart from the latest rebind, as the detached-reply branch does, so the
    call recovers instead of expiring against the stale deadline. Without the
    reset the window would run out after the sixth drop.
    """
    clock = _FakeClock()
    monkeypatch.setattr(proxy_mcp_manager_mod, "monotonic", clock)
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    registry = McpExecutionRegistry()
    cycle_s = 20.0
    drop_cycles = 8

    class _FlappingDropTransport(httpx.AsyncBaseTransport):
        """Rebinds then drops the next re-send in transit until the last call."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="initial",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            if len(self.calls) <= drop_cycles:
                # The tunnel rebinds right away, then the next re-send drops in
                # transit; each flap stays under the long-run threshold so only
                # a reset keeps the window alive.
                clock.now += cycle_s
                pending_approvals.notify_server_reconnect()
                raise httpx.ReadError("brief drop")
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "resumed"}],
                        "isError": False,
                    },
                }
            )

    transport = _FlappingDropTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await manager.call_tool(
            None, "sys_os_shell", {"command": "sleep 400; gh pr checks", "timeout": 600}
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert result == "resumed"
    assert transport.external_invocations == 1, "the shell command must not run twice"
    assert len(transport.calls) == drop_cycles + 1


@pytest.mark.asyncio
async def test_call_tool_returns_unrelated_server_error_without_waiting_for_rebind() -> None:
    """A ``-32000`` that is not the unbound-runner reply is surfaced at once.

    The retained operation is still registered, so only the exact unbound-runner
    message may start the rebind wait. Any other server-defined error, such as a
    tool denial, returns to the harness immediately rather than stalling.
    """
    registry = McpExecutionRegistry()

    class _DenyAfterRetainTransport(httpx.AsyncBaseTransport):
        """Retains the operation, then answers with an unrelated ``-32000``."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="initial",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32000, "message": "Tool call denied by policy"},
                }
            )

    transport = _DenyAfterRetainTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    try:
        result = await asyncio.wait_for(
            manager.call_tool(
                None, "sys_os_shell", {"command": "sleep 400; gh pr checks", "timeout": 600}
            ),
            timeout=10.0,
        )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert json.loads(result) == {"error": "Tool call denied by policy"}
    assert [call.body["id"] for call in transport.calls] == [1], (
        "an unrelated -32000 must not trigger a re-send"
    )
    assert not pending_approvals.has_reconnect_waiters()


@pytest.mark.asyncio
async def test_call_tool_budget_restarts_after_user_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hours spent waiting for an approval leave the whole budget for the approved call."""
    clock = _FakeClock()
    monkeypatch.setattr(proxy_mcp_manager_mod, "monotonic", clock)
    elicitation = "elicit_long_wait"
    registry = McpExecutionRegistry()

    class _ApproveAfterLongWaitTransport(httpx.AsyncBaseTransport):
        """Asks for approval, then answers the approved call a full budget later."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))
            if len(self.calls) == 1:
                return _input_required(elicitation, "approved-state", body["id"])

            async def _retained_work() -> McpExecutionResult:
                self.external_invocations += 1
                return McpExecutionResult(status_code=200, content={"result": {"output": "ok"}})

            await registry.execute(
                session_id="conv_test",
                operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                step="retry",
                params={"name": body["params"]["name"], "arguments": body["params"]["arguments"]},
                run=_retained_work,
            )
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "content": [{"type": "text", "text": "approved and done"}],
                        "isError": False,
                    },
                }
            )

    transport = _ApproveAfterLongWaitTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(
        manager.call_tool(None, "sys_os_shell", {"command": "sleep 300; echo ok", "timeout": 600})
    )
    try:
        await _wait_until_registered(elicitation)
        clock.now += proxy_mcp_manager_mod.MCP_PROXY_CALL_TIMEOUT_S + 3_600.0
        assert pending_approvals.resolve(elicitation, approved=True)

        assert await task == "approved and done"
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.external_invocations == 1, "the approved command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2]
    _, approved_params = [call.body["params"] for call in transport.calls]
    assert elicitation in approved_params["inputResponses"]


@pytest.mark.asyncio
async def test_call_tool_cannot_recover_an_approved_call_lost_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved call whose reply is lost cannot be replayed; the server rejects it.

    The server consumes the elicitation on the first approved request, so a
    re-send after a gateway drop gets ``Elicitation not found`` instead of the
    retained result. This known limitation surfaces as a soft error, not a hang.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    elicitation = "elicit_lost_reply"
    registry = McpExecutionRegistry()

    class _ApproveThenLoseReplyTransport(httpx.AsyncBaseTransport):
        """Asks for approval, loses the approved reply, then rejects the re-send."""

        def __init__(self) -> None:
            self.calls: list[_Call] = []
            self.external_invocations = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(_Call(url=str(request.url), body=body))
            if len(self.calls) == 1:
                return _input_required(elicitation, "approved-state", body["id"])
            if len(self.calls) == 2:

                async def _retained_work() -> McpExecutionResult:
                    self.external_invocations += 1
                    return McpExecutionResult(
                        status_code=200, content={"result": {"output": "ok"}}
                    )

                await registry.execute(
                    session_id="conv_test",
                    operation_id=body["params"][MCP_OPERATION_ID_PARAM],
                    step="retry",
                    params={
                        "name": body["params"]["name"],
                        "arguments": body["params"]["arguments"],
                    },
                    run=_retained_work,
                )
                return httpx.Response(502, text="502 Bad Gateway")
            return _json_resp(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {
                        "code": -32000,
                        "message": "Elicitation not found or already resolved",
                    },
                }
            )

    transport = _ApproveThenLoseReplyTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=registry,
    )
    pending_approvals.reset_for_tests()
    task = asyncio.create_task(
        manager.call_tool(None, "sys_os_shell", {"command": "sleep 300; echo ok", "timeout": 600})
    )
    try:
        await _wait_until_registered(elicitation)
        assert pending_approvals.resolve(elicitation, approved=True)

        result = await task
    finally:
        if not task.done():
            task.cancel()
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert json.loads(result) == {"error": "Elicitation not found or already resolved"}
    assert transport.external_invocations == 1, "the approved command must not run twice"
    assert [call.body["id"] for call in transport.calls] == [1, 2, 3]
    approved_params, retry_params = [call.body["params"] for call in transport.calls[1:]]
    assert retry_params == approved_params
    assert elicitation in retry_params["inputResponses"]


@pytest.mark.asyncio
async def test_call_tool_non_gateway_http_error_raises_without_retry() -> None:
    """Only gateway statuses mean the request was lost; a 500 is a terminal failure."""
    transport = _StubTransport([_json_resp({"detail": "boom"}, status=500)])
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=McpExecutionRegistry(),
    )

    with pytest.raises(RuntimeError, match="500"):
        await manager.call_tool(None, "sys_os_shell", {"command": "true"})
    await client.aclose()

    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_call_tool_gateway_error_without_retained_operation_raises() -> None:
    """Without a runner-side operation to reattach to, a gateway error is reported as-is."""
    transport = _StubTransport([_json_resp({}, status=502)])
    manager = _make_manager(transport)

    with pytest.raises(RuntimeError, match="502"):
        await manager.call_tool(None, "sys_os_shell", {"command": "true"})

    assert len(transport.calls) == 1


class _CountingFailureTransport(httpx.AsyncBaseTransport):
    """Transport that fails every request the same way and counts attempts.

    :param failure: Builds the status response to return, or the error to raise.
    """

    def __init__(self, failure: Callable[[], httpx.Response | Exception]) -> None:
        self._failure = failure
        self.attempts = 0
        self.read_timeouts: list[float] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        self.read_timeouts.append(request.extensions["timeout"]["read"])
        failure = self._failure()
        if isinstance(failure, Exception):
            raise failure
        return failure


@pytest.mark.asyncio
async def test_call_tool_stops_reattaching_when_no_server_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast refusals keep today's bounded reconnect window instead of the whole call budget."""
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.01)
    monkeypatch.setattr(proxy_mcp_manager_mod, "_SERVER_RECONNECT_WAIT_S", 0.1)
    transport = _CountingFailureTransport(lambda: httpx.ConnectError("connection refused"))
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=McpExecutionRegistry(),
    )
    pending_approvals.reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="no replacement connected") as exc_info:
            await manager.call_tool(None, "sys_os_shell", {"command": "true"})
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert "connection refused" in str(exc_info.value)
    assert transport.attempts > 1, "the retained operation must be re-sent before giving up"


@pytest.mark.asyncio
async def test_call_tool_reattach_loop_is_bounded_by_the_call_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cutoffs of long requests stop at the proxy call budget.

    Each re-send may only read for the budget that is left, and nothing is sent
    once the budget is spent, so the whole call ends within the budget.
    """
    clock = _FakeClock()
    monkeypatch.setattr(proxy_mcp_manager_mod, "monotonic", clock)
    monkeypatch.setattr(proxy_mcp_manager_mod, "_REATTACH_RETRY_DELAY_S", 0.001)
    monkeypatch.setattr(proxy_mcp_manager_mod, "MCP_PROXY_CALL_TIMEOUT_S", 100.0)

    def _cut_after_forty_seconds() -> httpx.Response:
        clock.now += 40.0
        return httpx.Response(502, text="502 Bad Gateway")

    transport = _CountingFailureTransport(_cut_after_forty_seconds)
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=McpExecutionRegistry(),
    )
    pending_approvals.reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="did not complete within 100s") as exc_info:
            await manager.call_tool(None, "sys_os_shell", {"command": "sleep 400"})
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert "502" in str(exc_info.value)
    assert transport.read_timeouts == [100.0, 60.0, 20.0]


@pytest.mark.asyncio
async def test_call_tool_overall_deadline_bounds_a_slow_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single attempt that stays in flight past the budget still ends the call.

    httpx's read timeout resets on every byte, so a slow-drip response that
    never stalls long enough to trip it could outlive the budget; the overall
    deadline must end such an attempt even though its read timeout never fires.
    """
    monkeypatch.setattr(proxy_mcp_manager_mod, "MCP_PROXY_CALL_TIMEOUT_S", 2.0)

    class _NeverCompletingTransport(httpx.AsyncBaseTransport):
        """Holds the response open past the budget without tripping a read timeout."""

        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            await asyncio.sleep(30.0)
            return _json_resp({"jsonrpc": "2.0", "id": 1, "result": {}})

    transport = _NeverCompletingTransport()
    client = httpx.AsyncClient(transport=transport, base_url="http://ap-server")
    manager = ProxyMcpManager(
        session_id="conv_test",
        ap_client=client,
        execution_registry=McpExecutionRegistry(),
    )
    pending_approvals.reset_for_tests()
    try:
        with pytest.raises(RuntimeError, match="did not complete within"):
            await asyncio.wait_for(
                manager.call_tool(None, "sys_os_shell", {"command": "sleep 600"}),
                timeout=10.0,
            )
    finally:
        await client.aclose()
        pending_approvals.reset_for_tests()

    assert transport.calls == 1


@pytest.mark.asyncio
async def test_call_tool_is_error_returns_json_error_string() -> None:
    """``isError=True`` in result must be returned as a JSON error string, not raised.

    Failure means tool-reported errors raise RuntimeError and crash the harness
    instead of surfacing cleanly to the LLM as a tool result.
    """
    rpc_resp = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": "Repository not found"}],
                "isError": True,
            },
        }
    )
    transport = _StubTransport([rpc_resp])
    manager = _make_manager(transport)

    output = await manager.call_tool(_make_spec("github"), "github__get_repo", {})

    parsed = json.loads(output)
    assert parsed == {"error": "Repository not found"}, (
        "isError=True must yield a JSON error object for the LLM to interpret"
    )


@pytest.mark.asyncio
async def test_call_tool_minus_32000_returns_json_error_not_raises() -> None:
    """RPC code -32000 (tool denial / server error) must return JSON string, not raise.

    Failure means policy denials (which use -32000) crash the harness instead
    of letting the LLM see the denial message.
    """
    rpc_error = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "Policy DENIED: push to main blocked"},
        }
    )
    transport = _StubTransport([rpc_error])
    manager = _make_manager(transport)

    output = await manager.call_tool(_make_spec("github"), "github__push", {})

    parsed = json.loads(output)
    assert parsed == {"error": "Policy DENIED: push to main blocked"}, (
        "-32000 must be returned as a soft JSON error, not raised"
    )


@pytest.mark.asyncio
async def test_call_tool_non_32000_rpc_error_raises() -> None:
    """An unexpected RPC error code (not -32000) must raise RuntimeError.

    Failure means protocol errors (invalid request, parse error) are silently
    swallowed and the LLM never learns the tool call failed.
    """
    rpc_error = _json_resp(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    )
    transport = _StubTransport([rpc_error])
    manager = _make_manager(transport)

    with pytest.raises(RuntimeError, match="-32600"):
        await manager.call_tool(_make_spec("github"), "github__search", {})


@pytest.mark.asyncio
async def test_call_tool_non_object_rpc_error_raises_precise_error() -> None:
    """A malformed JSON-RPC error remains fail-closed with a precise diagnostic."""
    transport = _StubTransport([_json_resp({"jsonrpc": "2.0", "id": 1, "error": []})])
    manager = _make_manager(transport)

    with pytest.raises(RuntimeError, match="non-object RPC error"):
        await manager.call_tool(_make_spec("github"), "github__search", {})


@pytest.mark.asyncio
async def test_call_tool_network_error_raises() -> None:
    """A network failure must raise RuntimeError containing the tool name and session.

    Failure means network errors are swallowed, leaving the harness
    in an undefined state with no feedback about the failed call.
    """

    class _FailingTransport(httpx.AsyncBaseTransport):
        """Transport that always raises a connection error."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            """Simulate a network failure.

            :param request: Ignored.
            :raises httpx.ConnectError: Always.
            """
            raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=_FailingTransport(), base_url="http://ap")
    manager = ProxyMcpManager(session_id="conv_test", ap_client=client)

    with pytest.raises(RuntimeError) as exc_info:
        await manager.call_tool(_make_spec("github"), "github__search", {})

    error_msg = str(exc_info.value)
    assert "github__search" in error_msg, "Tool name must appear in the error message"
    assert "conv_test" in error_msg, "Session id must appear in the error message"


# ── dispatch timeout nesting ────────────────────────────────────────────────


def test_proxy_call_timeout_exceeds_forward_timeout() -> None:
    """The outer MCP proxy timeout must exceed the AP→runner timeout."""
    from omnigent.runner.tool_dispatch import (
        _OS_ENV_SHELL_DEFAULT_TIMEOUT_S,
        _RUNNER_EXECUTION_TIMEOUT_S,
        MCP_PROXY_CALL_TIMEOUT_S,
        MCP_PROXY_FORWARD_TIMEOUT_S,
    )

    assert MCP_PROXY_FORWARD_TIMEOUT_S > _OS_ENV_SHELL_DEFAULT_TIMEOUT_S, (
        "AP→runner read timeout must exceed the default sys_os_shell timeout; "
        "otherwise a valid synchronous shell tool can be cut off by transport."
    )
    assert MCP_PROXY_FORWARD_TIMEOUT_S > _RUNNER_EXECUTION_TIMEOUT_S, (
        "AP→runner read timeout must exceed the runner execution timeout, not "
        "just the default shell timeout, because sys_os_shell accepts longer "
        "caller-provided timeouts."
    )
    assert MCP_PROXY_FORWARD_TIMEOUT_S < MCP_PROXY_CALL_TIMEOUT_S, (
        "Runner→AP outer timeout must exceed AP→runner forwarding timeout so "
        "the inner hop fails first with the useful runner-side error."
    )


@pytest.mark.asyncio
async def test_call_tool_uses_configured_read_timeout() -> None:
    """``call_tool`` must POST with the configured proxy read timeout."""
    from omnigent.runner.tool_dispatch import MCP_PROXY_CALL_TIMEOUT_S

    captured: dict[str, object] = {}

    class _TimeoutCapturingTransport(httpx.AsyncBaseTransport):
        """Transport that records the request's timeout extension."""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            """Record the timeout extension and return an empty-result response.

            :param request: The outgoing request whose timeout is captured.
            :returns: A minimal successful JSON-RPC result.
            """
            captured["timeout"] = request.extensions.get("timeout")
            return _json_resp({"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    client = httpx.AsyncClient(transport=_TimeoutCapturingTransport(), base_url="http://ap")
    manager = ProxyMcpManager(session_id="conv_test", ap_client=client)

    await manager.call_tool(_make_spec("github"), "github__search", {})

    timeout = captured["timeout"]
    assert isinstance(timeout, dict), "httpx records the timeout as a per-op dict"
    assert timeout["read"] == pytest.approx(MCP_PROXY_CALL_TIMEOUT_S, abs=1.0), (
        "ProxyMcpManager must pass the MCP_PROXY_CALL_TIMEOUT_S budget as the read timeout; "
        "otherwise call_tool may regress to httpx's shorter default."
    )
    assert timeout["connect"] == 10.0, "Connect timeout stays short to fail fast on a dead server"
