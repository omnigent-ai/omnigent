"""Change conversations.title from Text to VARCHAR(768).

Revision ID: w1a2b3c4d5e6
Revises: v1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

MySQL does not allow DEFAULT values on TEXT/BLOB columns, and TEXT columns
cannot be indexed without a key-prefix length. Converting ``title`` to
``VARCHAR(768)`` (the doc-spec value) fixes both issues:

- ``server_default=""`` now works on MySQL.
- ``ix_conversations_parent_title_unique`` can be defined without a prefix
  on SQLite/PostgreSQL; MySQL uses ``mysql_length={"title": 512}`` to keep
  the index key within MySQL's limit.

The migration also drops and recreates ``ix_conversations_parent_title_unique``
so the index picks up the new ``mysql_length`` hint on MySQL.

Upgrade path:
  Batch-rebuild ``conversations``, changing ``title`` from ``Text`` to
  ``String(768)``, then drop and recreate the unique index.
  ``recreate="always"`` on SQLite (cannot alter column types in-place);
  ``"auto"`` on other dialects.

Downgrade path:
  Reverse: ``String(768)`` → ``Text``, recreate the index without
  ``mysql_length``.

No PRAGMA foreign_keys guard needed — all FK constraints were removed in
p1a2b3c4d5e6.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w1a2b3c4d5e6"
down_revision: str | None = "v1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _is_sqlite() -> bool:
    return _dialect() == "sqlite"


def _index_exists(table: str, index_name: str) -> bool:
    """Return True if *index_name* exists on *table* in the current schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    """Change conversations.title from Text to VARCHAR(768); refresh unique index."""
    sqlite = _is_sqlite()

    with op.batch_alter_table(
        "conversations", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.alter_column(
            "title",
            existing_type=sa.Text(),
            type_=sa.String(768),
            nullable=False,
            existing_server_default="",
            server_default="",
        )

    # Drop the old index if it exists (on MySQL it may be absent because TEXT
    # columns cannot be indexed without a key-prefix length) and recreate it
    # with the mysql_length hint so it works across all three backends.
    if _index_exists("conversations", "ix_conversations_parent_title_unique"):
        op.drop_index(
            "ix_conversations_parent_title_unique",
            table_name="conversations",
        )
    op.create_index(
        "ix_conversations_parent_title_unique",
        "conversations",
        ["parent_conversation_id", "title"],
        unique=True,
        sqlite_where=sa.text("parent_conversation_id IS NOT NULL"),
        postgresql_where=sa.text("parent_conversation_id IS NOT NULL"),
        mysql_length={"title": 512},
    )


def downgrade() -> None:
    """Change conversations.title back from VARCHAR(768) to Text; restore index."""
    sqlite = _is_sqlite()

    # Drop the current index (created with mysql_length by upgrade) and restore
    # the original one without the prefix hint. On MySQL this index cannot be
    # recreated on a Text column anyway, so we skip it on that dialect.
    if _index_exists("conversations", "ix_conversations_parent_title_unique"):
        op.drop_index(
            "ix_conversations_parent_title_unique",
            table_name="conversations",
        )
    if _dialect() != "mysql":
        op.create_index(
            "ix_conversations_parent_title_unique",
            "conversations",
            ["parent_conversation_id", "title"],
            unique=True,
            sqlite_where=sa.text("parent_conversation_id IS NOT NULL"),
            postgresql_where=sa.text("parent_conversation_id IS NOT NULL"),
        )

    # MySQL does not allow DEFAULT values on TEXT/BLOB columns, so the
    # server_default must be omitted when reverting to Text on that dialect.
    mysql = _dialect() == "mysql"
    with op.batch_alter_table(
        "conversations", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.alter_column(
            "title",
            existing_type=sa.String(768),
            type_=sa.Text(),
            nullable=False,
            existing_server_default="" if not mysql else None,
            server_default="" if not mysql else None,
        )
