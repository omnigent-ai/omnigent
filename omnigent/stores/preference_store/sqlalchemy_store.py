"""SQLAlchemy-backed preference store."""

from __future__ import annotations

import time

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omnigent.db.db_models import SqlUserPreference, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.entities import UserPreference
from omnigent.stores.preference_store import PreferenceStore


def _to_entity(row: SqlUserPreference) -> UserPreference:
    """Convert a :class:`SqlUserPreference` ORM row to a domain entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`UserPreference` dataclass instance.
    """
    return UserPreference(
        user_id=row.user_id,
        key=row.key,
        value=row.value,
        updated_at=row.updated_at,
    )


class SqlAlchemyPreferenceStore(PreferenceStore):
    """SQLAlchemy-backed implementation of :class:`PreferenceStore`.

    Persists per-user preferences via SQLAlchemy ORM, using dialect-aware
    upsert (SQLite / PostgreSQL ``ON CONFLICT DO UPDATE``, MySQL
    ``ON DUPLICATE KEY UPDATE``).
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy preference store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def get(self, user_id: str, key: str) -> UserPreference | None:
        """Look up a single preference. See base class for contract."""
        with self._session() as session:
            row = session.get(SqlUserPreference, (current_workspace_id(), user_id, key))
            return _to_entity(row) if row is not None else None

    def get_all(self, user_id: str) -> dict[str, str]:
        """Return every preference for a user. See base class for contract."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlUserPreference).where(
                        SqlUserPreference.workspace_id == current_workspace_id(),
                        SqlUserPreference.user_id == user_id,
                    )
                )
                .scalars()
                .all()
            )
            return {row.key: row.value for row in rows}

    def set(self, user_id: str, key: str, value: str) -> UserPreference:
        """Upsert a preference. See base class for contract."""
        updated_at = int(time.time())
        with self._session() as session:
            dialect = self._engine.dialect.name
            values = {
                "user_id": user_id,
                "key": key,
                "value": value,
                "updated_at": updated_at,
            }
            if dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlUserPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": value, "updated_at": updated_at},
                    )
                )
            elif dialect == "mysql":
                stmt = (
                    mysql_insert(SqlUserPreference)
                    .values(**values)
                    .on_duplicate_key_update(value=value, updated_at=updated_at)
                )
            else:
                stmt = (
                    pg_insert(SqlUserPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": value, "updated_at": updated_at},
                    )
                )
            session.execute(stmt)
            return UserPreference(
                user_id=user_id,
                key=key,
                value=value,
                updated_at=updated_at,
            )

    def delete(self, user_id: str, key: str) -> bool:
        """Remove a preference. See base class for contract."""
        with self._session() as session:
            result = session.execute(
                delete(SqlUserPreference).where(
                    SqlUserPreference.workspace_id == current_workspace_id(),
                    SqlUserPreference.user_id == user_id,
                    SqlUserPreference.key == key,
                )
            )
            return result.rowcount > 0
