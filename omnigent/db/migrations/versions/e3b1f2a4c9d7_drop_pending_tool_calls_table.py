"""drop pending_tool_calls table

Revision ID: e3b1f2a4c9d7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-28

The ``pending_tool_calls`` table was scaffolded for a client-side
tool-tunneling feature that was never implemented. All store methods
that read/write it have been removed. This migration drops the table
from existing databases.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3b1f2a4c9d7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name in ("mysql", "mariadb"):
        # MySQL/MariaDB: cannot drop an index that supports a foreign key
        # constraint while the constraint still exists. Since we're dropping
        # the whole table anyway, skip straight to DROP TABLE — MySQL removes
        # all indexes and FK constraints with the table automatically.
        op.drop_table("pending_tool_calls")
        return
    op.drop_index("ix_pending_tool_calls_task_id", table_name="pending_tool_calls")
    op.drop_index("ix_pending_tool_calls_root_task_id", table_name="pending_tool_calls")
    op.drop_table("pending_tool_calls")


def downgrade() -> None:
    op.create_table(
        "pending_tool_calls",
        sa.Column("call_id", sa.String(length=64), nullable=False),
        sa.Column("root_task_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=256), nullable=False),
        sa.Column("arguments", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('action_required', 'completed')",
            name="ck_pending_tool_calls_status",
        ),
        sa.ForeignKeyConstraint(
            ["root_task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("call_id"),
    )
    op.create_index(
        "ix_pending_tool_calls_root_task_id", "pending_tool_calls", ["root_task_id"], unique=False
    )
    op.create_index(
        "ix_pending_tool_calls_task_id", "pending_tool_calls", ["task_id"], unique=False
    )
