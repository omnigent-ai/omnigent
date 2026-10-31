"""Add nullable sibling-family membership to conversation metadata.

Deploy before application readers and writers. Existing sessions remain
standalone and older binaries omit the nullable column. Application rollback
can leave the schema installed; downgrade discards family membership only.
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "f1c7a9e2b406"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "omnigent_conversation_metadata", sa.Column("family_id", Uuid16(), nullable=True)
    )
    op.create_index(
        "ix_conversation_metadata_family_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "family_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_metadata_family_id", table_name="omnigent_conversation_metadata"
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch:
        batch.drop_column("family_id")
