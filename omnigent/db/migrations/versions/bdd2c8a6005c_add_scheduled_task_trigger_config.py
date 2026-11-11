"""add event trigger columns to scheduled tasks and their runs

Revision ID: bdd2c8a6005c
Revises: a7d4c2e9f610
Create Date: 2026-11-11 00:00:00.000000

The shared schema for event-triggered automations, so each trigger provider
adds a ``provider`` value and its JSON shapes, not columns:

* ``scheduled_tasks.trigger_config``: nullable JSON naming the provider and its
  condition. A task with one runs on events instead of a schedule.
* ``scheduled_tasks.rrule`` becomes nullable, since an event-triggered task has
  no recurrence rule.
* ``scheduled_task_runs.event``: nullable JSON describing the event a run was
  recorded for.

Both JSON columns are read and written whole and never filtered in SQL beyond
``IS NULL``, so they are stored compressed. Additive, with no backfill: existing
tasks keep their rule and get a NULL ``trigger_config``. Deploy it before the
application change that reads the columns.

Downgrade refuses while any task has no ``rrule``, since the column becomes
NOT NULL again; delete the event-triggered automations first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from omnigent.db.compression import CompressedText

revision: str = "bdd2c8a6005c"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# MySQL BLOB holds 65,535 bytes; both documents are far smaller in practice.
_JSON_MAX_STORED_BYTES = 65_535


def upgrade() -> None:
    """Add the trigger and event JSON columns and allow tasks without a rule."""
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


def downgrade() -> None:
    """Drop the JSON columns and require ``rrule`` again, if no task lacks one."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check task rules")
    tasks = sa.table("scheduled_tasks", sa.column("rrule"))
    if (
        op.get_bind()
        .execute(sa.select(tasks.c.rrule).where(tasks.c.rrule.is_(None)).limit(1))
        .first()
    ):
        raise RuntimeError(
            "Delete the event-triggered scheduled tasks, which have no rrule, before downgrading"
        )
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.drop_column("event")
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.alter_column("rrule", existing_type=sa.String(512), nullable=False)
        batch_op.drop_column("trigger_config")
