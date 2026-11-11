"""add the event-trigger schema for scheduled tasks

Revision ID: bdd2c8a6005c
Revises: a7d4c2e9f610
Create Date: 2026-11-11 00:00:00.000000

The shared schema for event-triggered automations, so each trigger provider
adds a ``provider`` value and its JSON shapes, not columns:

* ``scheduled_tasks.trigger_config``: nullable JSON naming the provider and its
  condition. A task with one runs on events instead of a schedule.
* ``scheduled_tasks.rrule`` becomes nullable, since an event-triggered task has
  no recurrence rule. ``rrule IS NULL`` is what identifies one in SQL.
* ``scheduled_task_runs.event``: nullable JSON describing the event(s) a run was
  recorded for.
* ``scheduled_task_event_receipts``: one row per event occurrence a task
  accepted, unique per task, so an occurrence that joined another occurrence's
  run is still recognized when it is delivered again.
* ``ix_scheduled_task_runs_task_status`` finds a task's runs by status, such as
  its unclaimed pending run, without scanning its history.

The JSON columns are read and written whole and never filtered in SQL, so they
are stored compressed, at most 16,383 bytes each.

Additive, with no backfill: existing tasks keep their rule. Deploy it before the
application change that writes these columns. A server that predates that change
reads a task with no ``rrule`` as an invalid schedule and pauses it, so event
triggers stay off until no such server runs against the database.

Downgrade refuses while any task has no ``rrule``; delete the event-triggered
automations first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.mysql import VARCHAR

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision: str = "bdd2c8a6005c"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Below the 16,384-byte bound on any one column value.
_JSON_MAX_STORED_BYTES = 16_383


def upgrade() -> None:
    """Add the trigger and event columns, the receipts table and the status index."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger_config",
                CompressedText(max_stored_bytes=_JSON_MAX_STORED_BYTES),
                nullable=True,
            )
        )
        batch_op.alter_column("rrule", existing_type=sa.String(512), nullable=True)
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "event",
                CompressedText(max_stored_bytes=_JSON_MAX_STORED_BYTES),
                nullable=True,
            )
        )
        batch_op.create_index(
            "ix_scheduled_task_runs_task_status",
            ["workspace_id", "scheduled_task_id", "status", "scheduled_at", "id"],
        )
    op.create_table(
        "scheduled_task_event_receipts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("scheduled_task_id", Uuid16(), nullable=False),
        sa.Column(
            "trigger_key",
            sa.String(256).with_variant(
                VARCHAR(256, charset="utf8mb4", collation="utf8mb4_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("run_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "scheduled_task_id",
            "trigger_key",
            name="uq_scheduled_task_event_receipts_occurrence",
        ),
    )
    op.create_index(
        "ix_scheduled_task_event_receipts_age",
        "scheduled_task_event_receipts",
        ["workspace_id", "scheduled_task_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop the event-trigger schema and require ``rrule`` again, if no task lacks one."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check task rules")
    tasks = sa.table("scheduled_tasks", sa.column("rrule"))
    ruleless = sa.select(tasks.c.rrule).where(tasks.c.rrule.is_(None)).limit(1)
    if op.get_bind().execute(ruleless).first():
        raise RuntimeError(
            "Delete the event-triggered scheduled tasks, which have no rrule, before downgrading"
        )
    op.drop_index(
        "ix_scheduled_task_event_receipts_age", table_name="scheduled_task_event_receipts"
    )
    op.drop_table("scheduled_task_event_receipts")
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.drop_index("ix_scheduled_task_runs_task_status")
        batch_op.drop_column("event")
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.alter_column("rrule", existing_type=sa.String(512), nullable=False)
        batch_op.drop_column("trigger_config")
