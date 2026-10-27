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

    def check_dispatch_support(self) -> None:
        """Raise if the backend cannot provide atomic multi-statement claims."""
        ...

    def reset_dispatch_cursors(self) -> None:
        """Clear all persisted cursors before starting legacy timers after a rollback."""
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
        as does a changed task. Neither case starts another run.
        """
        ...

    def renew_starting_run(
        self, run_id: str, *, now: Callable[[], float], lease_seconds: int
    ) -> ScheduledTaskRun | None:
        """Renew only an unexpired startup, reading now after locking.

        Returns the current row even if no renewal occurred. Callers must check
        status and lease expiry; a non-None result alone does not confirm renewal.
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
    ) -> ScheduledTaskRun | None:
        """Finish startup conditionally on its lease still matching this snapshot.

        Executors use an unexpired lease. Reconcilers use ``expired=True`` only
        after checking whether the prompt was sent: preserve a started run,
        otherwise fail it. Never infer prompt delivery solely from lease expiry.

        Deleting a task prevents future claims; it does not cancel an existing
        claim. Startup and recovery may still record progress for that run.
        """
        ...
