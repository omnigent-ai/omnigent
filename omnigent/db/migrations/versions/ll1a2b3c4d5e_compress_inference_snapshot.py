"""Store inference snapshots as compressed BLOB/BYTEA.

Run with database readers and writers stopped. JSON is compressed before applying
the 65,535-byte limit. Snapshots that still exceed it become NULL, retaining
the conversation and other metadata but losing their saved inference profile.
Downgrade restores plaintext for retained snapshots; cleared data cannot return.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
import zstandard
from alembic import op
from sqlalchemy.dialects.mysql import LONGTEXT

revision: str = "ll1a2b3c4d5e"
down_revision: str | None = "kk1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"
_COLUMN = "inference_snapshot"
_BATCH_SIZE = 100
_logger = logging.getLogger(__name__)


def _plain(value: str | bytes | memoryview) -> str:
    # Keep the on-disk codec here so later application changes cannot alter rollback.
    if isinstance(value, str):
        return value
    data = bytes(value)
    if data.startswith(b"\x00\x01"):
        return zstandard.ZstdDecompressor().decompress(data[2:]).decode("utf-8")
    if data.startswith(b"\x00\x00"):
        return data[2:].decode("utf-8")
    return data.decode("utf-8")


def _publish(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        # Schema changes must be visible before subsequent reads and writes.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _convert(to_binary: bool) -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Inference snapshot compression requires an online migration")
    bind = op.get_bind()
    temporary = "_inference_snapshot_blob" if to_binary else "_inference_snapshot_text"
    target_type: sa.types.TypeEngine[Any] = (
        sa.LargeBinary() if to_binary else sa.Text().with_variant(LONGTEXT(), "mysql")
    )
    columns = {col["name"]: col["type"] for col in sa.inspect(bind).get_columns(_TABLE)}
    if temporary not in columns:
        # MySQL/CRDB may have committed the rename before Alembic recorded the revision.
        is_binary = isinstance(columns[_COLUMN], sa.LargeBinary)
        if is_binary == to_binary:
            return
        op.add_column(_TABLE, sa.Column(temporary, target_type, nullable=True))
        _publish(bind)

    if _COLUMN in columns:
        table = sa.table(
            _TABLE,
            sa.column("workspace_id", sa.BigInteger()),
            sa.column("id"),
            sa.column(_COLUMN),
            sa.column(temporary, target_type),
        )
        cursor: tuple[int, bytes | str] | None = None
        cleared = 0
        while True:
            query = sa.select(table.c.workspace_id, table.c.id, table.c[_COLUMN]).order_by(
                table.c.workspace_id, table.c.id
            )
            if cursor is not None:
                query = query.where(sa.tuple_(table.c.workspace_id, table.c.id) > cursor)
            rows = bind.execute(query.limit(_BATCH_SIZE)).all()
            if not rows:
                break
            for workspace_id, row_id, value in rows:
                stored: str | bytes | None = None
                if value is not None:
                    plain = _plain(value)
                    if to_binary:
                        raw = plain.encode("utf-8")
                        stored = (
                            b"\x00\x00" + raw
                            if len(raw) < 64
                            else b"\x00\x01" + zstandard.ZstdCompressor(level=19).compress(raw)
                        )
                        if len(stored) > 65_535:
                            stored = None
                            cleared += 1
                    else:
                        stored = plain
                bind.execute(
                    table.update()
                    .where(table.c.workspace_id == workspace_id, table.c.id == row_id)
                    .values({temporary: stored})
                )
            last_id = rows[-1].id
            cursor = (
                rows[-1].workspace_id,
                last_id if isinstance(last_id, str) else bytes(last_id),
            )
        if cleared:
            _logger.warning(
                "Cleared %d inference snapshots exceeding 65,535 compressed bytes", cleared
            )
        _publish(bind)
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
        _publish(bind)

    # Resumes safely if a nontransactional DDL backend stopped after dropping the source.
    with op.batch_alter_table(_TABLE) as batch:
        batch.alter_column(
            temporary, new_column_name=_COLUMN, existing_type=target_type, existing_nullable=True
        )
    _publish(bind)


def upgrade() -> None:
    _convert(to_binary=True)


def downgrade() -> None:
    _convert(to_binary=False)
