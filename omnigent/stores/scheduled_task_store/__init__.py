"""Scheduled-task store — persists scheduled tasks and their run history.

A scheduled task is a saved instruction that fires an agent session on a
recurring schedule. This store owns the ``scheduled_tasks``
table and its ``scheduled_task_runs`` history table.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import ScheduledTask, ScheduledTaskRun

# Sentinel meaning "caller did not supply this argument; leave the column unchanged."
# Distinct from None, which means "set the column to NULL."
_UNSET: Any = object()


class ScheduledTaskStore(ABC):
    """
    Abstract base for scheduled-task persistence.

    Manages the lifecycle of scheduled tasks (CRUD) and their run history. The
    ``list_active`` read path returns active tasks ordered by ``(created_at, id)``.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the scheduled-task store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///chat.db"`` for SQLAlchemy.
        """
        self.storage_location = storage_location

    # ── Scheduled tasks ──────────────────────────────────────────

    @abstractmethod
    def create(
        self,
        scheduled_task_id: str,
        name: str,
        prompt: str,
        rrule: str,
        user_id: str | None,
        agent_id: str,
        timezone: str,
        *,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
        host_id: str | None = None,
        state: str = "active",
    ) -> ScheduledTask:
        """
        Insert a new scheduled task.

        :param scheduled_task_id: Pre-generated unique task id (a UUID string).
        :param name: Human-readable task name.
        :param prompt: The instruction dispatched to the agent on each firing.
        :param rrule: The required RFC 5545 recurrence rule for the recurring
            trigger, e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``.
        :param user_id: User the spawned session's ``LEVEL_OWNER`` grant
            is written for; ``None`` in single-user mode.
        :param agent_id: The agent bound to this task.
        :param timezone: IANA timezone the trigger is evaluated in.
        :param model_override: Optional LLM model override.
        :param reasoning_effort: Optional reasoning-effort hint.
        :param workspace: Runner start path (source repo / working dir).
        :param host_id: The connected host to pin the run to.
        :param state: Lifecycle state — ``active``/``paused``/``deleted``.
            Defaults to ``"active"``.
        :returns: The newly created :class:`ScheduledTask`.
        :raises ValueError: If ``state`` is not a recognized value.
        """
        ...

    @abstractmethod
    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        """
        Return a scheduled task by id, or ``None`` if not found.

        :param scheduled_task_id: Opaque task identifier.
        :returns: The :class:`ScheduledTask` if found, else ``None``.
        """
        ...

    @abstractmethod
    def list(self, *, owner_user_id: str | None = None) -> list[ScheduledTask]:
        """
        List all scheduled tasks ordered by ``created_at ASC, id ASC``.

        :param owner_user_id: When given, return only tasks owned by this user.
        :returns: List of :class:`ScheduledTask` instances.
        """
        ...

    @abstractmethod
    def list_active(self) -> list[ScheduledTask]:
        """
        List active scheduled tasks ordered by ``created_at ASC, id ASC``.

        Returns only tasks in the ``active`` state.

        :returns: List of :class:`ScheduledTask` instances in state ``active``.
        """
        ...

    @abstractmethod
    def list_active_all_workspaces(self) -> list[ScheduledTask]:
        """
        List active scheduled tasks across every workspace.

        Scheduler startup runs outside a request workspace scope, so it cannot
        rely on ``current_workspace_id()`` without missing tenant-scoped rows.

        :returns: Active tasks ordered by ``workspace_id, created_at, id``.
        """
        ...

    @abstractmethod
    def update(
        self,
        scheduled_task_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        rrule: str | None = None,
        timezone: str | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
        host_id: str | None = _UNSET,
        state: str | None = None,
        last_run_at: int | None = None,
        last_run_conversation_id: str | None = _UNSET,
    ) -> ScheduledTask | None:
        """
        Update mutable fields of a task.

        Most parameters use ``None`` to mean "leave unchanged". For ``host_id``
        and ``last_run_conversation_id``, the sentinel default means "not
        provided / leave unchanged"; passing ``None`` explicitly sets the column
        to NULL (e.g. to clear a host binding or to null out the last-run
        conversation after it is deleted).

        Passing ``rrule`` updates the recurring trigger; ``None``
        leaves it unchanged.

        Returns ``None`` if the task does not exist.

        :param scheduled_task_id: Opaque task identifier.
        :returns: The updated :class:`ScheduledTask`, or ``None`` if not found.
        """
        ...

    @abstractmethod
    def delete(self, scheduled_task_id: str) -> bool:
        """
        Delete a scheduled task. Idempotent.

        :param scheduled_task_id: Opaque task identifier.
        :returns: ``True`` if removed; ``False`` if not found.
        """
        ...

    # ── Runs ─────────────────────────────────────────────────────

    @abstractmethod
    def create_run(
        self,
        run_id: str,
        scheduled_task_id: str,
        status: str,
        scheduled_at: int,
        *,
        conversation_id: str | None = None,
        fired_at: int | None = None,
        finished_at: int | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> ScheduledTaskRun:
        """
        Insert a new scheduled-task-run row.

        :param run_id: Pre-generated unique run id (a UUID string).
        :param scheduled_task_id: The task this run belongs to.
        :param status: One of ``scheduled``/``running``/``succeeded``/
            ``failed``/``skipped``.
        :param scheduled_at: Unix epoch seconds the firing was scheduled for.
        :param conversation_id: Optional conversation created by this firing.
        :param fired_at: Optional Unix epoch seconds dispatch began.
        :param finished_at: Optional Unix epoch seconds of terminal state.
        :param error: Optional failure detail.
        :param error_code: Optional short failure classification for future
            retry logic (e.g. ``"timeout"``, ``"rate_limited"``).
        :returns: The newly created :class:`ScheduledTaskRun`.
        """
        ...

    @abstractmethod
    def list_runs(
        self,
        scheduled_task_id: str,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> tuple[list[ScheduledTaskRun], str | None]:
        """
        List one page of a task's runs ordered by ``scheduled_at DESC, id DESC``
        (most recent first).

        Cursor-paginated so run history is never silently truncated: returns
        ``(runs, next_cursor)`` where ``next_cursor`` is the id to pass as
        ``after_id`` for the next page, or ``None`` when the last page is
        reached.

        :param scheduled_task_id: The task whose runs to return.
        :param limit: Maximum number of runs per page. Defaults to 100.
        :param after_id: Return runs ordered after this run id (exclusive).
        :returns: ``(runs, next_cursor)``.
        """
        ...

    @abstractmethod
    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: int,
        error: str | None = None,
        error_code: str | None = None,
    ) -> ScheduledTaskRun | None:
        """
        Transition a still-``running`` run to a terminal status.

        Idempotent and conditional: the update only applies to a run whose
        current status is ``running`` (guarded by ``WHERE status = running``),
        so a run already advanced to a terminal state (a fire-time
        ``skipped``/``failed``, or a prior reconciliation) is never clobbered
        and two concurrent sweeps cannot double-transition it.

        :param run_id: The run to transition.
        :param status: The terminal status to set — ``succeeded`` or
            ``failed``.
        :param finished_at: Unix epoch seconds the run reached the terminal
            state.
        :param error: Optional failure detail (only for ``failed``).
        :param error_code: Optional short failure classification (only for
            ``failed``), e.g. ``"incomplete"``.
        :returns: The updated :class:`ScheduledTaskRun` if a ``running`` run
            was transitioned; ``None`` if no matching ``running`` run existed
            (not found, or already terminal).
        """
        ...

    @abstractmethod
    def get_running_run_by_conversation(self, conversation_id: str) -> ScheduledTaskRun | None:
        """
        Return the ``running`` run for a conversation, or ``None``.

        The event-driven completion hook (fired when a conversation's turn
        reaches a terminal state) uses this reverse lookup to find the
        scheduled-task run to transition. Workspace-scoped like every other
        store read (filters on ``current_workspace_id()``), so the caller must
        run it inside the run's ``workspace_scope``. Backed by the
        ``ix_scheduled_task_runs_conversation_id`` index on
        ``(workspace_id, conversation_id)``.

        A conversation maps to at most one ``running`` run (a fire creates one
        run per conversation), so this returns a single row rather than a list.

        :param conversation_id: The fired conversation to look up.
        :returns: The matching ``running`` :class:`ScheduledTaskRun`, or
            ``None`` if the conversation has no run, or its run is already
            terminal.
        """
        ...

    @abstractmethod
    def list_running_runs_for_tasks(self, scheduled_task_ids: list[str]) -> list[ScheduledTaskRun]:
        """
        List ``running`` runs for the given tasks in the current workspace.

        Powers the lazy-on-read stale backstop on the scheduled-task LIST
        endpoint: the route resolves the owner's tasks, then this returns their
        still-``running`` runs so the route can force-fail the ones past the max
        age. Workspace-scoped (filters on ``current_workspace_id()``) like every
        other read; an empty id list returns an empty list.

        :param scheduled_task_ids: Task ids (already owner-scoped by the caller).
        :returns: ``running`` :class:`ScheduledTaskRun` instances for those
            tasks, ordered ``scheduled_at DESC, id DESC``.
        """
        ...
