"""Save portable preference snapshots with sessions.

Revision ID: ps1a2b3c4d5e
Revises: nf12ab34cd56
Create Date: 2026-10-21
"""

import sqlalchemy as sa
from alembic import op

revision = "ps1a2b3c4d5e"
down_revision = "nf12ab34cd56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("preference_snapshot", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("preference_snapshot")
