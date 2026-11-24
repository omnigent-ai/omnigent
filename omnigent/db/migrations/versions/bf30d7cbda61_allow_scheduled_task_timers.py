"""Allow the timer scheduled-task kind.

Revision ID: bf30d7cbda61
Revises: 5ab01aaca219

``kind`` 3 marks a timer an agent set with ``sys_timer_set``: a one-shot or
repeating hidden wake into its own session, which clients never list as an
automation. The migration also indexes tasks by kind within each owner's scope,
so listing one kind never reads another's rows and the downgrade finds any
timer in one indexed lookup.

Deployment order: run this migration first. Servers already running keep
working, since they never write kind 3 and the wider check accepts what they
write; as after any migration, though, a build from before it won't start
against the migrated database. Then deploy a release that reads timers to every
server sharing the database, and only then enable its ``session_timers`` release
feature: an older binary can't decode a timer row.

Rollback: to run a release that has this migration but can't read timers, first
turn ``session_timers`` off and drain the timers until no ``kind = 3`` row
remains. Going back to a build from before this migration also needs this
downgrade, which refuses while a timer row remains.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision: str = "bf30d7cbda61"
down_revision: str | None = "5ab01aaca219"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TIMER = 3
_KIND_INDEX = "ix_scheduled_tasks_kind_scope"
_KIND_INDEX_COLUMNS = ["kind", "workspace_id", "user_id", "created_at", "id"]
_TASKS = sa.table("scheduled_tasks", sa.column("kind"))
# The kind index leads with kind, so this reads at most one index entry.
_TIMER_EXISTS = sa.select(_TASKS.c.kind).where(_TASKS.c.kind == _TIMER).limit(1)


def _publish_earlier_schema_changes() -> None:
    """Commit pending schema changes on CockroachDB, which can't drop a check it is adding.

    Offline SQL has no connection to commit, so a generated script keeps one transaction.
    """
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    if bind.dialect.name == "cockroachdb":
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def upgrade() -> None:
    """Replace the kind check so it also accepts timers, and index kinds per owner."""
    # An upgrade that also runs the migration adding the old check holds it unpublished.
    _publish_earlier_schema_changes()
    with op.batch_alter_table("scheduled_tasks") as batch:
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_scheduled_tasks_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_task_kind", "kind IN (1, 2, 3)")
    op.create_index(_KIND_INDEX, "scheduled_tasks", _KIND_INDEX_COLUMNS)


def downgrade() -> None:
    """Restore the previous kind check only when no timer exists."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check task kinds")
    if op.get_bind().execute(_TIMER_EXISTS).first():
        raise RuntimeError("Cancel or delete scheduled-task timers before downgrading")
    # A round trip in one transaction would otherwise drop a check it is still adding.
    _publish_earlier_schema_changes()
    op.drop_index(_KIND_INDEX, table_name="scheduled_tasks")
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_task_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_kind", "kind IN (1, 2)")
