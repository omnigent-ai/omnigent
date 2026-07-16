"""add user_preferences table

Revision ID: b1c2d3e4f5a6
Revises: a7b3c4d5e6f7
Create Date: 2026-07-16 00:00:00.000000

Adds the ``user_preferences`` table: per-user client UI state that should
follow the account rather than live in one browser's ``localStorage`` — the
sidebar pin set and the section collapse/expand sets.

A generic ``(user_id, key)`` → JSON-``value`` KV so each preference is its own
row and adding a future one needs no migration. The table is brand-new and
created at the current schema state, so it carries the tenant-partition
``workspace_id`` column as the leading primary-key member (matching every
other table after ``r1a2b3c4d5e6``). There is no foreign-key constraint on
``user_id`` (schema Rule R032 — see ``p1a2b3c4d5e6``): the relationship to
``users.id`` is enforced by the application, not the database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a7b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``user_preferences`` table."""
    op.create_table(
        "user_preferences",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        # Opaque JSON blob (e.g. a serialized id list); never SQL-queried.
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "key"),
    )


def downgrade() -> None:
    """Drop the ``user_preferences`` table."""
    op.drop_table("user_preferences")
