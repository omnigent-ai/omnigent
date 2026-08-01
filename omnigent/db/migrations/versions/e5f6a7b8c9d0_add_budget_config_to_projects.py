"""add budget_config to projects

Revision ID: e5f6a7b8c9d0
Revises: b3c4d5e6f7a8
Create Date: 2026-07-30 00:00:00.000000

Adds a nullable ``budget_config`` text column to ``projects`` holding a
compact JSON object (``{"limit_usd": ..., "ask_thresholds_usd": [...]}``) for
the project's optional monthly spend budget (see ``PLAN.md``, closes #1662).
``NULL`` means "no budget configured" (unlimited) — this is an opt-in field,
so every pre-existing project is unaffected until its owner sets a limit.

Mirrors the existing ``config`` column added in
``b3c4d5e6f7a8_add_config_to_projects.py``: an opaque, client/policy-owned
JSON blob that the backend persists and reflects back whole, never filtered
in SQL.

Additive: a nullable column with no default, so an older server binary
reading the migrated DB simply ignores it. Rollback is a clean
``downgrade()`` (drops the column) since no existing data is rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``budget_config`` column to ``projects``."""
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("budget_config", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``budget_config`` column."""
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("budget_config")
