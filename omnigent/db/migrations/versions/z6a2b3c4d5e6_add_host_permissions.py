"""add host permissions

Revision ID: z6a2b3c4d5e6
Revises: z5a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Adds the ``host_permissions`` table: a junction table mapping
``(user_id, host_id)`` to a numeric level (1=view, 2=use, 3=manage)
that shares a host with users who do not own it.

The FK targets the durable ``hosts.host_id`` UNIQUE column (not the
``(owner, name)`` primary key) so a grant survives an owner change
across host identity rotation / re-owning.

NO backfill: existing hosts stay private after migration. Owner and
admin access is resolved in code, not stored as grants, so there are
no rows to seed — a non-owner gains access only when an explicit
grant is created.

See ``specs/admin-host-management-spec.md``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z6a2b3c4d5e6"
down_revision: str | None = "z5a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the host_permissions table. No backfill — hosts stay private."""
    # Idempotent: some databases already have this table (created out-of-band
    # from the same model), so skip creation when present. The definition below
    # is the source of truth for a fresh DB.
    if sa.inspect(op.get_bind()).has_table("host_permissions"):
        return
    # No DB foreign keys (Rule R032): the application owns referential
    # cleanup — HostStore.delete_host removes a host's grants.
    op.create_table(
        "host_permissions",
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("host_id", sa.String(64), primary_key=True),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.CheckConstraint("level IN (1, 2, 3)", name="ck_host_permissions_level"),
    )
    op.create_index(
        "ix_host_permissions_host_id",
        "host_permissions",
        ["host_id"],
    )


def downgrade() -> None:
    """Drop the host_permissions table."""
    op.drop_index("ix_host_permissions_host_id", table_name="host_permissions")
    op.drop_table("host_permissions")
