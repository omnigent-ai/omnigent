"""Add an optional project mandate loaded into every project session.

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d

Additive and nullable: NULL means the project has no mandate, so no rows are
rewritten. Older binaries ignore the column, so downgrade retains it and any
stored mandates; a re-run upgrade skips an existing column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary

revision: str = "5f6a7b8c9d0e"
down_revision: str | None = "4e5f6a7b8c9d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "mandate" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")
    }:
        op.add_column("projects", sa.Column("mandate", BoundedBinary(16383), nullable=True))


def downgrade() -> None:
    # Retaining the unused additive column is safe for old binaries and keeps mandates.
    pass
