"""add cross-device user preferences

Revision ID: c7e4d9a1b2f6
Revises: gb1b2c3d4e5f
Create Date: 2026-09-06 00:00:00.000000

The ORM maps this opaque JSON column through ``CompressedText``. Its database
representation is therefore binary (BLOB/BYTEA), while callers continue to
read and write normal JSON text.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e4d9a1b2f6"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the optional compressed preferences envelope."""
    op.add_column(
        "users",
        sa.Column("preferences", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    """Remove synced user preferences."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("preferences")
