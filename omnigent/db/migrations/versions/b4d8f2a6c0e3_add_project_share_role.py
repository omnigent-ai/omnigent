"""Add a role to project shares, so the owner can make a member a Manager.

Revision ID: b4d8f2a6c0e3
Revises: e5c2a9f7d3b1

Adds ``project_shares.role`` (``member`` or ``manager``), NOT NULL with a
server default of ``member``, so every existing share keeps its current access
and inserts from older binaries get ``member``.

Rollback: older binaries ignore the column and treat every share as an
ordinary member, so a Manager only loses the ability to invite. Downgrade
drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d8f2a6c0e3"
down_revision: str | None = "e5c2a9f7d3b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("project_shares")}
    if "role" not in columns:
        op.add_column(
            "project_shares",
            sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        )


def downgrade() -> None:
    with op.batch_alter_table("project_shares") as batch:
        batch.drop_column("role")
