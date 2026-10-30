"""Persist PR coordinates on session review comments.

Revision ID: f9b3e6c2a401
Revises: d0a1f879a01d
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText

revision: str = "f9b3e6c2a401"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add optional immutable PR context without changing file comments."""
    op.add_column(
        "comments",
        sa.Column("review_anchor", CompressedText(max_stored_bytes=16_383), nullable=True),
    )


def downgrade() -> None:
    """Remove PR context."""
    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_column("review_anchor")
