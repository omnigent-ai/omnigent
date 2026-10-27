"""Persist host-requested recovery intent alongside conversation input ordering."""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import CompressedText, Uuid16

revision = "c3f5a7b9d014"
down_revision = "b2e4f6a8c013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_recovery",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("conversation_id", Uuid16(), primary_key=True),
        sa.Column("state", CompressedText(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("session_recovery")
