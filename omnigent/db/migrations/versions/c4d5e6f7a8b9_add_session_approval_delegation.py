"""Add delegated approval authority to session permissions.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the owner-controlled approval capability."""
    with op.batch_alter_table("session_permissions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "can_approve",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    """Remove delegated approval authority."""
    with op.batch_alter_table("session_permissions") as batch_op:
        batch_op.drop_column("can_approve")
