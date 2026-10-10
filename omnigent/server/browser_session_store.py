"""Durable record of browser sessions ended by logout.

A browser session cookie carries a ``sid`` claim that survives sliding
renewal (see :meth:`omnigent.server.auth.UnifiedAuthProvider.renew_browser_session`).
Logout records that ``sid`` here, in the database every replica shares, so a
copy of the cookie cannot authenticate or be renewed on another replica or
after a restart. Rows are kept until the session's absolute expiry, after
which no token of it can be valid anyway; each logout then purges a bounded
batch of them.

Sibling to :class:`omnigent.server.device_grant_store.DeviceGrantStore` —
same database, separate API surface.
"""

from __future__ import annotations

import time

from sqlalchemy import Select, and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlBrowserSessionRevocation, current_workspace_id
from omnigent.db.query_context import query_name_scope
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)

# Matches the ``sid`` column width; the server never mints longer ids.
_MAX_SID_LENGTH = 64
# Matches the ``user_id`` column width, enforced here because SQLite doesn't.
_MAX_USER_ID_LENGTH = 128
# Lapsed rows a logout deletes. Each logout adds at most one row, so any
# batch above one drains a backlog while keeping every transaction bounded.
_PURGE_BATCH_SIZE = 100


def _lapsed_sids_query(workspace_id: int, now: int) -> Select[tuple[str]]:
    """The next batch of lapsed ``sid`` values of *workspace_id*, oldest first.

    Walks ``ix_browser_session_revocations_expires_at`` in index order, so
    it scans at most :data:`_PURGE_BATCH_SIZE` entries.
    """
    return (
        select(SqlBrowserSessionRevocation.sid)
        .where(
            SqlBrowserSessionRevocation.workspace_id == workspace_id,
            SqlBrowserSessionRevocation.expires_at <= now,
        )
        .order_by(
            SqlBrowserSessionRevocation.expires_at.asc(),
            SqlBrowserSessionRevocation.sid.asc(),
        )
        .limit(_PURGE_BATCH_SIZE)
    )


def _purge_lapsed_batch(session: Session, workspace_id: int, now: int) -> int:
    """Delete up to :data:`_PURGE_BATCH_SIZE` lapsed rows of *workspace_id*.

    The delete touches only the primary keys the bounded selection found.
    """
    lapsed = session.scalars(_lapsed_sids_query(workspace_id, now)).all()
    if lapsed:
        session.execute(
            delete(SqlBrowserSessionRevocation).where(
                and_(
                    SqlBrowserSessionRevocation.workspace_id == workspace_id,
                    SqlBrowserSessionRevocation.sid.in_(lapsed),
                    SqlBrowserSessionRevocation.expires_at <= now,
                )
            )
        )
    return len(lapsed)


class BrowserSessionRevocationStore:
    """SQLAlchemy-backed set of ended browser-session ids.

    :param storage_location: SQLAlchemy database URI. Shares the
        connection pool with the other stores via
        :func:`get_or_create_engine`.
    """

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.browser_session_store",
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.browser_session_store",
            immediate=True,
        )

    def revoke(self, sid: str, *, user_id: str, expires_at: int) -> None:
        """Record *sid* as ended until *expires_at*, purging a batch of lapsed rows.

        Idempotent: ending an already-ended session is a no-op.

        :param sid: The session's ``sid`` claim.
        :param user_id: The session's user, e.g. ``"alice"``; kept for
            audit and cut to the column width.
        :param expires_at: Unix epoch seconds of the session's absolute
            expiry (``auth_time`` + max lifetime).
        """
        if len(sid) > _MAX_SID_LENGTH:
            return
        user_id = user_id[:_MAX_USER_ID_LENGTH]
        now = int(time.time())

        def write(session: Session) -> None:
            workspace_id = current_workspace_id()
            with query_name_scope("omnigent.browser_session_store.purge_lapsed_browser_sessions"):
                _purge_lapsed_batch(session, workspace_id, now)
            if expires_at <= now:
                return
            if session.get(SqlBrowserSessionRevocation, (workspace_id, sid)) is None:
                session.add(
                    SqlBrowserSessionRevocation(
                        workspace_id=workspace_id,
                        sid=sid,
                        user_id=user_id,
                        revoked_at=now,
                        expires_at=expires_at,
                    )
                )

        try:
            run_write_transaction(self._session_immediate, "revoke_browser_session", write)
        except IntegrityError:
            # A concurrent logout of the same session inserted the row first.
            if not self.is_revoked(sid):
                raise

    def is_revoked(self, sid: str) -> bool:
        """Return True if *sid* was ended by logout and has not yet lapsed."""
        if len(sid) > _MAX_SID_LENGTH:
            return False
        with self._session("select_browser_session_revocation") as session:
            row = session.get(SqlBrowserSessionRevocation, (current_workspace_id(), sid))
            return row is not None and row.expires_at > int(time.time())
