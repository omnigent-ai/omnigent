"""REST CRUD for scheduled tasks (``/v1/scheduled-tasks``).

A scheduled task is a saved instruction that fires an agent session on a
recurring RRULE schedule. These endpoints let a client create, list, read,
update, and delete tasks; the live :class:`ScheduledTaskScheduler` is kept in
sync on every mutation so a change takes effect without a restart.

Ownership mirrors hosts: tasks are scoped to the calling user (``"local"`` when
auth is disabled). The RRULE is validated on create/update with
:func:`validate_rrule` — an invalid rule (bad syntax, never-fires, fires-once, or
below the minimum-interval floor) is a 400.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.server.scheduled.rrule import RRuleValidationError, validate_rrule
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)


class CreateScheduledTaskRequest(BaseModel):
    """Body for ``POST /v1/scheduled-tasks``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    prompt: str
    rrule: str
    agent_id: str
    timezone: str = "UTC"
    model_override: str | None = None
    reasoning_effort: str | None = None
    workspace: str = Field(min_length=1)
    host_id: str = Field(min_length=1)


class UpdateScheduledTaskRequest(BaseModel):
    """Body for ``PATCH /v1/scheduled-tasks/{id}``. Unset fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    prompt: str | None = None
    rrule: str | None = None
    timezone: str | None = None
    model_override: str | None = None
    reasoning_effort: str | None = None
    workspace: str | None = Field(default=None, min_length=1)
    host_id: str | None = Field(default=None, min_length=1)
    state: str | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> UpdateScheduledTaskRequest:
        """Keep the public update surface to active/paused connected-host runs."""
        if self.state is not None and self.state not in {"active", "paused"}:
            raise ValueError("state must be 'active' or 'paused'; use DELETE to delete a task")
        if "workspace" in self.model_fields_set and self.workspace is None:
            raise ValueError("workspace cannot be null")
        if "host_id" in self.model_fields_set and self.host_id is None:
            raise ValueError("host_id cannot be null")
        return self


def _to_response(task: ScheduledTask) -> dict[str, Any]:
    """Serialize a :class:`ScheduledTask` to a JSON-safe dict."""
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "rrule": task.rrule,
        "owner_user_id": task.owner_user_id,
        "agent_id": task.agent_id,
        "timezone": task.timezone,
        "created_at": task.created_at,
        "model_override": task.model_override,
        "reasoning_effort": task.reasoning_effort,
        "workspace": task.workspace,
        "host_id": task.host_id,
        "state": task.state,
        "last_run_at": task.last_run_at,
        "last_run_conversation_id": task.last_run_conversation_id,
        "updated_at": task.updated_at,
    }


def _validate_rrule_or_400(rrule: str) -> None:
    """Raise a 400 ``OmnigentError`` if the RRULE is invalid."""
    try:
        validate_rrule(rrule)
    except RRuleValidationError as exc:
        raise OmnigentError(f"invalid rrule: {exc}", code=ErrorCode.INVALID_INPUT) from exc


def _validate_timezone_or_400(timezone: str) -> None:
    """Raise a 400 ``OmnigentError`` if *timezone* is not a valid IANA timezone."""
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise OmnigentError(
            f"invalid timezone {timezone!r}: must be a valid IANA timezone name",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def create_scheduled_tasks_router(
    store: ScheduledTaskStore,
    *,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    agent_cache: Any | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the scheduled-tasks router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/scheduled-tasks[/{id}]``.

    :param store: The shared :class:`ScheduledTaskStore`.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` disables auth (owner resolves to ``"local"``).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner(request: Request) -> str:
        """Resolve the calling user, mapping the auth-disabled case to
        ``RESERVED_USER_LOCAL`` so single-user rows are always owned."""
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _scheduler(request: Request) -> Any | None:
        """The live scheduler off app state, or ``None`` if not running."""
        return getattr(request.app.state, "scheduled_task_scheduler", None)

    async def _validate_launch_inputs(
        request: Request,
        *,
        owner: str,
        agent_id: str,
        host_id: str,
        workspace: str,
        model_override: str | None,
        reasoning_effort: str | None,
    ) -> tuple[str, str | None, str | None]:
        """Validate inputs that scheduled tasks persist into future sessions."""
        user_id = None if owner == RESERVED_USER_LOCAL else owner
        agent = await validate_session_agent(
            user_id=user_id,
            agent_id=agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        validated_model, validated_effort = validate_session_model_metadata(
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )
        canonical_workspace = await validate_existing_host_workspace(
            user_id=user_id,
            host_id=host_id,
            workspace=workspace,
            agent=agent,
            agent_cache=agent_cache,
            host_store=getattr(request.app.state, "host_store", None),
            host_registry=getattr(request.app.state, "host_registry", None),
        )
        return canonical_workspace, validated_model, validated_effort

    def _require_owned(scheduled_task_id: str, owner: str) -> ScheduledTask:
        """Load a task the caller owns, or raise 404.

        A task owned by someone else 404s (not 403) so tasks aren't
        enumerable across users.
        """
        task = store.get(scheduled_task_id)
        if task is None or task.owner_user_id != owner:
            raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        return task

    @router.post("/scheduled-tasks")
    async def create_scheduled_task(
        request: Request,
        body: CreateScheduledTaskRequest,
    ) -> dict[str, Any]:
        """Create a scheduled task and arm it on the live scheduler."""
        owner = _owner(request)
        _validate_rrule_or_400(body.rrule)
        _validate_timezone_or_400(body.timezone)
        workspace, model_override, reasoning_effort = await _validate_launch_inputs(
            request,
            owner=owner,
            agent_id=body.agent_id,
            host_id=body.host_id,
            workspace=body.workspace,
            model_override=body.model_override,
            reasoning_effort=body.reasoning_effort,
        )
        task = store.create(
            scheduled_task_id=uuid.uuid4().hex,
            name=body.name,
            prompt=body.prompt,
            rrule=body.rrule,
            owner_user_id=None if owner == RESERVED_USER_LOCAL else owner,
            agent_id=body.agent_id,
            timezone=body.timezone,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            workspace=workspace,
            host_id=body.host_id,
        )
        scheduler = _scheduler(request)
        if scheduler is not None:
            scheduler.add(task)
        return _to_response(task)

    @router.get("/scheduled-tasks")
    async def list_scheduled_tasks(request: Request) -> dict[str, list[dict[str, Any]]]:
        """List the caller's scheduled tasks."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        tasks = [t for t in store.list() if t.owner_user_id == owner_id]
        return {"scheduled_tasks": [_to_response(t) for t in tasks]}

    @router.get("/scheduled-tasks/{scheduled_task_id}")
    async def get_scheduled_task(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Fetch one of the caller's scheduled tasks."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        task = _require_owned(scheduled_task_id, owner_id)
        return _to_response(task)

    @router.patch("/scheduled-tasks/{scheduled_task_id}")
    async def update_scheduled_task(
        request: Request,
        scheduled_task_id: str,
        body: UpdateScheduledTaskRequest,
    ) -> dict[str, Any]:
        """Update mutable fields of a task and re-sync the scheduler."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        existing = _require_owned(scheduled_task_id, owner_id)
        if body.rrule is not None:
            _validate_rrule_or_400(body.rrule)
        if body.timezone is not None:
            _validate_timezone_or_400(body.timezone)
        fields = body.model_dump(exclude_unset=True)
        if {"model_override", "reasoning_effort"}.intersection(fields):
            model_override, reasoning_effort = validate_session_model_metadata(
                model_override=fields.get("model_override", existing.model_override),
                reasoning_effort=fields.get("reasoning_effort", existing.reasoning_effort),
            )
            if "model_override" in fields:
                fields["model_override"] = model_override
            if "reasoning_effort" in fields:
                fields["reasoning_effort"] = reasoning_effort
        if {"workspace", "host_id"}.intersection(fields):
            workspace, _, _ = await _validate_launch_inputs(
                request,
                owner=owner,
                agent_id=existing.agent_id,
                host_id=fields.get("host_id", existing.host_id),
                workspace=fields.get("workspace", existing.workspace),
                model_override=fields.get("model_override", existing.model_override),
                reasoning_effort=fields.get("reasoning_effort", existing.reasoning_effort),
            )
            if "workspace" in fields:
                fields["workspace"] = workspace
        updated = store.update(scheduled_task_id, **fields)
        if updated is None:
            raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        scheduler = _scheduler(request)
        if scheduler is not None:
            scheduler.update(updated)
        return _to_response(updated)

    @router.delete("/scheduled-tasks/{scheduled_task_id}")
    async def delete_scheduled_task(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Delete a task and drop its timer from the scheduler."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(scheduled_task_id, owner_id)
        store.delete(scheduled_task_id)
        scheduler = _scheduler(request)
        if scheduler is not None:
            scheduler.remove(scheduled_task_id)
        return {"deleted": True, "id": scheduled_task_id}

    return router
