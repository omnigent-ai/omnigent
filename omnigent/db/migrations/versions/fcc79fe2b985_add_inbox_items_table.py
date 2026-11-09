"""add inbox_items table

Revision ID: fcc79fe2b985
Revises: f1c7a9e2b406
Create Date: 2026-11-09 00:00:00.000000

Adds the ``inbox_items`` table: suggestions an agent posts to its owner's
Inbox. ``workspace_id`` leads the primary key and there are no foreign keys
(schema Rule R032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "fcc79fe2b985"
down_revision: str | None = "f1c7a9e2b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``inbox_items`` table and its indexes."""
    op.create_table(
        "inbox_items",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("source_session_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        # INBOX_ITEM_KIND: followup=1, info=2, reminder=3.
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        # Free text stored compressed (CompressedText -> LargeBinary).
        sa.Column("body_md", sa.LargeBinary(), nullable=False),
        sa.Column("links", sa.LargeBinary(), nullable=False),
        sa.Column("suggested_prompt", sa.LargeBinary(), nullable=True),
        sa.Column("primary_link", sa.String(2048), nullable=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        # INBOX_ITEM_STATE: unread=1, read=2, done=3, dismissed=4.
        sa.Column("state", sa.SmallInteger(), nullable=False, server_default="1"),
        # INBOX_ITEM_FEEDBACK: none=0, useful=1, not_useful=2.
        sa.Column("feedback", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("feedback_text", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("read_at", sa.BigInteger(), nullable=True),
        sa.Column("acted_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("kind IN (1, 2, 3)", name="ck_inbox_items_kind"),
        sa.CheckConstraint("state IN (1, 2, 3, 4)", name="ck_inbox_items_state"),
        sa.CheckConstraint("feedback IN (0, 1, 2)", name="ck_inbox_items_feedback"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_inbox_items_user_created",
        "inbox_items",
        ["workspace_id", "user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_inbox_items_user_dedupe_key",
        "inbox_items",
        ["workspace_id", "user_id", "dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``inbox_items`` table."""
    op.drop_index("uq_inbox_items_user_dedupe_key", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_created", table_name="inbox_items")
    op.drop_table("inbox_items")
