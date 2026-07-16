"""The scheduled-task fire path — the real ``on_fire`` the scheduler invokes.

When :class:`~omnigent.server.scheduled.scheduler.ScheduledTaskScheduler` decides
a task is due it calls ``on_fire(scheduled_task_id)``. This module supplies the
real callback (the scheduler ships only a no-op placeholder). A firing:

#. **Re-reads the row.** The armed timer is never trusted: the row is re-read by
   id, and a row that vanished (deleted between arming and firing) or is no
   longer ``active`` (paused/deleted) is a logged no-op.
#. **Creates a session** bound to the task's agent, carrying the stored
   ``workspace`` / ``host_id`` / ``model_override`` / ``reasoning_effort``.
#. **Grants ownership.** The spawned session gets a ``LEVEL_OWNER`` grant for the
   task's ``owner_user_id`` — or :data:`RESERVED_USER_LOCAL` when it is NULL
   (single-user / OSS). Without the grant the run is invisible.
#. **Launches the runner and dispatches the prompt** so the agent actually runs
   (a seeded prompt with no launched runner would just sit as history).
#. **Records the run** — stamps ``last_run_at`` + ``last_run_conversation_id`` on
   the task row and writes a ``scheduled_task_runs`` history row.

**Fire-and-forget.** The re-read + state guard run synchronously so an obviously
dead fire costs nothing, but the session creation / launch is dispatched onto a
background :func:`asyncio.create_task` and ``on_fire`` returns immediately. If it
blocked on full session startup the scheduler could not re-arm the task's timer
for the fire's duration. A strong reference to each in-flight task is held until
it completes (``loop.create_task`` only keeps a weak one). Any failure in the
background work is caught and logged: a failed fire must never crash the
scheduler, and v1's retry policy is simply "the next occurrence fires normally".

**Execution target.** Only ``connected_host`` runs in v1. A ``managed_sandbox``
row is logged and recorded as a ``skipped`` run — provisioning a sandbox from the
fire path is a follow-up; the ``resolve_sandbox`` seam is intentionally left open.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from omnigent.entities import Conversation, ScheduledTask
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.schemas import SessionEventInput

_logger = logging.getLogger(__name__)

# How long to wait for a freshly launched runner to connect before giving up on
# dispatching the prompt this fire. The session + grant are already persisted, so
# a timeout leaves an owner-visible session the runner can still pick up later.
_RUNNER_CONNECT_TIMEOUT_S = 30.0

# Strong references to in-flight background fire tasks. ``loop.create_task`` holds
# only a weak reference, so without this a fire could be garbage-collected
# mid-flight; each task is discarded from the set when it completes.
_PENDING_FIRES: set[asyncio.Task[None]] = set()


# ``launch_dispatch(conv, task)`` — launch the runner for a freshly created
# session and dispatch the task's prompt so the agent runs. Injectable so the
# orchestration can be unit-tested without a live host/runner.
LaunchDispatch = Callable[[Conversation, ScheduledTask], Awaitable[None]]


@dataclass
class FireDeps:
    """The server dependencies the fire path needs, captured at wiring time.

    Mirrors how the scheduler captures its store: the ``on_fire`` factory grabs
    these off ``app.state`` once and closes over them, so a firing never needs a
    FastAPI request.
    """

    scheduled_task_store: Any
    conversation_store: Any
    agent_store: Any
    permission_store: Any | None
    host_store: Any | None
    host_registry: Any | None
    runner_router: Any | None = None
    tunnel_registry: Any | None = None
    file_store: Any | None = None
    artifact_store: Any | None = None


def _prompt_event(prompt: str) -> SessionEventInput:
    """Build the user-message event that carries a task's prompt to the runner."""
    return SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    )


def build_on_fire(
    deps: FireDeps,
    *,
    launch_dispatch: LaunchDispatch | None = None,
) -> Callable[[str], Awaitable[None]]:
    """Build the real ``on_fire`` callback bound to server ``deps``.

    :param deps: Server stores/registries the fire path operates on.
    :param launch_dispatch: Seam that launches the runner and dispatches the
        prompt for a created session. Defaults to the real connected-host
        implementation; tests inject a fake.
    :returns: An ``async on_fire(scheduled_task_id)`` suitable for
        :class:`ScheduledTaskScheduler`.
    """
    dispatch = launch_dispatch or _make_connected_host_dispatch(deps)

    async def on_fire(scheduled_task_id: str) -> None:
        # Re-read the row: never trust the armed timer. A deleted or
        # non-active row is a logged no-op done synchronously.
        task = await asyncio.to_thread(deps.scheduled_task_store.get, scheduled_task_id)
        if task is None:
            _logger.info("scheduled fire: task %s no longer exists — skipping", scheduled_task_id)
            return
        if task.state != "active":
            _logger.info(
                "scheduled fire: task %s is %s (not active) — skipping",
                scheduled_task_id,
                task.state,
            )
            return

        # Fire-and-forget: the session create + launch runs in the background so
        # on_fire returns immediately and the scheduler re-arms the timer now.
        fire_task = asyncio.create_task(
            _run_fire(deps, task, dispatch),
            name=f"scheduled-fire-{scheduled_task_id}",
        )
        _PENDING_FIRES.add(fire_task)
        fire_task.add_done_callback(_PENDING_FIRES.discard)

    return on_fire


async def _run_fire(
    deps: FireDeps,
    task: ScheduledTask,
    dispatch: LaunchDispatch,
) -> None:
    """Background body of a firing: create session, grant, launch, record run.

    Wrapped so any failure is logged rather than propagated — a failed fire must
    not crash the scheduler.
    """
    scheduled_at = int(time.time())
    try:
        # v1 only runs connected_host. A managed_sandbox row is recorded as a
        # skipped run; provisioning from the fire path is a follow-up.
        if task.execution_target != "connected_host":
            _logger.info(
                "scheduled fire: task %s target %r not supported in v1 — skipping",
                task.id,
                task.execution_target,
            )
            await asyncio.to_thread(
                deps.scheduled_task_store.create_run,
                _new_id(),
                task.id,
                "skipped",
                scheduled_at,
                error=f"execution_target {task.execution_target!r} not supported yet",
                error_code="unsupported_target",
            )
            return

        conv = await _create_session(deps, task)
        await _grant_owner(deps, task, conv.id)

        try:
            await dispatch(conv, task)
        except Exception:
            # The session + grant are already persisted and owner-visible, so a
            # launch/dispatch failure still records a run — just a failed one.
            _logger.exception(
                "scheduled fire: launch/dispatch failed for task %s (session %s)",
                task.id,
                conv.id,
            )
            await _record_run(
                deps,
                task,
                conv.id,
                scheduled_at,
                status="failed",
                error="runner launch/dispatch failed",
            )
            return

        await _record_run(deps, task, conv.id, scheduled_at, status="running")
        _logger.info("scheduled fire: task %s fired session %s", task.id, conv.id)
    except Exception:
        _logger.exception("scheduled fire: task %s failed", task.id)


async def _create_session(deps: FireDeps, task: ScheduledTask) -> Conversation:
    """Create a conversation bound to the task's agent, carrying the stored spec."""
    conv = await asyncio.to_thread(
        deps.conversation_store.create_conversation,
        agent_id=task.agent_id,
        title=task.name,
        host_id=task.host_id,
        workspace=task.workspace,
    )
    if task.model_override is not None or task.reasoning_effort is not None:
        updated = await asyncio.to_thread(
            deps.conversation_store.update_conversation,
            conv.id,
            model_override=task.model_override,
            reasoning_effort=task.reasoning_effort,
        )
        if updated is not None:
            conv = updated
    return conv


async def _grant_owner(deps: FireDeps, task: ScheduledTask, conversation_id: str) -> None:
    """Write the LEVEL_OWNER grant so the run is visible to its owner.

    A NULL ``owner_user_id`` (single-user / OSS) resolves to
    :data:`RESERVED_USER_LOCAL`. The grant is NEVER skipped — without it the
    session has no owner and is invisible in every list path.
    """
    if deps.permission_store is None:
        return
    owner = task.owner_user_id or RESERVED_USER_LOCAL
    await asyncio.to_thread(deps.permission_store.ensure_user, owner)
    await asyncio.to_thread(deps.permission_store.grant, owner, conversation_id, LEVEL_OWNER)


async def _record_run(
    deps: FireDeps,
    task: ScheduledTask,
    conversation_id: str,
    scheduled_at: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Stamp last_run_* on the task and write a scheduled_task_runs row."""
    now = int(time.time())
    await asyncio.to_thread(
        deps.scheduled_task_store.update,
        task.id,
        last_run_at=now,
        last_run_conversation_id=conversation_id,
    )
    await asyncio.to_thread(
        deps.scheduled_task_store.create_run,
        _new_id(),
        task.id,
        status,
        scheduled_at,
        conversation_id=conversation_id,
        fired_at=now,
        error=error,
    )


def _new_id() -> str:
    """A bare 32-char hex UUID, matching the store's id convention."""
    return uuid.uuid4().hex


def _make_connected_host_dispatch(deps: FireDeps) -> LaunchDispatch:
    """Build the real connected-host launch+dispatch seam.

    Resolves the target host (the task's pinned ``host_id`` or the owner's
    freshest online host), launches a runner on it, waits for the runner to
    connect, and dispatches the task's prompt so the agent runs.
    """

    async def _dispatch(conv: Conversation, task: ScheduledTask) -> None:
        from omnigent.server.routes._host_launch import resolve_host_launch
        from omnigent.server.routes.sessions import (
            _dispatch_session_event_to_runner,
            _ensure_runner_session_initialized,
            _launch_runner_on_host,
            _wait_for_runner_client,
        )

        if deps.host_registry is None or deps.host_store is None:
            _logger.warning(
                "scheduled fire: no host registry/store configured — cannot launch "
                "runner for task %s (session %s)",
                task.id,
                conv.id,
            )
            return

        owner = task.owner_user_id or RESERVED_USER_LOCAL
        host_id = _resolve_host_id(deps, task, owner)
        if host_id is None:
            _logger.warning(
                "scheduled fire: no online host for task %s (owner %s) — session %s "
                "created but not launched",
                task.id,
                owner,
                conv.id,
            )
            return

        # Authorize + resolve the live host connection (owner check skipped when
        # auth is disabled, consistent with single-user behavior).
        target = await asyncio.to_thread(
            resolve_host_launch,
            user_id=task.owner_user_id,
            host_id=host_id,
            session_id=conv.id,
            host_store=deps.host_store,
            host_registry=deps.host_registry,
            conversation_store=deps.conversation_store,
            permission_store=deps.permission_store,
        )

        attempt = await _launch_runner_on_host(
            target.conv,
            deps.conversation_store,
            deps.host_registry,
            target.conn,
        )
        if attempt.error is not None:
            raise RuntimeError(f"host launch failed: {attempt.error}")

        runner_client = await _wait_for_runner_client(
            conv.id,
            deps.runner_router,
            deps.tunnel_registry,
            runner_id=attempt.runner_id,
            timeout_s=_RUNNER_CONNECT_TIMEOUT_S,
        )
        if runner_client is None:
            raise RuntimeError("runner did not connect before timeout")

        # Re-read the row: the launch wrote runner_id, and the session-init
        # handshake wants the current agent binding.
        fresh = await asyncio.to_thread(deps.conversation_store.get_conversation, conv.id)
        conv_for_dispatch = fresh or conv

        await _ensure_runner_session_initialized(
            conv.id, conv_for_dispatch, runner_client, deps.conversation_store
        )
        await _dispatch_session_event_to_runner(
            conv.id,
            conv_for_dispatch,
            _prompt_event(task.prompt),
            deps.conversation_store,
            runner_client,
            agent_name=None,
            file_store=deps.file_store,
            artifact_store=deps.artifact_store,
            created_by=owner,
            runner_router=deps.runner_router,
        )

    return _dispatch


def _resolve_host_id(deps: FireDeps, task: ScheduledTask, owner: str) -> str | None:
    """Pick the host to launch on: the pinned ``host_id`` or the owner's
    freshest online host. Returns ``None`` when none is online."""
    online = set(deps.host_registry.online_host_ids())
    if task.host_id is not None:
        return task.host_id if task.host_id in online else None
    # list_hosts is ordered updated_at desc (freshest first).
    for host in deps.host_store.list_hosts(owner):
        if host.host_id in online:
            return host.host_id
    return None
