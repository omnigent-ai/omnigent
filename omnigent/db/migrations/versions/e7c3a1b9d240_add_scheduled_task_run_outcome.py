"""Add the outcome an agent records for a scheduled-task run.

Revision ID: e7c3a1b9d240
Revises: d0a1f879a01d

A run's ``status`` says whether it finished, not whether it reached its goal.
These nullable columns hold what the agent recorded with
``sys_scheduled_task_record_outcome``: whether it succeeded, an emoji, a few
words, and when it recorded them. Existing runs keep NULL (nothing recorded).
Downgrade drops the columns and every recorded outcome with them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a1b9d240"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable outcome columns."""
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.add_column(sa.Column("outcome_success", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("outcome_emoji", sa.String(16), nullable=True))
        batch.add_column(sa.Column("outcome_text", sa.String(80), nullable=True))
        batch.add_column(sa.Column("outcome_recorded_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Drop the outcome columns."""
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.drop_column("outcome_recorded_at")
        batch.drop_column("outcome_text")
        batch.drop_column("outcome_emoji")
        batch.drop_column("outcome_success")
