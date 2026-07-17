"""Store conversation_items.data as a compressed/encrypted BLOB.

Revision ID: c4d5e6f7a8b9
Revises: d1e2f3a4b5c6
Create Date: 2026-07-17 00:00:00.000000

Switches ``conversation_items.data`` from ``TEXT`` to a binary column so the
application layer can store it zstd-compressed — and, when a column encryptor is
installed, encrypted at rest (``omnigent/db/compression.py``, ``EncryptedText``).
This matches the treatment of the other opaque columns and lets a deployment
that needs customer-managed-key encryption protect conversation-item payloads
without a schema change of its own.

``data`` is never pattern-matched in SQL: the content-search fallback matches
``search_text`` (the same plain-text column the FTS path uses), so making
``data`` opaque does not regress search.

Existing rows need no backfill on upgrade: they become their raw UTF-8 bytes,
and the codec recognises unframed values and reads them back unchanged,
re-framing each on its next write. Downgrade decompresses every row back to
plaintext before restoring the ``TEXT`` type; a value that was encrypted cannot
be downgraded without the encryptor and raises — encrypted rows only exist where
a deployment installed one, never in an OSS/Alembic-managed database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import zstandard
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _alter_type(to_binary: bool) -> None:
    """Change ``conversation_items.data``'s SQL type in both directions.

    Uses batch mode on every dialect: SQLite cannot alter a column type in
    place (``recreate="always"`` rebuilds the table), and routing all dialects
    through ``batch_op`` keeps the change off the bare ``op`` proxy, which the
    SQLite-safety guard forbids for ``alter_column``.

    :param to_binary: ``True`` for ``TEXT`` → ``LargeBinary`` (upgrade),
        ``False`` for the reverse (downgrade).
    """
    sqlite = op.get_bind().dialect.name == "sqlite"
    old_type = sa.Text() if to_binary else sa.LargeBinary()
    new_type = sa.LargeBinary() if to_binary else sa.Text()
    # PostgreSQL cannot implicitly cast between text and bytea, so spell the
    # conversion out. Ignored by other dialects.
    cast = "convert_to(data, 'UTF8')" if to_binary else "convert_from(data, 'UTF8')"
    with op.batch_alter_table(
        "conversation_items", recreate="always" if sqlite else "auto"
    ) as batch:
        batch.alter_column(
            "data",
            existing_type=old_type,
            type_=new_type,
            existing_nullable=False,
            postgresql_using=cast,
        )


def upgrade() -> None:
    """``TEXT`` → ``LargeBinary``. Existing rows keep their raw UTF-8 bytes."""
    _alter_type(to_binary=True)


def _decode(value: object) -> str:
    """Reverse the compression frame written by ``omnigent/db/compression.py``.

    Inlined so the downgrade stays correct against this migration's on-disk
    format regardless of later codec changes.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    data = bytes(value)
    if not data or data[0] != 0x00:
        return data.decode("utf-8")  # legacy unframed text
    codec, payload = data[1], data[2:]
    if codec == 0x02:  # encrypted
        raise RuntimeError(
            "cannot downgrade an encrypted conversation_items.data value without the "
            "column encryptor; encrypted rows are not present in an OSS/Alembic database"
        )
    if codec == 0x01:  # zstd
        return zstandard.ZstdDecompressor().decompress(payload).decode("utf-8")
    return payload.decode("utf-8")  # framed, uncompressed


def downgrade() -> None:
    """Decompress every value, then restore the ``TEXT`` type."""
    bind = op.get_bind()
    on_sqlite = bind.dialect.name == "sqlite"
    # Rewrite each value as raw UTF-8 plaintext (bytes on PostgreSQL/MySQL, str
    # on dynamically-typed SQLite) so the binary → text conversion sees valid
    # UTF-8. Untyped text() SQL bypasses the column's binary type processor.
    # id / conversation_id are 16 raw bytes; SELECT returns them in that form
    # and the UPDATE binds them back verbatim, so the PK match is byte-exact.
    select_sql = (
        "SELECT workspace_id, conversation_id, id, created_at, data AS v FROM conversation_items"
    )
    update_sql = (
        "UPDATE conversation_items SET data = :v "
        "WHERE workspace_id = :ws AND conversation_id = :cid "
        "AND id = :id AND created_at = :created_at"
    )
    for workspace_id, conversation_id, row_id, created_at, value in bind.execute(
        sa.text(select_sql)
    ).fetchall():
        plain = _decode(value)
        stored = plain if on_sqlite else plain.encode("utf-8")
        bind.execute(
            sa.text(update_sql),
            {
                "v": stored,
                "ws": workspace_id,
                "cid": conversation_id,
                "id": row_id,
                "created_at": created_at,
            },
        )
    _alter_type(to_binary=False)
