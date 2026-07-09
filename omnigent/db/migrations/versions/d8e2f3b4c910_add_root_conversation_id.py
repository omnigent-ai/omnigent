"""add root_conversation_id to conversations

Revision ID: d8e2f3b4c910
Revises: a1b2c3d4e5f6
Create Date: 2026-05-28

Adds a ``root_conversation_id`` column to ``conversations`` so any
agent in a spawn tree can address any other by ``conversation_id``
in O(1) (no parent-chain walk). Top-level conversations have
``root_conversation_id == id``; child conversations inherit the
parent's root_id at creation time. Backfilled by walking the
existing ``parent_conversation_id`` chain.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e2f3b4c910"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot ALTER TABLE to add a FOREIGN KEY constraint;
    # alembic's batch mode rebuilds the table under the hood so
    # the migration works on both Postgres and SQLite. The same
    # batch wrapper is used elsewhere in this migration tree.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "root_conversation_id",
                sa.String(length=64),
                sa.ForeignKey(
                    "conversations.id",
                    ondelete="CASCADE",
                    name="fk_conversations_root_conversation_id",
                ),
                nullable=True,
            ),
        )
    op.create_index(
        "ix_conversations_root_conversation_id",
        "conversations",
        ["root_conversation_id"],
    )

    # Backfill top-level rows: root_conversation_id = id when
    # parent_conversation_id is NULL.
    op.execute(
        sa.text(
            "UPDATE conversations SET root_conversation_id = id "
            "WHERE parent_conversation_id IS NULL"
        )
    )

    # Iteratively backfill children whose parent has a populated
    # root_id. Each iteration covers one additional level of the
    # spawn tree; loops until the UPDATE affects zero rows. Bounded
    # by the maximum tree depth, which is small in practice.
    #
    # MySQL does not allow referencing the same table in a subquery
    # inside an UPDATE statement (error 1093). Use a JOIN-based UPDATE
    # for MySQL and the standard subquery form for SQLite/PostgreSQL.
    bind = op.get_bind()
    is_mysql = bind.dialect.name == "mysql"
    if is_mysql:
        backfill_sql = sa.text(
            """
            UPDATE conversations
            JOIN conversations AS parent
              ON parent.id = conversations.parent_conversation_id
            SET conversations.root_conversation_id = parent.root_conversation_id
            WHERE conversations.root_conversation_id IS NULL
              AND conversations.parent_conversation_id IS NOT NULL
              AND parent.root_conversation_id IS NOT NULL
            """
        )
    else:
        backfill_sql = sa.text(
            """
            UPDATE conversations
            SET root_conversation_id = (
                SELECT parent.root_conversation_id
                FROM conversations AS parent
                WHERE parent.id = conversations.parent_conversation_id
            )
            WHERE root_conversation_id IS NULL
              AND parent_conversation_id IS NOT NULL
              AND (
                SELECT parent.root_conversation_id
                FROM conversations AS parent
                WHERE parent.id = conversations.parent_conversation_id
              ) IS NOT NULL
            """
        )
    for _ in range(64):
        result = bind.execute(backfill_sql)
        if result.rowcount == 0:
            break

    # Fail loud if the backfill didn't converge — orphaned rows or
    # a malformed parent chain would otherwise leave permanent NULLs
    # that the runtime then has to paper over with fallbacks.
    remaining = bind.execute(
        sa.text("SELECT COUNT(*) FROM conversations WHERE root_conversation_id IS NULL")
    ).scalar()
    if remaining and remaining > 0:
        raise RuntimeError(
            f"root_conversation_id backfill incomplete: {remaining} rows still NULL. "
            "Likely cause: orphan parent_conversation_id pointing at a deleted row, "
            "or a parent chain deeper than 64 levels. Inspect conversations table "
            "before re-running this migration."
        )

    # Lock in the invariant: every conversation has a root_id. The
    # column was created nullable so the backfill could populate it;
    # now that it's complete, enforce NOT NULL at the schema level
    # so future inserts fail loud rather than rely on application
    # defensiveness.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.alter_column(
            "root_conversation_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    op.drop_index(
        "ix_conversations_root_conversation_id",
        table_name="conversations",
    )
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("root_conversation_id")
