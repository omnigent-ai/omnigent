"""add connect_generation to hosts

Revision ID: gb2c3d4e5f6a
Revises: mm1a2b3c4d5e
Create Date: 2026-09-02 00:00:00.000000

Adds a nullable per-connect token so host offline cleanup can compare-and-update
across replicas; legacy NULL rows never match a token.

Deployment order: ship this schema change before the application change that
reads the column. Older servers ignore the column and keep their unconditional
cleanup, so the guard is fully effective once every replica runs the new
application code and each host has reconnected (stamping its token). The
application change can be reverted on its own; downgrade this column only after
no running server reads it. Both directions are retry-safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gb2c3d4e5f6a"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "connect_generation"


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == _COLUMN for column in inspector.get_columns("hosts"))


def upgrade() -> None:
    """Add the nullable host connect token unless a retried run already did."""
    if _has_column():
        return
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Drop the host connect token if it is present."""
    if not _has_column():
        return
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column(_COLUMN)
