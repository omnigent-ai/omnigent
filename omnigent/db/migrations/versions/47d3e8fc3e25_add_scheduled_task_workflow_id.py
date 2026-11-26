"""Add the saved workflow a scheduled task's runs follow.

Revision ID: 47d3e8fc3e25
Revises: bf30d7cbda61

``workflow_id`` names the saved workflow every run of the task starts as a run
of, so whether an automation runs a saved workflow is stored rather than read
from its prompt. Like ``source_session_id``, it relates to another table, here
``workflows.id``, with no database foreign key. Tasks are read by primary key,
so no index is added.

The column is nullable, so this migration runs online, and servers already
running from before it keep working: they don't read the column. An older
server that starts after it refuses the database revision it doesn't know.
The model maps the column, so every read of a task selects it, and the
migration must run before any server with that mapping starts. Servers apply
pending migrations when they first open the database; a deployment that runs
migrations separately must apply this one first. The release that writes the
column still saves each such task's run message in ``prompt``, so a server
without that release keeps running those tasks from their saved prompts.

For rollback, stop every server that has this migration and keep them from
restarting, downgrade to ``bf30d7cbda61`` with one of their builds, then start
the older release. The downgrade drops the column; the tasks keep running their
saved prompts.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "47d3e8fc3e25"
down_revision: str | None = "bf30d7cbda61"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable saved workflow."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("workflow_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Drop the saved workflow."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("workflow_id")
