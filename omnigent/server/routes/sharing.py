"""Admin route for the server-wide session-sharing policy.

``GET /v1/sharing`` reports two independent settings and whether each is
editable here: the sharing *mode* (the tri-state tier + tier list) and whether
*public* (anyone-with-the-link) access may be granted. ``PUT /v1/sharing``
sets either or both (admin only), persisting an override file
(``<data_dir>/sharing_mode`` / ``<data_dir>/public_sharing``) that the grant
gate and ``GET /v1/info`` read per request.

Editing a setting is only possible when the server resolves it from its file
(the OSS default — ``create_app(sharing_mode=None, public_sharing=None)``). A
deployment that injects its own resolver — a static value or a callable such as
a Databricks SAFE flag — reports that setting as not editable and rejects
writes to it, since its policy is authoritative elsewhere.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider, SharingMode
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.sharing_settings import (
    write_public_sharing_override,
    write_sharing_mode_override,
)
from omnigent.stores.permission_store import PermissionStore

# The tiers offered to admins, most-permissive first (matches the UI order).
_TIERS: tuple[SharingMode, ...] = (
    SharingMode.ON,
    SharingMode.READ_ONLY,
    SharingMode.RESTRICTED_READ_ONLY,
    SharingMode.OFF,
)


class SetSharingRequest(BaseModel):
    """Body for ``PUT /v1/sharing``.

    Both fields are optional so an admin can update either setting
    independently; at least one must be present.
    """

    sharing_mode: str | None = None
    public_sharing: bool | None = None


def _state_response(request: Request) -> dict[str, Any]:
    """Shape the sharing-settings payload from live ``app.state`` — shared by
    GET and PUT so both reflect any override just written."""
    state = request.app.state
    mode: SharingMode = state.sharing_mode()
    return {
        "object": "sharing",
        "sharing_mode": mode.value,
        "editable": bool(getattr(state, "sharing_mode_writable", False)),
        "options": [tier.value for tier in _TIERS],
        "public_sharing_enabled": bool(state.public_sharing()),
        "public_sharing_editable": bool(getattr(state, "public_sharing_writable", False)),
    }


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Verify the caller is an admin, mirroring the default-policies gate.

    Single-user mode (no permission store) skips the check. Multi-user mode
    raises 401 if unauthenticated or 403 if the user is not an admin.
    """
    if permission_store is None:
        return
    user_id = get_user_id(request, auth_provider)
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
    if not is_admin:
        raise OmnigentError(
            "Admin privileges required to manage sharing settings",
            code=ErrorCode.FORBIDDEN,
        )


def create_sharing_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the admin sharing router (mounted under ``/v1``)."""
    router = APIRouter()

    @router.get("/sharing")
    async def get_sharing(request: Request) -> dict[str, Any]:
        """Report both settings, whether each is editable here, and the tiers."""
        await _require_admin(request, auth_provider, permission_store)
        return _state_response(request)

    @router.put("/sharing")
    async def set_sharing(request: Request, body: SetSharingRequest) -> dict[str, Any]:
        """Set the sharing mode and/or public-access setting (admin only).

        Updates only the fields present in the body; requires at least one.
        Rejects an unknown mode value with 400 (no fail-open coercion — an admin
        setting a value should learn about a typo). Rejects a write to a setting
        the deployment manages itself (not file-backed) with 403.
        """
        await _require_admin(request, auth_provider, permission_store)
        state = request.app.state
        if body.sharing_mode is None and body.public_sharing is None:
            raise OmnigentError(
                "No sharing settings to update.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Validate AND authorize both fields before writing either, so a request
        # updating both never persists one and then rejects the other (a partial
        # apply — reachable only when a deployment makes exactly one setting
        # file-backed and the other a managed callable).
        mode: SharingMode | None = None
        if body.sharing_mode is not None:
            if not getattr(state, "sharing_mode_writable", False):
                raise OmnigentError(
                    "Sharing mode is managed by this deployment and cannot be changed here.",
                    code=ErrorCode.FORBIDDEN,
                )
            try:
                mode = SharingMode(body.sharing_mode.strip().lower())
            except ValueError as exc:
                raise OmnigentError(
                    f"Unknown sharing mode {body.sharing_mode!r}. Expected one of: "
                    + ", ".join(tier.value for tier in _TIERS)
                    + ".",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        if body.public_sharing is not None and not getattr(
            state, "public_sharing_writable", False
        ):
            raise OmnigentError(
                "Public access is managed by this deployment and cannot be changed here.",
                code=ErrorCode.FORBIDDEN,
            )
        # All checks passed — apply the writes.
        if mode is not None:
            await asyncio.to_thread(write_sharing_mode_override, mode)
        if body.public_sharing is not None:
            await asyncio.to_thread(write_public_sharing_override, body.public_sharing)
        return _state_response(request)

    return router
