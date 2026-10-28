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

A task with ``session_mode = "continue"`` skips host resolution and session
creation once it has a ``session_id``: the prompt goes into that session
instead, waking its runner the way a user message would. Its first firing
creates the session as usual and records it as the task's ``session_id``.

**Fire-and-forget.** The re-read + state guard run synchronously so an obviously
dead fire costs nothing, but the session creation / launch is dispatched onto a
background :func:`asyncio.create_task` and ``on_fire`` returns immediately. If it
blocked on full session startup the scheduler could not re-arm the task's timer
for the fire's duration. A strong reference to each in-flight task is held until
it completes (``loop.create_task`` only keeps a weak one). Any failure in the
background work is caught and logged: a failed fire must never crash the
scheduler, and the current retry policy is simply "the next occurrence fires
normally".

**Execution target.** Scheduled tasks run on a ``connected_host`` (pin/resolve
the owner's own machine) or in a ``managed_sandbox`` — a FRESH server-provisioned
sandbox minted per fire, using the normal runner-idle and provider keepalive
lifecycle. A managed fire saves its prompt in the session before provisioning
starts; the host's replica delivers it once the sandbox connects, so a restart
mid-provision doesn't lose it, and the fire doesn't hold its overlap guard while
the sandbox starts. Future execution modes include branch selection and
replay/backfill.

**Persistent dispatch.** With ``automations_v2``, the database scheduler claims
each occurrence and :class:`ClaimedRunExecutor` runs the same steps on that
claimed run instead of inserting a new one. It records the session on the run
before launching, and ends startup once the prompt is sent (connected host) or
saved with the sandbox's host bound to the session (managed sandbox). Its
recovery callback decides interrupted startups from the session's state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.entities import Conversation, ScheduledTask, ScheduledTaskRun
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.mcp_selection import mcp_selection_revision
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.server.bundles import agent_for_user
from omnigent.server.host_registry import host_owner_scope
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_session_agent,
    validate_session_model_metadata,
    validate_session_permission_mode,
)
from omnigent.server.routes._sessions.helpers import _announce_session_added
from omnigent.server.scheduled.dispatch_scheduler import (
    FireIdentity,
    FireIdentityRevoked,
    StartupLease,
)
from omnigent.server.scheduled.schedule import continued_sandbox_host_id
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store import SCHEDULED_TASK_ORIGIN_LABEL_KEY

if TYPE_CHECKING:
    from omnigent.server.managed_hosts import ManagedLaunch, ManagedLaunchTracker

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
# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_PENDING_FIRES: set[asyncio.Task[None]] = set()

# Fire path overlap guard keyed by tenant + task. The scheduler's job.running
# only covers its short on_fire callback; this covers the background
# create/grant/dispatch work that continues after on_fire returns. A managed
# fire releases it once its prompt is saved and its launch has started.
# custom-lint: disable-next=workspace-scoped-cache -- already keyed by (workspace_id, task_id)
_IN_FLIGHT_TASKS: set[tuple[int, str]] = set()


# ``launch_dispatch(conv, task)`` — launch the runner for a freshly created
# session and dispatch the task's prompt so the agent runs. Injectable so the
# orchestration can be unit-tested without a live host/runner.
LaunchDispatch = Callable[[Conversation, ScheduledTask], Awaitable[None]]
ConnectedHostPreflight = Callable[[ScheduledTask], Awaitable[None]]


@dataclass(frozen=True)
class _LaunchErrorCodes:
    """Error codes for a managed launch that fails before or after its host is bound."""

    before_binding: str
    after_binding: str


# Legacy timers keep one code for every managed launch failure.
_LEGACY_LAUNCH_ERRORS = _LaunchErrorCodes("launch_failed", "launch_failed")
# Claimed runs record the failure stage: creating the sandbox, or starting its runner.
_STAGED_LAUNCH_ERRORS = _LaunchErrorCodes("sandbox_create_failed", "runner_start_failed")

# How long a claimed managed fire waits for its sandbox to register and bind.
SANDBOX_REGISTRATION_TIMEOUT_S = 12 * 60
# How long an abandoned launch gets to stop before its sandbox is torn down.
_ABANDONED_LAUNCH_SETTLE_S = 30.0

# ``launch_start(conv, task, host_id, error_codes)`` — start provisioning a sandbox
# in the background once the managed fire's prompt is saved. ``host_id`` is the
# host identity the sandbox registers under, or ``None`` to mint one.
ManagedLaunchStart = Callable[
    [Conversation, ScheduledTask, str | None, _LaunchErrorCodes], asyncio.Task[None]
]


class _FireResult(Enum):
    STARTED = "started"
    INACTIVE = "inactive"
    IN_FLIGHT = "in_flight"


class _CannotLaunchScheduledFire(RuntimeError):
    """A fire cannot start because its execution target is not usable."""

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
    policy_store: Any | None = None
    agent_cache: Any | None = None
    runner_router: Any | None = None
    tunnel_registry: Any | None = None
    file_store: Any | None = None
    artifact_store: Any | None = None
    # Managed-sandbox execution target. Both are needed to provision a fresh
    # sandbox per fire; ``None`` when the server has no ``sandbox:`` config, in
    # which case a ``managed_sandbox`` task records a failed run.
    sandbox_config: Any | None = None
    managed_launches: Any | None = None
    # Saves a managed fire's prompt until its sandbox connects. Present whenever
    # managed launches are supported.
    startup_inputs: Any | None = None
    # ``app.state``, read per fire for the default-public-sessions policy.
    # ``None`` (tests, embedders) leaves every fired session private.
    app_state: Any | None = None
    # Confirms a task's project still exists before filing the session into it.
    project_store: Any | None = None
    # Who owns a single-user task's projects and open tabs: "local" behind an
    # auth provider, ``None`` on a server without one.
    single_user_owner: str | None = RESERVED_USER_LOCAL


@dataclass
class _FireDispatch:
    """The launch seams a fire may use, selected per task by ``execution_target``.

    ``connected`` / ``connected_preflight`` drive the connected-host flow;
    ``managed`` saves the prompt and ``managed_start`` provisions a fresh
    sandbox after the run is recorded; ``reuse`` wakes the runner of an
    existing conversation. A test that injects a single ``launch_dispatch``
    gets it as ``connected``, ``managed`` and ``reuse`` (with no preflight or
    start), so the injected fake serves whichever path the task takes.
    """

    connected: LaunchDispatch
    connected_preflight: ConnectedHostPreflight | None
    managed: LaunchDispatch
    managed_start: ManagedLaunchStart | None = None
    # ``None`` uses the real seam that wakes the session's runner.
    reuse: LaunchDispatch | None = None


class _FireRecorder(Protocol):
    """How one fire records its progress and outcome.

    Legacy timers insert a new run row when the fire ends. Persistent dispatch
    records progress on the run it already claimed, under a startup lease.
    """

    async def check(self) -> None:
        """Raise before an external action if the fire may no longer proceed."""
        ...

    async def session_created(self, conversation_id: str) -> None:
        """Record the fired session before it is launched, so recovery can find it."""
        ...

    async def finish(
        self,
        status: str,
        *,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record the fire's startup outcome."""
        ...

    async def hand_off_managed(
        self, conv: Conversation, task: ScheduledTask, start: ManagedLaunchStart | None
    ) -> None:
        """Start the sandbox for a managed fire whose prompt is saved, and record the result."""
        ...


class _NewRunRecorder:
    """Legacy timers: insert one run row once the fire's outcome is known."""

    def __init__(self, deps: FireDeps, task: ScheduledTask, scheduled_at: int) -> None:
        self._deps = deps
        self._task = task
        self._scheduled_at = scheduled_at

    async def check(self) -> None:
        return None

    async def session_created(self, conversation_id: str) -> None:  # noqa: ARG002
        return None

    async def finish(
        self,
        status: str,
        *,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await _record_run(
            self._deps,
            self._task,
            conversation_id,
            self._scheduled_at,
            status=status,
            error=error,
            error_code=error_code,
        )

    async def hand_off_managed(
        self, conv: Conversation, task: ScheduledTask, start: ManagedLaunchStart | None
    ) -> None:
        await self.finish("running", conversation_id=conv.id)
        if start is None:
            return
        # Started only after the run exists, so a fast launch failure has a
        # running run to fail. A continue task keeps one sandbox host.
        host_id = (
            continued_sandbox_host_id(task.id, task.workspace_id)
            if task.session_mode == "continue"
            else None
        )
        try:
            start(conv, task, host_id, _LEGACY_LAUNCH_ERRORS)
        except Exception as exc:
            _logger.exception(
                "scheduled fire: managed launch did not start for task %s (session %s)",
                task.id,
                conv.id,
            )
            reason = f"managed sandbox failed to launch: {exc}"
            await _fail_running_run(self._deps, conv.id, reason, "launch_failed")
            if self._deps.startup_inputs is not None:
                await self._deps.startup_inputs.record_launch_failure(conv.id, reason)


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
) -> Callable[[int, str], Awaitable[bool]]:
    """Build the real ``on_fire`` callback bound to server ``deps``.

    :param deps: Server stores/registries the fire path operates on.
    :param launch_dispatch: Seam that launches the runner and dispatches the
        prompt for a created session. Defaults to the real connected-host
        implementation; tests inject a fake.
    :returns: An ``async on_fire(workspace_id, scheduled_task_id)`` suitable for
        :class:`ScheduledTaskScheduler`. False tells it to remove an inactive
        task's timer; an overlapping fire keeps its next occurrence armed.
    """
    fire_dispatch = _build_fire_dispatch(deps, launch_dispatch)

    async def on_fire(workspace_id: int, scheduled_task_id: str) -> bool:
        result = await _trigger_fire(
            deps,
            workspace_id,
            scheduled_task_id,
            fire_dispatch,
            require_active=True,
        )
        return result is not _FireResult.INACTIVE

    return on_fire


def build_run_now(
    deps: FireDeps,
    *,
    launch_dispatch: LaunchDispatch | None = None,
) -> Callable[[int, str], Awaitable[bool]]:
    """Build the manual "run now" trigger — an immediate fire of a task.

    Legacy timers only: reuses :func:`_run_fire_for_task` and its process-local
    ``_IN_FLIGHT_TASKS`` overlap guard. With ``automations_v2``, Run now is a
    claimed run started through the database scheduler instead.
    Unlike :func:`build_on_fire`, a paused task is runnable here
    (``require_active=False``). A deleted or missing task is still a no-op.

    Fire-and-forget like the scheduler path: the session create + launch runs in
    the background and the returned callback resolves as soon as the fire is
    accepted. The caller (the ``POST /run`` route) therefore returns ``202
    Accepted`` rather than the finished run.

    :returns: ``async run_now(workspace_id, scheduled_task_id) -> bool`` that
        returns ``True`` if a fire was started, ``False`` if it was skipped
        (row gone, or a fire for that task is already in flight).
    """
    fire_dispatch = _build_fire_dispatch(deps, launch_dispatch)

    async def run_now(workspace_id: int, scheduled_task_id: str) -> bool:
        result = await _trigger_fire(
            deps,
            workspace_id,
            scheduled_task_id,
            fire_dispatch,
            require_active=False,
        )
        return result is _FireResult.STARTED

    return run_now


def _build_fire_dispatch(deps: FireDeps, launch_dispatch: LaunchDispatch | None) -> _FireDispatch:
    """Build the per-target launch seams shared by the scheduler and run-now.

    A test-injected ``launch_dispatch`` is used for BOTH targets (no preflight),
    so a fake fire can exercise either the connected-host or managed-sandbox
    branch. The real path builds the connected-host dispatch/preflight plus the
    managed-sandbox dispatch.
    """
    if launch_dispatch is not None:
        return _FireDispatch(
            connected=launch_dispatch,
            connected_preflight=None,
            managed=launch_dispatch,
            reuse=launch_dispatch,
        )
    return _FireDispatch(
        connected=_make_connected_host_dispatch(deps),
        connected_preflight=_make_connected_host_preflight(deps),
        managed=_make_managed_sandbox_dispatch(deps),
        reuse=_make_reuse_dispatch(deps),
        managed_start=_make_managed_sandbox_start(deps),
    )


async def _trigger_fire(
    deps: FireDeps,
    workspace_id: int,
    scheduled_task_id: str,
    fire_dispatch: _FireDispatch,
    *,
    require_active: bool,
) -> _FireResult:
    """Synchronously guard a fire, then dispatch the run in the background.

    Shared by the scheduled fire path (``require_active=True``) and the manual
    run-now trigger (``require_active=False``). The re-read + guard run
    synchronously so an obviously dead fire costs nothing; the create/launch is
    fire-and-forget so the caller (scheduler timer or ``POST /run`` route)
    returns immediately.

    :returns: Whether work started, the task is inactive, or a fire is already
        in flight. Only an inactive task should lose its recurring timer.
    """
    # Re-read the row: never trust the caller. A deleted (or, for the scheduled
    # path, non-active) row is a logged no-op done synchronously.
    with workspace_scope(workspace_id):
        task = await asyncio.to_thread(deps.scheduled_task_store.get, scheduled_task_id)
        if task is None:
            _logger.info("scheduled fire: task %s no longer exists — skipping", scheduled_task_id)
            return _FireResult.INACTIVE
        if task.state == "deleted" or (require_active and task.state != "active"):
            _logger.info(
                "scheduled fire: task %s is %s (not active) — skipping",
                scheduled_task_id,
                task.state,
            )
            return _FireResult.INACTIVE

    key = (workspace_id, scheduled_task_id)
    if key in _IN_FLIGHT_TASKS:
        _logger.info("scheduled fire: task %s already in flight — skipping", scheduled_task_id)
        return _FireResult.IN_FLIGHT
    _IN_FLIGHT_TASKS.add(key)

    # Fire-and-forget: the session create + launch runs in the background so the
    # caller returns immediately (the scheduler re-arms the timer now; the route
    # returns 202).
    fire_task = asyncio.create_task(
        _run_fire(deps, workspace_id, scheduled_task_id, fire_dispatch, require_active),
        name=f"scheduled-fire-{scheduled_task_id}",
    )
    _PENDING_FIRES.add(fire_task)
    fire_task.add_done_callback(_PENDING_FIRES.discard)
    fire_task.add_done_callback(lambda _task: _IN_FLIGHT_TASKS.discard(key))
    return _FireResult.STARTED


async def _run_fire(
    deps: FireDeps,
    workspace_id: int,
    scheduled_task_id: str,
    fire_dispatch: _FireDispatch,
    require_active: bool = True,
) -> None:
    """Background body of a firing: create session, grant, launch, record run.

    Wrapped so any failure is logged rather than propagated — a failed fire must
    not crash the scheduler. ``require_active`` mirrors the synchronous guard:
    the scheduled path requires an active row, run-now allows a paused row.
    """
    with workspace_scope(workspace_id):
        task = await asyncio.to_thread(deps.scheduled_task_store.get, scheduled_task_id)
        if task is None:
            _logger.info("scheduled fire: task %s no longer exists — skipping", scheduled_task_id)
            return
        if task.state == "deleted" or (require_active and task.state != "active"):
            _logger.info(
                "scheduled fire: task %s is %s (not active) — skipping",
                scheduled_task_id,
                task.state,
            )
            return

        recorder = _NewRunRecorder(deps, task, int(time.time()))
        try:
            with (
                account_authority_scope(task.user_id, task.account_generation),
                host_owner_scope(task.user_id, task.account_generation),
            ):
                await _run_fire_for_task(deps, task, fire_dispatch, recorder)
        except Exception:
            _logger.exception("scheduled fire: task %s failed", task.id)


async def _run_fire_for_task(
    deps: FireDeps,
    task: ScheduledTask,
    fire_dispatch: _FireDispatch,
    recorder: _FireRecorder,
) -> None:
    """Run a task, routed by ``execution_target``, recording progress on *recorder*.

    ``managed_sandbox`` provisions a fresh sandbox per fire (never reusing an
    existing one); ``connected_host`` runs the resolve/validate/launch flow
    below. Any other target records a skipped run. Unexpected errors propagate:
    the legacy caller logs them, and persistent dispatch leaves the claimed run
    for recovery.
    """
    # A deleted / rotated owner registration fires nothing — managed or
    # connected — and disables the task.
    if await _owner_is_deleted(deps, task):
        await _disable_for_deleted_owner(deps, task, recorder)
        return
    managed = task.execution_target == "managed_sandbox"
    if not managed and task.execution_target != "connected_host":
        _logger.info(
            "scheduled fire: task %s target %r is not supported — skipping",
            task.id,
            task.execution_target,
        )
        await recorder.finish(
            "skipped",
            error=f"execution_target {task.execution_target!r} not supported yet",
            error_code="unsupported_target",
        )
        return

    if task.continues_session:
        await _continue_session(
            deps, task, fire_dispatch.reuse or _make_reuse_dispatch(deps), recorder
        )
        return

    dispatch = fire_dispatch.managed if managed else fire_dispatch.connected
    preflight = None if managed else fire_dispatch.connected_preflight

    # Resolve this fire's host/workspace without changing the stored task.
    # A managed launch starts hostless and binds a fresh sandbox afterward.
    try:
        effective = await _resolve_effective_task(deps, task)
    except _CannotLaunchScheduledFire as exc:
        _logger.warning("scheduled fire: task %s cannot launch: %s", task.id, exc)
        await recorder.finish("failed", error=str(exc), error_code=exc.error_code)
        return

    input_error = None if managed else _validate_connected_host_inputs(effective)
    if input_error is not None:
        error, error_code = input_error
        _logger.warning("scheduled fire: task %s cannot run: %s", task.id, error)
        await recorder.finish("failed", error=error, error_code=error_code)
        return

    if preflight is not None:
        try:
            await preflight(effective)
        except _CannotLaunchScheduledFire as exc:
            _logger.warning("scheduled fire: task %s cannot launch: %s", task.id, exc)
            await recorder.finish("failed", error=str(exc), error_code=exc.error_code)
            return

    # Check connected-host workspaces, including the resolved HOME default,
    # against the agent's cwd boundary. A new sandbox has no workspace yet.
    validate_workspace = preflight is not None and effective.workspace is not None
    validation_error = await _validate_fire_session_inputs(
        deps, effective, validate_workspace=validate_workspace
    )
    if validation_error is not None:
        error, error_code = validation_error
        _logger.warning("scheduled fire: task %s failed validation: %s", task.id, error)
        await recorder.finish("failed", error=error, error_code=error_code)
        return

    hand_off = managed
    if managed and task.session_mode == "continue":
        joined = await _continued_host_binding(deps, task)
        if joined is not None:
            # The task keeps one sandbox host. A new session joins it in an
            # earlier session's workspace, waking or relaunching it the way a
            # user message would, instead of provisioning a second sandbox.
            effective = replace(effective, host_id=joined[0], workspace=joined[1])
            dispatch = fire_dispatch.reuse or _make_reuse_dispatch(deps)
            hand_off = False

    await recorder.check()
    try:
        effective = await _own_task_agent(deps, effective)
        conv = await _create_session(deps, effective)
    except Exception:
        _logger.exception("scheduled fire: failed to create session for task %s", task.id)
        await recorder.finish(
            "failed", error="session creation failed", error_code="session_create_failed"
        )
        return
    try:
        await _grant_owner(deps, effective, conv.id)
    except Exception:
        _logger.exception(
            "scheduled fire: owner grant failed for task %s (session %s)",
            task.id,
            conv.id,
        )
        await recorder.finish(
            "failed",
            conversation_id=conv.id,
            error="owner grant failed",
            error_code="owner_grant_failed",
        )
        return
    # Recorded only once its owner can use it, so another run that finds it can continue it.
    # Compared as stored: ``effective`` carries this fire's resolved host.
    stored = replace(task, agent_id=effective.agent_id, updated_at=effective.updated_at)
    try:
        # A connected-host session pins its host, so every later run routes there.
        await _keep_first_session(
            deps, stored, conv.id, host_id=None if managed else effective.host_id
        )
    except _SessionTaken:
        await _discard_session(deps, conv.id)
        await recorder.finish(
            "skipped",
            error="another run already started the session this automation continues",
            error_code="continued_session_busy",
        )
        return
    except _SessionNotRecorded as exc:
        # Running anyway would put this run's work in a session later runs never see.
        if exc.certain:
            await _discard_session(deps, conv.id)
        await recorder.finish(
            "failed",
            error="could not record the session this automation continues",
            error_code="session_record_failed",
        )
        return
    await recorder.session_created(conv.id)

    await _attach_cost_budget(deps, task, conv.id)
    _announce_session_added(
        effective.user_id if effective.user_id is not None else deps.single_user_owner, conv.id
    )

    # Default-public access is optional decoration on top of the owner
    # grant, so a failure here must not cancel an otherwise-ready run — the
    # session simply stays private.
    try:
        await _grant_default_public(deps, effective, conv.id)
    except Exception:
        _logger.exception(
            "scheduled fire: default-public grant failed for task %s (session %s); "
            "continuing with a private session",
            task.id,
            conv.id,
        )

    await recorder.check()
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
        await recorder.finish(
            "failed",
            conversation_id=conv.id,
            error="runner launch/dispatch failed",
            error_code="launch_failed",
        )
        return

    if hand_off:
        await recorder.hand_off_managed(conv, effective, fire_dispatch.managed_start)
    else:
        await recorder.finish("running", conversation_id=conv.id)
    _logger.info("scheduled fire: task %s fired session %s", task.id, conv.id)


async def _disable_for_deleted_owner(
    deps: FireDeps, task: ScheduledTask, recorder: _FireRecorder
) -> None:
    _logger.warning(
        "scheduled fire: task %s owner %r registration is inactive; disabling task",
        task.id,
        task.user_id,
    )
    await asyncio.to_thread(deps.scheduled_task_store.update, task.id, state="deleted")
    await recorder.finish(
        "failed",
        error=f"owner {task.user_id!r} registration is no longer active; task disabled",
        error_code="owner_deleted",
    )


async def _continue_session(
    deps: FireDeps,
    task: ScheduledTask,
    dispatch: LaunchDispatch,
    recorder: _FireRecorder,
) -> None:
    """Send the task's prompt into the session it continues.

    The prompt goes in the way a user's message would, also while the session is
    busy with an earlier turn, and the run succeeds once it is delivered. Never
    falls back to a new session: one that cannot take the prompt records a
    failed run telling the owner how to start a new one.
    """
    session_id = task.session_id
    assert session_id is not None  # guarded by the caller
    conv = await asyncio.to_thread(deps.conversation_store.get_conversation, session_id)
    problem = await continued_session_problem(
        task, conv, host_store=deps.host_store, permission_store=deps.permission_store
    )
    if problem is not None:
        reason, error_code = problem
        _logger.warning("scheduled fire: task %s cannot continue its session: %s", task.id, reason)
        await recorder.finish(
            "failed", error=f"{reason}; {_NEW_SESSION_HINT}", error_code=error_code
        )
        return
    assert conv is not None

    await recorder.check()
    spent_usd = await asyncio.to_thread(_session_cost_usd, deps, session_id)
    try:
        await _attach_cost_budget(deps, task, session_id, spent_usd=spent_usd, replace=True)
    except _BudgetNotSet:
        _logger.exception(
            "scheduled fire: task %s could not set the cost cap on session %s",
            task.id,
            session_id,
        )
        await recorder.finish(
            "failed",
            conversation_id=session_id,
            error="could not set the automation's cost cap on the session; any cap it "
            "already had stays in place",
            error_code="continued_session_budget_failed",
        )
        return
    except _BudgetConflict:
        _logger.warning(
            "scheduled fire: task %s session %s already has a cost cap it did not set",
            task.id,
            session_id,
        )
        await recorder.finish(
            "failed",
            conversation_id=session_id,
            error="the session already has a cost cap this automation did not set; remove it "
            f"from the session's policies, or {_NEW_SESSION_HINT}",
            error_code="continued_session_budget_conflict",
        )
        return
    try:
        await dispatch(conv, task)
    except Exception:
        _logger.exception(
            "scheduled fire: dispatch into continued session %s failed for task %s",
            session_id,
            task.id,
        )
        await recorder.finish(
            "failed",
            conversation_id=session_id,
            error="runner launch/dispatch failed",
            error_code="launch_failed",
        )
        return
    await recorder.finish("succeeded", conversation_id=session_id)
    _logger.info("scheduled fire: task %s continued session %s", task.id, session_id)


_NEW_SESSION_HINT = "edit the automation to start a new session on its next run"


async def continued_session_problem(
    task: ScheduledTask,
    conv: Conversation | None,
    *,
    host_store: Any | None,
    permission_store: Any | None,
) -> tuple[str, str] | None:
    """Why ``conv`` cannot take *task*'s prompts, with an error code, or ``None`` if it can.

    Checked when the owner picks a session and again on every firing, since the
    session and the task can both change in between.
    """
    if conv is None:
        return "the session no longer exists", "continued_session_missing"
    # Ownership first: nothing else about a session the owner cannot see is told.
    if permission_store is not None and not await asyncio.to_thread(
        permission_store.check_access, task.user_id or RESERVED_USER_LOCAL, conv.id, LEVEL_OWNER
    ):
        return "the automation's owner does not own the session", "continued_session_forbidden"
    if conv.archived:
        return "the session is archived", "continued_session_archived"
    if conv.parent_conversation_id is not None:
        return "the session is a sub-agent's session", "continued_session_incompatible"
    if conv.agent_id != task.agent_id:
        return "the session runs a different agent", "continued_session_incompatible"
    # A pin the task changed since would otherwise be ignored silently: the
    # session keeps running where it was created.
    if (task.host_id is not None and task.host_id != conv.host_id) or (
        task.workspace is not None and task.workspace != conv.workspace
    ):
        return (
            "the session runs on a different host or workspace than the automation pins",
            "continued_session_incompatible",
        )
    if task.execution_target == "managed_sandbox":
        if conv.host_id is None:
            # Hostless only while the task's own first run provisions its sandbox.
            # The origin label is server-set, so no other session can claim it.
            if conv.labels.get(SCHEDULED_TASK_ORIGIN_LABEL_KEY) != task.id:
                return "the session does not run in a sandbox", "continued_session_incompatible"
        elif conv.host_id != continued_sandbox_host_id(task.id, task.workspace_id):
            # A continue task routes by its own sandbox host, so a session on another
            # sandbox would be unreachable from the replica running the task.
            return (
                "the session runs in a sandbox this automation did not start",
                "continued_session_incompatible",
            )
    host = (
        await asyncio.to_thread(host_store.get_host, conv.host_id)
        if host_store is not None and conv.host_id is not None
        else None
    )
    if host is not None:
        in_sandbox = host.sandbox_provider is not None
        if in_sandbox != (task.execution_target == "managed_sandbox"):
            where = "in a sandbox" if in_sandbox else "on a connected host"
            return (
                f"the session runs {where}, unlike the automation's execution target",
                "continued_session_incompatible",
            )
    return None


class _SessionNotRecorded(Exception):
    """Recording a first firing's session on its task failed.

    ``certain`` means the task was re-read and does not hold the session, so
    the session is safe to delete.
    """

    def __init__(self, *, certain: bool) -> None:
        super().__init__()
        self.certain = certain


class _SessionTaken(Exception):
    """Another run already recorded the session a continue task's first firing created."""


async def _keep_first_session(
    deps: FireDeps, task: ScheduledTask, session_id: str, *, host_id: str | None
) -> bool:
    """Record the session a continue task's first firing created as the one it continues.

    Recorded only once the session exists and its owner can use it, so a run
    never finds a recorded session it cannot continue. ``False`` for a ``"new"`` task, or
    when the task changed since this firing read it: the run then uses a
    session the task does not keep, and the next firing starts one for the
    edited task.

    :raises _SessionTaken: Another first firing recorded its session first.
    :raises _SessionNotRecorded: The write failed, and re-reading the task did
        not show it landed.
    """
    if task.session_mode != "continue":
        return False
    try:
        outcome = await asyncio.to_thread(
            deps.scheduled_task_store.adopt_session,
            task.id,
            session_id,
            snapshot=task,
            host_id=host_id,
        )
    except Exception:
        _logger.exception(
            "scheduled fire: could not record the session task %s continues", task.id
        )
        # The write may have committed before the error reached here.
        try:
            stored = await asyncio.to_thread(deps.scheduled_task_store.get, task.id)
        except Exception:
            _logger.exception("scheduled fire: could not re-read task %s", task.id)
            raise _SessionNotRecorded(certain=False) from None
        if stored is not None and stored.session_id == session_id:
            return True
        raise _SessionNotRecorded(certain=True) from None
    if outcome == "taken":
        raise _SessionTaken
    if outcome == "stale":
        _logger.info(
            "scheduled fire: task %s changed during its first run; not keeping its session",
            task.id,
        )
        return False
    return True


async def _discard_session(deps: FireDeps, conversation_id: str) -> None:
    """Delete a first firing's session that lost to another run's, before anything ran in it.

    Deleting the session also drops its owner grant.
    """
    try:
        await deps.conversation_store.delete_conversation(conversation_id)
    except Exception:
        _logger.exception(
            "scheduled fire: could not delete the unused session %s", conversation_id
        )


async def _continued_host_binding(deps: FireDeps, task: ScheduledTask) -> tuple[str, str] | None:
    """The continue task's sandbox host, with a workspace a new session can use there.

    The workspace is the one the task's last run used on that host. ``None``
    when the host does not exist yet or no earlier session on it is known; the
    fire then provisions the sandbox, or replaces it under the same identity.
    """
    if deps.host_store is None or task.last_run_conversation_id is None:
        return None
    host_id = continued_sandbox_host_id(task.id, task.workspace_id)
    host = await asyncio.to_thread(deps.host_store.get_host, host_id)
    if host is None or host.sandbox_provider is None:
        return None
    earlier = await asyncio.to_thread(
        deps.conversation_store.get_conversation, task.last_run_conversation_id
    )
    if earlier is None or earlier.host_id != host_id or earlier.workspace is None:
        return None
    return host_id, earlier.workspace


def _session_cost_usd(deps: FireDeps, conversation_id: str) -> float:
    """The session's spend so far, as the cost-budget policy counts it."""
    from omnigent.runtime.policies.builder import _policy_usage_seed

    spent = _policy_usage_seed(conversation_id, deps.conversation_store).get("total_cost_usd")
    return float(spent) if isinstance(spent, (int, float)) else 0.0


async def _owner_is_deleted(deps: FireDeps, task: ScheduledTask) -> bool:
    """True when the task's captured owner registration is no longer active.

    A NULL owner (single-user / OSS) always resolves to
    :data:`RESERVED_USER_LOCAL` and is never considered deleted; without a
    permission store there is no account to check against.
    """
    if task.user_id is None or deps.permission_store is None:
        return False
    owner = await asyncio.to_thread(deps.permission_store.get_user, task.user_id)
    return owner is None or owner.account_generation != task.account_generation


async def _resolve_effective_task(deps: FireDeps, task: ScheduledTask) -> ScheduledTask:
    """Resolve the host/workspace the fire actually launches against.

    A managed-sandbox task starts hostless, even if a legacy row has a stale
    host/workspace. The regular managed launch binds a fresh sandbox afterward.

    For connected-host tasks, this fills omitted values for this one fire:

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
    if task.execution_target == "managed_sandbox":
        if deps.sandbox_config is None or not deps.sandbox_config.managed_launch_supported:
            raise _CannotLaunchScheduledFire(
                "managed sandboxes are not configured on this server",
                error_code="managed_sandbox_unavailable",
            )
        return replace(task, host_id=None, workspace=None)

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

    Server-managed sandbox hosts (``sandbox_provider`` set) are skipped: they
    are launch targets the server creates on demand, not machines a
    connected-host automation should silently reuse (a warm sandbox from an
    interactive session would leak its state into the run and break once it is
    gone). An automation that wants a sandbox uses ``execution_target =
    "managed_sandbox"``, which mints a fresh one.
    """
    if deps.host_store is None or deps.host_registry is None:
        raise _CannotLaunchScheduledFire(
            "connected host registry/store is not configured",
            error_code="host_registry_unavailable",
        )
    owner = task.user_id or RESERVED_USER_LOCAL
    hosts = await asyncio.to_thread(deps.host_store.list_hosts, owner)
    for host in hosts:
        # Skip stale-generation hosts (owner account rotated) and server-managed
        # sandbox hosts (never silently reuse a warm interactive sandbox).
        if task.user_id is not None and host.account_generation != task.account_generation:
            continue
        if getattr(host, "sandbox_provider", None) is not None:
            continue
        if deps.host_registry.get(host.host_id) is not None:
            return str(host.host_id)
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


_PERMISSION_MODE_HARNESS = "claude-native"


async def _permission_mode_launch_args(deps: FireDeps, task: ScheduledTask) -> list[str] | None:
    """Derive the native-terminal ``--permission-mode`` args for a task.

    Mirrors how the interactive New Chat dialog builds ``terminal_launch_args``:
    a set permission mode becomes ``["--permission-mode", <value>]``, which the
    runner appends to Claude Code's argv. ``None`` (agent default) sets nothing.

    Fail-safe on harness: only Claude Code accepts ``--permission-mode``, so the
    flag is injected ONLY when the task's agent is confirmed ``claude-native``.
    If the harness can't be resolved (no cache / bundle / a load error), the flag
    is omitted rather than injected — a session that just uses the agent's own
    default is strictly safer than one launched with an unknown flag. This makes
    the Claude-only guarantee hold regardless of whether the create/update/fire
    capability gates ran, so a mis-stamped non-Claude row can never break a fire.
    """
    if task.permission_mode is None:
        return None
    if deps.agent_cache is None:
        return None
    from omnigent.harness_aliases import canonicalize_harness

    try:
        agent = await asyncio.to_thread(deps.agent_store.get, task.agent_id)
        if agent is None or getattr(agent, "bundle_location", None) is None:
            return None
        loaded = await asyncio.to_thread(deps.agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        raw_harness = (executor.config.get("harness") or executor.type) if executor else None
        harness = canonicalize_harness(raw_harness) or raw_harness
    except Exception:
        _logger.exception(
            "scheduled fire: could not resolve harness for task %s; omitting --permission-mode",
            task.id,
        )
        return None
    if harness != _PERMISSION_MODE_HARNESS:
        return None
    return ["--permission-mode", task.permission_mode]


async def _spec_reasoning_effort(deps: FireDeps, task: ScheduledTask) -> str | None:
    """Read ``executor.reasoning_effort`` from the task's agent spec.

    Fail-safe like :func:`_permission_mode_launch_args`: a task with no explicit
    effort inherits the spec default, and any load failure yields ``None`` (the
    harness default) rather than breaking the fire. The spec value is validated
    at spec load, so it needs no re-validation here.
    """
    if deps.agent_cache is None:
        return None
    try:
        agent = await asyncio.to_thread(deps.agent_store.get, task.agent_id)
        if agent is None or getattr(agent, "bundle_location", None) is None:
            return None
        loaded = await asyncio.to_thread(deps.agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        return getattr(executor, "reasoning_effort", None) if executor else None
    except Exception:
        _logger.exception(
            "scheduled fire: could not resolve reasoning_effort for task %s; "
            "using harness default",
            task.id,
        )
        return None


async def _presentation_labels(deps: FireDeps, task: ScheduledTask) -> dict[str, str]:
    """Resolve the terminal-first presentation labels for a fired session.

    The interactive New Chat path stamps ``omnigent.ui = "terminal"`` (plus the
    matching ``omnigent.wrapper`` for native CLIs) at create time so the web
    UI's Chat/Terminal switcher is available; the fire path bypasses that path
    and must stamp the same labels itself, or an automation-created session on a
    terminal harness renders Chat-only with no way to reach its live terminal.

    Two harness shapes get a terminal:

    * A native-CLI wrapper agent (``pi-native-ui`` etc.) — the terminal IS the
      main view. Resolved from the bound agent's name, mirroring
      ``native_coding_agent_for_agent_name`` in the interactive create path.
    * A non-native SDK agent whose runner auto-creates the ``omnigent`` REPL
      terminal — host-bound only (an in-process session has no runner to host
      a terminal), mirroring ``_repl_terminal_ui_labels``.

    Fail-safe: any resolution error omits the labels rather than guessing, so
    the session falls back to Chat-only rather than breaking the fire.
    """
    from omnigent.native.native_coding_agents import native_coding_agent_for_agent_name
    from omnigent.server.routes.sessions import _repl_terminal_ui_labels

    try:
        agent = await asyncio.to_thread(deps.agent_store.get, task.agent_id)
        if agent is None:
            return {}
        # Native-wrapper labels come solely from the agent name, so they resolve
        # without the cache — matching the interactive path, which has no cache
        # dependency for this branch.
        native_agent = native_coding_agent_for_agent_name(agent.name)
        if native_agent is not None:
            return dict(native_agent.presentation_labels)
        # Non-native SDK session: it only gets a REPL terminal when a runner
        # hosts it, so a task with no resolved host stays Chat-only. Resolving
        # the harness for that branch needs the cache.
        if task.host_id is None or deps.agent_cache is None:
            return {}
        return await asyncio.to_thread(
            _repl_terminal_ui_labels,
            agent=agent,
            agent_cache=deps.agent_cache,
            harness_override=None,
        )
    except Exception:
        _logger.exception(
            "scheduled fire: could not resolve presentation labels for task %s; "
            "session will render Chat-only",
            task.id,
        )
        return {}


async def _own_task_agent(deps: FireDeps, task: ScheduledTask) -> ScheduledTask:
    """Move a task saved on another user's agent onto its owner's own copy, once.

    Tasks get the copy when created; this covers ones saved before that.
    """
    agent = await asyncio.to_thread(deps.agent_store.get, task.agent_id)
    if agent is None:
        return task
    bound = await asyncio.to_thread(
        agent_for_user, deps.agent_store, deps.artifact_store, agent, task.user_id
    )
    if bound.id == agent.id:
        return task
    stored = await asyncio.to_thread(deps.scheduled_task_store.update, task.id, agent_id=bound.id)
    # The write moved updated_at; later checks of the snapshot compare against it.
    return replace(
        task,
        agent_id=bound.id,
        updated_at=stored.updated_at if stored is not None else task.updated_at,
    )


async def _agent_revision(deps: FireDeps, conv: Conversation) -> str | None:
    """The bundle the kickoff runs, so the runner can tell when it later changes."""
    agent = await asyncio.to_thread(deps.agent_store.get, conv.agent_id) if conv.agent_id else None
    return (
        mcp_selection_revision(agent.bundle_location, conv.labels) if agent is not None else None
    )


async def _fire_project_id(deps: FireDeps, task: ScheduledTask) -> str | None:
    """Return the project to file a fired session into, or ``None``.

    A deleted project leaves the session unfiled and is cleared from the task.
    """
    if task.project_id is None or deps.project_store is None:
        # Without a store the id can't be verified, so file nothing.
        return None
    owner = task.user_id if task.user_id is not None else deps.single_user_owner
    project = await asyncio.to_thread(deps.project_store.get, task.project_id, user_id=owner)
    if project is None:
        _logger.warning(
            "scheduled fire: task %s project %s no longer exists; leaving the session unfiled",
            task.id,
            task.project_id,
        )
        # Clear only if the task still names this project; never fail the run.
        try:
            await asyncio.to_thread(
                deps.scheduled_task_store.clear_project_id, task.id, task.project_id
            )
        except Exception:
            _logger.exception("scheduled fire: task %s could not clear its project", task.id)
        return None
    return project.id


def _session_labels(task: ScheduledTask) -> dict[str, str]:
    """Labels a fired session starts with.

    A continue task's sandbox session records the host id it routes by, so a
    relaunched sandbox keeps connecting to the replica that owns that id.
    """
    labels = {SCHEDULED_TASK_ORIGIN_LABEL_KEY: task.id}
    if task.execution_target == "managed_sandbox" and task.session_mode == "continue":
        from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY

        labels[REQUESTED_HOST_ID_LABEL_KEY] = continued_sandbox_host_id(task.id, task.workspace_id)
    return labels


async def _create_session(deps: FireDeps, task: ScheduledTask) -> Conversation:
    """Create a conversation bound to the task's agent, carrying the stored spec."""
    conv: Conversation = await asyncio.to_thread(
        deps.conversation_store.create_conversation,
        agent_id=task.agent_id,
        title=task.name,
        host_id=task.host_id,
        workspace=task.workspace,
        terminal_launch_args=await _permission_mode_launch_args(deps, task),
        labels=_session_labels(task),
        project_id=await _fire_project_id(deps, task),
    )
    reasoning_effort = task.reasoning_effort
    if reasoning_effort is None:
        reasoning_effort = await _spec_reasoning_effort(deps, task)
    if task.model_override is not None or reasoning_effort is not None:
        updated: Conversation | None = await asyncio.to_thread(
            deps.conversation_store.update_conversation,
            conv.id,
            model_override=task.model_override,
            reasoning_effort=reasoning_effort,
        )
        if updated is not None:
            conv = updated
    # Stamp terminal-first presentation labels the interactive create path would
    # have set, so a fired session on a terminal harness exposes the
    # Chat/Terminal switcher instead of rendering Chat-only. Stamped last (after
    # the override reload above) so the labels land on the conversation returned
    # to the launch/dispatch caller, not a stale pre-label reload of it.
    labels = await _presentation_labels(deps, task)
    if labels:
        await asyncio.to_thread(deps.conversation_store.set_labels, conv.id, labels)
        conv.labels.update(labels)
    return conv


_COST_BUDGET_HANDLER = "omnigent.policies.builtins.cost.cost_budget"
_COST_BUDGET_POLICY_NAME = "__scheduled_task_cost_budget"


def _cost_budget_policy_id(task_id: str, conversation_id: str) -> str:
    """The id a task's cost cap has in a session, so its later fires find it by key.

    Scoped to the task as well, so an automation only ever changes its own cap,
    never one another automation continuing the same session set.
    """
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"omnigent:{_COST_BUDGET_POLICY_NAME}:{task_id}:{conversation_id}"
    ).hex


class _BudgetConflict(Exception):
    """The session already has a cost cap that is not this task's."""


class _BudgetNotSet(Exception):
    """A continued session's cap could not be set; whatever cap it had stays."""


async def _attach_cost_budget(
    deps: FireDeps,
    task: ScheduledTask,
    conversation_id: str,
    *,
    spent_usd: float = 0.0,
    replace: bool = False,
) -> None:
    """Attach a cost_budget policy to a session spawned by a scheduled task.

    The budget is per fire, and the policy gates cumulative session spend, so a
    continued session's cap is set to ``spent_usd`` plus the budget. With
    ``replace``, the cap an earlier fire set under the session's fixed id is
    updated in place, so a failed write leaves it in force; a budget since
    cleared deletes it.

    For a new session a failure only logs a warning — an uncapped new session
    is better than a dead run.

    :raises _BudgetConflict: With ``replace``, when the session already has a
        cap under another id: another automation's, or one set before caps had
        a fixed id. That cap would keep applying, so the run should not go on.
    :raises _BudgetNotSet: With ``replace``, when the cap could not be set.
    """
    if deps.policy_store is None or (task.max_cost_usd is None and not replace):
        return
    from omnigent.runtime.policies.builder import invalidate_session_policy_specs_cache

    policy_id = _cost_budget_policy_id(task.id, conversation_id)
    cap = None if task.max_cost_usd is None else {"max_cost_usd": spent_usd + task.max_cost_usd}
    try:
        if cap is None:
            await asyncio.to_thread(deps.policy_store.delete, policy_id, conversation_id)
        elif not replace or (
            await asyncio.to_thread(
                deps.policy_store.update,
                policy_id,
                conversation_id,
                factory_params=cap,
                enabled=True,
            )
            is None
        ):
            try:
                await asyncio.to_thread(
                    deps.policy_store.create,
                    policy_id=policy_id,
                    session_id=conversation_id,
                    name=_COST_BUDGET_POLICY_NAME,
                    type="python",
                    handler=_COST_BUDGET_HANDLER,
                    factory_params=cap,
                    enabled=True,
                )
            except IntegrityError:
                # The name is taken. Under the fixed id it is another run's cap
                # from moments ago; under any other id it is not this task's.
                if not replace:
                    raise
                if (
                    await asyncio.to_thread(deps.policy_store.get, policy_id, conversation_id)
                    is None
                ):
                    raise _BudgetConflict from None
        if replace:
            invalidate_session_policy_specs_cache(conversation_id)
    except _BudgetConflict:
        raise
    except Exception as exc:
        if replace:
            raise _BudgetNotSet from exc
        _logger.warning(
            "scheduled fire: failed to attach cost budget for task %s (session %s)",
            task.id,
            conversation_id,
            exc_info=True,
        )


async def _grant_owner(deps: FireDeps, task: ScheduledTask, conversation_id: str) -> None:
    """Write the LEVEL_OWNER grant so the run is visible to its owner.

    A NULL ``user_id`` (single-user / OSS) resolves to
    :data:`RESERVED_USER_LOCAL`, whose row is created on demand. A real owner's
    row is never (re)created here — a deleted account must stay deleted. When
    ``permission_store`` is ``None`` (no auth configured) this is a no-op — the
    session is still accessible because auth is disabled system-wide.
    """
    if deps.permission_store is None:
        return
    if task.user_id is None:
        await asyncio.to_thread(deps.permission_store.ensure_user, RESERVED_USER_LOCAL)
        owner = RESERVED_USER_LOCAL
    else:
        owner = task.user_id
    await asyncio.to_thread(deps.permission_store.grant, owner, conversation_id, LEVEL_OWNER)


async def _grant_default_public(deps: FireDeps, task: ScheduledTask, conversation_id: str) -> None:
    """Add the default ``__public__`` read grant when the server policy covers
    this run. Best-effort: default-public access is not required for the run to
    proceed, so the caller isolates any failure here rather than failing the run.
    """
    if deps.permission_store is None or deps.app_state is None:
        return
    from omnigent.server.sharing_settings import (
        host_is_managed_sandbox,
        new_session_starts_public,
    )

    managed = task.execution_target == "managed_sandbox" or host_is_managed_sandbox(
        deps.host_registry, task.host_id
    )
    if not new_session_starts_public(deps.app_state, managed=managed, workspace=task.workspace):
        return
    await asyncio.to_thread(deps.permission_store.ensure_user, RESERVED_USER_PUBLIC)
    await asyncio.to_thread(
        deps.permission_store.grant, RESERVED_USER_PUBLIC, conversation_id, LEVEL_READ
    )


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
    now = _stamp_last_run_sync(deps, task, conversation_id)
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


def _stamp_last_run_sync(deps: FireDeps, task: ScheduledTask, conversation_id: str | None) -> int:
    """Stamp ``last_run_at`` (and the fired session, if any) on the task; return the time."""
    now = int(time.time())
    update_fields: dict[str, Any] = {"last_run_at": now}
    if conversation_id is not None:
        update_fields["last_run_conversation_id"] = conversation_id
    deps.scheduled_task_store.update(task.id, **update_fields)
    return now


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
        validate_session_permission_mode(task.permission_mode)
        # NB: the harness gate for permission_mode is enforced fail-safe in
        # _permission_mode_launch_args (the flag is injected only for a confirmed
        # claude-native agent), so a mis-stamped non-Claude row degrades to "no
        # flag" rather than failing the whole fire here.
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
    if task.user_id is not None and host.account_generation != task.account_generation:
        raise _CannotLaunchScheduledFire(
            f"connected host {host_id!r} belongs to a different account registration",
            error_code="host_authority_revoked",
        )
    if host.sandbox_provider is not None:
        raise _CannotLaunchScheduledFire(
            "automations cannot use an existing sandbox; select a new sandbox for each run",
            error_code="existing_sandbox_not_allowed",
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
        await _send_prompt(deps, fresh or conv, runner_client, task)

    return _dispatch


# The first run records its session a moment before it starts the sandbox
# launch; a run continuing the session meanwhile waits this long for it.
_LAUNCH_START_WAIT_S = 30.0
_LAUNCH_START_POLL_S = 0.25


async def _await_launch_start(
    deps: FireDeps, conv: Conversation
) -> tuple[Conversation, ManagedLaunch | None]:
    """Wait for a hostless sandbox session's launch to begin, or its host to bind.

    Returns the session as last read and its launch, if one is in flight.
    """
    if deps.managed_launches is None:
        return conv, None
    deadline = time.monotonic() + _LAUNCH_START_WAIT_S
    while True:
        launch = deps.managed_launches.get(conv.id)
        if launch is not None or time.monotonic() >= deadline:
            return conv, launch
        await asyncio.sleep(_LAUNCH_START_POLL_S)
        conv = await asyncio.to_thread(deps.conversation_store.get_conversation, conv.id) or conv
        if conv.host_id is not None:
            return conv, None


def _make_reuse_dispatch(deps: FireDeps) -> LaunchDispatch:
    """Build the seam that sends a task's prompt into an existing session.

    Wakes the session's runner through the same ladder a user message uses
    (live runner, relaunch on its online host, or wake its managed sandbox); a
    session whose runner cannot be woken raises so the fire records a failure.
    """

    async def _dispatch(conv: Conversation, task: ScheduledTask) -> None:
        from omnigent.server.routes.sessions import (
            _await_settled_managed_launch,
            ensure_runner_connected,
        )

        launch = None
        if conv.host_id is None and task.execution_target == "managed_sandbox":
            conv, launch = await _await_launch_start(deps, conv)
        if launch is not None:
            # The first run's sandbox is still provisioning. Wait for it the way a
            # user's message does, then read the host and workspace it bound.
            await _await_settled_managed_launch(launch)
            conv = (
                await asyncio.to_thread(deps.conversation_store.get_conversation, conv.id) or conv
            )
        if task.execution_target == "managed_sandbox" and conv.host_id != (
            continued_sandbox_host_id(task.id, task.workspace_id)
        ):
            # A sandbox automation's prompt never goes to a runner outside its
            # sandbox, such as a local one a hostless session may have.
            raise RuntimeError(f"session {conv.id} is not bound to the automation's sandbox")
        runner_client, conv = await ensure_runner_connected(
            session_id=conv.id,
            conv=conv,
            app_state=deps.app_state,
            conversation_store=deps.conversation_store,
            runner_router=deps.runner_router,
        )
        if runner_client is None:
            raise RuntimeError(f"no runner can be reached or woken for session {conv.id}")
        await _send_prompt(deps, conv, runner_client, task)

    return _dispatch


async def _send_prompt(
    deps: FireDeps, conv: Conversation, runner_client: Any, task: ScheduledTask
) -> None:
    """Initialize the runner's session if needed and dispatch the task's prompt."""
    from omnigent.server.routes.sessions import (
        _dispatch_session_event_to_runner,
        _ensure_runner_session_initialized,
    )

    await _ensure_runner_session_initialized(conv.id, conv, runner_client, deps.conversation_store)
    await _dispatch_session_event_to_runner(
        conv.id,
        conv,
        _prompt_event(task.prompt),
        deps.conversation_store,
        runner_client,
        agent_name=None,
        file_store=deps.file_store,
        artifact_store=deps.artifact_store,
        created_by=task.user_id or RESERVED_USER_LOCAL,
        runner_router=deps.runner_router,
        agent_revision=await _agent_revision(deps, conv),
    )


def _make_managed_sandbox_dispatch(deps: FireDeps) -> LaunchDispatch:
    """Build the managed-sandbox seam that saves the task's prompt in the session.

    The prompt is saved as the session's first startup input, the same way
    ``POST /v1/sessions`` saves an ``initial_input``. The replica holding the
    sandbox host's tunnel delivers it through normal dispatch once the host
    connects, re-checking the owner's access then.
    """

    async def _dispatch(conv: Conversation, task: ScheduledTask) -> None:
        from omnigent.server.background_session_titles import BACKGROUND_SESSION_TITLES_HEADER

        if deps.startup_inputs is None:
            raise RuntimeError("managed sandboxes are not configured")
        # No browser preference applies to a fire; keep the server default.
        await deps.startup_inputs.enqueue(
            conv.id,
            _prompt_event(task.prompt),
            user_id=task.user_id or RESERVED_USER_LOCAL,
            headers=Headers({BACKGROUND_SESSION_TITLES_HEADER: "on"}),
        )

    return _dispatch


def _make_managed_sandbox_start(deps: FireDeps) -> ManagedLaunchStart:
    """Build the seam that provisions a FRESH sandbox for a managed fire.

    Reuses the ``host_type="managed"`` pipeline ``POST /v1/sessions`` uses:
    :func:`_run_managed_launch` provisions the sandbox (``relaunch_host`` left
    unset), binds a clean ``$HOME/workspace`` + a new host to the session and
    launches the runner. It runs as a tracked background task, so a launch
    failure fails both the saved prompt and the fire's run.
    """

    def _start(
        conv: Conversation,
        task: ScheduledTask,
        host_id: str | None,
        error_codes: _LaunchErrorCodes,
    ) -> asyncio.Task[None]:
        from omnigent.server.routes.sessions import (
            _publish_sandbox_status,
            _run_managed_launch,
            _start_managed_launch_task,
        )

        if deps.sandbox_config is None or deps.managed_launches is None or deps.host_store is None:
            raise RuntimeError("managed sandboxes are not configured")
        tracker = deps.managed_launches
        tracker.begin(conv.id)
        _publish_sandbox_status(
            conv.id, "provisioning", provider=deps.sandbox_config.default.provider
        )

        async def _launch() -> None:
            try:
                # A continue task's sandbox host outlives a session reset; a new
                # sandbox generation replaces it rather than a second host row.
                existing = (
                    await asyncio.to_thread(deps.host_store.get_host, host_id)
                    if host_id is not None
                    else None
                )
                await _run_managed_launch(
                    session_id=conv.id,
                    owner=task.user_id or RESERVED_USER_LOCAL,
                    sandbox_config=deps.sandbox_config,
                    repos=(),
                    tracker=tracker,
                    conversation_store=deps.conversation_store,
                    host_store=deps.host_store,
                    host_registry=deps.host_registry,
                    tunnel_registry=deps.tunnel_registry,
                    provider=None,
                    agent_store=deps.agent_store,
                    agent_id=task.agent_id,
                    host_id=host_id,
                    relaunch_host=existing
                    if existing is not None and existing.sandbox_provider is not None
                    else None,
                )
            except Exception as exc:
                await _fail_running_run(
                    deps,
                    conv.id,
                    f"managed sandbox failed to launch: {exc}",
                    _launch_error_code(tracker, conv.id, error_codes),
                )
                raise
            launch = tracker.get(conv.id)
            if launch is not None and launch.error is not None:
                await _fail_running_run(
                    deps,
                    conv.id,
                    f"managed sandbox launch failed: {launch.error}",
                    _launch_error_code(tracker, conv.id, error_codes),
                )

        return _start_managed_launch_task(
            conv.id,
            _launch(),
            conversation_store=deps.conversation_store,
            tracker=tracker,
            startup=deps.startup_inputs,
        )

    return _start


def _launch_error_code(
    tracker: ManagedLaunchTracker, session_id: str, codes: _LaunchErrorCodes
) -> str:
    launch = tracker.get(session_id)
    return (
        codes.after_binding
        if launch is not None and launch.bound.is_set()
        else codes.before_binding
    )


async def _fail_running_run(
    deps: FireDeps, conversation_id: str, error: str, error_code: str
) -> None:
    """Fail a fired session's still-``running`` run; a terminal run is left as is."""

    def _transition() -> None:
        run = deps.scheduled_task_store.get_running_run_by_conversation(conversation_id)
        if run is not None:
            deps.scheduled_task_store.update_run(
                run.id,
                status="failed",
                finished_at=int(time.time()),
                error=error,
                error_code=error_code,
            )

    await asyncio.to_thread(_transition)


class _ClaimedRunRecorder:
    """Persistent dispatch: record progress on the claimed run, under its startup lease."""

    def __init__(
        self,
        deps: FireDeps,
        task: ScheduledTask,
        lease: StartupLease,
        *,
        registration_timeout_s: float = SANDBOX_REGISTRATION_TIMEOUT_S,
    ) -> None:
        self._deps = deps
        self._task = task
        self._lease = lease
        self._registration_timeout_s = registration_timeout_s

    async def check(self) -> None:
        await self._lease.check()

    async def session_created(self, conversation_id: str) -> None:
        await self._lease.attach_session(conversation_id)

    async def finish(
        self,
        status: str,
        *,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._lease.finish(
            status, conversation_id=conversation_id, error=error, error_code=error_code
        )
        # The outcome is already recorded; a failed stamp only leaves the task's
        # last-run summary stale.
        try:
            await asyncio.to_thread(
                _stamp_last_run_sync, self._deps, self._task, self._lease.run.conversation_id
            )
        except Exception:
            _logger.exception(
                "scheduled fire: could not stamp the last run on task %s", self._task.id
            )

    async def hand_off_managed(
        self, conv: Conversation, task: ScheduledTask, start: ManagedLaunchStart | None
    ) -> None:
        if start is None:
            await self.finish("running", conversation_id=conv.id)
            return
        await self.check()
        # The sandbox registers under the run's host ID, so host-ID routing sends
        # its connection back to the replica that owns this run.
        try:
            launch_task = start(conv, task, self._lease.run.host_id, _STAGED_LAUNCH_ERRORS)
        except Exception as exc:
            _logger.exception(
                "scheduled fire: managed launch did not start for task %s (session %s)",
                task.id,
                conv.id,
            )
            reason = f"managed sandbox failed to launch: {exc}"
            await self._fail_saved_prompt(conv.id, reason)
            await self.finish(
                "failed",
                conversation_id=conv.id,
                error=reason,
                error_code=_STAGED_LAUNCH_ERRORS.before_binding,
            )
            return
        # The saved prompt alone is not a handoff: until the session records its
        # host, no other replica can finish the launch or deliver the prompt.
        try:
            async with asyncio.timeout(self._registration_timeout_s):
                error = await _wait_for_managed_binding(self._deps, conv.id, launch_task)
        except TimeoutError:
            await self._abandon_unregistered_sandbox(conv.id, launch_task)
            return
        if error is not None:
            await self.finish(
                "failed",
                conversation_id=conv.id,
                error=error,
                error_code=_STAGED_LAUNCH_ERRORS.before_binding,
            )
            return
        await self.finish("running", conversation_id=conv.id)
        # The runner launch after binding can fail before this write commits,
        # while the launch's own check still found no running run to fail.
        error = _managed_launch_failure(self._deps.managed_launches, conv.id, launch_task)
        if error is not None:
            await _fail_running_run(
                self._deps, conv.id, error, _STAGED_LAUNCH_ERRORS.after_binding
            )

    async def _fail_saved_prompt(self, conversation_id: str, reason: str) -> None:
        if self._deps.startup_inputs is not None:
            await self._deps.startup_inputs.record_launch_failure(conversation_id, reason)

    async def _abandon_unregistered_sandbox(
        self, conversation_id: str, launch_task: asyncio.Task[None]
    ) -> None:
        """Fail a sandbox that never registered: its prompt, its launch, then the sandbox."""
        reason = f"the sandbox did not register within {self._registration_timeout_s:g} seconds"
        # Fail the prompt first, so a host that binds during cleanup delivers nothing,
        # and the cancelled launch finds no pending prompt to fail with its own reason.
        await self._fail_saved_prompt(conversation_id, reason)
        launch_task.cancel()
        await asyncio.wait({launch_task}, timeout=_ABANDONED_LAUNCH_SETTLE_S)
        await _terminate_run_sandbox(self._deps, self._lease.run.host_id)
        await self.finish(
            "failed",
            conversation_id=conversation_id,
            error=reason,
            error_code="sandbox_registration_timeout",
        )


async def _wait_for_managed_binding(
    deps: FireDeps, session_id: str, launch_task: asyncio.Task[None]
) -> str | None:
    """Wait for a managed launch to bind its host; return why it did not, or ``None``.

    Leaves the launch running: once bound, it finishes on its own.
    """
    tracker = deps.managed_launches
    launch = tracker.get(session_id) if tracker is not None else None
    waiters: list[asyncio.Future[Any]] = []
    if launch is not None:
        waiters = [
            asyncio.ensure_future(launch.bound.wait()),
            asyncio.ensure_future(launch.settled.wait()),
        ]
    try:
        await asyncio.wait([*waiters, launch_task], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    conv = await asyncio.to_thread(deps.conversation_store.get_conversation, session_id)
    if conv is not None and conv.host_id is not None:
        return None
    return (
        _managed_launch_failure(tracker, session_id, launch_task)
        or "managed sandbox launch ended before its host connected"
    )


def _managed_launch_failure(
    tracker: ManagedLaunchTracker | None, session_id: str, launch_task: asyncio.Task[None]
) -> str | None:
    """Why a managed launch failed, if it has; ``None`` while it is running or succeeded."""
    launch = tracker.get(session_id) if tracker is not None else None
    if launch is not None and launch.error is not None:
        return f"managed sandbox launch failed: {launch.error}"
    if launch_task.done() and not launch_task.cancelled() and launch_task.exception() is not None:
        return f"managed sandbox failed to launch: {launch_task.exception()}"
    return None


# How far back recovery reads a fired session's transcript for its prompt and reply.
_RECOVERY_TRANSCRIPT_ITEMS = 50


@dataclass(frozen=True)
class _TranscriptEvidence:
    """What a fired session's newest transcript items show about its first turn."""

    # The prompt reached the agent: it is in the transcript, or the agent produced output.
    delivered: bool
    # The newest item after the prompt, apart from runner bookkeeping, is the agent's reply.
    answered: bool


def _transcript_evidence(items: list[Any]) -> _TranscriptEvidence:
    """Read items newest first. Runner bookkeeping such as ``resource_event`` is not evidence."""
    answered = agent_output = False
    for item in items:
        role = getattr(item.data, "role", None) if item.type == "message" else None
        if role == "user":
            return _TranscriptEvidence(delivered=True, answered=answered)
        if role == "assistant" and not agent_output:
            answered = True
        if item.type != "resource_event":
            agent_output = True
    return _TranscriptEvidence(delivered=agent_output, answered=answered)


def _delivered_run_outcome(
    conv: Conversation, evidence: _TranscriptEvidence
) -> Literal["running", "succeeded", "failed"]:
    """Outcome of a run whose prompt reached the agent.

    ``live_status`` alone cannot prove the turn finished: a session can read
    ``idle`` with its prompt saved but never run. Only an idle session whose
    newest output is the agent's reply has finished.
    """
    if conv.live_status == "failed":
        return "failed"
    if conv.live_status == "idle" and evidence.answered:
        return "succeeded"
    return "running"


class ClaimedRunExecutor:
    """Start runs the persistent scheduler claimed, and recover interrupted startups.

    Both run as the task's owner: the account and host-owner scopes the legacy
    fire path uses, plus an optional *fire_identity* (e.g. an assumed
    delegation) for deployments whose stores need the owner's credentials.
    """

    def __init__(
        self,
        deps: FireDeps,
        *,
        fire_identity: FireIdentity | None = None,
        launch_dispatch: LaunchDispatch | None = None,
        registration_timeout_s: float = SANDBOX_REGISTRATION_TIMEOUT_S,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._deps = deps
        self._fire_identity = fire_identity
        self._fire_dispatch = _build_fire_dispatch(deps, launch_dispatch)
        self._registration_timeout_s = registration_timeout_s
        self._now = now

    async def fire(self, task: ScheduledTask, lease: StartupLease) -> None:
        """``on_fire``: start the claimed run and record its startup outcome."""
        recorder = _ClaimedRunRecorder(
            self._deps, task, lease, registration_timeout_s=self._registration_timeout_s
        )
        async with contextlib.AsyncExitStack() as stack:
            try:
                await self._enter_owner_identity(stack, task)
            except FireIdentityRevoked:
                _logger.warning(
                    "scheduled fire: task %s owner can no longer be acted for",
                    task.id,
                    exc_info=True,
                )
                await self._fail_revoked_fire(task, recorder)
                return
            except Exception:
                _logger.warning(
                    "scheduled fire: could not act as the owner of task %s", task.id, exc_info=True
                )
                await recorder.finish(
                    "failed",
                    error="Could not act as the automation's owner. The next run tries again.",
                    error_code="owner_identity_unavailable",
                )
                return
            await _run_fire_for_task(self._deps, task, self._fire_dispatch, recorder)

    async def _fail_revoked_fire(self, task: ScheduledTask, recorder: _FireRecorder) -> None:
        # A deleted owner's identity is revoked too; disable the task as the
        # fire path does, instead of asking a deleted account to re-authorize.
        try:
            deleted = await _owner_is_deleted(self._deps, task)
        except Exception:
            _logger.warning(
                "scheduled fire: could not check the owner of task %s", task.id, exc_info=True
            )
            deleted = False
        if deleted:
            await _disable_for_deleted_owner(self._deps, task, recorder)
            return
        await recorder.finish(
            "failed",
            error="Could not act as the automation's owner. "
            "The owner must re-authorize the automation.",
            error_code="reauthorization_required",
        )

    async def recover(
        self, run: ScheduledTaskRun
    ) -> Literal["running", "succeeded", "failed"] | None:
        """``on_expired``: decide an interrupted startup from its session; never resend.

        A temporary failure to act as the owner propagates, so a later sweep
        checks the run again.
        """
        task = await asyncio.to_thread(self._deps.scheduled_task_store.get, run.scheduled_task_id)
        async with contextlib.AsyncExitStack() as stack:
            if task is not None:
                try:
                    await self._enter_owner_identity(stack, task)
                except FireIdentityRevoked:
                    # Nothing can be inspected or cleaned up as the owner any more;
                    # an abandoned sandbox is left to the provider's lifetime cap.
                    _logger.warning(
                        "scheduled fire: task %s owner can no longer be acted for; "
                        "recording run %s failed without inspecting it",
                        task.id,
                        run.id,
                        exc_info=True,
                    )
                    return "failed"
            return await self._inspect_startup(run)

    async def _enter_owner_identity(
        self, stack: contextlib.AsyncExitStack, task: ScheduledTask
    ) -> None:
        if self._fire_identity is not None:
            await stack.enter_async_context(self._fire_identity(task))
        stack.enter_context(account_authority_scope(task.user_id, task.account_generation))
        stack.enter_context(host_owner_scope(task.user_id, task.account_generation))

    async def _inspect_startup(
        self, run: ScheduledTaskRun
    ) -> Literal["running", "succeeded", "failed"] | None:
        from omnigent.server.scheduled.schedule import sandbox_host_id

        if run.conversation_id is None:
            # Launch starts only after the session is recorded, so nothing ran.
            return "failed"
        conv = await asyncio.to_thread(
            self._deps.conversation_store.get_conversation, run.conversation_id
        )
        # Decided from the run, not the task: its target may have changed since
        # the claim. Only a sandbox run's host ID is derived, from its occurrence
        # or, for a task that continues a session, from the task.
        managed = run.trigger_key is not None and run.host_id in (
            sandbox_host_id(run.scheduled_task_id, run.workspace_id, run.trigger_key),
            continued_sandbox_host_id(run.scheduled_task_id, run.workspace_id),
        )
        if conv is None:
            if managed:
                await self._terminate_sandbox(run)
            return "failed"
        if managed:
            return await self._inspect_managed_startup(run, conv)
        evidence = await self._transcript_evidence(conv.id)
        if not evidence.delivered and conv.live_status not in ("running", "waiting"):
            # The prompt was never sent, and recovery never sends it.
            return "failed"
        return _delivered_run_outcome(conv, evidence)

    async def _transcript_evidence(self, conversation_id: str) -> _TranscriptEvidence:
        page = await asyncio.to_thread(
            self._deps.conversation_store.list_items,
            conversation_id,
            limit=_RECOVERY_TRANSCRIPT_ITEMS,
            order="desc",
        )
        return _transcript_evidence(page.data)

    async def _inspect_managed_startup(
        self, run: ScheduledTaskRun, conv: Conversation
    ) -> Literal["running", "succeeded", "failed"] | None:
        from omnigent.server.startup_inputs import STARTUP_LABEL

        if conv.labels.get(STARTUP_LABEL) == "failed":
            # The launch failure is already recorded on the session. A sandbox
            # that bound anyway can never deliver the failed prompt.
            await self._terminate_sandbox(run)
            return "failed"
        if conv.host_id is not None:
            # The prompt was saved before the launch began; the host's replica
            # delivers it.
            pending = await asyncio.to_thread(
                self._deps.conversation_store.list_pending_inputs, conv.id
            )
            if any(item.status in ("in_progress", "incomplete") for item in pending):
                return "running"
            evidence = await self._transcript_evidence(conv.id)
            if not evidence.delivered and conv.live_status not in ("running", "waiting"):
                # Nothing is saved to deliver and nothing reached the agent: a
                # session that joined the task's sandbox never got its prompt.
                return "failed"
            return _delivered_run_outcome(conv, evidence)
        tracker = self._deps.managed_launches
        launch = tracker.get(conv.id) if tracker is not None else None
        if launch is not None and not launch.settled.is_set():
            # Still provisioning on this server; a later sweep sees the binding.
            return None
        # A replica shutting down stops renewing at once, but keeps finishing
        # unbound launches for the deployment's shutdown drain.
        drain = getattr(self._deps.sandbox_config, "shutdown_drain_s", 0.0) or 0.0
        if run.lease_expires_at is not None and self._now() < run.lease_expires_at + drain:
            return None
        reason = (
            launch.error
            if launch is not None and launch.error is not None
            else "the sandbox launch stopped before its host connected"
        )
        await self._terminate_sandbox(run)
        if self._deps.startup_inputs is not None:
            await self._deps.startup_inputs.record_launch_failure(conv.id, reason)
        return "failed"

    async def _terminate_sandbox(self, run: ScheduledTaskRun) -> None:
        # A continue task's sandbox outlives any one session: another session of
        # the task, such as one started after a reset, may be using it now, and
        # the task's next run reuses it. Recovery leaves it to the task and the
        # provider's lifetime cap.
        if run.host_id == continued_sandbox_host_id(run.scheduled_task_id, run.workspace_id):
            return
        await _terminate_run_sandbox(self._deps, run.host_id)


async def _terminate_run_sandbox(deps: FireDeps, host_id: str | None) -> None:
    """Tear down the sandbox registered under a run's host ID, if one exists."""
    from omnigent.server.managed_hosts import terminate_managed_host

    if host_id is None or deps.host_store is None:
        return
    host = await asyncio.to_thread(deps.host_store.get_host, host_id)
    # Only a server-managed sandbox is torn down, never a user's own host.
    if host is None or host.sandbox_provider is None:
        return
    await terminate_managed_host(host, deps.host_store, deps.sandbox_config)
