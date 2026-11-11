"""add muted column to scheduled_tasks

Revision ID: b5d8e1f3a7c2
Revises: a7d4c2e9f610
Create Date: 2026-11-11 00:00:00.000000

Adds a NOT NULL ``muted`` (BOOLEAN, server default false) column to
``scheduled_tasks``: whether every session the task fires starts muted for its
owner. The server default backfills existing tasks to ``false``, which is how
they behave today. The column is never filtered or indexed.

Additive. Deploy it before the application change that reads and writes the
column; until then nothing uses it. Roll the application back first and keep
the column: a binary that maps it names it in every insert, so dropping it
while such a binary runs breaks task creation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d8e1f3a7c2"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add muted to scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("muted", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Remove muted from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("muted")
