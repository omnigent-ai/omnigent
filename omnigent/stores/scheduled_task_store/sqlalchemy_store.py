"""SQLAlchemy-backed scheduled-task store."""

from __future__ import annotations

import builtins
import logging
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any

from sqlalchemy import and_, asc, desc, func, or_, select, tuple_, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlScheduledTask,
    SqlScheduledTaskRun,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.enum_codecs import (
    SCHEDULED_TASK_STATE,
    decode_scheduled_task_execution_target,
    decode_scheduled_task_run_status,
    decode_scheduled_task_state,
    encode_scheduled_task_execution_target,
    encode_scheduled_task_run_status,
    encode_scheduled_task_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import ScheduledTask, ScheduledTaskRun
from omnigent.server.scheduled.run_reconciler import (
    STALE_RUN_ERROR_CODE,
    STALE_RUN_MAX_AGE_SECONDS,
)
from omnigent.server.scheduled.schedule import next_scheduled_time, scheduled_trigger_key
from omnigent.stores.scheduled_task_store import ScheduledTaskStore
from omnigent.stores.scheduled_task_store.dispatch import (
    DeletedTaskCursor,
    DispatchCursor,
    TaskCursor,
)

_logger = logging.getLogger(__name__)

# Sentinel meaning "caller did not supply this argument; leave the column unchanged."
# Distinct from None, which means "set the column to NULL."
_UNSET: Any = object()

_UNFINISHED_CLAIM_STATUSES = {
    encode_scheduled_task_run_status(status) for status in ("starting", "running")
}


def _purge_finished_deleted_task(
    session: Session, task: SqlScheduledTask | None, run: SqlScheduledTaskRun
) -> None:
    """Remove retained records after completion; the caller holds the task lock."""
    if (
        task is None
        or task.state != encode_scheduled_task_state("deleted")
        or run.trigger_key is None
    ):
        return
    if run.status not in {
        encode_scheduled_task_run_status(status) for status in ("succeeded", "failed", "skipped")
    }:
        return
    session.delete(run)
    remaining = session.scalar(
        select(SqlScheduledTaskRun.id)
        .where(
            SqlScheduledTaskRun.workspace_id == task.workspace_id,
            SqlScheduledTaskRun.scheduled_task_id == task.id,
            SqlScheduledTaskRun.id != run.id,
        )
        .limit(1)
        .with_for_update()
    )
    if remaining is None:
        session.delete(task)


def _to_entity(row: SqlScheduledTask) -> ScheduledTask:
    """
    Convert a :class:`SqlScheduledTask` ORM row to a :class:`ScheduledTask`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ScheduledTask` dataclass instance.
    """
    return ScheduledTask(
        id=row.id,
        name=row.name,
        prompt=row.prompt,
        user_id=row.user_id,
        account_generation=row.account_generation,
        agent_id=row.agent_id,
        timezone=row.timezone,
        created_at=row.created_at,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        rrule=row.rrule,
        model_override=row.model_override,
        reasoning_effort=row.reasoning_effort,
        permission_mode=row.permission_mode,
        max_cost_usd=row.max_cost_usd,
        workspace=row.workspace,
        base_branch=row.base_branch,
        execution_target=decode_scheduled_task_execution_target(row.execution_target),
        host_id=row.host_id,
        state=decode_scheduled_task_state(row.state),
        last_run_at=row.last_run_at,
        last_run_conversation_id=row.last_run_conversation_id,
        updated_at=row.updated_at,
        next_run_at=row.next_run_at,
        project_id=row.project_id,
        delegation_ref=row.delegation_ref,
    )


def _run_to_entity(row: SqlScheduledTaskRun) -> ScheduledTaskRun:
    """
    Convert a :class:`SqlScheduledTaskRun` ORM row to a :class:`ScheduledTaskRun`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`ScheduledTaskRun` dataclass instance.
    """
    return ScheduledTaskRun(
        id=row.id,
        scheduled_task_id=row.scheduled_task_id,
        status=decode_scheduled_task_run_status(row.status),
        scheduled_at=row.scheduled_at,
        conversation_id=row.conversation_id,
        fired_at=row.fired_at,
        finished_at=row.finished_at,
        error=row.error,
        error_code=row.error_code,
        workspace_id=row.workspace_id,
        trigger_key=row.trigger_key,
        host_id=row.host_id,
        lease_expires_at=row.lease_expires_at,
    )


class SqlAlchemyScheduledTaskStore(ScheduledTaskStore):
    """
    SQLAlchemy-backed implementation of :class:`ScheduledTaskStore`.

    Persists scheduled tasks and their run history in a relational database via
    the SQLAlchemy ORM.
    """

    # Keyset page size for the scheduler-boot full scan. An instance attribute
    # so tests can shrink it to exercise multi-page pagination cheaply.
    _active_boot_batch_size = 10_000
    _cleanup_batch_size = 100

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy scheduled-task store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._dispatch_enabled = False
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.scheduled_task_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.scheduled_task_store",
            immediate=True,
        )

    # ── Scheduled tasks ──────────────────────────────────────────

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
        permission_mode: str | None = None,
        max_cost_usd: float | None = None,
        workspace: str | None = None,
        host_id: str | None = None,
        execution_target: str = "connected_host",
        state: str = "active",
    ) -> ScheduledTask:
        """Insert a new scheduled task with a required recurring ``rrule``."""
        created_at = now_epoch()

        def write(session: Session) -> ScheduledTask:
            generation = require_active_account(session, user_id)
            row = SqlScheduledTask(
                account_generation=generation,
                id=scheduled_task_id,
                name=name,
                prompt=prompt,
                rrule=rrule,
                user_id=user_id,
                agent_id=agent_id,
                timezone=timezone,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                permission_mode=permission_mode,
                max_cost_usd=max_cost_usd,
                workspace=workspace,
                base_branch=None,
                execution_target=encode_scheduled_task_execution_target(execution_target),
                host_id=host_id,
                state=encode_scheduled_task_state(state),
                last_run_at=None,
                last_run_conversation_id=None,
                created_at=created_at,
                updated_at=None,
            )
            if self._dispatch_enabled:
                row.next_run_at = next_scheduled_time(_to_entity(row), created_at)
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_task", write)

    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        """Return a scheduled task by id, or ``None`` if not found."""
        with self._session("select_task_by_id") as session:
            row = session.get(SqlScheduledTask, (current_workspace_id(), scheduled_task_id))
            if row is None:
                return None
            return _to_entity(row)

    def list(self, *, owner_user_id: str | None = None) -> list[ScheduledTask]:
        """List non-deleted scheduled tasks ordered by ``created_at ASC, id ASC``.

        When *owner_user_id* is given, only tasks owned by that user are returned.
        """
        with self._session("list_tasks") as session:
            stmt = (
                select(SqlScheduledTask)
                .where(SqlScheduledTask.workspace_id == current_workspace_id())
                .where(SqlScheduledTask.state != encode_scheduled_task_state("deleted"))
                .order_by(asc(SqlScheduledTask.created_at), asc(SqlScheduledTask.id))
            )
            if owner_user_id is not None:
                stmt = stmt.where(SqlScheduledTask.user_id == owner_user_id)
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_active(self) -> builtins.list[ScheduledTask]:
        """List active scheduled tasks ordered by ``created_at ASC, id ASC``."""
        with self._session("list_active_tasks") as session:
            stmt = (
                select(SqlScheduledTask)
                .where(SqlScheduledTask.workspace_id == current_workspace_id())
                .where(SqlScheduledTask.state == encode_scheduled_task_state("active"))
                .order_by(asc(SqlScheduledTask.created_at), asc(SqlScheduledTask.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_active_all_workspaces(self) -> builtins.list[ScheduledTask]:
        """List active scheduled tasks across every workspace for scheduler boot.

        Pages internally by ``(workspace_id, created_at, id)`` keyset in
        batches so the scheduler arms *every* active task — no silent cap that
        would leave tasks beyond a fixed limit un-armed and never firing.
        """
        batch_size = self._active_boot_batch_size
        active_code = encode_scheduled_task_state("active")
        tasks: list[ScheduledTask] = []
        cursor: tuple[int, int, str] | None = None
        with self._session("list_active_tasks_for_all_workspaces") as session:
            while True:
                stmt = (
                    select(SqlScheduledTask)
                    .where(SqlScheduledTask.state == active_code)
                    .order_by(
                        asc(SqlScheduledTask.workspace_id),
                        asc(SqlScheduledTask.created_at),
                        asc(SqlScheduledTask.id),
                    )
                    .limit(batch_size)
                )
                if cursor is not None:
                    ws, created, tid = cursor
                    stmt = stmt.where(
                        tuple_(
                            SqlScheduledTask.workspace_id,
                            SqlScheduledTask.created_at,
                            SqlScheduledTask.id,
                        )
                        > (ws, created, tid)
                    )
                rows = session.execute(stmt).scalars().all()
                if not rows:
                    break
                tasks.extend(_to_entity(r) for r in rows)
                if len(rows) < batch_size:
                    break
                last = rows[-1]
                cursor = (last.workspace_id, last.created_at, last.id)
        return tasks

    def update(
        self,
        scheduled_task_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        rrule: str | None = None,
        agent_id: str | None = None,
        timezone: str | None = None,
        model_override: str | None = _UNSET,
        reasoning_effort: str | None = _UNSET,
        permission_mode: str | None = _UNSET,
        max_cost_usd: float | None = _UNSET,
        workspace: str | None = _UNSET,
        host_id: str | None = _UNSET,
        execution_target: str | None = None,
        state: str | None = None,
        last_run_at: int | None = None,
        last_run_conversation_id: str | None = _UNSET,
    ) -> ScheduledTask | None:
        """Update mutable fields.

        ``None`` leaves most fields unchanged. For the per-task overrides
        (``model_override``, ``reasoning_effort``, ``permission_mode``),
        ``workspace``, ``host_id``, ``max_cost_usd``, and
        ``last_run_conversation_id``, the sentinel default means "not provided /
        leave unchanged"; passing ``None`` explicitly sets the column to NULL —
        so resetting an override to the agent default actually clears it (a set
        ``bypassPermissions`` can be turned back off), and clearing both
        ``host_id`` and ``workspace`` unpins a task (e.g. switching it to
        managed-sandbox execution). Passing ``rrule`` updates the recurring
        trigger and ``agent_id`` rebinds the task to a different agent
        (switching the harness future firings run); ``None`` leaves either
        unchanged.
        """
        updated_at = now_epoch()

        def write(session: Session) -> ScheduledTask | None:
            row = session.get(
                SqlScheduledTask, (current_workspace_id(), scheduled_task_id), with_for_update=True
            )
            if row is None or row.state == encode_scheduled_task_state("deleted"):
                return None
            if state is not None and state != "deleted":
                require_active_account(session, row.user_id, generation=row.account_generation)
            schedule_before = (row.rrule, row.timezone, row.state)
            changed = False
            if name is not None and row.name != name:
                row.name = name
                changed = True
            if prompt is not None and row.prompt != prompt:
                row.prompt = prompt
                changed = True
            if rrule is not None and row.rrule != rrule:
                row.rrule = rrule
                changed = True
            if agent_id is not None and row.agent_id != agent_id:
                row.agent_id = agent_id
                changed = True
            if timezone is not None and row.timezone != timezone:
                row.timezone = timezone
                changed = True
            if model_override is not _UNSET and row.model_override != model_override:
                row.model_override = model_override
                changed = True
            if reasoning_effort is not _UNSET and row.reasoning_effort != reasoning_effort:
                row.reasoning_effort = reasoning_effort
                changed = True
            if permission_mode is not _UNSET and row.permission_mode != permission_mode:
                row.permission_mode = permission_mode
                changed = True
            if max_cost_usd is not _UNSET and row.max_cost_usd != max_cost_usd:
                row.max_cost_usd = max_cost_usd
                changed = True
            if workspace is not _UNSET and row.workspace != workspace:
                row.workspace = workspace
                changed = True
            if host_id is not _UNSET and row.host_id != host_id:
                row.host_id = host_id
                changed = True
            if execution_target is not None:
                encoded_target = encode_scheduled_task_execution_target(execution_target)
                if row.execution_target != encoded_target:
                    row.execution_target = encoded_target
                    changed = True
            if state is not None:
                encoded_state = encode_scheduled_task_state(state)
                if row.state != encoded_state:
                    row.state = encoded_state
                    changed = True
            if last_run_at is not None and row.last_run_at != last_run_at:
                row.last_run_at = last_run_at
                changed = True
            if last_run_conversation_id is not _UNSET and (
                row.last_run_conversation_id != last_run_conversation_id
            ):
                row.last_run_conversation_id = last_run_conversation_id
                changed = True
            if (row.rrule, row.timezone, row.state) != schedule_before:
                row.next_run_at = (
                    next_scheduled_time(_to_entity(row), updated_at)
                    if self._dispatch_enabled
                    else None
                )
            if changed:
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_task", write)

    def delete(self, scheduled_task_id: str) -> bool:
        """Hide the task, then remove history in bounded transactions, preserving live claims."""

        def write(session: Session) -> bool:
            row = session.get(
                SqlScheduledTask, (current_workspace_id(), scheduled_task_id), with_for_update=True
            )
            if row is None or row.state == encode_scheduled_task_state("deleted"):
                return False
            row.state = encode_scheduled_task_state("deleted")
            row.next_run_at = None
            row.updated_at = now_epoch()
            return True

        removed = run_write_transaction(self._session_immediate, "delete_task", write)
        cursor = None
        try:
            while True:
                cursor = self._purge_deleted_task_page(scheduled_task_id, after=cursor)
                if cursor is None:
                    break
        except Exception:
            # Deletion already committed; cleanup can be retried explicitly.
            # Automatic retries require an enabled persistent-scheduler sweep.
            _logger.exception("History cleanup deferred for deleted task %s", scheduled_task_id)
        return removed

    def _purge_deleted_task_page(
        self, task_id: str, *, after: tuple[int, str] | None = None
    ) -> tuple[int, str] | None:
        def write(session: Session) -> tuple[int, str] | None:
            task = session.get(
                SqlScheduledTask, (current_workspace_id(), task_id), with_for_update=True
            )
            if task is None or task.state != encode_scheduled_task_state("deleted"):
                return None
            columns = (SqlScheduledTaskRun.scheduled_at, SqlScheduledTaskRun.id)
            stmt = (
                select(SqlScheduledTaskRun)
                .where(
                    SqlScheduledTaskRun.workspace_id == current_workspace_id(),
                    SqlScheduledTaskRun.scheduled_task_id == task_id,
                )
                .order_by(*columns)
                .limit(self._cleanup_batch_size)
                .with_for_update()
            )
            if after is not None:
                stmt = stmt.where(self._cursor_after(columns, after))
            runs = list(session.scalars(stmt))
            if not runs:
                remaining = session.scalar(
                    select(SqlScheduledTaskRun.id)
                    .where(
                        SqlScheduledTaskRun.workspace_id == current_workspace_id(),
                        SqlScheduledTaskRun.scheduled_task_id == task_id,
                    )
                    .limit(1)
                )
                if remaining is None:
                    session.delete(task)
                return None
            cursor = (runs[-1].scheduled_at, runs[-1].id)
            timestamp = now_epoch()
            stale_before = timestamp - STALE_RUN_MAX_AGE_SECONDS
            for run in runs:
                age_from = run.fired_at if run.fired_at is not None else run.scheduled_at
                stale_running = (
                    run.status == encode_scheduled_task_run_status("running")
                    and age_from <= stale_before
                )
                if run.trigger_key is not None and stale_running:
                    # Apply the read-time backstop under the row lock; the next
                    # sweep removes the failed record like any other finished run.
                    run.status = encode_scheduled_task_run_status("failed")
                    run.finished_at = timestamp
                    run.error_code = STALE_RUN_ERROR_CODE
                    run.error = (
                        "scheduled run did not reach a terminal state within "
                        f"{STALE_RUN_MAX_AGE_SECONDS}s"
                    )
                elif run.trigger_key is None or run.status not in _UNFINISHED_CLAIM_STATUSES:
                    session.delete(run)
            return cursor

        return run_write_transaction(self._session_immediate, "purge_deleted_task_page", write)

    def purge_deleted_tasks(
        self, *, after: DeletedTaskCursor | None = None, limit: int = 100
    ) -> DeletedTaskCursor | None:
        """Retry one page of interrupted deletions; retain unfinished runs for recovery."""
        if limit < 1:
            raise ValueError("The deleted-task cleanup page size must be at least 1.")
        with self._session("discover_deleted_tasks") as session:
            columns = (SqlScheduledTask.workspace_id, SqlScheduledTask.id)
            stmt = (
                select(*columns)
                .where(
                    SqlScheduledTask.state == encode_scheduled_task_state("deleted"),
                    SqlScheduledTask.next_run_at.is_(None),
                )
                .order_by(
                    *(
                        columns
                        if self._engine.dialect.name == "mysql"
                        else (SqlScheduledTask.next_run_at, *columns)
                    )
                )
                .limit(limit)
            )
            if after is not None:
                task_key = after[:2]
                stmt = stmt.where(
                    self._cursor_after(columns, task_key, inclusive=after[2] is not None)
                )
            tasks = list(session.execute(stmt).tuples())
        for workspace_id, task_id in tasks:
            try:
                with workspace_scope(workspace_id):
                    run_cursor = (
                        after[2]
                        if after is not None and after[:2] == (workspace_id, task_id)
                        else None
                    )
                    next_run_cursor = self._purge_deleted_task_page(task_id, after=run_cursor)
                if next_run_cursor is not None:
                    return (workspace_id, task_id, next_run_cursor)
            except Exception:
                _logger.exception(
                    "Could not clean up deleted task %s in workspace %s; "
                    "continuing with other tasks and retrying on the next scan",
                    task_id,
                    workspace_id,
                )
        return (tasks[-1][0], tasks[-1][1], None) if len(tasks) == limit else None

    # ── Runs ─────────────────────────────────────────────────────

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
        """Insert a new scheduled-task-run row."""

        def write(session: Session) -> ScheduledTaskRun:
            row = SqlScheduledTaskRun(
                id=run_id,
                scheduled_task_id=scheduled_task_id,
                status=encode_scheduled_task_run_status(status),
                scheduled_at=scheduled_at,
                conversation_id=conversation_id,
                fired_at=fired_at,
                finished_at=finished_at,
                error=error,
                error_code=error_code,
            )
            session.add(row)
            session.flush()
            return _run_to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_task_run", write)

    def list_runs(
        self,
        scheduled_task_id: str,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> tuple[builtins.list[ScheduledTaskRun], str | None]:
        """List a task's runs ordered by ``scheduled_at DESC, id DESC``.

        Cursor-paginated: fetches ``limit`` runs and returns
        ``(runs, next_cursor)`` where ``next_cursor`` is the id of the last
        returned run when a further page exists, else ``None``. Run ids are
        random UUIDs (not monotonic), so the keyset resolves the cursor row's
        ``scheduled_at`` and compares the full ``(scheduled_at, id)`` tuple —
        an id-only keyset would be incorrect under the compound DESC order.

        :param scheduled_task_id: The task whose runs to return.
        :param limit: Maximum number of runs per page.
        :param after_id: Return runs ordered after this run id (exclusive).
        :returns: ``(runs, next_cursor)``.
        """
        with self._session("list_task_runs") as session:
            stmt = (
                select(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.scheduled_task_id == scheduled_task_id)
                .order_by(desc(SqlScheduledTaskRun.scheduled_at), desc(SqlScheduledTaskRun.id))
            )
            if after_id is not None:
                cursor_scheduled_at = (
                    select(SqlScheduledTaskRun.scheduled_at)
                    .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                    .where(SqlScheduledTaskRun.scheduled_task_id == scheduled_task_id)
                    .where(SqlScheduledTaskRun.id == after_id)
                    .scalar_subquery()
                )
                # DESC order: the next page holds rows whose (scheduled_at, id)
                # sorts strictly *after* the cursor, i.e. is strictly smaller.
                stmt = stmt.where(
                    or_(
                        SqlScheduledTaskRun.scheduled_at < cursor_scheduled_at,
                        and_(
                            SqlScheduledTaskRun.scheduled_at == cursor_scheduled_at,
                            SqlScheduledTaskRun.id < after_id,
                        ),
                    )
                )
            rows = session.execute(stmt.limit(limit + 1)).scalars().all()
            has_more = len(rows) > limit
            page = list(rows[:limit])
            next_cursor = page[-1].id if has_more else None
            return [_run_to_entity(r) for r in page], next_cursor

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: int,
        error: str | None = None,
        error_code: str | None = None,
    ) -> ScheduledTaskRun | None:
        """Transition a still-``running`` run to a terminal status.

        Conditional on the current status being ``running`` so an
        already-terminal run is never clobbered and concurrent sweeps cannot
        double-transition (see the interface docstring).
        """
        running_code = encode_scheduled_task_run_status("running")

        def write(session: Session) -> ScheduledTaskRun | None:
            task_id = session.scalar(
                select(SqlScheduledTaskRun.scheduled_task_id).where(
                    SqlScheduledTaskRun.workspace_id == current_workspace_id(),
                    SqlScheduledTaskRun.id == run_id,
                )
            )
            if task_id is None:
                return None
            # Match deletion's lock order: task first, then its run.
            task = session.get(
                SqlScheduledTask, (current_workspace_id(), task_id), with_for_update=True
            )
            row = session.get(
                SqlScheduledTaskRun, (current_workspace_id(), run_id), with_for_update=True
            )
            if row is None or row.status != running_code:
                return None
            row.status = encode_scheduled_task_run_status(status)
            row.finished_at = finished_at
            row.error = error
            row.error_code = error_code
            result = _run_to_entity(row)
            _purge_finished_deleted_task(session, task, row)
            return result

        return run_write_transaction(self._session_immediate, "update_task_run", write)

    def get_running_run_by_conversation(self, conversation_id: str) -> ScheduledTaskRun | None:
        """Return the ``running`` run for a conversation, or ``None``.

        Workspace-scoped reverse lookup for the event-driven completion hook;
        backed by ``ix_scheduled_task_runs_conversation_id``. A conversation has
        at most one ``running`` run, so ``.first()`` is exact rather than lossy.
        """
        running_code = encode_scheduled_task_run_status("running")
        with self._session("select_running_run_by_conversation") as session:
            stmt = (
                select(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.conversation_id == conversation_id)
                .where(SqlScheduledTaskRun.status == running_code)
            )
            row = session.execute(stmt).scalars().first()
            return _run_to_entity(row) if row is not None else None

    def list_running_runs_for_tasks(
        self,
        scheduled_task_ids: builtins.list[str],
    ) -> builtins.list[ScheduledTaskRun]:
        """List ``running`` runs for the given tasks in the current workspace.

        Powers the lazy-on-read stale backstop on the scheduled-task LIST
        endpoint: the route resolves the owner's tasks, then this returns their
        still-``running`` runs (one indexed, workspace-scoped query over the
        ``scheduled_task_id`` index) so the route can force-fail the stale ones.
        An empty ``scheduled_task_ids`` returns an empty list without a query.

        :param scheduled_task_ids: Task ids (already owner-scoped by the caller).
        :returns: ``running`` runs for those tasks, ordered
            ``scheduled_at DESC, id DESC``.
        """
        if not scheduled_task_ids:
            return []
        running_code = encode_scheduled_task_run_status("running")
        with self._session("list_running_runs_for_tasks") as session:
            stmt = (
                select(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.scheduled_task_id.in_(scheduled_task_ids))
                .where(SqlScheduledTaskRun.status == running_code)
                .order_by(desc(SqlScheduledTaskRun.scheduled_at), desc(SqlScheduledTaskRun.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_run_to_entity(r) for r in rows]

    def list_latest_run_status_for_tasks(
        self,
        scheduled_task_ids: builtins.list[str],
    ) -> dict[str, str]:
        """Return ``{task_id: latest_run_status}`` for the given tasks.

        One windowed query: ``row_number()`` partitioned by ``scheduled_task_id``
        and ordered ``scheduled_at DESC, id DESC`` (the same order as
        :meth:`list_runs`) picks each task's single most-recent run, decoded to
        its status name. Tasks with no runs are absent from the map. Both SQLite
        (>= 3.25) and PostgreSQL support window functions, matching the
        conversation store's ``row_number()`` usage.
        """
        if not scheduled_task_ids:
            return {}
        with self._session("list_latest_run_status_for_tasks") as session:
            ranked = (
                select(
                    SqlScheduledTaskRun.scheduled_task_id.label("task_id"),
                    SqlScheduledTaskRun.status.label("status"),
                    func.row_number()
                    .over(
                        partition_by=SqlScheduledTaskRun.scheduled_task_id,
                        order_by=(
                            desc(SqlScheduledTaskRun.scheduled_at),
                            desc(SqlScheduledTaskRun.id),
                        ),
                    )
                    .label("row_num"),
                )
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.scheduled_task_id.in_(scheduled_task_ids))
                .subquery()
            )
            stmt = select(ranked.c.task_id, ranked.c.status).where(ranked.c.row_num == 1)
            rows = session.execute(stmt).all()
            return {task_id: decode_scheduled_task_run_status(status) for task_id, status in rows}

    # ── Database scheduler ───────────────────────────────────────

    def _cursor_after(
        self, columns: tuple[Any, ...], cursor: tuple[Any, ...], *, inclusive: bool = False
    ) -> ColumnElement[bool]:
        """MySQL needs scalar ranges to seek past a composite cursor."""
        if self._engine.dialect.name != "mysql":
            return tuple_(*columns) >= cursor if inclusive else tuple_(*columns) > cursor
        return or_(
            *(
                and_(
                    *(columns[previous] == cursor[previous] for previous in range(index)),
                    column >= cursor[index]
                    if inclusive and index == len(columns) - 1
                    else column > cursor[index],
                )
                for index, column in enumerate(columns)
            )
        )

    def configure_dispatch(self, enabled: bool) -> None:
        """Select cursor maintenance before serving requests, using the app's release flag."""
        self._dispatch_enabled = enabled

    def reset_dispatch_cursors(self) -> None:
        """Clear saved schedules in bounded transactions before starting legacy timers."""

        def write(
            session: Session, *, state: int, cursor: DispatchCursor | None
        ) -> DispatchCursor | None:
            columns = (
                SqlScheduledTask.next_run_at,
                SqlScheduledTask.workspace_id,
                SqlScheduledTask.id,
            )
            stmt = (
                select(*columns)
                .where(
                    SqlScheduledTask.state == state,
                    SqlScheduledTask.next_run_at.is_not(None),
                )
                .order_by(*columns)
                .limit(self._cleanup_batch_size)
                .with_for_update()
            )
            if cursor is not None:
                stmt = stmt.where(self._cursor_after(columns, cursor))
            rows = list(session.execute(stmt).tuples())
            if not rows:
                return None
            session.execute(
                update(SqlScheduledTask)
                .where(tuple_(*columns[1:]).in_([(row[1], row[2]) for row in rows]))
                .values(next_run_at=None)
            )
            next_run_at, workspace_id, task_id = rows[-1]
            assert next_run_at is not None
            return (next_run_at, workspace_id, task_id)

        for state in SCHEDULED_TASK_STATE.values():
            cursor: DispatchCursor | None = None
            while True:
                cursor = run_write_transaction(
                    self._session_immediate,
                    "reset_dispatch_cursor_page",
                    partial(write, state=state, cursor=cursor),
                )
                if cursor is None:
                    break

    def list_uninitialized_tasks(
        self, *, after: TaskCursor | None = None, limit: int = 100
    ) -> builtins.list[ScheduledTask]:
        """One indexed page of active tasks that have no saved next run time."""
        if limit < 1:
            raise ValueError("The uninitialized-task page size must be at least 1.")
        with self._session("discover_uninitialized_tasks") as session:
            columns = (SqlScheduledTask.workspace_id, SqlScheduledTask.id)
            stmt = (
                select(SqlScheduledTask)
                .where(
                    SqlScheduledTask.state == encode_scheduled_task_state("active"),
                    SqlScheduledTask.next_run_at.is_(None),
                )
                .order_by(
                    *(
                        columns
                        if self._engine.dialect.name == "mysql"
                        else (SqlScheduledTask.next_run_at, *columns)
                    )
                )
                .limit(limit)
            )
            if after is not None:
                stmt = stmt.where(self._cursor_after(columns, after))
            return [_to_entity(row) for row in session.scalars(stmt)]

    def check_dispatch_support(self) -> None:
        """D1's REST driver auto-commits each statement and cannot roll back a claim."""
        if self._engine.dialect.name == "cloudflare_d1":
            raise NotImplementedError(
                "The persistent scheduler cannot use Cloudflare D1: its REST driver does not "
                "support the multi-statement transactions needed to create a run and advance "
                "its schedule together. Use a supported database backend, or disable "
                "automations_v2 to use the existing scheduler."
            )

    def initialize_next_run_at(self, task: ScheduledTask, next_run_at: int) -> bool:
        """Backfill an unchanged legacy task without overwriting edits or claims."""
        if task.workspace_id != current_workspace_id() or task.next_run_at is not None:
            return False

        def write(session: Session) -> bool:
            row = session.get(SqlScheduledTask, (task.workspace_id, task.id), with_for_update=True)
            if row is None or _to_entity(row) != task or task.state != "active":
                return False
            row.next_run_at = next_run_at
            return True

        return run_write_transaction(self._session_immediate, "initialize_schedule", write)

    def pause_invalid_schedule(self, task: ScheduledTask) -> bool:
        """Pause an invalid schedule only if no concurrent edit changed the snapshot."""
        if task.workspace_id != current_workspace_id() or task.state != "active":
            return False

        def write(session: Session) -> bool:
            row = session.get(SqlScheduledTask, (task.workspace_id, task.id), with_for_update=True)
            if row is None or _to_entity(row) != task:
                return False
            row.state = encode_scheduled_task_state("paused")
            row.next_run_at = None
            row.updated_at = now_epoch()
            return True

        return run_write_transaction(self._session_immediate, "pause_invalid_schedule", write)

    def list_due_tasks(
        self, now: int, *, after: DispatchCursor | None = None, limit: int = 100
    ) -> builtins.list[ScheduledTask]:
        """Indexed global discovery, keyset-paged past tasks owned by other replicas."""
        if limit < 1:
            raise ValueError("The page size (limit) for fetching due tasks must be at least 1.")
        with self._session("discover_due_tasks") as session:
            columns = (
                SqlScheduledTask.next_run_at,
                SqlScheduledTask.workspace_id,
                SqlScheduledTask.id,
            )
            stmt = (
                select(SqlScheduledTask)
                .where(
                    SqlScheduledTask.state == encode_scheduled_task_state("active"),
                    SqlScheduledTask.next_run_at <= now,
                )
                .order_by(*columns)
                .limit(limit)
            )
            if after is not None:
                stmt = stmt.where(self._cursor_after(columns, after))
            return [_to_entity(row) for row in session.scalars(stmt)]

    def claim_scheduled_run(
        self,
        task: ScheduledTask,
        *,
        now: int,
        next_run_at: int | None,
        host_id: str | None,
        lease_seconds: int,
    ) -> ScheduledTaskRun | None:
        """Lock/recheck the complete task snapshot, then advance and insert atomically.

        Comparing the full snapshot also catches same-second prompt/host edits;
        updated_at alone cannot serve as a revision. A uniqueness error rolls
        back the advance, as does any other write failure.
        """
        self.check_dispatch_support()
        if lease_seconds <= 0 or (next_run_at is not None and next_run_at <= now):
            raise ValueError(
                "Cannot claim a scheduled run: lease_seconds must be greater than zero, "
                "and next_run_at must be later than now or None when no future run remains."
            )
        if (
            task.workspace_id != current_workspace_id()
            or task.state != "active"
            or task.next_run_at is None
            or task.next_run_at > now
        ):
            return None
        scheduled_at = task.next_run_at
        trigger_key = scheduled_trigger_key(scheduled_at)
        run_id = uuid.uuid4().hex

        def write(session: Session) -> ScheduledTaskRun | None:
            row = session.get(SqlScheduledTask, (task.workspace_id, task.id), with_for_update=True)
            if row is None or _to_entity(row) != task:
                return None
            existing = session.scalar(
                select(SqlScheduledTaskRun.id).where(
                    SqlScheduledTaskRun.workspace_id == task.workspace_id,
                    SqlScheduledTaskRun.scheduled_task_id == task.id,
                    SqlScheduledTaskRun.trigger_key == trigger_key,
                )
            )
            row.next_run_at = next_run_at
            if existing is not None:
                return None
            run = SqlScheduledTaskRun(
                workspace_id=task.workspace_id,
                id=run_id,
                scheduled_task_id=task.id,
                status=encode_scheduled_task_run_status("starting"),
                scheduled_at=scheduled_at,
                fired_at=now,
                trigger_key=trigger_key,
                host_id=host_id,
                lease_expires_at=now + lease_seconds,
            )
            session.add(run)
            return _run_to_entity(run)

        return run_write_transaction(self._session_immediate, "claim_occurrence", write)

    def renew_starting_run(
        self, run_id: str, *, now: Callable[[], float], lease_seconds: int
    ) -> ScheduledTaskRun | None:
        """Only the executor actively preparing this startup may call this method.

        Expired startups are returned unchanged: a late heartbeat must not
        revive them. Read the clock after acquiring the row lock on every attempt.
        Completed/running rows are likewise never modified.
        """
        if lease_seconds <= 0:
            raise ValueError(
                "Cannot renew a startup lease: its duration (lease_seconds) must be "
                "greater than zero."
            )

        def write(session: Session) -> ScheduledTaskRun | None:
            row = session.get(
                SqlScheduledTaskRun, (current_workspace_id(), run_id), with_for_update=True
            )
            timestamp = int(now())
            if row is None:
                return None
            if (
                row.status == encode_scheduled_task_run_status("starting")
                and row.lease_expires_at is not None
                and row.lease_expires_at > timestamp
            ):
                row.lease_expires_at = max(row.lease_expires_at, timestamp + lease_seconds)
            return _run_to_entity(row)

        return run_write_transaction(self._session_immediate, "renew_startup_lease", write)

    def list_expired_starting_runs(
        self, now: int, *, after: DispatchCursor | None = None, limit: int = 100
    ) -> builtins.list[ScheduledTaskRun]:
        """Find expired startup leases across workspaces; running sessions are excluded."""
        if limit < 1:
            raise ValueError(
                "The page size (limit) for fetching runs with expired startup leases "
                "must be at least 1."
            )
        with self._session("discover_expired_startups") as session:
            columns = (
                SqlScheduledTaskRun.lease_expires_at,
                SqlScheduledTaskRun.workspace_id,
                SqlScheduledTaskRun.id,
            )
            stmt = (
                select(SqlScheduledTaskRun)
                .where(
                    SqlScheduledTaskRun.status == encode_scheduled_task_run_status("starting"),
                    SqlScheduledTaskRun.lease_expires_at <= now,
                )
                .order_by(*columns)
                .limit(limit)
            )
            if after is not None:
                stmt = stmt.where(self._cursor_after(columns, after))
            return [_run_to_entity(row) for row in session.scalars(stmt)]

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
        """Transition only a matching startup lease."""
        if status not in {"starting", "running", "failed", "skipped"}:
            raise ValueError(
                f"Cannot update a starting run to status {status!r}. "
                "Expected 'starting', 'running', 'failed', or 'skipped'."
            )
        if status == "starting" and (expired or conversation_id is None):
            raise ValueError(
                "Keeping a run in 'starting' requires a conversation_id to attach its "
                "session and an unexpired startup lease (expired=False)."
            )
        if run.workspace_id != current_workspace_id():
            return None

        def write(session: Session) -> ScheduledTaskRun | None:
            task = session.get(
                SqlScheduledTask, (run.workspace_id, run.scheduled_task_id), with_for_update=True
            )
            row = session.get(
                SqlScheduledTaskRun, (run.workspace_id, run.id), with_for_update=True
            )
            timestamp = int(now())
            if (
                row is None
                or row.status != encode_scheduled_task_run_status("starting")
                or row.lease_expires_at is None
                or row.lease_expires_at != run.lease_expires_at
                or (row.lease_expires_at <= timestamp) != expired
            ):
                return None
            row.status = encode_scheduled_task_run_status(status)
            if status != "starting":
                row.lease_expires_at = None
            if conversation_id is not None:
                row.conversation_id = conversation_id
            row.finished_at = timestamp if status in {"failed", "skipped"} else None
            row.error = error
            row.error_code = error_code
            result = _run_to_entity(row)
            _purge_finished_deleted_task(session, task, row)
            return result

        return run_write_transaction(self._session_immediate, "finish_startup", write)
