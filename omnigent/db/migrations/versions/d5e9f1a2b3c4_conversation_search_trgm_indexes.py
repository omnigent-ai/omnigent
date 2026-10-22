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
non-Postgres dialect. The indexes are built with ``CREATE INDEX CONCURRENTLY``
so a large ``conversation_items`` table never blocks writers for the duration of
the GIN build. ``CONCURRENTLY`` is illegal inside a transaction block, so the
builds run in Alembic's ``autocommit_block``, which commits the migration
transaction up to that point — safe here because everything before the block is
idempotent. A concurrent build that fails mid-flight leaves an INVALID index
behind that ``IF NOT EXISTS`` would silently keep, so any such leftover is
dropped before (re)creating each index.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e9f1a2b3c4"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEMS_INDEX = "ix_conversation_items_search_text_trgm"
_TITLE_INDEX = "ix_conversations_title_trgm"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _drop_index_if_invalid(index_name: str) -> None:
    """
    Drop *index_name* if a previously failed ``CONCURRENTLY`` build left it
    INVALID — ``IF NOT EXISTS`` would otherwise keep the unusable leftover.

    :param index_name: Name of the index to check, e.g.
        ``"ix_conversations_title_trgm"``.
    """
    invalid = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": index_name},
        )
        .scalar()
    )
    if invalid:
        op.execute(f"DROP INDEX {index_name}")


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
    # CONCURRENTLY cannot run inside a transaction block; the autocommit block
    # commits the migration transaction so far and runs each statement in
    # autocommit mode, letting writers proceed while the indexes build.
    with op.get_context().autocommit_block():
        for index_name, table, expression in (
            (_ITEMS_INDEX, "conversation_items", "LOWER(search_text)"),
            (_TITLE_INDEX, "conversations", "LOWER(title)"),
        ):
            _drop_index_if_invalid(index_name)
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table} USING gin ({expression} gin_trgm_ops)"
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
