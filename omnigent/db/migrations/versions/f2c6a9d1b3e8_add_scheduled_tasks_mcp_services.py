"""add mcp_services column to scheduled_tasks

Revision ID: f2c6a9d1b3e8
Revises: e5b2c8d4f1a7
Create Date: 2026-11-05 00:00:00.000000

Adds a nullable ``mcp_services`` (VARCHAR(2048)) column to ``scheduled_tasks``: a JSON
list of the MCP service names every fired session starts with. ``NULL`` means
fired sessions get no MCP selection, which is how existing tasks behave.
Additive, with no backfill; older binaries ignore the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c6a9d1b3e8"
down_revision: str | None = "e5b2c8d4f1a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add mcp_services to scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("mcp_services", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    """Remove mcp_services from scheduled_tasks."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("mcp_services")
