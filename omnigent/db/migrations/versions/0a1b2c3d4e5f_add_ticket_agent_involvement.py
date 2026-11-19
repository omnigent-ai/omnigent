"""Add optional per-ticket agent involvement without changing existing links.

Revision ID: 0a1b2c3d4e5f
Revises: f9a0b1c2d3e4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary

revision: str = "0a1b2c3d4e5f"
down_revision: str | None = "f9a0b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_ticket_sessions", sa.Column("involvement", BoundedBinary(2048), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("project_ticket_sessions", "involvement")
