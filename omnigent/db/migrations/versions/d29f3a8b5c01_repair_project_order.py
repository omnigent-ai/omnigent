"""Ensure project ordering exists on databases stamped by an older merge graph.

Revision ID: d29f3a8b5c01
Revises: c18e2f7a4b90
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMBLOB

revision: str = "d29f3a8b5c01"
down_revision: str | None = "c18e2f7a4b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Repair skipped DDL without replacing existing ordering preferences."""
    columns = {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "project_order" in columns:
        column = columns["project_order"]
        if not isinstance(column["type"], sa.LargeBinary) or not column["nullable"]:
            raise RuntimeError("users.project_order must be nullable binary data")
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column(
                "project_order",
                sa.LargeBinary().with_variant(MEDIUMBLOB(), "mysql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Retain the column owned by the earlier project-order migration."""
