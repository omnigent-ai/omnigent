"""add config to projects

Revision ID: b3c4d5e6f7a8
Revises: f4664ca64ea8
Create Date: 2026-07-23 00:00:00.000000

Phase 2 of the projects feature (see ``designs/PROJECTS_PRD.md`` §8.1): gives a
project a place to store its default session settings (host, workspace, harness,
model, reasoning effort, git base-branch, …). Adds a nullable ``config`` text
column to ``projects`` holding a compact JSON object of those hints. ``NULL``
means "no stored defaults".

The column is an opaque JSON object: the backend persists it whole and never
filters on it, so the set of keys is owned by the client (the new-chat dialog)
and can grow without a schema change. The stored values are *hints* — the
dialog pre-fills them and always lets the user override, dropping any that
aren't currently satisfiable (e.g. the default host is offline).

Additive: a nullable column with no default, so an older server binary reading
the migrated DB simply ignores it. Rollback is a clean ``downgrade()`` (drops
the column) since no existing data is rewritten.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "f4664ca64ea8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``config`` column to ``projects``."""
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("config", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the ``config`` column."""
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("config")
