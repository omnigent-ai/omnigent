"""Add the ``ix_conversations_runner_id`` index.

Revision ID: z2a2b3c4d5e6
Revises: z1a2b3c4d5e6
Create Date: 2026-07-08 02:00:00.000000

Reconnect/relaunch reconciliation looks up a runner's session(s) by
``runner_id`` (``list_conversations_by_runner_id``) on every runner
reconnect. Four server call sites drive that query; without an index
each is a full table scan of ``conversations``. Index ``runner_id`` to
make the lookup selective.

Creating an index is a simple operation on SQLite, PostgreSQL, and
MySQL alike, so no table rebuild / batch mode is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "z2a2b3c4d5e6"
down_revision: str | None = "z1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``ix_conversations_runner_id`` index."""
    op.create_index("ix_conversations_runner_id", "conversations", ["runner_id"])


def downgrade() -> None:
    """Drop the ``ix_conversations_runner_id`` index."""
    op.drop_index("ix_conversations_runner_id", table_name="conversations")
