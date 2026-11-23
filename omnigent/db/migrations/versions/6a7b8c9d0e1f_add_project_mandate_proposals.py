"""Add proposed project-mandate additions that wait for the owner.

Revision ID: 6a7b8c9d0e1f
Revises: 5f6a7b8c9d0e

Additive and nullable: NULL means no pending proposals, so no rows are
rewritten. Older binaries ignore the column, so downgrade retains it and any
pending proposals; a re-run upgrade skips an existing column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary

revision: str = "6a7b8c9d0e1f"
down_revision: str | None = "5f6a7b8c9d0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "mandate_proposals" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")
    }:
        op.add_column(
            "projects", sa.Column("mandate_proposals", BoundedBinary(16383), nullable=True)
        )


def downgrade() -> None:
    # Retaining the unused additive column is safe for old binaries and keeps proposals.
    pass
