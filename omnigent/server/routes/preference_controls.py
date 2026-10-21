"""Owner-only host controls for adoption, import review and local overrides."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from omnigent.host.frames import (
    CAP_PREFERENCES,
    HostPreferencesFrame,
    HostPreferencesResultFrame,
    encode_host_frame,
)
from omnigent.preference_controls import PREFERENCE_CONTROL, PreferenceControlResult
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore
from omnigent.stores.preference_store import PreferenceStore

MAX_CONTROL_BYTES = 8 * 1024 * 1024
CONTROL_TIMEOUT_SECONDS = 15


def create_preference_controls_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    preference_store: PreferenceStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/hosts/{host_id}/preferences", response_model=PreferenceControlResult)
    async def control(request: Request, host_id: str) -> PreferenceControlResult:
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_PREFERENCES not in conn.hello.capabilities:
            raise HTTPException(501, "Update this host to manage shared preferences")
        if len(conn.pending_preferences) >= 4:
            raise HTTPException(429, "Preference actions are busy; retry shortly")
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > MAX_CONTROL_BYTES:
                raise HTTPException(413, "Preference action exceeds the request limit")
            payload.extend(chunk)
        try:
            action = PREFERENCE_CONTROL.validate_json(payload)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid preference action") from exc
        identity = preference_store.identity(user_id)
        if len(conn.pending_preferences) >= 4:
            raise HTTPException(429, "Preference actions are busy; retry shortly")
        request_id = secrets.token_hex(16)
        future: asyncio.Future[HostPreferencesResultFrame] = (
            asyncio.get_running_loop().create_future()
        )
        conn.pending_preferences[request_id] = future
        try:
            host_registry.send_text(
                conn, encode_host_frame(HostPreferencesFrame(request_id, identity, action))
            )
            reply = await asyncio.wait_for(future, CONTROL_TIMEOUT_SECONDS)
        except (ConnectionError, TimeoutError) as exc:
            raise HTTPException(
                503,
                "Host did not finish the preference action. "
                "Refresh status before retrying a write.",
            ) from exc
        finally:
            conn.pending_preferences.pop(request_id, None)
        result = reply.result
        if result is None:
            raise HTTPException(
                502,
                "Preference action failed. "
                "Check host login, pending edits and connection, then retry.",
            )
        # A stale or malformed host must not expose another namespace's local state.
        if (
            (result.shared is not None and result.shared.identity != identity)
            or (result.preview is not None and result.preview.identity != identity)
            or any(operation.identity != identity for operation in result.pending)
        ):
            raise HTTPException(502, "Host preference identity changed; reconnect the host")
        return result

    return router
