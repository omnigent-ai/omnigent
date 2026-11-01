"""Authenticated discovery of provider-owned sandbox profiles."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from omnigent.onboarding.sandboxes.profiles import SandboxProfileSummary
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user


class SandboxProfilesResponse(BaseModel):
    profiles: list[SandboxProfileSummary]
    managed_providers: list[str]
    next_cursor: str | None


def create_sandbox_profiles_router(*, auth_provider: AuthProvider | None) -> APIRouter:
    router = APIRouter()

    @router.get("/sandbox-profiles", response_model=SandboxProfilesResponse)
    async def list_profiles(
        request: Request,
        response: Response,
        cursor: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=100, ge=1, le=100),
        provider: str | None = Query(default=None, min_length=1, max_length=32),
        search: str = Query(default="", max_length=200),
        profile_id: str | None = Query(default=None, max_length=64),
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        response.headers["Cache-Control"] = "private, no-store"
        service = getattr(request.app.state, "sandbox_profiles", None)
        if service is None:
            return {"profiles": [], "managed_providers": [], "next_cursor": None}
        profiles, next_cursor = await asyncio.to_thread(
            service.list_profiles,
            cursor=cursor,
            limit=limit,
            provider=provider,
            user_id=user_id,
            search=search,
            profile_id=profile_id,
        )
        managed_providers = await asyncio.to_thread(service.managed_providers, provider)
        return {
            "profiles": profiles,
            "managed_providers": managed_providers,
            "next_cursor": next_cursor,
        }

    return router
