"""Index the sessions bound to a host for managed startup recovery."""

from alembic import op

revision = "nf12ab34cd56"
down_revision = "mm1a2b3c4d5e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_conversation_metadata_host_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "host_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_metadata_host_id", table_name="omnigent_conversation_metadata")
