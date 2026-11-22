"""Allow the timer scheduled-task kind.

Revision ID: bf30d7cbda61
Revises: 5ab01aaca219

``kind`` 3 marks a timer an agent set with ``sys_timer_set``: a one-shot or
repeating hidden wake into its own session, which clients never list as an
automation.

Deployment order: run this migration first. Older binaries keep working, since
they never write kind 3 and the wider check accepts what they write. Then deploy
a release that reads timers to every server sharing the database, and only then
enable its ``session_timers`` release feature: an older binary can't decode a
timer row.

Rollback: before moving a server to a release that can't read timers, turn
``session_timers`` off and drain the timers until no ``kind = 3`` row remains.
The downgrade refuses while one does.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

from omnigent.db.keyset import keyset_after

revision: str = "bf30d7cbda61"
down_revision: str | None = "5ab01aaca219"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TIMER = 3
# No index leads with kind, so the downgrade looks for timers one bounded batch of
# primary keys at a time.
_CHECK_BATCH = 1000


def _publish_earlier_schema_changes() -> None:
    """Commit pending schema changes on CockroachDB, which can't drop a check it is adding."""
    bind = op.get_bind()
    if bind.dialect.name == "cockroachdb":
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _timers_remain() -> bool:
    """Whether any timer remains, reading at most ``_CHECK_BATCH`` rows per query."""
    bind = op.get_bind()
    tasks = sa.table(
        "scheduled_tasks", sa.column("workspace_id"), sa.column("id"), sa.column("kind")
    )
    key = (tasks.c.workspace_id, tasks.c.id)
    after: tuple[int, bytes] | None = None
    while True:
        batch = sa.select(*key, tasks.c.kind).order_by(*key).limit(_CHECK_BATCH)
        if after is not None:
            batch = batch.where(keyset_after(bind.dialect.name, key, after))
        rows = bind.execute(batch).all()
        if any(row.kind == _TIMER for row in rows):
            return True
        if len(rows) < _CHECK_BATCH:
            return False
        after = (rows[-1].workspace_id, rows[-1].id)


def upgrade() -> None:
    """Replace the kind check so it also accepts timers."""
    # An upgrade that also runs the migration adding the old check holds it unpublished.
    _publish_earlier_schema_changes()
    with op.batch_alter_table("scheduled_tasks") as batch:
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_scheduled_tasks_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_task_kind", "kind IN (1, 2, 3)")


def downgrade() -> None:
    """Restore the previous kind check only when no timer exists."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check task kinds")
    if _timers_remain():
        raise RuntimeError("Cancel or delete scheduled-task timers before downgrading")
    # A round trip in one transaction would otherwise drop a check it is still adding.
    _publish_earlier_schema_changes()
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_task_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_kind", "kind IN (1, 2)")
