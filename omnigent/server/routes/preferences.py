"""Explicit shared-preference reads and versioned edits; no implicit adoption."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request

from omnigent.preference_sync import (
    PREFERENCE_PAGE_SIZE,
    PreferenceMutation,
    PreferencePage,
    PreferenceResult,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.preference_store import PreferenceStore


def create_preferences_router(
    store: PreferenceStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/preferences/shared", response_model=PreferencePage)
    async def read_preferences(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=PREFERENCE_PAGE_SIZE)] = PREFERENCE_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
    ) -> PreferencePage:
        return await asyncio.to_thread(
            store.read, user_id=require_user(request, auth_provider), limit=limit, cursor=cursor
        )

    @router.post("/preferences/shared/operations", response_model=PreferenceResult)
    async def apply_preference(request: Request, body: PreferenceMutation) -> PreferenceResult:
        return await asyncio.to_thread(
            store.apply, body, user_id=require_user(request, auth_provider)
        )

    return router
