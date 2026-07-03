"""Integration tests for the MCP proxy endpoint ``POST /v1/sessions/{id}/mcp``.

Covers JSON-RPC validation, method routing, and error paths that do
not require a live runner connection.  The MCP endpoint proxies
JSON-RPC requests to the runner's MCP servers; these tests exercise
the validation layer and error handling on the AP server side.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
which wires a real ``RunnerRouter`` with an empty tunnel registry,
so the endpoint is reachable and runner-dependent methods return the
"no runner bound" application error.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_session

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
        "conv_does_not_exist_mcp",
        _jsonrpc("tools/list", rpc_id=10),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["id"] == 10
    assert payload["error"]["code"] == -32000
    assert "No runner bound" in payload["error"]["message"]
