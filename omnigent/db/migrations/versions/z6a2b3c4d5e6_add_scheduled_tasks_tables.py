"""add scheduled_tasks and scheduled_task_runs tables

Revision ID: z6a2b3c4d5e6
Revises: z5a2b3c4d5e6
Create Date: 2026-07-09 00:00:00.000000

Adds the ``scheduled_tasks`` table (saved, scheduled agent instructions) and its
``scheduled_task_runs`` history table (one row per firing). This is the
persistence foundation only — no scheduler reads or dispatches these rows yet.

The task trigger is a required recurring ``cron_expression`` (matching the
recurring-only shape of the reference platforms): every task fires on a cron
schedule, so ``cron_expression`` is NOT NULL.

Both tables are brand-new and are created at the current schema state, so each
carries the tenant-partition ``workspace_id`` column as the leading primary-key
member (matching every other table after ``r1a2b3c4d5e6``). There are no
foreign-key constraints (schema Rule R032 — see ``p1a2b3c4d5e6``): the
``agent_id`` / ``conversation_id`` / ``scheduled_task_id`` relationships are
enforced by the application, not the database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z6a2b3c4d5e6"
down_revision: str | None = "z5a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``scheduled_tasks`` and ``scheduled_task_runs`` tables."""
    op.create_table(
        "scheduled_tasks",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # Opaque PK, e.g. "st_<hex>". On Isaac→Omni migration a FRESH id is minted
        # here (not Isaac's schedule_id); the original Isaac schedule_id is preserved
        # in `metadata` (e.g. {"isaac_schedule_id": ...}) for traceability + idempotent re-runs.
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        # Opaque free text stored compressed (CompressedText → LargeBinary).
        sa.Column("prompt", sa.LargeBinary(), nullable=False),
        # Recurring trigger: a required cron string (e.g. "0 9 * * *").
        sa.Column("cron_expression", sa.String(255), nullable=False),
        sa.Column("owner_user_id", sa.String(255), nullable=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("harness_override", sa.String(64), nullable=True),
        sa.Column("model_override", sa.String(128), nullable=True),
        sa.Column("reasoning_effort", sa.String(32), nullable=True),
        sa.Column("workspace", sa.Text(), nullable=True),
        # Git base ref a firing branches from when it creates a worktree.
        sa.Column("base_branch", sa.String(255), nullable=True),
        sa.Column("sandbox_target", sa.String(64), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False),
        # Enum stored as a stable int code (see omnigent.db.enum_codecs
        # SCHEDULED_TASK_STATE: active=1, paused=2, deleted=3).
        sa.Column("state", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.Integer(), nullable=True),
        sa.Column("last_run_conversation_id", sa.String(64), nullable=True),
        # Opaque JSON stored compressed (CompressedText → LargeBinary). No
        # server_default: BLOB columns can't have one; NOT NULL is fine — the
        # store always writes "{}" explicitly on insert.
        sa.Column("metadata", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.CheckConstraint("state IN (1, 2, 3)", name="ck_scheduled_tasks_state"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_scheduled_tasks_created_at",
        "scheduled_tasks",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_tasks_owner_user_id",
        "scheduled_tasks",
        ["workspace_id", "owner_user_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_tasks_agent_id",
        "scheduled_tasks",
        ["workspace_id", "agent_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_scheduled_tasks_state",
        "scheduled_tasks",
        ["workspace_id", "state", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "scheduled_task_runs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("scheduled_task_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        # Enum stored as a stable int code (see omnigent.db.enum_codecs
        # SCHEDULED_TASK_RUN_STATUS: scheduled=1, running=2, succeeded=3,
        # failed=4, skipped=5).
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("scheduled_at", sa.Integer(), nullable=False),
        sa.Column("fired_at", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        # Opaque free-text error blob stored compressed (CompressedText → LargeBinary).
        sa.Column("error", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            "status IN (1, 2, 3, 4, 5)",
            name="ck_scheduled_task_runs_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_scheduled_task_runs_scheduled_task_id",
        "scheduled_task_runs",
        ["workspace_id", "scheduled_task_id", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``scheduled_task_runs`` and ``scheduled_tasks`` tables."""
    op.drop_index("ix_scheduled_task_runs_scheduled_task_id", table_name="scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
    op.drop_index("ix_scheduled_tasks_state", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_agent_id", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_owner_user_id", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_created_at", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
