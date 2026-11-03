"""Owner-only MCP tool discovery and harness sign-in through a connected host."""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.host.frames import (
    CAP_MCP_LOGIN,
    CAP_MCP_TOOLS,
    HostMcpLoginFrame,
    HostMcpToolsFrame,
    HostMcpToolsResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

# The host's 10s probe may be followed by a 5s harness sign-in status check.
_MCP_TOOLS_TIMEOUT_S = 20.0
# Starting waits for the harness CLI to print its authorization page (30s on the host).
_MCP_LOGIN_START_TIMEOUT_S = 40.0
_MCP_LOGIN_TIMEOUT_S = 10.0
_LOGIN_STATUS_CODES = {"not_found": 404, "invalid": 400, "busy": 503}
_ResultFrame = TypeVar("_ResultFrame")
LoginId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class McpToolsRequest(BaseModel):
    harness: Literal["claude", "codex", "cursor"]
    server: str = Field(min_length=1, max_length=1024)
    plugin: str | None = Field(default=None, max_length=1024)
    source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class McpLoginRequest(BaseModel):
    harness: Literal["claude", "codex"]
    server: str = Field(min_length=1, max_length=1024)
    plugin: str | None = Field(default=None, max_length=1024)
    source_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class McpLoginCallbackRequest(BaseModel):
    callback_url: str = Field(min_length=1, max_length=4096)


class McpLoginResponse(BaseModel):
    """A harness sign-in on the host; ``authorization_url`` is set only while pending."""

    model_config = ConfigDict(strict=True)
    login_id: str = Field(max_length=64)
    state: Literal["pending", "succeeded", "failed", "expired", "cancelled"]
    authorization_url: str | None = Field(default=None, max_length=8192)
    error: str | None = Field(default=None, max_length=300)


class McpToolSummary(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(max_length=256)
    description: str | None = Field(default=None, max_length=300)


class McpToolsResponse(BaseModel):
    model_config = ConfigDict(strict=True)
    tools: list[McpToolSummary] = Field(max_length=500)
    connection: Literal["connected", "needs_auth", "unreachable", "timeout", "unsupported"]
    truncated: bool
    # With ``needs_auth``: the harness can sign in (``available``) or already has.
    harness_sign_in: Literal["available", "signed_in"] | None = None


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
                    "harness_sign_in": result.harness_sign_in,
                }
            )
        except ValidationError:
            raise HTTPException(
                status_code=502, detail="host sent a malformed MCP tools reply"
            ) from None

    async def login_host(request: Request, host_id: str) -> HostConnection:
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_MCP_LOGIN not in conn.hello.capabilities:
            raise HTTPException(
                status_code=501, detail="update the host to sign in to MCP servers"
            )
        return conn

    async def send_login(
        conn: HostConnection, timeout_s: float, **fields: Any
    ) -> McpLoginResponse:
        request_id = secrets.token_hex(8)
        result = await _request_host(
            host_registry,
            conn,
            conn.pending_mcp_logins,
            HostMcpLoginFrame(request_id=request_id, **fields),
            timeout_s,
            "answer the MCP sign-in request",
        )
        if result.status != "ok":
            code = _LOGIN_STATUS_CODES.get(result.status, 502)
            detail = result.error if code != 502 else "host MCP sign-in failed"
            raise HTTPException(status_code=code, detail=detail or "MCP sign-in failed")
        try:
            return McpLoginResponse.model_validate(
                {
                    "login_id": result.login_id,
                    "state": result.state,
                    "authorization_url": result.authorization_url,
                    "error": result.error,
                }
            )
        except ValidationError:
            raise HTTPException(
                status_code=502, detail="host sent a malformed MCP sign-in reply"
            ) from None

    @router.post("/hosts/{host_id}/mcp-servers/login")
    async def start_mcp_login(
        request: Request, host_id: str, body: McpLoginRequest
    ) -> McpLoginResponse:
        conn = await login_host(request, host_id)
        return await send_login(
            conn,
            _MCP_LOGIN_START_TIMEOUT_S,
            action="start",
            harness=body.harness,
            server=body.server,
            plugin=body.plugin,
            source_id=body.source_id,
        )

    @router.get("/hosts/{host_id}/mcp-servers/login/{login_id}")
    async def get_mcp_login(request: Request, host_id: str, login_id: LoginId) -> McpLoginResponse:
        conn = await login_host(request, host_id)
        return await send_login(conn, _MCP_LOGIN_TIMEOUT_S, action="status", login_id=login_id)

    @router.post("/hosts/{host_id}/mcp-servers/login/{login_id}/callback")
    async def submit_mcp_login_callback(
        request: Request, host_id: str, login_id: LoginId, body: McpLoginCallbackRequest
    ) -> McpLoginResponse:
        conn = await login_host(request, host_id)
        return await send_login(
            conn,
            _MCP_LOGIN_TIMEOUT_S,
            action="submit",
            login_id=login_id,
            callback_url=body.callback_url,
        )

    @router.delete("/hosts/{host_id}/mcp-servers/login/{login_id}")
    async def cancel_mcp_login(
        request: Request, host_id: str, login_id: LoginId
    ) -> McpLoginResponse:
        conn = await login_host(request, host_id)
        return await send_login(conn, _MCP_LOGIN_TIMEOUT_S, action="cancel", login_id=login_id)

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
    return await _request_host(
        host_registry,
        host_conn,
        host_conn.pending_mcp_tools,
        HostMcpToolsFrame(
            request_id=request_id,
            harness=harness,
            server=server,
            plugin=plugin,
            source_id=source_id,
        ),
        _MCP_TOOLS_TIMEOUT_S,
        "report MCP tools",
    )


async def _request_host(
    host_registry: HostRegistry,
    host_conn: HostConnection,
    pending: dict[str, asyncio.Future[_ResultFrame]],
    frame: HostMcpToolsFrame | HostMcpLoginFrame,
    timeout_s: float,
    action: str,
) -> _ResultFrame:
    future: asyncio.Future[_ResultFrame] = asyncio.get_running_loop().create_future()
    pending[frame.request_id] = future
    try:
        host_registry.send_text(host_conn, encode_host_frame(frame))
        return await asyncio.wait_for(future, timeout=timeout_s)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"host '{host_conn.host_id}' connection lost"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"host '{host_conn.host_id}' did not {action} within {timeout_s:.0f}s",
        ) from exc
    finally:
        pending.pop(frame.request_id, None)
