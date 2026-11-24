"""Add where each session's plan stands to its journal heads row.

Revision ID: ce6afa56e9f7
Revises: 47d3e8fc3e25

Reserves two nullable columns on ``session_journal_heads`` for a follow-up
that records a bounded summary of each session's plan progress, so a session
list can show a workflow run's step by reading heads rows by primary key
instead of reading journals:

* ``plan_progress``: the summary, at most 2,048 bytes, encoded by the journal
  store's content codec. It will be opaque to the database and never filtered
  on; MySQL gets a ``VARBINARY(2048)``.
* ``plan_progress_position``: the ``next_position`` the summary was recorded at.
  A server without the follow-up advances ``next_position`` and leaves this
  alone, so a summary it left out of date can be recognized and ignored.

The columns are nullable with no default, so this migration runs online and
needs no backfill: a session gets values at its next plan or status write once
the follow-up ships, and shows no step until then. Servers already running
from before it keep working, since they don't read or write the columns. An
older server that starts after it refuses the database revision it doesn't
know. The model maps the columns, so inserting a heads row names them, and the
migration must run before any server with that mapping starts. Servers apply
pending migrations when they first open the database; a deployment that runs
migrations separately must apply this one first.

For rollback, stop every server that has this migration and keep them from
restarting, downgrade to ``47d3e8fc3e25`` with one of their builds, then start
the older release. The downgrade drops the columns; journals are unchanged.
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
    """Add the nullable plan progress and the position it was recorded at."""
    with op.batch_alter_table("session_journal_heads") as batch:
        batch.add_column(
            sa.Column(
                "plan_progress",
                sa.LargeBinary().with_variant(VARBINARY(2048), "mysql"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("plan_progress_position", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the plan progress and its position."""
    with op.batch_alter_table("session_journal_heads") as batch:
        batch.drop_column("plan_progress_position")
        batch.drop_column("plan_progress")
