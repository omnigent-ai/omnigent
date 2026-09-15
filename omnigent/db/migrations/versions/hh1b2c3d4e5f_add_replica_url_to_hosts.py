"""add replica_url to hosts

Revision ID: hh1b2c3d4e5f
Revises: gg1b2c3d4e5f
Create Date: 2026-09-15 00:00:00.000000

Adds ``hosts.replica_url`` — the base URL peer server replicas can reach
the replica holding this host's live tunnel on, e.g.
``'http://10.68.3.7:8000'``. Stamped by the owning replica on tunnel
connect and heartbeat, and read by a replica that receives a mis-routed
session request so it can forward the request to the owner instead of
stranding the session on ``400 wrong_replica``. NULL means the owning
replica has no advertised address (forwarding stays disabled and the
pre-existing wrong-replica error is returned as before).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "hh1b2c3d4e5f"
down_revision: str | None = "gg1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``replica_url`` column to ``hosts``.

    Batch mode so the DDL runs on SQLite too, and so the project's
    migration-safety test (which requires every schema change to go
    through ``batch_alter_table``) passes.
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("replica_url", sa.String(256), nullable=True))


def downgrade() -> None:
    """Drop the ``replica_url`` column from ``hosts``.

    Batch mode so ``DROP COLUMN`` works on SQLite (rejected by the bare
    ``op`` proxy pre-3.35).
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("replica_url")
