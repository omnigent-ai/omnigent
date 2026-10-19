"""Explicit shared-preference reads and versioned edits; no implicit adoption."""

import asyncio

from fastapi import APIRouter, Request

from omnigent.preference_sync import PreferenceMutation, PreferenceResult, PreferenceSnapshot
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.preference_store import PreferenceStore


def create_preferences_router(
    store: PreferenceStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/preferences/shared", response_model=PreferenceSnapshot)
    async def read_preferences(request: Request) -> PreferenceSnapshot:
        return await asyncio.to_thread(store.read, user_id=require_user(request, auth_provider))

    @router.post("/preferences/shared/operations", response_model=PreferenceResult)
    async def apply_preference(request: Request, body: PreferenceMutation) -> PreferenceResult:
        return await asyncio.to_thread(
            store.apply, body, user_id=require_user(request, auth_provider)
        )

    return router
