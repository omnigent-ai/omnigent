"""SQLAlchemy-backed permission store."""

from __future__ import annotations

from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from omnigent.db.db_models import SqlSessionPermission, SqlUser
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.entities import ResolvedAccess, SessionPermission
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_PUBLIC
from omnigent.stores.permission_store import PermissionStore


def _to_entity(row: SqlSessionPermission) -> SessionPermission:
    """Convert a :class:`SqlSessionPermission` ORM row to a domain entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`SessionPermission` dataclass instance.
    """
    return SessionPermission(
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        level=row.level,
    )


class SqlAlchemyPermissionStore(PermissionStore):
    """SQLAlchemy-backed implementation of :class:`PermissionStore`.

    Persists session permissions in a relational database via
    SQLAlchemy ORM. Uses dialect-aware upsert for grants
    (SQLite ``ON CONFLICT DO UPDATE``, PostgreSQL
    ``ON CONFLICT ... DO UPDATE``).
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy permission store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    def grant(
        self,
        user_id: str,
        conversation_id: str,
        level: int,
    ) -> SessionPermission:
        """Upsert a permission grant. See base class for contract."""
        with self._session() as session:
            dialect = self._engine.dialect.name
            values = {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "level": level,
            }
            if dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlSessionPermission)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["user_id", "conversation_id"],
                        set_={"level": level},
                    )
                )
            elif dialect in ("mysql", "mariadb"):
                # MySQL/MariaDB: ON DUPLICATE KEY UPDATE instead of PostgreSQL's
                # ON CONFLICT DO UPDATE. Both are atomic upserts; the SQLAlchemy
                # dialect objects differ but the intent is identical.
                from sqlalchemy.dialects.mysql import insert as mysql_insert

                stmt = mysql_insert(SqlSessionPermission).values(**values)
                stmt = stmt.on_duplicate_key_update(level=stmt.inserted.level)
            else:
                stmt = (
                    pg_insert(SqlSessionPermission)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["user_id", "conversation_id"],
                        set_={"level": level},
                    )
                )
            session.execute(stmt)
            session.flush()
            return SessionPermission(
                user_id=user_id,
                conversation_id=conversation_id,
                level=level,
            )

    def revoke(self, user_id: str, conversation_id: str) -> bool:
        """Remove a permission grant. See base class for contract."""
        with self._session() as session:
            result = session.execute(
                delete(SqlSessionPermission).where(
                    SqlSessionPermission.user_id == user_id,
                    SqlSessionPermission.conversation_id == conversation_id,
                )
            )
            return result.rowcount > 0

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        """Look up a single grant. See base class for contract."""
        with self._session() as session:
            row = session.get(SqlSessionPermission, (user_id, conversation_id))
            return _to_entity(row) if row is not None else None

    def reassign_user_grants(self, from_user_id: str, to_user_id: str) -> int:
        """Move all of one user's session grants to another user.

        Used on a single-user loopback server's first accounts setup to
        hand the new admin the sessions previously owned by the reserved
        ``local`` user, so pre-accounts chats stay visible after opting
        into accounts. For each grant on *from_user_id*: if *to_user_id*
        has no grant for that conversation, repoint the grant; otherwise
        drop the duplicate ``from`` grant. The destination user row is
        ensured first so the ``session_permissions.user_id`` foreign key
        (``users.id``) holds.

        :param from_user_id: Source grantee whose grants move, e.g.
            ``"local"``.
        :param to_user_id: Destination grantee that receives them, e.g.
            ``"alice"``.
        :returns: The number of grants repointed to *to_user_id*.
        """
        moved = 0
        with self._session() as session:
            # FK target: ensure the destination users.id row exists. Don't
            # downgrade an existing admin flag; only create it if missing.
            if session.get(SqlUser, to_user_id) is None:
                session.add(SqlUser(id=to_user_id, is_admin=False))
                session.flush()
            rows = (
                session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.user_id == from_user_id,
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                conversation_id = row.conversation_id
                if session.get(SqlSessionPermission, (to_user_id, conversation_id)) is not None:
                    # Destination already has access — drop the duplicate.
                    session.delete(row)
                    continue
                # user_id is part of the PK, so repoint with a targeted Core
                # UPDATE rather than mutating the ORM object's primary key.
                session.execute(
                    update(SqlSessionPermission)
                    .where(
                        SqlSessionPermission.user_id == from_user_id,
                        SqlSessionPermission.conversation_id == conversation_id,
                    )
                    .values(user_id=to_user_id)
                )
                moved += 1
            return moved

    def list_for_session(self, conversation_id: str) -> list[SessionPermission]:
        """Return all grants on a session. See base class for contract."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.conversation_id == conversation_id,
                    )
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def list_for_sessions(self, conversation_ids: list[str]) -> dict[str, list[SessionPermission]]:
        """Return all grants for multiple sessions.  See base class for contract."""
        if not conversation_ids:
            return {}
        with self._session() as session:
            # Convert to entities inside the session so ORM attributes are
            # accessed while the session is still open (avoids DetachedInstanceError).
            entities = [
                _to_entity(r)
                for r in session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.conversation_id.in_(conversation_ids)
                    )
                )
                .scalars()
                .all()
            ]
        result: dict[str, list[SessionPermission]] = {cid: [] for cid in conversation_ids}
        for entity in entities:
            result[entity.conversation_id].append(entity)
        return result

    def list_for_user(self, user_id: str) -> list[SessionPermission]:
        """Return all grants for a user. See base class for contract."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.user_id == user_id,
                    )
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        """Upsert a user row. See base class for contract."""
        with self._session() as session:
            dialect = self._engine.dialect.name
            values = {"id": user_id, "is_admin": is_admin}
            if dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlUser)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            elif dialect in ("mysql", "mariadb"):
                # MySQL/MariaDB: no ON CONFLICT DO NOTHING; use a no-op
                # ON DUPLICATE KEY UPDATE instead (update id to itself).
                from sqlalchemy.dialects.mysql import insert as mysql_insert

                stmt = mysql_insert(SqlUser).values(**values)
                stmt = stmt.on_duplicate_key_update(id=stmt.inserted.id)
            else:
                stmt = (
                    pg_insert(SqlUser)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            session.execute(stmt)

    def is_admin(self, user_id: str) -> bool:
        """Check the admin flag. See base class for contract."""
        with self._session() as session:
            row = session.get(SqlUser, user_id)
            return row is not None and row.is_admin

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        """Set the admin flag on an existing user. See base class for contract."""
        with self._session() as session:
            session.execute(update(SqlUser).where(SqlUser.id == user_id).values(is_admin=is_admin))

    def check_access(
        self,
        user_id: str | None,
        conversation_id: str,
        required_level: int,
    ) -> bool:
        """Check grant-level access. See base class for contract."""
        if user_id is None:
            return False

        grant = self.get(user_id, conversation_id)
        if grant is not None and grant.level >= required_level:
            return True

        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None and public_grant.level >= required_level:
            return True

        return False

    def get_permission_level(
        self,
        user_id: str | None,
        conversation_id: str,
    ) -> int | None:
        """Return the user's effective permission level. See base class for contract."""
        if user_id is None:
            return None
        if self.is_admin(user_id):
            return LEVEL_OWNER
        grant = self.get(user_id, conversation_id)
        if grant is not None:
            return grant.level
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        if public_grant is not None:
            return public_grant.level
        return None

    def resolve_access(
        self,
        user_id: str | None,
        conversation_id: str,
    ) -> ResolvedAccess:
        """Resolve admin flag + user + public grants together. See base class."""
        if user_id is None:
            return ResolvedAccess(
                is_admin=False,
                user_grant_level=None,
                public_grant_level=None,
            )
        # One session = one connection checkout + transaction. Against a
        # remote DB (Lakebase) this is the round-trip that matters; the three
        # primary-key reads below pipeline on the same connection rather than
        # paying three separate checkout/BEGIN/COMMIT cycles (which is what
        # calling is_admin + check_access + get_permission_level separately
        # did — see the GET /v1/sessions/{id} snapshot path).
        with self._session() as session:
            user_row = session.get(SqlUser, user_id)
            user_grant = session.get(SqlSessionPermission, (user_id, conversation_id))
            public_grant = session.get(
                SqlSessionPermission, (RESERVED_USER_PUBLIC, conversation_id)
            )
            return ResolvedAccess(
                is_admin=user_row is not None and user_row.is_admin,
                user_grant_level=user_grant.level if user_grant is not None else None,
                public_grant_level=public_grant.level if public_grant is not None else None,
            )

    def has_any_grants(self, conversation_id: str) -> bool:
        """Check for any permission rows. See base class for contract."""
        with self._session() as session:
            return session.execute(
                select(
                    exists().where(
                        SqlSessionPermission.conversation_id == conversation_id,
                    )
                )
            ).scalar_one()
