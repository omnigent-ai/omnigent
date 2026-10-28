"""Add the skills marketplace tables.

Revision ID: zb1c2d3e4f5a
Revises: b2e4f6a8c013
Create Date: 2026-10-28 00:00:00.000000

Creates ``skill_sources`` (repositories the marketplace browses) and
``installed_skills`` (skills fetched from a repository and stored as tarballs
in the artifact store). Both are workspace-partitioned like the other
application tables, carry no database foreign keys (schema Rule R032), and
declare no SQL defaults: the application supplies every value.

Additive. No existing data is touched. Tables and indexes are checked and
created independently, so a run interrupted between the two is repaired by
the next run; a pre-existing table with a different column set is refused
rather than stamped over.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zb1c2d3e4f5a"
down_revision: str | None = "b2e4f6a8c013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _skill_sources_columns() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("url", sa.String(255), nullable=False),
        sa.Column("ref", sa.String(255), nullable=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    ]


def _installed_skills_columns() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("source_url", sa.String(255), nullable=False),
        sa.Column("ref", sa.String(255), nullable=True),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("path", sa.String(1024), nullable=False),
        # Stable int code (omnigent.db.enum_codecs INSTALLED_SKILL_SCOPE: user=1, org=2).
        sa.Column("scope", sa.SmallInteger(), nullable=False),
        # "" for an org-scoped row, never NULL, so the identity index is unique everywhere.
        sa.Column("owner_user_id", sa.String(128), nullable=False),
        sa.Column("bundle_location", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("installed_by", sa.String(128), nullable=True),
        sa.Column("installed_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
    ]


# (table, columns, extra constraints, indexes as (name, columns, unique))
_TABLES = [
    (
        "skill_sources",
        _skill_sources_columns,
        (),
        [
            ("ix_skill_sources_order", ["workspace_id", "created_at", "id"], False),
            ("ix_skill_sources_identity", ["workspace_id", "url", "ref", "id"], False),
        ],
    ),
    (
        "installed_skills",
        _installed_skills_columns,
        (sa.CheckConstraint("scope IN (1, 2)", name="ck_installed_skills_scope"),),
        [
            (
                "ux_installed_skills_identity",
                ["workspace_id", "owner_user_id", "name", "scope"],
                True,
            ),
            (
                "ix_installed_skills_owner_order",
                ["workspace_id", "owner_user_id", "installed_at", "id"],
                False,
            ),
        ],
    ),
]


def _existing_columns(table: str) -> set[str] | None:
    """The table's column names, or ``None`` when it does not exist."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    """Create ``skill_sources`` and ``installed_skills`` and their indexes."""
    for table, columns, constraints, indexes in _TABLES:
        wanted = columns()
        existing = _existing_columns(table)
        if existing is None:
            op.create_table(
                table, *wanted, *constraints, sa.PrimaryKeyConstraint("workspace_id", "id")
            )
        elif existing != {column.name for column in wanted}:
            raise RuntimeError(
                f"table {table} already exists with columns {sorted(existing)}; "
                "drop or rename it before upgrading"
            )
        present = _existing_indexes(table)
        for name, index_columns, unique in indexes:
            if name not in present:
                op.create_index(name, table, index_columns, unique=unique)


def downgrade() -> None:
    """Drop the skills marketplace tables; tolerates a partially applied upgrade."""
    for table, _columns, _constraints, indexes in reversed(_TABLES):
        if _existing_columns(table) is None:
            continue
        present = _existing_indexes(table)
        for name, _index_columns, _unique in reversed(indexes):
            if name in present:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
