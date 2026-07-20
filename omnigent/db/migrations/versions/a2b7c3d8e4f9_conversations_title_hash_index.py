"""Index conversation child-title uniqueness by a title hash instead of the title.

Revision ID: a2b7c3d8e4f9
Revises: f4a1c8b2d3e6
Create Date: 2026-07-20 00:00:00.000000

``conversations`` enforced per-parent title uniqueness with a UNIQUE index on
``(workspace_id, parent_conversation_id, title)`` where ``title`` is a
``VARCHAR(768)`` folded to a 512-char key prefix on MySQL (``mysql_length``).
That prefix reserves up to ~2 KB per index entry on utf8mb4.

This migration adds a ``title_hash`` column holding the first 16 bytes of
``sha256(title)`` and repoints the unique index at it, so entries are a fixed 16
bytes. The index keeps its name so the store's IntegrityError-to-
NameAlreadyExistsError translation still matches. Uniqueness semantics are
unchanged: two titles collide iff their (128-bit) digests do, and collisions
only matter among siblings under one parent, so 16 bytes is ample.

SQLite has no ``sha256()`` SQL function, so ``title_hash`` is back-filled in
Python, keyset-batched by the ``(workspace_id, id)`` primary key to bound memory
on a large table. The column is nullable (the ORM default and this backfill
populate it, so app rows always have a hash), which keeps the whole upgrade
native DDL — no NOT NULL flip, no table rebuild. Downgrade drops the column via
``batch_alter_table`` (``DROP COLUMN`` needs it on older SQLite).

Column type by dialect: ``LargeBinary`` renders as ``BYTEA`` (Postgres) /
``BLOB`` (SQLite), but MySQL cannot index a ``BLOB`` without a key-prefix
length, so the column is ``BINARY(16)`` on MySQL.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

revision: str = "a2b7c3d8e4f9"
down_revision: str | None = "f4a1c8b2d3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BYTEA/BLOB elsewhere, BINARY(16) on MySQL (BLOB is not indexable there).
_CKSUM16 = sa.LargeBinary(length=16).with_variant(MySQLBinary(16), "mysql")

_INDEX = "ix_conversations_parent_title_unique"
_BACKFILL_BATCH = 1000


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _title_hash(title: str) -> bytes:
    """First 16 bytes of sha256(title) (kept self-contained in the migration)."""
    return hashlib.sha256(title.encode("utf-8")).digest()[:16]


def _backfill_title_hash() -> None:
    """Compute ``title_hash`` for every existing row in Python, keyset-batched.

    Pages by the ``(workspace_id, id)`` primary key so memory stays bounded to
    one batch regardless of table size (unlike a single ``fetchall``).
    """
    bind = op.get_bind()
    last_ws: int | None = None
    last_id: object = None
    while True:
        if last_ws is None:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, id, title FROM conversations "
                    "ORDER BY workspace_id, id LIMIT :lim"
                ),
                {"lim": _BACKFILL_BATCH},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, id, title FROM conversations "
                    "WHERE workspace_id > :ws OR (workspace_id = :ws AND id > :id) "
                    "ORDER BY workspace_id, id LIMIT :lim"
                ),
                {"ws": last_ws, "id": last_id, "lim": _BACKFILL_BATCH},
            ).fetchall()
        if not rows:
            break
        for workspace_id, conv_id, title in rows:
            bind.execute(
                sa.text(
                    "UPDATE conversations SET title_hash = :h "
                    "WHERE workspace_id = :ws AND id = :id"
                ),
                {"h": _title_hash(title or ""), "ws": workspace_id, "id": conv_id},
            )
        last_ws, last_id = rows[-1][0], rows[-1][1]
        if len(rows) < _BACKFILL_BATCH:
            break


def upgrade() -> None:
    """
    1. Add ``title_hash`` (nullable — see the model; the ORM/backfill populate it).
    2. Back-fill ``title_hash = sha256(title)[:16]`` for existing rows in Python.
    3. Swap the unique index off ``title`` onto ``title_hash``.

    Every step is native DDL on all dialects: no NOT NULL flip means no table
    rebuild, so no ``batch_alter_table`` / ``PRAGMA foreign_keys`` dance.
    """
    op.add_column("conversations", sa.Column("title_hash", _CKSUM16, nullable=True))
    _backfill_title_hash()

    # Swap the wide-title unique index onto the fixed-width hash. Keep the index
    # name so the store's IntegrityError → NameAlreadyExistsError match still holds.
    op.drop_index(_INDEX, table_name="conversations")
    op.create_index(
        _INDEX,
        "conversations",
        ["workspace_id", "parent_conversation_id", "title_hash"],
        unique=True,
    )


def downgrade() -> None:
    """Restore the ``title``-keyed unique index and drop ``title_hash``."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    op.drop_index(_INDEX, table_name="conversations")

    with op.batch_alter_table(
        "conversations", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.drop_column("title_hash")

    op.create_index(
        _INDEX,
        "conversations",
        ["workspace_id", "parent_conversation_id", "title"],
        unique=True,
        mysql_length={"title": 512},
    )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
