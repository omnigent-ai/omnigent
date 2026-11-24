"""Let agents create project identities directly; drop identity proposals.

Revision ID: d4a8e1c7b2f6
Revises: c9f4e2a8b1d3

Adds nullable ``project_identities.created_by_session_id``: NULL marks an
owner-made identity, which agents may not edit. Drops the
``project_identity_proposals`` table, because agents no longer wait for
approval and pending proposals are discarded. Downgrade recreates the empty table
and keeps the additive column, which older binaries ignore.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "d4a8e1c7b2f6"
down_revision: str | None = "c9f4e2a8b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("project_identities")}
    if "created_by_session_id" not in columns:
        op.add_column(
            "project_identities",
            sa.Column("created_by_session_id", Uuid16(), nullable=True),
        )
    if "project_identity_proposals" in inspector.get_table_names():
        op.drop_index(
            "ix_project_identity_proposals_created", table_name="project_identity_proposals"
        )
        op.drop_table("project_identity_proposals")


def downgrade() -> None:
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
