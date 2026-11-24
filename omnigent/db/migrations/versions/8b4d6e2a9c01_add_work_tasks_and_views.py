"""Add independent work tasks, session participation and personal saved views.

Revision ID: 8b4d6e2a9c01
Revises: 47d3e8fc3e25

Additive tables; existing projects and sessions are unchanged. Downgrade removes
the new tasks and views, so export them before rolling back a populated database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16
from omnigent.db.envelope import EnvelopedJSON, EnvelopedText

revision: str = "8b4d6e2a9c01"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "work_tasks",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("project_id", Uuid16(), nullable=True),
        sa.Column("title", sa.String(768), nullable=False),
        sa.Column("description", EnvelopedText(65535), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_work_tasks_owner", "work_tasks", ["workspace_id", "user_id", "created_at", "id"]
    )
    op.create_index("ix_work_tasks_project", "work_tasks", ["workspace_id", "project_id", "id"])
    op.create_table(
        "work_task_sessions",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("task_id", Uuid16(), nullable=False),
        sa.Column("session_id", Uuid16(), primary_key=True),
        sa.Column("involvement", sa.String(1200), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_work_task_sessions_task", "work_task_sessions", ["workspace_id", "task_id"]
    )
    op.create_table(
        "work_views",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("name", sa.String(480), nullable=False),
        sa.Column("definition", EnvelopedJSON(16383), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
    )
    op.create_index(
        "ix_work_views_owner", "work_views", ["workspace_id", "user_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("work_views")
    op.drop_table("work_task_sessions")
    op.drop_table("work_tasks")
