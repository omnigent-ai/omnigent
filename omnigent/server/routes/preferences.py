"""Explicit shared-preference reads and versioned edits; no implicit adoption."""

import asyncio
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Query, Request
from pydantic import TypeAdapter, ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.preference_sync import (
    PREFERENCE_PAGE_SIZE,
    Name,
    PreferenceMutation,
    PreferencePage,
    PreferenceResult,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.preference_store import PreferenceStore

_USER_ID = TypeAdapter(Name)


def create_preferences_router(
    store: PreferenceStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    def user_id(request: Request) -> str | None:
        authenticated = require_user(request, auth_provider)
        if authenticated is None:
            return None
        try:
            return _USER_ID.validate_python(authenticated)
        except ValidationError as exc:
            raise OmnigentError(
                "Invalid preference user ID", code=ErrorCode.INVALID_INPUT
            ) from exc

    @router.get("/preferences/shared", response_model=PreferencePage)
    async def read_preferences(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=PREFERENCE_PAGE_SIZE)] = PREFERENCE_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
        protocol_version: Annotated[int, Query(ge=1, le=2)] = 1,
    ) -> PreferencePage:
        return await asyncio.to_thread(
            store.read,
            user_id=user_id(request),
            limit=limit,
            cursor=cursor,
            protocol_version=cast(Literal[1, 2], protocol_version),
        )

    @router.post("/preferences/shared/operations", response_model=PreferenceResult)
    async def apply_preference(request: Request, body: PreferenceMutation) -> PreferenceResult:
        return await asyncio.to_thread(store.apply, body, user_id=user_id(request))

    return router
