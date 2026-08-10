"""Trigram GIN indexes so session search stops sequential-scanning content.

Revision ID: d5e9f1a2b3c4
Revises: f7a8b9c0d1e2
Create Date: 2026-08-09 00:00:00.000000

Session search (``GET /v1/sessions?search_query=``) matches a conversation when
``LOWER(title) LIKE '%q%'`` OR any item's ``LOWER(search_text) LIKE '%q%'``. The
leading wildcard makes both predicates unindexable by a b-tree, so on Postgres
the content half sequential-scans every ``conversation_items`` row in the
workspace (and lowercases its wide ``search_text``). On a real deployment that
runs for tens of seconds; the palette has no request timeout, so it hangs on
"Searching…" forever.

``pg_trgm`` fixes this without changing match semantics: a GIN index on the
``LOWER(...)`` expression accelerates the existing substring ``LIKE`` directly
(no switch to ``tsvector``, so the UI's substring highlighting still lines up).
Both sides of the OR are indexed — the content scan is the bottleneck, but
leaving ``title`` unindexed would let the planner fall back to scanning
``conversations`` for the OR.

Postgres-only. SQLite (dev/tests) keeps its FTS5 virtual table and small tables,
so the substring scan there is a non-issue; this migration is a no-op on any
non-Postgres dialect. Plain ``CREATE INDEX`` (not ``CONCURRENTLY``) because the
Alembic runner wraps every migration in a transaction, where ``CONCURRENTLY`` is
illegal — matching every other index migration in this chain.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d5e9f1a2b3c4"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEMS_INDEX = "ix_conversation_items_search_text_trgm"
_TITLE_INDEX = "ix_conversations_title_trgm"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """
    Enable ``pg_trgm`` and add GIN trigram indexes on the lowercased search
    expressions the session-search query uses. No-op off Postgres.
    """
    if not _is_postgres():
        return
    # IF NOT EXISTS so a deployment that already enabled the extension (or
    # pre-created an index) migrates cleanly.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_ITEMS_INDEX} "
        "ON conversation_items USING gin (LOWER(search_text) gin_trgm_ops)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_TITLE_INDEX} "
        "ON conversations USING gin (LOWER(title) gin_trgm_ops)"
    )


def downgrade() -> None:
    """
    Drop the trigram indexes. The ``pg_trgm`` extension is left installed — it is
    cheap, may back other objects, and dropping it could fail if anything else
    depends on it. No-op off Postgres.
    """
    if not _is_postgres():
        return
    op.execute(f"DROP INDEX IF EXISTS {_TITLE_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_ITEMS_INDEX}")
