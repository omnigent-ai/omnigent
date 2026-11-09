"""add inbox_items table

Revision ID: fcc79fe2b985
Revises: f1c7a9e2b406
Create Date: 2026-11-09 00:00:00.000000

Adds the ``inbox_items`` table: suggestions an agent posts to its owner's
Inbox. ``workspace_id`` leads the primary key, there are no foreign keys
(schema Rule R032), and no column has a SQL default: the application supplies
every value. The table is new and unused until the Inbox item routes ship, so
deploying it first, and rolling it back by downgrade before those routes ship,
is safe.
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
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("session_slot", sa.SmallInteger(), nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        # INBOX_ITEM_KIND: followup=1, info=2, reminder=3.
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        # Compressed text, capped at 16,383 stored bytes by the model.
        sa.Column("body_md", sa.LargeBinary(), nullable=False),
        sa.Column("links", sa.LargeBinary(), nullable=False),
        sa.Column("actions", sa.LargeBinary(), nullable=False),
        sa.Column("chart", sa.LargeBinary(), nullable=True),
        sa.Column("primary_link", sa.String(2048), nullable=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        # INBOX_ITEM_STATE: unread=1, read=2, done=3, dismissed=4.
        sa.Column("state", sa.SmallInteger(), nullable=False),
        # INBOX_ITEM_RESOLUTION: none=0, action_taken=1, already_done=2, useful=3,
        # not_relevant=4, incorrect=5, not_now=6.
        sa.Column("resolution", sa.SmallInteger(), nullable=False),
        sa.Column("resolution_action", sa.SmallInteger(), nullable=True),
        sa.Column("feedback_text", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("read_at", sa.BigInteger(), nullable=True),
        sa.Column("acted_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("kind IN (1, 2, 3)", name="ck_inbox_items_kind"),
        sa.CheckConstraint("state IN (1, 2, 3, 4)", name="ck_inbox_items_state"),
        sa.CheckConstraint(
            "resolution IN (0, 1, 2, 3, 4, 5, 6)", name="ck_inbox_items_resolution"
        ),
        sa.CheckConstraint(
            "session_slot >= 0 AND session_slot < 10", name="ck_inbox_items_session_slot"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_inbox_items_user_created",
        "inbox_items",
        ["workspace_id", "user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_inbox_items_user_state_created",
        "inbox_items",
        ["workspace_id", "user_id", "state", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_inbox_items_user_dedupe_key",
        "inbox_items",
        ["workspace_id", "user_id", "dedupe_key"],
        unique=True,
    )
    op.create_index(
        "uq_inbox_items_session_slot",
        "inbox_items",
        ["workspace_id", "user_id", "source_session_id", "session_slot"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``inbox_items`` table."""
    op.drop_index("uq_inbox_items_session_slot", table_name="inbox_items")
    op.drop_index("uq_inbox_items_user_dedupe_key", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_state_created", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_created", table_name="inbox_items")
    op.drop_table("inbox_items")
