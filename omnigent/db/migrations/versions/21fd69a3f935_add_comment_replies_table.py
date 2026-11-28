"""Add the comment_replies table for file comment threads.

Revision ID: 21fd69a3f935
Revises: f1c7a9e2b406

Each row is one message in a file comment's thread, from the user
(``author_kind`` 1) or the agent (2). The key nests a reply under its comment,
``(workspace_id, conversation_id, comment_id, id)``, so a thread and a
conversation's replies are both primary-key prefix ranges that the store can
delete in bounded batches. ``ix_comment_replies_thread`` reads one thread in
creation order, paginated by ``(created_at, id)``. ``comment_id`` relates to
``comments.id`` with no database foreign key (Rule R032).

``body`` is compressed text capped at 16,383 stored bytes by the ORM type.
SQLite and Cloudflare D1 do not enforce declared types, so there the table also
gets CHECKs bounding ``created_by`` to 512 bytes (128 characters at up to four
bytes each) and ``body`` to 16,383 bytes, and requiring an integer
``created_at``. The new table has no column defaults: writers supply every
value.

Additive: nothing reads or writes the table yet, so deploy it before the server
that does. Downgrade drops the table and any replies in it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "21fd69a3f935"
down_revision: str | None = "f1c7a9e2b406"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "comment_replies"
_THREAD_INDEX = "ix_comment_replies_thread"
_BODY_MAX_STORED_BYTES = 16_383
# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Create ``comment_replies`` and its thread index."""
    bind = op.get_bind()
    # Committed DDL can survive an upgrade interrupted before the revision stamp.
    if not sa.inspect(bind).has_table(_TABLE):
        sqlite_checks = (
            [
                sa.CheckConstraint(
                    "created_by IS NULL OR length(CAST(created_by AS BLOB)) <= 512",
                    name="ck_comment_replies_created_by_bytes",
                ),
                sa.CheckConstraint(
                    f"length(CAST(body AS BLOB)) <= {_BODY_MAX_STORED_BYTES}",
                    name="ck_comment_replies_body_bytes",
                ),
                sa.CheckConstraint(
                    "typeof(created_at) = 'integer'",
                    name="ck_comment_replies_created_at_integer",
                ),
            ]
            if bind.dialect.name in _SQLITE_FAMILY
            else []
        )
        op.create_table(
            _TABLE,
            sa.Column("workspace_id", sa.BigInteger(), nullable=False),
            sa.Column("conversation_id", Uuid16(), nullable=False),
            sa.Column("comment_id", Uuid16(), nullable=False),
            sa.Column("id", Uuid16(), nullable=False),
            # COMMENT_REPLY_AUTHOR_KIND int code: user=1, agent=2.
            sa.Column("author_kind", sa.SmallInteger(), nullable=False),
            # Compressed text (CompressedText -> LargeBinary), capped by the ORM.
            sa.Column("body", sa.LargeBinary(), nullable=False),
            sa.Column("created_by", sa.String(128), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.CheckConstraint("author_kind IN (1, 2)", name="ck_comment_replies_author_kind"),
            *sqlite_checks,
            sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "comment_id", "id"),
        )
    if not sa.inspect(bind).has_index(_TABLE, _THREAD_INDEX):
        op.create_index(
            _THREAD_INDEX,
            _TABLE,
            ["workspace_id", "conversation_id", "comment_id", "created_at", "id"],
        )


def downgrade() -> None:
    """Drop ``comment_replies`` and its thread index."""
    op.drop_index(_THREAD_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
