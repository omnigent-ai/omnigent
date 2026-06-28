"""drop tasks table

Revision ID: b9c1d2e3f4a5
Revises: d8e2f3b4c910
Create Date: 2026-05-28

The ``tasks`` table was never populated in production code (no
``task_store.create()`` calls exist in any production path). All reads
against it returned empty. This migration drops the dead table and its
indexes to keep the schema consistent with the removed ``TaskStore`` /
``SqlTask`` ORM class.

The conversation-level ``agent_id`` filter in
``SqlAlchemyConversationStore.list_conversations`` has been updated to
use ``conversations.agent_id`` directly instead of the removed EXISTS
subquery against tasks.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c1d2e3f4a5"
down_revision: str | None = "d8e2f3b4c910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the tasks table and all of its indexes."""
    bind = op.get_bind()
    if bind.dialect.name in ("mysql", "mariadb"):
        # MySQL/MariaDB: cannot drop indexes that support FK constraints while
        # the constraints still exist. DROP TABLE removes everything atomically.
        op.drop_table("tasks")
        return
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("ix_tasks_conversation_id")
        batch_op.drop_index("ix_tasks_agent_id")
        batch_op.drop_index("ix_tasks_created_at")
        batch_op.drop_index("ix_tasks_root_task_id")
        batch_op.drop_index("ix_tasks_parent_task_id")
        batch_op.drop_index("ix_tasks_kind")
    op.drop_table("tasks")


def downgrade() -> None:
    """Recreate the tasks table and its indexes."""
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("previous_response_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("agent_name", sa.String(256), nullable=False),
        sa.Column("background", sa.Boolean, default=False),
        sa.Column(
            "root_task_id",
            sa.String(64),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "parent_task_id",
            sa.String(64),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "kind",
            sa.String(32),
            default="agent_task",
            server_default="agent_task",
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('agent_task', 'tool', 'sub_agent', 'client_tool', 'terminal')",
            name="ck_tasks_kind",
        ),
    )
    op.create_index("ix_tasks_conversation_id", "tasks", ["conversation_id"])
    op.create_index("ix_tasks_agent_id", "tasks", ["agent_id"])
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_root_task_id", "tasks", ["root_task_id"])
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_index("ix_tasks_kind", "tasks", ["kind"])
