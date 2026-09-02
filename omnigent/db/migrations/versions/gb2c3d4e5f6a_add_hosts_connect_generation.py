"""add connect_generation to hosts

Revision ID: gb2c3d4e5f6a
Revises: gb1b2c3d4e5f
Create Date: 2026-09-02 00:00:00.000000

Adds ``hosts.connect_generation`` — an epoch-microseconds token stamped by
each connect's ``upsert_on_connect``. Cleanup paths that hold no live
``HostConnection`` (a connect that persisted its row but failed before
registering) mark the row offline with a compare-and-update against this
token, so a superseded connection cannot overwrite a newer connect's
online row — including across server replicas. NULL means the row was
last written before the column existed; a NULL token never matches, so
legacy rows are simply never conditionally offlined.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "gb2c3d4e5f6a"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``connect_generation`` column to ``hosts``.

    Batch mode so the DDL runs on SQLite too, and so the project's
    migration-safety test (which requires every schema change to go
    through ``batch_alter_table``) passes.
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("connect_generation", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Drop the ``connect_generation`` column from ``hosts``.

    Batch mode so ``DROP COLUMN`` works on SQLite (rejected by the bare
    ``op`` proxy pre-3.35).
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("connect_generation")
