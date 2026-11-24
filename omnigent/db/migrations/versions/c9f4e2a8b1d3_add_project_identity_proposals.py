"""Add proposed project identities that wait for the owner to accept.

Revision ID: c9f4e2a8b1d3
Revises: b7e3d1f9a6c4

Additive: a new table that older binaries ignore. Downgrade drops it and any
pending proposals; accepted ones already live in ``project_identities``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "c9f4e2a8b1d3"
down_revision: str | None = "b7e3d1f9a6c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "project_identity_proposals" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "project_identity_proposals",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("identity_id", Uuid16(), nullable=True),
        sa.Column("name", BoundedBinary(1024), nullable=False),
        sa.Column("instructions", BoundedBinary(16383), nullable=False),
        sa.Column("rationale", BoundedBinary(2048), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "id"),
    )
    op.create_index(
        "ix_project_identity_proposals_created",
        "project_identity_proposals",
        ["workspace_id", "project_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_identity_proposals_created", table_name="project_identity_proposals")
    op.drop_table("project_identity_proposals")
