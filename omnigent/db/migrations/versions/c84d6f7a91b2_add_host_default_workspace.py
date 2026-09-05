"""add per-host pinned workspace

Revision ID: c84d6f7a91b2
Revises: gb1b2c3d4e5f
Create Date: 2026-09-06 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84d6f7a91b2"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the optional Host-native folder shortcut."""
    op.add_column(
        "hosts",
        sa.Column("default_workspace", sa.String(length=2048), nullable=True),
    )


def downgrade() -> None:
    """Remove the per-Host folder shortcut."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("default_workspace")
