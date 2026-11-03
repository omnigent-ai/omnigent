"""Add nullable scheduled-run limits and observations.

Revision ID: a3d91e7b6c20
Revises: b4e2a8d06f19

Deploy before the application that writes these fields. Old binaries leave
them NULL and remain compatible; roll back the application before downgrading.
No backfill: historical completion and business outcomes are unknown.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a3d91e7b6c20"
down_revision: str | None = "b4e2a8d06f19"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduled_tasks", sa.Column("max_duration_seconds", sa.Integer(), nullable=True)
    )
    for name, column_type in (
        ("completion_kind", sa.String(32)),
        ("outcome", sa.String(32)),
        ("outcome_summary", sa.LargeBinary()),
        ("heartbeat_at", sa.BigInteger()),
        ("last_activity_at", sa.BigInteger()),
        ("deadline_at", sa.BigInteger()),
        ("runtime_state", sa.String(32)),
    ):
        op.add_column("scheduled_task_runs", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_task_runs") as batch:
        for name in (
            "completion_kind",
            "outcome",
            "outcome_summary",
            "heartbeat_at",
            "last_activity_at",
            "deadline_at",
            "runtime_state",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("max_duration_seconds")
