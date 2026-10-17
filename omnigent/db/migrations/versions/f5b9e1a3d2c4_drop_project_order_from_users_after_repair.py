"""Remove project_order column re-added by d29f3a8b5c01 repair migration.

The d29f3a8b5c01 repair migration unconditionally adds project_order to users
when it's missing, but jj1a2b3c4d5e moves the data to preferences and drops it.
After merging both branches, project_order should only be in preferences.

Revision ID: f5b9e1a3d2c4
Revises: ce67dc4f3baa
Create Date: 2026-09-26 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5b9e1a3d2c4"
down_revision: str | None = "ce67dc4f3baa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove project_order if it was re-added after jj migration moved it to preferences."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Check if both tables exist and project_order is in users
    if not inspector.has_table("users") or not inspector.has_table("preferences"):
        return

    columns = {c["name"] for c in inspector.get_columns("users")}
    if "project_order" not in columns:
        return

    # If we got here, project_order was re-added to users (by d29f3a8b5c01 on a merged branch).
    # Remove it since preferences has the canonical copy.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("project_order")


def downgrade() -> None:
    """Restore project_order column to users (will be added by repair migration on downgrade)."""
