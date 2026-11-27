"""Add retry settings and an overlap mode to scheduled tasks.

Revision ID: 130b0e8d0e13
Revises: 47d3e8fc3e25

On ``scheduled_tasks``: ``max_retries`` is how many times a run whose startup
failed is started again, and ``retry_delay_seconds`` is how long the scheduler
waits before each retry. ``overlap_mode`` is what a due occurrence does while
the task's previous run is still going: start anyway (1), be skipped (2) or
wait in a queue (3). The columns are not nullable, so each has a default:
existing tasks get no retries, a five-minute delay and mode 1, which is how
every task runs today, and a server from before this migration can keep
inserting tasks.

On ``scheduled_task_runs``: ``trigger_kind`` is what started the run: its
schedule (1), a Run now request (2) or a retry (3). It repeats the prefix of
``trigger_key`` as an integer, so an index can seek a task's waiting retry or
queued occurrence. ``prompt_dispatch`` is whether the run's prompt has left the
executor that claimed it: not started (1) or started (2). Both are nullable
with no default or backfill: ``NULL`` marks a run recorded by a release that
doesn't write them, which is never taken as proof that a prompt was not sent.

``ix_scheduled_task_runs_task_status`` puts ``status`` and ``trigger_kind``
after the task, then ``scheduled_at``, so finding a task's runs of one status
and kind, or the ones of them scheduled after a given time, reads only those
runs and not the task's run history.

SQLite does not enforce declared types, so there, and on Cloudflare D1, which
serves SQLite, the two retry columns also get a CHECK that they hold an
integer. The other three new columns are bounded on every engine by the CHECK
on their codes.

Additive and unused by earlier releases, so this schema can ship before the
application code that reads it. Servers already running keep working; an older
server that starts after the migration refuses the database revision it doesn't
know. For rollback, stop every server that has this migration, downgrade to
``47d3e8fc3e25`` with one of their builds, then start the older release. The
downgrade drops the index and the five columns, and every saved setting with
them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "130b0e8d0e13"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})
_INTEGER_CHECKS = {
    "ck_scheduled_tasks_max_retries_integer": "typeof(max_retries) = 'integer'",
    "ck_scheduled_tasks_retry_delay_integer": "typeof(retry_delay_seconds) = 'integer'",
}
_RUN_INDEX = "ix_scheduled_task_runs_task_status"


def upgrade() -> None:
    """Add the settings and run columns, leaving every existing task on today's behavior."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(
            sa.Column("max_retries", sa.SmallInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "retry_delay_seconds", sa.SmallInteger(), nullable=False, server_default="300"
            )
        )
        batch.add_column(
            sa.Column("overlap_mode", sa.SmallInteger(), nullable=False, server_default="1")
        )
        batch.create_check_constraint(
            "ck_scheduled_tasks_overlap_mode", "overlap_mode IN (1, 2, 3)"
        )
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            for name, condition in _INTEGER_CHECKS.items():
                batch.create_check_constraint(name, condition)
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.add_column(sa.Column("trigger_kind", sa.SmallInteger(), nullable=True))
        batch.add_column(sa.Column("prompt_dispatch", sa.SmallInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_scheduled_task_runs_trigger_kind", "trigger_kind IN (1, 2, 3)"
        )
        batch.create_check_constraint(
            "ck_scheduled_task_runs_prompt_dispatch", "prompt_dispatch IN (1, 2)"
        )
        batch.create_index(
            _RUN_INDEX,
            ["workspace_id", "scheduled_task_id", "status", "trigger_kind", "scheduled_at", "id"],
        )


def downgrade() -> None:
    """Drop the index, the run columns and the settings."""
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.drop_index(_RUN_INDEX)
        batch.drop_constraint("ck_scheduled_task_runs_prompt_dispatch", type_="check")
        batch.drop_constraint("ck_scheduled_task_runs_trigger_kind", type_="check")
        batch.drop_column("prompt_dispatch")
        batch.drop_column("trigger_kind")
    with op.batch_alter_table("scheduled_tasks") as batch:
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # The rebuilt table would otherwise keep CHECKs on the dropped columns.
            for name in _INTEGER_CHECKS:
                batch.drop_constraint(name, type_="check")
        batch.drop_constraint("ck_scheduled_tasks_overlap_mode", type_="check")
        batch.drop_column("overlap_mode")
        batch.drop_column("retry_delay_seconds")
        batch.drop_column("max_retries")
