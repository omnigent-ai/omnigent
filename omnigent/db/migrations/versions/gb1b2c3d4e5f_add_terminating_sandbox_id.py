"""Add pending managed sandbox termination to hosts.

Revision ID: gb1b2c3d4e5f
Revises: ga1b2c3d4e5f
Create Date: 2026-09-03 00:00:00.000000

The managed sandbox reaper detaches a stale provider id from the active host
generation before making the external termination call. Persisting that id on
the host row makes failed cleanup retryable while allowing a fresh generation
to launch under the durable host identity.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gb1b2c3d4e5f"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the retryable pending-termination slot."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("terminating_sandbox_id", sa.String(256), nullable=True))


def downgrade() -> None:
    """Remove the pending-termination slot."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("terminating_sandbox_id")
