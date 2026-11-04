"""Add background_task_count to conversation metadata.

Revision ID: a3f8b3a5902c
Revises: d0a1f879a01d

Adds a nullable Integer ``background_task_count`` column to
``omnigent_conversation_metadata``: the number of background shells still
running after a session's turn ended, written by the replica holding the
runner tunnel so any replica's session list can show them.

Additive with no default or backfill; NULL means never reported. The ORM maps
the column, so run this migration before starting a server that includes it.
Servers already running an older binary ignore the column. To roll back to an
older binary, downgrade to d0a1f879a01d first: an older binary refuses to start
on a revision it does not know. Downgrade discards the counts, which the next
turn end reports again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f8b3a5902c"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"


def upgrade() -> None:
    """Add ``background_task_count`` to ``omnigent_conversation_metadata``."""
    op.add_column(_TABLE, sa.Column("background_task_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove ``background_task_count`` from ``omnigent_conversation_metadata``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("background_task_count")
