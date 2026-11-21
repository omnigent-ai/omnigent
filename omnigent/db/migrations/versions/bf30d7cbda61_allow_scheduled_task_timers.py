"""Allow the timer scheduled-task kind.

Revision ID: bf30d7cbda61
Revises: 5ab01aaca219

``kind`` 3 marks a timer an agent set with ``sys_timer_set``: a one-shot or
repeating hidden wake into its own session, which clients never list as an
automation. Deploy before any writer creates timers: an older binary cannot
read the new code. Downgrade refuses while a timer exists; cancel timers first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision: str = "bf30d7cbda61"
down_revision: str | None = "5ab01aaca219"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TIMER = 3


def _publish_earlier_schema_changes() -> None:
    """Commit pending schema changes on CockroachDB, which can't drop a check it is adding."""
    bind = op.get_bind()
    if bind.dialect.name == "cockroachdb":
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


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
    tasks = sa.table("scheduled_tasks", sa.column("kind"))
    timer = sa.select(tasks.c.kind).where(tasks.c.kind == _TIMER).limit(1)
    if op.get_bind().execute(timer).first():
        raise RuntimeError("Cancel or delete scheduled-task timers before downgrading")
    # A round trip in one transaction would otherwise drop a check it is still adding.
    _publish_earlier_schema_changes()
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_task_kind", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_kind", "kind IN (1, 2)")
