"""add share_with column to scheduled_tasks

Revision ID: 57d10bb63551
Revises: a7d4c2e9f610
Create Date: 2026-11-11 00:00:00.000000

Adds a nullable ``share_with`` (VARCHAR(4096)) column to ``scheduled_tasks``: a
JSON list of the principals (users or groups) and permission levels every
session a task creates is shared with, e.g.
``[{"user_id": "team.engineering", "level": 1}]``. ``NULL`` means runs stay
private to the owner, which is how existing tasks behave. The column is never
filtered or indexed, and the store caps each value at 4,096 UTF-8 bytes on
every write.

Additive, with no backfill. Deploy it before the application change that
reads and writes the column; until then nothing uses it. Roll the application
back first and keep the column: a binary that maps it names it in every
insert, so dropping it while such a binary runs breaks task creation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "57d10bb63551"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add share_with to scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("share_with", sa.String(length=4096), nullable=True))


def downgrade() -> None:
    """Remove share_with from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("share_with")
