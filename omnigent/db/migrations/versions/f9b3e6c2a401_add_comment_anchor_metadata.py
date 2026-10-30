"""Persist provider-specific anchor metadata on session comments.

Revision ID: f9b3e6c2a401
Revises: e5b2c8d4f1a7
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText

revision: str = "f9b3e6c2a401"
down_revision: str | None = "e5b2c8d4f1a7"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add optional immutable anchor context without changing file comments."""
    op.add_column(
        "comments",
        sa.Column("anchor_metadata", CompressedText(max_stored_bytes=16_383), nullable=True),
    )


def downgrade() -> None:
    """Remove provider-specific anchor context."""
    with op.batch_alter_table("comments") as batch_op:
        batch_op.drop_column("anchor_metadata")
