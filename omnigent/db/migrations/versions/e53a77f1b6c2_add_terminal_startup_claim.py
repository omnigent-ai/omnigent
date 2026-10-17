"""add independent terminal startup ownership

Revision ID: e53a77f1b6c2
Revises: d42f66e0a5b1
Create Date: 2026-10-17 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e53a77f1b6c2"
down_revision: str | None = "d42f66e0a5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("terminal_startup_claim", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("terminal_startup_claim")
