"""SQLAlchemy-backed host permission store."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omnigent.db.db_models import SqlHostPermission
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.entities import HostPermission
from omnigent.stores.host_permission_store import HostPermissionStore


def _to_entity(row: SqlHostPermission) -> HostPermission:
    """Convert a :class:`SqlHostPermission` ORM row to a domain entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`HostPermission` dataclass instance.
    """
    return HostPermission(
        user_id=row.user_id,
        host_id=row.host_id,
        level=row.level,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


class SqlAlchemyHostPermissionStore(HostPermissionStore):
    """SQLAlchemy-backed implementation of :class:`HostPermissionStore`.

    Persists host permissions in a relational database via SQLAlchemy
    ORM. Uses dialect-aware upsert for grants (SQLite
    ``ON CONFLICT DO UPDATE``, PostgreSQL ``ON CONFLICT ... DO UPDATE``),
    preserving ``created_at`` / ``created_by`` on conflict.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy host permission store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def grant(
        self,
        user_id: str,
        host_id: str,
        level: int,
        *,
        created_by: str | None = None,
    ) -> HostPermission:
        """Upsert a host permission grant. See base class for contract."""
        now = now_epoch()
        with self._session() as session:
            is_sqlite = self._engine.dialect.name == "sqlite"
            insert = sqlite_insert if is_sqlite else pg_insert
            stmt = (
                insert(SqlHostPermission)
                .values(
                    user_id=user_id,
                    host_id=host_id,
                    level=level,
                    created_at=now,
                    updated_at=now,
                    created_by=created_by,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "host_id"],
                    # Only the level and updated_at change on re-grant;
                    # created_at / created_by stay as first written.
                    set_={"level": level, "updated_at": now},
                )
            )
            session.execute(stmt)
            session.flush()
            row = session.get(SqlHostPermission, (user_id, host_id))
            # row is non-None: we just upserted it.
            assert row is not None
            return _to_entity(row)

    def revoke(self, user_id: str, host_id: str) -> bool:
        """Remove a host permission grant. See base class for contract."""
        with self._session() as session:
            result = session.execute(
                delete(SqlHostPermission).where(
                    SqlHostPermission.user_id == user_id,
                    SqlHostPermission.host_id == host_id,
                )
            )
            return result.rowcount > 0

    def get(self, user_id: str, host_id: str) -> HostPermission | None:
        """Look up a single grant. See base class for contract."""
        with self._session() as session:
            row = session.get(SqlHostPermission, (user_id, host_id))
            return _to_entity(row) if row is not None else None

    def list_for_host(self, host_id: str) -> list[HostPermission]:
        """Return all grants on a host. See base class for contract."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlHostPermission).where(SqlHostPermission.host_id == host_id)
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def list_for_user(self, user_id: str) -> list[HostPermission]:
        """Return all grants for a user. See base class for contract."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlHostPermission).where(SqlHostPermission.user_id == user_id)
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def check_access(
        self,
        user_id: str | None,
        host_id: str,
        required_level: int,
    ) -> bool:
        """Check grant-level access. See base class for contract."""
        if user_id is None:
            return False
        grant = self.get(user_id, host_id)
        return grant is not None and grant.level >= required_level

    def get_permission_level(
        self,
        user_id: str | None,
        host_id: str,
    ) -> int | None:
        """Return the user's effective grant level. See base class for contract."""
        if user_id is None:
            return None
        grant = self.get(user_id, host_id)
        return grant.level if grant is not None else None
