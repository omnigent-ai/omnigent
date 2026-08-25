"""add harness column to user_daily_cost

Revision ID: zb1a2b3c4d5e
Revises: za1b2c3d4e5f
Create Date: 2026-08-21 00:00:00.000000

Adds a ``harness`` column to ``user_daily_cost`` to enable optional per-harness
budget scoping. The table continues to store daily cost records only
(``day_utc`` as ``"YYYY-MM-DD"``). Period-based policies (week, month, quarter,
year) aggregate daily records at read time rather than pre-computing rollups.

Supports two budget modes:
- **Cross-harness budgets** (default): Use the sentinel value ``"__all__"`` to
  sum cost across all harnesses for a user+day.
- **Per-harness budgets**: Track cost separately for each harness (e.g.
  ``"codex-native"``).

This is a **backward-compatible** additive migration:
- Existing daily-cost rows get ``harness="__all__"`` via the server default
- Existing queries that don't filter by harness will read all rows (cross-harness)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb1a2b3c4d5e"
down_revision: str | None = "za1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add harness column to user_daily_cost and rebuild primary key.

    Uses ALTER TABLE for better performance and less downtime. The new
    harness column defaults to "__all__" for existing rows.
    """
    with op.batch_alter_table("user_daily_cost") as batch_op:
        # Add harness column with default value
        batch_op.add_column(
            sa.Column("harness", sa.String(64), nullable=False, server_default="__all__")
        )
        # Drop old primary key
        batch_op.drop_constraint("pk_user_daily_cost", type_="primary")
        # Add new primary key including harness
        batch_op.create_primary_key(
            "pk_user_daily_cost", ["workspace_id", "user_id", "day_utc", "harness"]
        )


def downgrade() -> None:
    """
    Remove harness column and restore original primary key.

    WARNING: This will DELETE all per-harness rows (harness != "__all__").
    Only cross-harness data will be preserved.
    """
    # Delete per-harness rows before removing the column
    op.execute("DELETE FROM user_daily_cost WHERE harness != '__all__'")

    with op.batch_alter_table("user_daily_cost") as batch_op:
        # Drop current primary key
        batch_op.drop_constraint("pk_user_daily_cost", type_="primary")
        # Recreate original primary key without harness
        batch_op.create_primary_key("pk_user_daily_cost", ["workspace_id", "user_id", "day_utc"])
        # Drop harness column
        batch_op.drop_column("harness")
