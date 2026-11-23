"""Add shared session document references, without defaults or a backfill.

Revision ID: c6a8e2f401bd
Revises: bf30d7cbda61

Deploy before writers. Existing private preferences remain untouched.
Downgrade discards shared references; Google documents are unaffected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c6a8e2f401bd"
down_revision: str | None = "bf30d7cbda61"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("session_documents", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("session_documents")
