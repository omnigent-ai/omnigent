"""add connect_generation to hosts

Revision ID: gb2c3d4e5f6a
Revises: ll1a2b3c4d5e
Create Date: 2026-09-02 00:00:00.000000

Nullable epoch-µs tokens let pre-registry cleanup compare-and-update across
replicas. Registered disconnects remain unguarded; legacy NULLs never match.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gb2c3d4e5f6a"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable host connect tokens."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("connect_generation", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Drop host connect tokens."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("connect_generation")
