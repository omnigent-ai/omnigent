"""Add task_summary to conversation metadata.

Revision ID: za2b3c4d5e6f
Revises: d5e9f1a2b3c4
Create Date: 2026-08-10 00:00:00.000000

Adds a nullable ``task_summary`` column to ``omnigent_conversation_metadata``.
Sub-agent sessions use this column to store a human-readable, task-derived label
(e.g. "Investigate auth token refresh") generated asynchronously by the
background title coordinator. The structured title
(``"{agent_type}:{agent_type}-{ordinal}"``) stays in ``conversations.title`` as
the stable spawn-or-continue key; ``task_summary`` is purely presentational.

Additive. No existing data needs backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za2b3c4d5e6f"
down_revision: str | None = "d5e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``task_summary`` to ``omnigent_conversation_metadata``.

    Idempotent: skips the DDL when the column already exists (e.g. added
    outside Alembic by a hotfix), so re-running upgrade does not crash with
    ``duplicate column name: task_summary``.
    """
    existing = {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("omnigent_conversation_metadata")
    }
    if "task_summary" not in existing:
        op.add_column(
            "omnigent_conversation_metadata",
            sa.Column("task_summary", sa.String(128), nullable=True),
        )


def downgrade() -> None:
    """Remove ``task_summary`` from ``omnigent_conversation_metadata``."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("task_summary")
