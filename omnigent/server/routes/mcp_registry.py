"""Catalog and per-user account management for server-side MCP services."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.mcp_registry import McpOAuthHooks, McpRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.connections_base import ConnectionError, create_connection_router


class ConnectMcpToken(BaseModel):
    token: str = Field(min_length=1, max_length=16000, repr=False)


def create_mcp_registry_router(
    registry: McpRegistry, auth_provider: AuthProvider | None
) -> APIRouter:
    router = APIRouter()

    def user(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    def service(request: Request, service_id: str):
        try:
            return registry.service(service_id, user(request))
        except ConnectionError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/mcp-registry/services")
    async def catalog(request: Request, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        user_id = user(request)
        data = []
        for entry in registry.services.values():
            if not entry.permits(user_id):
                continue
            connected = entry.auth == "none"
            if entry.auth in {"github", "databricks"}:
                store = getattr(request.app.state, f"{entry.auth}_store", None)
                connected = (
                    store is not None and await asyncio.to_thread(store.get, user_id) is not None
                )
            elif entry.auth in {"oauth", "bearer"} and registry.store is not None:
                connected = (
                    await asyncio.to_thread(registry.store.get, user_id, f"mcp:{entry.id}")
                    is not None
                )
            data.append(
                {
                    "id": entry.id,
                    "title": entry.title,
                    "description": entry.description,
                    "auth": entry.auth,
                    "connected": connected,
                    "tools": entry.tools,
                    "connect_provider": f"mcp-{entry.id}" if entry.oauth else entry.auth,
                }
            )
        return {"data": data}

    @router.put("/mcp-registry/services/{service_id}/connection")
    async def connect_token(
        request: Request, service_id: str, body: ConnectMcpToken
    ) -> dict[str, bool]:
        entry = service(request, service_id)
        if entry.auth != "bearer" or registry.store is None:
            raise HTTPException(400, "This service does not accept a personal bearer token")
        if any(c.isspace() for c in body.token):
            raise HTTPException(400, "Bearer tokens must not contain whitespace")
        await asyncio.to_thread(
            registry.store.upsert,
            user(request),
            f"mcp:{entry.id}",
            secret={"access_token": body.token},
            metadata={},
        )
        return {"connected": True}

    @router.delete("/mcp-registry/services/{service_id}/connection")
    async def disconnect(request: Request, service_id: str) -> dict[str, bool]:
        entry = service(request, service_id)
        if entry.auth not in {"oauth", "bearer"} or registry.store is None:
            raise HTTPException(
                400, "Manage the shared provider connection in Sandbox Integrations"
            )
        user_id = user(request)
        await asyncio.to_thread(registry.store.delete, user_id, f"mcp:{entry.id}")
        await asyncio.to_thread(registry.store.delete, user_id, f"mcp-pending:{entry.id}")
        return {"disconnected": True}

    @router.post("/mcp-registry/services/{service_id}/test")
    async def test_connection(request: Request, service_id: str) -> dict[str, Any]:
        entry = service(request, service_id)
        try:
            tools = await registry.execute(entry.id, user(request), request.app.state)
        except ConnectionError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                502, "MCP connection failed. Check the account and server configuration."
            ) from exc
        return {"tools": [t.name for t in tools]}

    for entry in registry.services.values():
        if entry.oauth:
            router.include_router(
                create_connection_router(
                    McpOAuthHooks(registry, entry), auth_provider=auth_provider
                )
            )
    return router
