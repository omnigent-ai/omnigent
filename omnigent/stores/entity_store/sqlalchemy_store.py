"""SQLAlchemy-backed entity store."""

from __future__ import annotations

from sqlalchemy import desc, select

from omnigent.db.converters import sql_entity_to_entity
from omnigent.db.db_models import SqlEntity
from omnigent.db.utils import (
    generate_entity_id,
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Entity
from omnigent.stores.entity_store import EntityStore


class SqlAlchemyEntityStore(EntityStore):
    """
    SQLAlchemy-backed implementation of :class:`EntityStore`.

    Persists entities in a relational database via SQLAlchemy ORM.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy entity store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///entities.db"`` or
            ``"postgresql://user:pass@host/db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def create_entity(
        self,
        *,
        title: str,
        instruction: str,
        created_by: str | None = None,
        group_id: str | None = None,
    ) -> Entity:
        """Create a new entity. See :meth:`EntityStore.create_entity`."""
        ts = now_epoch()
        row = SqlEntity(
            id=generate_entity_id(),
            created_at=ts,
            updated_at=ts,
            title=title,
            instruction=instruction,
            created_by=created_by,
            group_id=group_id,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return sql_entity_to_entity(row)

    def get_entity(self, entity_id: str) -> Entity | None:
        """Fetch an entity by id. See :meth:`EntityStore.get_entity`."""
        with self._session() as session:
            row = session.get(SqlEntity, entity_id)
            return sql_entity_to_entity(row) if row else None

    def list_entities(self, *, created_by: str | None = None) -> list[Entity]:
        """List entities newest-updated first. See :meth:`EntityStore.list_entities`."""
        with self._session() as session:
            stmt = select(SqlEntity)
            if created_by is not None:
                stmt = stmt.where(SqlEntity.created_by == created_by)
            stmt = stmt.order_by(desc(SqlEntity.updated_at), desc(SqlEntity.id))
            rows = session.execute(stmt).scalars().all()
            return [sql_entity_to_entity(r) for r in rows]

    def update_entity(
        self,
        entity_id: str,
        *,
        title: str | None = None,
        instruction: str | None = None,
        group_id: str | None = None,
    ) -> Entity | None:
        """Patch an entity and bump ``updated_at``. See :meth:`EntityStore.update_entity`."""
        with self._session() as session:
            row = session.get(SqlEntity, entity_id)
            if not row:
                return None
            if title is not None:
                row.title = title
            if instruction is not None:
                row.instruction = instruction
            # Empty string clears the group (ungrouped); None leaves it unchanged.
            if group_id is not None:
                row.group_id = group_id or None
            row.updated_at = now_epoch()
            return sql_entity_to_entity(row)

    def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity. See :meth:`EntityStore.delete_entity`."""
        with self._session() as session:
            row = session.get(SqlEntity, entity_id)
            if not row:
                return False
            session.delete(row)
            return True
