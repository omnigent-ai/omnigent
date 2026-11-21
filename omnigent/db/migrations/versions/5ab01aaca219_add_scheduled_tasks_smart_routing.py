"""Add the Smart Routing choice to scheduled tasks.

Revision ID: 5ab01aaca219
Revises: d3bda6dbade3

``smart_routing`` records whether the owner wants the task's runs to use Smart
Routing instead of a fixed model: on (1) or off (2). ``NULL`` means the owner
never chose, which keeps a later default separate from an explicit off. Tasks
are read with their row, never filtered by it in SQL, so no index is added.

Additive and unused by earlier releases, so this schema can ship before the
application code that reads it. Downgrade drops the column and every stored
choice with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "5ab01aaca219"
down_revision: str | None = "d3bda6dbade3"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the choice, unset on every existing task."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("smart_routing", sa.SmallInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_scheduled_tasks_smart_routing", "smart_routing IN (1, 2)"
        )


def downgrade() -> None:
    """Drop the choice."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_constraint("ck_scheduled_tasks_smart_routing", type_="check")
        batch.drop_column("smart_routing")
