"""Drop the per-parent title-uniqueness index and the ``title_hash`` column.

Revision ID: 72e6dceae14f
Revises: b3c1a2d4e5f6
Create Date: 2026-07-21 00:00:00.000000

``conversations`` enforced per-parent child-title uniqueness with a UNIQUE index
on ``(workspace_id, parent_conversation_id, title_hash)``, where ``title_hash``
was a fixed 16-byte ``sha256(title)[:16]`` mirror of ``title`` maintained solely
to key that index. Uniqueness now lives in application code
(``create_conversation`` does a per-parent ``(parent, title)`` existence check
before inserting), so both the index and the column are removed. Reads that used
to touch the unique index — the runner's find-or-create pre-check and the new
create-time check — are served by ``idx_conversations_parent``
(``workspace_id, parent_conversation_id, ...``), which seeks the parent's
children and filters ``title`` as a residual.

The column drop uses ``batch_alter_table`` so SQLite (which needs a table
rebuild for ``DROP COLUMN`` on older versions) is handled uniformly. On SQLite
the DESC-ordered ``idx_conversations_parent`` is dropped and recreated
explicitly around the rebuild so its column sort order is not lost to batch
reflection; MySQL/Postgres drop the column with native ``ALTER`` and leave every
other index untouched.

Downgrade re-adds ``title_hash`` (nullable), back-fills
``title_hash = sha256(title)[:16]`` in Python (keyset-batched to bound memory —
SQLite has no ``sha256()`` SQL function), and restores the UNIQUE index on it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

revision: str = "72e6dceae14f"
down_revision: str | None = "b3c1a2d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BYTEA/BLOB elsewhere, BINARY(16) on MySQL (BLOB is not indexable there).
_CKSUM16 = sa.LargeBinary(length=16).with_variant(MySQLBinary(16), "mysql")

_UNIQUE_INDEX = "ix_conversations_parent_title_unique"
_PARENT_INDEX = "idx_conversations_parent"
_BACKFILL_BATCH = 1000


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _title_hash(title: str) -> bytes:
    """First 16 bytes of sha256(title) (kept self-contained in the migration)."""
    return hashlib.sha256(title.encode("utf-8")).digest()[:16]


def _create_parent_index() -> None:
    """Recreate ``idx_conversations_parent`` with its DESC ordering intact."""
    op.create_index(
        _PARENT_INDEX,
        "conversations",
        [
            "workspace_id",
            "parent_conversation_id",
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    )


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
    """Drop the unique index, then the ``title_hash`` column."""
    sqlite = _is_sqlite()
    op.drop_index(_UNIQUE_INDEX, table_name="conversations")

    if sqlite:
        # The table rebuild would reflect and re-emit idx_conversations_parent,
        # losing its DESC column ordering; drop it first and recreate it by hand.
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))
        op.drop_index(_PARENT_INDEX, table_name="conversations")
        with op.batch_alter_table("conversations", recreate="always") as batch_op:
            batch_op.drop_column("title_hash")
        _create_parent_index()
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
    else:
        # Batch (never on the bare op proxy) so the SQLite-safe-DDL guard passes;
        # on MySQL/Postgres "auto" mode issues a native ALTER ... DROP COLUMN with
        # no table rebuild, leaving every other index untouched.
        with op.batch_alter_table("conversations") as batch_op:
            batch_op.drop_column("title_hash")


def downgrade() -> None:
    """Restore ``title_hash`` (back-filled) and the UNIQUE index on it."""
    op.add_column("conversations", sa.Column("title_hash", _CKSUM16, nullable=True))
    _backfill_title_hash()
    op.create_index(
        _UNIQUE_INDEX,
        "conversations",
        ["workspace_id", "parent_conversation_id", "title_hash"],
        unique=True,
    )
