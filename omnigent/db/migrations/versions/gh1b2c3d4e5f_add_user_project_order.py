"""Add a nullable personal project order to users."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "gh1b2c3d4e5f"
down_revision: str | None = "gg1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Missing preferences retain alphabetical sorting; no backfill is needed."""
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("project_order", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Remove ordering preferences while retaining users and projects."""
    with op.batch_alter_table("users") as batch:
        batch.drop_column("project_order")
