"""SQLAlchemy-backed scheduled-task store."""

from __future__ import annotations

from typing import Any

from sqlalchemy import asc, delete, desc, select

from omnigent.db.db_models import (
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
        owner_user_id=row.owner_user_id,
        agent_id=row.agent_id,
        timezone=row.timezone,
        created_at=row.created_at,
        cron_expression=row.cron_expression,
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
        cron_expression: str,
        owner_user_id: str | None,
        agent_id: str,
        timezone: str,
        *,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
        base_branch: str | None = None,
        execution_target: str = "connected_host",
        host_id: str | None = None,
        state: str = "active",
    ) -> ScheduledTask:
        """Insert a new scheduled task with a required recurring ``cron_expression``."""
        row = SqlScheduledTask(
            id=scheduled_task_id,
            name=name,
            prompt=prompt,
            cron_expression=cron_expression,
            owner_user_id=owner_user_id,
            agent_id=agent_id,
            timezone=timezone,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            workspace=workspace,
            base_branch=base_branch,
            execution_target=encode_scheduled_task_execution_target(execution_target),
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

    def list(self) -> list[ScheduledTask]:
        """List all scheduled tasks ordered by ``created_at ASC, id ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlScheduledTask)
                .where(SqlScheduledTask.workspace_id == current_workspace_id())
                .order_by(asc(SqlScheduledTask.created_at), asc(SqlScheduledTask.id))
            )
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

    def update(
        self,
        scheduled_task_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        cron_expression: str | None = None,
        timezone: str | None = None,
        model_override: str | None = None,
        reasoning_effort: str | None = None,
        workspace: str | None = None,
        base_branch: str | None = None,
        execution_target: str | None = None,
        host_id: str | None = _UNSET,
        state: str | None = None,
        last_run_at: int | None = None,
        last_run_conversation_id: str | None = _UNSET,
    ) -> ScheduledTask | None:
        """Update mutable fields.

        ``None`` leaves most fields unchanged. For ``host_id`` and
        ``last_run_conversation_id``, the sentinel default means "not provided
        / leave unchanged"; passing ``None`` explicitly sets the column to NULL.
        Passing ``cron_expression`` updates the recurring trigger; ``None``
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
            if cron_expression is not None and row.cron_expression != cron_expression:
                row.cron_expression = cron_expression
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
            if base_branch is not None and row.base_branch != base_branch:
                row.base_branch = base_branch
                changed = True
            if execution_target is not None:
                encoded_target = encode_scheduled_task_execution_target(execution_target)
                if row.execution_target != encoded_target:
                    row.execution_target = encoded_target
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

    def list_runs(self, scheduled_task_id: str) -> list[ScheduledTaskRun]:
        """List a task's runs ordered by ``scheduled_at DESC, id DESC``."""
        with self._session() as session:
            stmt = (
                select(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.workspace_id == current_workspace_id())
                .where(SqlScheduledTaskRun.scheduled_task_id == scheduled_task_id)
                .order_by(desc(SqlScheduledTaskRun.scheduled_at), desc(SqlScheduledTaskRun.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_run_to_entity(r) for r in rows]
