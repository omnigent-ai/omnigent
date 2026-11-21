"""Add optional user-owned host labels without changing host identity.

Revision ID: no1a2b3c4d5e
Revises: nn1a2b3c4d5e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "no1a2b3c4d5e"
down_revision: str | None = "nn1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Old rows and old writers leave the optional label unset."""
    with op.batch_alter_table("hosts") as batch:
        batch.add_column(sa.Column("display_name", sa.String(64), nullable=True))


def downgrade() -> None:
    """Remove labels without changing host registration names."""
    with op.batch_alter_table("hosts") as batch:
        batch.drop_column("display_name")
