"""Persistence contract for the database scheduler and its fire executor."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from omnigent.entities import ScheduledTask, ScheduledTaskRun

# Ordered exactly like the due-task and expired-startup indexes.
DispatchCursor = tuple[int, int, str]
TaskCursor = tuple[int, str]
# Resume both the task scan and an unfinished page scan within that task.
DeletedTaskCursor = tuple[int, str, tuple[int, str] | None]


@runtime_checkable
class ScheduledTaskDispatchStore(Protocol):
    """Global discovery/reset; per-task mutations require the row's workspace scope."""

    def configure_dispatch(self, enabled: bool) -> None:
        """Select persisted cursor maintenance before serving requests."""
        ...

    def configure_trigger_polling(self, seconds: int | None) -> None:
        """Set how soon a saved trigger automation first polls; ``None`` keeps its schedule."""
        ...

    def check_dispatch_support(self) -> None:
        """Raise if the backend cannot provide atomic multi-statement claims."""
        ...

    def reset_dispatch_cursors(self) -> None:
        """Clear all persisted cursors and skip pending runs before starting legacy timers."""
        ...

    def list_uninitialized_tasks(
        self, *, after: TaskCursor | None = None, limit: int = 100
    ) -> list[ScheduledTask]: ...

    def purge_deleted_tasks(
        self, *, after: DeletedTaskCursor | None = None, limit: int = 100
    ) -> DeletedTaskCursor | None:
        """Clean bounded run pages, returning progress within and across deleted tasks."""
        ...

    def initialize_next_run_at(self, task: ScheduledTask, next_run_at: int) -> bool:
        """Initialize a legacy NULL schedule only if the task is still unchanged."""
        ...

    def pause_invalid_schedule(self, task: ScheduledTask) -> bool:
        """Pause only if the active task still matches the rejected snapshot."""
        ...

    def complete_schedule(self, task: ScheduledTask) -> bool:
        """Mark an active schedule with no remaining occurrence completed, if unchanged."""
        ...

    def list_due_tasks(
        self, now: int, *, after: DispatchCursor | None = None, limit: int = 100
    ) -> list[ScheduledTask]: ...

    def claim_scheduled_run(
        self,
        task: ScheduledTask,
        *,
        now: int,
        next_run_at: int | None,
        host_id: str | None,
        lease_seconds: int,
    ) -> ScheduledTaskRun | None:
        """Advance an unchanged task and insert its unique starting run atomically.

        A ``None`` next run marks the task completed.

        An already-recorded occurrence advances the cursor but returns None,
        as does a changed task. Neither case starts another run. A retry of an
        earlier run that has not started is recorded as ``skipped``
        (``retry_superseded``).

        A timer runs one firing at a time: while its previous run is unfinished, this
        returns None and changes nothing. Otherwise it first deletes the timer's
        finished runs, so a timer keeps only its newest one.
        """
        ...

    def skip_scheduled_occurrence(
        self, task: ScheduledTask, *, now: int, next_run_at: int
    ) -> bool:
        """Advance an unchanged task past its due occurrence without recording a run.

        Returns False, changing nothing, if the task changed since it was listed.
        """
        ...

    def has_unfinished_run(self, scheduled_task_id: str, *, running_since: int) -> bool:
        """Whether a run of the task is ``starting``, ``running``, or a waiting Run now request.

        Asked before a due occurrence of a task whose ``overlap_mode`` is ``skip``.
        A ``running`` run that fired at or before *running_since* (or, with no fire
        time, was scheduled then) is stale and doesn't count, and neither does a
        retry waiting out its delay. The read is bounded, so a full page of stale
        ``running`` runs could hide a newer one. This fails that page, as the
        stale-run backstop would, and answers True until a later call can tell.
        """
        ...

    def skip_overlapping_occurrence(
        self,
        task: ScheduledTask,
        *,
        now: int,
        next_run_at: int,
        host_id: str | None,
        running_since: int | None,
    ) -> ScheduledTaskRun | None:
        """Advance an unchanged task past its due occurrence, recording it as ``skipped``.

        For a task whose ``overlap_mode`` is ``skip`` while its previous run is still
        going: the run gets ``error_code`` ``previous_run_active``, and a retry still
        waiting is superseded as a claim would. With *running_since*, the write first
        checks again, under the task lock, that :meth:`has_unfinished_run` still
        holds. It records nothing if that run ended meanwhile, or if only stale
        runs it cannot read past are in the way, which it fails. Pass ``None`` when
        the session the task continues is what is busy. Returns None then, and if
        the task changed since it was listed; an occurrence that already has a run
        only advances.
        """
        ...

    def request_manual_run(
        self, task: ScheduledTask, *, now: int, trigger_key: str, host_id: str | None
    ) -> ScheduledTaskRun | None:
        """Record a Run now request as a pending ``scheduled`` run, for any undeleted task.

        Returns None if the task no longer exists or was deleted.
        """
        ...

    def list_pending_runs(
        self, *, after: TaskCursor | None = None, limit: int = 100
    ) -> list[ScheduledTaskRun]:
        """Pending runs across workspaces, keyset-paged by ``(workspace_id, id)``."""
        ...

    def claim_pending_run(
        self, run: ScheduledTaskRun, *, now: int, lease_seconds: int
    ) -> tuple[ScheduledTask, ScheduledTaskRun] | None:
        """Start a pending run under a lease, returning the task snapshot to fire.

        Returns None if another replica already claimed it, or for a retry whose
        ``scheduled_at`` has not come yet. A retry is recorded as ``skipped``
        instead when its task is paused (``task_paused``), now allows fewer
        retries (``retry_cancelled``), or has its next occurrence due
        (``retry_superseded``). A run whose task was
        deleted is recorded as ``skipped`` instead. A run whose host changed with
        the task's target gets the new host and stays pending. Both return None.
        """
        ...

    def renew_starting_run(
        self,
        run_id: str,
        *,
        now: Callable[[], float],
        lease_seconds: int,
        dispatching: bool = False,
    ) -> ScheduledTaskRun | None:
        """Renew only an unexpired startup, reading now after locking.

        Returns the current row even if no renewal occurred. Callers must check
        status and lease expiry; a non-None result alone does not confirm renewal.

        With *dispatching*, a renewed run's ``prompt_dispatch`` becomes ``"started"``:
        its prompt is about to leave the executor. Every claim records
        ``"not_started"``, which is the only proof recovery accepts that a run
        without a session never sent its prompt.
        """
        ...

    def list_expired_starting_runs(
        self, now: int, *, after: DispatchCursor | None = None, limit: int = 100
    ) -> list[ScheduledTaskRun]: ...

    def transition_starting_run(
        self,
        run: ScheduledTaskRun,
        *,
        now: Callable[[], float],
        status: str,
        expired: bool = False,
        conversation_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> ScheduledTaskRun | None:
        """Finish startup conditionally on its lease still matching this snapshot.

        A ``failed`` startup marked *retryable* never got its prompt to the agent. The
        same write records the occurrence's next retry, a pending run whose
        ``scheduled_at`` is when it may start. None is recorded if the task has no
        retry left or already has one waiting, or if a later occurrence is due
        first or already has a run.

        Executors use an unexpired lease. Reconcilers use ``expired=True`` only
        after checking whether the prompt was sent: preserve a started run,
        record ``succeeded`` if its session already finished, otherwise fail it.
        Never infer prompt delivery solely from lease expiry.

        Deleting a task prevents future claims; it does not cancel an existing
        claim. Startup and recovery may still record progress for that run.
        """
        ...

    def hand_back_timer_firing(
        self, run: ScheduledTaskRun, *, now: Callable[[], float], host_id: str
    ) -> ScheduledTaskRun | None:
        """Return a claimed timer firing unsent, for the replica that owns *host_id* to claim.

        Under the task lock and the run's live startup lease, the timer takes *host_id* and
        is due again a second later, so the next claim gets its own trigger key. A one-shot
        that its claim completed is active again. The run records ``skipped``
        (``timer_host_moved``), or ``timer_cancelled`` when the timer was deleted first.

        :param run: The claimed firing, e.g. a ``StartupLease``'s run.
        :param now: The clock the commit reads, e.g. ``time.time``.
        :param host_id: The host the timer's session moved to, e.g. ``"host_2"``.
        :returns: The finished run, or ``None`` when the run's lease no longer matches.
        """
        ...
