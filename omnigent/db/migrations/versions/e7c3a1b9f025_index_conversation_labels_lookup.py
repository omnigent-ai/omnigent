"""Index session-family membership lookups without changing existing labels.

Deploy before session-family readers. Existing binaries ignore the index;
rolling the application back does not require dropping it. Downgrade removes
only the index. Key/value equality followed by conversation_id keyset paging
uses a covering index rather than scanning every label in a workspace.
"""

from alembic import op

revision: str = "e7c3a1b9f025"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_conversation_labels_lookup",
        "conversation_labels",
        ["workspace_id", "key", "value", "conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_labels_lookup", table_name="conversation_labels")
