"""add trigger to runs

Revision ID: q1a2b3c4d5e6
Revises: p1a2b3c4d5e6
Create Date: 2026-06-26 18:30:00.000000

Adds the ``trigger`` column to the ``runs`` table: how a run was triggered —
``adhoc`` (a manual "Run now") or ``scheduled`` (spawned by the background
time-trigger scheduler). The column is NOT NULL with a ``server_default`` of
``adhoc`` so existing rows backfill on upgrade; the ORM always supplies the
value explicitly on insert. The default is intentionally *kept* — dropping it
in the same ``batch_alter_table`` block makes SQLite rebuild the table without
applying it to existing rows, violating the NOT NULL constraint.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q1a2b3c4d5e6"
down_revision: str | None = "p1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger",
                sa.String(length=32),
                nullable=False,
                server_default="adhoc",
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("trigger")
