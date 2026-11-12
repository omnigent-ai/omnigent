"""add scheduled_task_shares table

Revision ID: a8be64286ffc
Revises: a7d4c2e9f610
Create Date: 2026-11-12 00:00:00.000000

Adds ``scheduled_task_shares``, one row per user or group that an automation's
runs are shared with. Rows are instructions for the sessions a run creates;
nothing reads them for access checks. The application writes them with the
owning ``scheduled_tasks`` row and deletes them with it.

Additive and unused by earlier releases, so this schema can ship before the
application code that writes it. Rolling the schema back drops the table and
the share instructions in it. There are no foreign-key constraints (schema Rule
R032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a8be64286ffc"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``scheduled_task_shares`` table."""
    op.create_table(
        "scheduled_task_shares",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID stored as 16 raw bytes (Uuid16), read back as bare hex.
        sa.Column("scheduled_task_id", Uuid16(), nullable=False),
        # String(128) matches session_permissions.user_id; a user or group ID.
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        # Leads with workspace_id; the prefix serves per-task reads and deletes.
        sa.PrimaryKeyConstraint("workspace_id", "scheduled_task_id", "user_id"),
    )


def downgrade() -> None:
    """Drop the ``scheduled_task_shares`` table."""
    op.drop_table("scheduled_task_shares")
