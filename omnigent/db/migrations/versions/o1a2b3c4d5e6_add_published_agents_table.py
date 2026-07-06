"""add published_agents table for the community registry

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-06-30 00:00:00.000000

Adds the ``published_agents`` table that backs the community registry
feature (issue #67, "Registry for custom agents").

Each row is a distinct ``name@version`` publication. The table is
intentionally separate from the existing ``agents`` table — locally
registered template / session-scoped agents and community-published
entries have different lifecycles, versioning semantics, and access
patterns, and mixing the two would complicate both layers.

Indexes added:
- ``ix_published_agents_name``         — fast lookup by agent name (used
  by ``GET /v1/registry/{name}`` detail and ``omni get``).
- ``ix_published_agents_category``     — filter by category on browse.
- ``ix_published_agents_harness``      — filter by harness on browse.
- ``ix_published_agents_created_at``   — cursor-based pagination.
- ``ix_published_agents_stars_count``  — sort by popularity.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "published_agents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("harness", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False, server_default="'[]'"),
        sa.Column("prompt_excerpt", sa.Text(), nullable=True),
        sa.Column("network_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("write_access", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("guardrails", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=256), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("stars_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bundle_location", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_published_agents_name_version"),
    )
    op.create_index("ix_published_agents_name", "published_agents", ["name"])
    op.create_index("ix_published_agents_category", "published_agents", ["category"])
    op.create_index("ix_published_agents_harness", "published_agents", ["harness"])
    op.create_index("ix_published_agents_created_at", "published_agents", ["created_at"])
    op.create_index("ix_published_agents_stars_count", "published_agents", ["stars_count"])


def downgrade() -> None:
    op.drop_index("ix_published_agents_stars_count", table_name="published_agents")
    op.drop_index("ix_published_agents_created_at", table_name="published_agents")
    op.drop_index("ix_published_agents_harness", table_name="published_agents")
    op.drop_index("ix_published_agents_category", table_name="published_agents")
    op.drop_index("ix_published_agents_name", table_name="published_agents")
    op.drop_table("published_agents")
