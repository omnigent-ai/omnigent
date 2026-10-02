"""User-level MCP server inventory for a connected host."""

from __future__ import annotations

import asyncio
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from omnigent.host.frames import (
    CAP_MCP_INVENTORY,
    HostMcpServersFrame,
    HostMcpServersResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

_MCP_SERVERS_TIMEOUT_S = 15.0


class McpServerSummary(BaseModel):
    """An MCP server a harness loads on the host, without secrets.

    :param name: Server name as configured, e.g. ``"github"``.
    :param harness: Harness family that loads it: ``claude``, ``codex``, or ``cursor``.
    :param transport: ``stdio`` for a local process, ``http`` for a remote server.
    :param scope: Config scope; always ``user`` today.
    :param plugin: Plugin that bundles the server, e.g. ``"figma"``.
    :param url_host: Hostname of a remote server, e.g. ``"mcp.linear.app"``.
    """

    name: str
    harness: str
    transport: Literal["stdio", "http"]
    scope: Literal["user"]
    plugin: str | None = None
    url_host: str | None = None


class McpServersResponse(BaseModel):
    """User-level MCP servers discovered on a host."""

    mcp_servers: list[McpServerSummary]


def create_mcp_servers_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the MCP inventory route, mounted under ``/v1``."""
    router = APIRouter()

    @router.get("/hosts/{host_id}/mcp-servers")
    async def get_mcp_servers(request: Request, host_id: str) -> McpServersResponse:
        """List the user-level MCP servers configured for each harness on a host.

        Returns names and non-secret metadata only; the caller must own the host.
        """
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_MCP_INVENTORY not in conn.hello.capabilities:
            raise HTTPException(status_code=501, detail="update the host to list its MCP servers")
        result = await request_host_mcp_servers(host_registry=host_registry, host_conn=conn)
        if result.status != "ok":
            raise HTTPException(
                status_code=502, detail=result.error or "host MCP inventory failed"
            )
        return McpServersResponse(
            mcp_servers=[McpServerSummary.model_validate(s) for s in result.mcp_servers]
        )

    return router


async def request_host_mcp_servers(
    *, host_registry: HostRegistry, host_conn: HostConnection
) -> HostMcpServersResultFrame:
    """Request the MCP inventory over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostMcpServersResultFrame] = asyncio.get_running_loop().create_future()
    host_conn.pending_mcp_servers[request_id] = future
    try:
        host_registry.send_text(
            host_conn, encode_host_frame(HostMcpServersFrame(request_id=request_id))
        )
        return await asyncio.wait_for(future, timeout=_MCP_SERVERS_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"host '{host_conn.host_id}' connection lost"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not list MCP servers "
                f"within {_MCP_SERVERS_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_mcp_servers.pop(request_id, None)
