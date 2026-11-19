"""Add what a scheduled task is for.

Revision ID: 27d16059fab9
Revises: b4e2d7a91c35

``kind`` records whether a task is an ordinary automation (1, the default for
existing tasks) or a reminder an agent set for its own session (2), so clients
can list reminders apart from automations without guessing from a task's name
or schedule. Tasks are read with their row, never filtered by kind in SQL, so
no index is added. Downgrade drops the column, so reminders list as ordinary
automations again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "27d16059fab9"
down_revision: str | None = "b4e2d7a91c35"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the kind, defaulting every existing task to an ordinary automation."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("kind", sa.SmallInteger(), nullable=False, server_default="1"))
        batch.create_check_constraint("ck_scheduled_tasks_kind", "kind IN (1, 2)")


def downgrade() -> None:
    """Drop the kind."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_kind", type_="check")
        batch.drop_column("kind")
