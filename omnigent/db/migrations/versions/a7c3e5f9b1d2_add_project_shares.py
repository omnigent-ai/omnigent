"""Add project shares: users an owner shared a project with.

Revision ID: a7c3e5f9b1d2
Revises: f3b8d2e6a1c7

Additive: old code ignores the new table. Downgrade drops the shares.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a7c3e5f9b1d2"
down_revision: str | None = "f3b8d2e6a1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_shares",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "user_id"),
    )
    op.create_index(
        "ix_project_shares_user", "project_shares", ["workspace_id", "user_id", "project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_shares_user", table_name="project_shares")
    op.drop_table("project_shares")
