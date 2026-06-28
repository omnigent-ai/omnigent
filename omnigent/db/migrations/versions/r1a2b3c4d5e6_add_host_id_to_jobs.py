"""add host_id to jobs

Revision ID: r1a2b3c4d5e6
Revises: q1a2b3c4d5e6
Create Date: 2026-06-28 14:00:00.000000

Adds the ``host_id`` column to the ``jobs`` table: the preferred host a job's
runs launch their runner on, persisted in the job definition. Nullable (pick any
online host when unset) and a plain id with no foreign key, so host lifecycle is
decoupled from the job.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r1a2b3c4d5e6"
down_revision: str | None = "q1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("host_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("host_id")
