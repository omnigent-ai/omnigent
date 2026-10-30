"""add ssh_target to hosts

Revision ID: ed17a0b1c2d3
Revises: d0a1f879a01d
Create Date: 2026-10-27 00:00:00.000000

Adds ``hosts.ssh_target`` — the SSH destination the host's owner reaches it by
from their own machine (e.g. ``"arclet_dev.ssh"``), reported in ``host.hello``.
NULL means the host never reported one; there is no backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed17a0b1c2d3"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``ssh_target`` column to ``hosts`` (batch mode for SQLite)."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("ssh_target", sa.String(255), nullable=True))


def downgrade() -> None:
    """Drop the ``ssh_target`` column from ``hosts`` (batch mode for SQLite)."""
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("ssh_target")
