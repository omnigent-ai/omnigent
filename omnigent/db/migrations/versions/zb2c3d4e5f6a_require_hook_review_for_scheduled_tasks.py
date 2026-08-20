"""Persist scheduled-task hook review state.

Revision ID: zb2c3d4e5f6a
Revises: za2b3c4d5e6f
"""

import sqlalchemy as sa
from alembic import op

revision = "zb2c3d4e5f6a"
down_revision = "za2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks",
        sa.Column("requires_hook_review", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("requires_hook_review")
