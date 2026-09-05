"""add edited_at column to comments

Revision ID: ha1b2c3d4e5f
Revises: ga1b2c3d4e5f
Create Date: 2026-09-02 00:00:00.000000

Adds a nullable ``edited_at`` (BIGINT, epoch microseconds) column to
``comments``, recording when the comment's body text was last rewritten.
``NULL`` means the text was never edited — including all pre-existing
rows, which is the correct backfill since past body edits were never
distinguished from status changes. Unlike ``updated_at`` it is not
bumped by status-only mutations, so clients can render an "edited"
indicator only when the visible text actually changed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ha1b2c3d4e5f"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add edited_at to comments."""
    op.add_column(
        "comments",
        sa.Column("edited_at", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    """Remove edited_at from comments."""
    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_column("edited_at")
