"""add work_items table

Revision ID: f0e1d2c3b4a5
Revises: n1a2b3c4d5e6
Create Date: 2026-06-28 00:00:00.000000

Adds the ``work_items`` table: tracked units of work (manual, or ingested
from Slack / email / GitHub / Jira) that an agent processes in a linked
conversation. A globally-unique ``dedup_key`` makes intake idempotent. The
table is a thin layer over conversations — it does NOT revive the removed
``tasks`` table; the conversation tree still owns sub-sessions/threads.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0e1d2c3b4a5"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=256), nullable=True),
        sa.Column("dedup_key", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'new'"),
            nullable=False,
        ),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("assignee_user_id", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_key", name="uq_work_items_dedup_key"),
    )
    op.create_index("ix_work_items_status", "work_items", ["status"], unique=False)
    op.create_index(
        "ix_work_items_conversation_id", "work_items", ["conversation_id"], unique=False
    )
    op.create_index("ix_work_items_created_at", "work_items", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_work_items_created_at", table_name="work_items")
    op.drop_index("ix_work_items_conversation_id", table_name="work_items")
    op.drop_index("ix_work_items_status", table_name="work_items")
    op.drop_table("work_items")
