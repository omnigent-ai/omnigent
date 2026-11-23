"""Add project identities: named persistent roles with a single active session.

Revision ID: b7e3d1f9a6c4
Revises: d8e2b5a7c901

Additive: a new table that older binaries ignore. Downgrade drops it and the
identities it holds; their sessions remain ordinary project conversations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "b7e3d1f9a6c4"
down_revision: str | None = "d8e2b5a7c901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "project_identities" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "project_identities",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("name", BoundedBinary(1024), nullable=False),
        sa.Column("instructions", BoundedBinary(16383), nullable=False),
        sa.Column("agent_id", sa.String(128), nullable=True),
        sa.Column("model", sa.String(256), nullable=True),
        sa.Column("effort", sa.String(64), nullable=True),
        sa.Column("active_session_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "id"),
    )
    op.create_index(
        "ix_project_identities_created",
        "project_identities",
        ["workspace_id", "project_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_identities_created", table_name="project_identities")
    op.drop_table("project_identities")
