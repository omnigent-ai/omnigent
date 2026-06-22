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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

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
    assert timeout["read"] == MCP_PROXY_CALL_TIMEOUT_S, (
        "ProxyMcpManager must pass MCP_PROXY_CALL_TIMEOUT_S as the read timeout; "
        "otherwise call_tool may regress to httpx's shorter default."
    )
    assert timeout["connect"] == 10.0, "Connect timeout stays short to fail fast on a dead server"
