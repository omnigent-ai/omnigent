"""Authenticated same-origin proxy for the quota controller's burst policy."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._quota_controller import proxy, require_quota_admin
from omnigent.stores.permission_store import PermissionStore

_BURST_POLICY_PATH = "/v1/burst-policy"


class QuotaConfigUpdate(BaseModel):
    """Browser-writable subset of the controller's burst policy."""

    max_burst_factor: float | None = Field(default=None, ge=1, allow_inf_nan=False)
    adaptive_enabled: bool


class QuotaConfigResponse(QuotaConfigUpdate):
    """Authoritative controller policy returned to the browser."""

    initial_burst_factor: float = Field(ge=1, allow_inf_nan=False)
    current_burst_factors: dict[str, Annotated[float, Field(ge=1, allow_inf_nan=False)]] = Field(
        default_factory=dict
    )


def _response(payload: dict[str, Any]) -> QuotaConfigResponse:
    try:
        return QuotaConfigResponse.model_validate(payload)
    except ValueError as exc:
        raise OmnigentError(
            "Invalid quota controller response",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc


def create_quota_config_router(
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Create the browser-facing burst-policy proxy."""
    router = APIRouter()

    @router.get("/quota/config")
    async def get_quota_config(request: Request) -> QuotaConfigResponse:
        await require_quota_admin(request, auth_provider, permission_store)
        return _response(await proxy("GET", _BURST_POLICY_PATH))

    @router.patch("/quota/config")
    async def patch_quota_config(request: Request, body: QuotaConfigUpdate) -> QuotaConfigResponse:
        await require_quota_admin(request, auth_provider, permission_store)
        payload: dict[str, object] = {
            "idempotency_key": f"omnigent-{uuid.uuid4().hex}",
            "max_burst_factor": body.max_burst_factor,
            "adaptive_enabled": body.adaptive_enabled,
        }
        return _response(await proxy("PUT", _BURST_POLICY_PATH, payload))

    return router


__all__ = ["create_quota_config_router"]
