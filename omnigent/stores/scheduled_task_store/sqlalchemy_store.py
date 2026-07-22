"""SQLAlchemy-backed scheduled-task store."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, asc, delete, desc, or_, select, tuple_

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlScheduledTask,
    SqlScheduledTaskRun,
    current_workspace_id,
)
from omnigent.db.enum_codecs import (
    decode_scheduled_task_execution_target,
    decode_scheduled_task_run_status,
    decode_scheduled_task_state,
    encode_scheduled_task_execution_target,
    encode_scheduled_task_run_status,
    encode_scheduled_task_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import ScheduledTask, ScheduledTaskRun
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

# Sentinel meaning "caller did not supply this argument; leave the column unchanged."
# Distinct from None, which means "set the column to NULL."
_UNSET: Any = object()


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
        agent_id=row.agent_id,
        timezone=row.timezone,
        created_at=row.created_at,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        rrule=row.rrule,
        model_override=row.model_override,
        reasoning_effort=row.reasoning_effort,
        workspace=row.workspace,
        base_branch=row.base_branch,
        execution_target=decode_scheduled_task_execution_target(row.execution_target),
        host_id=row.host_id,
        state=decode_scheduled_task_state(row.state),
        last_run_at=row.last_run_at,
        last_run_conversation_id=row.last_run_conversation_id,
        updated_at=row.updated_at,
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

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy scheduled-task store.

        Creates or reuses a SQLAlchemy engine and session factory for the
        given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

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
        workspace: str | None = None,
        host_id: str | None = None,
        state: str = "active",
    ) -> ScheduledTask:
        """Insert a new scheduled task with a required recurring ``rrule``."""
        row = SqlScheduledTask(
            id=scheduled_task_id,
            name=name,
            prompt=prompt,
            rrule=rrule,
            user_id=user_id,
            agent_id=agent_id,
            timezone=timezone,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            workspace=workspace,
            base_branch=None,
            execution_target=encode_scheduled_task_execution_target("connected_host"),
            host_id=host_id,
            state=encode_scheduled_task_state(state),
            last_run_at=None,
            last_run_conversation_id=None,
            created_at=now_epoch(),
            updated_at=None,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get(self, scheduled_task_id: str) -> ScheduledTask | None:
        """Return a scheduled task by id, or ``None`` if not found."""
        with self._session() as session:
            row = session.get(SqlScheduledTask, (current_workspace_id(), scheduled_task_id))
            if row is None:
                return None
            return _to_entity(row)

    def list(self, *, owner_user_id: str | None = None) -> list[ScheduledTask]:
        """List all scheduled tasks ordered by ``created_at ASC, id ASC``.

        When *owner_user_id* is given, only tasks owned by that user are returned.
        """
        with self._session() as session:
            stmt = (
                select(SqlScheduledTask)
                .where(SqlScheduledTask.workspace_id == current_workspace_id())
                .order_by(asc(SqlScheduledTask.created_at), asc(SqlScheduledTask.id))
            )
            if owner_user_id is not None:
                stmt = stmt.where(SqlScheduledTask.user_id == owner_user_id)
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_active(self) -> list[ScheduledTask]:
        """List active scheduled tasks ordered by ``created_at ASC, id ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlScheduledTask)
                .where(SqlScheduledTask.workspace_id == current_workspace_id())
                .where(SqlScheduledTask.state == encode_scheduled_task_state("active"))
                .order_by(asc(SqlScheduledTask.created_at), asc(SqlScheduledTask.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_active_all_workspaces(self) -> list[ScheduledTask]:
        """List active scheduled tasks across every workspace for scheduler boot.

        Pages internally by ``(workspace_id, created_at, id)`` keyset in
        batches so the scheduler arms *every* active task — no silent cap that
        would leave tasks beyond a fixed limit un-armed and never firing.
        """
        batch_size = self._active_boot_batch_size
        active_code = encode_scheduled_task_state("active")
        tasks: list[ScheduledTask] = []
        cursor: tuple[str, int, str] | None = None
        with self._session() as session:
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
        timezone: str | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
        host_id: str | None = _UNSET,
        state: str | None = None,
        last_run_at: int | None = None,
        last_run_conversation_id: str | None = _UNSET,
    ) -> ScheduledTask | None:
        """Update mutable fields.

        ``None`` leaves most fields unchanged. For ``host_id`` and
        ``last_run_conversation_id``, the sentinel default means "not provided
        / leave unchanged"; passing ``None`` explicitly sets the column to NULL.
        Passing ``rrule`` updates the recurring trigger; ``None``
        leaves it unchanged.
        """
        with self._session() as session:
            row = session.get(SqlScheduledTask, (current_workspace_id(), scheduled_task_id))
            if row is None:
                return None
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
            if timezone is not None and row.timezone != timezone:
                row.timezone = timezone
                changed = True
            if model_override is not None and row.model_override != model_override:
                row.model_override = model_override
                changed = True
            if reasoning_effort is not None and row.reasoning_effort != reasoning_effort:
                row.reasoning_effort = reasoning_effort
                changed = True
            if workspace is not None and row.workspace != workspace:
                row.workspace = workspace
                changed = True
            if host_id is not _UNSET and row.host_id != host_id:
                row.host_id = host_id
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
            if changed:
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete(self, scheduled_task_id: str) -> bool:
        """Delete a scheduled task and all of its runs. Idempotent: returns ``False`` if not
        found."""
        with self._session() as session:
            row = session.get(SqlScheduledTask, (current_workspace_id(), scheduled_task_id))
            if row is None:
                return False
            session.execute(
                delete(SqlScheduledTaskRun).where(
                    SqlScheduledTaskRun.workspace_id == current_workspace_id(),
                    SqlScheduledTaskRun.scheduled_task_id == scheduled_task_id,
                )
            )
            session.delete(row)
            return True

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
        with self._session() as session:
            session.add(row)
            session.flush()
            return _run_to_entity(row)

    def list_runs(
        self,
        scheduled_task_id: str,
        *,
        limit: int = 100,
        after_id: str | None = None,
    ) -> tuple[list[ScheduledTaskRun], str | None]:
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
        with self._session() as session:
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
        with self._session() as session:
            row = session.get(SqlScheduledTaskRun, (current_workspace_id(), run_id))
            if row is None or row.status != running_code:
                return None
            row.status = encode_scheduled_task_run_status(status)
            row.finished_at = finished_at
            row.error = error
            row.error_code = error_code
            session.flush()
            return _run_to_entity(row)

    def get_running_run_by_conversation(self, conversation_id: str) -> ScheduledTaskRun | None:
        """Return the ``running`` run for a conversation, or ``None``.

        Workspace-scoped reverse lookup for the event-driven completion hook;
        backed by ``ix_scheduled_task_runs_conversation_id``. A conversation has
        at most one ``running`` run, so ``.first()`` is exact rather than lossy.
        """
        running_code = encode_scheduled_task_run_status("running")
        with self._session() as session:
            stmt = (
                select(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.conversation_id == conversation_id)
                .where(SqlScheduledTaskRun.status == running_code)
            )
            row = session.execute(stmt).scalars().first()
            return _run_to_entity(row) if row is not None else None

    def list_running_runs_for_tasks(self, scheduled_task_ids: list[str]) -> list[ScheduledTaskRun]:
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
        with self._session() as session:
            stmt = (
                select(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.scheduled_task_id.in_(scheduled_task_ids))
                .where(SqlScheduledTaskRun.status == running_code)
                .order_by(desc(SqlScheduledTaskRun.scheduled_at), desc(SqlScheduledTaskRun.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_run_to_entity(r) for r in rows]
