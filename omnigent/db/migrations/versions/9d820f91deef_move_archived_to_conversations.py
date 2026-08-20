"""move archived column from metadata back to conversations

Revision ID: 9d820f91deef
Revises: cc3d4e5f6a7b
Create Date: 2026-07-14 00:00:00.000000

The conversations split (``aa1b2c3d4e5f``) moved ``archived`` onto
``omnigent_conversation_metadata``. That forced ``list_conversations`` to
pre-fetch every non-archived conversation id from the Omnigent DB and filter
the AP query with a giant ``IN (...)``, because the sort keys
(``created_at``/``updated_at``) stayed on ``conversations`` while the filter
moved to the other logical DB.

This migration moves ``archived`` back onto ``conversations`` so the AP query
can filter it inline next to the sort keys. It adds the column, backfills from
``omnigent_conversation_metadata`` via a portable correlated subquery, drops
it from the metadata table, and adds a composite index supporting the default
sidebar (``archived=false ORDER BY updated_at DESC``). ``kind`` intentionally
stays on the metadata table — the list filter now derives it from
``parent_conversation_id`` instead.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9d820f91deef"
down_revision: str | None = "cc3d4e5f6a7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add ``conversations.archived``, backfill it from the metadata table,
    then drop it from ``omnigent_conversation_metadata``.

    ``server_default=sa.false()`` backfills existing rows for the NOT NULL
    add; the subsequent UPDATE overwrites them with the real value copied
    from metadata. Batch mode is used for the column add/drop for SQLite
    compatibility; the backfill uses a correlated subquery so it runs on
    SQLite, MySQL, and PostgreSQL alike (``UPDATE … FROM`` is
    PostgreSQL-only).
    """
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archived",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.execute(
        """
        UPDATE conversations
        SET archived = COALESCE(
            (SELECT m.archived
             FROM omnigent_conversation_metadata m
             WHERE m.workspace_id = conversations.workspace_id
               AND m.id = conversations.id),
            FALSE
        )
        """
    )

    # Default sidebar: archived=false ORDER BY updated_at DESC. archived leads
    # as an equality so the page walk stays index-only.
    op.create_index(
        "ix_conversations_archived_updated",
        "conversations",
        ["workspace_id", "archived", "updated_at", "id"],
    )

    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("archived")


def downgrade() -> None:
    """
    Reverse the move: re-add ``archived`` to the metadata table, backfill it
    from ``conversations``, drop the sidebar index, and drop the column from
    ``conversations``.
    """
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(
            sa.Column(
                "archived",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )

    op.execute(
        """
        UPDATE omnigent_conversation_metadata
        SET archived = COALESCE(
            (SELECT c.archived
             FROM conversations c
             WHERE c.workspace_id = omnigent_conversation_metadata.workspace_id
               AND c.id = omnigent_conversation_metadata.id),
            FALSE
        )
        """
    )

    op.drop_index("ix_conversations_archived_updated", table_name="conversations")

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("archived")
