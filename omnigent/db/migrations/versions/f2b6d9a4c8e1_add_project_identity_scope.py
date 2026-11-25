"""Give a project role a structured "Owns / Doesn't own" scope.

Revision ID: f2b6d9a4c8e1
Revises: b4d8f2a6c0e3

Adds nullable ``project_identities.scope``: the role's ``owns`` and
``excludes`` lists as an enveloped JSON object. NULL means no scope yet, which
every existing role starts with. No data changes.

Rollback: older binaries ignore the column, so roles keep working with their
persona only. Downgrade drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary

# Fixed here, not imported, so the migration keeps its shape if the model changes.
PROJECT_IDENTITY_SCOPE_MAX_BYTES = 16383

revision: str = "f2b6d9a4c8e1"
down_revision: str | None = "b4d8f2a6c0e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("project_identities")}
    if "scope" not in columns:
        op.add_column(
            "project_identities",
            sa.Column("scope", BoundedBinary(PROJECT_IDENTITY_SCOPE_MAX_BYTES), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("project_identities") as batch:
        batch.drop_column("scope")
