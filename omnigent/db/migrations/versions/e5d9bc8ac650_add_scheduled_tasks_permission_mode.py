"""add permission_mode column to scheduled_tasks

Revision ID: e5d9bc8ac650
Revises: za1b2c3d4e5f
Create Date: 2026-08-24 00:00:00.000000

Adds an optional ``permission_mode`` (VARCHAR(32), nullable) column to
``scheduled_tasks``. When set, the fire path turns it into the runner's
``--permission-mode`` terminal launch arg for native coding harnesses that
support one (Claude Code). NULL = use the agent's configured default.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5d9bc8ac650"
down_revision: str | None = "za1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add permission_mode to scheduled_tasks."""
    op.add_column(
        "scheduled_tasks",
        sa.Column("permission_mode", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    """Remove permission_mode from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("permission_mode")
