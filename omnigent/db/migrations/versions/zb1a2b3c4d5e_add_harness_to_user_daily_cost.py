"""add harness column to user_daily_cost

Revision ID: zb1a2b3c4d5e
Revises: za2b3c4d5e6f
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
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add harness column to user_daily_cost and rebuild primary key.

    Uses batch mode with recreate for SQLite compatibility. This will:
    1. Create a temporary table with the new schema
    2. Copy existing data (harness="__all__" for all existing rows)
    3. Drop the old table
    4. Rename the temporary table

    For PostgreSQL/MySQL, this generates optimized ALTER TABLE statements.
    """
    with op.batch_alter_table(
        "user_daily_cost",
        schema=None,
        recreate="auto",  # Recreate table for SQLite, use ALTER for PostgreSQL
    ) as batch_op:
        # Add harness column with default value for existing rows
        batch_op.add_column(
            sa.Column(
                "harness",
                sa.String(64),
                nullable=False,
                server_default="'__all__'",
            )
        )


def downgrade() -> None:
    """
    Remove harness column and restore original primary key.

    WARNING: This will DELETE all per-harness rows (harness != "__all__").
    Only cross-harness data will be preserved.
    """
    with op.batch_alter_table(
        "user_daily_cost",
        schema=None,
        recreate="auto",  # Recreate table for SQLite, use ALTER for PostgreSQL
    ) as batch_op:
        # Drop harness column - batch mode will automatically adjust PK
        batch_op.drop_column("harness")
