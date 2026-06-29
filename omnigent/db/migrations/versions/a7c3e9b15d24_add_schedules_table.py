"""add schedules table

Revision ID: a7c3e9b15d24
Revises: f0e1d2c3b4a5
Create Date: 2026-06-28 00:30:00.000000

Adds the ``schedules`` table: loops (cron-driven prompts) and monitors
(stream-driven prompts) scoped to a conversation. Replaces the unimplemented
``sys_timer_set`` stub path with a persisted, scheduler-armed definition.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9b15d24"
down_revision: str | None = "f0e1d2c3b4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=128), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron", sa.String(length=128), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'idle'"), nullable=False
        ),
        sa.Column("last_fired_at", sa.Integer(), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "name", name="uq_schedules_conversation_id_name"),
    )
    op.create_index("ix_schedules_conversation_id", "schedules", ["conversation_id"], unique=False)
    op.create_index("ix_schedules_enabled", "schedules", ["enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_schedules_enabled", table_name="schedules")
    op.drop_index("ix_schedules_conversation_id", table_name="schedules")
    op.drop_table("schedules")
