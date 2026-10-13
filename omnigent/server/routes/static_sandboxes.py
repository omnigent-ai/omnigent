"""Static sandboxes: the user's own long-lived sandboxes, saved as New Session hosts.

Mounted under ``/v1`` when a configured provider could offer static
sandboxes. The saved list lives with each provider; these routes pass through
to it as the caller, and only removal and wake also act on the sandbox's
Omnigent host. Nothing here writes an Omnigent table except the host row a
wake (re)arms.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, TypeVar

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.sandboxes.base import StaticSandboxProvider
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.vm_hosts import (
    ensure_vm_host,
    shim_error,
    static_launcher,
    static_provider_candidates,
    stop_vm_host,
)

SandboxStatus = Literal["running", "paused", "gone"]
_T = TypeVar("_T")


class AvailableSandbox(BaseModel):
    """One of the caller's sandboxes that can be added as a static sandbox."""

    provider: str
    sandbox_id: str
    status: str
    repo: str = ""
    last_activity_at: str | None = None


class StaticSandbox(BaseModel):
    """One saved static sandbox, with the sandbox's live status."""

    provider: str
    sandbox_id: str
    label: str
    status: SandboxStatus
    repo: str = ""
    last_activity_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class StaticSandboxList(BaseModel):
    """``GET /v1/static-sandboxes``: the caller's saved entries."""

    static_sandboxes: list[StaticSandbox] = Field(default_factory=list)


class AvailableSandboxList(BaseModel):
    """``GET /v1/static-sandboxes/available``: every sandbox the caller could add."""

    available_sandboxes: list[AvailableSandbox] = Field(default_factory=list)


class StaticSandboxPut(BaseModel):
    """Add or rename body; an empty label falls back to the sandbox id."""

    label: str = Field(default="", max_length=64)


class StaticSandboxWake(BaseModel):
    """Result of waking a static sandbox: its host is online."""

    provider: str
    sandbox_id: str
    host_id: str


def create_static_sandboxes_router(
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the ``/static-sandboxes`` router.

    :param auth_provider: Resolves the caller; ``None`` on single-user servers.
    """
    router = APIRouter()

    def _user(request: Request) -> str:
        user = require_user(request, auth_provider)
        if user is None:
            raise OmnigentError(
                "static sandboxes need a signed-in user", code=ErrorCode.INVALID_INPUT
            )
        return user

    async def _call(
        request: Request,
        owner: str,
        provider: str,
        action: str,
        call: Callable[[StaticSandboxProvider], _T],
    ) -> _T:
        launcher = static_launcher(request.app.state.sandbox_config, owner, provider).sandboxes
        try:
            return await asyncio.to_thread(call, launcher)
        except OmnigentError:
            raise
        except Exception as exc:
            raise shim_error(exc, action=action) from exc

    async def _each_provider(
        request: Request, action: str, call: Callable[[StaticSandboxProvider], list[_T]]
    ) -> list[tuple[str, _T]]:
        """*call*'s items from every provider offering static sandboxes, tagged by provider."""
        owner = _user(request)
        found: list[tuple[str, _T]] = []
        for provider in static_provider_candidates(request.app.state.sandbox_config):
            try:
                items = await _call(request, owner, provider, action, call)
            except OmnigentError as exc:
                # A provider that turns out not to offer static sandboxes lists nothing.
                if exc.code != ErrorCode.INVALID_INPUT:
                    raise
                continue
            found.extend((provider, item) for item in items)
        return found

    @router.get("/static-sandboxes", response_model=StaticSandboxList)
    async def list_static_sandboxes(request: Request) -> StaticSandboxList:
        """The caller's static sandboxes, with each sandbox's live status."""
        entries = await _each_provider(
            request, "list your static sandboxes", lambda p: p.list_static_sandboxes()
        )
        return StaticSandboxList(
            static_sandboxes=[
                StaticSandbox.model_validate({**entry, "provider": provider})
                for provider, entry in entries
            ]
        )

    @router.get("/static-sandboxes/available", response_model=AvailableSandboxList)
    async def list_available_sandboxes(request: Request) -> AvailableSandboxList:
        """Every sandbox the caller owns that can be added as a static sandbox."""
        found = await _each_provider(
            request, "list your sandboxes", lambda p: p.list_available_sandboxes()
        )
        return AvailableSandboxList(
            available_sandboxes=[
                AvailableSandbox.model_validate({**item, "provider": provider})
                for provider, item in found
            ]
        )

    @router.put(
        "/static-sandboxes/{provider}/{sandbox_id}",
        response_model=StaticSandbox,
        dependencies=[Depends(require_trusted_origin)],
    )
    async def put_static_sandbox(
        request: Request, provider: str, sandbox_id: str, body: StaticSandboxPut
    ) -> StaticSandbox:
        """Add one of the caller's sandboxes as a static sandbox, or rename it."""
        label = " ".join(body.label.split()) or sandbox_id
        entry = await _call(
            request,
            _user(request),
            provider,
            f"save {sandbox_id}",
            lambda p: p.put_static_sandbox(sandbox_id, label),
        )
        return StaticSandbox.model_validate({**entry, "provider": provider})

    @router.delete(
        "/static-sandboxes/{provider}/{sandbox_id}",
        status_code=204,
        dependencies=[Depends(require_trusted_origin)],
    )
    async def delete_static_sandbox(request: Request, provider: str, sandbox_id: str) -> Response:
        """Remove the entry and stop its host. The sandbox and its files stay."""
        removed = await stop_vm_host(
            owner=_user(request),
            provider=provider,
            sandbox_id=sandbox_id,
            sandbox_config=request.app.state.sandbox_config,
            host_store=request.app.state.host_store,
        )
        if not removed:
            raise shim_error(_NotFound(), action=f"remove {sandbox_id}")
        return Response(status_code=204)

    @router.post(
        "/static-sandboxes/{provider}/{sandbox_id}/wake",
        response_model=StaticSandboxWake,
        dependencies=[Depends(require_trusted_origin)],
    )
    async def wake_static_sandbox(
        request: Request, provider: str, sandbox_id: str
    ) -> StaticSandboxWake:
        """Wake the sandbox and start its host ahead of a New Session's Start."""
        host_id = await ensure_vm_host(
            owner=_user(request),
            provider=provider,
            sandbox_id=sandbox_id,
            sandbox_config=request.app.state.sandbox_config,
            host_store=request.app.state.host_store,
        )
        return StaticSandboxWake(provider=provider, sandbox_id=sandbox_id, host_id=host_id)

    return router


class _NotFound(Exception):
    status_code = 404
