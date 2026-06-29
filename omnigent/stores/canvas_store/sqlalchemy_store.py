"""SQLAlchemy-backed canvas store."""

from __future__ import annotations

from sqlalchemy import select

from omnigent.db.db_models import SqlCanvas
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities.canvas import Canvas
from omnigent.stores.canvas_store import CanvasStore


def _to_entity(row: SqlCanvas) -> Canvas:
    """Convert a :class:`SqlCanvas` ORM row to a :class:`Canvas` entity."""
    return Canvas(
        id=row.id,
        conversation_id=row.conversation_id,
        title=row.title,
        content=row.content,
        content_type=row.content_type,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyCanvasStore(CanvasStore):
    """SQLAlchemy-backed implementation of :class:`CanvasStore`."""

    def __init__(self, storage_location: str) -> None:
        """Create or reuse a SQLAlchemy engine + session factory."""
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        # immediate=True: upsert is a check-then-write, so take the SQLite
        # write lock up front to avoid a concurrent-create race.
        self._session = make_managed_session_maker(self._engine, immediate=True)

    def get_by_conversation(self, conversation_id: str) -> Canvas | None:
        """Return the conversation's canvas, or ``None``."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlCanvas).where(SqlCanvas.conversation_id == conversation_id)
                )
                .scalars()
                .first()
            )
            return _to_entity(row) if row is not None else None

    def upsert(
        self,
        canvas_id: str,
        conversation_id: str,
        title: str,
        content: str,
        content_type: str,
    ) -> Canvas:
        """Create or overwrite the conversation's canvas."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlCanvas).where(SqlCanvas.conversation_id == conversation_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                row = SqlCanvas(
                    id=canvas_id,
                    conversation_id=conversation_id,
                    title=title,
                    content=content,
                    content_type=content_type,
                    created_at=now_epoch(),
                    updated_at=None,
                )
                session.add(row)
            else:
                row.title = title
                row.content = content
                row.content_type = content_type
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete(self, conversation_id: str) -> bool:
        """Delete the conversation's canvas. Idempotent."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlCanvas).where(SqlCanvas.conversation_id == conversation_id)
                )
                .scalars()
                .first()
            )
            if row is None:
                return False
            session.delete(row)
            return True
