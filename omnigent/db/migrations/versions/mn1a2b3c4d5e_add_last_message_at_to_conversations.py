"""Add the visible-message watermark to conversations.

Revision ID: mn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-05 00:00:00.000000

``updated_at`` also moves for metadata and lifecycle changes, while unread
tracking needs a watermark for content a user can actually read. Existing rows
start at ``updated_at`` so this additive field preserves their current unread
baseline; new writes advance it only for non-meta message items.
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
    """Add and conservatively initialize the visible-message watermark."""
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("conversations")}
    if "last_message_at" not in existing:
        op.add_column(
            "conversations",
            sa.Column("last_message_at", sa.Integer(), nullable=True),
        )
    if bind.dialect.name == "cockroachdb":
        # CockroachDB publishes ADD COLUMN asynchronously. Commit the schema
        # change before the backfill, then run the data write in a fresh
        # serializable transaction so it cannot observe a column mid-backfill.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    op.execute(
        sa.text(
            "UPDATE conversations SET last_message_at = updated_at WHERE last_message_at IS NULL"
        )
    )


def downgrade() -> None:
    """Remove the visible-message watermark."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("last_message_at")
