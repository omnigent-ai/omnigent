"""Shrink hosts.name from VARCHAR(256) to VARCHAR(64).

Revision ID: t1a2b3c4d5e6
Revises: s1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Host names come from ``~/.omnigent/config.yaml`` and are short identifiers
like ``"corey-laptop"``. 256 characters is far more than needed; 64 matches
every other short-identifier column in the schema and keeps the composite
primary key (workspace_id, owner, name) compact.

No FK constraints reference ``hosts.name`` (all FKs were removed in
p1a2b3c4d5e6), so no PRAGMA guard is required and no dependent indexes need
manual rebuilding — the batch rebuild recreates the table DDL from the current
metadata (String(64)) and the only constraint on ``name`` is its role as a
composite PK member.

Upgrade path:
  Batch-rebuild the ``hosts`` table, narrowing ``name`` from VARCHAR(256)
  to VARCHAR(64). recreate="always" on SQLite (cannot ALTER column types
  in-place); "auto" on other dialects.

Downgrade path:
  Batch-rebuild the table, widening ``name`` back to VARCHAR(256).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t1a2b3c4d5e6"
down_revision: str | None = "s1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Narrow hosts.name from VARCHAR(256) to VARCHAR(64)."""
    sqlite = _is_sqlite()

    with op.batch_alter_table("hosts", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(256),
            type_=sa.String(64),
            nullable=False,
        )


def downgrade() -> None:
    """Widen hosts.name back to VARCHAR(256)."""
    sqlite = _is_sqlite()

    with op.batch_alter_table("hosts", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.alter_column(
            "name",
            existing_type=sa.String(64),
            type_=sa.String(256),
            nullable=False,
        )
