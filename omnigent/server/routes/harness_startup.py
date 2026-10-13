"""Owner-only access to a connected host's read-only launch defaults."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException, Request

from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.frames import CAP_HARNESS_STARTUP, HostHarnessStartupFrame, encode_host_frame
from omnigent.host.harness_startup import SUPPORTED_HARNESSES, HarnessStartup
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.stores.host_store import HostStore

_STARTUP_TIMEOUT_S = 15.0


def create_harness_startup_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the launch settings route, mounted under /v1."""
    router = APIRouter()

    @router.get("/hosts/{host_id}/harnesses/{harness}/startup")
    async def get_harness_startup(request: Request, host_id: str, harness: str) -> HarnessStartup:
        """Read host launch defaults, configured invocation, and env-wrapper settings."""
        user_id = require_user(request, auth_provider)
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
        )
        harness = canonicalize_harness(harness) or harness
        if harness not in SUPPORTED_HARNESSES:
            raise HTTPException(404, "launch settings are not reported for this harness")
        conn = host_registry.get(host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_HARNESS_STARTUP not in conn.hello.capabilities:
            raise HTTPException(501, "update the host to read launch settings")
        request_id = secrets.token_hex(8)
        future: asyncio.Future[HarnessStartup | None] = asyncio.get_running_loop().create_future()
        conn.pending_harness_startup[request_id] = future
        try:
            host_registry.send_text(
                conn, encode_host_frame(HostHarnessStartupFrame(request_id, harness))
            )
            result = await asyncio.wait_for(future, timeout=_STARTUP_TIMEOUT_S)
        except ConnectionError as exc:
            raise HTTPException(502, "host connection lost") from exc
        except TimeoutError as exc:
            raise HTTPException(504, "host launch settings request timed out") from exc
        finally:
            conn.pending_harness_startup.pop(request_id, None)
        if result is None:
            raise HTTPException(502, "host could not report launch settings")
        return result

    return router
