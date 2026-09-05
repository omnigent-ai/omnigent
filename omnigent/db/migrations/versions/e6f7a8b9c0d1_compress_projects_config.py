"""Store ``projects.config`` as compressed BLOB/BYTEA

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-05 00:00:00.000000

Finishes the sweep of z9a2b3c4d5e6, which converted the then-remaining opaque
``TEXT`` columns (``policies.handler`` / ``factory_params``,
``hosts.configured_harnesses``) to a binary column so the application layer can
store them zstd-compressed (``omnigent/db/compression.py``). ``projects.config``
landed four days earlier (b1c2d3e4f5a6) and was missed, leaving it the last
plain-``TEXT`` column outside ``conversation_items``.

``config`` qualifies on the same terms: it holds a machine-generated JSON object
of default session settings, is read and written whole with the row, and is never
filtered, ordered, or pattern-matched in SQL. Compressing it also gives a uniform
on-disk size across backends — MySQL's InnoDB does not compress ``TEXT``/``BLOB``
by default and SQLite never does, so without client-side compression the column
would sit uncompressed there while PostgreSQL (TOAST) compressed it.

The Python type stays ``str | None``, so the store, entity, and routes are
unchanged.

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

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _alter_type(to_binary: bool) -> None:
    """Change ``projects.config``'s SQL type in both directions.

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
    cast = "convert_to(config, 'UTF8')" if to_binary else "convert_from(config, 'UTF8')"
    with op.batch_alter_table("projects", recreate="always" if sqlite else "auto") as batch:
        batch.alter_column(
            "config",
            existing_type=old_type,
            type_=new_type,
            existing_nullable=True,
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
        data = value.tobytes()
    elif isinstance(value, bytes):
        data = value
    elif isinstance(value, bytearray):
        data = bytes(value)
    else:
        raise TypeError(f"expected binary compressed text, got {type(value).__name__}")
    if not data or data[0] != 0x00:
        return data.decode("utf-8")  # legacy unframed text
    codec, payload = data[1], data[2:]
    if codec == 0x01:  # zstd
        decompressed: bytes = zstandard.ZstdDecompressor().decompress(payload)
        return decompressed.decode("utf-8")
    return payload.decode("utf-8")  # framed, uncompressed


def downgrade() -> None:
    """Decompress every value, then restore the ``TEXT`` type."""
    bind = op.get_bind()
    on_sqlite = bind.dialect.name == "sqlite"
    # Rewrite each value as raw UTF-8 plaintext (bytes on PostgreSQL/MySQL, str
    # on dynamically-typed SQLite) so the binary → text conversion sees valid
    # UTF-8. Untyped text() SQL bypasses the column's binary type processor.
    select_sql = "SELECT workspace_id, id AS k, config AS v FROM projects WHERE config IS NOT NULL"
    update_sql = "UPDATE projects SET config = :v WHERE workspace_id = :ws AND id = :k"
    for workspace_id, row_key, value in bind.execute(sa.text(select_sql)).fetchall():
        plain = _decode(value)
        stored = plain if on_sqlite else plain.encode("utf-8")
        bind.execute(sa.text(update_sql), {"v": stored, "ws": workspace_id, "k": row_key})
    _alter_type(to_binary=False)
