"""Add the stable session archive timestamp.

Revision ID: gb2b3c4d5e6
Revises: gb1b2c3d4e5f
Create Date: 2026-09-06 00:00:00.000000

Archived sessions already exist in deployed databases. Their server-written
``omnigent.archived_at`` label is the authoritative historical transition time;
fall back to ``updated_at`` only when that legacy value is missing or invalid.
Later archive transitions use the column directly so metadata edits cannot move
archive date filters.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gb2b3c4d5e6"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add, backfill, and index the stable archive timestamp."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.Integer(), nullable=True))
    bind = op.get_bind()
    metadata = sa.MetaData()
    conversations = sa.Table("conversations", metadata, autoload_with=bind)
    labels = sa.Table("conversation_labels", metadata, autoload_with=bind)
    legacy_rows = bind.execute(
        sa.select(conversations.c.workspace_id, conversations.c.id, labels.c.value)
        .select_from(
            conversations.join(
                labels,
                sa.and_(
                    labels.c.workspace_id == conversations.c.workspace_id,
                    labels.c.conversation_id == conversations.c.id,
                ),
            )
        )
        .where(
            conversations.c.archived.is_(True),
            conversations.c.archived_at.is_(None),
            labels.c.key == "omnigent.archived_at",
        )
    ).all()
    valid_updates: list[dict[str, object]] = []
    for workspace_id, conversation_id, raw_value in legacy_rows:
        try:
            timestamp = int(str(raw_value).strip())
        except (TypeError, ValueError):
            continue
        if timestamp <= 0 or timestamp > 2**63 - 1:
            continue
        valid_updates.append(
            {
                "_archive_workspace_id": workspace_id,
                "_archive_conversation_id": conversation_id,
                "_archive_timestamp": timestamp,
            }
        )
    if valid_updates:
        bind.execute(
            conversations.update()
            .where(
                conversations.c.workspace_id == sa.bindparam("_archive_workspace_id"),
                conversations.c.id == sa.bindparam("_archive_conversation_id"),
                conversations.c.archived.is_(True),
                conversations.c.archived_at.is_(None),
            )
            .values(archived_at=sa.bindparam("_archive_timestamp")),
            valid_updates,
        )
    bind.execute(
        conversations.update()
        .where(
            conversations.c.archived.is_(True),
            conversations.c.archived_at.is_(None),
        )
        .values(archived_at=conversations.c.updated_at)
    )
    op.create_index(
        "ix_conversations_archived_archived_at",
        "conversations",
        ["workspace_id", "archived", "archived_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the stable archive timestamp and its index."""
    op.drop_index("ix_conversations_archived_archived_at", table_name="conversations")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("archived_at")
