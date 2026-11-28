"""Add the notifications choice to scheduled tasks.

Revision ID: 8e1a06fad781
Revises: 47d3e8fc3e25

``notifications`` records whether the sessions a task's runs start notify its
owner; ``false`` starts them muted. It is ``NOT NULL`` with a ``true`` server
default, so every existing task keeps notifying. Tasks are read with their row,
never filtered by it in SQL, so no index is added.

Additive and unused by earlier releases, so this schema can ship before the
application code that reads it. Downgrade drops the column and every stored
choice with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "8e1a06fad781"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the choice, on for every existing task."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(
            sa.Column("notifications", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    """Drop the choice."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("notifications")
