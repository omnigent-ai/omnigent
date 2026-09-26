"""Make conversation_items.search_text nullable.

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-09-25 00:00:00.000000

Opaque-data stores return None from _item_search_text and omit the column
on insert. Nullable rows preserve those items while excluding them from FTS."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the NOT NULL constraint on conversation_items.search_text."""
    # Batch mode: SQLite cannot alter nullability in place (rebuilds the
    # table); PostgreSQL/MySQL run a plain ALTER.
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.alter_column(
            "search_text",
            existing_type=sa.Text(),
            nullable=True,
        )


def downgrade() -> None:
    """Restore NOT NULL, backfilling NULL rows with an empty string."""
    op.execute("UPDATE conversation_items SET search_text = '' WHERE search_text IS NULL")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.alter_column(
            "search_text",
            existing_type=sa.Text(),
            nullable=False,
        )
