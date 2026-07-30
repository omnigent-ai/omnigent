"""add project_monthly_cost table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-30 00:00:00.000001

Adds the ``project_monthly_cost`` table: a per-project, per-UTC-calendar-month
rollup of LLM spend used by the project monthly cost-budget policy to read a
project's accumulated month-to-date cost in O(1) (see ``PLAN.md``, closes
#1662). One row per ``(workspace_id, project_id, month_utc)``, incremented at
each turn boundary alongside ``user_daily_cost``.

Mirrors ``user_daily_cost`` (``cad9b3e1f7a2_add_user_daily_cost_table.py`` +
``d4f1a9c2b8e3_add_ask_approved_to_user_daily_cost.py``) in shape, including
``ask_approved_usd`` from the start (unlike ``user_daily_cost``, which grew it
in a follow-up migration) and the ``workspace_id`` tenant-partition column
that ``user_daily_cost`` only gained via the later
``r1a2b3c4d5e6_add_workspace_id_to_all_tables.py`` retrofit — this table is
created after that retrofit landed, so it carries the column from creation.

This is a brand-new table, so it does not affect deployments whose database
lacks it: the server only ever reads or writes it from policy-gated code
paths, which are inert when no project has a budget configured.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``project_monthly_cost`` table."""
    op.create_table(
        "project_monthly_cost",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("month_utc", sa.String(7), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ask_approved_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "month_utc"),
    )


def downgrade() -> None:
    """Drop the ``project_monthly_cost`` table."""
    op.drop_table("project_monthly_cost")
