"""add jobs and runs tables

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-06-26 12:00:00.000000

Adds the ``jobs`` and ``runs`` tables for the Jobs/Workflows feature. A job is a
saved AI workflow (node graph) stored as opaque JSON plus a rendered English
narrative; a run records one execution (an agent session) of a job. See the
"Omnigent jobs" brief.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("graph", sa.Text(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=True),
        sa.Column("harness_override", sa.String(length=64), nullable=True),
        sa.Column("model_override", sa.String(length=128), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.Column("schedule_config", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_jobs_created_by_updated_at", "jobs", ["created_by", "updated_at"], unique=False
    )
    op.create_index("ix_jobs_updated_at", "jobs", ["updated_at"], unique=False)

    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_job_id_started_at", "runs", ["job_id", "started_at"], unique=False)
    op.create_index("ix_runs_session_id", "runs", ["session_id"], unique=False)
    op.create_index("ix_runs_status", "runs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_session_id", table_name="runs")
    op.drop_index("ix_runs_job_id_started_at", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_jobs_updated_at", table_name="jobs")
    op.drop_index("ix_jobs_created_by_updated_at", table_name="jobs")
    op.drop_table("jobs")
