"""Routes for standalone, owner-scoped MCP servers (``/v1/mcp-servers``).

Standalone MCP servers are reusable connections a user registers once and
references when creating agents — instead of re-typing url/headers into
every create-agent form. This router exposes owner-scoped CRUD plus a
**verify** endpoint that actually connects to the server and returns its
tool list, so the user can confirm a connection (and see what it offers)
before saving or selecting it.

Mounted with ``prefix="/v1"`` → final paths are ``/v1/mcp-servers*``.

Security notes:
- ``headers``/``env`` are secret-bearing. They are accepted on write and
  used for verify/agent-baking, but never returned verbatim — responses
  expose only the *keys* (``header_keys`` / ``env_keys``).
- Verify connects from the server to an operator-supplied URL (same
  posture as the existing server-side MCP pool). In a hardened
  multi-tenant deployment this warrants an egress allowlist / SSRF guard.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator

from omnigent.entities import McpServer
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_user
from omnigent.spec.types import MCPServerConfig
from omnigent.stores.mcp_server_store import McpServerStore

_logger = logging.getLogger(__name__)

# Same name shape as session MCP servers (schemas._MCP_SERVER_NAME_RE):
# safe as a YAML filename and tool-namespace prefix.
_MCP_SERVER_NAME_RE = r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$"

# Hard cap on the verify connect so a hung/slow server can't pin a worker.
_VERIFY_TIMEOUT_S = 25.0


class _MCPFields(BaseModel):
    """Shared transport-shape validation for MCP write/verify bodies."""

    transport: Literal["http", "stdio"]
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list, max_length=64)
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> _MCPFields:
        """Enforce the same transport shape as the agent spec parser."""
        if self.transport == "http":
            if not self.url:
                raise ValueError("url is required when transport is 'http'")
            if not (self.url.startswith("http://") or self.url.startswith("https://")):
                raise ValueError("url must start with http:// or https://")
            if self.command:
                raise ValueError("command is not allowed when transport is 'http'")
            if self.args:
                raise ValueError("args are not allowed when transport is 'http'")
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("command is required when transport is 'stdio'")
            if self.url:
                raise ValueError("url is not allowed when transport is 'stdio'")
        return self


class MCPServerWriteRequest(_MCPFields):
    """Body for creating or updating a standalone MCP server."""

    name: str = Field(min_length=1, max_length=128, pattern=_MCP_SERVER_NAME_RE)
    description: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def _reject_dot_names(cls, value: str) -> str:
        """Reject names that would make unsafe YAML filenames."""
        if value in {".", ".."}:
            raise ValueError("name cannot be '.' or '..'")
        return value


class MCPVerifyRequest(_MCPFields):
    """Body for an ad-hoc connection verify (name optional)."""

    name: str | None = Field(default=None, max_length=128)
    timeout: int | None = Field(default=None, ge=1, le=120)


class MCPToolInfo(BaseModel):
    """One tool discovered on an MCP server."""

    name: str
    description: str | None = None


class MCPVerifyResponse(BaseModel):
    """Result of a connection verify: ``ok`` + the discovered tools."""

    ok: bool
    tools: list[MCPToolInfo] = Field(default_factory=list)
    error: str | None = None


class MCPServerObject(BaseModel):
    """Safe wire shape of a stored MCP server (no secret values)."""

    id: str
    name: str
    transport: str
    description: str | None = None
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    header_keys: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    created_at: int
    updated_at: int | None = None


def _to_object(server: McpServer) -> MCPServerObject:
    """Project an :class:`McpServer` to its secret-free API shape."""
    return MCPServerObject(
        id=server.id,
        name=server.name,
        transport=server.transport,
        description=server.description,
        url=server.url,
        command=server.command,
        args=list(server.args),
        header_keys=sorted(server.headers),
        env_keys=sorted(server.env),
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


class MCPServerFullConfig(BaseModel):
    """Full config of a stored server, *including* secret values.

    Returned only to the owner, only via the explicit ``/config`` route.
    The create-agent flow uses it to bake a selected preconfigured server
    into a new agent bundle (which the browser builds client-side and
    uploads) — the same trust boundary as typing the secrets into the
    create-agent form directly.
    """

    id: str
    name: str
    transport: str
    description: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


def _to_full_config(server: McpServer) -> MCPServerFullConfig:
    """Project an :class:`McpServer` to its full (secret-bearing) shape."""
    return MCPServerFullConfig(
        id=server.id,
        name=server.name,
        transport=server.transport,
        description=server.description,
        url=server.url,
        headers=dict(server.headers),
        command=server.command,
        args=list(server.args),
        env=dict(server.env),
    )


async def _verify_config(config: MCPServerConfig) -> MCPVerifyResponse:
    """Connect to an MCP server and return its tools (or the error)."""
    # Imported lazily: pulls the MCP client stack, which the route module
    # otherwise wouldn't need at import time.
    from omnigent.tools.mcp import McpServerConnection

    conn = McpServerConnection(config=config)
    try:
        tools = await asyncio.wait_for(conn.connect(), timeout=_VERIFY_TIMEOUT_S)
        return MCPVerifyResponse(
            ok=True,
            tools=[
                MCPToolInfo(name=t.name, description=getattr(t, "description", None))
                for t in tools
            ],
        )
    except TimeoutError:
        return MCPVerifyResponse(
            ok=False, error=f"Connection timed out after {int(_VERIFY_TIMEOUT_S)}s"
        )
    except Exception as exc:  # noqa: BLE001 — surface any connect failure to the user
        return MCPVerifyResponse(ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 — best-effort teardown
            _logger.debug("MCP verify connection close failed", exc_info=True)


def _config_from_fields(fields: _MCPFields, name: str, timeout: int | None) -> MCPServerConfig:
    """Build an :class:`MCPServerConfig` from a validated request body."""
    if fields.transport == "http":
        return MCPServerConfig(
            name=name,
            transport="http",
            url=fields.url,
            headers=dict(fields.headers),
            timeout=timeout,
        )
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=fields.command,
        args=list(fields.args),
        env=dict(fields.env),
        timeout=timeout,
    )


def create_mcp_servers_router(
    mcp_server_store: McpServerStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the standalone MCP servers router (owner-scoped CRUD + verify).

    :param mcp_server_store: Persistence for standalone MCP servers.
    :param auth_provider: Optional auth provider; when set the caller
        must be authenticated and rows are scoped to their identity.
    :returns: A FastAPI router mounted under ``/v1``.
    """
    router = APIRouter()

    def _owner(request: Request) -> str:
        """Resolve the caller's owner key (``"local"`` in single-user)."""
        return get_user_id(request, auth_provider) or RESERVED_USER_LOCAL

    async def _owned(request: Request, server_id: str) -> McpServer:
        """Fetch a server the caller owns, else 404 (hides others' rows)."""
        owner = _owner(request)
        server = await asyncio.to_thread(mcp_server_store.get, server_id)
        if server is None or server.owner != owner:
            raise OmnigentError("MCP server not found", code=ErrorCode.NOT_FOUND)
        return server

    # ── Static paths before /{server_id} so they aren't captured by it ──

    @router.get("/mcp-servers/mine")
    async def list_my_mcp_servers(request: Request) -> dict[str, Any]:
        """List the caller's standalone MCP servers (no secret values)."""
        require_user(request, auth_provider)
        owner = _owner(request)
        servers = await asyncio.to_thread(mcp_server_store.list_for_owner, owner)
        return {"object": "list", "data": [_to_object(s).model_dump() for s in servers]}

    @router.post("/mcp-servers/verify")
    async def verify_mcp_server(request: Request, body: MCPVerifyRequest) -> MCPVerifyResponse:
        """Connect to an ad-hoc MCP config and return its tool list."""
        require_user(request, auth_provider)
        config = _config_from_fields(body, body.name or "verify", body.timeout)
        return await _verify_config(config)

    @router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
    async def create_mcp_server(
        request: Request, body: MCPServerWriteRequest
    ) -> MCPServerObject:
        """Register a new standalone MCP server for the caller."""
        require_user(request, auth_provider)
        owner = _owner(request)
        existing = await asyncio.to_thread(mcp_server_store.get_by_name, owner, body.name)
        if existing is not None:
            raise OmnigentError(
                f"MCP server {body.name!r} already exists",
                code=ErrorCode.CONFLICT,
            )
        server_id = f"mcp_{secrets.token_hex(12)}"
        created = await asyncio.to_thread(
            mcp_server_store.create,
            server_id,
            owner,
            body.name,
            body.transport,
            url=body.url,
            headers=body.headers,
            command=body.command,
            args=body.args,
            env=body.env,
            description=body.description,
        )
        return _to_object(created)

    @router.get("/mcp-servers/{server_id}")
    async def get_mcp_server(request: Request, server_id: str) -> MCPServerObject:
        """Fetch one of the caller's MCP servers (no secret values)."""
        require_user(request, auth_provider)
        return _to_object(await _owned(request, server_id))

    @router.get("/mcp-servers/{server_id}/config")
    async def get_mcp_server_config(request: Request, server_id: str) -> MCPServerFullConfig:
        """Fetch a server's full config *with* secrets (owner only).

        Used by the create-agent flow to bake a selected preconfigured
        server into a new agent bundle. 404 hides servers the caller
        doesn't own.
        """
        require_user(request, auth_provider)
        return _to_full_config(await _owned(request, server_id))

    @router.put("/mcp-servers/{server_id}")
    async def update_mcp_server(
        request: Request, server_id: str, body: MCPServerWriteRequest
    ) -> MCPServerObject:
        """Replace one of the caller's MCP servers."""
        require_user(request, auth_provider)
        owner = _owner(request)
        server = await _owned(request, server_id)
        if body.name != server.name:
            clash = await asyncio.to_thread(mcp_server_store.get_by_name, owner, body.name)
            if clash is not None:
                raise OmnigentError(
                    f"MCP server {body.name!r} already exists",
                    code=ErrorCode.CONFLICT,
                )
        updated = await asyncio.to_thread(
            mcp_server_store.update,
            server_id,
            name=body.name,
            transport=body.transport,
            url=body.url,
            headers=body.headers,
            command=body.command,
            args=body.args,
            env=body.env,
            description=body.description,
        )
        if updated is None:
            raise OmnigentError("MCP server not found", code=ErrorCode.NOT_FOUND)
        return _to_object(updated)

    @router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_mcp_server(request: Request, server_id: str) -> Response:
        """Delete one of the caller's MCP servers."""
        require_user(request, auth_provider)
        await _owned(request, server_id)
        await asyncio.to_thread(mcp_server_store.delete, server_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/mcp-servers/{server_id}/verify")
    async def verify_saved_mcp_server(request: Request, server_id: str) -> MCPVerifyResponse:
        """Verify a stored server using its saved (secret) config."""
        require_user(request, auth_provider)
        server = await _owned(request, server_id)
        config = MCPServerConfig(
            name=server.name,
            transport=server.transport,  # type: ignore[arg-type]
            url=server.url,
            headers=dict(server.headers),
            command=server.command,
            args=list(server.args),
            env=dict(server.env),
        )
        return await _verify_config(config)

    return router
