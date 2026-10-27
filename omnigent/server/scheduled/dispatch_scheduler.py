"""Database polling, startup leases, and recovery; see README.md for callback contracts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from omnigent.db.db_models import workspace_scope
from omnigent.debug_logging import debug_event
from omnigent.entities import ScheduledTask, ScheduledTaskRun
from omnigent.server.scheduled.config import DEFAULT_SCHEDULER_CONFIG, ScheduledTaskSchedulerConfig
from omnigent.server.scheduled.rrule import RRuleSearchError, RRuleValidationError, validate_rrule
from omnigent.server.scheduled.schedule import (
    next_scheduled_time,
    occurrence_host_id,
    scheduled_trigger_key,
)
from omnigent.stores.scheduled_task_store.dispatch import (
    DeletedTaskCursor,
    DispatchCursor,
    ScheduledTaskDispatchStore,
    TaskCursor,
)

_logger = logging.getLogger(__name__)


def _run_event(event_name: str, run: ScheduledTaskRun, **attributes: object) -> dict[str, object]:
    """Structured debug-log event for one scheduled run, keyed by its session when known."""
    return debug_event(
        event_name,
        session_id=run.conversation_id,
        # Explicit so a clashing attribute raises and type checks don't match **attributes.
        turn_id=None,
        user_id=None,
        task_id=run.scheduled_task_id,
        run_id=run.id,
        task_workspace_id=run.workspace_id,
        scheduled_at=run.scheduled_at,
        **attributes,
    )


def _log_safely(log: Callable[[], None]) -> None:
    """Run a deferred event log; a logging failure must never change scheduling."""
    try:
        log()
    except Exception:
        with contextlib.suppress(Exception):
            _logger.exception("Could not log a scheduler event")


def _log_event(log: Callable[..., None], message: str, *args: object, **kwargs: object) -> None:
    """Log a scheduler event at its call site; a logging failure must never change scheduling."""
    try:
        # Attribute the record to the caller rather than to this helper.
        log(message, *args, stacklevel=2, **kwargs)
    except Exception:
        with contextlib.suppress(Exception):
            _logger.exception("Could not log a scheduler event")


def _log_recovery_result(
    run: ScheduledTaskRun, status: str, transition: asyncio.Future[ScheduledTaskRun | None]
) -> None:
    if transition.cancelled() or transition.exception() is not None:
        # The sweep logs failures it awaits; _log_late_recovery_failure covers the rest.
        return
    # None means the run was no longer the expired startup recovery inspected.
    outcome = status if transition.result() is not None else "superseded"
    _logger.info(
        "Recovery recorded outcome %s for run %s",
        outcome,
        run.id,
        extra=_run_event("scheduled_run_recovered", run, outcome=outcome),
    )


def _log_late_recovery_failure(
    run: ScheduledTaskRun, transition: asyncio.Future[ScheduledTaskRun | None]
) -> None:
    error = None if transition.cancelled() else transition.exception()
    if error is not None:
        _logger.error(
            "Recovery update for run %s failed after its sweep timed out; "
            "a later sweep will check the run again",
            run.id,
            exc_info=(type(error), error, error.__traceback__),
            extra=_run_event("scheduled_run_recovered", run, outcome="error"),
        )


def _seconds(value: float) -> float:
    # Clock skew between replicas must not produce a negative delay.
    return round(max(0.0, float(value)), 3)


class StartupLeaseLost(RuntimeError):
    """Startup must stop; the executor can no longer prove a live claim."""


class StartupLease:
    """Serialize heartbeats and executor checkpoints for one locally owned startup."""

    def __init__(
        self,
        store: ScheduledTaskDispatchStore,
        run: ScheduledTaskRun,
        *,
        now: Callable[[], float],
        lease_seconds: int,
        target: str | None = None,
    ) -> None:
        self.run = run
        self._store = store
        self._now = now
        self._lease_seconds = lease_seconds
        self._target = target
        self._lock = asyncio.Lock()
        self._writes: set[asyncio.Task[None]] = set()

    async def check(self) -> None:
        """Confirm and renew the lease before advancing startup; raise if it was lost."""
        await self._write(
            lambda: self._store.renew_starting_run(
                self.run.id, now=self._now, lease_seconds=self._lease_seconds
            ),
            renewing=True,
        )

    async def attach_session(self, conversation_id: str) -> None:
        """Persist the session before dispatch so crash recovery can inspect it."""
        # Measured at the call, so database time is not counted as fire delay.
        recorded_at = self._now()

        def log_session(previous: ScheduledTaskRun) -> None:
            # Checked under the lease lock, so concurrent calls log each session once.
            if previous.conversation_id != conversation_id:
                _logger.info(
                    "Recorded session %s for scheduled run %s",
                    conversation_id,
                    self.run.id,
                    extra=_run_event(
                        "scheduled_run_session_attached",
                        self.run,
                        target=self._target,
                        session_delay_seconds=_seconds(recorded_at - self.run.scheduled_at),
                    ),
                )

        await self._transition("starting", conversation_id=conversation_id, committed=log_session)

    async def finish(
        self,
        status: str,
        *,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record prompt delivery or a known startup failure, with a live lease."""
        if status not in {"running", "failed", "skipped"}:
            raise ValueError(
                f"Cannot finish startup with status {status!r}. "
                "Use 'running' after sending the prompt, 'failed' for a known failure, "
                "or 'skipped' when execution is intentionally skipped."
            )

        finished_at = self._now()

        def log_outcome(_previous: ScheduledTaskRun) -> None:
            _logger.info(
                "Scheduled run %s startup ended with status %s",
                self.run.id,
                status,
                extra=_run_event(
                    "scheduled_run_startup_ended",
                    self.run,
                    target=self._target,
                    outcome=status,
                    error_code=error_code,
                    prompt_delay_seconds=_seconds(finished_at - self.run.scheduled_at)
                    if status == "running"
                    else None,
                ),
            )

        await self._transition(
            status,
            conversation_id=conversation_id,
            error=error,
            error_code=error_code,
            committed=log_outcome,
        )

    def _when_writes_settle(self, callback: Callable[[], None]) -> None:
        """Run *callback* once in-flight lease writes finish, or now if there are none."""
        pending = list(self._writes)
        if not pending:
            _log_safely(callback)
            return
        settled = asyncio.gather(*pending, return_exceptions=True)
        settled.add_done_callback(lambda _: _log_safely(callback))

    async def _transition(
        self,
        status: str,
        *,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        committed: Callable[[ScheduledTaskRun], None] | None = None,
    ) -> None:
        await self._write(
            lambda: self._store.transition_starting_run(
                self.run,
                now=self._now,
                status=status,
                conversation_id=conversation_id,
                error=error,
                error_code=error_code,
            ),
            committed=committed,
        )

    async def _write(
        self,
        operation: Callable[[], ScheduledTaskRun | None],
        *,
        renewing: bool = False,
        committed: Callable[[ScheduledTaskRun], None] | None = None,
    ) -> None:
        async def apply() -> None:
            try:
                previous = self.run
                with workspace_scope(self.run.workspace_id):
                    run = await asyncio.to_thread(operation)
                if run is None or (
                    renewing
                    and (
                        run.status != "starting"
                        or run.lease_expires_at is None
                        or run.lease_expires_at <= self._now()
                    )
                ):
                    raise StartupLeaseLost(
                        f"Cannot update startup for run {self.run.id}: the run or its lease "
                        "has changed or expired. Stop further startup actions and let recovery "
                        "check the run."
                    )
                self.run = run
                # Runs even if the caller was cancelled, so committed outcomes are logged.
                if committed is not None:
                    _log_safely(lambda: committed(previous))
            finally:
                self._lock.release()

        # Waiting callers can cancel. Once acquired, the shielded write owns the
        # lock until its database call and snapshot update finish.
        await self._lock.acquire()
        write = asyncio.create_task(apply())
        self._writes.add(write)
        write.add_done_callback(self._write_done)
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            write.add_done_callback(self._log_cancelled_write_failure)
            raise

    def _log_cancelled_write_failure(self, write: asyncio.Task[None]) -> None:
        if write.cancelled():
            return
        error = write.exception()
        if error is not None and not isinstance(error, StartupLeaseLost):
            _logger.error(
                "Lease write failed for run %s after its caller was cancelled",
                self.run.id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _write_done(self, write: asyncio.Task[None]) -> None:
        self._writes.discard(write)
        if not write.cancelled():
            write.exception()  # Retrieve errors even if the caller was cancelled.


ClaimedFire = Callable[[ScheduledTask, StartupLease], Awaitable[None]]
ReconcileStartup = Callable[[ScheduledTaskRun], Awaitable[Literal["running", "failed"] | None]]
OwnsHost = Callable[[int, str | None], Awaitable[bool]]


async def _owns_every_host(_workspace_id: int, _host_id: str | None) -> bool:
    return True


@dataclass(frozen=True)
class ScheduledTaskDispatchHooks:
    """Execution, recovery, and host-ownership callbacks; contracts are in README.md."""

    on_fire: ClaimedFire
    on_expired: ReconcileStartup
    owns_host: OwnsHost = _owns_every_host


class PersistentTaskScheduler:
    """Poll due tasks, supervise bounded concurrent startups, and recover expired leases."""

    def __init__(
        self,
        store: ScheduledTaskDispatchStore,
        on_fire: ClaimedFire,
        on_expired: ReconcileStartup,
        *,
        owns_host: OwnsHost = _owns_every_host,
        now: Callable[[], float] = time.time,
        poll_seconds: float = DEFAULT_SCHEDULER_CONFIG.poll_seconds,
        renewal_seconds: float = DEFAULT_SCHEDULER_CONFIG.renewal_seconds,
        lease_seconds: int = DEFAULT_SCHEDULER_CONFIG.lease_seconds,
        batch_size: int = DEFAULT_SCHEDULER_CONFIG.batch_size,
        max_startups: int = DEFAULT_SCHEDULER_CONFIG.max_startups,
        recovery_seconds: float = DEFAULT_SCHEDULER_CONFIG.recovery_seconds,
    ) -> None:
        config = ScheduledTaskSchedulerConfig(
            poll_seconds=poll_seconds,
            renewal_seconds=renewal_seconds,
            lease_seconds=lease_seconds,
            batch_size=batch_size,
            max_startups=max_startups,
            recovery_seconds=recovery_seconds,
        )
        store.check_dispatch_support()
        store.configure_dispatch(True)
        self._store = store
        self._on_fire = on_fire
        self._on_expired = on_expired
        self._owns_host = owns_host
        self._now = now
        self._poll_seconds = config.poll_seconds
        self._renewal_seconds = config.renewal_seconds
        self._lease_seconds = config.lease_seconds
        self._batch_size = config.batch_size
        self._max_startups = config.max_startups
        self._recovery_seconds = config.recovery_seconds
        self._loop: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[None] | None = None
        self._startups: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._poll_lock = asyncio.Lock()
        self._stopped = False
        self._initialization_cursor: TaskCursor | None = None
        self._cleanup_cursor: DeletedTaskCursor | None = None
        self._recovery_cursor: DispatchCursor | None = None
        self._recovery_cutoff: int | None = None

    @property
    def is_started(self) -> bool:
        return self._loop is not None

    async def start(self) -> None:
        """Start a self-retrying poll loop; a DB outage must not disable scheduling."""
        async with self._poll_lock:
            if self._loop is None:
                self._stopped = False
                self._loop = asyncio.create_task(self._run(), name="scheduled-task-poller")

    async def stop(self) -> None:
        """Stop polling and cancel local startups; unfinished claims remain recoverable."""
        self._stopped = True
        loop = self._loop
        if loop is not None:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        startups = list(self._startups.values())
        if self._recovery is not None:
            startups.append(self._recovery)
        for task in startups:
            task.cancel()
        await asyncio.gather(*startups, return_exceptions=True)
        self._startups.clear()
        self._loop = None
        self._recovery = None
        self._recovery_cursor = None
        self._recovery_cutoff = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                _logger.exception(
                    "Could not check scheduled tasks for work; will check again on the next poll",
                    extra=debug_event("scheduled_task_operation_failed", operation="poll"),
                )
            await asyncio.sleep(self._poll_seconds)

    async def poll_once(self) -> None:
        """Serialize due-task polling and start recovery in the background if idle."""
        async with self._poll_lock:
            if self._stopped:
                return
            if self._recovery is None or self._recovery.done():
                self._recovery = asyncio.create_task(
                    self._recover(), name="scheduled-task-recovery"
                )
            try:
                await self._initialize()
            except Exception:
                _logger.exception(
                    "Could not read tasks needing a next run time; retrying next poll",
                    extra=debug_event("scheduled_task_operation_failed", operation="initialize"),
                )
            await self._claim_due()

    async def _initialize(self) -> None:
        tasks = await asyncio.to_thread(
            self._store.list_uninitialized_tasks,
            after=self._initialization_cursor,
            limit=self._batch_size,
        )
        for task in tasks:
            self._initialization_cursor = (task.workspace_id, task.id)
            try:
                if not await self._validate_schedule(task):
                    continue
                next_run = await asyncio.to_thread(next_scheduled_time, task, int(self._now()))
                if next_run is not None:
                    with workspace_scope(task.workspace_id):
                        await asyncio.to_thread(self._store.initialize_next_run_at, task, next_run)
            except Exception:
                _logger.exception(
                    "Could not initialize the next run time for task %s; "
                    "will check it again on a later scan",
                    task.id,
                    extra=debug_event(
                        "scheduled_task_operation_failed",
                        operation="initialize",
                        task_id=task.id,
                        task_workspace_id=task.workspace_id,
                    ),
                )
        if len(tasks) < self._batch_size:
            self._initialization_cursor = None

    async def _validate_schedule(self, task: ScheduledTask) -> bool:
        try:
            await asyncio.to_thread(validate_rrule, task.rrule)
        except RRuleSearchError:
            raise
        except RRuleValidationError:
            with workspace_scope(task.workspace_id):
                paused = await asyncio.to_thread(self._store.pause_invalid_schedule, task)
            if paused:
                _logger.warning("Paused task %s with an invalid schedule", task.id, exc_info=True)
            return False
        return True

    async def _recover(self) -> None:
        try:
            await self._reconcile()
        except Exception:
            _logger.exception(
                "Could not check runs with expired startup leases; "
                "recovery will try again on the next poll",
                extra=debug_event("scheduled_task_operation_failed", operation="recovery"),
            )
        try:
            self._cleanup_cursor = await asyncio.to_thread(
                self._store.purge_deleted_tasks, after=self._cleanup_cursor, limit=self._batch_size
            )
        except Exception:
            _logger.exception(
                "Could not finish deleted-task cleanup; retrying next poll",
                extra=debug_event("scheduled_task_operation_failed", operation="cleanup"),
            )

    async def _reconcile(self) -> None:
        # Keep the cutoff fixed until the scan wraps so new expirations cannot
        # indefinitely postpone retrying uncertain outcomes from earlier pages.
        if self._recovery_cutoff is None:
            self._recovery_cutoff = int(self._now())
        runs = await asyncio.to_thread(
            self._store.list_expired_starting_runs,
            self._recovery_cutoff,
            after=self._recovery_cursor,
            limit=self._batch_size,
        )
        for run in runs:
            assert run.lease_expires_at is not None
            self._recovery_cursor = (run.lease_expires_at, run.workspace_id, run.id)
            transition: asyncio.Future[ScheduledTaskRun | None] | None = None
            try:
                async with asyncio.timeout(self._recovery_seconds):
                    if await self._owns_host(run.workspace_id, run.host_id):
                        local = self._startups.get((run.workspace_id, run.id))
                        if local is not None:
                            local.cancel()
                            await asyncio.gather(local, return_exceptions=True)
                        with workspace_scope(run.workspace_id):
                            status = await self._on_expired(run)
                            if status is None:
                                _log_event(
                                    _logger.info,
                                    "Recovery could not yet determine the outcome of run %s",
                                    run.id,
                                    extra=_run_event(
                                        "scheduled_run_recovered", run, outcome="uncertain"
                                    ),
                                )
                            else:
                                if status not in ("running", "failed"):
                                    raise ValueError(
                                        f"Invalid recovery result {status!r}. "
                                        "Expected 'running', 'failed', or None "
                                        "to check again later."
                                    )
                                transition = asyncio.ensure_future(
                                    asyncio.to_thread(
                                        self._store.transition_starting_run,
                                        run,
                                        now=self._now,
                                        status=status,
                                        expired=True,
                                        error="Run startup did not complete before its lease "
                                        "expired; recovery marked the run as failed."
                                        if status == "failed"
                                        else None,
                                        error_code="startup_expired"
                                        if status == "failed"
                                        else None,
                                    )
                                )
                                # Logged on completion, even if this sweep times out first.
                                transition.add_done_callback(
                                    lambda done, run=run, status=status: _log_safely(
                                        lambda: _log_recovery_result(run, status, done)
                                    )
                                )
                                await asyncio.shield(transition)
            except TimeoutError:
                _log_event(
                    _logger.warning,
                    "Recovery timed out for run %s; an in-flight database update may "
                    "still complete. If the run remains starting, a later sweep "
                    "will check it again",
                    run.id,
                    extra=_run_event("scheduled_run_recovered", run, outcome="timeout"),
                )
                if transition is not None:
                    self._report_late_recovery_failure(run, transition)
            except asyncio.CancelledError:
                # Stopping cancels the sweep; the shielded update may still fail later.
                if transition is not None:
                    self._report_late_recovery_failure(run, transition)
                raise
            except Exception:
                _logger.exception(
                    "Could not finish recovery for run %s after its startup lease expired; "
                    "will check the run again on a later poll",
                    run.id,
                    extra=_run_event("scheduled_run_recovered", run, outcome="error"),
                )
        if len(runs) < self._batch_size:
            self._recovery_cursor = None
            self._recovery_cutoff = None

    @staticmethod
    def _report_late_recovery_failure(
        run: ScheduledTaskRun, transition: asyncio.Future[ScheduledTaskRun | None]
    ) -> None:
        # Nothing awaits the update any more, so its failure is logged here.
        transition.add_done_callback(
            lambda done: _log_safely(lambda: _log_late_recovery_failure(run, done))
        )

    async def _claim_due(self) -> None:
        cutoff = int(self._now())
        cursor: DispatchCursor | None = None
        while not self._stopped and len(self._startups) < self._max_startups:
            tasks = await asyncio.to_thread(
                self._store.list_due_tasks, cutoff, after=cursor, limit=self._batch_size
            )
            if not tasks:
                return
            for task in tasks:
                if self._stopped or len(self._startups) >= self._max_startups:
                    return
                assert task.next_run_at is not None
                cursor = (task.next_run_at, task.workspace_id, task.id)
                try:
                    if not await self._validate_schedule(task):
                        continue
                    host_id = occurrence_host_id(task, scheduled_trigger_key(task.next_run_at))
                    if not await self._owns_host(task.workspace_id, host_id):
                        continue
                    now = int(self._now())
                    next_run = await asyncio.to_thread(next_scheduled_time, task, now)
                    if self._stopped:
                        return
                    with workspace_scope(task.workspace_id):
                        run = await asyncio.to_thread(
                            self._store.claim_scheduled_run,
                            task,
                            now=now,
                            next_run_at=next_run,
                            host_id=host_id,
                            lease_seconds=self._lease_seconds,
                        )
                    if run is None:
                        _log_event(
                            _logger.info,
                            "Skipped occurrence %s of task %s: it was already claimed, "
                            "or the task changed after it was listed",
                            task.next_run_at,
                            task.id,
                            extra=debug_event(
                                "scheduled_run_claim_skipped",
                                task_id=task.id,
                                task_workspace_id=task.workspace_id,
                                scheduled_at=task.next_run_at,
                                target=task.execution_target,
                            ),
                        )
                    else:
                        # Start first so a failing claim log cannot keep the run from starting.
                        if not self._stopped:
                            key = (run.workspace_id, run.id)
                            startup = asyncio.create_task(self._execute(task, run))
                            self._startups[key] = startup
                            startup.add_done_callback(
                                lambda _, key=key: self._startups.pop(key, None)
                            )
                        _log_event(
                            _logger.info,
                            "Claimed scheduled run %s for task %s",
                            run.id,
                            task.id,
                            extra=_run_event(
                                "scheduled_run_claimed",
                                run,
                                target=task.execution_target,
                                host_id=run.host_id,
                                claim_delay_seconds=_seconds(now - run.scheduled_at),
                            ),
                        )
                except Exception:
                    _logger.exception(
                        "Could not prepare a scheduled run for task %s; "
                        "will check the task again on a later poll",
                        task.id,
                        extra=debug_event(
                            "scheduled_task_operation_failed",
                            operation="claim",
                            task_id=task.id,
                            task_workspace_id=task.workspace_id,
                        ),
                    )

    async def _execute(self, task: ScheduledTask, run: ScheduledTaskRun) -> None:
        lease = StartupLease(
            self._store,
            run,
            now=self._now,
            lease_seconds=self._lease_seconds,
            target=task.execution_target,
        )
        # Logged only if startup ends without a final status from the executor.
        outcome = "unfinished"
        with workspace_scope(run.workspace_id):
            fire = asyncio.create_task(self._fire(task, lease))
            heartbeat = asyncio.create_task(self._heartbeat(lease))
            try:
                done, _ = await asyncio.wait(
                    {fire, heartbeat}, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    finished.result()
                if heartbeat in done:
                    await fire
            except StartupLeaseLost:
                outcome = "lease_lost"
                _logger.warning(
                    "Startup lease lost for run %s (status=%s); stopping local startup work",
                    run.id,
                    lease.run.status,
                )
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception:
                outcome = "interrupted"
                # Recovery inspects runs still marked starting after lease expiry.
                _logger.exception(
                    "Startup interrupted for run %s (status=%s); "
                    "stopping local startup work and lease renewals",
                    run.id,
                    lease.run.status,
                )
            finally:
                fire.cancel()
                heartbeat.cancel()
                await asyncio.gather(fire, heartbeat, return_exceptions=True)

                def log_unfinished() -> None:
                    if lease.run.status == "starting":
                        _logger.info(
                            "Scheduled run %s startup ended without a result (%s); "
                            "recovery will check it after its lease expires",
                            lease.run.id,
                            outcome,
                            extra=_run_event(
                                "scheduled_run_startup_ended",
                                lease.run,
                                target=task.execution_target,
                                outcome=outcome,
                            ),
                        )

                # A cancelled finish can still commit; log only if it did not.
                lease._when_writes_settle(log_unfinished)

    async def _fire(self, task: ScheduledTask, lease: StartupLease) -> None:
        await lease.check()
        await self._on_fire(task, lease)
        if lease.run.status == "starting":
            _logger.warning(
                "on_fire returned while run %s still has status 'starting'; "
                "no final startup outcome was recorded. "
                "This run remains available for recovery after its lease expires",
                lease.run.id,
            )

    async def _heartbeat(self, lease: StartupLease) -> None:
        while lease.run.status == "starting":
            remaining = (lease.run.lease_expires_at or 0) - self._now()
            await asyncio.sleep(max(0, min(self._renewal_seconds, remaining)))
            if lease.run.status != "starting":
                return
            try:
                remaining = (lease.run.lease_expires_at or 0) - self._now()
                if remaining <= 0:
                    raise StartupLeaseLost(f"Startup lease expired for run {lease.run.id}")
                async with asyncio.timeout(remaining):
                    await lease.check()
            except StartupLeaseLost:
                # Finish may have won the lock while this heartbeat was waiting.
                if lease.run.status != "starting":
                    return
                raise
            except Exception as exc:
                if lease.run.status != "starting":
                    return
                if (lease.run.lease_expires_at or 0) <= self._now():
                    raise StartupLeaseLost(
                        f"Startup lease expired for run {lease.run.id} while renewal was failing"
                    ) from exc
                _log_event(
                    _logger.warning,
                    "Could not renew the startup lease for run %s; retrying while its "
                    "last confirmed lease remains valid",
                    lease.run.id,
                    exc_info=True,
                    extra=_run_event(
                        "scheduled_task_operation_failed", lease.run, operation="lease_renewal"
                    ),
                )
