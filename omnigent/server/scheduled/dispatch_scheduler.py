"""Poll due tasks and supervise startup leases; callback contracts are in README.md."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from omnigent.db.db_models import workspace_scope
from omnigent.entities import ScheduledTask, ScheduledTaskRun
from omnigent.server.scheduled.config import DEFAULT_SCHEDULER_CONFIG, ScheduledTaskSchedulerConfig
from omnigent.server.scheduled.rrule import RRuleSearchError, RRuleValidationError, validate_rrule
from omnigent.server.scheduled.schedule import (
    next_scheduled_time,
    occurrence_host_id,
    scheduled_trigger_key,
)
from omnigent.stores.scheduled_task_store.dispatch import (
    DispatchCursor,
    ScheduledTaskDispatchStore,
    TaskCursor,
)

_logger = logging.getLogger(__name__)


class StartupLeaseLost(RuntimeError):
    """Startup must stop; the executor can no longer prove a live claim."""


class StartupLease:
    """Serialize database updates for one locally owned startup."""

    def __init__(
        self,
        store: ScheduledTaskDispatchStore,
        run: ScheduledTaskRun,
        *,
        now: Callable[[], float],
        lease_seconds: int,
    ) -> None:
        self.run = run
        self._store = store
        self._now = now
        self._lease_seconds = lease_seconds
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
        await self._transition("starting", conversation_id=conversation_id)

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
        await self._transition(
            status, conversation_id=conversation_id, error=error, error_code=error_code
        )

    async def _transition(
        self,
        status: str,
        *,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._write(
            lambda: self._store.transition_starting_run(
                self.run,
                now=self._now,
                status=status,
                conversation_id=conversation_id,
                error=error,
                error_code=error_code,
            )
        )

    async def _write(
        self, operation: Callable[[], ScheduledTaskRun | None], *, renewing: bool = False
    ) -> None:
        async def apply() -> None:
            try:
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
OwnsHost = Callable[[int, str | None], Awaitable[bool]]


async def _owns_every_host(_workspace_id: int, _host_id: str | None) -> bool:
    return True


class PersistentTaskScheduler:
    """Poll due tasks and supervise bounded concurrent startups."""

    def __init__(
        self,
        store: ScheduledTaskDispatchStore,
        on_fire: ClaimedFire,
        *,
        owns_host: OwnsHost = _owns_every_host,
        now: Callable[[], float] = time.time,
        poll_seconds: float = DEFAULT_SCHEDULER_CONFIG.poll_seconds,
        renewal_seconds: float = DEFAULT_SCHEDULER_CONFIG.renewal_seconds,
        lease_seconds: int = DEFAULT_SCHEDULER_CONFIG.lease_seconds,
        batch_size: int = DEFAULT_SCHEDULER_CONFIG.batch_size,
        max_startups: int = DEFAULT_SCHEDULER_CONFIG.max_startups,
    ) -> None:
        config = ScheduledTaskSchedulerConfig(
            poll_seconds=poll_seconds,
            renewal_seconds=renewal_seconds,
            lease_seconds=lease_seconds,
            batch_size=batch_size,
            max_startups=max_startups,
        )
        store.check_dispatch_support()
        store.configure_dispatch(True)
        self._store = store
        self._on_fire = on_fire
        self._owns_host = owns_host
        self._now = now
        self._poll_seconds = config.poll_seconds
        self._renewal_seconds = config.renewal_seconds
        self._lease_seconds = config.lease_seconds
        self._batch_size = config.batch_size
        self._max_startups = config.max_startups
        self._loop: asyncio.Task[None] | None = None
        self._startups: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._poll_lock = asyncio.Lock()
        self._stopped = False
        self._initialization_cursor: TaskCursor | None = None

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
        for task in startups:
            task.cancel()
        await asyncio.gather(*startups, return_exceptions=True)
        self._startups.clear()
        self._loop = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                _logger.exception(
                    "Could not check scheduled tasks for work; will check again on the next poll"
                )
            await asyncio.sleep(self._poll_seconds)

    async def poll_once(self) -> None:
        """One non-overlapping poll; useful for deterministic tests and embedders."""
        async with self._poll_lock:
            if self._stopped:
                return
            try:
                await self._initialize()
            except Exception:
                _logger.exception(
                    "Could not read tasks needing a next run time; retrying next poll"
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
                    if run is not None and not self._stopped:
                        key = (run.workspace_id, run.id)
                        startup = asyncio.create_task(self._execute(task, run))
                        self._startups[key] = startup
                        startup.add_done_callback(lambda _, key=key: self._startups.pop(key, None))
                except Exception:
                    _logger.exception(
                        "Could not prepare a scheduled run for task %s; "
                        "will check the task again on a later poll",
                        task.id,
                    )

    async def _execute(self, task: ScheduledTask, run: ScheduledTaskRun) -> None:
        lease = StartupLease(self._store, run, now=self._now, lease_seconds=self._lease_seconds)
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
                _logger.warning(
                    "Startup lease lost for run %s (status=%s); stopping local startup work",
                    run.id,
                    lease.run.status,
                )
            except Exception:
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
                _logger.warning(
                    "Could not renew the startup lease for run %s; retrying while its "
                    "last confirmed lease remains valid",
                    lease.run.id,
                    exc_info=True,
                )
