"""Add the per-user background session title preference.

Revision ID: gd1b2c3d4e5f
Revises: gc1b2c3d4e5f
Create Date: 2026-09-08 00:00:00.000000

``NULL`` and ``TRUE`` enable background titles; ``FALSE`` opts out.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gd1b2c3d4e5f"
down_revision: str | None = "gc1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable default-on preference to users."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("background_session_titles_enabled", sa.Boolean(), nullable=True)
        )


def downgrade() -> None:
    """Remove the background session title preference."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("background_session_titles_enabled")
