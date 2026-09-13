"""add max_cost_usd column to scheduled_tasks

Revision ID: za1b2c3d4e5f
Revises: za2b3c4d5e6f
Create Date: 2026-08-13 00:00:00.000000

Adds an optional ``max_cost_usd`` (FLOAT, nullable) column to
``scheduled_tasks``. When set, the fire path attaches a ``cost_budget``
policy to each spawned session capping cumulative spend at this limit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za1b2c3d4e5f"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add max_cost_usd to scheduled_tasks."""
    op.add_column(
        "scheduled_tasks",
        sa.Column("max_cost_usd", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Remove max_cost_usd from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("max_cost_usd")
