"""Add durable Forum coordinator state.

Revision ID: fm1a2b3c4d5e
Revises: mm1a2b3c4d5e
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision = "fm1a2b3c4d5e"
down_revision = "mm1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forums",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("state", CompressedText(), nullable=False),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_until", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("forums")
