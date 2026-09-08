"""Shared transport for the LLMQ quota controller.

Both the burst-policy proxy (:mod:`omnigent.server.routes.quota_config`) and the
read-only status panel (:mod:`omnigent.server.routes.quota_status`) talk to the
same controller with the same credential, so the token read, endpoint
resolution, and request/response handling live here once.

The controller is a personal-infrastructure service reachable only over the
tailnet; ``LLMQ_CONTROLLER_URL`` names it and ``LLMQ_CONTROLLER_TOKEN_FILE``
names a mode-0600, single-link, caller-owned file holding its bearer token. The
strict ``fstat`` checks below are deliberate: the token grants write access to
fleet-wide pacing policy, so a token file that is a symlink, group/world
readable, hard-linked, or implausibly large is treated as tampered with rather
than trusted.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore

CONTROLLER_URL_ENV = "LLMQ_CONTROLLER_URL"
CONTROLLER_TOKEN_FILE_ENV = "LLMQ_CONTROLLER_TOKEN_FILE"

_MAX_TOKEN_BYTES = 16_384


async def require_quota_admin(
    request: Any,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Reject callers who may not read or write fleet quota state.

    Quota state is fleet-wide: window usage and workstream buckets describe
    every session on the instance, not just the caller's. Both the read and the
    write side are therefore admin-gated, matching the burst control the status
    panel sits beside.
    """
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


def read_controller_token() -> str:
    """Return the controller bearer token, or raise if it is not trustworthy."""
    raw_path = os.environ.get(CONTROLLER_TOKEN_FILE_ENV, "")
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
            or metadata.st_size > _MAX_TOKEN_BYTES
        ):
            raise OSError("unsafe token file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw_token = stream.read(_MAX_TOKEN_BYTES + 1)
        if len(raw_token) > _MAX_TOKEN_BYTES:
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


def controller_endpoint(path: str) -> tuple[str, str]:
    """Return the absolute controller URL for *path* and its bearer token."""
    base_url = os.environ.get(CONTROLLER_URL_ENV, "").rstrip("/")
    if urlsplit(base_url).scheme not in {"http", "https"}:
        raise OmnigentError(
            "Quota controller is not configured",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return f"{base_url}{path}", read_controller_token()


async def proxy(
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Call the controller and return its JSON object response."""
    endpoint, token = controller_endpoint(path)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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


__all__ = [
    "CONTROLLER_TOKEN_FILE_ENV",
    "CONTROLLER_URL_ENV",
    "controller_endpoint",
    "proxy",
    "read_controller_token",
    "require_quota_admin",
]
