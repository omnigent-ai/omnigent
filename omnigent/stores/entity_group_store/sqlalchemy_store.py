"""SQLAlchemy-backed entity group store."""

from __future__ import annotations

from sqlalchemy import desc, select, update

from omnigent.db.converters import sql_entity_group_to_entity_group
from omnigent.db.db_models import SqlEntity, SqlEntityGroup
from omnigent.db.utils import (
    generate_entity_group_id,
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import EntityGroup
from omnigent.stores.entity_group_store import EntityGroupStore


class SqlAlchemyEntityGroupStore(EntityGroupStore):
    """
    SQLAlchemy-backed implementation of :class:`EntityGroupStore`.

    Persists user-created entity groups in a relational database via SQLAlchemy.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy entity group store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///entities.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def create_group(
        self,
        *,
        name: str,
        icon_key: str | None = None,
        created_by: str | None = None,
    ) -> EntityGroup:
        """Create a new group. See :meth:`EntityGroupStore.create_group`."""
        ts = now_epoch()
        row = SqlEntityGroup(
            id=generate_entity_group_id(),
            created_at=ts,
            updated_at=ts,
            name=name,
            icon_key=icon_key,
            created_by=created_by,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return sql_entity_group_to_entity_group(row)

    def get_group(self, group_id: str) -> EntityGroup | None:
        """Fetch a group by id. See :meth:`EntityGroupStore.get_group`."""
        with self._session() as session:
            row = session.get(SqlEntityGroup, group_id)
            return sql_entity_group_to_entity_group(row) if row else None

    def list_groups(self, *, created_by: str | None = None) -> list[EntityGroup]:
        """List groups newest-updated first. See :meth:`EntityGroupStore.list_groups`."""
        with self._session() as session:
            stmt = select(SqlEntityGroup)
            if created_by is not None:
                stmt = stmt.where(SqlEntityGroup.created_by == created_by)
            stmt = stmt.order_by(desc(SqlEntityGroup.updated_at), desc(SqlEntityGroup.id))
            rows = session.execute(stmt).scalars().all()
            return [sql_entity_group_to_entity_group(r) for r in rows]

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        icon_key: str | None = None,
        icon_artifact_key: str | None = None,
        icon_content_type: str | None = None,
    ) -> EntityGroup | None:
        """Patch a group and bump ``updated_at``. See :meth:`EntityGroupStore.update_group`."""
        with self._session() as session:
            row = session.get(SqlEntityGroup, group_id)
            if not row:
                return None
            if name is not None:
                row.name = name
            if icon_key is not None:
                row.icon_key = icon_key
            if icon_artifact_key is not None:
                row.icon_artifact_key = icon_artifact_key
            if icon_content_type is not None:
                row.icon_content_type = icon_content_type
            row.updated_at = now_epoch()
            return sql_entity_group_to_entity_group(row)

    def delete_group(self, group_id: str) -> bool:
        """Delete a group, ungrouping its entities. See :meth:`EntityGroupStore.delete_group`."""
        with self._session() as session:
            row = session.get(SqlEntityGroup, group_id)
            if not row:
                return False
            # Null dependents so they fall back to "ungrouped" rather than
            # pointing at a deleted group (no DB FK enforces this).
            session.execute(
                update(SqlEntity)
                .where(SqlEntity.group_id == group_id)
                .values(group_id=None)
            )
            session.delete(row)
            return True
