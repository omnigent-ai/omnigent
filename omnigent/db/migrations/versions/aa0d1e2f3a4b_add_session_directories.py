"""Add stable multi-directory metadata to sessions.

Revision ID: aa0d1e2f3a4b
Revises: d5e9f1a2b3c4
Create Date: 2026-07-28 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa0d1e2f3a4b"
down_revision: str | None = "d5e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the compressed JSON directory-set column."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("directories", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Remove multi-directory metadata."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("directories")
