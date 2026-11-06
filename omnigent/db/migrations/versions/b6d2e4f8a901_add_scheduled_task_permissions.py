"""Add selected-person automation definition grants.

Revision ID: b6d2e4f8a901
Revises: f1c7a9e2b406
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "b6d2e4f8a901"
down_revision = "f1c7a9e2b406"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_task_permissions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("scheduled_task_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("account_generation", sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "scheduled_task_id", "user_id"),
    )
    op.create_index(
        "ix_scheduled_task_permissions_recipient",
        "scheduled_task_permissions",
        ["workspace_id", "user_id", "scheduled_task_id"],
    )


def downgrade() -> None:
    op.drop_table("scheduled_task_permissions")
