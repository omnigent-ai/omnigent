"""add harness column to user_daily_cost

Revision ID: zb1a2b3c4d5e
Revises: za1b2c3d4e5f
Create Date: 2026-08-21 00:00:00.000000

Extends ``user_daily_cost`` to support both daily and other period granularities
(week, month, quarter, year) with optional per-harness budget scoping.

Adds a ``harness`` column to the primary key to enable two budget modes:
- **Cross-harness budgets**: Use the sentinel value ``"__all__"`` (the new
  default) to sum cost across all harnesses for a user+period.
- **Per-harness budgets**: Track cost separately for each harness (e.g.
  ``"codex-native"``).

The ``day_utc`` column is reused for all period granularities:
- Day: ``"YYYY-MM-DD"`` (e.g. ``"2026-08-21"``, existing format)
- Week: ``"YYYY-Www"`` (e.g. ``"2026-W34"``, ISO week)
- Month: ``"YYYY-MM"`` (e.g. ``"2026-08"``)
- Quarter: ``"YYYY-Qq"`` (e.g. ``"2026-Q3"``)
- Year: ``"YYYY"`` (e.g. ``"2026"``)

This is a **backward-compatible** additive migration:
- Existing daily-cost rows get ``harness="__all__"`` via the server default
- Existing queries that don't filter by harness will read all rows (cross-harness)
- New period policies can use the same table for week/month/quarter/year rollups

The write path is **unconditional** (every priced turn issues UPSERTs for
day/week/month/quarter/year regardless of policies configured), so deployments
must run ``alembic upgrade head`` before this code ships to avoid errors.
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

    Manually recreates the table with the new schema for cross-database
    compatibility. This ensures the primary key is correctly updated.
    """
    # Create new table with harness in PK
    op.create_table(
        "user_daily_cost_new",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("day_utc", sa.String(10), nullable=False),
        sa.Column("harness", sa.String(64), nullable=False, server_default="__all__"),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("ask_approved_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "day_utc", "harness"),
    )

    # Copy existing data, adding harness='__all__' to all rows
    op.execute(
        """
        INSERT INTO user_daily_cost_new
            (workspace_id, user_id, day_utc, harness, cost_usd, ask_approved_usd, updated_at)
        SELECT
            workspace_id, user_id, day_utc, '__all__', cost_usd, ask_approved_usd, updated_at
        FROM user_daily_cost
        """
    )

    # Drop old table
    op.drop_table("user_daily_cost")

    # Rename new table to original name
    op.rename_table("user_daily_cost_new", "user_daily_cost")


def downgrade() -> None:
    """
    Remove harness column and restore original primary key.

    WARNING: This will DELETE all per-harness rows (harness != "__all__").
    Only cross-harness data will be preserved.
    """
    # Create table without harness column
    op.create_table(
        "user_daily_cost_old",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("day_utc", sa.String(10), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("ask_approved_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "day_utc"),
    )

    # Copy only cross-harness data
    op.execute(
        """
        INSERT INTO user_daily_cost_old
            (workspace_id, user_id, day_utc, cost_usd, ask_approved_usd, updated_at)
        SELECT
            workspace_id, user_id, day_utc, cost_usd, ask_approved_usd, updated_at
        FROM user_daily_cost
        WHERE harness = '__all__'
        """
    )

    # Drop current table
    op.drop_table("user_daily_cost")

    # Rename old table back
    op.rename_table("user_daily_cost_old", "user_daily_cost")
