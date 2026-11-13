"""SQLAlchemy-backed permission store."""

from __future__ import annotations

import collections
import os
import threading
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import TypeVar, cast

from sqlalchemy import String, case, delete, exists, func, literal, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ColumnElement

from omnigent.db.account_authority import account_generation, lock_account, require_active_account
from omnigent.db.db_models import (
    SqlScheduledTask,
    SqlScheduledTaskShare,
    SqlSessionPermission,
    SqlUser,
    current_workspace_id,
)
from omnigent.db.enum_codecs import encode_scheduled_task_state
from omnigent.db.utils import (
    NamedManagedSessionMaker,
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.entities import Account, ResolvedAccess, ScheduledTaskPermission, SessionPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import session_sharing
from omnigent.server.auth import (
    LEVEL_OWNER,
    RESERVED_USER_LOCAL,
    RESERVED_USER_PUBLIC,
    effective_admin,
)
from omnigent.server.sharing_settings import effective_public_level
from omnigent.stores.permission_store import MAX_GRANTS_PAGE_SIZE, PermissionStore

# Sentinel rows excluded from list_users() — never real, actionable
# actors. Mirrors accounts_store._HIDDEN_LIST_USERS so the admin user
# list is identical across auth modes.
_HIDDEN_LIST_USERS = frozenset({RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL})

# Width of scheduled_task_shares.user_id, which SQLite does not enforce.
_GRANTEE_ID_TYPE = SqlScheduledTaskShare.__table__.c.user_id.type
assert isinstance(_GRANTEE_ID_TYPE, String) and _GRANTEE_ID_TYPE.length is not None
_GRANTEE_ID_MAX_CHARS: int = _GRANTEE_ID_TYPE.length
# Never automation grantees: automations are never public, and ``local`` is the
# single-user identity rather than someone to share with.
_RESERVED_GRANTEES = frozenset({RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL})

# Short-lived cache of resolve_access() results. The per-event access-control
# check on a busy session otherwise re-reads session_permissions + users on
# every streamed event, for a session whose grants are stable across the turn.
# Only a *positive* standing is cached — a no-access result is never stored, so
# a freshly granted user is authorized on their next request, not after the TTL.
# This store's own grant/revoke/reassign/set_admin writes evict, and a
# generation counter stops an in-flight reader from re-storing a pre-commit
# positive on top of that eviction, so once such a write returns this instance
# serves no stale decision. Role changes made through the separate accounts
# store (admin demote, user delete) are NOT evicted here and propagate within
# the TTL. Across replicas there is no invalidation broadcast either, so a
# revoke can be up to the TTL late elsewhere; that window is LEVEL_EDIT only —
# the destructive stop/kill path re-gates LEVEL_OWNER separately, and a deleted
# session still 404s on its uncached conversation read. Entries are keyed by
# (conversation_id, user_id):
# conversation ids are globally unique, so eviction needs no workspace context,
# and the map is an LRU bounded by a hard entry cap so a long-lived replica
# cannot grow it without limit (resolve_access is on the snapshot path too, not
# just the hot event path). Set the TTL env to 0 to disable (zero overhead).
_RESOLVE_ACCESS_CACHE_TTL_ENV = "OMNIGENT_ACL_RESOLVE_CACHE_TTL_S"
_DEFAULT_RESOLVE_ACCESS_CACHE_TTL_S = 5.0
_RESOLVE_ACCESS_CACHE_MAX_ENTRIES_ENV = "OMNIGENT_ACL_RESOLVE_CACHE_MAX_ENTRIES"
_DEFAULT_RESOLVE_ACCESS_CACHE_MAX_ENTRIES = 50_000


def _resolve_access_cache_ttl_s() -> float:
    """Read the resolve_access cache TTL (seconds) from the environment.

    Defaults to :data:`_DEFAULT_RESOLVE_ACCESS_CACHE_TTL_S`; a value <= 0
    disables the cache. An unparseable value falls back to the default.
    """
    raw = os.environ.get(_RESOLVE_ACCESS_CACHE_TTL_ENV)
    if raw is None:
        return _DEFAULT_RESOLVE_ACCESS_CACHE_TTL_S
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return _DEFAULT_RESOLVE_ACCESS_CACHE_TTL_S


def _resolve_access_cache_max_entries() -> int:
    """Read the resolve_access cache entry cap from the environment.

    Defaults to :data:`_DEFAULT_RESOLVE_ACCESS_CACHE_MAX_ENTRIES`; ``0`` means
    unbounded. An unparseable value falls back to the default.
    """
    raw = os.environ.get(_RESOLVE_ACCESS_CACHE_MAX_ENTRIES_ENV)
    if raw is None:
        return _DEFAULT_RESOLVE_ACCESS_CACHE_MAX_ENTRIES
    try:
        return max(int(raw), 0)
    except ValueError:
        return _DEFAULT_RESOLVE_ACCESS_CACHE_MAX_ENTRIES


def _to_account(row: SqlUser) -> Account:
    """Convert a :class:`SqlUser` ORM row to an :class:`Account` entity.

    Strips ``password_hash`` — it never leaves the store via this
    conversion (see :class:`Account`). Mirrors
    ``accounts_store._to_account`` so both stores surface the same
    admin user shape.
    """
    return Account(
        id=row.id,
        is_admin=row.is_admin,
        created_at=row.created_at,
        last_login_at=row.last_login_at,
        has_password=row.password_hash is not None,
        account_generation=row.account_generation,
    )


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


def _upsert_grant(
    dialect: str,
    model: type[SqlSessionPermission] | type[SqlScheduledTaskShare],
    key: dict[str, str],
    level: int,
    *,
    ensure_minimum: bool = False,
) -> Insert:
    """Build a grant upsert, replacing the level unless stronger access is preserved.

    :param key: The row's primary key columns other than ``workspace_id``.
    :param ensure_minimum: Atomically retain the higher existing or requested level.
    """
    key_columns = ["workspace_id", *key]
    next_level = case((model.level < level, level), else_=model.level) if ensure_minimum else level
    if dialect == "sqlite":
        return (
            sqlite_insert(model)
            .values(**key, level=level)
            .on_conflict_do_update(index_elements=key_columns, set_={"level": next_level})
        )
    if dialect == "mysql":
        return (
            mysql_insert(model)
            .values(**key, level=level)
            .on_duplicate_key_update(level=next_level)
        )
    return (
        pg_insert(model)
        .values(**key, level=level)
        .on_conflict_do_update(index_elements=key_columns, set_={"level": next_level})
    )


def _reserved_grantee_error(user_id: str) -> OmnigentError:
    return OmnigentError(
        f"An automation cannot be shared with {user_id!r}.",
        code=ErrorCode.INVALID_INPUT,
    )


def _require_storable_scheduled_task_grant(user_id: str, level: int) -> None:
    """Refuse a grant the share table cannot hold or the store must never hold."""
    if len(user_id) > _GRANTEE_ID_MAX_CHARS:
        raise OmnigentError(
            f"A grantee ID may be at most {_GRANTEE_ID_MAX_CHARS} characters.",
            code=ErrorCode.INVALID_INPUT,
        )
    # A fast path; the grant rechecks under the key's collation, which folds accents too.
    if user_id.strip().casefold() in _RESERVED_GRANTEES:
        raise _reserved_grantee_error(user_id)
    if level not in session_sharing.SCHEDULED_TASK_GRANT_LEVELS:
        raise OmnigentError(
            f"Automations cannot grant permission level {level!r}.",
            code=ErrorCode.INVALID_INPUT,
        )


def _lock_live_scheduled_task(session: Session, scheduled_task_id: str, grantee: str) -> None:
    """Lock a task's row before its grant rows, the order account deletion also takes.

    A task deleted before the lock refuses the grant, so none outlives it. The owner
    is compared in SQL, so a variant the column's collation matches is refused too.
    """
    row = session.execute(
        select(SqlScheduledTask.state, SqlScheduledTask.user_id == grantee)
        .where(
            SqlScheduledTask.workspace_id == current_workspace_id(),
            SqlScheduledTask.id == scheduled_task_id,
        )
        .with_for_update()
    ).one_or_none()
    if row is None or row[0] == encode_scheduled_task_state("deleted"):
        raise OmnigentError("Automation not found", code=ErrorCode.NOT_FOUND)
    if row[1]:
        raise OmnigentError(
            "An automation's owner cannot be granted access to it.",
            code=ErrorCode.INVALID_INPUT,
        )


_GrantRow = TypeVar("_GrantRow", SqlSessionPermission, SqlScheduledTaskShare)
_Grant = TypeVar("_Grant")


def _grant_page(
    session: Session,
    model: type[_GrantRow],
    resource: ColumnElement[bool],
    to_entity: Callable[[_GrantRow], _Grant],
    *,
    limit: int,
    after_user_id: str | None,
) -> tuple[list[_Grant], str | None]:
    """Read one page of a resource's grants ordered by grantee.

    The cursor is the last returned user_id; the next page reads ``user_id > cursor``.

    :raises OmnigentError: ``INVALID_INPUT`` unless ``1 <= limit <= MAX_GRANTS_PAGE_SIZE``.
    """
    if not 1 <= limit <= MAX_GRANTS_PAGE_SIZE:
        raise OmnigentError(
            f"limit must be between 1 and {MAX_GRANTS_PAGE_SIZE}.",
            code=ErrorCode.INVALID_INPUT,
        )
    stmt = (
        select(model)
        .where(model.workspace_id == current_workspace_id(), resource)
        .order_by(model.user_id.asc())
        .limit(limit + 1)
    )
    if after_user_id is not None:
        stmt = stmt.where(model.user_id > after_user_id)
    rows = session.execute(stmt).scalars().all()
    page = rows[:limit]
    next_cursor = page[-1].user_id if len(rows) > limit else None
    return [to_entity(row) for row in page], next_cursor


def _delete_grant_batch(
    session: Session,
    *,
    match: ColumnElement[bool],
    key_column: InstrumentedAttribute[str],
    after: str | None,
    batch_size: int,
    proceed: Callable[[Session], bool] | None,
) -> tuple[str | None, int]:
    """Delete up to *batch_size* matching grants past *after*; return the last key and count."""
    if proceed is not None and not proceed(session):
        return None, 0
    stmt = (
        select(key_column)
        .where(SqlScheduledTaskShare.workspace_id == current_workspace_id(), match)
        .order_by(key_column)
        .limit(batch_size)
    )
    if after is not None:
        stmt = stmt.where(key_column > after)
    keys = list(session.execute(stmt).scalars())
    if not keys:
        return None, 0
    result = cast(
        CursorResult[tuple[object]],
        session.execute(
            delete(SqlScheduledTaskShare).where(
                SqlScheduledTaskShare.workspace_id == current_workspace_id(),
                match,
                key_column.in_(keys),
            )
        ),
    )
    return keys[-1], result.rowcount


def _delete_grants_in_batches(
    session_maker: NamedManagedSessionMaker,
    operation_name: str,
    *,
    match: ColumnElement[bool],
    key_column: InstrumentedAttribute[str],
    batch_size: int,
    proceed: Callable[[Session], bool] | None = None,
) -> int:
    """Walk an index over the matching grants, deleting one batch per transaction.

    Stops at an empty batch, or when *proceed* returns ``False`` at the start of one.
    """
    deleted, after = 0, None
    while True:
        after, count = run_write_transaction(
            session_maker,
            operation_name,
            partial(
                _delete_grant_batch,
                match=match,
                key_column=key_column,
                after=after,
                batch_size=batch_size,
                proceed=proceed,
            ),
        )
        if after is None:
            return deleted
        deleted += count


def delete_scheduled_task_grants(
    session_maker: NamedManagedSessionMaker, scheduled_task_id: str, *, batch_size: int
) -> int:
    """Delete a task's grants along the primary key, one bounded batch per transaction."""
    return _delete_grants_in_batches(
        session_maker,
        "delete_scheduled_task_grant_batch",
        match=SqlScheduledTaskShare.scheduled_task_id == scheduled_task_id,
        key_column=SqlScheduledTaskShare.user_id,
        batch_size=batch_size,
    )


def delete_deleted_task_grants(
    session_maker: NamedManagedSessionMaker, scheduled_task_id: str, *, batch_size: int
) -> int:
    """Delete a deleted task's grants, one bounded batch per transaction.

    Each batch first locks the task row and stops unless the task is deleted or gone,
    so a live task, such as one a re-registered owner created, keeps its grants.
    """

    def task_is_deleted(session: Session) -> bool:
        state = session.execute(
            select(SqlScheduledTask.state)
            .where(
                SqlScheduledTask.workspace_id == current_workspace_id(),
                SqlScheduledTask.id == scheduled_task_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        return state is None or state == encode_scheduled_task_state("deleted")

    return _delete_grants_in_batches(
        session_maker,
        "delete_deleted_task_grant_batch",
        match=SqlScheduledTaskShare.scheduled_task_id == scheduled_task_id,
        key_column=SqlScheduledTaskShare.user_id,
        batch_size=batch_size,
        proceed=task_is_deleted,
    )


def delete_deleted_account_grants(
    session_maker: NamedManagedSessionMaker, user_id: str, *, batch_size: int
) -> int:
    """Delete the grants a deleted account held, one bounded batch per transaction.

    Walks ``ix_scheduled_task_shares_user_id``. Each batch first locks the account and
    stops once the name belongs to a live account again, whose grants are its own.
    """

    def account_is_deleted(session: Session) -> bool:
        account = lock_account(session, user_id)
        return account is None or account.deleted_at is not None

    return _delete_grants_in_batches(
        session_maker,
        "delete_deleted_account_grant_batch",
        match=SqlScheduledTaskShare.user_id == user_id,
        key_column=SqlScheduledTaskShare.scheduled_task_id,
        batch_size=batch_size,
        proceed=account_is_deleted,
    )


def holds_scheduled_task_grants(session: Session, user_id: str) -> bool:
    """Whether any grant to *user_id* remains in the workspace, in one indexed probe."""
    return session.execute(
        select(
            exists().where(
                SqlScheduledTaskShare.workspace_id == current_workspace_id(),
                SqlScheduledTaskShare.user_id == user_id,
            )
        )
    ).scalar_one()


def _to_scheduled_task_permission(row: SqlScheduledTaskShare) -> ScheduledTaskPermission:
    """Convert a :class:`SqlScheduledTaskShare` ORM row to a domain entity."""
    return ScheduledTaskPermission(
        user_id=row.user_id,
        scheduled_task_id=row.scheduled_task_id,
        level=row.level,
    )


class SqlAlchemyPermissionStore(PermissionStore):
    """SQLAlchemy-backed implementation of :class:`PermissionStore`.

    Persists session permissions in a relational database via
    SQLAlchemy ORM. Uses dialect-aware upsert for grants
    (SQLite ``ON CONFLICT DO UPDATE``, PostgreSQL
    ``ON CONFLICT ... DO UPDATE``).
    """

    # Grants deleted per transaction; tests shrink it to span several batches.
    _cleanup_batch_size = 100

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy permission store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.permission_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.permission_store",
            immediate=True,
        )
        # resolve_access cache (see _RESOLVE_ACCESS_CACHE_TTL_ENV). An LRU keyed
        # (conversation_id, user_id, account_generation) -> (expiry, access). IDs are
        # globally unique, so grant/revoke can drop a whole session's entries —
        # including the shared __public__ grant, which affects every user of
        # that session — without depending on the ambient workspace context.
        # The hard entry cap bounds memory on a long-lived replica. Per store
        # instance, so each replica caches independently and tests get a fresh
        # cache with each store. ``_resolve_cache_clock`` is injectable for
        # deterministic TTL tests.
        self._resolve_cache_ttl_s = _resolve_access_cache_ttl_s()
        self._resolve_cache_max_entries = _resolve_access_cache_max_entries()
        self._resolve_cache: collections.OrderedDict[
            tuple[str, str, str | None], tuple[float, ResolvedAccess]
        ] = collections.OrderedDict()
        self._resolve_cache_lock = threading.Lock()
        self._resolve_cache_clock: Callable[[], float] = time.monotonic
        # Bumped by every invalidation. resolve_access samples it before its DB
        # read and refuses to store a result sampled under an older generation,
        # so a reader whose snapshot predates a concurrent grant/revoke commit
        # cannot re-poison the cache after that write already evicted.
        self._resolve_cache_generation = 0

    def grant(
        self,
        user_id: str,
        conversation_id: str,
        level: int,
        *,
        ensure_minimum: bool = False,
    ) -> SessionPermission:
        """Upsert a permission grant. See base class for contract."""

        def write(session: Session) -> int:
            require_active_account(session, user_id)
            session.execute(
                _upsert_grant(
                    self._engine.dialect.name,
                    SqlSessionPermission,
                    {"user_id": user_id, "conversation_id": conversation_id},
                    level,
                    ensure_minimum=ensure_minimum,
                )
            )
            session.flush()
            if ensure_minimum:
                return session.execute(
                    select(SqlSessionPermission.level).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == user_id,
                        SqlSessionPermission.conversation_id == conversation_id,
                    )
                ).scalar_one()
            return level

        granted_level = run_write_transaction(
            self._session_immediate,
            "grant_permission",
            write,
        )
        # Evict after commit: the grant changed this session's access picture.
        self._invalidate_resolve_cache_for_session(conversation_id)
        return SessionPermission(
            user_id=user_id,
            conversation_id=conversation_id,
            level=granted_level,
        )

    def revoke(self, user_id: str, conversation_id: str) -> bool:
        """Remove a permission grant. See base class for contract."""

        def write(session: Session) -> bool:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    delete(SqlSessionPermission).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == user_id,
                        SqlSessionPermission.conversation_id == conversation_id,
                    )
                ),
            )
            return result.rowcount > 0

        deleted = run_write_transaction(
            self._session_immediate,
            "revoke_permission",
            write,
        )
        # Evict after commit: a revoke must not be served stale from this
        # instance's cache.
        self._invalidate_resolve_cache_for_session(conversation_id)
        return deleted

    def get(self, user_id: str, conversation_id: str) -> SessionPermission | None:
        """Look up a single grant. See base class for contract."""
        with self._session("select_permission") as session:
            row = session.get(
                SqlSessionPermission, (current_workspace_id(), user_id, conversation_id)
            )
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

        def write(session: Session) -> tuple[int, bool]:
            require_active_account(session, to_user_id)
            # FK target: ensure the destination users.id row exists. Don't
            # downgrade an existing admin flag; only create it if missing.
            if session.get(SqlUser, (current_workspace_id(), to_user_id)) is None:
                session.add(SqlUser(id=to_user_id, is_admin=False))
                session.flush()
            rows = (
                session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == from_user_id,
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return 0, False
            conversation_ids = [r.conversation_id for r in rows]
            # Single query: which conversation_ids does to_user already hold?
            existing_to = set(
                session.execute(
                    select(SqlSessionPermission.conversation_id).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == to_user_id,
                        SqlSessionPermission.conversation_id.in_(conversation_ids),
                    )
                ).scalars()
            )
            # Partition into duplicates (to_user already has access) vs. reassigns.
            duplicate_ids = [cid for cid in conversation_ids if cid in existing_to]
            reassign_ids = [cid for cid in conversation_ids if cid not in existing_to]
            # Bulk delete duplicates (to_user already has the grant).
            if duplicate_ids:
                session.execute(
                    delete(SqlSessionPermission).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == from_user_id,
                        SqlSessionPermission.conversation_id.in_(duplicate_ids),
                    )
                )
            # Bulk UPDATE reassigns in one statement.
            if reassign_ids:
                # user_id is part of the PK, so use a Core UPDATE.
                session.execute(
                    update(SqlSessionPermission)
                    .where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == from_user_id,
                        SqlSessionPermission.conversation_id.in_(reassign_ids),
                    )
                    .values(user_id=to_user_id)
                )
            return len(reassign_ids), True

        moved, changed = run_write_transaction(
            self._session_immediate,
            "reassign_user_grants",
            write,
        )
        # Grants moved between users across sessions; drop this store's cache
        # only when the transaction found source grants to change.
        if changed:
            self._invalidate_resolve_cache_all()
        return moved

    def list_for_session(
        self,
        conversation_id: str,
        *,
        limit: int = 100,
        after_user_id: str | None = None,
    ) -> tuple[list[SessionPermission], str | None]:
        """Return grants on a session with cursor pagination. See base class for contract."""
        with self._session("list_session_permissions") as session:
            return _grant_page(
                session,
                SqlSessionPermission,
                SqlSessionPermission.conversation_id == conversation_id,
                _to_entity,
                limit=limit,
                after_user_id=after_user_id,
            )

    def list_for_sessions(
        self, conversation_ids: list[str], *, user_id: str | None = None
    ) -> dict[str, list[SessionPermission]]:
        """Return all grants for multiple sessions.  See base class for contract."""
        if not conversation_ids:
            return {}
        with self._session("list_permissions_for_sessions") as session:
            # Convert to entities inside the session so ORM attributes are
            # accessed while the session is still open (avoids DetachedInstanceError).
            entities = [
                _to_entity(r)
                for r in session.execute(
                    select(SqlSessionPermission).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.conversation_id.in_(conversation_ids),
                        *(
                            [SqlSessionPermission.user_id == user_id]
                            if user_id is not None
                            else []
                        ),
                    )
                )
                .scalars()
                .all()
            ]
        result: dict[str, list[SessionPermission]] = {cid: [] for cid in conversation_ids}
        for entity in entities:
            result[entity.conversation_id].append(entity)
        return result

    def list_for_user(self, user_id: str, *, limit: int = 1000) -> list[SessionPermission]:
        """Return all grants for a user. See base class for contract."""
        with self._session("list_user_permissions") as session:
            rows = (
                session.execute(
                    select(SqlSessionPermission)
                    .where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.user_id == user_id,
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def ensure_user(self, user_id: str, *, is_admin: bool = False) -> None:
        """Upsert a user row. See base class for contract."""

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            dialect = self._engine.dialect.name
            values = {"id": user_id, "is_admin": is_admin}
            stmt: Insert
            if dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlUser)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["workspace_id", "id"])
                )
            elif dialect == "mysql":
                # ON DUPLICATE KEY UPDATE with a no-op to silently skip conflicts.
                stmt = (
                    mysql_insert(SqlUser)
                    .values(**values)
                    .on_duplicate_key_update(id=literal(user_id))
                )
            else:
                stmt = (
                    pg_insert(SqlUser)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["workspace_id", "id"])
                )
            session.execute(stmt)

        run_write_transaction(self._session_immediate, "ensure_user", write)

    def get_user(self, user_id: str) -> Account | None:
        """Read the target identity without creating it. See base class."""
        with self._session("read_target_account") as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            return _to_account(row) if row is not None and row.deleted_at is None else None

    def user_exists(self, user_id: str) -> bool:
        """Check for a user row. See base class for contract."""
        with self._session("select_user_exists") as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            return row is not None and row.deleted_at is None

    def list_users(self, *, limit: int = 1000) -> list[Account]:
        """List every real user row. See base class for contract."""
        with self._session("list_users") as session:
            rows = (
                session.execute(
                    select(SqlUser)
                    .where(SqlUser.workspace_id == current_workspace_id())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                _to_account(r)
                for r in rows
                if r.id not in _HIDDEN_LIST_USERS and r.deleted_at is None
            ]

    def is_admin(self, user_id: str) -> bool:
        """Check the admin flag. See base class for contract."""
        with self._session("select_user_admin_status") as session:
            row = session.get(SqlUser, (current_workspace_id(), user_id))
            return row is not None and row.deleted_at is None and row.is_admin

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        """Set the admin flag on an existing user. See base class for contract."""

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            session.execute(
                update(SqlUser)
                .where(
                    SqlUser.workspace_id == current_workspace_id(),
                    SqlUser.id == user_id,
                )
                .values(is_admin=is_admin)
            )

        run_write_transaction(self._session_immediate, "set_user_admin_status", write)
        # The admin flag flips access on every session for this user; drop
        # this store's cache.
        self._invalidate_resolve_cache_all()

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
        public_level = effective_public_level(public_grant.level if public_grant else None)
        if public_level is not None and public_level >= required_level:
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
        if effective_admin(self.is_admin(user_id)):
            return LEVEL_OWNER
        grant = self.get(user_id, conversation_id)
        public_grant = self.get(RESERVED_USER_PUBLIC, conversation_id)
        levels = [
            level
            for level in (
                grant.level if grant is not None else None,
                effective_public_level(public_grant.level if public_grant else None),
            )
            if level is not None
        ]
        return max(levels) if levels else None

    def _resolve_cache_lookup(self, conversation_id: str, user_id: str) -> ResolvedAccess | None:
        """Return a live cached resolve_access result, or ``None`` on miss/expiry."""
        now = self._resolve_cache_clock()
        key = (conversation_id, user_id, account_generation(user_id))
        with self._resolve_cache_lock:
            entry = self._resolve_cache.get(key)
            if entry is None:
                return None
            expiry, access = entry
            if now >= expiry:
                del self._resolve_cache[key]
                return None
            self._resolve_cache.move_to_end(key)  # LRU: mark most-recently used
            return access

    def _resolve_cache_generation_now(self) -> int:
        """Sample the invalidation generation before a read begins."""
        with self._resolve_cache_lock:
            return self._resolve_cache_generation

    def _resolve_cache_store(
        self,
        conversation_id: str,
        user_id: str,
        access: ResolvedAccess,
        generation: int,
    ) -> None:
        """Cache one *granted* resolve_access result until now + TTL.

        Dropped when *generation* is stale — an invalidation landed while this
        result was being read, so the value may predate that write and must not
        be stored on top of the eviction it already performed. Enforces the LRU
        entry cap so the cache cannot grow without bound on a long-lived replica.
        """
        key = (conversation_id, user_id, account_generation(user_id))
        expiry = self._resolve_cache_clock() + self._resolve_cache_ttl_s
        with self._resolve_cache_lock:
            if generation != self._resolve_cache_generation:
                return
            self._resolve_cache[key] = (expiry, access)
            self._resolve_cache.move_to_end(key)
            max_entries = self._resolve_cache_max_entries
            if max_entries > 0:
                while len(self._resolve_cache) > max_entries:
                    self._resolve_cache.popitem(last=False)  # drop least-recently used

    def _invalidate_resolve_cache_for_session(self, conversation_id: str) -> None:
        """Drop every cached decision for one session (all users + ``__public__``)."""
        with self._resolve_cache_lock:
            self._resolve_cache_generation += 1
            stale = [key for key in self._resolve_cache if key[0] == conversation_id]
            for key in stale:
                del self._resolve_cache[key]

    def _invalidate_resolve_cache_all(self) -> None:
        """Drop the whole cache — for admin-flag or bulk-grant changes."""
        with self._resolve_cache_lock:
            self._resolve_cache_generation += 1
            self._resolve_cache.clear()

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
        workspace_id = current_workspace_id()
        cache_enabled = self._resolve_cache_ttl_s > 0
        generation = 0
        if cache_enabled:
            cached = self._resolve_cache_lookup(conversation_id, user_id)
            if cached is not None:
                return cached
            # Sampled before the read: an invalidation landing while the rows
            # below are being fetched makes this result unstorable, so a write
            # committed mid-read can't be undone by a stale positive.
            generation = self._resolve_cache_generation_now()
        # One session = one connection checkout + transaction. Against a
        # remote DB (Lakebase) this is the round-trip that matters; the three
        # primary-key reads below pipeline on the same connection rather than
        # paying three separate checkout/BEGIN/COMMIT cycles (which is what
        # calling is_admin + check_access + get_permission_level separately
        # did — see the GET /v1/sessions/{id} snapshot path).
        with self._session("resolve_access") as session:
            user_row = session.get(SqlUser, (workspace_id, user_id))
            user_grant = session.get(
                SqlSessionPermission, (workspace_id, user_id, conversation_id)
            )
            public_grant = session.get(
                SqlSessionPermission,
                (workspace_id, RESERVED_USER_PUBLIC, conversation_id),
            )
            access = ResolvedAccess(
                is_admin=user_row is not None
                and user_row.deleted_at is None
                and user_row.is_admin,
                user_grant_level=user_grant.level if user_grant is not None else None,
                public_grant_level=public_grant.level if public_grant is not None else None,
            )
        # Cache only a positive standing: a no-access result is left uncached so
        # a freshly granted user is authorized on their next request, not after
        # the TTL elapses.
        if cache_enabled and (
            access.is_admin
            or access.user_grant_level is not None
            or access.public_grant_level is not None
        ):
            self._resolve_cache_store(conversation_id, user_id, access, generation)
        return access

    def has_any_grants(self, conversation_id: str) -> bool:
        """Check for any permission rows. See base class for contract."""
        with self._session("select_user_grant_presence") as session:
            return session.execute(
                select(
                    exists().where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        SqlSessionPermission.conversation_id == conversation_id,
                    )
                )
            ).scalar_one()

    # ── Scheduled task grants ─────────────────────────────────────────────

    @property
    def supports_scheduled_task_grants(self) -> bool:
        """Grants persist in ``scheduled_task_shares``. See base class."""
        return True

    def grant_for_scheduled_task(
        self,
        user_id: str,
        scheduled_task_id: str,
        level: int,
    ) -> ScheduledTaskPermission:
        """Upsert a scheduled task grant. See base class for contract.

        Locks the accounts, then the task, then the grant row.

        :raises OmnigentError: ``INVALID_INPUT`` for an over-wide ID, a reserved
            grantee, the task's owner, or a level outside
            :data:`~omnigent.server.session_sharing.SCHEDULED_TASK_GRANT_LEVELS`;
            ``NOT_FOUND`` when the task is missing or deleted; the account errors
            of :meth:`grant`.
        """
        _require_storable_scheduled_task_grant(user_id, level)

        def write(session: Session) -> None:
            require_active_account(session, user_id)
            _lock_live_scheduled_task(session, scheduled_task_id, user_id)
            session.execute(
                _upsert_grant(
                    self._engine.dialect.name,
                    SqlScheduledTaskShare,
                    {"scheduled_task_id": scheduled_task_id, "user_id": user_id},
                    level,
                )
            )
            # The stored key's collation may match a reserved ID the fast path let through.
            reserved = session.execute(
                select(SqlScheduledTaskShare.user_id).where(
                    SqlScheduledTaskShare.workspace_id == current_workspace_id(),
                    SqlScheduledTaskShare.scheduled_task_id == scheduled_task_id,
                    SqlScheduledTaskShare.user_id == user_id,
                    SqlScheduledTaskShare.user_id.in_(sorted(_RESERVED_GRANTEES)),
                )
            ).first()
            if reserved is not None:
                raise _reserved_grantee_error(user_id)

        run_write_transaction(self._session_immediate, "grant_scheduled_task_permission", write)
        return ScheduledTaskPermission(
            user_id=user_id,
            scheduled_task_id=scheduled_task_id,
            level=level,
        )

    def revoke_for_scheduled_task(self, user_id: str, scheduled_task_id: str) -> bool:
        """Remove a scheduled task grant. See base class for contract."""

        def write(session: Session) -> bool:
            result = cast(
                CursorResult[tuple[object]],
                session.execute(
                    delete(SqlScheduledTaskShare).where(
                        SqlScheduledTaskShare.workspace_id == current_workspace_id(),
                        SqlScheduledTaskShare.scheduled_task_id == scheduled_task_id,
                        SqlScheduledTaskShare.user_id == user_id,
                    )
                ),
            )
            return result.rowcount > 0

        return run_write_transaction(
            self._session_immediate, "revoke_scheduled_task_permission", write
        )

    def list_for_scheduled_task(
        self,
        scheduled_task_id: str,
        *,
        limit: int = 100,
        after_user_id: str | None = None,
    ) -> tuple[list[ScheduledTaskPermission], str | None]:
        """Return a scheduled task's grants with cursor pagination. See base class."""
        with self._session("list_scheduled_task_permissions") as session:
            return _grant_page(
                session,
                SqlScheduledTaskShare,
                SqlScheduledTaskShare.scheduled_task_id == scheduled_task_id,
                _to_scheduled_task_permission,
                limit=limit,
                after_user_id=after_user_id,
            )

    def resolve_scheduled_task_access(
        self,
        principals: Sequence[str],
        scheduled_task_id: str,
    ) -> int | None:
        """Return the highest level any principal holds. See base class for contract."""
        if not principals:
            return None
        with self._session("resolve_scheduled_task_access") as session:
            return session.execute(
                select(func.max(SqlScheduledTaskShare.level)).where(
                    SqlScheduledTaskShare.workspace_id == current_workspace_id(),
                    SqlScheduledTaskShare.scheduled_task_id == scheduled_task_id,
                    SqlScheduledTaskShare.user_id.in_(list(principals)),
                )
            ).scalar_one()

    def delete_for_scheduled_task(self, scheduled_task_id: str) -> int:
        """Delete every grant on a scheduled task, one batch per transaction. See base class."""
        return delete_scheduled_task_grants(
            self._session_immediate, scheduled_task_id, batch_size=self._cleanup_batch_size
        )
