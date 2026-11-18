"""Runner routes that read and reconnect a running harness's own MCP connections."""

from __future__ import annotations

import logging
from collections.abc import Awaitable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from omnigent.runner.app_support import _client_safe_error_detail
from omnigent.runner.native import _forward_harness_response
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError

_logger = logging.getLogger("omnigent.runner.app")

# Above the harness's own budgets: one status read (10 s), or a reconnect (30 s)
# bracketed by status reads.
_LIVE_MCP_STATUS_TIMEOUT_S = 15.0
_LIVE_MCP_RECONNECT_TIMEOUT_S = 60.0


def register_live_mcp_routes(
    app: FastAPI, *, process_manager: HarnessProcessManager | None
) -> None:
    """Register ``/v1/sessions/{session_id}/live-mcp-servers[/reconnect]`` on *app*.

    Both routes reach only a harness that is already running; they never start one.
    """

    @app.get("/v1/sessions/{session_id}/live-mcp-servers")
    async def get_live_mcp_servers(session_id: str) -> Response:
        client = await _running_harness_client(process_manager, session_id)
        if client is None:
            return JSONResponse({"running": False, "servers": []})
        return await _forward(
            client.get(
                f"/v1/sessions/{session_id}/mcp-status", timeout=_LIVE_MCP_STATUS_TIMEOUT_S
            ),
            context="live MCP status",
        )

    @app.post("/v1/sessions/{session_id}/live-mcp-servers/reconnect")
    async def reconnect_live_mcp_server(session_id: str, request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.strip():
            return JSONResponse(
                {"error": "invalid_request", "detail": "name is required"}, status_code=400
            )
        client = await _running_harness_client(process_manager, session_id)
        if client is None:
            return JSONResponse({"error": "harness_not_running"}, status_code=409)
        return await _forward(
            client.post(
                f"/v1/sessions/{session_id}/mcp-reconnect",
                json={"name": name},
                timeout=_LIVE_MCP_RECONNECT_TIMEOUT_S,
            ),
            context="live MCP reconnect",
        )


async def _running_harness_client(
    process_manager: HarnessProcessManager | None, session_id: str
) -> httpx.AsyncClient | None:
    if process_manager is None:
        return None
    try:
        return await process_manager.get_client(session_id, "any")
    except NoLiveHarnessError:
        return None
    except RuntimeError:
        # The manager is starting up or shutting down; nothing is reachable either way.
        _logger.debug("No harness client for live MCP request", exc_info=True)
        return None


async def _forward(request: Awaitable[httpx.Response], *, context: str) -> Response:
    try:
        response = await request
    except httpx.TimeoutException:
        return JSONResponse({"error": "harness_timeout"}, status_code=504)
    except httpx.HTTPError as exc:
        _logger.warning("%s forward failed", context, exc_info=True)
        return JSONResponse(
            {
                "error": "harness_unreachable",
                "detail": _client_safe_error_detail(exc, context=context),
            },
            status_code=502,
        )
    return _forward_harness_response(response)
