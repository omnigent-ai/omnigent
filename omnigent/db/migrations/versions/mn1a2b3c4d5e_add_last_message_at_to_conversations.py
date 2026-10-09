"""Add nullable visible-message watermark storage.

Revision ID: mn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-05 00:00:00.000000

Existing rows remain nullable legacy rows. New application writers initialize
the field to zero and advance it only for visible messages.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable storage without scanning or backfilling existing rows."""
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("conversations")}
    if "last_message_at" not in existing:
        op.add_column("conversations", sa.Column("last_message_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove watermark storage after all schema-aware binaries are stopped."""
    existing = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversations")
    }
    if "last_message_at" in existing:
        with op.batch_alter_table("conversations") as batch:
            batch.drop_column("last_message_at")
