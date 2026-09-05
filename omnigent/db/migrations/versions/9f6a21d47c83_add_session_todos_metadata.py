"""Persist native Plan snapshots on conversation metadata.

Revision ID: 9f6a21d47c83
Revises: gb1b2c3d4e5f
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f6a21d47c83"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable compressed JSON snapshot; existing sessions stay empty."""
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("session_todos", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    """Remove the display snapshot without altering conversation history."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch:
        batch.drop_column("session_todos")
