"""Make conversations.title NOT NULL, back-filling NULLs with empty string.

Revision ID: s1a2b3c4d5e6
Revises: r1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

The ``conversations.title`` column was nullable, using NULL to represent
untitled conversations. This migration converts NULL to empty string so
the column can be declared NOT NULL — keeping the DB constraint tight while
the application layer continues to treat ``''`` and ``None`` as equivalent
at the entity boundary (the store converts between the two).

Upgrade path:
1. Back-fill every NULL title to ``''`` with a plain UPDATE.
2. Alter the column to NOT NULL (batch rebuild on SQLite since it cannot
   alter column constraints in-place; native ALTER on other dialects).
   No PRAGMA foreign_keys guard needed — all FK constraints were removed
   in migration p1a2b3c4d5e6.

Downgrade path:
1. Rebuild the table restoring ``title`` to nullable.
2. Convert every ``''`` title back to NULL so the data looks pre-migration.

Uniqueness semantics across backends
-------------------------------------
``ix_conversations_parent_title_unique`` is ``UNIQUE(parent_conversation_id,
title)`` scoped to rows where ``parent_conversation_id IS NOT NULL`` (partial
index on SQLite/Postgres; full index on MySQL which lacks partial-index support).

The empty-string sentinel (``''``) that now represents untitled conversations
is safe on all backends:

- **Top-level conversations** (``parent_conversation_id = NULL``): the partial
  index excludes them on SQLite/Postgres, and MySQL allows multiple ``(NULL,
  '')`` rows because NULL values are treated as distinct in unique indexes.
- **Sub-agent conversations** always receive a non-empty derived title in
  production (e.g. ``"agent_type:session_id"``), so ``title = ''`` never
  occurs for children — no conflict on any backend.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s1a2b3c4d5e6"
down_revision: str | None = "r1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Back-fill NULL titles to '' and make the column NOT NULL."""
    sqlite = _is_sqlite()

    # Sub-agent children (parent_conversation_id IS NOT NULL) must have a
    # unique title per parent because of ix_conversations_parent_title_unique.
    # In production every sub-agent is created with a derived title
    # (e.g. "agent_type:session_id"), so NULL sub-agent titles should not
    # exist. Guard against any that do by stamping them with a fallback
    # that incorporates the row id, guaranteeing uniqueness.
    op.execute(
        sa.text(
            "UPDATE conversations SET title = 'untitled:' || id"
            " WHERE title IS NULL AND parent_conversation_id IS NOT NULL"
        )
    )

    # Top-level conversations (parent_conversation_id IS NULL) may be untitled;
    # they are not covered by the partial unique index so '' is safe for all.
    op.execute(sa.text("UPDATE conversations SET title = '' WHERE title IS NULL"))

    with op.batch_alter_table(
        "conversations", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.alter_column(
            "title",
            existing_type=sa.Text(),
            nullable=False,
            # MySQL doesn't allow DEFAULT on TEXT columns; omit server_default
            # there — all rows have been back-filled above so no default needed.
            server_default="" if op.get_bind().dialect.name != "mysql" else None,
        )


def downgrade() -> None:
    """Restore title to nullable and convert '' back to NULL."""
    sqlite = _is_sqlite()

    with op.batch_alter_table(
        "conversations", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.alter_column(
            "title",
            existing_type=sa.Text(),
            nullable=True,
            server_default=None,
        )

    # Restore empty-string titles to NULL so data looks pre-migration.
    op.execute(sa.text("UPDATE conversations SET title = NULL WHERE title = ''"))
