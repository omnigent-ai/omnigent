"""Store opaque text columns as compressed BLOB/BYTEA.

Revision ID: z4a2b3c4d5e6
Revises: z3a2b3c4d5e6
Create Date: 2026-07-08 03:00:00.000000

Switches six columns that hold machine-generated JSON / free text — none of
which is ever filtered, ordered, or pattern-matched in SQL — from ``TEXT`` to a
binary column so the application layer can store them zstd-compressed
(``omnigent/db/compression.py``):

    conversations.session_usage / session_state / terminal_launch_args
    comments.body / anchor_content
    agents.description

This yields a uniform on-disk size across backends. MySQL's InnoDB does not
compress ``TEXT``/``BLOB`` by default and SQLite never does, so without
client-side compression these columns would sit uncompressed on those engines
while PostgreSQL (TOAST) compressed them.

Existing rows need no backfill on upgrade: they become their raw UTF-8 bytes,
and the codec recognises unframed values and reads them back unchanged,
re-framing each on its next write. Downgrade decompresses every row back to
plaintext before restoring the ``TEXT`` type.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import zstandard
from alembic import op

revision: str = "z4a2b3c4d5e6"
down_revision: str | None = "z3a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Columns grouped by table so SQLite rebuilds each table exactly once. The bool
# is the column's existing nullability.
_TABLE_COLUMNS: dict[str, list[tuple[str, bool]]] = {
    "conversations": [
        ("session_usage", True),
        ("session_state", True),
        ("terminal_launch_args", True),
    ],
    "comments": [("body", False), ("anchor_content", True)],
    "agents": [("description", True)],
}


def _alter_types(to_binary: bool) -> None:
    """Change the columns' SQL type in both directions.

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
    cast = "convert_to({col}, 'UTF8')" if to_binary else "convert_from({col}, 'UTF8')"
    for table, cols in _TABLE_COLUMNS.items():
        with op.batch_alter_table(table, recreate="always" if sqlite else "auto") as batch:
            for col, nullable in cols:
                batch.alter_column(
                    col,
                    existing_type=old_type,
                    type_=new_type,
                    existing_nullable=nullable,
                    postgresql_using=cast.format(col=col),
                )


def upgrade() -> None:
    """``TEXT`` → ``LargeBinary``. Existing rows keep their raw UTF-8 bytes."""
    _alter_types(to_binary=True)


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
    for table, cols in _TABLE_COLUMNS.items():
        for col, _nullable in cols:
            select_sql = (
                f"SELECT workspace_id, id, {col} AS v FROM {table} WHERE {col} IS NOT NULL"
            )
            update_sql = f"UPDATE {table} SET {col} = :v WHERE workspace_id = :ws AND id = :id"
            for workspace_id, row_id, value in bind.execute(sa.text(select_sql)).fetchall():
                plain = _decode(value)
                stored = plain if on_sqlite else plain.encode("utf-8")
                bind.execute(
                    sa.text(update_sql),
                    {"v": stored, "ws": workspace_id, "id": row_id},
                )
    _alter_types(to_binary=False)
