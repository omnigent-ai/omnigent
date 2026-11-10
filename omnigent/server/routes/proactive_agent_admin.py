"""Admin controls for iterating on the proactive agent: its prompt and Run now.

Every route returns 404 where the deployment does not offer the agent, and
403 to callers who are not admins.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities import ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.proactive_agent import PROACTIVE_AGENT_ID, find_proactive_task
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores import PermissionStore
from omnigent.stores.proactive_agent_settings_store import (
    MAX_PROMPT_OVERRIDE_BYTES,
    ProactiveAgentSettingsStore,
)

# Most users one Run now may name, and most tasks a "*" run starts.
MAX_RUN_USERS = 100
MAX_RUN_ALL_TASKS = 500
_RUN_ALL_PAGE = 100


class ProactiveAgentPrompt(BaseModel):
    """The prompt the agent runs with.

    :param prompt: The admin's override, or the bundled prompt without one.
    :param default_prompt: The bundled prompt, which a release can change.
    :param overridden: Whether an override replaces the bundled prompt.
    """

    prompt: str
    default_prompt: str
    overridden: bool


class SetProactiveAgentPrompt(BaseModel):
    """Body of a prompt change; ``null`` restores the bundled prompt."""

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(min_length=1, max_length=MAX_PROMPT_OVERRIDE_BYTES)


class RunProactiveAgent(BaseModel):
    """Body of a Run now: user ids, or ``["*"]`` for every user with a task."""

    model_config = ConfigDict(extra="forbid")

    users: list[str] = Field(min_length=1, max_length=MAX_RUN_USERS)


class StartedRun(BaseModel):
    user_id: str
    task_id: str
    run_id: str | None = None


class SkippedRun(BaseModel):
    user_id: str
    reason: Literal["no_task", "turned_off", "already_running", "unavailable"]


class ProactiveAgentRuns(BaseModel):
    """Result of a Run now.

    :param truncated: ``"*"`` matched more than :data:`MAX_RUN_ALL_TASKS`
        tasks; only that many were started.
    """

    started: list[StartedRun]
    skipped: list[SkippedRun]
    truncated: bool = False


def create_proactive_agent_admin_router(
    store: ProactiveAgentSettingsStore,
    *,
    offered: Callable[[], bool],
    default_prompt: Callable[[], str],
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the proactive agent admin router (mounted under ``/v1``).

    :param store: Where the prompt override and the on/off settings live.
    :param offered: Whether the deployment offers the agent to this request's caller.
    :param default_prompt: Returns the agent's bundled prompt.
    :param auth_provider: Auth provider; ``None`` when auth is off.
    :param permission_store: Permission store; ``None`` in single-user mode,
        where the one user is the admin.
    """

    async def require_admin(request: Request) -> None:
        if not offered():
            raise HTTPException(status_code=404, detail="not found")
        if permission_store is None:
            return
        user_id = require_user(request, auth_provider) or RESERVED_USER_LOCAL
        if not await asyncio.to_thread(permission_store.is_admin, user_id):
            raise OmnigentError(
                "Admin privileges required to manage the proactive agent",
                code=ErrorCode.FORBIDDEN,
            )

    router = APIRouter(dependencies=[Depends(require_admin)])

    async def _prompt() -> ProactiveAgentPrompt:
        override = await asyncio.to_thread(store.read_prompt_override)
        default = default_prompt()
        return ProactiveAgentPrompt(
            prompt=override if override is not None else default,
            default_prompt=default,
            overridden=override is not None,
        )

    @router.get("/proactive-agent/prompt", response_model=ProactiveAgentPrompt)
    async def read_prompt() -> ProactiveAgentPrompt:
        return await _prompt()

    @router.put("/proactive-agent/prompt", response_model=ProactiveAgentPrompt)
    async def set_prompt(request: Request, body: SetProactiveAgentPrompt) -> ProactiveAgentPrompt:
        prompt = body.prompt if body.prompt and body.prompt.strip() else None
        try:
            await asyncio.to_thread(store.set_prompt_override, prompt)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        # A new bundle version: the next run of every user's task uses it.
        await asyncio.to_thread(request.app.state.reseed_proactive_agent)
        return await _prompt()

    @router.post("/proactive-agent/runs", response_model=ProactiveAgentRuns)
    async def run_now(request: Request, body: RunProactiveAgent) -> ProactiveAgentRuns:
        tasks_store = getattr(request.app.state, "scheduled_task_store", None)
        if tasks_store is None:
            raise OmnigentError(
                "Scheduled tasks are not available", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        result = ProactiveAgentRuns(started=[], skipped=[])
        if body.users == ["*"]:
            tasks, result.truncated = await _all_tasks(tasks_store)
            targets: list[tuple[str, ScheduledTask | None]] = [
                (_display_user(task.user_id), task) for task in tasks
            ]
        else:
            if "*" in body.users:
                raise OmnigentError(
                    'Use "*" alone to run for every user', code=ErrorCode.INVALID_INPUT
                )
            targets = [
                (
                    user_id,
                    await asyncio.to_thread(find_proactive_task, tasks_store, _owner(user_id)),
                )
                for user_id in dict.fromkeys(user.strip() for user in body.users if user.strip())
            ]
        for user_id, task in targets:
            if task is None:
                result.skipped.append(SkippedRun(user_id=user_id, reason="no_task"))
                continue
            workspace_on, user_on = await asyncio.to_thread(store.read, user_id)
            if not (workspace_on and user_on):
                result.skipped.append(SkippedRun(user_id=user_id, reason="turned_off"))
                continue
            outcome = await _start(request, task)
            if isinstance(outcome, StartedRun):
                result.started.append(outcome.model_copy(update={"user_id": user_id}))
            else:
                result.skipped.append(SkippedRun(user_id=user_id, reason=outcome))
        return result

    return router


def _owner(user_id: str) -> str | None:
    """Single-user mode stores its tasks without an owner."""
    return None if user_id == RESERVED_USER_LOCAL else user_id


def _display_user(user_id: str | None) -> str:
    return user_id if user_id is not None else RESERVED_USER_LOCAL


async def _all_tasks(store: Any) -> tuple[list[ScheduledTask], bool]:
    """Every live proactive task, read in bounded pages, up to the run cap."""
    tasks: list[ScheduledTask] = []
    after: str | None = None
    while True:
        page, after = await asyncio.to_thread(
            store.list_for_agent, PROACTIVE_AGENT_ID, after=after, limit=_RUN_ALL_PAGE
        )
        tasks.extend(task for task in page if task.state == "active")
        if len(tasks) > MAX_RUN_ALL_TASKS:
            return tasks[:MAX_RUN_ALL_TASKS], True
        if after is None:
            return tasks, False


async def _start(
    request: Request, task: ScheduledTask
) -> StartedRun | Literal["already_running", "unavailable"]:
    """Start *task* now the way the scheduled-task Run now does."""
    state = request.app.state
    if getattr(state, "scheduled_task_dispatch_enabled", False):
        scheduler = getattr(state, "scheduled_task_scheduler", None)
        if scheduler is None:
            return "unavailable"
        run = await scheduler.run_now(task)
        if run is None:
            return "unavailable"
        return StartedRun(user_id="", task_id=task.id, run_id=run.id)
    run_now = getattr(state, "scheduled_task_run_now", None)
    if run_now is None:
        return "unavailable"
    if not await run_now(task.workspace_id, task.id):
        return "already_running"
    return StartedRun(user_id="", task_id=task.id)
