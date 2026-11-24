"""Add where each session's plan stands to its journal heads row.

Revision ID: ce6afa56e9f7
Revises: 47d3e8fc3e25

``session_journal_heads.plan_progress`` holds a few counts and the name of the
step the session's current plan is on, so a session list can show a workflow
run's step by reading heads rows by primary key, without reading any journal.
Appends already lock and update the heads row, so the write that changes a
plan or a step's status rewrites it in the same transaction. The journal store
encodes it with its content codec, so a deployment that encrypts journal
content encrypts it too; it stays opaque to the database and is never filtered
on. Values are at most 2,048 bytes; MySQL gets a ``VARBINARY(2048)``.

The column is nullable with no default, so this migration runs online and
needs no backfill: the release that records progress writes a session's value
on its next plan or status write, and the list shows no step until then.
Servers already running from before it keep working, since they don't read or
write the column. An older server that starts after it refuses the database
revision it doesn't know. The model maps the column, so inserting a heads row
names it, and the migration must run before any server with that mapping
starts. Servers apply pending migrations when they first open the database; a
deployment that runs migrations separately must apply this one first.

For rollback, stop every server that has this migration and keep them from
restarting, downgrade to ``47d3e8fc3e25`` with one of their builds, then start
the older release. The downgrade drops the column; journals are unchanged.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY

revision: str = "ce6afa56e9f7"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable plan progress."""
    with op.batch_alter_table("session_journal_heads") as batch:
        batch.add_column(
            sa.Column(
                "plan_progress",
                sa.LargeBinary().with_variant(VARBINARY(2048), "mysql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Drop the plan progress."""
    with op.batch_alter_table("session_journal_heads") as batch:
        batch.drop_column("plan_progress")
