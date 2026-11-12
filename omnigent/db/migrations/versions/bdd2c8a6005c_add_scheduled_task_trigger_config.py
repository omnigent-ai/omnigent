"""add the event-trigger schema for scheduled tasks

Revision ID: bdd2c8a6005c
Revises: a7d4c2e9f610
Create Date: 2026-11-11 00:00:00.000000

The shared schema for event-triggered automations, so each trigger provider
adds a ``provider`` value and its JSON shapes, not columns:

* ``scheduled_tasks.trigger_config``: nullable JSON naming the provider and its
  condition. A task with one runs on events instead of a schedule.
* ``scheduled_tasks.rrule`` becomes nullable, since an event-triggered task has
  no recurrence rule.
* ``scheduled_tasks.pending_event_run_id``: the event run that a task continuing
  one session adds new events to while it waits for a claim, or NULL.
* ``scheduled_tasks.trigger_provider``: the provider named in ``trigger_config``,
  or NULL for a scheduled task. It is the scalar that SQL filters on, through
  ``ix_scheduled_tasks_trigger`` on ``(state, trigger_provider, next_run_at,
  workspace_id, id)``: an equality seek finds a workspace's active tasks for one
  provider, and the search for scheduled tasks without a next run time skips
  event-triggered ones.
* ``scheduled_task_runs.event``: nullable JSON describing the event(s) a run was
  recorded for.
* ``scheduled_task_event_receipts``: one row per event occurrence a task
  accepted, unique per task, so an occurrence that joined another occurrence's
  run is still recognized when it is delivered again.

The JSON columns are read and written whole and never filtered in SQL, so they
are stored compressed, at most 16,383 bytes each (``VARBINARY(16383)`` on MySQL).

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
# Tasks the downgrade guard reads per query, in primary-key order.
_GUARD_BATCH = 500
# Dialects that ignore VARCHAR lengths and store a value of any size.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})
_RECEIPT_KEY_CHECK = "ck_scheduled_task_event_receipts_key_bytes"
_PROVIDER_CHECK = "ck_scheduled_tasks_trigger_provider_bytes"


def upgrade() -> None:
    """Add the trigger and event columns, the receipts table and the status index."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger_config",
                CompressedText(max_stored_bytes=_JSON_MAX_STORED_BYTES, mysql_varbinary=True),
                nullable=True,
            )
        )
        batch_op.alter_column("rrule", existing_type=sa.String(512), nullable=True)
        batch_op.add_column(sa.Column("trigger_provider", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("pending_event_run_id", Uuid16(), nullable=True))
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # SQLite ignores VARCHAR lengths: 32 characters at up to four bytes each.
            batch_op.create_check_constraint(
                _PROVIDER_CHECK,
                "trigger_provider IS NULL OR length(CAST(trigger_provider AS BLOB)) <= 128",
            )
        batch_op.create_index(
            "ix_scheduled_tasks_trigger",
            ["state", "trigger_provider", "next_run_at", "workspace_id", "id"],
        )
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "event",
                CompressedText(max_stored_bytes=_JSON_MAX_STORED_BYTES, mysql_varbinary=True),
                nullable=True,
            )
        )
    # SQLite ignores VARCHAR lengths: 256 characters at up to four bytes each.
    key_check = (
        [sa.CheckConstraint("length(CAST(trigger_key AS BLOB)) <= 1024", name=_RECEIPT_KEY_CHECK)]
        if op.get_bind().dialect.name in _SQLITE_FAMILY
        else []
    )
    op.create_table(
        "scheduled_task_event_receipts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("scheduled_task_id", Uuid16(), nullable=False),
        sa.Column(
            "trigger_key",
            sa.String(256).with_variant(
                VARCHAR(256, charset="utf8mb4", collation="utf8mb4_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("run_id", Uuid16(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "scheduled_task_id",
            "trigger_key",
            name="uq_scheduled_task_event_receipts_occurrence",
        ),
        *key_check,
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
    _refuse_while_a_task_has_no_rule()
    op.drop_index(
        "ix_scheduled_task_event_receipts_age", table_name="scheduled_task_event_receipts"
    )
    op.drop_table("scheduled_task_event_receipts")
    with op.batch_alter_table("scheduled_task_runs") as batch_op:
        batch_op.drop_column("event")
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_index("ix_scheduled_tasks_trigger")
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # The rebuilt table would otherwise keep a CHECK on the dropped column.
            batch_op.drop_constraint(_PROVIDER_CHECK, type_="check")
        batch_op.drop_column("trigger_provider")
        batch_op.drop_column("pending_event_run_id")
        batch_op.alter_column("rrule", existing_type=sa.String(512), nullable=False)
        batch_op.drop_column("trigger_config")


def _refuse_while_a_task_has_no_rule() -> None:
    """Read every task in primary-key batches, before any DDL, and refuse on a NULL rule."""
    tasks = sa.table(
        "scheduled_tasks", sa.column("workspace_id"), sa.column("id"), sa.column("rrule")
    )
    bind = op.get_bind()
    after: tuple[object, object] | None = None
    while True:
        rows = bind.execute(_guard_page(tasks, after, bind.dialect.name)).all()
        if any(row.rrule is None for row in rows):
            raise RuntimeError(
                "Delete the event-triggered scheduled tasks, which have no rrule, "
                "before downgrading"
            )
        if len(rows) < _GUARD_BATCH:
            return
        after = (rows[-1].workspace_id, rows[-1].id)


def _guard_page(
    tasks: sa.TableClause, after: tuple[object, object] | None, dialect: str
) -> sa.Select:
    """One primary-key page of tasks after *after*, as a seek to the cursor."""
    columns = (tasks.c.workspace_id, tasks.c.id)
    page = sa.select(*columns, tasks.c.rrule).order_by(*columns).limit(_GUARD_BATCH)
    if after is None:
        return page
    if dialect == "mysql":
        # MySQL treats tuple inequalities as filters over a full index scan.
        return page.where(
            sa.or_(
                tasks.c.workspace_id > after[0],
                sa.and_(tasks.c.workspace_id == after[0], tasks.c.id > after[1]),
            )
        )
    return page.where(sa.tuple_(*columns) > after)
