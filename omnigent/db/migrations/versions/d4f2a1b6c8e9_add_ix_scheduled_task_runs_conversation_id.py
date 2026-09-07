"""Add the ``ix_scheduled_task_runs_conversation_id`` index.

Revision ID: d4f2a1b6c8e9
Revises: 72e6dceae14f
Create Date: 2026-07-21 00:00:00.000000

The event-driven run-completion hook transitions a scheduled-task run the
instant its conversation's turn reaches a terminal state. To find the run it
reverse-looks-up by ``conversation_id`` (``get_running_run_by_conversation``)
on every turn-terminal edge — for interactive sessions too, not just scheduled
ones. Without an index that is a full scan of ``scheduled_task_runs``. Index
``(workspace_id, conversation_id)`` to make the lookup a selective point read.

Creating an index is a simple operation on SQLite, PostgreSQL, and MySQL
alike, so no table rebuild / batch mode is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4f2a1b6c8e9"
down_revision: str | None = "72e6dceae14f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``ix_scheduled_task_runs_conversation_id`` index."""
    op.create_index(
        "ix_scheduled_task_runs_conversation_id",
        "scheduled_task_runs",
        ["workspace_id", "conversation_id"],
    )


def downgrade() -> None:
    """Drop the ``ix_scheduled_task_runs_conversation_id`` index."""
    op.drop_index(
        "ix_scheduled_task_runs_conversation_id",
        table_name="scheduled_task_runs",
    )
