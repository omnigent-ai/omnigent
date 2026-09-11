"""add version to hosts

Revision ID: gg1b2c3d4e5f
Revises: gf1b2c3d4e5f
Create Date: 2026-09-10 00:00:00.000000

Adds ``hosts.version`` — the omnigent version a host reports in its
``host.hello`` frame, e.g. ``"0.13.0.dev3"``. NULL means the host has not
connected since the column existed. Surfaced via ``GET /v1/hosts`` so the
web UI can tell a user their host runs an older build than the server.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "gg1b2c3d4e5f"
down_revision: str | None = "gf1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``version`` column to ``hosts``.

    Batch mode so the DDL runs on SQLite too, and so the project's
    migration-safety test (which requires every schema change to go
    through ``batch_alter_table``) passes.
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.add_column(sa.Column("version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Drop the ``version`` column from ``hosts``.

    Batch mode so ``DROP COLUMN`` works on SQLite (rejected by the bare
    ``op`` proxy pre-3.35).
    """
    with op.batch_alter_table("hosts") as batch_op:
        batch_op.drop_column("version")
