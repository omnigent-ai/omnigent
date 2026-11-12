"""Add how a scheduled task chooses the session each run uses.

Revision ID: d0a1f879a01d
Revises: b2e4f6a8c013

``session_mode`` selects a new session per run (1, the default for existing
tasks) or one continued session (2). ``session_id`` names that continued
session; it relates to ``conversations.id`` with no database foreign key, like
``last_run_conversation_id``. Downgrade drops both, so continuing tasks start a
new session on each run again.

A continued session collects one run per firing, so
``ix_scheduled_task_runs_conversation_status`` adds ``status`` and ``id`` after
the session: finding a session's running run then reads only that run, not the
session's whole run history. It supersedes
``ix_scheduled_task_runs_conversation_id``, which stays until a later migration
drops it after an observation period. Downgrade drops the new index.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "d0a1f879a01d"
down_revision: str | None = "b2e4f6a8c013"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the session mode, defaulting every existing task to a new session per run."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(
            sa.Column("session_mode", sa.SmallInteger(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("session_id", Uuid16(), nullable=True))
        batch.create_check_constraint("ck_scheduled_tasks_session_mode", "session_mode IN (1, 2)")
    op.create_index(
        "ix_scheduled_task_runs_conversation_status",
        "scheduled_task_runs",
        ["workspace_id", "conversation_id", "status", "id"],
    )


def downgrade() -> None:
    """Drop the session mode and the continued session."""
    op.drop_index("ix_scheduled_task_runs_conversation_status", table_name="scheduled_task_runs")
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_session_mode", type_="check")
        batch.drop_column("session_id")
        batch.drop_column("session_mode")
