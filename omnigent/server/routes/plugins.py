"""Owner-only installed plugin inventory over the host tunnel."""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.host.frames import (
    CAP_PLUGINS,
    HostPluginsFrame,
    HostPluginsResultFrame,
    encode_host_frame,
)
from omnigent.host.plugins import (
    MAX_PLUGIN_DESCRIPTION,
    MAX_PLUGIN_ITEMS,
    MAX_PLUGIN_NAME,
    MAX_PLUGINS,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

_PLUGINS_TIMEOUT_S = 15.0


class PluginSummary(BaseModel):
    """Installed Claude plugin metadata, without paths or executable configuration."""

    model_config = ConfigDict(strict=True)

    harness: Literal["claude"]
    name: str = Field(min_length=1, max_length=MAX_PLUGIN_NAME)
    marketplace: str = Field(max_length=MAX_PLUGIN_NAME)
    version: str | None = Field(default=None, max_length=MAX_PLUGIN_NAME)
    description: str | None = Field(default=None, max_length=MAX_PLUGIN_DESCRIPTION)
    enabled: bool
    skills: list[Annotated[str, Field(max_length=MAX_PLUGIN_NAME)]] = Field(
        max_length=MAX_PLUGIN_ITEMS
    )
    mcp_servers: list[Annotated[str, Field(max_length=MAX_PLUGIN_NAME)]] = Field(
        max_length=MAX_PLUGIN_ITEMS
    )
    has_hooks: bool
    has_commands: bool


class PluginsResponse(BaseModel):
    """Installed plugins reported by the host (Claude only)."""

    plugins: list[PluginSummary] = Field(max_length=MAX_PLUGINS)


def create_plugins_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the installed plugins route, mounted under ``/v1``."""
    router = APIRouter()

    @router.get("/hosts/{host_id}/plugins")
    async def get_plugins(request: Request, host_id: str) -> PluginsResponse:
        """List installed Claude plugins. The caller must own the host."""
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_PLUGINS not in conn.hello.capabilities:
            raise HTTPException(status_code=501, detail="update the host to see installed plugins")
        result = await request_host_plugins(host_registry=host_registry, host_conn=conn)
        if result.status != "ok" or result.plugins is None:
            raise HTTPException(status_code=502, detail="host plugin inventory failed")
        try:
            return PluginsResponse.model_validate({"plugins": result.plugins})
        except ValidationError as exc:
            # A malformed host reply is a host failure; don't echo its payload.
            raise HTTPException(
                status_code=502, detail="host sent a malformed plugin inventory reply"
            ) from exc

    return router


async def request_host_plugins(
    *, host_registry: HostRegistry, host_conn: HostConnection
) -> HostPluginsResultFrame:
    """Request plugins over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostPluginsResultFrame] = asyncio.get_running_loop().create_future()
    host_conn.pending_plugins[request_id] = future
    try:
        host_registry.send_text(
            host_conn,
            encode_host_frame(HostPluginsFrame(request_id=request_id)),
        )
        return await asyncio.wait_for(future, timeout=_PLUGINS_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502, detail=f"host '{host_conn.host_id}' connection lost"
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not report plugins "
                f"within {_PLUGINS_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_plugins.pop(request_id, None)
