"""SQLAlchemy-backed policy store."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import asc, select
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlPolicy,
    current_workspace_id,
    policy_name_cksum,
)
from omnigent.db.enum_codecs import (
    decode_policy_scope,
    decode_policy_type,
    encode_policy_scope,
    encode_policy_type,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Policy
from omnigent.stores.policy_store import PolicyStore


def _to_entity(row: SqlPolicy) -> Policy:
    """
    Convert a :class:`SqlPolicy` ORM row to a :class:`Policy` entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Policy` dataclass instance.
    """
    return Policy(
        id=row.id,
        name=row.name,
        session_id=row.session_id,
        scope=decode_policy_scope(row.scope),
        created_at=row.created_at,
        type=decode_policy_type(row.type),
        handler=row.handler,
        factory_params=json.loads(row.factory_params) if row.factory_params else None,
        enabled=bool(row.enabled),
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


class SqlAlchemyPolicyStore(PolicyStore):
    """
    SQLAlchemy-backed implementation of :class:`PolicyStore`.

    Persists policies in a relational database via SQLAlchemy ORM.
    Supports both session-scoped (``session_id`` set) and
    server-wide default (``session_id IS NULL``) policies.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy policy store.

        Creates or reuses a SQLAlchemy engine and session
        factory for the given database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)

    # ── Session-scoped policy methods ────────────────────────────

    def create(
        self,
        policy_id: str,
        session_id: str,
        name: str,
        type: str,
        handler: str,
        factory_params: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> Policy:
        """Insert a new session-scoped policy.

        Raises ``IntegrityError`` on ``(session_id, name)`` collision.
        """
        row = SqlPolicy(
            id=policy_id,
            name=name,
            session_id=session_id,
            scope=encode_policy_scope("session"),
            created_at=now_epoch(),
            updated_at=None,
            type=encode_policy_type(type),
            handler=handler,
            factory_params=json.dumps(factory_params) if factory_params else None,
            enabled=enabled,
        )
        with self._session() as session:
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get(self, policy_id: str, session_id: str) -> Policy | None:
        """Return the policy if it belongs to the given session."""
        with self._session() as session:
            row = session.get(SqlPolicy, (current_workspace_id(), policy_id))
            if row is None or row.session_id != session_id:
                return None
            return _to_entity(row)

    def list_for_session(self, session_id: str) -> list[Policy]:
        """List policies for a session ordered by ``created_at ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlPolicy)
                .where(SqlPolicy.workspace_id == current_workspace_id())
                .where(SqlPolicy.session_id == session_id)
                .order_by(asc(SqlPolicy.created_at), asc(SqlPolicy.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        policy_id: str,
        session_id: str,
        *,
        name: str | None = None,
        handler: str | None = None,
        enabled: bool | None = None,
    ) -> Policy | None:
        """
        Update mutable fields. Returns ``None`` if not found or
        wrong session.
        """
        with self._session() as session:
            row = session.get(SqlPolicy, (current_workspace_id(), policy_id))
            if row is None or row.session_id != session_id:
                return None
            changed = False
            if name is not None and row.name != name:
                row.name = name
                # Column defaults don't fire on UPDATE — recompute the digest.
                row.name_cksum = policy_name_cksum(name)
                changed = True
            if handler is not None and row.handler != handler:
                row.handler = handler
                changed = True
            if enabled is not None and bool(row.enabled) != enabled:
                row.enabled = enabled
                changed = True
            if changed:
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete(self, policy_id: str, session_id: str) -> bool:
        """Delete a policy. Idempotent: returns ``False`` if not found."""
        with self._session() as session:
            row = session.get(SqlPolicy, (current_workspace_id(), policy_id))
            if row is None or row.session_id != session_id:
                return False
            session.delete(row)
            return True

    # ── Default (server-wide) policy methods ─────────────────────

    def create_default(
        self,
        policy_id: str,
        name: str,
        type: str,
        handler: str,
        factory_params: dict[str, Any] | None = None,
        enabled: bool = True,
        created_by: str | None = None,
    ) -> Policy:
        """Insert a new default policy (``session_id=NULL``).

        Raises ``IntegrityError`` on name collision among defaults.

        SQLite treats NULLs as distinct in composite unique
        constraints, so the ``(session_id, name_cksum)`` constraint
        does not enforce uniqueness among default policies.
        This method checks for duplicates explicitly (by name digest).
        """

        row = SqlPolicy(
            id=policy_id,
            name=name,
            session_id=None,
            scope=encode_policy_scope("default"),
            created_at=now_epoch(),
            updated_at=None,
            type=encode_policy_type(type),
            handler=handler,
            factory_params=json.dumps(factory_params) if factory_params else None,
            enabled=enabled,
            created_by=created_by,
        )
        with self._session() as session:
            # Explicit uniqueness check: SQLite treats NULLs as
            # distinct in composite unique constraints, so
            # (NULL, name) won't collide with another (NULL, name).
            existing = (
                session.execute(
                    select(SqlPolicy)
                    .where(SqlPolicy.workspace_id == current_workspace_id())
                    .where(SqlPolicy.scope == encode_policy_scope("default"))
                    .where(SqlPolicy.name_cksum == policy_name_cksum(name))
                )
                .scalars()
                .first()
            )
            if existing is not None:
                raise IntegrityError(
                    "Duplicate default policy name",
                    params={"name": name},
                    orig=Exception(f"UNIQUE constraint: name={name!r}"),
                )
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get_default(self, policy_id: str) -> Policy | None:
        """Return a default policy by ID (``scope = 'default'``)."""
        with self._session() as session:
            row = session.get(SqlPolicy, (current_workspace_id(), policy_id))
            if row is None or row.scope != encode_policy_scope("default"):
                return None
            return _to_entity(row)

    def list_defaults(self) -> list[Policy]:
        """List all default policies ordered by ``created_at ASC``."""
        with self._session() as session:
            stmt = (
                select(SqlPolicy)
                .where(SqlPolicy.workspace_id == current_workspace_id())
                .where(SqlPolicy.scope == encode_policy_scope("default"))
                .order_by(asc(SqlPolicy.created_at), asc(SqlPolicy.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update_default(
        self,
        policy_id: str,
        *,
        name: str | None = None,
        handler: str | None = None,
        enabled: bool | None = None,
    ) -> Policy | None:
        """
        Update mutable fields of a default policy. Returns ``None``
        if not found or not a default policy.
        """
        with self._session() as session:
            row = session.get(SqlPolicy, (current_workspace_id(), policy_id))
            if row is None or row.scope != encode_policy_scope("default"):
                return None
            changed = False
            if name is not None and row.name != name:
                # Explicit uniqueness check for the application layer;
                # the partial index ix_policies_default_name_cksum is the
                # DB-layer guard, but checking here gives a cleaner error.
                conflict = (
                    session.execute(
                        select(SqlPolicy)
                        .where(SqlPolicy.workspace_id == current_workspace_id())
                        .where(SqlPolicy.scope == encode_policy_scope("default"))
                        .where(SqlPolicy.name_cksum == policy_name_cksum(name))
                        .where(SqlPolicy.id != policy_id)
                    )
                    .scalars()
                    .first()
                )
                if conflict is not None:
                    raise IntegrityError(
                        "Duplicate default policy name",
                        params={"name": name},
                        orig=Exception(f"UNIQUE constraint: name={name!r}"),
                    )
                row.name = name
                # Column defaults don't fire on UPDATE — recompute the digest.
                row.name_cksum = policy_name_cksum(name)
                changed = True
            if handler is not None and row.handler != handler:
                row.handler = handler
                changed = True
            if enabled is not None and bool(row.enabled) != enabled:
                row.enabled = enabled
                changed = True
            if changed:
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete_default(self, policy_id: str) -> bool:
        """Delete a default policy. Idempotent."""
        with self._session() as session:
            row = session.get(SqlPolicy, (current_workspace_id(), policy_id))
            if row is None or row.scope != encode_policy_scope("default"):
                return False
            session.delete(row)
            return True
