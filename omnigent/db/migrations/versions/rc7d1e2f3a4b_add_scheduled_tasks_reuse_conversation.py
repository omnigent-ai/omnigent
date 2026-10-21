"""add reuse_conversation column to scheduled_tasks

Revision ID: rc7d1e2f3a4b
Revises: nf12ab34cd56
Create Date: 2026-10-21 00:00:00.000000

Adds ``scheduled_tasks.reuse_conversation``. When true, each firing after the
first sends its prompt into ``last_run_conversation_id`` instead of creating a
new conversation. ``server_default`` of false keeps existing tasks on one
conversation per fire and lets the NOT NULL column apply to a populated table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "rc7d1e2f3a4b"
down_revision: str | None = "nf12ab34cd56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add reuse_conversation to scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "reuse_conversation",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Remove reuse_conversation from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("reuse_conversation")
