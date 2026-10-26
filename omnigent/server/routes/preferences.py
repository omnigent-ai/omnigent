"""Explicit shared-preference reads and versioned edits; no implicit adoption."""

from functools import partial
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import TypeAdapter, ValidationError

from omnigent.configuration_profile import ConfigurationSave, ConfigurationSnapshot
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.preference_sync import (
    PREFERENCE_PAGE_SIZE,
    Name,
    PreferenceMutation,
    PreferencePage,
    PreferenceResult,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.preference_requests import (
    DEFAULT_PREFERENCE_POLICY,
    MAX_PREFERENCE_REQUEST_BYTES,
    PreferencePolicy,
    read_preference_payload,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.preference_store import PreferenceStore

_USER_ID = TypeAdapter(Name)


def create_preferences_router(
    store: PreferenceStore,
    auth_provider: AuthProvider | None = None,
    *,
    policy: PreferencePolicy = DEFAULT_PREFERENCE_POLICY,
) -> APIRouter:
    router = APIRouter()

    def user_id(request: Request) -> str | None:
        policy.require_enabled()
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
    ) -> PreferencePage:
        authenticated = user_id(request)
        return await policy.run(
            authenticated, partial(store.read, user_id=authenticated, limit=limit, cursor=cursor)
        )

    @router.post("/preferences/shared/operations", response_model=PreferenceResult)
    async def apply_preference(request: Request) -> PreferenceResult:
        authenticated = user_id(request)
        payload = await read_preference_payload(request, MAX_PREFERENCE_REQUEST_BYTES)
        try:
            body = PreferenceMutation.model_validate_json(payload)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid preference edit") from exc
        return await policy.run(authenticated, partial(store.apply, body, user_id=authenticated))

    @router.get("/preferences/configuration", response_model=ConfigurationSnapshot)
    async def read_configuration(request: Request) -> ConfigurationSnapshot:
        authenticated = user_id(request)
        return await policy.run(
            authenticated, partial(store.read_configuration, user_id=authenticated)
        )

    @router.put("/preferences/configuration", response_model=ConfigurationSnapshot)
    async def save_configuration(request: Request) -> ConfigurationSnapshot:
        authenticated = user_id(request)
        payload = await read_preference_payload(request, 16000)
        try:
            body = ConfigurationSave.model_validate_json(payload)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid configuration snapshot") from exc
        return await policy.run(
            authenticated, partial(store.save_configuration, body, user_id=authenticated)
        )

    return router
