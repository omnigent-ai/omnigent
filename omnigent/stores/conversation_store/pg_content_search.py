"""Opt-in PostgreSQL trigram fast path for session content search (see
``docs/postgres-session-search.md``): probe an operator-built index instead of scanning.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import weakref
from typing import Literal

from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversationItem, current_workspace_id

_logger = logging.getLogger(__name__)

CONTENT_SEARCH_MODE_ENV = "OMNIGENT_PG_CONTENT_SEARCH"
CONTENT_SEARCH_INDEX = "ix_conversation_items_search_text_gin_trgm"

# Probe row cap. Bounds the heap rows the probe rechecks (each detoasts a full
# ``search_text``) and the size of the ``id IN (...)`` list; beyond it the term
# is common enough for the legacy page-filling query.
CONTENT_SEARCH_PROBE_CAP = 1000

# The index catalog check is re-run at most this often per engine, so an index
# built or dropped while the server runs is picked up without a restart.
_INDEX_CHECK_TTL_S = 60.0

ContentSearchMode = Literal["off", "auto"]

_index_state: weakref.WeakKeyDictionary[Engine, tuple[bool, float]] = weakref.WeakKeyDictionary()
_index_state_lock = threading.Lock()


def content_search_mode() -> ContentSearchMode:
    """Read ``OMNIGENT_PG_CONTENT_SEARCH``; blank or unset means ``off``."""
    raw = os.environ.get(CONTENT_SEARCH_MODE_ENV, "")
    value = raw.strip().lower() or "off"
    if value not in ("off", "auto"):
        raise RuntimeError(f"{CONTENT_SEARCH_MODE_ENV} must be 'auto' or 'off', got {raw!r}.")
    return value  # type: ignore[return-value]


def is_trigram_eligible(query: str) -> bool:
    """A pattern needs a run of three ASCII alphanumerics to yield an index trigram."""
    return re.search(r"[A-Za-z0-9]{3}", query) is not None


def has_content_search_index(session: Session, engine: Engine) -> bool:
    """Return whether a valid trigram index exists, caching the catalog lookup per engine."""
    now = time.monotonic()
    with _index_state_lock:
        cached = _index_state.get(engine)
    if cached is not None and now - cached[1] < _INDEX_CHECK_TTL_S:
        return cached[0]
    valid = session.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "WHERE c.relname = :index_name AND t.relname = 'conversation_items'"
        ),
        {"index_name": CONTENT_SEARCH_INDEX},
    ).scalar()
    present = bool(valid)
    with _index_state_lock:
        _index_state[engine] = (present, now)
    return present


def reset_content_search_index_cache() -> None:
    """Forget cached catalog lookups (tests and the index admin command)."""
    with _index_state_lock:
        _index_state.clear()


def probe_content_matches(
    session: Session,
    pattern: str,
    *,
    cap: int | None = None,
) -> dict[str, int] | None:
    """Return ``{conversation_id: earliest matching position}`` for the ``ILIKE``
    *pattern* via the trigram index, or ``None`` when more than *cap* items match.
    """
    if cap is None:
        cap = CONTENT_SEARCH_PROBE_CAP
    stmt = (
        select(SqlConversationItem.conversation_id, SqlConversationItem.position)
        .where(
            SqlConversationItem.workspace_id == current_workspace_id(),
            SqlConversationItem.search_text.ilike(pattern),
        )
        .limit(cap + 1)
    )
    rows = session.execute(stmt).all()
    if len(rows) > cap:
        _logger.debug("Content search probe overflowed %d rows; using the legacy scan", cap)
        return None
    positions: dict[str, int] = {}
    for conversation_id, position in rows:
        current = positions.get(conversation_id)
        if current is None or position < current:
            positions[conversation_id] = position
    return positions


def _autocommit_connection(engine: Engine) -> Connection:
    # CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction block.
    return engine.connect().execution_options(isolation_level="AUTOCOMMIT")


def build_content_search_index(engine: Engine) -> bool:
    """Create the trigram index (and ``pg_trgm``) concurrently; ``True`` when created,
    ``False`` when a valid one already existed.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("The session content search index requires PostgreSQL.")
    with _autocommit_connection(engine) as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        valid = conn.execute(
            text(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :index_name"
            ),
            {"index_name": CONTENT_SEARCH_INDEX},
        ).scalar()
        if valid is not None and not valid:
            # An interrupted CONCURRENTLY build leaves an INVALID index that
            # IF NOT EXISTS would keep; replace it.
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {CONTENT_SEARCH_INDEX}"))
            valid = None
        if valid:
            return False
        conn.execute(
            text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {CONTENT_SEARCH_INDEX} "
                "ON conversation_items USING gin (search_text gin_trgm_ops)"
            )
        )
    reset_content_search_index_cache()
    return True


def drop_content_search_index(engine: Engine) -> bool:
    """Drop the trigram index if present (``pg_trgm`` stays); ``True`` when one existed."""
    if engine.dialect.name != "postgresql":
        raise ValueError("The session content search index requires PostgreSQL.")
    with _autocommit_connection(engine) as conn:
        existed = (
            conn.execute(
                text("SELECT 1 FROM pg_class WHERE relname = :index_name AND relkind = 'i'"),
                {"index_name": CONTENT_SEARCH_INDEX},
            ).scalar()
            is not None
        )
        conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {CONTENT_SEARCH_INDEX}"))
    reset_content_search_index_cache()
    return existed
