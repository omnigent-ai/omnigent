"""add user_period_cost table

Revision ID: zb1a2b3c4d5e
Revises: za2b3c4d5e6f
Create Date: 2026-08-21 00:00:00.000000

Adds the ``user_period_cost`` table: a per-user, per-period,
per-harness rollup of LLM spend used by cost-aware policies to read a
user's accumulated cost for any time period in O(1). One row per
``(user_id, period, harness)``, incremented at each turn boundary.

The ``period`` column supports multiple granularities:
- Day: ``"YYYY-MM-DD"`` (e.g. ``"2026-08-21"``)
- Week: ``"YYYY-Www"`` (e.g. ``"2026-W34"``, ISO week)
- Month: ``"YYYY-MM"`` (e.g. ``"2026-08"``)
- Quarter: ``"YYYY-Qq"`` (e.g. ``"2026-Q3"``)
- Year: ``"YYYY"`` (e.g. ``"2026"``)

The ``harness`` column (nullable) enables two budget modes:
- Per-harness budgets: read cost for a specific harness (e.g.
  ``harness="codex-native"``).
- Cross-harness budgets: sum cost across all harnesses for a user+period
  (``GROUP BY user_id, period``).

This is a brand-new table (not a column on existing tables), so it does
not affect deployments whose database lacks it: the server only ever
reads or writes it from policy-gated code paths, which are inert when no
policy is configured.
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
    """Create the ``user_period_cost`` table."""
    op.create_table(
        "user_period_cost",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("harness", sa.String(64), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ask_approved_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "period", "harness"),
    )


def downgrade() -> None:
    """Drop the ``user_period_cost`` table."""
    op.drop_table("user_period_cost")
