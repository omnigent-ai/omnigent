"""Add logical deletion tombstones to managed hosts.

Revision ID: gc1b2c3d4e5f
Revises: gb1b2c3d4e5f
Create Date: 2026-09-05 00:00:00.000000

Managed host rows remain as internal tombstones while provider sandbox
termination is pending. This keeps cleanup retryable without exposing the
logically deleted host to users.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gc1b2c3d4e5f"
down_revision: str | None = "gb1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the logical deletion timestamp."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove the logical deletion timestamp."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("deleted_at")
