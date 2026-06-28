"""add entity_groups table

Revision ID: s1a2b3c4d5e6
Revises: r1a2b3c4d5e6
Create Date: 2026-06-28 19:00:00.000000

Adds the ``entity_groups`` table — named, icon-bearing categories for entities,
shown in the flow builder's step picker. Holds only user-created groups; the
built-in Jira/GitHub groups are code-owned (see omnigent/entities/builtins.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s1a2b3c4d5e6"
down_revision: str | None = "r1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_groups",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("icon_key", sa.String(length=64), nullable=True),
        sa.Column("icon_artifact_key", sa.String(length=256), nullable=True),
        sa.Column("icon_content_type", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_entity_groups_created_by_updated_at",
        "entity_groups",
        ["created_by", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_entity_groups_updated_at", "entity_groups", ["updated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_entity_groups_updated_at", table_name="entity_groups")
    op.drop_index("ix_entity_groups_created_by_updated_at", table_name="entity_groups")
    op.drop_table("entity_groups")
