"""SQLAlchemy-backed schedule store."""

from __future__ import annotations

from sqlalchemy import asc, select

from omnigent.db.db_models import SqlSchedule
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities.schedule import Schedule
from omnigent.stores.schedule_store import ScheduleStore


def _to_entity(row: SqlSchedule) -> Schedule:
    """
    Convert a :class:`SqlSchedule` ORM row to a :class:`Schedule` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Schedule` dataclass instance.
    """
    return Schedule(
        id=row.id,
        conversation_id=row.conversation_id,
        name=row.name,
        kind=row.kind,
        prompt=row.prompt,
        enabled=bool(row.enabled),
        status=row.status,
        created_at=row.created_at,
        cron=row.cron,
        command=row.command,
        created_by_user_id=row.created_by_user_id,
        last_fired_at=row.last_fired_at,
        last_run_id=row.last_run_id,
        updated_at=row.updated_at,
    )


class SqlAlchemyScheduleStore(ScheduleStore):
    """SQLAlchemy-backed implementation of :class:`ScheduleStore`."""

    def __init__(self, storage_location: str) -> None:
        """
        Create or reuse a SQLAlchemy engine + session factory.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def create(
        self,
        schedule_id: str,
        conversation_id: str,
        name: str,
        kind: str,
        prompt: str,
        *,
        cron: str | None = None,
        command: str | None = None,
        enabled: bool = True,
        created_by_user_id: str | None = None,
    ) -> Schedule:
        """Insert a new schedule.

        Raises ``IntegrityError`` on a ``(conversation_id, name)`` collision.
        """
        row = SqlSchedule(
            id=schedule_id,
            conversation_id=conversation_id,
            name=name,
            kind=kind,
            prompt=prompt,
            cron=cron,
            command=command,
            enabled=enabled,
            status="idle",
            created_by_user_id=created_by_user_id,
            created_at=now_epoch(),
            updated_at=None,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get(self, schedule_id: str) -> Schedule | None:
        """Return the schedule by id, or ``None``."""
        with self._session() as session:
            row = session.get(SqlSchedule, schedule_id)
            return _to_entity(row) if row is not None else None

    def list_for_conversation(self, conversation_id: str) -> list[Schedule]:
        """List a conversation's schedules ordered ``created_at ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlSchedule)
                .where(SqlSchedule.conversation_id == conversation_id)
                .order_by(asc(SqlSchedule.created_at), asc(SqlSchedule.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_enabled(self) -> list[Schedule]:
        """List all enabled schedules ordered ``created_at ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlSchedule)
                .where(SqlSchedule.enabled)
                .order_by(asc(SqlSchedule.created_at), asc(SqlSchedule.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        schedule_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        cron: str | None = None,
        command: str | None = None,
        enabled: bool | None = None,
        status: str | None = None,
        last_fired_at: int | None = None,
        last_run_id: str | None = None,
    ) -> Schedule | None:
        """Update mutable fields. Returns ``None`` if not found."""
        with self._session() as session:
            row = session.get(SqlSchedule, schedule_id)
            if row is None:
                return None
            changed = False
            for field, value in (
                ("name", name),
                ("prompt", prompt),
                ("cron", cron),
                ("command", command),
                ("enabled", enabled),
                ("status", status),
                ("last_fired_at", last_fired_at),
                ("last_run_id", last_run_id),
            ):
                if value is not None and getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
            if changed:
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete(self, schedule_id: str) -> bool:
        """Delete a schedule. Idempotent: returns ``False`` if not found."""
        with self._session() as session:
            row = session.get(SqlSchedule, schedule_id)
            if row is None:
                return False
            session.delete(row)
            return True
