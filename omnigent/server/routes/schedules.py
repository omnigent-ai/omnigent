"""REST routes for schedules (loops & monitors).

CRUD over the ``schedules`` table at ``/schedules[/{schedule_id}]``. A loop is
either conversation-scoped (``conversation_id``) or **global** (``agent_name``
→ spawns a fresh session for that registered agent on each fire). ``GET`` with
``?conversation_id=`` lists that conversation's schedules; without it, lists all
(the global Schedules panel). Requires authentication in multi-user mode.
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

# Global loops spawn a FRESH conversation on every fire, so an over-frequent
# cron floods the workspace. Floor the fire rate for global loops. NOTE: this
# bounds the rate, not the cumulative count — a TTL / auto-archive of
# scheduler-spawned runs is a follow-up.
_GLOBAL_LOOP_MIN_INTERVAL_S = 300


class CreateScheduleBody(BaseModel):
    """Request body for ``POST /schedules``.

    Exactly one of ``conversation_id`` (fire into that conversation) or
    ``agent_name`` (global loop → spawn a fresh session per fire) must be set.
    """

    conversation_id: str | None = None
    agent_name: str | None = None
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
        "agent_name": s.agent_name,
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


async def _refresh_scheduler(request: Request) -> None:
    """Re-arm the live scheduler after a mutation, if one is running.

    The scheduler is started in the server lifespan and stashed on
    ``app.state`` (absent in minimal/test apps, hence the ``getattr``).
    :meth:`SchedulerService.refresh` reconciles armed cron tasks with the
    persisted enabled loops, so a freshly created/enabled loop arms at once and
    a deleted/disabled one is cancelled — without a server restart.
    """
    svc = getattr(request.app.state, "scheduler_service", None)
    if svc is not None:
        await svc.refresh()


def create_schedules_router(
    store: ScheduleStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the schedules router (mounted at ``/schedules``)."""
    router = APIRouter()

    def _is_admin(user_id: str | None) -> bool:
        return (
            permission_store is not None
            and user_id is not None
            and permission_store.is_admin(user_id)
        )

    def _load_owned(schedule_id: str, user_id: str | None) -> Schedule:
        """Load a schedule, enforcing the caller owns it (or is admin).

        Multi-user only; single-user / no-permission deployments skip the check.
        Non-owners get 404 (not 403) so a schedule's existence isn't leaked.
        """
        sched = store.get(schedule_id)
        if sched is None:
            raise OmnigentError("Schedule not found", code=ErrorCode.NOT_FOUND)
        if (
            permission_store is not None
            and not _is_admin(user_id)
            and sched.created_by_user_id != user_id
        ):
            raise OmnigentError("Schedule not found", code=ErrorCode.NOT_FOUND)
        return sched

    @router.get("/schedules")
    async def list_schedules(
        request: Request, conversation_id: str | None = None
    ) -> dict[str, Any]:
        """List schedules. With ``?conversation_id=`` → that conversation's;
        without it → all schedules (the global Schedules panel)."""
        user_id = _require_auth(request, auth_provider, permission_store)
        items = (
            store.list_for_conversation(conversation_id) if conversation_id else store.list_all()
        )
        # Multi-user: a caller sees only their own schedules (admins see all) —
        # never leak other users' prompts. Single-user deployments see all.
        if permission_store is not None and not _is_admin(user_id):
            items = [s for s in items if s.created_by_user_id == user_id]
        return {"object": "list", "data": [_to_response(s) for s in items]}

    @router.post("/schedules")
    async def create_schedule(request: Request, body: CreateScheduleBody) -> dict[str, Any]:
        """Create a loop or monitor (conversation-scoped or global)."""
        user_id = _require_auth(request, auth_provider, permission_store)
        if body.kind not in SCHEDULE_KINDS:
            raise OmnigentError(f"kind must be one of {_KINDS}", code=ErrorCode.INVALID_INPUT)
        if body.kind == "loop" and not body.cron:
            raise OmnigentError("cron is required for a loop", code=ErrorCode.INVALID_INPUT)
        if body.kind == "monitor" and not body.command:
            raise OmnigentError("command is required for a monitor", code=ErrorCode.INVALID_INPUT)
        # Scoping: exactly one of conversation_id / agent_name. A global
        # (agent_name) loop spawns a fresh session per fire; monitors are
        # conversation-scoped only.
        if bool(body.conversation_id) == bool(body.agent_name):
            raise OmnigentError(
                "exactly one of conversation_id or agent_name is required",
                code=ErrorCode.INVALID_INPUT,
            )
        if body.agent_name is not None:
            if body.kind != "loop":
                raise OmnigentError(
                    "agent_name (global) is only valid for a loop",
                    code=ErrorCode.INVALID_INPUT,
                )
            from omnigent.runtime import get_agent_store

            if get_agent_store().get_by_name(body.agent_name) is None:
                raise OmnigentError(
                    f"no registered agent named '{body.agent_name}'",
                    code=ErrorCode.INVALID_INPUT,
                )
        # Validate the cron up front — a typo would otherwise persist as an
        # enabled-looking loop that silently disarms at its first fire.
        if body.kind == "loop" and body.cron:
            from datetime import datetime, timezone

            from omnigent.runtime.cron import next_cron_time

            now = datetime.now(timezone.utc)
            try:
                first = next_cron_time(body.cron, now)
            except ValueError as exc:
                raise OmnigentError(
                    f"invalid cron expression: {exc}", code=ErrorCode.INVALID_INPUT
                ) from exc
            # Global loops spawn a fresh conversation per fire — floor the rate.
            if body.agent_name is not None:
                second = next_cron_time(body.cron, first)
                if (second - first).total_seconds() < _GLOBAL_LOOP_MIN_INTERVAL_S:
                    raise OmnigentError(
                        "a global loop must fire at most once every "
                        f"{_GLOBAL_LOOP_MIN_INTERVAL_S // 60} minutes",
                        code=ErrorCode.INVALID_INPUT,
                    )
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
                agent_name=body.agent_name,
            )
        except IntegrityError as exc:
            raise OmnigentError(
                f"A schedule named '{body.name}' already exists in this conversation",
                code=ErrorCode.CONFLICT,
            ) from exc
        await _refresh_scheduler(request)
        return _to_response(sched)

    @router.patch("/schedules/{schedule_id}")
    async def update_schedule(
        request: Request, schedule_id: str, body: UpdateScheduleBody
    ) -> dict[str, Any]:
        """Update a schedule's mutable fields (enable/disable, rename, etc.)."""
        user_id = _require_auth(request, auth_provider, permission_store)
        existing = _load_owned(schedule_id, user_id)
        # Re-validate the agent when (re)enabling a global loop — its agent may
        # have been unregistered since creation, which would dead-end every fire.
        if body.enabled and existing.agent_name is not None:
            from omnigent.runtime import get_agent_store

            if get_agent_store().get_by_name(existing.agent_name) is None:
                raise OmnigentError(
                    f"cannot enable: no registered agent named '{existing.agent_name}'",
                    code=ErrorCode.INVALID_INPUT,
                )
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
        await _refresh_scheduler(request)
        return _to_response(sched)

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
        """Delete a schedule. Idempotent."""
        user_id = _require_auth(request, auth_provider, permission_store)
        _load_owned(schedule_id, user_id)
        deleted = store.delete(schedule_id)
        await _refresh_scheduler(request)
        return {"deleted": deleted}

    return router
