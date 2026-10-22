"""Add scheduled-task dispatch fields and widen existing timestamps.

Revision ID: c8e4f2a6b901
Revises: nf12ab34cd56

Deploy before consumers; reserve status 6 until all readers support starting.
Downgrade requires stopping new writers, reconciling starting runs, and fitting
legacy timestamps into signed 32-bit integers. It discards new fields. Older
binaries require a compatible revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.mysql import VARCHAR

from omnigent.db.db_models import Uuid16

revision: str = "c8e4f2a6b901"
down_revision: str | None = "nf12ab34cd56"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TASK_TIMESTAMPS = ("last_run_at", "created_at", "updated_at")
_RUN_TIMESTAMPS = ("scheduled_at", "fired_at", "finished_at")


def upgrade() -> None:
    """Widen timestamps and add dispatch fields, uniqueness, and scan indexes."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        for column in _TASK_TIMESTAMPS:
            batch.alter_column(
                column,
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                existing_nullable=column != "created_at",
            )
        batch.add_column(sa.Column("next_run_at", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("project_id", Uuid16(), nullable=True))
        batch.add_column(sa.Column("delegation_ref", sa.String(1024), nullable=True))
        batch.create_index(
            "ix_scheduled_tasks_due", ["state", "next_run_at", "workspace_id", "id"]
        )

    with op.batch_alter_table("scheduled_task_runs") as batch:
        for column in _RUN_TIMESTAMPS:
            batch.alter_column(
                column,
                existing_type=sa.Integer(),
                type_=sa.BigInteger(),
                existing_nullable=column != "scheduled_at",
            )
        batch.add_column(
            sa.Column(
                "trigger_key",
                sa.String(256).with_variant(
                    VARCHAR(256, charset="utf8mb4", collation="utf8mb4_bin"), "mysql"
                ),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("host_id", Uuid16(), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.BigInteger(), nullable=True))
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_scheduled_task_runs_status", type_="check")
        batch.create_check_constraint(
            "ck_scheduled_task_runs_dispatch_status", "status IN (1, 2, 3, 4, 5, 6)"
        )
        batch.create_unique_constraint(
            "uq_scheduled_task_runs_occurrence",
            ["workspace_id", "scheduled_task_id", "trigger_key"],
        )
        batch.create_index(
            "ix_scheduled_task_runs_expired_claims",
            ["status", "lease_expires_at", "workspace_id", "id"],
        )


def downgrade() -> None:
    """Restore the previous schema only when statuses and timestamps fit."""
    if context.is_offline_mode():
        raise RuntimeError(
            "Downgrade requires a live connection to check run statuses and timestamps"
        )
    runs = sa.table("scheduled_task_runs", sa.column("status"))
    if op.get_bind().execute(sa.select(runs.c.status).where(runs.c.status == 6).limit(1)).first():
        raise RuntimeError("Reconcile starting scheduled-task runs before downgrading")

    # Check both tables before DDL, including on engines with nontransactional DDL.
    for table_name, columns in (
        ("scheduled_tasks", _TASK_TIMESTAMPS),
        ("scheduled_task_runs", _RUN_TIMESTAMPS),
    ):
        table = sa.table(table_name, *(sa.column(column) for column in columns))
        outside_range = sa.or_(
            *(sa.or_(column < -(2**31), column > 2**31 - 1) for column in table.c)
        )
        if op.get_bind().execute(sa.select(table).where(outside_range).limit(1)).first():
            raise RuntimeError(
                f"Reconcile timestamps outside signed 32-bit range in {table_name} "
                "before downgrading"
            )

    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.drop_index("ix_scheduled_task_runs_expired_claims")
        if op.get_bind().dialect.name == "cockroachdb":
            # CockroachDB removes unique constraints through their backing index.
            op.execute("DROP INDEX scheduled_task_runs@uq_scheduled_task_runs_occurrence CASCADE")
        else:
            batch.drop_constraint("uq_scheduled_task_runs_occurrence", type_="unique")
        batch.drop_constraint("ck_scheduled_task_runs_dispatch_status", type_="check")
        batch.create_check_constraint("ck_scheduled_task_runs_status", "status IN (1, 2, 3, 4, 5)")
        batch.drop_column("lease_expires_at")
        batch.drop_column("host_id")
        batch.drop_column("trigger_key")
        # CRDB cannot narrow indexed timestamps; keep their widened types.
        if op.get_bind().dialect.name != "cockroachdb":
            for column in _RUN_TIMESTAMPS:
                batch.alter_column(
                    column,
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=column != "scheduled_at",
                )

    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_index("ix_scheduled_tasks_due")
        batch.drop_column("delegation_ref")
        batch.drop_column("project_id")
        batch.drop_column("next_run_at")
        # CRDB cannot narrow indexed timestamps; keep their widened types.
        if op.get_bind().dialect.name != "cockroachdb":
            for column in _TASK_TIMESTAMPS:
                batch.alter_column(
                    column,
                    existing_type=sa.BigInteger(),
                    type_=sa.Integer(),
                    existing_nullable=column != "created_at",
                )
