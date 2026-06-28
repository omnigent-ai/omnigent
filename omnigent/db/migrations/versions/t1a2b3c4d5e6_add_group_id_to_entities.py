"""add group_id to entities

Revision ID: t1a2b3c4d5e6
Revises: s1a2b3c4d5e6
Create Date: 2026-06-28 19:05:00.000000

Adds the nullable ``group_id`` column to ``entities`` — the entity group an
entity belongs to (or NULL if ungrouped). Deliberately a plain column with no
foreign key: an entity may reference a code-owned built-in group id (which has
no row in ``entity_groups``), and EntityGroupStore.delete_group nulls dependent
entities when a user group is deleted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t1a2b3c4d5e6"
down_revision: str | None = "s1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("entities") as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_entities_group_id", ["group_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("entities") as batch_op:
        batch_op.drop_index("ix_entities_group_id")
        batch_op.drop_column("group_id")
