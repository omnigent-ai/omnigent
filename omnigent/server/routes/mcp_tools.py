"""Owner-only, lazy MCP tool discovery through a connected host."""

from __future__ import annotations

import asyncio
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.host.frames import (
    CAP_MCP_TOOLS,
    HostMcpToolsFrame,
    HostMcpToolsResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

_MCP_TOOLS_TIMEOUT_S = 15.0


class McpToolsRequest(BaseModel):
    harness: Literal["claude", "codex", "cursor"]
    server: str = Field(min_length=1, max_length=1024)
    plugin: str | None = Field(default=None, max_length=1024)
    source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class McpToolSummary(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(max_length=256)
    description: str | None = Field(default=None, max_length=300)


class McpToolsResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    tools: list[McpToolSummary] = Field(max_length=500)
    connection: Literal["connected", "needs_auth", "unreachable", "timeout", "unsupported"]
    truncated: bool


def create_mcp_tools_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/hosts/{host_id}/mcp-servers/tools")
    async def get_mcp_tools(
        request: Request, host_id: str, body: McpToolsRequest
    ) -> McpToolsResponse:
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_MCP_TOOLS not in conn.hello.capabilities:
            raise HTTPException(status_code=501, detail="update the host to list MCP tools")
        result = await request_host_mcp_tools(
            host_registry=host_registry,
            host_conn=conn,
            harness=body.harness,
            server=body.server,
            plugin=body.plugin,
            source_id=body.source_id,
        )
        if result.status == "busy":
            raise HTTPException(status_code=503, detail="host MCP probe capacity exhausted")
        if result.status != "ok":
            raise HTTPException(status_code=502, detail="host MCP tools lookup failed")
        try:
            return McpToolsResponse.model_validate(
                {
                    "tools": result.tools,
                    "connection": result.connection,
                    "truncated": result.truncated,
                }
            )
        except ValidationError:
            raise HTTPException(
                status_code=502, detail="host sent a malformed MCP tools reply"
            ) from None

    return router


async def request_host_mcp_tools(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    server: str,
    plugin: str | None = None,
    source_id: str | None = None,
) -> HostMcpToolsResultFrame:
    """Request an MCP server's tools over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostMcpToolsResultFrame] = asyncio.get_running_loop().create_future()
    host_conn.pending_mcp_tools[request_id] = future
    try:
        host_registry.send_text(
            host_conn,
            encode_host_frame(
                HostMcpToolsFrame(
                    request_id=request_id,
                    harness=harness,
                    server=server,
                    plugin=plugin,
                    source_id=source_id,
                )
            ),
        )
        return await asyncio.wait_for(future, timeout=_MCP_TOOLS_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"host '{host_conn.host_id}' connection lost"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not report MCP tools "
                f"within {_MCP_TOOLS_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_mcp_tools.pop(request_id, None)
