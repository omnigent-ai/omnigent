"""Allow the completed scheduled-task state.

Revision ID: 9e82957bd115
Revises: c8e4f2a6b901

Deploy before any writer marks schedules completed. Downgrade refuses while any
task is completed; reconcile those rows first, for example by setting them to
active with no next run time, which is how finished schedules were stored before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision: str = "9e82957bd115"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_COMPLETED = 4


def upgrade() -> None:
    """Replace the state check so it also accepts completed tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_scheduled_tasks_state", type_="check")
        batch.create_check_constraint(
            "ck_scheduled_tasks_lifecycle_state", "state IN (1, 2, 3, 4)"
        )


def downgrade() -> None:
    """Restore the previous state check only when no task is completed."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check task states")
    tasks = sa.table("scheduled_tasks", sa.column("state"))
    completed = sa.select(tasks.c.state).where(tasks.c.state == _COMPLETED).limit(1)
    if op.get_bind().execute(completed).first():
        raise RuntimeError("Reconcile completed scheduled tasks before downgrading")
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_lifecycle_state", type_="check")
        batch.create_check_constraint("ck_scheduled_tasks_state", "state IN (1, 2, 3)")
