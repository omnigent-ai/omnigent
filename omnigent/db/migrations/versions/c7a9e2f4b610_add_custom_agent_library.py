"""Add an owner-scoped custom Agent library.

Revision ID: c7a9e2f4b610
Revises: gb1b2c3d4e5f
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a9e2f4b610"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_agents",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(256), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.LargeBinary(), nullable=True),
        sa.Column("harness", sa.String(128), nullable=False),
        sa.Column("model", sa.String(512), nullable=True),
        sa.Column("bundle_location", sa.String(512), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_custom_agents_owner", "custom_agents", ["workspace_id", "owner_id", "deleted_at"]
    )


def downgrade() -> None:
    op.drop_table("custom_agents")
