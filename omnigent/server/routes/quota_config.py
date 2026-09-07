"""Authenticated same-origin proxy for the quota controller's burst policy."""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore

_CONTROLLER_URL_ENV = "LLMQ_CONTROLLER_URL"
_CONTROLLER_TOKEN_FILE_ENV = "LLMQ_CONTROLLER_TOKEN_FILE"


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


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    if permission_store is None:
        return
    user_id = get_user_id(request, auth_provider)
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    if not await asyncio.to_thread(permission_store.is_admin, user_id):
        raise OmnigentError(
            "Admin privileges required to manage quota pacing",
            code=ErrorCode.FORBIDDEN,
        )


def _read_controller_token() -> str:
    raw_path = os.environ.get(_CONTROLLER_TOKEN_FILE_ENV, "")
    if not raw_path:
        raise OmnigentError(
            "Quota controller is not configured",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    path = Path(raw_path)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > 16_384
        ):
            raise OSError("unsafe token file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw_token = stream.read(16_385)
        if len(raw_token) > 16_384:
            raise OSError("oversized token file")
        token = raw_token.decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise OmnigentError(
            "Quota controller credentials are unavailable",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not token:
        raise OmnigentError(
            "Quota controller credentials are unavailable",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return token


def _controller_endpoint() -> tuple[str, str]:
    base_url = os.environ.get(_CONTROLLER_URL_ENV, "").rstrip("/")
    if urlsplit(base_url).scheme not in {"http", "https"}:
        raise OmnigentError(
            "Quota controller is not configured",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return f"{base_url}/v1/burst-policy", _read_controller_token()


async def _proxy(method: str, body: dict[str, object] | None = None) -> dict[str, Any]:
    endpoint, token = _controller_endpoint()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.request(
                method,
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OmnigentError(
            "Quota controller is unavailable",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    if not isinstance(payload, dict):
        raise OmnigentError("Invalid quota controller response", code=ErrorCode.INTERNAL_ERROR)
    return payload


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
        await _require_admin(request, auth_provider, permission_store)
        return _response(await _proxy("GET"))

    @router.patch("/quota/config")
    async def patch_quota_config(request: Request, body: QuotaConfigUpdate) -> QuotaConfigResponse:
        await _require_admin(request, auth_provider, permission_store)
        payload: dict[str, object] = {
            "idempotency_key": f"omnigent-{uuid.uuid4().hex}",
            "max_burst_factor": body.max_burst_factor,
            "adaptive_enabled": body.adaptive_enabled,
        }
        return _response(await _proxy("PUT", payload))

    return router


__all__ = ["create_quota_config_router"]
