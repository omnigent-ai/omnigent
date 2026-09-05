"""add active_range_start/active_range_end columns to scheduled_tasks

Revision ID: ga2b3c4d5e6f
Revises: ga1b2c3d4e5f
Create Date: 2026-09-03 00:00:00.000000

Adds an optional ``active_range_start`` / ``active_range_end`` pair (both
``VARCHAR(5)``, nullable) to ``scheduled_tasks``: a daily time-of-day window
("HH:MM", 24-hour) that gates which rule occurrences are allowed to fire. Both
NULL means unrestricted (today's behavior); the route is the only writer and
validates that they are always set together, so there is no CHECK constraint
here (SQLite regex CHECKs are unavailable under batch mode).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ga2b3c4d5e6f"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add active_range_start/active_range_end to scheduled_tasks."""
    op.add_column(
        "scheduled_tasks",
        sa.Column("active_range_start", sa.String(5), nullable=True),
    )
    op.add_column(
        "scheduled_tasks",
        sa.Column("active_range_end", sa.String(5), nullable=True),
    )


def downgrade() -> None:
    """Remove active_range_start/active_range_end from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("active_range_start")
        batch_op.drop_column("active_range_end")
