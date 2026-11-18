"""Add bounded, enveloped file-comment replies.

Revision ID: 4d8e2b7f1a63
Revises: ffeec66eadb5

Deploy schema before reply writers. Running older processes ignore this additive
table, but this revision is the binary rollback floor: older binaries refuse
unknown revisions at startup (omnigent.db.utils). Stop reply writers and downgrade
schema to ffeec66eadb5 with this build before starting an older binary. Downgrade
drops all replies; export any needed data first.

The body is an encryption-capable envelope, including framing and metadata,
bounded to 16,383 stored bytes. No column defaults or foreign keys are added.
The primary-key prefix supports thread/conversation cleanup; the thread index
supports (created_at, id) pagination. SQLite/D1 CHECKs enforce types and bounds.
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16
from omnigent.db.envelope import EnvelopedText

revision: str = "4d8e2b7f1a63"
down_revision: str | None = "ffeec66eadb5"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the reply table without touching existing comments."""
    constraints = [
        sa.CheckConstraint("author_kind IN (1, 2)", name="ck_comment_replies_author_kind")
    ]
    if op.get_bind().dialect.name in ("sqlite", "cloudflare_d1"):
        constraints.extend(
            [
                sa.CheckConstraint(
                    "typeof(workspace_id) = 'integer'",
                    name="ck_comment_replies_workspace_id_integer",
                ),
                sa.CheckConstraint(
                    "typeof(conversation_id) = 'blob' AND length(conversation_id) = 16",
                    name="ck_comment_replies_conversation_id_bytes",
                ),
                sa.CheckConstraint(
                    "typeof(comment_id) = 'blob' AND length(comment_id) = 16",
                    name="ck_comment_replies_comment_id_bytes",
                ),
                sa.CheckConstraint(
                    "typeof(id) = 'blob' AND length(id) = 16", name="ck_comment_replies_id_bytes"
                ),
                sa.CheckConstraint(
                    "typeof(body) = 'blob' AND length(body) <= 16383",
                    name="ck_comment_replies_body_bytes",
                ),
                sa.CheckConstraint(
                    "created_by IS NULL OR (typeof(created_by) = 'text' "
                    "AND length(CAST(created_by AS BLOB)) <= 512)",
                    name="ck_comment_replies_created_by_bytes",
                ),
                sa.CheckConstraint(
                    "typeof(created_at) = 'integer'", name="ck_comment_replies_created_at_integer"
                ),
            ]
        )
    op.create_table(
        "comment_replies",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("comment_id", Uuid16(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("author_kind", sa.SmallInteger(), nullable=False),
        sa.Column("body", EnvelopedText(16_383), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "comment_id", "id"),
        *constraints,
    )
    op.create_index(
        "ix_comment_replies_thread",
        "comment_replies",
        ["workspace_id", "conversation_id", "comment_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop replies after all reply writers have stopped."""
    op.drop_index("ix_comment_replies_thread", table_name="comment_replies")
    op.drop_table("comment_replies")
