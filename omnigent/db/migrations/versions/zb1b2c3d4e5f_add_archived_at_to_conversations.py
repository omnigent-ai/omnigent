"""Add archived_at timestamp to conversations.

Revision ID: zb1b2c3d4e5f
Revises: za2b3c4d5e6f
Create Date: 2026-08-10 00:00:00.000000

Records when a session was archived so retention policies can age out old
archived sessions.  Backfills existing archived rows with their current
``updated_at`` value as a best-effort approximation of the archive time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb1b2c3d4e5f"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.Integer(), nullable=True))

    # Backfill: approximate archive time with updated_at for already-archived rows.
    conversations = sa.table(
        "conversations",
        sa.column("archived", sa.Boolean),
        sa.column("archived_at", sa.Integer),
        sa.column("updated_at", sa.Integer),
    )
    op.execute(
        conversations.update()
        .where(conversations.c.archived == sa.true())
        .where(conversations.c.archived_at.is_(None))
        .values(archived_at=conversations.c.updated_at)
    )


def downgrade() -> None:
    with op.batch_alter_table("conversations", recreate="auto") as batch_op:
        batch_op.drop_column("archived_at")
