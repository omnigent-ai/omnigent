"""Add cost_alerts table.

Revision ID: zb3c4d5e6f7a
Revises: za2b3c4d5e6f
Create Date: 2026-08-21 00:00:00.000000

Adds a ``cost_alerts`` table for per-user spending thresholds. Each alert
defines a USD threshold and a period (daily or monthly) so the usage report
can flag when current spend exceeds the threshold.

Additive. No existing data needs backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zb3c4d5e6f7a"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``cost_alerts`` table."""
    op.create_table(
        "cost_alerts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("threshold_usd", sa.Float(), nullable=False),
        sa.Column("period", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_cost_alerts_user",
        "cost_alerts",
        ["workspace_id", "user_id", "created_at"],
    )


def downgrade() -> None:
    """Drop the ``cost_alerts`` table."""
    op.drop_index("ix_cost_alerts_user", table_name="cost_alerts")
    op.drop_table("cost_alerts")
