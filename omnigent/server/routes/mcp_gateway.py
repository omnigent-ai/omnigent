"""Per-service MCP endpoints with an Omnigent policy adapter."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response

from omnigent.server.mcp_policy_adapter import McpPolicyAdapter
from omnigent.server.mcp_registry import McpUpstreamError
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._sessions.helpers import _mcp_error_response, _mcp_ok_response
from omnigent.server.routes.connections_base import ConnectionError


def create_mcp_gateway_router(adapter: McpPolicyAdapter) -> APIRouter:
    router = APIRouter()

    @router.post("/mcp/{service_id}", dependencies=[Depends(require_json_content_type)])
    async def gateway(
        service_id: str,
        request: Request,
        session_id: Annotated[
            str, Header(alias="X-Omnigent-Session-Id", min_length=1, max_length=128)
        ],
    ) -> Response:
        context = await adapter.authorize(request, session_id, service_id)
        try:
            body = await request.json()
        except ValueError:
            return _mcp_error_response(None, -32700, "Parse error: invalid JSON")
        if not isinstance(body, dict):
            return _mcp_error_response(None, -32600, "Expected a JSON-RPC object")
        rpc_id = body.get("id")
        if body.get("jsonrpc") != "2.0" or (rpc_id is not None and type(rpc_id) not in (int, str)):
            return _mcp_error_response(None, -32600, "Invalid JSON-RPC request")
        method = body.get("method")
        if method == "notifications/initialized":
            return Response(status_code=202)
        params: Any = body.get("params", {})
        if not isinstance(params, dict):
            return _mcp_error_response(rpc_id, -32602, "Expected object params")
        if method == "initialize":
            return _mcp_ok_response(
                rpc_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": service_id, "version": "1.0.0"},
                },
            )
        if method == "tools/list":
            try:
                tools = await adapter.list_tools(request, context)
                return _mcp_ok_response(rpc_id, {"tools": tools})
            except (ConnectionError, McpUpstreamError) as exc:
                return _mcp_error_response(rpc_id, -32000, str(exc))
            except Exception:  # noqa: BLE001 — upstream errors may contain credentials
                return _mcp_error_response(rpc_id, -32000, "MCP discovery failed")
        if method == "tools/call":
            if not isinstance(params.get("name"), str) or not params["name"]:
                return _mcp_error_response(rpc_id, -32602, "Expected a tool name")
            if not isinstance(params.get("arguments", {}), dict):
                return _mcp_error_response(rpc_id, -32602, "Expected object arguments")
            if "requestState" in params and not isinstance(params["requestState"], str):
                return _mcp_error_response(rpc_id, -32602, "Expected string requestState")
            if not isinstance(params.get("inputResponses", {}), dict):
                return _mcp_error_response(rpc_id, -32602, "Expected object inputResponses")
            return await adapter.call_tool(request, context, rpc_id, params)
        return _mcp_error_response(rpc_id, -32601, "Method not found")

    return router
