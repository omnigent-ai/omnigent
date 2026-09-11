"""Make conversation_items.search_text nullable.

Revision ID: gh1b2c3d4e5f
Revises: gg1b2c3d4e5f
Create Date: 2026-09-11 00:00:00.000000

``SqlAlchemyConversationStore._item_search_text`` is a documented seam: a
store whose item ``data`` is opaque (it cannot derive a plaintext body to
index) returns ``None``, and ``append()`` drops ``search_text`` from the
batch INSERT. With the column NOT NULL that whole INSERT aborted
(``NOT NULL constraint failed: conversation_items.search_text``), so relay
persistence silently lost every item on such a store. Nullable reconciles
the seam with the schema; NULL rows are simply never matched by plaintext
session search.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gh1b2c3d4e5f"
down_revision: str | None = "gg1b2c3d4e5f"
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
