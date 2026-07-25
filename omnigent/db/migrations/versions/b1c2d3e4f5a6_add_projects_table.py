"""add projects table

Revision ID: b1c2d3e4f5a6
Revises: d4f2a1b6c8e9
Create Date: 2026-07-16 00:00:00.000000

Promotes "projects" from the implicit ``omni_project`` conversation label to a
first-class entity (see ``designs/PROJECTS_PRD.md``). Adds the ``projects``
table — a user-defined, owner-private container that groups sessions and exists
independently of its members (so it can be empty, renamed, and carry its own
config).

This migration creates only the container. Session→project membership (the
``omnigent_conversation_metadata.project_id`` column) lands in a follow-up
migration alongside the store/route code that reads it, so this PR ships no
column that nothing consumes yet.

Additive. There are no foreign-key constraints (schema Rule R032): the
project relationship is enforced by the application, not the database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "d4f2a1b6c8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``projects`` table."""
    op.create_table(
        "projects",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID PK stored as 16 raw bytes (Uuid16), read back as bare hex.
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        # Owner stamped on the row (projects have no ACL, Rule R032 / PRD §9).
        # NULL in single-user / OSS mode.
        sa.Column("owner_user_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    # created_at is in the key so "list my projects" (WHERE workspace_id,
    # owner_user_id ORDER BY created_at, id) is a pure index scan — no filesort.
    op.create_index(
        "ix_projects_owner_user_id",
        "projects",
        ["workspace_id", "owner_user_id", "created_at", "id"],
        unique=False,
    )
    # UNIQUE on (workspace_id, owner_user_id, name): enforces per-owner name
    # uniqueness at the DB layer for non-NULL owners, closing the store's
    # check-then-insert race. SQL treats NULLs as distinct, so single-user rows
    # (owner_user_id IS NULL) can still share a name — the store's _name_taken
    # check covers that case. Also backs the get-by-name lookup.
    op.create_index(
        "ix_projects_name",
        "projects",
        ["workspace_id", "owner_user_id", "name"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``projects`` table."""
    op.drop_index("ix_projects_name", table_name="projects")
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    op.drop_table("projects")
