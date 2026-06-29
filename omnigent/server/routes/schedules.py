"""REST routes for schedules (loops & monitors).

CRUD over the ``schedules`` table at ``/schedules[/{schedule_id}]``, scoped to
a conversation (``GET`` requires ``?conversation_id=``). Powers the
Loops/Monitors management UI. Requires authentication in multi-user mode.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import generate_schedule_id
from omnigent.entities.schedule import SCHEDULE_KINDS, Schedule
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.schedule_store import ScheduleStore

_KINDS = sorted(SCHEDULE_KINDS)


class CreateScheduleBody(BaseModel):
    """Request body for ``POST /schedules``."""

    conversation_id: str
    name: str
    kind: str
    prompt: str
    cron: str | None = None
    command: str | None = None
    enabled: bool = True


class UpdateScheduleBody(BaseModel):
    """Request body for ``PATCH /schedules/{id}``. Unset fields are unchanged."""

    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    command: str | None = None
    enabled: bool | None = None


def _to_response(s: Schedule) -> dict[str, Any]:
    """Serialize a :class:`Schedule` to a response dict."""
    return {
        "id": s.id,
        "object": "schedule",
        "conversation_id": s.conversation_id,
        "name": s.name,
        "kind": s.kind,
        "prompt": s.prompt,
        "cron": s.cron,
        "command": s.command,
        "enabled": s.enabled,
        "status": s.status,
        "last_fired_at": s.last_fired_at,
        "last_run_id": s.last_run_id,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _require_auth(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    """Identify the caller; require authentication in multi-user mode."""
    user_id = get_user_id(request, auth_provider)
    if permission_store is not None and user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    return user_id


def create_schedules_router(
    store: ScheduleStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the schedules router (mounted at ``/schedules``)."""
    router = APIRouter()

    @router.get("/schedules")
    async def list_schedules(request: Request, conversation_id: str) -> dict[str, Any]:
        """List a conversation's schedules (``?conversation_id=`` required)."""
        _require_auth(request, auth_provider, permission_store)
        items = store.list_for_conversation(conversation_id)
        return {"object": "list", "data": [_to_response(s) for s in items]}

    @router.post("/schedules")
    async def create_schedule(request: Request, body: CreateScheduleBody) -> dict[str, Any]:
        """Create a loop or monitor."""
        user_id = _require_auth(request, auth_provider, permission_store)
        if body.kind not in SCHEDULE_KINDS:
            raise OmnigentError(f"kind must be one of {_KINDS}", code=ErrorCode.INVALID_INPUT)
        if body.kind == "loop" and not body.cron:
            raise OmnigentError("cron is required for a loop", code=ErrorCode.INVALID_INPUT)
        if body.kind == "monitor" and not body.command:
            raise OmnigentError("command is required for a monitor", code=ErrorCode.INVALID_INPUT)
        try:
            sched = store.create(
                generate_schedule_id(),
                body.conversation_id,
                body.name,
                body.kind,
                body.prompt,
                cron=body.cron,
                command=body.command,
                enabled=body.enabled,
                created_by_user_id=user_id,
            )
        except IntegrityError as exc:
            raise OmnigentError(
                f"A schedule named '{body.name}' already exists in this conversation",
                code=ErrorCode.CONFLICT,
            ) from exc
        return _to_response(sched)

    @router.patch("/schedules/{schedule_id}")
    async def update_schedule(
        request: Request, schedule_id: str, body: UpdateScheduleBody
    ) -> dict[str, Any]:
        """Update a schedule's mutable fields (enable/disable, rename, etc.)."""
        _require_auth(request, auth_provider, permission_store)
        sched = store.update(
            schedule_id,
            name=body.name,
            prompt=body.prompt,
            cron=body.cron,
            command=body.command,
            enabled=body.enabled,
        )
        if sched is None:
            raise OmnigentError("Schedule not found", code=ErrorCode.NOT_FOUND)
        return _to_response(sched)

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
        """Delete a schedule. Idempotent."""
        _require_auth(request, auth_provider, permission_store)
        return {"deleted": store.delete(schedule_id)}

    return router
