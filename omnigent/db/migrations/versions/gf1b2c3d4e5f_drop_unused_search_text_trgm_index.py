"""Drop the unused conversation item content trigram index.

Revision ID: gf1b2c3d4e5f
Revises: ge1b2c3d4e5f
Create Date: 2026-09-03 00:00:00.000000

Conversation content search deliberately uses raw ``ILIKE`` inside a
conversation-correlated probe. This keeps the planner on the compact
``(workspace_id, conversation_id)`` index instead of scanning the global
lowercased-content GIN index. The GIN index therefore consumes space and write
amplification without serving the current query.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "gf1b2c3d4e5f"
down_revision: str | None = "ge1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_items_search_text_trgm"


def upgrade() -> None:
    """Remove the unused content trigram index on PostgreSQL-family databases."""
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        # A plain DROP INDEX takes an ACCESS EXCLUSIVE lock on
        # conversation_items for the drop's duration; CONCURRENTLY avoids
        # stalling writers, matching the concurrent build in d5e9f1a2b3c4.
        # It cannot run inside a transaction, hence the autocommit block
        # (safe: the statement is idempotent via IF EXISTS).
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
    elif dialect == "cockroachdb":
        # CockroachDB performs index drops online as a schema change.
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")


def downgrade() -> None:
    """Restore the legacy content trigram index (Postgres only)."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {_INDEX} ON conversation_items "
            "USING gin (LOWER(search_text) gin_trgm_ops)"
        )
