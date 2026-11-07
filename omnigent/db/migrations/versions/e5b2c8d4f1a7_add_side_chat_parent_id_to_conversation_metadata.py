"""add side_chat_parent_id to omnigent_conversation_metadata

Revision ID: e5b2c8d4f1a7
Revises: d0a1f879a01d
Create Date: 2026-11-04 00:00:00.000000

Adds a nullable ``side_chat_parent_id`` (Uuid16) to
``omnigent_conversation_metadata``: the session whose Workspace rail lists
the side chat. ``NULL`` means the session is not a side chat. The index
lets any of the user's devices list a session's side chats in id order.

Additive, with no backfill: existing side chats keep their current
behavior, and older binaries ignore the column. There is no foreign-key
constraint (schema Rule R032); the application maintains the relationship.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e5b2c8d4f1a7"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``side_chat_parent_id`` and its tenant-scoped lookup index."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("side_chat_parent_id", Uuid16(), nullable=True))
    op.create_index(
        "ix_conversation_metadata_side_chat_parent_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "side_chat_parent_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the index and ``side_chat_parent_id`` column."""
    op.drop_index(
        "ix_conversation_metadata_side_chat_parent_id",
        table_name="omnigent_conversation_metadata",
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("side_chat_parent_id")
