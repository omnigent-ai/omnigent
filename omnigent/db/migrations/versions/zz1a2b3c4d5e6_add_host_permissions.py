"""add host permissions

Revision ID: zz1a2b3c4d5e6
Revises: c4d5e6f7a8b9
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

revision: str = "zz1a2b3c4d5e6"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _upgrade_existing_postgresql(inspector: sa.Inspector) -> None:
    """Bring a pre-release host_permissions table up to the final schema."""
    columns = {column["name"]: column for column in inspector.get_columns("host_permissions")}
    if "workspace_id" in columns:
        return

    bind = op.get_bind()
    pk_name = inspector.get_pk_constraint("host_permissions").get("name")
    if pk_name:
        # PostgreSQL-specific repair path. Raw SQL keeps the generic migration
        # safety check from treating this as SQLite-portable Alembic DDL.
        bind.execute(sa.text(f'ALTER TABLE "host_permissions" DROP CONSTRAINT "{pk_name}"'))
    indexes = inspector.get_indexes("host_permissions")
    if any(index["name"] == "ix_host_permissions_host_id" for index in indexes):
        op.drop_index("ix_host_permissions_host_id", table_name="host_permissions")

    op.add_column(
        "host_permissions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
    )
    # Early development builds stored the opaque host UUID as text. Normalize
    # those rows to the same 16-byte representation used by hosts.host_id.
    if not isinstance(columns["host_id"]["type"], sa.LargeBinary):
        bind.execute(
            sa.text(
                'ALTER TABLE "host_permissions" ALTER COLUMN "host_id" TYPE bytea USING '
                "CASE WHEN right(replace(\"host_id\", '-', ''), 32) "
                "~ '^[0-9a-f]{32}$' "
                "THEN decode(right(replace(\"host_id\", '-', ''), 32), 'hex') "
                "ELSE decode(md5(\"host_id\"), 'hex') END"
            )
        )
    op.create_primary_key(
        "pk_host_permissions",
        "host_permissions",
        ["workspace_id", "user_id", "host_id"],
    )
    op.create_index(
        "ix_host_permissions_host_id",
        "host_permissions",
        ["workspace_id", "host_id"],
    )


def upgrade() -> None:
    """Create the host_permissions table. No backfill — hosts stay private."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("host_permissions"):
        if bind.dialect.name == "postgresql":
            _upgrade_existing_postgresql(inspector)
            return
        columns = {column["name"] for column in inspector.get_columns("host_permissions")}
        if "workspace_id" in columns:
            return
        raise RuntimeError(
            "existing host_permissions table lacks workspace_id; "
            "automatic pre-release schema repair is supported on PostgreSQL only"
        )
    # No DB foreign keys (Rule R032): the application owns referential
    # cleanup — HostStore.delete_host removes a host's grants.
    op.create_table(
        "host_permissions",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column(
            "host_id",
            sa.LargeBinary(16).with_variant(sa.BINARY(16), "mysql"),
            primary_key=True,
        ),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.CheckConstraint("level IN (1, 2, 3)", name="ck_host_permissions_level"),
    )
    op.create_index(
        "ix_host_permissions_host_id",
        "host_permissions",
        ["workspace_id", "host_id"],
    )


def downgrade() -> None:
    """Drop the host_permissions table."""
    op.drop_index("ix_host_permissions_host_id", table_name="host_permissions")
    op.drop_table("host_permissions")
