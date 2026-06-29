"""SQLAlchemy-backed work-item store."""

from __future__ import annotations

from sqlalchemy import desc, select

from omnigent.db.db_models import SqlWorkItem
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import WorkItem
from omnigent.stores.work_item_store import WorkItemStore


def _to_entity(row: SqlWorkItem) -> WorkItem:
    """
    Convert a :class:`SqlWorkItem` ORM row to a :class:`WorkItem` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`WorkItem` dataclass instance.
    """
    return WorkItem(
        id=row.id,
        source=row.source,
        title=row.title,
        dedup_key=row.dedup_key,
        status=row.status,
        created_at=row.created_at,
        external_id=row.external_id,
        body=row.body,
        pr_url=row.pr_url,
        conversation_id=row.conversation_id,
        assignee_user_id=row.assignee_user_id,
        created_by=row.created_by,
        plan=row.plan,
        updated_at=row.updated_at,
    )


class SqlAlchemyWorkItemStore(WorkItemStore):
    """SQLAlchemy-backed implementation of :class:`WorkItemStore`."""

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
        work_item_id: str,
        source: str,
        title: str,
        *,
        dedup_key: str,
        external_id: str | None = None,
        body: str | None = None,
        status: str = "new",
        conversation_id: str | None = None,
        assignee_user_id: str | None = None,
        created_by: str | None = None,
        plan: str | None = None,
    ) -> WorkItem:
        """Insert a new work item.

        Raises ``IntegrityError`` on a ``dedup_key`` collision.
        """
        row = SqlWorkItem(
            id=work_item_id,
            source=source,
            title=title,
            dedup_key=dedup_key,
            status=status,
            external_id=external_id,
            body=body,
            conversation_id=conversation_id,
            assignee_user_id=assignee_user_id,
            created_by=created_by,
            plan=plan,
            created_at=now_epoch(),
            updated_at=None,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get(self, work_item_id: str) -> WorkItem | None:
        """Return the work item by id, or ``None``."""
        with self._session() as session:
            row = session.get(SqlWorkItem, work_item_id)
            return _to_entity(row) if row is not None else None

    def get_by_dedup_key(self, dedup_key: str) -> WorkItem | None:
        """Return the work item with this dedup key, or ``None``."""
        with self._session() as session:
            row = (
                session.execute(select(SqlWorkItem).where(SqlWorkItem.dedup_key == dedup_key))
                .scalars()
                .first()
            )
            return _to_entity(row) if row is not None else None

    def list(
        self,
        *,
        status: str | None = None,
        conversation_id: str | None = None,
        limit: int = 200,
    ) -> list[WorkItem]:
        """List work items newest-first, optionally filtered."""
        with self._session() as session:
            stmt = select(SqlWorkItem)
            if status is not None:
                stmt = stmt.where(SqlWorkItem.status == status)
            if conversation_id is not None:
                stmt = stmt.where(SqlWorkItem.conversation_id == conversation_id)
            # Newest first; tie-break on id descending for a stable order
            # when two rows share a created_at second.
            stmt = stmt.order_by(desc(SqlWorkItem.created_at), desc(SqlWorkItem.id)).limit(limit)
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        work_item_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        status: str | None = None,
        pr_url: str | None = None,
        conversation_id: str | None = None,
        assignee_user_id: str | None = None,
        plan: str | None = None,
    ) -> WorkItem | None:
        """Update mutable fields. Returns ``None`` if not found."""
        with self._session() as session:
            row = session.get(SqlWorkItem, work_item_id)
            if row is None:
                return None
            changed = False
            for field, value in (
                ("title", title),
                ("body", body),
                ("status", status),
                ("pr_url", pr_url),
                ("conversation_id", conversation_id),
                ("assignee_user_id", assignee_user_id),
                ("plan", plan),
            ):
                if value is not None and getattr(row, field) != value:
                    setattr(row, field, value)
                    changed = True
            if changed:
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete(self, work_item_id: str) -> bool:
        """Delete a work item. Idempotent: returns ``False`` if not found."""
        with self._session() as session:
            row = session.get(SqlWorkItem, work_item_id)
            if row is None:
                return False
            session.delete(row)
            return True
