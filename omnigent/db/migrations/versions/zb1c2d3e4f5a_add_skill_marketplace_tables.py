"""Add the skills marketplace tables.

Revision ID: zb1c2d3e4f5a
Revises: d0a1f879a01d
Create Date: 2026-10-28 00:00:00.000000

Creates ``skill_sources`` (repositories the marketplace browses),
``installed_skills`` (skills fetched from a repository and stored as tarballs
in the artifact store) and ``skill_principals`` (one lock row per principal
that installs, carrying its row count and enabled bytes, so a quota is
checked and written under one lock without scanning its rows). All are
workspace-partitioned like the other application tables, carry no database
foreign keys (schema Rule R032), and declare no SQL defaults: the
application supplies every value.

Additive. No existing data is touched. Tables and indexes are checked and
created independently, so a run interrupted between the two is repaired by
the next run. The guard compares names only: a pre-existing table whose
column names differ, or an index of the same name with the wrong uniqueness,
is refused rather than stamped over; it does not compare column types or
constraints.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from omnigent.db.db_models import Uuid16

revision: str = "zb1c2d3e4f5a"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _exact_string(length: int) -> sa.String:
    """Identity text compared byte for byte on MySQL too; see ``db_models._exact_string``."""
    return sa.String(length).with_variant(
        mysql.VARCHAR(length, charset="utf8mb4", collation="utf8mb4_bin"), "mysql"
    )


def _skill_sources_columns() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("url", _exact_string(255), nullable=False),
        # "HEAD", git's name for the remote's default branch, when none was chosen.
        sa.Column("ref", _exact_string(255), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    ]


def _installed_skills_columns() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("name", _exact_string(255), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("source_url", sa.String(255), nullable=False),
        sa.Column("ref", sa.String(255), nullable=True),
        sa.Column("commit", sa.String(64), nullable=True),
        sa.Column("path", sa.String(1024), nullable=False),
        # Stable int code (omnigent.db.enum_codecs INSTALLED_SKILL_SCOPE: user=1, org=2).
        sa.Column("scope", sa.SmallInteger(), nullable=False),
        # "org", "local" or "user:<owner_user_id>": never absent, partitions every read.
        sa.Column("principal", _exact_string(133), nullable=False),
        sa.Column("owner_user_id", sa.String(128), nullable=True),
        sa.Column("bundle_location", sa.String(512), nullable=False),
        sa.Column("bundle_bytes", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # From the skill's own SKILL.md: false keeps it out of the user's menu
        # while it is still delivered to the agent.
        sa.Column("user_invocable", sa.Boolean(), nullable=False),
        sa.Column("installed_by", sa.String(128), nullable=True),
        sa.Column("installed_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
    ]


def _skill_principals_columns() -> list[sa.Column]:
    return [
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("principal", _exact_string(133), nullable=False),
        # Maintained under the row's lock: a quota check reads these, never the rows.
        sa.Column("installed_count", sa.Integer(), nullable=False),
        sa.Column("enabled_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    ]


# (table, columns, primary key, extra constraints, indexes as (name, columns, unique))
_TABLES = [
    (
        "skill_sources",
        _skill_sources_columns,
        ("workspace_id", "id"),
        (),
        [
            ("ix_skill_sources_order", ["workspace_id", "created_at", "id"], False),
            ("ux_skill_sources_identity", ["workspace_id", "url", "ref"], True),
        ],
    ),
    (
        "installed_skills",
        _installed_skills_columns,
        ("workspace_id", "id"),
        (sa.CheckConstraint("scope IN (1, 2)", name="ck_installed_skills_scope"),),
        [
            ("ux_installed_skills_identity", ["workspace_id", "principal", "name"], True),
            (
                "ix_installed_skills_principal_order",
                ["workspace_id", "principal", "installed_at", "id"],
                False,
            ),
        ],
    ),
    (
        "skill_principals",
        _skill_principals_columns,
        ("workspace_id", "principal"),
        (),
        [],
    ),
]


def _existing_columns(table: str) -> set[str] | None:
    """The table's column names, or ``None`` when it does not exist."""
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def _existing_indexes(table: str) -> dict[str, bool]:
    """The table's index names mapped to whether each is unique."""
    return {
        index["name"]: bool(index["unique"])
        for index in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    """Create the three tables and their indexes."""
    for table, columns, primary_key, constraints, indexes in _TABLES:
        wanted = columns()
        existing = _existing_columns(table)
        if existing is None:
            op.create_table(table, *wanted, *constraints, sa.PrimaryKeyConstraint(*primary_key))
        elif existing != {column.name for column in wanted}:
            raise RuntimeError(
                f"table {table} already exists with columns {sorted(existing)}; "
                "drop or rename it before upgrading"
            )
        present = _existing_indexes(table)
        for name, index_columns, unique in indexes:
            if name not in present:
                op.create_index(name, table, index_columns, unique=unique)
            elif present[name] != unique:
                raise RuntimeError(
                    f"index {name} on {table} already exists with unique={present[name]}; "
                    "drop it before upgrading"
                )


def downgrade() -> None:
    """Drop the skills marketplace tables; tolerates a partially applied upgrade."""
    for table, _columns, _primary_key, _constraints, indexes in reversed(_TABLES):
        if _existing_columns(table) is None:
            continue
        present = _existing_indexes(table)
        for name, _index_columns, _unique in reversed(indexes):
            if name in present:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
