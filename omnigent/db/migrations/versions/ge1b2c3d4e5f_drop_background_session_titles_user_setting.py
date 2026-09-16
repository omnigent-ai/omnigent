"""Drop the per-user background session title preference.

Revision ID: ge1b2c3d4e5f
Revises: gd1b2c3d4e5f
Create Date: 2026-09-09 00:00:00.000000

The preference now lives in browser localStorage and is sent as a request header.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ge1b2c3d4e5f"
down_revision: str | None = "gd1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove the unused server-side preference column."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("background_session_titles_enabled")


def downgrade() -> None:
    """Restore the column used by releases before localStorage persistence."""
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("background_session_titles_enabled", sa.Boolean(), nullable=True)
        )
