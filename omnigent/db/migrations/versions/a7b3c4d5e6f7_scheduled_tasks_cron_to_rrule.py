"""switch scheduled_tasks trigger from cron_expression to rrule

Revision ID: a7b3c4d5e6f7
Revises: z8a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Replaces the ``scheduled_tasks.cron_expression`` column with ``rrule``, an
RFC 5545 recurrence rule string (e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``).
The recurrence engine moves from cron expressions to RRULE; RRULE strings are
longer, so the column widens from ``String(255)`` to ``String(512)``.

The ``scheduled_tasks`` table holds zero rows in every deployment (the feature
is inert — no create endpoint and no fire path exist yet), so this is a pure
DDL swap: no backfill and no row transformation. The new column is added
``NOT NULL`` with an empty-string ``server_default`` purely to satisfy the
constraint for the (zero) existing rows; the default is dropped in the same
batch so future inserts must supply an explicit ``rrule``.

Batch mode (``op.batch_alter_table``) is mandatory: this repo runs Alembic with
``render_as_batch=True`` so SQLite — which cannot ``ALTER TABLE ... DROP
COLUMN`` in place — rebuilds the table via the copy-and-swap batch path. The
``timezone`` column is untouched: RRULE still needs a timezone anchor for
``DTSTART``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b3c4d5e6f7"
down_revision: str | None = "z8a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``rrule`` and drop ``cron_expression`` on ``scheduled_tasks``."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("rrule", sa.String(512), nullable=False, server_default=""))
        batch_op.drop_column("cron_expression")
        # The server_default only existed to satisfy NOT NULL for existing rows;
        # drop it so future inserts must supply an explicit rrule.
        batch_op.alter_column("rrule", server_default=None)


def downgrade() -> None:
    """Restore ``cron_expression`` and drop ``rrule`` on ``scheduled_tasks``."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(
            sa.Column("cron_expression", sa.String(255), nullable=False, server_default="")
        )
        batch_op.drop_column("rrule")
        batch_op.alter_column("cron_expression", server_default=None)
