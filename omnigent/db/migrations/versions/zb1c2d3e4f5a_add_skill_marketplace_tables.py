"""Add the skills marketplace tables.

Revision ID: zb1c2d3e4f5a
Revises: b2e4f6a8c013
Create Date: 2026-10-28 00:00:00.000000

Creates ``skill_sources`` (repositories the marketplace browses) and
``installed_skills`` (skills fetched from a repository and stored as tarballs
in the artifact store). Both are workspace-partitioned like the other
application tables, carry no database foreign keys (schema Rule R032), and
declare no SQL defaults: the application supplies every value.

Additive. No existing data is touched.
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


def upgrade() -> None:
    """Create ``skill_sources`` and ``installed_skills``."""
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "skill_sources" not in existing:
        op.create_table(
            "skill_sources",
            sa.Column("workspace_id", sa.BigInteger(), nullable=False),
            sa.Column("id", Uuid16(), nullable=False),
            sa.Column("url", sa.String(512), nullable=False),
            sa.Column("ref", sa.String(255), nullable=True),
            sa.Column("name", sa.String(256), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=True),
            sa.Column("created_at", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("workspace_id", "id"),
        )
        op.create_index(
            "ix_skill_sources_order", "skill_sources", ["workspace_id", "created_at", "id"]
        )
    if "installed_skills" not in existing:
        op.create_table(
            "installed_skills",
            sa.Column("workspace_id", sa.BigInteger(), nullable=False),
            sa.Column("id", Uuid16(), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("display_name", sa.String(256), nullable=True),
            sa.Column("description", sa.String(1024), nullable=False),
            sa.Column("source_url", sa.String(512), nullable=False),
            sa.Column("ref", sa.String(255), nullable=True),
            sa.Column("commit", sa.String(64), nullable=True),
            sa.Column("path", sa.String(1024), nullable=False),
            # Stable int code (omnigent.db.enum_codecs INSTALLED_SKILL_SCOPE: user=1, org=2).
            sa.Column("scope", sa.SmallInteger(), nullable=False),
            sa.Column("owner_user_id", sa.String(128), nullable=True),
            sa.Column("bundle_location", sa.String(512), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("installed_by", sa.String(128), nullable=True),
            sa.Column("installed_at", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.Integer(), nullable=True),
            sa.CheckConstraint("scope IN (1, 2)", name="ck_installed_skills_scope"),
            sa.PrimaryKeyConstraint("workspace_id", "id"),
        )
        op.create_index(
            "ix_installed_skills_owner_order",
            "installed_skills",
            ["workspace_id", "owner_user_id", "installed_at", "id"],
        )
        op.create_index(
            "ix_installed_skills_owner_name",
            "installed_skills",
            ["workspace_id", "owner_user_id", "name", "id"],
        )


def downgrade() -> None:
    """Drop the skills marketplace tables."""
    op.drop_index("ix_installed_skills_owner_name", table_name="installed_skills")
    op.drop_index("ix_installed_skills_owner_order", table_name="installed_skills")
    op.drop_table("installed_skills")
    op.drop_index("ix_skill_sources_order", table_name="skill_sources")
    op.drop_table("skill_sources")
