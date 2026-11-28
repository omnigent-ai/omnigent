"""REST CRUD for scheduled tasks (``/v1/scheduled-tasks``).

A scheduled task is a saved instruction that fires an agent session on a
recurring RRULE schedule. These endpoints let a client create, list, read,
update, and delete tasks; the live :class:`ScheduledTaskScheduler` is kept in
sync on every mutation so a change takes effect without a restart.

Ownership mirrors hosts: tasks are scoped to the calling user (``"local"`` when
auth is disabled). People an automation is shared with can also read it, its runs
and their steps; every change stays with its owner. The RRULE is validated on
create/update with :func:`validate_rrule` — an invalid rule (bad syntax,
never-fires, fires-once, or below the minimum-interval floor) is a 400. With the
database scheduler and the ``one_time_automations`` release feature, an explicit
``COUNT=1`` is accepted as a one-time schedule such as a reminder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from omnigent.db.account_authority import account_generation, current_account_user
from omnigent.entities import Agent, Conversation, ScheduledTask, ScheduledTaskRun
from omnigent.entities.workflow import Workflow
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner._subprocess_lifecycle import await_cleanup_task
from omnigent.runtime import pending_elicitations
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.bundles import agent_for_user
from omnigent.server.feature_flags import DeploymentGate, deployment_gate_enabled
from omnigent.server.managed_hosts import parse_repo_workspace
from omnigent.server.proactive_agent import (
    PROACTIVE_AGENT_IDS,
    is_proactive_task,
    reject_proactive_agent_id,
)
from omnigent.server.routes._agent_origin import require_agent_origin
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.server.routes._sessions.helpers import (
    SessionLiveness,
    _read_state_entry,
    _session_status_from_cache,
)
from omnigent.server.routes._sessions.orchestration import _list_status_with_starting
from omnigent.server.routes.scheduled_task_permissions import (
    ScheduledTaskViewerRole,
    agent_shared_target_guard,
    display_names_or_ids,
    owns_scheduled_task,
    register_scheduled_task_permissions_routes,
    request_from_agent,
    require_agent_keeps_shared_target,
    scheduled_task_shareable,
    scheduled_task_sharing_supported,
    scheduled_task_viewer_role,
    without_legacy_sandbox_workspace,
)
from omnigent.server.scheduled.harness_settings import (
    scheduled_agent_harness,
    validate_scheduled_permission_mode,
    validate_scheduled_permission_support,
)
from omnigent.server.scheduled.rrule import (
    RRuleSearchError,
    RRuleValidationError,
    is_one_time_rule,
    validate_rrule,
)
from omnigent.server.scheduled.run_reconciler import force_fail_stale_runs
from omnigent.server.scheduled.run_sharing import delete_task_grants
from omnigent.server.scheduled.schedule import (
    continued_sandbox_host_id,
    is_trigger_automation,
    timer_rule,
)
from omnigent.server.scheduled.telemetry import log_task_created
from omnigent.server.scheduled.workflow_automations import (
    CONTINUED_SESSION_MESSAGE,
    is_bare_saved_workflow_run,
    normalize_workflow_details,
    saved_workflow_id,
    saved_workflow_run,
    saved_workflow_run_message,
    skill_prefix,
    task_workflow_id,
    workflow_session_reuse_error,
    workflow_skill_prefix,
)
from omnigent.server.session_mcp_selection import (
    MAX_SESSION_MCP_SERVICES,
    SessionMcpSelection,
    mcp_selection_for,
    run_sandbox,
    session_mcp_services,
)
from omnigent.server.workflows import require_workflows
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.permission_store import MAX_SHARED_SCHEDULED_TASKS_PAGE_SIZE
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.scheduled_task_store import (
    AutomationFailure,
    ScheduledTaskFirstRunMismatchError,
    ScheduledTaskStore,
    encode_mcp_services,
)
from omnigent.tools.browser_agent_tools import (
    BROWSER_AGENT_TOOLS_CREATION_LABEL_KEYS,
    browser_agent_tools_creation_labels,
)
from omnigent.tools.builtins.timer import DURABLE_TIMER_MIN_SECONDS, validate_timer_set_args
from omnigent.util.session_lifecycle import is_session_closed
from omnigent.util.uuid import normalize_uuid

_logger = logging.getLogger(__name__)

# Execution targets a task may run on. ``connected_host`` pins/resolves the
# owner's own machine; ``managed_sandbox`` provisions a FRESH server-managed
# sandbox per fire, using the server's normal sandbox lifecycle.
_VALID_EXECUTION_TARGETS = frozenset({"connected_host", "managed_sandbox"})
_MANAGED_SANDBOX_WITH_HOST_MSG = (
    "a managed_sandbox task runs in a fresh sandbox each fire; do not set host_id"
)
_SESSION_ID_WITHOUT_CONTINUE_MSG = "session_id requires session_mode 'continue'"
_SOURCE_SESSION_CONTINUE_MSG = (
    "an automation with source_session_id starts each run in a new session; "
    "session_mode must be 'new'"
)
_PROMPT_OR_WORKFLOW_MSG = "prompt is required unless workflow_id names a saved workflow to run"
_DETAILS_WITHOUT_WORKFLOW_MSG = "workflow_details needs a saved workflow; send workflow_id"
_PROMPT_WITH_WORKFLOW_MSG = (
    "with workflow_id or workflow_details, leave out prompt, or on an update resend it "
    "unchanged: the server writes the message that runs the workflow, and "
    "workflow_details carries this run's specifics"
)
# Only an explicit workflow_id null detaches a workflow, so a reworded prompt can't.
_WORKFLOW_PROMPT_OWNED_MSG = (
    "this automation runs a saved workflow, so the server writes its prompt: change the "
    "run's specifics with workflow_details, or send workflow_id null with a new prompt to "
    "stop running the workflow"
)
_WORKFLOW_CHANGED_MSG = "The automation's prompt or saved workflow changed. Reload and try again."
_WORKFLOW_PROMPT_WRAPPED_MSG = (
    "this automation's prompt runs its saved workflow inside other text, so the server "
    "can't rewrite its specifics: send the whole prompt as it should run, or send "
    "workflow_id null with a new prompt"
)
_STOP_WORKFLOW_NEEDS_PROMPT_MSG = (
    "workflow_id null stops running the saved workflow, so it needs a new prompt that runs none"
)
# A continued session cannot follow a change to these, so the next run starts a new one.
_SESSION_BOUND_FIELDS = frozenset(
    {"agent_id", "host_id", "workspace", "base_branch", "execution_target", "mcp_services"}
)


class AutomationFailurePage(BaseModel):
    """Failures from one bounded page of owned automations."""

    data: list[AutomationFailure]
    next_cursor: str | None


# 9999-12-31T23:59:59Z, the last second ``datetime`` can format.
_MAX_FIRST_RUN_AT = 253402300799


class CreateScheduledTaskRequest(BaseModel):
    """Body for ``POST /v1/scheduled-tasks``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    # The message every run sends. Left out with ``workflow_id``, whose run message the
    # server writes.
    prompt: str | None = None
    rrule: str
    agent_id: str
    # Only explicit null opts into the owner's recorded zone; omission stays UTC.
    timezone: str | None = "UTC"
    model_override: str | None = None
    reasoning_effort: str | None = None
    # Native-harness permission or execution mode; validated against the agent.
    permission_mode: str | None = None
    # MCP services every session the task creates starts with, as the
    # deployment's service names; validated against the owner's connections.
    mcp_services: list[str] = Field(default_factory=list, max_length=MAX_SESSION_MCP_SERVICES)
    max_cost_usd: float | None = Field(default=None, gt=0)
    # "continue" sends every run's prompt into one session: ``session_id`` when
    # given, else the session the first run creates. A prompt that runs a saved
    # workflow needs "new" (see ``workflow_session_reuse_error``).
    session_mode: Literal["new", "continue"] = "new"
    session_id: str | None = Field(default=None, min_length=1)
    # Unpinned connected-host tasks resolve an online host and its home at fire time.
    # Omission or null selects the default; empty strings are invalid.
    workspace: str | None = Field(default=None, min_length=1)
    base_branch: str | None = Field(default=None, min_length=1, max_length=255)
    host_id: str | None = Field(default=None, min_length=1)
    # ``managed_sandbox`` runs the task in a fresh server-provisioned sandbox
    # (no host_id, optional repository URL); default keeps connected-host behavior.
    execution_target: str = "connected_host"
    # One of the caller's projects to file fired sessions into.
    project_id: str | None = None
    # A session the caller owns. Create copies it, and every run starts a new
    # session from that copy; responses carry the copy's id here.
    source_session_id: str | None = Field(default=None, min_length=1)
    # Expected first-run epoch; reject the create if the schedule resolves differently.
    first_run_at: int | None = Field(default=None, gt=0, le=_MAX_FIRST_RUN_AT)
    # "reminder" marks a one-time message into session_id, which clients can list
    # apart from automations.
    kind: Literal["automation", "reminder"] = "automation"
    # A saved workflow every run starts as a run of, in a new session each time.
    workflow_id: str | None = Field(default=None, min_length=1)
    # This run's specifics, sent after the workflow's run request; only with workflow_id.
    workflow_details: str | None = None

    @model_validator(mode="after")
    def _validate_create(self) -> CreateScheduledTaskRequest:
        if self.workflow_id is None:
            if self.prompt is None:
                raise ValueError(_PROMPT_OR_WORKFLOW_MSG)
            if self.workflow_details is not None:
                raise ValueError(_DETAILS_WITHOUT_WORKFLOW_MSG)
        elif self.prompt is not None:
            raise ValueError(_PROMPT_WITH_WORKFLOW_MSG)
        if self.execution_target not in _VALID_EXECUTION_TARGETS:
            raise ValueError("execution_target must be 'connected_host' or 'managed_sandbox'")
        if self.execution_target == "managed_sandbox" and self.host_id is not None:
            raise ValueError(_MANAGED_SANDBOX_WITH_HOST_MSG)
        if self.session_id is not None and self.session_mode != "continue":
            raise ValueError(_SESSION_ID_WITHOUT_CONTINUE_MSG)
        if self.source_session_id is not None and self.session_mode != "new":
            raise ValueError(_SOURCE_SESSION_CONTINUE_MSG)
        if self.kind == "reminder" and (
            self.session_id is None or not is_one_time_rule(self.rrule)
        ):
            raise ValueError(
                "kind 'reminder' needs a one-time (COUNT=1) rule that continues a given session_id"
            )
        _validate_worktree_target(
            self.base_branch, self.execution_target, self.host_id, self.workspace
        )
        return self


class SetDefaultTimezoneRequest(BaseModel):
    """Body for ``PUT /v1/scheduled-tasks/default-timezone``."""

    model_config = ConfigDict(extra="forbid")

    timezone: str


class UpdateScheduledTaskRequest(BaseModel):
    """Body for ``PATCH /v1/scheduled-tasks/{id}``. Unset fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    prompt: str | None = None
    rrule: str | None = None
    # Rebinding the agent switches the harness the task fires with. The
    # per-agent settings below do not survive the switch — see the PATCH
    # handler, which clears any the caller does not resend.
    agent_id: str | None = Field(default=None, min_length=1)
    timezone: str | None = None
    model_override: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    # Replaces the task's MCP services; [] clears them.
    mcp_services: list[str] | None = Field(default=None, max_length=MAX_SESSION_MCP_SERVICES)
    max_cost_usd: float | None = Field(default=None, gt=0)  # null clears the cap
    session_mode: Literal["new", "continue"] | None = None
    # Null makes a continue-mode task's next run start a new session.
    session_id: str | None = Field(default=None, min_length=1)
    workspace: str | None = Field(default=None, min_length=1)
    base_branch: str | None = Field(default=None, min_length=1, max_length=255)
    host_id: str | None = Field(default=None, min_length=1)
    execution_target: str | None = Field(default=None, min_length=1)
    state: str | None = None
    project_id: str | None = None  # null unfiles future runs
    # Runs this saved workflow from now on; the server writes the prompt. Null stops
    # running it and needs a new ``prompt``.
    workflow_id: str | None = Field(default=None, min_length=1)
    # The run's specifics for the task's saved workflow; null clears them.
    workflow_details: str | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> UpdateScheduledTaskRequest:
        """Keep the public update surface to active/paused connected-host runs."""
        if self.state is not None and self.state not in {"active", "paused"}:
            raise ValueError("state must be 'active' or 'paused'; use DELETE to delete a task")
        if "host_id" in self.model_fields_set and self.host_id is None:
            raise ValueError("host_id cannot be null")
        if self.execution_target is not None and self.execution_target not in (
            _VALID_EXECUTION_TARGETS
        ):
            raise ValueError("execution_target must be 'connected_host' or 'managed_sandbox'")
        if self.execution_target == "managed_sandbox" and "host_id" in self.model_fields_set:
            raise ValueError(_MANAGED_SANDBOX_WITH_HOST_MSG)
        if "agent_id" in self.model_fields_set and self.agent_id is None:
            raise ValueError("agent_id cannot be null")
        return self


def _validate_worktree_target(
    base_branch: str | None, execution_target: str, host_id: str | None, workspace: str | None
) -> None:
    if base_branch is not None and (
        execution_target != "connected_host" or host_id is None or workspace is None
    ):
        raise ValueError("random worktree requires a connected host and repository workspace")


def _next_run_at(task: ScheduledTask, request: Request) -> str | None:
    if getattr(request.app.state, "scheduled_task_dispatch_enabled", False):
        return (
            datetime.fromtimestamp(task.next_run_at, UTC).isoformat()
            if task.next_run_at is not None
            else None
        )
    scheduler = getattr(request.app.state, "scheduled_task_scheduler", None)
    return scheduler.next_run_at(task.id) if scheduler is not None else None


def _task_workflow(task: ScheduledTask) -> tuple[str | None, str | None]:
    """The saved workflow *task*'s runs start as runs of, and the specifics they carry.

    :returns: ``(workflow_id, details)``, or ``(None, None)`` for a task without one.
    """
    run = saved_workflow_run(task.prompt)
    return (None, None) if run is None else (run.workflow_id, run.details)


def _hides_trigger_rrule(request: Request) -> bool:
    """Whether trigger automations' rules are withheld from responses.

    With automations v2 the server decides when a trigger polls; the legacy timers
    poll it on its saved rule, so they return it.
    """
    return getattr(request.app.state, "scheduled_task_dispatch_enabled", False)


def _to_response(
    task: ScheduledTask,
    *,
    shareable: bool,
    hide_trigger_rrule: bool,
    last_run_status: str | None = None,
    next_run_at: str | None = None,
    viewer_role: ScheduledTaskViewerRole = "owner",
    owner_display_name: str | None = None,
) -> dict[str, Any]:
    """Serialize a :class:`ScheduledTask` to a JSON-safe dict.

    :param shareable: Whether the automation itself can be shared, from
        :func:`scheduled_task_shareable`.
    :param hide_trigger_rrule: Send a trigger automation's ``rrule`` as ``None``, from
        :func:`_hides_trigger_rrule`.
    :param last_run_status: The status of the task's most recent run
        (``succeeded`` / ``failed`` / ``skipped`` / ``running`` / ``scheduled``),
        or ``None`` when the task has never run. Surfaced so the Tasks list can
        render a completion badge without an extra per-row ``/runs`` fetch.
    :param next_run_at: ISO-8601 timestamp of the task's next scheduled fire as
        computed by the live scheduler (the server's authoritative anchor), or
        ``None`` when the task is paused / not armed. Deliberately server-sourced
        — the client must never recompute next-run (it can't match the server
        anchor for INTERVAL>1 rules).
    :param viewer_role: ``"owner"``, or ``"viewer"`` for someone it is shared with,
        who may only read it.
    :param owner_display_name: The owner's name for a viewer; ``None`` for the owner.
    """
    workflow_id, workflow_details = _task_workflow(task)
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "rrule": None if hide_trigger_rrule and is_trigger_automation(task) else task.rrule,
        # JSON key preserved for API/UI stability; the DB column + entity
        # attribute are now ``user_id``.
        "owner_user_id": task.user_id,
        "agent_id": task.agent_id,
        "timezone": task.timezone,
        "created_at": task.created_at,
        "model_override": task.model_override,
        "reasoning_effort": task.reasoning_effort,
        "permission_mode": task.permission_mode,
        "mcp_services": list(task.mcp_services),
        "max_cost_usd": task.max_cost_usd,
        "session_mode": task.session_mode,
        "session_id": task.session_id,
        "source_session_id": task.source_session_id,
        "workflow_id": workflow_id,
        "workflow_details": workflow_details,
        "kind": task.kind,
        "workspace": task.workspace,
        "base_branch": task.base_branch,
        "host_id": task.host_id,
        "execution_target": task.execution_target,
        "project_id": task.project_id,
        "state": task.state,
        "last_run_at": task.last_run_at,
        "last_run_status": last_run_status,
        "last_run_conversation_id": task.last_run_conversation_id,
        "next_run_at": next_run_at,
        "updated_at": task.updated_at,
        "viewer_role": viewer_role,
        "owner_display_name": owner_display_name,
        "shareable": shareable,
    }


# Bounds each run's free-text failure detail in run lists; the session transcript
# keeps the same cap for a launch failure's reason.
_RUN_ERROR_DETAIL_MAX_CHARS = 1000


def _run_to_response(
    run: ScheduledTaskRun,
    *,
    conversation_updated_at: int | None = None,
    conversation_status: str | None = None,
    viewer_last_seen: int | None = None,
    viewer_unread: bool | None = None,
    session_cost_usd: float | None = None,
    conversation_pending_approvals: int = 0,
    include_error_detail: bool = True,
) -> dict[str, Any]:
    """Serialize a :class:`ScheduledTaskRun` to a JSON-safe dict.

    ``error_code`` carries the queryable failure classification. The free-text
    ``error`` is sent as ``error_detail``, capped at
    :data:`_RUN_ERROR_DETAIL_MAX_CHARS`, so the run list can say why a run failed,
    e.g. the sandbox provider's recovery steps for a sandbox that never connected.
    Each session field defaults to its value for a run with no session.

    :param conversation_updated_at: The conversation's ``updated_at`` epoch
        seconds, or ``None`` for runs without a conversation. Embedded so the
        frontend can call ``isConversationUnseen`` with no per-row fetch.
    :param conversation_status: The session's status as ``GET /v1/sessions``
        reports it (``"idle"``, ``"running"`` or ``"failed"``; waiting and
        booting read as running), without that list's sub-agent roll-up. ``None``
        for runs without a conversation.
    :param viewer_last_seen: When the caller last saw the conversation (epoch
        seconds), or ``None`` if never or for runs without a conversation. Same
        value as ``viewer_last_seen`` in ``GET /v1/sessions``.
    :param viewer_unread: The caller's ``viewer_unread`` flag for the
        conversation (in-memory read-state explicit-unread), or ``None`` for
        runs without a conversation. Hydrates ``useUnseenConversations`` on load
        so the frontend never needs a separate session fetch.
    :param session_cost_usd: Current cumulative session cost, or ``None`` when
        unreported. Runs continuing the same session share this total.
    :param conversation_pending_approvals: Current session approval/question count.
    :param include_error_detail: ``False`` leaves ``error_detail`` null, for a caller
        who sees only the run's outcome.
    """
    return {
        "id": run.id,
        "scheduled_task_id": run.scheduled_task_id,
        "status": run.status,
        "scheduled_at": run.scheduled_at,
        "conversation_id": run.conversation_id,
        "fired_at": run.fired_at,
        "finished_at": run.finished_at,
        "error_code": run.error_code,
        "error_detail": (
            run.error[:_RUN_ERROR_DETAIL_MAX_CHARS] if include_error_detail and run.error else None
        ),
        "conversation_updated_at": conversation_updated_at,
        "conversation_status": conversation_status,
        "viewer_last_seen": viewer_last_seen,
        "viewer_unread": viewer_unread,
        "session_cost_usd": session_cost_usd,
        "conversation_pending_approvals": conversation_pending_approvals,
    }


def one_time_schedules_supported(request: Request) -> bool:
    """Whether creation accepts single-fire rules; see :func:`validate_rrule`.

    Also gated by the ``one_time_automations`` release feature, because an older
    database scheduler polling the same database would pause such a task.
    """
    return bool(getattr(request.app.state, "one_time_automations_enabled", False))


async def _validate_rrule_or_400(rrule: str, *, allow_single_fire: bool) -> None:
    """Raise a 400 ``OmnigentError`` if the RRULE is invalid."""
    try:
        await asyncio.to_thread(validate_rrule, rrule, allow_single_fire=allow_single_fire)
    except RRuleValidationError as exc:
        raise OmnigentError(f"invalid rrule: {exc}", code=ErrorCode.INVALID_INPUT) from exc


def _validate_timezone_or_400(timezone: str) -> None:
    """Raise a 400 ``OmnigentError`` if *timezone* is not a valid IANA timezone."""
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, KeyError, ValueError, OSError) as exc:
        raise OmnigentError(
            f"invalid timezone {timezone!r}: must be a valid IANA timezone name",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _resolve_create_timezone(
    body: CreateScheduledTaskRequest, store: ScheduledTaskStore, user_id: str | None
) -> str:
    """Resolve an explicit request for the owner's default without changing legacy creates."""
    timezone = body.timezone
    if timezone is None:
        timezone = store.get_default_timezone(user_id) or "UTC"
    _validate_timezone_or_400(timezone)
    return timezone


async def _workflow_reuse_problem(
    prompt: str, session_mode: str, workflow_id: str | None = None
) -> str | None:
    """Why an automation that runs a saved workflow can't reuse a session, if it would.

    Runs in a thread: reading a prompt edited outside the form can take a pass over all of it.
    """
    return await asyncio.to_thread(workflow_session_reuse_error, prompt, session_mode, workflow_id)


def _refuse_workflow_reuse(problem: str | None) -> None:
    if problem is not None:
        raise OmnigentError(problem, code=ErrorCode.INVALID_INPUT)


def _workflow_reuse_change(
    task: ScheduledTask, requested: dict[str, Any]
) -> tuple[str, str, str | None] | None:
    """The prompt, session mode and saved workflow an update leaves *task* with, if it
    changes them or the session; None otherwise.

    An automation saved before workflows needed new sessions keeps running until an
    update changes what it runs or the session it uses.
    """
    prompt = task.prompt if requested.get("prompt") is None else requested["prompt"]
    session_mode = requested.get("session_mode", task.session_mode)
    workflow_before = task_workflow_id(task)
    workflow_id = requested.get("workflow_id", workflow_before)
    if (
        prompt == task.prompt
        and session_mode == task.session_mode
        and workflow_id == workflow_before
        and requested.get("session_id", task.session_id) == task.session_id
    ):
        return None
    return prompt, session_mode, workflow_id


def _breaks_workflow_prompt(locked: ScheduledTask, fields: dict[str, Any]) -> bool:
    """Whether writing *fields* over *locked* would leave its saved workflow with a prompt
    that doesn't run it, as an edit checked before a concurrent attach could."""
    workflow_id = fields.get("workflow_id", locked.workflow_id)
    if workflow_id is None:
        return False
    prompt = fields["prompt"] if fields.get("prompt") is not None else locked.prompt
    return saved_workflow_id(prompt) != workflow_id


def _align_stored_workflow(existing: ScheduledTask, fields: dict[str, Any]) -> bool:
    """Store the workflow the prompt an update leaves runs, if the update sets none and the
    stored one differs, as after a server that doesn't store workflows rewrote the prompt.

    :returns: Whether *fields* now sets ``workflow_id``, read from the snapshot.
    """
    if "workflow_id" in fields:
        return False
    prompt = fields["prompt"] if fields.get("prompt") is not None else existing.prompt
    workflow_id = saved_workflow_id(prompt)
    if workflow_id == existing.workflow_id:
        return False
    fields["workflow_id"] = workflow_id
    return True


_AGENT_AUTOMATION_ACTION = "Agent automation changes"


def _inbox_cursor_task_id(task_id: str) -> str:
    """An Inbox cursor's task ID: bare hex if a UUID, else as the store allocated it.

    :raises ValueError: If the ID is empty.
    """
    if not task_id:
        raise ValueError("missing task ID")
    try:
        return uuid.UUID(task_id).hex
    except ValueError:
        return task_id


class CloneScheduledTaskRequest(BaseModel):
    """Optional body for ``POST /v1/scheduled-tasks/{id}/clone``."""

    model_config = ConfigDict(extra="forbid")

    # The automation's own host, which clients send so the request routes to that
    # host's replica; the copy keeps the original's host either way.
    host_id: str | None = None


# The scheduled_tasks.name column's length.
_TASK_NAME_MAX_CHARS = 256
_CLONE_NAME_SUFFIX = " (copy)"


def _clone_name(name: str) -> str:
    """*name* with the copy suffix, shortened to fit the name column."""
    return name[: _TASK_NAME_MAX_CHARS - len(_CLONE_NAME_SUFFIX)] + _CLONE_NAME_SUFFIX


def _clone_request(task: ScheduledTask) -> CreateScheduledTaskRequest:
    """The create body that copies *task*'s settings, leaving its continued session behind."""
    return CreateScheduledTaskRequest(
        name=_clone_name(task.name),
        prompt=task.prompt,
        rrule=task.rrule,
        agent_id=task.agent_id,
        timezone=task.timezone,
        model_override=task.model_override,
        reasoning_effort=task.reasoning_effort,
        permission_mode=task.permission_mode,
        mcp_services=list(task.mcp_services),
        max_cost_usd=task.max_cost_usd,
        session_mode="continue" if task.session_mode == "continue" else "new",
        workspace=task.workspace,
        base_branch=task.base_branch,
        host_id=task.host_id,
        execution_target=task.execution_target,
        project_id=task.project_id,
        source_session_id=task.source_session_id,
    )


def create_scheduled_tasks_router(
    store: ScheduledTaskStore,
    *,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    agent_cache: Any | None = None,
    auth_provider: AuthProvider | None = None,
    artifact_store: ArtifactStore | None = None,
    file_store: Any | None = None,
    project_store: ProjectStore | None = None,
    automations: DeploymentGate = True,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
) -> APIRouter:
    """Build the scheduled-tasks router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/scheduled-tasks[/{id}]``.

    :param store: The shared :class:`ScheduledTaskStore`.
    :param artifact_store: Holds the copy a task gets of another user's agent.
    :param file_store: Copies a source session's uploaded files into the
        session its runs start from. ``None`` copies none.
    :param project_store: Checks the project fired sessions are filed into.
        ``None`` rejects tasks that set a project.
    :param automations: Per-request gate; every route returns 404 while it is off.
    :param liveness_lookup: Shared bulk session liveness; masks stale approval counts.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` disables auth (owner resolves to ``"local"``).
    :returns: A configured :class:`APIRouter`.
    """

    def require_automations() -> None:
        if not deployment_gate_enabled(automations, "automations"):
            raise HTTPException(status_code=404, detail="not found")

    router = APIRouter(dependencies=[Depends(require_automations)])

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
        host_id: str | None,
        workspace: str | None,
        model_override: str | None,
        reasoning_effort: str | None,
        permission_mode: str | None,
        execution_target: str = "connected_host",
        continues_session: bool = False,
    ) -> tuple[Agent, str | None, str | None, str | None]:
        """Validate inputs that scheduled tasks persist into future sessions.

        A task that continues a session (*continues_session*) and selects no
        permission mode wakes it in the session's own mode, even one that skips
        approval.

        Workspace is always optional. When it is unset the canonical workspace
        persists as ``None`` and the fire path defaults it to the launch host's
        home directory — this holds whether the host was pinned or is resolved
        from the owner's live hosts at fire time. Only a workspace pinned WITHOUT
        a host is an error (a path with no machine is meaningless). When both a
        host and a workspace are supplied, the workspace is validated against the
        host boundary here so a bad pin fails fast at create.

        A ``managed_sandbox`` target accepts a repository URL instead of a host
        directory. The server must be able to launch a sandbox, so
        an unconfigured server rejects the task at create instead of failing every
        fire.
        """
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
        # Each harness has its own permission vocabulary and launch flags.
        await validate_scheduled_permission_support(
            permission_mode=permission_mode,
            agent=agent,
            agent_cache=agent_cache,
            continues_session=continues_session,
        )
        if execution_target == "managed_sandbox":
            sandbox_config = getattr(request.app.state, "sandbox_config", None)
            if sandbox_config is None or not sandbox_config.managed_launch_supported:
                raise OmnigentError(
                    "managed sandboxes are not configured on this server",
                    code=ErrorCode.INVALID_INPUT,
                )
            # A default profile is authorized at fire time, like every launch.
            if sandbox_config.default.manages_profiles and (
                getattr(request.app.state, "sandbox_profiles", None) is None
                or sandbox_config.default.default_profile_id is None
            ):
                raise OmnigentError(
                    "Scheduled fresh sandboxes cannot select a profile yet; "
                    "use a connected host or a default provider without profiles "
                    "or with a default profile",
                    code=ErrorCode.INVALID_INPUT,
                )
            if workspace is not None:
                try:
                    parse_repo_workspace(workspace)
                except ValueError as exc:
                    raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            return agent, workspace, validated_model, validated_effort
        if host_id is not None:
            host_store = getattr(request.app.state, "host_store", None)
            if host_store is not None:
                host = await asyncio.to_thread(
                    resolve_host_owner,
                    user_id=user_id,
                    host_id=host_id,
                    host_store=host_store,
                )
                if host.sandbox_provider is not None:
                    raise OmnigentError(
                        "automations cannot use an existing sandbox; "
                        "select a new sandbox for each run",
                        code=ErrorCode.INVALID_INPUT,
                    )
        if workspace is None:
            # The fire resolves an omitted workspace to the authorized host's HOME.
            return agent, None, validated_model, validated_effort
        if host_id is None:
            raise OmnigentError(
                "host_id required when workspace is set",
                code=ErrorCode.INVALID_INPUT,
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
        return agent, canonical_workspace, validated_model, validated_effort

    def _project_owner(owner: str) -> str | None:
        # Match /v1/projects owner scoping ("local" in single-user mode).
        return None if auth_provider is None else owner

    async def _owned_project_id(project_owner: str | None, project_id: str) -> str:
        """Return *project_id* if it is one of the owner's projects, else a 404."""
        if project_store is None:
            raise OmnigentError(
                "projects are not available on this server", code=ErrorCode.NOT_FOUND
            )
        project = await asyncio.to_thread(project_store.get, project_id, user_id=project_owner)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project.id

    async def _bind_agent(agent: Agent, owner_id: str | None) -> Agent:
        """A task runs another user's agent from its owner's own copy, taken now."""
        return await asyncio.to_thread(
            agent_for_user, agent_store, artifact_store, agent, owner_id
        )

    def _require_owned(scheduled_task_id: str, owner: str | None) -> ScheduledTask:
        """Load a task the caller owns, or raise 404.

        A task owned by someone else 404s (not 403) so tasks aren't
        enumerable across users.
        """
        task = store.get(scheduled_task_id)
        # Proactive tasks are framework-owned and never shown to their owner.
        if task is None or is_proactive_task(task) or not owns_scheduled_task(task, owner):
            raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        return task

    _reject_proactive_agent = reject_proactive_agent_id

    def _refuse_timer(task: ScheduledTask) -> None:
        """Raise a 400 for a timer: it keeps the schedule its agent set until cancelled.

        Its session's own sharing decides who can use it, so it has no grants either.
        """
        if task.kind == "timer":
            raise OmnigentError(
                "A timer can't be run, edited or shared; cancel it and set a new one",
                code=ErrorCode.INVALID_INPUT,
            )

    async def _require_readable(
        request: Request, scheduled_task_id: str
    ) -> tuple[ScheduledTask, ScheduledTaskViewerRole]:
        """Load a task its owner or a recipient may read, or raise ``_require_owned``'s 404.

        Only read routes use this; every change stays owner-only.
        """
        owner = _owner(request)
        caller_id = None if owner == RESERVED_USER_LOCAL else owner
        task = await asyncio.to_thread(store.get, scheduled_task_id)
        # Proactive tasks stay hidden here too, as in ``_require_owned``.
        role = (
            await asyncio.to_thread(
                scheduled_task_viewer_role, request, task, caller_id, permission_store
            )
            if task is not None and not is_proactive_task(task)
            else None
        )
        if task is None or role is None:
            raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        return task, role

    async def _owner_display_names(tasks: list[ScheduledTask]) -> dict[str, str]:
        """Display names for the owners of tasks shown to a recipient, in one batch."""
        owners = sorted({task.user_id for task in tasks if task.user_id is not None})
        if not owners or permission_store is None:
            return {}
        return await asyncio.to_thread(display_names_or_ids, permission_store, owners)

    async def _task_mcp_selection(
        request: Request, execution_target: str, host_id: str | None
    ) -> SessionMcpSelection:
        """The selection a task's next run uses: its sandbox's provider's, or the registry's.

        *host_id* is the existing sandbox the next run joins, if any, as for a fire.
        """
        state = request.app.state
        sandbox = await asyncio.to_thread(
            run_sandbox,
            getattr(state, "sandbox_config", None),
            getattr(state, "host_store", None),
            execution_target,
            host_id,
        )
        return mcp_selection_for(sandbox)

    async def _validated_mcp_services(
        request: Request,
        services: list[str],
        agent: Agent,
        owner: str,
        execution_target: str,
        host_id: str | None = None,
    ) -> tuple[str, ...]:
        """Check *services* with the selection of where the task's next run lands."""
        if not services:
            return ()
        spec = None
        if agent_cache is not None:
            try:
                spec = (
                    await asyncio.to_thread(
                        agent_cache.load,
                        agent.id,
                        agent.bundle_location,
                        expand_env=agent.operator_authored,
                    )
                ).spec
            except (KeyError, AttributeError, ValueError, ImportError, OSError):
                # The fire path reports an unloadable agent; skip only the spec check.
                spec = None
        selection = await _task_mcp_selection(request, execution_target, host_id)
        validated = await asyncio.to_thread(
            selection.validate, request, services, spec=spec, user_id=owner
        )
        # Rejects a list too long for the column before anything is written.
        encode_mcp_services(validated)
        return validated

    async def _require_continuable(request: Request, task: ScheduledTask) -> Conversation:
        """Return a picked session *task* can continue; reject it otherwise, hiding one
        the owner cannot see."""
        from omnigent.server.scheduled.fire import continued_session_problem

        assert task.session_id is not None
        conv = await asyncio.to_thread(conversation_store.get_conversation, task.session_id)
        problem = await continued_session_problem(
            task,
            conv,
            host_store=getattr(request.app.state, "host_store", None),
            permission_store=permission_store,
        )
        if problem is None:
            assert conv is not None
            return conv
        reason, error_code = problem
        if error_code in ("continued_session_missing", "continued_session_forbidden"):
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        raise OmnigentError(
            f"cannot continue this session: {reason}", code=ErrorCode.INVALID_INPUT
        )

    def _require_mcp_services_fit_session(
        request: Request, session: Conversation, services: tuple[str, ...]
    ) -> None:
        """Reject saved *services* the continued *session* does not run.

        Continuing reuses the session as it is, so a run could not add them. An
        empty selection asks for nothing beyond what the session already has.
        """
        if not services:
            return
        running = session_mcp_services(
            getattr(request.app.state, "sandbox_config", None), session.labels
        )
        if set(services) != set(running):
            raise OmnigentError(
                "The picked session runs different MCP services. Select the ones it "
                "runs, or start a new session on every run.",
                code=ErrorCode.INVALID_INPUT,
            )

    async def _require_source_session(
        source_id: str, owner: str, *, cloned_task_id: str | None = None
    ) -> tuple[Conversation, str]:
        """A session *owner* can start an automation from, hiding one they can't see.

        :param cloned_task_id: The automation being cloned, whose own source copy may
            be copied again for the clone.
        :returns: The session and its last finished response, which its copy stops at.
        """
        from omnigent.stores.conversation_store import (
            SCHEDULED_TASK_SOURCE_LABEL_KEY,
            SIDE_CHAT_LABEL_KEY,
        )

        conv = await asyncio.to_thread(conversation_store.get_conversation, source_id)
        if conv is None or (
            permission_store is not None
            and not await asyncio.to_thread(
                permission_store.check_access, owner, source_id, LEVEL_OWNER
            )
        ):
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if conv.parent_conversation_id is not None or conv.labels.get(SIDE_CHAT_LABEL_KEY):
            raise OmnigentError(
                "An automation can't start from a sub-agent's session or a side chat",
                code=ErrorCode.INVALID_INPUT,
            )
        source_of = conv.labels.get(SCHEDULED_TASK_SOURCE_LABEL_KEY)
        if source_of and (cloned_task_id is None or source_of != cloned_task_id):
            raise OmnigentError(
                "An automation already runs from this session", code=ErrorCode.INVALID_INPUT
            )
        if conv.agent_id is None:
            raise OmnigentError("The session has no agent to copy", code=ErrorCode.INVALID_INPUT)
        # Its copy would carry the hidden proactive agent into a user automation.
        if normalize_uuid(conv.agent_id) in PROACTIVE_AGENT_IDS:
            raise OmnigentError(
                "An automation can't start from a proactive agent's session",
                code=ErrorCode.INVALID_INPUT,
            )
        from omnigent.server.scheduled.source_session import last_finished_response_id

        # Runs start from the history through the last finished reply, so there must be one.
        up_to = await asyncio.to_thread(
            last_finished_response_id,
            conversation_store,
            conv.id,
            running=_session_status_from_cache(conv.id, conv.live_status) == "running",
        )
        if up_to is None:
            raise OmnigentError(
                "The session has no finished reply to start an automation from yet",
                code=ErrorCode.INVALID_INPUT,
            )
        return conv, up_to

    async def _require_openable_attachments(
        session_id: str, agent: Agent, up_to_response_id: str | None = None
    ) -> None:
        """Reject an agent that can't open the files the copied history refers to."""
        from omnigent.server.routes.sessions.routes_core import (
            _require_attachment_compatible_history,
        )

        await asyncio.to_thread(
            _require_attachment_compatible_history,
            session_id,
            agent,
            conversation_store,
            file_store,
            up_to_response_id=up_to_response_id,
        )

    async def _source_history_carry(agent: Agent) -> bool:
        from omnigent.server.scheduled.source_session import (
            SourceHistoryUnsupportedError,
            carries_native_history,
        )

        try:
            return await asyncio.to_thread(carries_native_history, agent)
        except SourceHistoryUnsupportedError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

    async def _copy_history_carry(source: Conversation) -> bool:
        """Whether the copy, which keeps the session's own agent, rebuilds a native transcript."""
        from omnigent.server.scheduled.source_session import (
            SourceHistoryUnsupportedError,
            carries_native_history,
        )

        assert source.agent_id is not None  # _require_source_session rejects agentless sessions
        source_agent = await asyncio.to_thread(agent_store.get, source.agent_id)
        if source_agent is None:
            return False
        try:
            return await asyncio.to_thread(carries_native_history, source_agent)
        except SourceHistoryUnsupportedError:
            return False

    async def _copy_source_session(
        source: Conversation,
        *,
        up_to: str,
        task_id: str,
        name: str,
        owner: str,
        project_id: str | None,
        carry: bool,
        browser_labels: dict[str, str],
    ) -> Conversation:
        """Copy *source*, through its last finished reply, for the task's runs to start from."""
        # Its threads can commit after the request is cancelled; the copy is deleted once they do.
        setup = asyncio.create_task(
            _copy_and_own_source(
                source,
                up_to=up_to,
                task_id=task_id,
                name=name,
                owner=owner,
                project_id=project_id,
                carry=carry,
                browser_labels=browser_labels,
            )
        )
        try:
            return await asyncio.shield(setup)
        except asyncio.CancelledError:

            async def settle_and_discard() -> None:
                # A setup that failed has already deleted its copy.
                with contextlib.suppress(Exception):
                    await _delete_session_quietly((await setup).id)

            cleanup = asyncio.create_task(settle_and_discard())
            while not cleanup.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            raise

    async def _copy_and_own_source(
        source: Conversation,
        *,
        up_to: str,
        task_id: str,
        name: str,
        owner: str,
        project_id: str | None,
        carry: bool,
        browser_labels: dict[str, str],
    ) -> Conversation:
        """Copy *source* and grant its owner the copy, deleting the copy if either fails."""
        from omnigent.server.scheduled.source_session import (
            SourceCopyIncompleteError,
            copy_session,
            source_copy_title,
        )
        from omnigent.stores.conversation_store import SCHEDULED_TASK_SOURCE_LABEL_KEY

        # _require_source_session rejects a session without an agent.
        assert source.agent_id is not None
        try:
            copy = await asyncio.to_thread(
                copy_session,
                conversation_store=conversation_store,
                file_store=file_store,
                artifact_store=artifact_store,
                source_id=source.id,
                title=source_copy_title(source.title, name),
                # The copy keeps the session's agent; each run binds the task's own.
                agent_id=source.agent_id,
                created_by=owner,
                project_id=project_id,
                carry_history_into_native=carry,
                # The copy, and so every run copied from it, takes its browser
                # tools from the creating request, not from the source.
                labels={SCHEDULED_TASK_SOURCE_LABEL_KEY: task_id, **browser_labels},
                dropped_label_keys=BROWSER_AGENT_TOOLS_CREATION_LABEL_KEYS,
                up_to_response_id=up_to,
            )
        except SourceCopyIncompleteError as exc:
            await _delete_session_quietly(exc.copy.id)
            raise OmnigentError(
                "Could not copy every file of the session. Try again.",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        try:
            if permission_store is not None:
                await asyncio.to_thread(permission_store.ensure_user, owner)
                await asyncio.to_thread(permission_store.grant, owner, copy.id, LEVEL_OWNER)
        except Exception:
            await _delete_session_quietly(copy.id)
            raise
        return copy

    async def _delete_session_quietly(session_id: str) -> None:
        """Delete an unused copy with its file rows; blobs another session still uses stay."""
        try:
            if file_store is not None:
                orphaned = await asyncio.to_thread(file_store.delete_all_for_session, session_id)
                if artifact_store is not None:
                    for blob_key in orphaned:
                        try:
                            await asyncio.to_thread(artifact_store.delete, blob_key)
                        except Exception:
                            # A blob that won't delete must not keep the copy.
                            _logger.warning("could not delete blob %s", blob_key, exc_info=True)
            await conversation_store.delete_conversation(session_id)
        except Exception:
            _logger.exception("could not delete the unused session copy %s", session_id)

    @contextlib.asynccontextmanager
    async def _allocated_task_id(owner_id: str | None, name: str) -> AsyncIterator[str]:
        """Allocate a new task's ID, releasing it if the block fails, cancellation included.

        The block must end once the store saves the task, so a saved task keeps its ID.
        """
        # An allocator may create an external object, which a cancel must not leak.
        allocating = asyncio.create_task(
            asyncio.to_thread(store.allocate_scheduled_task_id, user_id=owner_id, name=name)
        )
        try:
            task_id = await asyncio.shield(allocating)
        except asyncio.CancelledError:

            async def settle_and_release() -> None:
                # A failed allocation has no ID to release.
                with contextlib.suppress(Exception):
                    await _release_task_id_quietly(await allocating)

            await await_cleanup_task(asyncio.create_task(settle_and_release()))
            raise
        try:
            yield task_id
        except BaseException:
            await _release_task_id_quietly(task_id)
            raise

    async def _release_task_id_quietly(task_id: str) -> None:
        """Release an unused task ID, finishing through cancellations; failures are only logged."""
        releasing = asyncio.create_task(
            asyncio.to_thread(store.release_scheduled_task_id, task_id)
        )
        try:
            await await_cleanup_task(releasing)
        except Exception:
            # The original error still reaches the client.
            _logger.exception("could not release the unused scheduled task ID %s", task_id)

    async def _require_mode_fits_session(request: Request, task: ScheduledTask) -> None:
        """Reject a mode change that leaves *task*'s continued session unable to take prompts.

        Other session problems are left to the next firing, which records them.
        """
        from omnigent.server.scheduled.fire import (
            UNATTENDED_PERMISSION_ERROR_CODE,
            continued_session_problem,
        )

        assert task.session_id is not None
        conv = await asyncio.to_thread(conversation_store.get_conversation, task.session_id)
        problem = await continued_session_problem(
            task,
            conv,
            host_store=getattr(request.app.state, "host_store", None),
            permission_store=permission_store,
        )
        if problem is not None and problem[1] == UNATTENDED_PERMISSION_ERROR_CODE:
            raise OmnigentError(
                f"cannot continue this session: {problem[0]}", code=ErrorCode.INVALID_INPUT
            )

    async def _require_runnable_workflow(request: Request, prompt: str, owner: str) -> None:
        """Refuse a schedule's prompt that runs a saved workflow its owner can't run.

        Every run starts as a run of the workflow the prompt names, so none could start.

        :param prompt: The automation's prompt, e.g.
            ``'/workflow Run the saved workflow "Triage" (ID 0123456789abcdef0123456789abcdef).'``
        :param owner: The automation's owner, e.g. ``"alice@example.com"``.
        :raises OmnigentError: If workflows are off, or the owner can't run that workflow.
        """
        workflow_id = await asyncio.to_thread(saved_workflow_id, prompt)
        if workflow_id is not None:
            await _runnable_workflow(request, workflow_id, owner)

    async def _runnable_workflow(request: Request, workflow_id: str, owner: str) -> Workflow:
        """The saved workflow *workflow_id*, if its owner can start runs of it.

        :param workflow_id: The workflow, e.g. ``"0123456789abcdef0123456789abcdef"``.
        :param owner: The automation's owner, e.g. ``"alice@example.com"``.
        :raises OmnigentError: If workflows are off, or the owner can't run that workflow.
        """
        from omnigent.server.session_journal import load_workflow_for_run

        require_workflows(request)
        return await load_workflow_for_run(
            conversation_store, workflow_id, user_id=owner, permission_store=permission_store
        )

    async def _apply_workflow_update(
        request: Request, existing: ScheduledTask, fields: dict[str, Any], owner: str
    ) -> bool:
        """Turn an update's ``workflow_id`` and ``workflow_details`` into the prompt it saves.

        While a task runs a saved workflow the server writes its prompt, and only
        ``workflow_id`` null stops running it, so a reworded prompt can't. A prompt in the
        exact form that runs a workflow, as older clients write it, saves that workflow.

        :param fields: The update's set fields; ``workflow_details`` is consumed, and
            ``prompt`` and ``workflow_id`` are set to what the task saves.
        :returns: Whether ``prompt`` or ``workflow_id`` came from the snapshot rather than
            the request, so they mustn't land on a row that has changed since.
        """
        if fields.get("prompt") == existing.prompt:
            # Resent unchanged, as the agent tool does, it mustn't undo a concurrent edit.
            fields.pop("prompt")
        current = await asyncio.to_thread(task_workflow_id, existing)
        saved_run = (
            await asyncio.to_thread(saved_workflow_run, existing.prompt)
            if current is not None
            else None
        )
        saved_details = saved_run.details if saved_run is not None else ""
        details_given = "workflow_details" in fields
        details = normalize_workflow_details(fields.pop("workflow_details", None) or "")
        prompt = fields.get("prompt")
        prompt_changed = prompt is not None and prompt != existing.prompt
        if "workflow_id" in fields and fields["workflow_id"] is None:
            if details_given:
                raise OmnigentError(_DETAILS_WITHOUT_WORKFLOW_MSG, code=ErrorCode.INVALID_INPUT)
            if (current is not None and not prompt_changed) or (
                prompt is not None
                and prompt_changed
                and await asyncio.to_thread(saved_workflow_id, prompt)
            ):
                raise OmnigentError(_STOP_WORKFLOW_NEEDS_PROMPT_MSG, code=ErrorCode.INVALID_INPUT)
            return False
        if "workflow_id" in fields or details_given:
            target = fields.get("workflow_id") or current
            if target is None:
                raise OmnigentError(_DETAILS_WITHOUT_WORKFLOW_MSG, code=ErrorCode.INVALID_INPUT)
            if prompt_changed:
                raise OmnigentError(_PROMPT_WITH_WORKFLOW_MSG, code=ErrorCode.INVALID_INPUT)
            if target != current or (details_given and details != saved_details):
                if current is not None and not is_bare_saved_workflow_run(existing.prompt):
                    raise OmnigentError(_WORKFLOW_PROMPT_WRAPPED_MSG, code=ErrorCode.INVALID_INPUT)
                workflow = await _runnable_workflow(request, target, owner)
                agent = await _target_agent(existing, fields, owner)
                derived = "workflow_id" not in fields or not details_given
                fields["prompt"] = await _workflow_run_message(
                    agent, workflow, details if details_given else saved_details
                )
                fields["workflow_id"] = workflow.id
                return derived
            # Unchanged, so other edits save without the workflow, which may be gone.
            fields["workflow_id"] = current
        if (
            saved_run is not None
            and not prompt_changed
            and fields.get("agent_id") not in (None, existing.agent_id)
            and is_bare_saved_workflow_run(existing.prompt)
        ):
            harness = await scheduled_agent_harness(
                await _target_agent(existing, fields, owner), agent_cache
            )
            prefix = workflow_skill_prefix(harness)
            # Only a harness that invokes the workflow skill another way needs a new prompt.
            if skill_prefix(existing.prompt) != prefix:
                fields["prompt"] = saved_workflow_run_message(
                    prefix, saved_run.name, saved_run.workflow_id, saved_run.details
                )
                fields["workflow_id"] = saved_run.workflow_id
                return True
        if not prompt_changed or prompt is None:
            return "workflow_id" in fields
        run = await asyncio.to_thread(saved_workflow_run, prompt)
        if run is not None:
            fields["workflow_id"] = run.workflow_id
        elif current is not None:
            raise OmnigentError(_WORKFLOW_PROMPT_OWNED_MSG, code=ErrorCode.INVALID_INPUT)
        return False

    async def _target_agent(existing: ScheduledTask, fields: dict[str, Any], owner: str) -> Agent:
        """The agent an update leaves *existing* bound to."""
        return await validate_session_agent(
            user_id=None if owner == RESERVED_USER_LOCAL else owner,
            agent_id=fields.get("agent_id") or existing.agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )

    async def _workflow_run_message(agent: Agent, workflow: Workflow, details: str) -> str:
        """The prompt that runs *workflow*, with the skill prefix *agent*'s harness uses."""
        harness = await scheduled_agent_harness(agent, agent_cache)
        return saved_workflow_run_message(
            workflow_skill_prefix(harness), workflow.name, workflow.id, details
        )

    @router.post("/scheduled-tasks")
    async def create_scheduled_task(
        request: Request,
        body: CreateScheduledTaskRequest,
    ) -> dict[str, Any]:
        """Create a scheduled task and arm it on the live scheduler."""
        return await _create_task(request, body)

    @router.post("/scheduled-tasks/{scheduled_task_id}/clone")
    async def clone_scheduled_task(
        request: Request,
        scheduled_task_id: str,
        body: CloneScheduledTaskRequest | None = None,
    ) -> dict[str, Any]:
        """Copy an owned automation under a new ID, without its runs or continued session.

        Needs the database scheduler: the legacy timer re-anchors an interval
        schedule whenever it arms one, so it can't run the clone on the original's days.
        """
        if not getattr(request.app.state, "scheduled_task_dispatch_enabled", False):
            raise OmnigentError(
                "Cloning an automation needs automations_v2, which this server does not enable",
                code=ErrorCode.INVALID_INPUT,
            )
        owner = _owner(request)
        task = _require_owned(scheduled_task_id, None if owner == RESERVED_USER_LOCAL else owner)
        if task.kind != "automation":
            raise OmnigentError("Only an automation can be cloned", code=ErrorCode.INVALID_INPUT)
        if body is not None and body.host_id is not None and body.host_id != task.host_id:
            raise OmnigentError(
                "host_id must be the automation's own host", code=ErrorCode.INVALID_INPUT
            )
        try:
            create = _clone_request(task)
        except ValidationError as exc:
            raise OmnigentError(
                f"This automation can't be cloned: {exc.errors()[0]['msg']}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if create.project_id is not None and (
            project_store is None
            or await asyncio.to_thread(
                project_store.get, create.project_id, user_id=_project_owner(owner)
            )
            is None
        ):
            # A run files nothing into a deleted project, so the copy starts unfiled.
            create = create.model_copy(update={"project_id": None})
        return await _create_task(request, create, cloned_from=task)

    async def _create_task(
        request: Request,
        body: CreateScheduledTaskRequest,
        *,
        cloned_from: ScheduledTask | None = None,
    ) -> dict[str, Any]:
        """Create a task from *body*; a clone may copy *cloned_from*'s source copy and pause."""
        owner = _owner(request)
        require_agent_origin(
            request,
            action=_AGENT_AUTOMATION_ACTION,
            owner=owner,
            auth_provider=auth_provider,
            conversation_store=conversation_store,
            permission_store=permission_store,
        )
        _reject_proactive_agent(body.agent_id)
        workflow: Workflow | None = None
        if body.workflow_id is not None:
            # Each of its runs is a run of the workflow, in a session of its own.
            if body.session_mode == "continue":
                raise OmnigentError(CONTINUED_SESSION_MESSAGE, code=ErrorCode.INVALID_INPUT)
            workflow = await _runnable_workflow(request, body.workflow_id, owner)
        else:
            # The request model requires a prompt without workflow_id.
            typed_prompt = body.prompt or ""
            _refuse_workflow_reuse(await _workflow_reuse_problem(typed_prompt, body.session_mode))
            await _require_runnable_workflow(request, typed_prompt, owner)
        await _validate_rrule_or_400(
            body.rrule, allow_single_fire=one_time_schedules_supported(request)
        )
        if body.first_run_at is not None and not one_time_schedules_supported(request):
            # Only the database scheduler saves the next run the check compares.
            raise OmnigentError(
                "first_run_at needs one-time automations, which this server does not enable",
                code=ErrorCode.INVALID_INPUT,
            )
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        timezone = await asyncio.to_thread(_resolve_create_timezone, body, store, owner_id)
        permission_mode = validate_scheduled_permission_mode(body.permission_mode)
        agent, workspace, model_override, reasoning_effort = await _validate_launch_inputs(
            request,
            owner=owner,
            agent_id=body.agent_id,
            host_id=body.host_id,
            workspace=body.workspace,
            model_override=body.model_override,
            reasoning_effort=body.reasoning_effort,
            permission_mode=permission_mode,
            execution_target=body.execution_target,
            continues_session=body.session_id is not None,
        )
        if workflow is not None:
            prompt = await _workflow_run_message(agent, workflow, body.workflow_details or "")
            workflow_id: str | None = workflow.id
        else:
            prompt = body.prompt or ""
            # A client that writes the run message itself saves the workflow it names.
            workflow_id = await asyncio.to_thread(saved_workflow_id, prompt)
        project_owner = _project_owner(owner)
        # Known before the task exists, since a sandbox session must run on the
        # sandbox host derived from it.
        async with _allocated_task_id(owner_id, body.name) as task_id:
            host_id = body.host_id
            source: Conversation | None = None
            source_up_to = ""
            carry = False
            mcp_services = await _validated_mcp_services(
                request, body.mcp_services, agent, owner, body.execution_target
            )
            if body.session_id is not None:
                # A picked session keeps the agent it runs, so the task takes no copy.
                bound = agent
                picked = await _require_continuable(
                    request,
                    ScheduledTask(
                        id=task_id,
                        name=body.name,
                        prompt=prompt,
                        rrule=body.rrule,
                        user_id=owner_id,
                        agent_id=agent.id,
                        timezone=timezone,
                        created_at=0,
                        workspace=workspace,
                        base_branch=body.base_branch,
                        host_id=body.host_id,
                        execution_target=body.execution_target,
                        permission_mode=permission_mode,
                        session_mode=body.session_mode,
                        session_id=body.session_id,
                    ),
                )
                _require_mcp_services_fit_session(request, picked, mcp_services)
                # The session cannot move, so the task routes every run to its host.
                if host_id is None and body.execution_target != "managed_sandbox":
                    host_id = picked.host_id
            else:
                if body.source_session_id is not None:
                    source, source_up_to = await _require_source_session(
                        body.source_session_id,
                        owner,
                        cloned_task_id=cloned_from.id if cloned_from is not None else None,
                    )
                    # Runs rebuild the copied history in the task's agent, so it must carry one,
                    # and open the files it refers to.
                    await _source_history_carry(agent)
                    await _require_openable_attachments(source.id, agent, source_up_to)
                    carry = await _copy_history_carry(source)
                bound = await _bind_agent(agent, owner_id)
            project_id: str | None = None
            source_copy: Conversation | None = None
            try:
                if body.project_id is not None:
                    project_id = await _owned_project_id(project_owner, body.project_id)
                if source is not None:
                    source_copy = await _copy_source_session(
                        source,
                        up_to=source_up_to,
                        task_id=task_id,
                        name=body.name,
                        owner=owner,
                        project_id=project_id,
                        carry=carry,
                        browser_labels=browser_agent_tools_creation_labels(request.headers),
                    )
                # Synchronous, so a cancel can't land after the save and release the saved ID.
                task = store.create(
                    scheduled_task_id=task_id,
                    name=body.name,
                    prompt=prompt,
                    rrule=body.rrule,
                    user_id=owner_id,
                    agent_id=bound.id,
                    timezone=timezone,
                    model_override=model_override,
                    reasoning_effort=reasoning_effort,
                    permission_mode=permission_mode,
                    mcp_services=mcp_services,
                    max_cost_usd=body.max_cost_usd,
                    session_mode=body.session_mode,
                    session_id=body.session_id,
                    source_session_id=source_copy.id if source_copy is not None else None,
                    workflow_id=workflow_id,
                    workspace=workspace,
                    base_branch=body.base_branch,
                    host_id=host_id,
                    project_id=project_id,
                    execution_target=body.execution_target,
                    first_run_at=body.first_run_at,
                    kind=body.kind,
                    # A clone of a paused automation stays paused, so it can't run unasked.
                    state=(
                        "paused"
                        if cloned_from is not None and cloned_from.state == "paused"
                        else "active"
                    ),
                    # An interval schedule's phase follows the creation time, so a
                    # clone keeps the original's to run on the same days.
                    schedule_anchor=cloned_from.created_at if cloned_from is not None else None,
                )
            except Exception as exc:
                if source_copy is not None:
                    await _delete_session_quietly(source_copy.id)
                if bound.id != agent.id:
                    agent_store.delete(bound.id)
                if isinstance(exc, ScheduledTaskFirstRunMismatchError):
                    actual = (
                        datetime.fromtimestamp(exc.actual, UTC).isoformat()
                        if exc.actual is not None
                        else "never"
                    )
                    raise OmnigentError(
                        "The schedule's first run would be "
                        f"{actual}, not {datetime.fromtimestamp(exc.expected, UTC).isoformat()} "
                        "as requested: the request may have arrived after that time, the "
                        "client's clock may be behind the server's, or the rule may not match "
                        "it. The task was not created.",
                        code=ErrorCode.INVALID_INPUT,
                    ) from exc
                if isinstance(exc, RRuleSearchError):
                    raise OmnigentError(
                        "Could not calculate the next run time for this schedule. "
                        "The task was not created.",
                        code=ErrorCode.INVALID_INPUT,
                    ) from exc
                raise
        if source_copy is not None:
            from omnigent.server.routes._sessions.helpers import _announce_session_added

            # Surface the copy in the owner's open sidebars, keyed as they subscribe.
            _announce_session_added(require_user(request, auth_provider), source_copy.id)
        log_task_created(
            task,
            owner=owner,
            source=request.headers.get("x-omnigent-automation-source"),
            template=request.headers.get("x-omnigent-automation-template"),
        )
        scheduler = _scheduler(request)
        if scheduler is not None and not getattr(
            request.app.state, "scheduled_task_dispatch_enabled", False
        ):
            scheduler.add(task)
        # A freshly created task has no runs (last_run_status is None); its
        # next_run_at comes from the legacy timer or the persisted schedule.
        return _to_response(
            task,
            shareable=scheduled_task_shareable(permission_store, task.id),
            hide_trigger_rrule=_hides_trigger_rrule(request),
            next_run_at=_next_run_at(task, request),
        )

    @router.put("/scheduled-tasks/default-timezone")
    async def set_default_timezone(
        request: Request,
        body: SetDefaultTimezoneRequest,
    ) -> dict[str, str]:
        """Record the caller's local zone for tasks that explicitly request the default."""
        owner = _owner(request)
        _validate_timezone_or_400(body.timezone)
        try:
            await asyncio.to_thread(
                store.set_default_timezone,
                None if owner == RESERVED_USER_LOCAL else owner,
                body.timezone,
            )
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=501, detail="default timezone is not supported"
            ) from exc
        return {"timezone": body.timezone}

    @router.get("/scheduled-tasks")
    async def list_scheduled_tasks(
        request: Request,
        kind: Literal["automation", "all"] = Query(default="automation"),
    ) -> dict[str, list[dict[str, Any]]]:
        """List the caller's scheduled tasks.

        Lists automations unless *kind* is ``all``, so the reminders and timers agents
        set for their own sessions stay off the Automations page. The kind filter runs
        in the query, so listing automations never reads an agent's timers. There is no
        reminders-only view; a selective view needs an indexed, paginated query first.

        Lazy-on-read stale backstop: before returning, force-fail any of this
        owner's runs still ``running`` past the 6h max age (``incomplete``), so
        a future Tasks-list "last-run status" badge never shows a stale orphan
        as ``running``. Pure age check — one indexed, owner-scoped query for the
        owner's running runs, then a conditional ``update_run``; NO per-run
        conversation I/O. Young in-flight runs are untouched, and completion of
        a normal run is handled event-driven (the ``_publish_status`` hook), not
        here.
        """
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        tasks = [
            t
            for t in store.list(
                owner_user_id=owner_id,
                exclude_agent_ids=PROACTIVE_AGENT_IDS,
                kinds=None if kind == "all" else ("automation",),
            )
            if owns_scheduled_task(t, owner_id)
        ]
        task_ids = [t.id for t in tasks]
        running = store.list_running_runs_for_tasks(task_ids)
        # Force-fail stale orphans FIRST so the completion badge below reports a
        # dead run as ``failed`` rather than a stuck ``running``.
        force_fail_stale_runs(store, running)
        latest_status = store.list_latest_run_status_for_tasks(task_ids)
        hide_trigger_rrule = _hides_trigger_rrule(request)
        return {
            "scheduled_tasks": [
                _to_response(
                    t,
                    shareable=scheduled_task_shareable(permission_store, t.id),
                    hide_trigger_rrule=hide_trigger_rrule,
                    last_run_status=latest_status.get(t.id),
                    next_run_at=_next_run_at(t, request),
                )
                for t in tasks
            ]
        }

    @router.get("/scheduled-tasks/inbox", response_model=AutomationFailurePage)
    async def list_automation_inbox(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        # Room for a timestamp and a task ID in any form the store allocates.
        after: str | None = Query(default=None, max_length=256),
    ) -> AutomationFailurePage:
        """Latest sessionless failures; session-linked failures use the session Inbox."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        cursor = None
        if after is not None:
            try:
                timestamp, task_id = after.split(":", 1)
                cursor = (int(timestamp), _inbox_cursor_task_id(task_id))
                if not 0 <= cursor[0] <= 2**63 - 1:
                    raise ValueError("invalid timestamp")
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="Invalid automation Inbox cursor"
                ) from exc
        failures, next_cursor = store.list_inbox_failures(
            owner_user_id=owner_id,
            generation=account_generation(owner_id or RESERVED_USER_LOCAL),
            check_generation=current_account_user() is not None,
            after=cursor,
            limit=limit,
            exclude_agent_ids=PROACTIVE_AGENT_IDS,
        )
        return AutomationFailurePage(
            data=failures,
            next_cursor=f"{next_cursor[0]}:{next_cursor[1]}" if next_cursor else None,
        )

    @router.get("/scheduled-tasks/shared")
    async def list_shared_scheduled_tasks(
        request: Request,
        limit: int = Query(default=50, ge=1, le=MAX_SHARED_SCHEDULED_TASKS_PAGE_SIZE),
        after: str | None = Query(
            default=None, max_length=64, description="Cursor: a previous page's next_cursor"
        ),
    ) -> dict[str, Any]:
        """List one page of the automations other users shared with the caller.

        A grant to the caller or to any of their groups counts, and each automation is
        listed once, ordered by ID. Deleted automations, reminders and the caller's own
        automations are left out, so a page can be short, or empty while
        ``next_cursor`` is set. Each item has the list's shape with ``viewer_role``
        ``"viewer"`` and the owner's ``owner_display_name``. Empty on a server that
        cannot share automations and for the local user. Unlike the owner's list, this
        doesn't fail stale running runs, so it reads one run per automation.

        :param limit: Max automations to return (1–100, default 50).
        :param after: Cursor, a previous page's ``next_cursor``.
        :returns: ``{"scheduled_tasks": [...], "next_cursor": str|null}``.
        :raises OmnigentError: 403 for an agent's request; 422 for a malformed cursor.
        """
        if request_from_agent(request):
            raise OmnigentError(
                "Agents cannot list the automations shared with you",
                code=ErrorCode.FORBIDDEN,
            )
        owner = _owner(request)
        if owner == RESERVED_USER_LOCAL or not scheduled_task_sharing_supported(
            request.app.state, permission_store
        ):
            return {"scheduled_tasks": [], "next_cursor": None}
        assert permission_store is not None
        try:
            cursor = uuid.UUID(after).hex if after is not None else None
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Invalid shared automations cursor"
            ) from exc
        principals = await asyncio.to_thread(permission_store.caller_principals, owner)
        task_ids, next_cursor = await asyncio.to_thread(
            permission_store.list_scheduled_tasks_shared_with,
            principals,
            limit=limit,
            after_scheduled_task_id=cursor,
        )
        loaded = await asyncio.to_thread(store.get_many, task_ids)
        tasks = [
            task
            for task_id in task_ids
            if (task := loaded.get(task_id)) is not None
            and task.state != "deleted"
            and task.kind == "automation"
            and task.user_id != owner
            and not is_proactive_task(task)
        ]
        ids = [task.id for task in tasks]
        # Reads one run per automation; a stale running run fails when its history is read.
        latest_status = await asyncio.to_thread(store.list_latest_run_status_for_tasks, ids)
        names = await _owner_display_names(tasks)
        hide_trigger_rrule = _hides_trigger_rrule(request)
        return {
            "scheduled_tasks": [
                _to_response(
                    task,
                    shareable=scheduled_task_shareable(permission_store, task.id),
                    hide_trigger_rrule=hide_trigger_rrule,
                    last_run_status=latest_status.get(task.id),
                    next_run_at=_next_run_at(task, request),
                    viewer_role="viewer",
                    owner_display_name=names.get(task.user_id or "", task.user_id),
                )
                for task in tasks
            ],
            "next_cursor": next_cursor,
        }

    @router.get("/scheduled-tasks/{scheduled_task_id}")
    async def get_scheduled_task(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Fetch a scheduled task the caller owns or that is shared with them.

        Anyone else gets 404. ``viewer_role`` says which, and a recipient also gets the
        owner's ``owner_display_name``.
        """
        task, role = await _require_readable(request, scheduled_task_id)
        running = store.list_running_runs_for_tasks([task.id])
        force_fail_stale_runs(store, running)
        latest_status = store.list_latest_run_status_for_tasks([task.id])
        owner_name = None
        if role == "viewer":
            owner_name = (await _owner_display_names([task])).get(task.user_id or "", task.user_id)
        return _to_response(
            task,
            shareable=scheduled_task_shareable(permission_store, task.id),
            hide_trigger_rrule=_hides_trigger_rrule(request),
            last_run_status=latest_status.get(task.id),
            next_run_at=_next_run_at(task, request),
            viewer_role=role,
            owner_display_name=owner_name,
        )

    @router.get("/scheduled-tasks/{scheduled_task_id}/runs")
    async def list_scheduled_task_runs(
        request: Request,
        scheduled_task_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List the run history of a scheduled task the caller owns or that is shared with them.

        Anyone else gets 404, as for ``GET /scheduled-tasks/{id}``, so runs aren't
        enumerable across users. Runs come
        back most-recent-first (``scheduled_at DESC``); an empty history is an
        empty list.

        Someone it is shared with gets every run's times, status, ``error_code`` and
        ``conversation_id``, but none of its session's data: ``error_detail``, the
        ``conversation_*`` and ``viewer_*`` fields and ``session_cost_usd`` are null and
        ``conversation_pending_approvals`` is 0, since they may not be able to open
        that session.

        Cursor-paginated so history is never silently truncated: pass ``limit``
        (1-1000, default 100) and ``after`` (a prior page's ``next_cursor``).
        The response is ``{"runs": [...], "next_cursor": <id or null>}``; a
        ``null`` cursor marks the last page.

        Lazy-on-read backstop: before listing, force-fail any of this task's
        runs still ``running`` past the 6h max age (``incomplete``). Completion
        itself is event-driven (the ``_publish_status`` hook); this only
        catches a genuine orphan — a run whose terminal event never fired (host
        died mid-turn) — so the "every run eventually terminal" invariant holds
        without a background poll or startup sweep. Pure age check (no
        conversation I/O); a young in-flight run is untouched, and the
        conditional ``update_run`` never clobbers an already-terminal row.
        """
        _, role = await _require_readable(request, scheduled_task_id)
        runs, next_cursor = store.list_runs(scheduled_task_id, limit=limit, after_id=after)
        runs = force_fail_stale_runs(store, runs)
        if role == "viewer":
            return {
                "runs": [_run_to_response(r, include_error_detail=False) for r in runs],
                "next_cursor": next_cursor,
            }
        # Read state is keyed by the caller's raw identity, as the session
        # read-state routes key it.
        viewer_id = require_user(request, auth_provider)
        # Batch-fetch conversation metadata for runs that produced a session so
        # the frontend can compute unread dots without per-row session GETs.
        # Runs without a conversation_id (skipped/failed) contribute null fields.
        conv_ids = [r.conversation_id for r in runs if r.conversation_id is not None]
        sessions = conversation_store.get_session_activity(conv_ids) if conv_ids else {}
        live_counts = pending_elicitations.counts_for(list(sessions))
        # Only runner-bound sessions need the persisted cross-replica mirror.
        pending_counts = {
            sid: max(live_counts.get(sid, 0), session.pending_elicitation_count or 0)
            if session.runner_id is not None
            else live_counts.get(sid, 0)
            for sid, session in sessions.items()
        }
        pending_ids = [sid for sid, count in pending_counts.items() if count]
        liveness = (
            await asyncio.to_thread(liveness_lookup, pending_ids)
            if liveness_lookup is not None and pending_ids
            else {}
        )
        run_rows = []
        for r in runs:
            session = sessions.get(r.conversation_id) if r.conversation_id is not None else None
            if r.conversation_id is not None and session is not None:
                last_seen, explicit_unread = _read_state_entry(viewer_id, r.conversation_id)
                pending_approvals = pending_counts[r.conversation_id]
                live = liveness.get(r.conversation_id)
                # A disconnected runner's persisted count can outlive its approval requests.
                if live is not None and not live.runner_online:
                    pending_approvals = 0
                # Runs sharing a continued session report its live activity only on
                # the run it's attributed to now; earlier runs keep their own outcome.
                is_current_run = session.current_run_id in (None, r.id)
                run_rows.append(
                    _run_to_response(
                        r,
                        conversation_updated_at=session.updated_at,
                        conversation_status=_list_status_with_starting(
                            _session_status_from_cache(r.conversation_id, session.live_status),
                            r.conversation_id,
                        )
                        if is_current_run
                        else None,
                        viewer_last_seen=last_seen,
                        viewer_unread=explicit_unread,
                        session_cost_usd=session.total_cost_usd,
                        conversation_pending_approvals=pending_approvals if is_current_run else 0,
                    )
                )
            else:
                run_rows.append(_run_to_response(r))
        return {
            "runs": run_rows,
            "next_cursor": next_cursor,
        }

    @router.get(
        "/scheduled-tasks/{scheduled_task_id}/run-steps",
        dependencies=[Depends(require_workflows)],
    )
    async def list_scheduled_task_run_steps(
        request: Request,
        scheduled_task_id: str,
        limit: int = Query(default=20, ge=1, le=50),
        after: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """The plan steps of one page of a scheduled task's runs, for their timelines.

        Pages and authorizes like ``GET /scheduled-tasks/{id}/runs``, most recent first. Each
        run that wrote a plan lists its latest plan within that invocation and
        each step's newest status entry. The first plan's ID still guards actions.
        The response is ``{"runs": [{"run_id", "plan_id", "plan_graph_revision",
        "workflow_id", "plan_name", "is_current", "steps", "statuses"}], "next_cursor"}``.
        Plans are session content, so someone it is shared with always gets
        ``{"runs": [], "next_cursor": null}``.
        """
        from omnigent.server.session_journal import entry_to_response, open_store

        _, role = await _require_readable(request, scheduled_task_id)
        if role == "viewer":
            return {"runs": [], "next_cursor": None}
        runs, next_cursor = store.list_runs(scheduled_task_id, limit=limit, after_id=after)
        journal = open_store(conversation_store)
        served = [run for run in runs if run.conversation_id is not None]
        if journal is None or not served:
            return {"runs": [], "next_cursor": next_cursor}
        plans = await asyncio.to_thread(
            journal.run_start_plans, [(run.id, run.conversation_id or "") for run in served]
        )
        found = [(run, plan) for run, plan in zip(served, plans, strict=True) if plan is not None]
        latest_plans = await asyncio.to_thread(
            journal.run_latest_plans, [plan for _, plan in found]
        )
        revisions = await asyncio.to_thread(journal.plan_graph_revisions, latest_plans)
        statuses = await asyncio.to_thread(journal.plan_step_statuses, latest_plans)
        current_plans = await asyncio.to_thread(
            journal.current_run_plan_ids, [plan for _, plan in found]
        )
        sessions = await asyncio.to_thread(
            conversation_store.get_conversations, [plan.conversation_id for _, plan in found]
        )
        return {
            "runs": [
                {
                    "run_id": run.id,
                    "plan_id": plan.id,
                    "plan_graph_revision": revision,
                    "workflow_id": plan.workflow_id,
                    "plan_name": plan.content.get("name"),
                    "is_current": (
                        plan.id in current_plans
                        and (session := sessions.get(plan.conversation_id)) is not None
                        and session.labels.get(SCHEDULED_TASK_RUN_LABEL_KEY) == run.id
                        and not session.archived
                    ),
                    "steps": latest_plan.content.get("steps", []),
                    "statuses": [
                        entry_to_response(entry).model_dump(mode="json")
                        for entry in step_statuses.values()
                    ],
                }
                for (run, plan), latest_plan, revision, step_statuses in zip(
                    found, latest_plans, revisions, statuses, strict=True
                )
            ],
            "next_cursor": next_cursor,
        }

    @router.get("/scheduled-tasks/{scheduled_task_id}/run-pull-requests")
    async def list_scheduled_task_run_pull_requests(
        request: Request,
        scheduled_task_id: str,
        limit: int = Query(default=20, ge=1, le=50),
        after: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List stored PRs for a page of runs; shared viewers receive no session content."""
        from omnigent.server import session_pull_requests as server_prs

        _, role = await _require_readable(request, scheduled_task_id)
        empty: dict[str, Any] = {"runs": [], "refresh_pending": False, "next_cursor": None}
        if role == "viewer":
            return empty
        runs, next_cursor = store.list_runs(scheduled_task_id, limit=limit, after_id=after)
        pull_request_store = server_prs.open_store(conversation_store)
        served = [run for run in runs if run.conversation_id is not None]
        if pull_request_store is None or not served:
            return {**empty, "next_cursor": next_cursor}
        conversation_ids = list(dict.fromkeys(run.conversation_id or "" for run in served))
        stored, initial_runs = await asyncio.gather(
            asyncio.to_thread(pull_request_store.read_many, conversation_ids),
            asyncio.to_thread(pull_request_store.initial_run_ids, conversation_ids),
        )
        now = time.time()
        refresh_pending = False
        for conversation_id, prs in stored.items():
            if server_prs.has_stale_state(prs, now=now):
                server_prs.request_refresh(conversation_id)
                refresh_pending = True
        rows: list[dict[str, Any]] = []
        for run in served:
            conversation_id = run.conversation_id or ""
            prs = stored.get(conversation_id)
            if prs is None:
                continue
            picked = server_prs.run_pull_requests(
                run.id, prs, created_session=initial_runs.get(conversation_id) == run.id
            )
            if picked:
                rows.append(
                    {
                        "run_id": run.id,
                        "pull_requests": [server_prs.entry_payload(entry) for entry in picked],
                    }
                )
        return {"runs": rows, "refresh_pending": refresh_pending, "next_cursor": next_cursor}

    @router.post("/scheduled-tasks/{scheduled_task_id}/run", status_code=202)
    async def run_scheduled_task_now(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Trigger an immediate fire of one of the caller's scheduled tasks.

        A manual override that reuses the SHARED scheduled-fire path (the same
        ``on_fire`` machinery — session create, owner grant, runner launch,
        prompt dispatch, run recording). Legacy timers fire through
        ``app.state.scheduled_task_run_now`` and share its process-local overlap
        guard. With ``automations_v2``, the request is recorded as a pending run
        that the host's owning replica claims and starts like any occurrence, so
        Run now gets the same lease, recovery, and host routing. Each request is
        its own run; it may overlap a scheduled one.

        Paused tasks ARE runnable here — run-now is an explicit manual override,
        so it does not require the ``active`` state the scheduler enforces.

        Fire-and-forget: like the scheduler, the session create + launch runs in
        the background, so this returns ``202 Accepted`` with the task id rather
        than the finished run. The new run row appears in the task's ``/runs``
        history and in the LIST endpoint's ``last_run_status``. Legacy timers
        return ``409`` when a fire for this task is already in flight. Both return
        ``503`` when the scheduler subsystem is not running.
        The database scheduler also returns ``run_id`` to identify the queued run.
        """
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        task = _require_owned(scheduled_task_id, owner_id)
        _refuse_timer(task)
        if getattr(request.app.state, "scheduled_task_dispatch_enabled", False):
            scheduler = _scheduler(request)
            if scheduler is None:
                raise OmnigentError(
                    "scheduled task scheduler is not running",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            run = await scheduler.run_now(task)
            if run is None:
                raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
            return {"triggered": True, "id": task.id, "run_id": run.id}
        run_now = getattr(request.app.state, "scheduled_task_run_now", None)
        if run_now is None:
            raise OmnigentError(
                "scheduled task scheduler is not running",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        started = await run_now(task.workspace_id, task.id)
        if not started:
            # The row exists (we just loaded it) and paused is allowed, so the
            # only skip reason is an already-in-flight fire for this task.
            raise OmnigentError(
                "a run for this scheduled task is already in flight",
                code=ErrorCode.CONFLICT,
            )
        return {"triggered": True, "id": task.id}

    @router.patch("/scheduled-tasks/{scheduled_task_id}")
    async def update_scheduled_task(
        request: Request,
        scheduled_task_id: str,
        body: UpdateScheduledTaskRequest,
    ) -> dict[str, Any]:
        """Update mutable fields of a task and re-sync the scheduler."""
        owner = _owner(request)
        require_agent_origin(
            request,
            action=_AGENT_AUTOMATION_ACTION,
            owner=owner,
            auth_provider=auth_provider,
            conversation_store=conversation_store,
            permission_store=permission_store,
        )
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        stored = _require_owned(scheduled_task_id, owner_id)
        _refuse_timer(stored)
        _reject_proactive_agent(body.agent_id)
        existing = without_legacy_sandbox_workspace(stored)
        legacy_workspace = existing.workspace != stored.workspace
        if body.timezone is not None:
            _validate_timezone_or_400(body.timezone)
        fields = body.model_dump(exclude_unset=True)
        if fields.get("session_mode") is None:
            fields.pop("session_mode", None)
        if fields.get("mcp_services") is None:
            fields.pop("mcp_services", None)
        derives_workflow_prompt = await _apply_workflow_update(request, existing, fields, owner)
        if await asyncio.to_thread(_align_stored_workflow, existing, fields):
            derives_workflow_prompt = True
        # Whether a new agent needs the prompt's skill prefix rewritten was decided on the
        # snapshot, so a workflow attached since then conflicts too.
        if fields.get("agent_id") not in (None, existing.agent_id):
            derives_workflow_prompt = True
        target_session_mode = fields.get("session_mode", existing.session_mode)
        # The caller's own changes, before the ones derived below.
        requested_session = {
            name: fields[name]
            for name in ("prompt", "session_mode", "session_id", "workflow_id")
            if name in fields
        }
        # Checked on this snapshot, then again on the locked row for a concurrent edit.
        workflow_checks: dict[tuple[str, str, str | None], str | None] = {}
        snapshot_change = _workflow_reuse_change(existing, requested_session)
        if snapshot_change is not None:
            workflow_checks[snapshot_change] = await _workflow_reuse_problem(*snapshot_change)
            _refuse_workflow_reuse(workflow_checks[snapshot_change])
        if body.prompt is not None and body.prompt != existing.prompt:
            await _require_runnable_workflow(request, body.prompt, owner)
        target_execution = fields.get("execution_target") or existing.execution_target
        if body.rrule is not None:
            await _validate_rrule_or_400(
                body.rrule, allow_single_fire=one_time_schedules_supported(request)
            )
        if target_session_mode == "new":
            if fields.get("session_id") is not None:
                raise OmnigentError(_SESSION_ID_WITHOUT_CONTINUE_MSG, code=ErrorCode.INVALID_INPUT)
            fields["session_id"] = None
        # A task continuing a session wakes it in the session's own mode.
        continues_session = fields.get("session_id", existing.session_id) is not None
        target_agent_id = fields.get("agent_id") or existing.agent_id
        agent_changed = target_agent_id != existing.agent_id
        if existing.source_session_id is not None and target_session_mode != "new":
            raise OmnigentError(_SOURCE_SESSION_CONTINUE_MSG, code=ErrorCode.INVALID_INPUT)
        new_copy: Agent | None = None
        if agent_changed:
            # A harness switch invalidates the per-agent settings stored beside
            # it: model ids and permission vocabularies belong to the harness.
            # Clear whichever the caller did not resend so a switched task never
            # fires the new harness with the old one's flags.
            for stale in ("model_override", "reasoning_effort", "permission_mode"):
                fields.setdefault(stale, None)
        if {"model_override", "reasoning_effort"}.intersection(fields):
            model_override, reasoning_effort = validate_session_model_metadata(
                model_override=fields.get("model_override", existing.model_override),
                reasoning_effort=fields.get("reasoning_effort", existing.reasoning_effort),
            )
            if "model_override" in fields:
                fields["model_override"] = model_override
            if "reasoning_effort" in fields:
                fields["reasoning_effort"] = reasoning_effort
        if "permission_mode" in fields:
            new_mode = validate_scheduled_permission_mode(fields["permission_mode"])
            fields["permission_mode"] = new_mode
            # Gate the mode on the agent's harness. Clearing to null falls back to the
            # agent's own default, which must not skip approval unless the task continues
            # a session; an agent switch revalidates that default with the launch inputs.
            if new_mode is not None or not agent_changed:
                agent = await validate_session_agent(
                    user_id=owner_id,
                    agent_id=target_agent_id,
                    agent_store=agent_store,
                    permission_store=permission_store,
                    conversation_store=conversation_store,
                )
                await validate_scheduled_permission_support(
                    permission_mode=new_mode,
                    agent=agent,
                    agent_cache=agent_cache,
                    continues_session=continues_session,
                )
        if "mcp_services" in fields or (
            existing.mcp_services
            and (
                agent_changed
                or target_execution != existing.execution_target
                or fields.get("session_mode", existing.session_mode) != existing.session_mode
            )
        ):
            # A rebound agent, or a change of where the next run lands (a sandbox
            # in place of a host, or a new sandbox in place of the one a continuing
            # task keeps), must still accept the services the task keeps.
            mcp_agent = await validate_session_agent(
                user_id=owner_id,
                agent_id=target_agent_id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
            # A continuing sandbox task's next run joins the sandbox it already has.
            continues_in_sandbox = (
                target_execution == "managed_sandbox"
                and fields.get("session_mode", existing.session_mode) == "continue"
            )
            fields["mcp_services"] = await _validated_mcp_services(
                request,
                list(fields.get("mcp_services", existing.mcp_services)),
                mcp_agent,
                owner,
                target_execution,
                continued_sandbox_host_id(existing.id, existing.workspace_id)
                if continues_in_sandbox
                else None,
            )
        switching_to_managed = target_execution == "managed_sandbox"
        if not switching_to_managed and "workspace" in fields and fields["workspace"] is None:
            # Target-dependent validation retains the schema's original 422 response.
            raise RequestValidationError(
                [
                    {
                        "type": "value_error",
                        "loc": ("body",),
                        "msg": "Value error, workspace cannot be null",
                        "input": await request.json(),
                        "ctx": {"error": ValueError("workspace cannot be null")},
                    }
                ]
            )
        # Check the effective target even when the PATCH omits execution_target.
        if switching_to_managed and fields.get("host_id") is not None:
            raise OmnigentError(
                _MANAGED_SANDBOX_WITH_HOST_MSG,
                code=ErrorCode.INVALID_INPUT,
            )
        target_changed = target_execution != existing.execution_target
        if switching_to_managed and fields.get("base_branch") is None:
            fields["base_branch"] = None
        try:
            _validate_worktree_target(
                fields.get("base_branch", existing.base_branch),
                target_execution,
                fields.get("host_id", existing.host_id),
                fields.get("workspace", None if target_changed else existing.workspace),
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        if agent_changed or {"workspace", "host_id", "execution_target"}.intersection(fields):
            # Revalidate placement against the selected agent and execution target.
            agent, workspace, _, _ = await _validate_launch_inputs(
                request,
                owner=owner,
                agent_id=target_agent_id,
                host_id=None if switching_to_managed else fields.get("host_id", existing.host_id),
                workspace=(
                    fields.get("workspace", None if target_changed else existing.workspace)
                ),
                model_override=fields.get("model_override", existing.model_override),
                reasoning_effort=fields.get("reasoning_effort", existing.reasoning_effort),
                permission_mode=None,
                execution_target=target_execution,
                continues_session=continues_session,
            )
            if agent_changed and existing.source_session_id is not None:
                # Runs rebuild the copied history in the new agent, so it must carry one
                # and open the files it refers to.
                await _source_history_carry(agent)
                await _require_openable_attachments(existing.source_session_id, agent)
            if switching_to_managed:
                # A sandbox has no pinned host; its workspace is a repository URL.
                fields["host_id"] = None
            if "workspace" in fields or target_changed:
                fields["workspace"] = workspace
            # A picked session keeps the agent it runs, so the task takes no copy.
            if agent_changed and fields.get("session_id") is None:
                bound = await _bind_agent(agent, owner_id)
                fields["agent_id"] = bound.id
                new_copy = bound if bound.id != agent.id else None
        if legacy_workspace:
            fields.setdefault("workspace", None)
        if "session_id" not in fields and any(
            name in fields and fields[name] != getattr(existing, name)
            for name in _SESSION_BOUND_FIELDS
        ):
            fields["session_id"] = None
        # The task as this update leaves it, for checks against the session it continues.
        continuing = replace(
            existing,
            **{
                name: fields[name]
                for name in (
                    "agent_id",
                    "host_id",
                    "workspace",
                    "base_branch",
                    "execution_target",
                    "permission_mode",
                    "session_mode",
                    "session_id",
                )
                if name in fields
            },
        )
        if fields.get("session_id") is not None:
            picked = await _require_continuable(request, continuing)
            _require_mcp_services_fit_session(
                request, picked, fields.get("mcp_services", existing.mcp_services)
            )
            # The session cannot move, so the task routes every run to its host.
            if (
                fields.get("host_id", existing.host_id) is None
                and fields.get("execution_target", existing.execution_target) != "managed_sandbox"
            ):
                fields["host_id"] = picked.host_id
        elif "permission_mode" in fields and continuing.continues_session:
            await _require_mode_fits_session(request, continuing)
        project_owner = _project_owner(owner)
        cancellation: asyncio.CancelledError | None = None
        # Rechecked under the task's lock, so a grant made meanwhile counts.
        shared_target_guard = agent_shared_target_guard(request, fields, permission_store)

        def guard(locked: ScheduledTask) -> None:
            if shared_target_guard is not None:
                shared_target_guard(locked)
            # Checked on the snapshot: if the prompt or workflow changed since, refuse what
            # the snapshot decided, and a write that would split the workflow from its prompt.
            if (locked.prompt, locked.workflow_id) != (existing.prompt, existing.workflow_id) and (
                derives_workflow_prompt or _breaks_workflow_prompt(locked, fields)
            ):
                raise OmnigentError(_WORKFLOW_CHANGED_MSG, code=ErrorCode.CONFLICT)
            # Another update may have changed the prompt or session mode since the snapshot.
            locked_change = _workflow_reuse_change(locked, requested_session)
            if locked_change is not None:
                if locked_change not in workflow_checks:
                    workflow_checks[locked_change] = workflow_session_reuse_error(*locked_change)
                _refuse_workflow_reuse(workflow_checks[locked_change])

        try:
            # On the derived fields, so a session the update clears counts as a change.
            await require_agent_keeps_shared_target(request, fields, existing, permission_store)
            if fields.get("project_id") is not None:
                fields["project_id"] = await _owned_project_id(project_owner, fields["project_id"])
            # Off the event loop: a deployment's store may call out while it writes.
            updating = asyncio.ensure_future(
                asyncio.to_thread(
                    store.update,
                    scheduled_task_id,
                    expected_schedule=existing,
                    guard=guard,
                    **fields,
                )
            )
            try:
                updated = await asyncio.shield(updating)
            except asyncio.CancelledError as cancelled:
                # The write runs on; settle it as usual, then let the cancel propagate.
                cancellation = cancelled
                await await_cleanup_task(updating)
                updated = updating.result()
            if updated is None:
                raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        except Exception as exc:
            # The task never bound the copy, so nothing else uses it.
            if new_copy is not None:
                agent_store.delete(new_copy.id)
            if cancellation is not None:
                raise cancellation from exc
            if isinstance(exc, RRuleSearchError):
                raise OmnigentError(
                    "Could not calculate the next run time for this schedule. "
                    "Your changes were not saved.",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
            raise
        scheduler = _scheduler(request)
        if scheduler is not None and not getattr(
            request.app.state, "scheduled_task_dispatch_enabled", False
        ):
            scheduler.update(updated)
        if cancellation is not None:
            raise cancellation
        latest_status = store.list_latest_run_status_for_tasks([updated.id])
        return _to_response(
            updated,
            shareable=scheduled_task_shareable(permission_store, updated.id),
            hide_trigger_rrule=_hides_trigger_rrule(request),
            last_run_status=latest_status.get(updated.id),
            next_run_at=_next_run_at(updated, request),
        )

    @router.delete("/scheduled-tasks/{scheduled_task_id}")
    async def delete_scheduled_task(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Check ownership, then delete the task and history in a worker thread.

        The ownership lookup remains synchronous; remove the timer after deletion.
        """
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        task = _require_owned(scheduled_task_id, owner_id)
        from omnigent.stores.conversation_store import SCHEDULED_TASK_SOURCE_LABEL_KEY

        copy_id = task.source_session_id
        if copy_id is not None and (
            await asyncio.to_thread(conversation_store.get_conversation, copy_id) is None
        ):
            copy_id = None
        if copy_id is not None:
            # The copy stays, like a run's session, but no longer belongs to an automation.
            # Cleared first: if that fails, the automation and its label stay for a retry.
            await asyncio.to_thread(
                conversation_store.delete_label, copy_id, SCHEDULED_TASK_SOURCE_LABEL_KEY
            )
        try:
            await asyncio.to_thread(store.delete, scheduled_task_id)
        except Exception:
            if copy_id is not None:
                # The automation stays, so its copy goes on naming it.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        conversation_store.set_labels,
                        copy_id,
                        {SCHEDULED_TASK_SOURCE_LABEL_KEY: task.id},
                    )
            raise
        # After the task, so a grant racing the delete is refused rather than left behind.
        await delete_task_grants(permission_store, scheduled_task_id)
        scheduler = _scheduler(request)
        if scheduler is not None and not getattr(
            request.app.state, "scheduled_task_dispatch_enabled", False
        ):
            scheduler.remove(scheduled_task_id)
        return {"deleted": True, "id": scheduled_task_id}

    def _load_owned_by_caller(request: Request, scheduled_task_id: str) -> ScheduledTask:
        owner = _owner(request)
        task = _require_owned(scheduled_task_id, None if owner == RESERVED_USER_LOCAL else owner)
        # A grant would also share the timer's session.
        _refuse_timer(task)
        return task

    register_scheduled_task_permissions_routes(
        router,
        load_owned_task=_load_owned_by_caller,
        conversation_store=conversation_store,
        permission_store=permission_store,
    )

    # Agent timers are a session primitive, so the per-request automations gate
    # doesn't hide them; they still need the database scheduler.
    timers = APIRouter()

    async def _timer_session(request: Request, session_id: str) -> tuple[str | None, Conversation]:
        """Authorize a timer call as a message would be.

        :returns: The timer owner, ``None`` for the local user as on every task, and
            the session.
        """
        user_id = require_user(request, auth_provider)
        access = await require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        return (None if user_id == RESERVED_USER_LOCAL else user_id), conv

    # Called by the runner's timer tools, so hidden from the public API reference.
    @timers.post(
        "/sessions/{session_id}/timers",
        status_code=201,
        include_in_schema=False,
        # The reply is a saved timer, or a JSONResponse telling the runner to keep it.
        response_model=None,
    )
    async def set_session_timer(
        request: Request, session_id: str, body: dict[str, Any]
    ) -> dict[str, Any] | JSONResponse:
        """Save a ``sys_timer_set`` timer: a hidden wake into this session.

        One scheduled task holds the timer, so it survives runner and server restarts,
        and its ID is the ``timer_id``. The first firing falls on the whole second at or
        after ``seconds`` from now, so it never comes early; a repeating timer then fires
        every ``seconds`` rounded up to a whole second. Firings that come due while the
        server is down make one late firing when it is back, not one each. A sub-agent's
        or side chat's timer isn't saved: the reply, ``200`` with ``durable`` false and a
        ``reason``, tells the runner to keep it in memory.

        :param body: ``{"seconds": number, "repeat": bool, "note": str | null}``, checked
            like the tool's arguments, e.g. ``{"seconds": 120, "repeat": true}``.
        :returns: ``durable`` true, ``timer_id``, ``status`` ``"scheduled"``, the UTC
            ``fire_at`` of the first firing, ``interval_seconds`` for a repeating timer
            (else ``None``), and the ``seconds``, ``repeat`` and ``note`` it was set with.
        :raises OmnigentError: 400 for bad arguments, a delay under
            ``DURABLE_TIMER_MIN_SECONDS``, an archived or closed session, or a server with
            timers off (no database scheduler, or the ``session_timers`` feature off);
            403/404 as for posting a message.
        """
        if not getattr(request.app.state, "session_timers_enabled", False):
            raise OmnigentError(
                "Timers are off on this server: they need the database scheduler and the "
                "session_timers feature",
                code=ErrorCode.INVALID_INPUT,
            )
        unknown = sorted(set(body) - {"seconds", "repeat", "note"})
        if unknown:
            raise OmnigentError(
                f"unknown timer fields: {', '.join(unknown)}", code=ErrorCode.INVALID_INPUT
            )
        validated = validate_timer_set_args(body)
        if isinstance(validated, str):
            raise OmnigentError(validated, code=ErrorCode.INVALID_INPUT)
        seconds, repeat, note = validated
        if seconds < DURABLE_TIMER_MIN_SECONDS:
            raise OmnigentError(
                f"The server saves timers of at least {DURABLE_TIMER_MIN_SECONDS} seconds; "
                "the runner keeps shorter ones",
                code=ErrorCode.INVALID_INPUT,
            )
        user_id, conv = await _timer_session(request, session_id)
        if conv.archived:
            raise OmnigentError(
                "This session is archived, so it can't take timers",
                code=ErrorCode.INVALID_INPUT,
            )
        if is_session_closed(conv.labels, conv.title):
            raise OmnigentError(
                "This session is closed to new messages, so it can't take timers",
                code=ErrorCode.INVALID_INPUT,
            )
        if conv.agent_id is None:
            raise OmnigentError(
                "This session has no agent for a timer to wake", code=ErrorCode.INVALID_INPUT
            )
        from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY

        if conv.parent_conversation_id is not None or conv.labels.get(SIDE_CHAT_LABEL_KEY):
            # It runs on another session's runner, so its timers stay in that runner.
            return JSONResponse(
                {"durable": False, "reason": "a sub-agent's or side chat's timers aren't saved"}
            )
        agent_id = conv.agent_id
        interval = max(1, math.ceil(seconds))
        fire_at = math.ceil(time.time() + seconds)
        async with _allocated_task_id(user_id, "Timer") as task_id:
            # Synchronous, so a cancel can't land after the save and release the saved ID.
            task = store.create(
                scheduled_task_id=task_id,
                name=f"Timer: {note}"[:256] if note else "Timer",
                # The hidden message each firing sends into the session.
                prompt=f"[System: timer {task_id} fired]" + (f"\nnote: {note!r}" if note else ""),
                rrule=timer_rule(interval, repeat=repeat),
                user_id=user_id,
                agent_id=agent_id,
                timezone="UTC",
                session_mode="continue",
                session_id=conv.id,
                # Runs route to the replica holding the session's host.
                host_id=conv.host_id,
                first_run_at=fire_at,
                kind="timer",
                # Firings fall at the anchor plus whole intervals, the first at fire_at.
                schedule_anchor=fire_at - interval,
            )
        return {
            "durable": True,
            "timer_id": task.id,
            "status": "scheduled",
            "fire_at": datetime.fromtimestamp(fire_at, UTC).isoformat(),
            "interval_seconds": interval if repeat else None,
            "seconds": seconds,
            "repeat": repeat,
            "note": note,
        }

    @timers.delete("/sessions/{session_id}/timers/{timer_id}", include_in_schema=False)
    async def cancel_session_timer(
        request: Request, session_id: str, timer_id: str
    ) -> dict[str, str]:
        """Cancel one of this session's timers by deleting its scheduled task.

        A firing still waiting to go out is suppressed; one already on its way still
        arrives. Repeating is safe: a cancelled, fired or unknown timer is a 404.

        :returns: ``{"timer_id": "...", "status": "cancelled"}``.
        :raises OmnigentError: 404 unless the caller set this timer in this session and
            it has not fired or been cancelled yet.
        """
        user_id, conv = await _timer_session(request, session_id)
        task = await asyncio.to_thread(store.get, timer_id)
        if (
            task is None
            or task.kind != "timer"
            or task.state == "deleted"
            or task.session_id != conv.id
            or task.user_id != user_id
            or not await asyncio.to_thread(store.delete, task.id)
        ):
            raise OmnigentError("Timer not found", code=ErrorCode.NOT_FOUND)
        return {"timer_id": task.id, "status": "cancelled"}

    combined = APIRouter()
    combined.include_router(router)
    combined.include_router(timers)
    return combined
