"""The scheduled-task fire path — the real ``on_fire`` the scheduler invokes.

When :class:`~omnigent.server.scheduled.scheduler.ScheduledTaskScheduler` decides
a task is due it calls ``on_fire(workspace_id, scheduled_task_id)``. This module
supplies the real callback (the scheduler ships only a no-op placeholder). A
firing:

#. **Re-reads the row.** The armed timer is never trusted: the row is re-read by
   id, and a row that vanished (deleted between arming and firing) or is no
   longer ``active`` (paused/deleted) is a logged no-op.
#. **Resolves and validates the launch target.** A task that pinned no
   ``host_id`` resolves the owner's most-recently-active live host at fire time;
   a task that pinned no ``workspace`` (research / summaries / chat-only) starts
   the runner in the host's home directory. A pinned host that is missing or
   offline — and an owner with no live host at all — records a failed/skipped
   run instead of a running run.
#. **Creates a session** bound to the task's agent, carrying the resolved
   ``workspace`` / ``host_id`` and the stored ``model_override`` /
   ``reasoning_effort``.
#. **Grants ownership.** The spawned session gets a ``LEVEL_OWNER`` grant for the
   task's ``user_id`` — or :data:`RESERVED_USER_LOCAL` when it is NULL
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
scheduler, and the current retry policy is simply "the next occurrence fires
normally".

**Execution target.** Scheduled tasks currently support connected-host,
existing-workspace runs only. Future execution modes include managed sandbox,
branch selection, replay/backfill, completion tracking, and multi-replica
leasing through shared session-create orchestration rather than this direct
fire path.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation, ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_session_agent,
    validate_session_model_metadata,
)
from omnigent.server.schemas import SessionEventInput

_logger = logging.getLogger(__name__)

# How long to wait for a freshly launched runner to connect before giving up on
# dispatching the prompt this fire. The session + grant are already persisted, so
# a timeout leaves an owner-visible session the runner can still pick up later.
_RUNNER_CONNECT_TIMEOUT_S = 30.0

# The path stat'd on the resolved host to derive a fallback workspace for a task
# that pinned no workspace (research / summaries / chat-only). The runner still
# needs a real cwd and the DB check constraint
# ``ck_conversations_workspace_required_for_host`` requires a workspace once a
# host is bound. Only the host knows its own ``HOME``, so the server sends this
# tilde and stores the absolute ``canonical_path`` the host resolves it to (never
# the literal ``~`` — see ``_resolve_default_workspace``).
_DEFAULT_WORKSPACE = "~"

# Strong references to in-flight background fire tasks. ``loop.create_task`` holds
# only a weak reference, so without this a fire could be garbage-collected
# mid-flight; each task is discarded from the set when it completes.
_PENDING_FIRES: set[asyncio.Task[None]] = set()

# Fire path overlap guard keyed by tenant + task. The scheduler's job.running
# only covers its short on_fire callback; this covers the background
# create/grant/dispatch work that continues after on_fire returns.
_IN_FLIGHT_TASKS: set[tuple[int, str]] = set()


# ``launch_dispatch(conv, task)`` — launch the runner for a freshly created
# session and dispatch the task's prompt so the agent runs. Injectable so the
# orchestration can be unit-tested without a live host/runner.
LaunchDispatch = Callable[[Conversation, ScheduledTask], Awaitable[None]]
ConnectedHostPreflight = Callable[[ScheduledTask], Awaitable[None]]


class _CannotLaunchScheduledFire(RuntimeError):
    """A fire cannot start because the connected-host target is not usable."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class FireDeps:
    """The server dependencies the fire path needs, captured at wiring time.

    Mirrors how the scheduler captures its store: the ``on_fire`` factory grabs
    these off ``app.state`` once and closes over them, so a firing never needs a
    FastAPI request.
    """

    scheduled_task_store: Any
    agent_store: Any
    conversation_store: Any
    permission_store: Any | None
    host_store: Any | None
    host_registry: Any | None
    agent_cache: Any | None = None
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
) -> Callable[[int, str], Awaitable[None]]:
    """Build the real ``on_fire`` callback bound to server ``deps``.

    :param deps: Server stores/registries the fire path operates on.
    :param launch_dispatch: Seam that launches the runner and dispatches the
        prompt for a created session. Defaults to the real connected-host
        implementation; tests inject a fake.
    :returns: An ``async on_fire(workspace_id, scheduled_task_id)`` suitable for
        :class:`ScheduledTaskScheduler`.
    """
    preflight: ConnectedHostPreflight | None = None
    if launch_dispatch is None:
        dispatch = _make_connected_host_dispatch(deps)
        preflight = _make_connected_host_preflight(deps)
    else:
        dispatch = launch_dispatch

    async def on_fire(workspace_id: int, scheduled_task_id: str) -> None:
        # Re-read the row: never trust the armed timer. A deleted or
        # non-active row is a logged no-op done synchronously.
        with workspace_scope(workspace_id):
            task = await asyncio.to_thread(deps.scheduled_task_store.get, scheduled_task_id)
            if task is None:
                _logger.info(
                    "scheduled fire: task %s no longer exists — skipping", scheduled_task_id
                )
                return
            if task.state != "active":
                _logger.info(
                    "scheduled fire: task %s is %s (not active) — skipping",
                    scheduled_task_id,
                    task.state,
                )
                return

        key = (workspace_id, scheduled_task_id)
        if key in _IN_FLIGHT_TASKS:
            _logger.info("scheduled fire: task %s already in flight — skipping", scheduled_task_id)
            return
        _IN_FLIGHT_TASKS.add(key)

        # Fire-and-forget: the session create + launch runs in the background so
        # on_fire returns immediately and the scheduler re-arms the timer now.
        fire_task = asyncio.create_task(
            _run_fire(deps, workspace_id, scheduled_task_id, dispatch, preflight),
            name=f"scheduled-fire-{scheduled_task_id}",
        )
        _PENDING_FIRES.add(fire_task)
        fire_task.add_done_callback(_PENDING_FIRES.discard)
        fire_task.add_done_callback(lambda _task: _IN_FLIGHT_TASKS.discard(key))

    return on_fire


async def _run_fire(
    deps: FireDeps,
    workspace_id: int,
    scheduled_task_id: str,
    dispatch: LaunchDispatch,
    preflight: ConnectedHostPreflight | None,
) -> None:
    """Background body of a firing: create session, grant, launch, record run.

    Wrapped so any failure is logged rather than propagated — a failed fire must
    not crash the scheduler.
    """
    with workspace_scope(workspace_id):
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

        scheduled_at = int(time.time())
        try:
            await _run_fire_for_task(deps, task, dispatch, preflight, scheduled_at)
        except Exception:
            _logger.exception("scheduled fire: task %s failed", task.id)


async def _run_fire_for_task(
    deps: FireDeps,
    task: ScheduledTask,
    dispatch: LaunchDispatch,
    preflight: ConnectedHostPreflight | None,
    scheduled_at: int,
) -> None:
    """Run a freshly re-read active task inside its workspace scope."""
    try:
        if task.execution_target != "connected_host":
            _logger.info(
                "scheduled fire: task %s target %r is not supported — skipping",
                task.id,
                task.execution_target,
            )
            await asyncio.to_thread(
                _record_run_sync,
                deps,
                task,
                None,
                scheduled_at,
                "skipped",
                error=f"execution_target {task.execution_target!r} not supported yet",
                error_code="unsupported_target",
            )
            return

        # Resolve the effective launch target. An unset ``host_id`` means "you
        # didn't pin WHICH host", not "run hostless": resolve the owner's live
        # host at fire time. An unset ``workspace`` (research / summaries /
        # chat-only) defaults to the host's home directory so the runner still
        # has a real cwd. If no live host can be resolved, this records a
        # failed run — the same honest behavior as a pinned host that is offline.
        #
        # ``task`` stays the source of truth for the persisted row; ``effective``
        # carries the resolved host_id / defaulted workspace through preflight,
        # validation, create, and dispatch WITHOUT writing them back to the row
        # (the next fire re-resolves the live host).
        try:
            effective = await _resolve_effective_task(deps, task)
        except _CannotLaunchScheduledFire as exc:
            _logger.warning("scheduled fire: task %s cannot launch: %s", task.id, exc)
            await _record_run(
                deps,
                task,
                None,
                scheduled_at,
                status="failed",
                error=str(exc),
                error_code=exc.error_code,
            )
            return

        input_error = _validate_connected_host_inputs(effective)
        if input_error is not None:
            error, error_code = input_error
            _logger.warning("scheduled fire: task %s cannot run: %s", task.id, error)
            await _record_run(
                deps,
                task,
                None,
                scheduled_at,
                status="failed",
                error=error,
                error_code=error_code,
            )
            return

        if preflight is not None:
            try:
                await preflight(effective)
            except _CannotLaunchScheduledFire as exc:
                _logger.warning("scheduled fire: task %s cannot launch: %s", task.id, exc)
                await _record_run(
                    deps,
                    task,
                    None,
                    scheduled_at,
                    status="failed",
                    error=str(exc),
                    error_code=exc.error_code,
                )
                return

        # Validate the RESOLVED host/workspace. ``effective.workspace`` is always
        # an absolute realpath by this point — a caller-supplied path or the
        # canonicalized default (HOME). Gating on ``effective.workspace`` (not the
        # stored ``task.workspace``) means the agent's ``os_env.cwd`` boundary is
        # enforced even for a defaulted workspace, exactly as ``POST /v1/sessions``
        # does — an agent that pins an absolute cwd outside HOME records a failed
        # run instead of silently launching outside its declared boundary.
        validate_workspace = preflight is not None and effective.workspace is not None
        validation_error = await _validate_fire_session_inputs(
            deps, effective, validate_workspace=validate_workspace
        )
        if validation_error is not None:
            error, error_code = validation_error
            _logger.warning("scheduled fire: task %s failed validation: %s", task.id, error)
            await _record_run(
                deps,
                task,
                None,
                scheduled_at,
                status="failed",
                error=error,
                error_code=error_code,
            )
            return

        try:
            conv = await _create_session(deps, effective)
        except Exception:
            _logger.exception("scheduled fire: failed to create session for task %s", task.id)
            await _record_run(
                deps,
                task,
                None,
                scheduled_at,
                status="failed",
                error="session creation failed",
                error_code="session_create_failed",
            )
            return

        try:
            await _grant_owner(deps, task, conv.id)
        except Exception:
            _logger.exception(
                "scheduled fire: owner grant failed for task %s (session %s)",
                task.id,
                conv.id,
            )
            await _record_run(
                deps,
                task,
                conv.id,
                scheduled_at,
                status="failed",
                error="owner grant failed",
                error_code="owner_grant_failed",
            )
            return

        try:
            await dispatch(conv, effective)
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
                error_code="launch_failed",
            )
            return

        await _record_run(deps, task, conv.id, scheduled_at, status="running")
        _logger.info("scheduled fire: task %s fired session %s", task.id, conv.id)
    except Exception:
        _logger.exception("scheduled fire: task %s failed", task.id)


async def _resolve_effective_task(deps: FireDeps, task: ScheduledTask) -> ScheduledTask:
    """Resolve the host/workspace the fire actually launches against.

    A task may omit ``host_id`` (run on the owner's live host, whichever it is)
    and/or ``workspace`` (a task that does no code work — e.g. an MCP-only task).
    This returns a copy of *task* with those holes filled for this one fire:

    * ``host_id`` unset → the owner's most-recently-active ONLINE host. No live
      host (or no host store/registry) raises :class:`_CannotLaunchScheduledFire`
      so the caller records a failed run instead of silently no-oping.
    * ``workspace`` unset → the launch host's home directory, canonicalized to an
      absolute realpath via a ``host.stat`` round-trip, so the runner launches
      with a real cwd and the stored row never holds a literal ``~``. This HOME
      default applies whether the host was pinned or resolved above.

    A pinned ``host_id`` is left untouched — not re-resolved — and its liveness is
    enforced by the existing preflight, not here. The resolved values are never
    written back to the stored row; the next fire re-resolves the live host.
    """
    host_id = task.host_id
    if host_id is None:
        host_id = await _resolve_owner_host(deps, task)
    workspace = task.workspace
    if workspace is None:
        # Authorize a PINNED host's ownership BEFORE the home-dir stat below.
        # ``_resolve_default_workspace`` issues a ``host.stat`` RPC to the host,
        # and the ownership check otherwise lives in the preflight, which runs
        # AFTER resolution — so a task pinning another owner's host would dispatch
        # a stat to a host it doesn't own before being rejected. A host resolved
        # above (``task.host_id`` was None) is by construction the owner's own, so
        # only the pinned case needs this pre-RPC check.
        if task.host_id is not None:
            await _authorize_pinned_host(deps, task, host_id)
        # Canonicalize the host's home dir to an ABSOLUTE realpath rather than
        # persisting the literal ``~``. ``conv.workspace`` is contracted to be an
        # already-resolved absolute path (many consumers do plain ``Path`` math /
        # ``startswith('/')`` on it without expanding ``~``), so a stat round-trip
        # here mirrors how the normal session-create path stores canonical_path.
        workspace = await _resolve_default_workspace(deps, host_id)
    if host_id is task.host_id and workspace is task.workspace:
        return task
    return replace(task, host_id=host_id, workspace=workspace)


async def _resolve_owner_host(deps: FireDeps, task: ScheduledTask) -> str:
    """Pick the owner's most-recently-active online host for an unpinned task.

    ``list_hosts`` returns the owner's hosts most-recently-active first and
    includes offline ones, so the first that is live in the registry is the
    natural default. First-online is the v1 tiebreak.
    """
    if deps.host_store is None or deps.host_registry is None:
        raise _CannotLaunchScheduledFire(
            "connected host registry/store is not configured",
            error_code="host_registry_unavailable",
        )
    owner = task.user_id or RESERVED_USER_LOCAL
    hosts = await asyncio.to_thread(deps.host_store.list_hosts, owner)
    for host in hosts:
        if deps.host_registry.get(host.host_id) is not None:
            return host.host_id
    raise _CannotLaunchScheduledFire(
        "no online host is available for the scheduled task owner",
        error_code="no_online_host",
    )


async def _resolve_default_workspace(deps: FireDeps, host_id: str) -> str:
    """Canonicalize the host's home directory to an absolute realpath.

    Sends a ``host.stat`` for :data:`_DEFAULT_WORKSPACE` (``~``) to the resolved
    host — the host expands the tilde against its own ``HOME`` and returns the
    absolute ``canonical_path``, the same value the normal session-create path
    stores. Raises :class:`_CannotLaunchScheduledFire` if the host is gone or
    can't resolve its home dir, so the caller records an honest failed run.
    """
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        _ask_host_stat,
    )

    if deps.host_registry is None:
        raise _CannotLaunchScheduledFire(
            "connected host registry is not configured",
            error_code="host_registry_unavailable",
        )
    host_conn = deps.host_registry.get(host_id)
    if host_conn is None:
        raise _CannotLaunchScheduledFire(
            f"connected host {host_id!r} is not online on this server",
            error_code="host_offline",
        )
    try:
        stat = await _ask_host_stat(
            host_registry=deps.host_registry,
            host_conn=host_conn,
            path=_DEFAULT_WORKSPACE,
        )
    except WorkspaceValidationError as exc:
        raise _CannotLaunchScheduledFire(
            f"could not resolve a default workspace on host {host_id!r}: {exc}",
            error_code="default_workspace_unresolved",
        ) from exc
    canonical = stat.get("canonical_path")
    if not stat.get("exists") or not isinstance(canonical, str):
        raise _CannotLaunchScheduledFire(
            f"host {host_id!r} did not resolve a home directory for the default workspace",
            error_code="default_workspace_unresolved",
        )
    return canonical


async def _create_session(deps: FireDeps, task: ScheduledTask) -> Conversation:
    """Create a conversation bound to the task's agent, carrying the stored spec."""
    # Connected-host, existing-workspace runs create the conversation directly.
    # Future execution modes such as managed sandbox, branch selection, and
    # replay/backfill must use shared session-create orchestration.
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

    A NULL ``user_id`` (single-user / OSS) resolves to
    :data:`RESERVED_USER_LOCAL`. When ``permission_store`` is ``None`` (no auth
    configured) this is a no-op — the session is still accessible because auth
    is disabled system-wide.
    """
    if deps.permission_store is None:
        return
    owner = task.user_id or RESERVED_USER_LOCAL
    await asyncio.to_thread(deps.permission_store.ensure_user, owner)
    await asyncio.to_thread(deps.permission_store.grant, owner, conversation_id, LEVEL_OWNER)


async def _record_run(
    deps: FireDeps,
    task: ScheduledTask,
    conversation_id: str | None,
    scheduled_at: int,
    *,
    status: str,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    """Stamp last_run_* on the task and write a scheduled_task_runs row."""
    await asyncio.to_thread(
        _record_run_sync,
        deps,
        task,
        conversation_id,
        scheduled_at,
        status,
        error=error,
        error_code=error_code,
    )


def _record_run_sync(
    deps: FireDeps,
    task: ScheduledTask,
    conversation_id: str | None,
    scheduled_at: int,
    status: str,
    *,
    error: str | None = None,
    error_code: str | None = None,
) -> None:
    """Synchronous run recording body for ``asyncio.to_thread`` callers."""
    now = int(time.time())
    update_fields: dict[str, Any] = {"last_run_at": now}
    if conversation_id is not None:
        update_fields["last_run_conversation_id"] = conversation_id
    deps.scheduled_task_store.update(task.id, **update_fields)
    deps.scheduled_task_store.create_run(
        _new_id(),
        task.id,
        status,
        scheduled_at,
        conversation_id=conversation_id,
        fired_at=now,
        error=error,
        error_code=error_code,
    )


async def _validate_fire_session_inputs(
    deps: FireDeps,
    task: ScheduledTask,
    *,
    validate_workspace: bool,
) -> tuple[str, str] | None:
    """Validate stored task fields before creating a conversation."""
    try:
        owner = task.user_id
        agent = await validate_session_agent(
            user_id=owner,
            agent_id=task.agent_id,
            agent_store=deps.agent_store,
            permission_store=deps.permission_store,
            conversation_store=deps.conversation_store,
        )
        validate_session_model_metadata(
            model_override=task.model_override,
            reasoning_effort=task.reasoning_effort,
        )
        if validate_workspace:
            if task.host_id is None or task.workspace is None:
                return (
                    "scheduled tasks connected-host execution requires host_id and workspace",
                    "missing_execution_input",
                )
            await validate_existing_host_workspace(
                user_id=owner,
                host_id=task.host_id,
                workspace=task.workspace,
                agent=agent,
                agent_cache=deps.agent_cache,
                host_store=deps.host_store,
                host_registry=deps.host_registry,
            )
    except OmnigentError as exc:
        return exc.message, exc.code
    except Exception:
        _logger.exception("scheduled fire: unexpected validation failure for task %s", task.id)
        return "scheduled task validation failed", ErrorCode.INTERNAL_ERROR
    return None


def _validate_connected_host_inputs(task: ScheduledTask) -> tuple[str, str] | None:
    """Return a failure reason/code when a task lacks connected-host inputs."""
    if not isinstance(task.host_id, str) or not task.host_id.strip():
        return "scheduled tasks connected-host execution requires host_id", "missing_host_id"
    if not isinstance(task.workspace, str) or not task.workspace.strip():
        return (
            "scheduled tasks connected-host execution requires an existing workspace",
            "missing_workspace",
        )
    return None


async def _authorize_pinned_host(deps: FireDeps, task: ScheduledTask, host_id: str) -> None:
    """Verify a host belongs to the task owner (local store read, no host RPC).

    Shared by the preflight and by :func:`_resolve_effective_task`'s pre-stat
    check so a task pinning another owner's host is rejected before any RPC
    reaches that host. ``get_host`` is a local DB lookup — it never contacts the
    host. When ``user_id`` is ``None`` (single-user / auth disabled) the owner
    check is skipped, matching the preflight and the rest of the server.
    """
    if deps.host_store is None:
        raise _CannotLaunchScheduledFire(
            "connected host registry/store is not configured",
            error_code="host_registry_unavailable",
        )
    host = await asyncio.to_thread(deps.host_store.get_host, host_id)
    if host is None:
        raise _CannotLaunchScheduledFire(
            f"connected host {host_id!r} was not found",
            error_code="host_not_found",
        )
    if task.user_id is not None and host.user_id != task.user_id:
        raise _CannotLaunchScheduledFire(
            f"connected host {host_id!r} is not owned by the scheduled task owner",
            error_code="host_not_owned",
        )


def _make_connected_host_preflight(deps: FireDeps) -> ConnectedHostPreflight:
    """Build a preflight check for the connected-host execution target."""

    async def _preflight(task: ScheduledTask) -> None:
        if deps.host_registry is None or deps.host_store is None:
            raise _CannotLaunchScheduledFire(
                "connected host registry/store is not configured",
                error_code="host_registry_unavailable",
            )

        host_id = task.host_id
        assert host_id is not None  # guarded by _validate_connected_host_inputs
        # Existence + ownership (local store read; no RPC to the host).
        await _authorize_pinned_host(deps, task, host_id)
        if deps.host_registry.get(host_id) is None:
            raise _CannotLaunchScheduledFire(
                f"connected host {host_id!r} is not online on this server",
                error_code="host_offline",
            )

    return _preflight


def _new_id() -> str:
    """A bare 32-char hex UUID, matching the store's id convention."""
    return uuid.uuid4().hex


def _make_connected_host_dispatch(deps: FireDeps) -> LaunchDispatch:
    """Build the real connected-host launch+dispatch seam.

    Uses the task's pinned ``host_id``, launches a runner on it, waits for the
    runner to connect, and dispatches the task's prompt so the agent runs.
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
            raise RuntimeError("connected host registry/store is not configured")

        owner = task.user_id or RESERVED_USER_LOCAL
        host_id = task.host_id
        if host_id is None or deps.host_registry.get(host_id) is None:
            raise RuntimeError(f"connected host {host_id!r} is not online")

        # Authorize + resolve the live host connection (owner check skipped when
        # auth is disabled, consistent with single-user behavior).
        target = await asyncio.to_thread(
            resolve_host_launch,
            user_id=owner,
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
