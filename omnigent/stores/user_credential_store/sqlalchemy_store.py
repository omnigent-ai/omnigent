"""SQLAlchemy-backed per-user credential vault store."""

from __future__ import annotations

from sqlalchemy import select

from omnigent.db.db_models import SqlUserCredential
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities.user_credential import UserCredential
from omnigent.stores.user_credential_store import UserCredentialStore


def _to_entity(row: SqlUserCredential) -> UserCredential:
    """Convert a row to a metadata entity (never exposes the ciphertext)."""
    return UserCredential(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyUserCredentialStore(UserCredentialStore):
    """SQLAlchemy-backed implementation of :class:`UserCredentialStore`."""

    def __init__(self, storage_location: str) -> None:
        """Create or reuse a SQLAlchemy engine + session factory."""
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        # immediate=True: upsert is a check-then-write, so take the SQLite write
        # lock up front to avoid a concurrent-create race on (user_id, name).
        self._session = make_managed_session_maker(self._engine, immediate=True)

    def list_for_user(self, user_id: str) -> list[UserCredential]:
        """Return the user's credential metadata (no secret values)."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlUserCredential).where(SqlUserCredential.user_id == user_id)
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def upsert(
        self,
        credential_id: str,
        user_id: str,
        name: str,
        secret_encrypted: str,
    ) -> UserCredential:
        """Store or overwrite a user's secret, keyed by (user_id, name)."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlUserCredential).where(
                        SqlUserCredential.user_id == user_id,
                        SqlUserCredential.name == name,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = SqlUserCredential(
                    id=credential_id,
                    user_id=user_id,
                    name=name,
                    secret_encrypted=secret_encrypted,
                    created_at=now_epoch(),
                    updated_at=None,
                )
                session.add(row)
            else:
                row.secret_encrypted = secret_encrypted
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def get_encrypted(self, user_id: str, name: str) -> str | None:
        """Return the encrypted secret for (user_id, name), or None."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlUserCredential).where(
                        SqlUserCredential.user_id == user_id,
                        SqlUserCredential.name == name,
                    )
                )
                .scalars()
                .first()
            )
            return row.secret_encrypted if row is not None else None

    def delete(self, user_id: str, name: str) -> bool:
        """Delete a user's credential. Idempotent."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlUserCredential).where(
                        SqlUserCredential.user_id == user_id,
                        SqlUserCredential.name == name,
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                return False
            session.delete(row)
            return True
