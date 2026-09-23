"""Add source_metadata to files table.

Revision ID: hh2c3d4e5f6a
Revises: gh1b2c3d4e5f
Create Date: 2026-09-14

Adds a nullable JSON-text column recording metadata about the original upload
before any server-side transform. Today only images that were downscaled at
upload populate it (``{"width", "height"}`` with the pre-downscale pixel size),
used to tell the model the stored image is a reduced-resolution version (resize
notice). Non-image / not-downscaled files leave it NULL, and new file types can
add keys without another migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "hh2c3d4e5f6a"
down_revision: str | None = "gh1b2c3d4e5f"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable source_metadata column to files."""
    op.add_column("files", sa.Column("source_metadata", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the source_metadata column."""
    with op.batch_alter_table("files") as batch_op:
        batch_op.drop_column("source_metadata")
