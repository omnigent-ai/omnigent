"""Authenticated discovery and administrator editing of sandbox profiles."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, local_single_user_enabled
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.sandbox_profiles import SandboxProfileCatalog, SandboxProfileWrite
from omnigent.stores.permission_store import PermissionStore


def create_sandbox_profiles_router(
    *, auth_provider: AuthProvider | None, permission_store: PermissionStore | None
) -> APIRouter:
    router = APIRouter()

    async def can_manage(request: Request) -> bool:
        user_id = require_user(request, auth_provider)
        if auth_provider is None or (
            local_single_user_enabled() and user_id == RESERVED_USER_LOCAL
        ):
            return True
        return bool(
            permission_store is not None
            and user_id is not None
            and await asyncio.to_thread(permission_store.is_admin, user_id)
        )

    def catalog(request: Request) -> SandboxProfileCatalog:
        service = getattr(request.app.state, "sandbox_profiles", None)
        if service is None:
            raise HTTPException(
                status_code=422, detail="Managed sandbox profiles are not configured"
            )
        return service

    @router.get("/sandbox-profiles")
    async def list_profiles(
        request: Request,
        response: Response,
        cursor: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        admin = await can_manage(request)
        response.headers["Cache-Control"] = "private, no-store"
        service = getattr(request.app.state, "sandbox_profiles", None)
        if service is None:
            return {"profiles": [], "providers": [], "can_manage": False, "next_cursor": None}
        profiles, next_cursor = await asyncio.to_thread(
            service.list_profiles,
            include_disabled=admin,
            cursor=cursor,
            limit=limit,
            user_id=require_user(request, auth_provider),
        )
        providers = await asyncio.to_thread(service.providers)
        managed_providers = await asyncio.to_thread(service.managed_providers)
        return {
            "profiles": profiles,
            "providers": providers,
            "can_manage": admin and bool(providers),
            "managed_providers": managed_providers,
            "next_cursor": next_cursor,
        }

    @router.put("/sandbox-profiles/{profile_id}")
    async def put_profile(
        request: Request, profile_id: str, body: SandboxProfileWrite
    ) -> dict[str, Any]:
        if not await can_manage(request):
            raise HTTPException(
                status_code=403, detail="Admin privileges required to manage sandbox profiles"
            )
        return await asyncio.to_thread(catalog(request).put, profile_id, body)

    @router.delete("/sandbox-profiles/{profile_id}")
    async def disable_profile(request: Request, profile_id: str) -> dict[str, Any]:
        if not await can_manage(request):
            raise HTTPException(
                status_code=403, detail="Admin privileges required to manage sandbox profiles"
            )
        return await asyncio.to_thread(catalog(request).disable, profile_id)

    return router
