"""Store and index the key that orders sessions by Name.

``conversations.title_sort_key`` is ``"0"`` plus the title with only A-Z
lowercased, cut to 512 code points, or ``"1"`` for an untitled session. A binary
collation makes every backend compare it by code point, and
``ix_conversations_archived_title`` lets a Name page read only the rows it
returns instead of sorting every session first.

Deployment: additive. The backfill commits per batch, and on PostgreSQL the
index builds ``CONCURRENTLY``, so sessions stay writable during the upgrade.
Builds from before this revision ignore the column; until the rollout finishes,
sessions they title keep the ``"1"`` default or their earlier key, so Name
ordering lists them as untitled or under their old name until the next rename.
After the last older build stops, recompute those keys with Alembic: ``stamp
d0a1f879a01d``, ``upgrade e413195738fb``, then stamp the revision the database
was at before. The backfill skips rows renamed while it runs. Downgrade drops
the index and the column.
"""

from __future__ import annotations

import string
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar

revision: str = "e413195738fb"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversations"
_COLUMN = "title_sort_key"
_INDEX = "ix_conversations_archived_title"
_INDEX_COLUMNS = ["workspace_id", "archived", _COLUMN, "id"]
_PARENT_INDEX = "idx_conversations_parent"
_MAX_CHARS = 512
_BATCH_SIZE = 1000
_SORT_KEY_TYPE = (
    sa.String(_MAX_CHARS)
    .with_variant(sa.String(_MAX_CHARS, collation="C"), "postgresql")
    .with_variant(
        MySQLVarchar(_MAX_CHARS, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
    )
)
_ASCII_LOWERCASE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _sort_key(title: str | None) -> str:
    # Kept here so later application changes cannot alter this backfill.
    if not title:
        return "1"
    return ("0" + title.translate(_ASCII_LOWERCASE))[:_MAX_CHARS]


def _publish(bind: sa.Connection) -> None:
    if bind.dialect.name == "cockroachdb":
        # Schema changes must be visible before subsequent reads and writes.
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _backfill(bind: sa.Connection) -> None:
    table = sa.table(
        _TABLE,
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id"),
        sa.column("title", sa.Text()),
        sa.column(_COLUMN, _SORT_KEY_TYPE),
    )
    # Skip rows renamed since the read; upgraded builds write their key with the title.
    update = (
        table.update()
        .where(
            table.c.workspace_id == sa.bindparam("row_workspace_id"),
            table.c.id == sa.bindparam("row_id"),
            table.c.title == sa.bindparam("row_title"),
            table.c[_COLUMN] == sa.bindparam("row_sort_key"),
        )
        .values({_COLUMN: sa.bindparam("sort_key")})
    )
    cursor: tuple[int, Any] | None = None
    while True:
        query = sa.select(
            table.c.workspace_id, table.c.id, table.c.title, table.c[_COLUMN]
        ).order_by(table.c.workspace_id, table.c.id)
        if cursor is not None:
            if bind.dialect.name == "mysql":
                # MySQL treats tuple inequalities as filters over a full index scan.
                query = query.where(
                    sa.or_(
                        table.c.workspace_id > cursor[0],
                        sa.and_(table.c.workspace_id == cursor[0], table.c.id > cursor[1]),
                    )
                )
            else:
                query = query.where(sa.tuple_(table.c.workspace_id, table.c.id) > cursor)
        rows = bind.execute(query.limit(_BATCH_SIZE)).all()
        if not rows:
            break
        changed = [
            {
                "row_workspace_id": row.workspace_id,
                "row_id": row.id,
                "row_title": row.title,
                "row_sort_key": row.title_sort_key,
                "sort_key": key,
            }
            for row in rows
            if (key := _sort_key(row.title)) != row.title_sort_key
        ]
        if changed:
            bind.execute(update, changed)
        last_id = rows[-1].id
        cursor = (rows[-1].workspace_id, last_id if isinstance(last_id, str) else bytes(last_id))
        if len(rows) < _BATCH_SIZE:
            break
        if bind.dialect.name == "cockroachdb":
            _publish(bind)
        else:
            with op.get_context().autocommit_block():
                pass


def _create_index(bind: sa.Connection) -> None:
    if bind.dialect.name != "postgresql":
        if _INDEX not in {index["name"] for index in sa.inspect(bind).get_indexes(_TABLE)}:
            op.create_index(_INDEX, _TABLE, _INDEX_COLUMNS)
        return
    # CONCURRENTLY can't run in a transaction; autocommit lets writers proceed.
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an INVALID index that IF NOT EXISTS would keep.
        invalid = bind.execute(
            sa.text(
                "SELECT i.indexrelid::regclass::text FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = to_regclass(:table) AND c.relname = :name "
                "AND NOT i.indisvalid"
            ),
            {"table": _TABLE, "name": _INDEX},
        ).scalar()
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY {invalid}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} ({', '.join(_INDEX_COLUMNS)})"
        )


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("The title sort key backfill requires an online migration")
    bind = op.get_bind()
    # MySQL and CockroachDB may have committed the column before a failed run ended.
    if _COLUMN not in {column["name"] for column in sa.inspect(bind).get_columns(_TABLE)}:
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(
                sa.Column(_COLUMN, _SORT_KEY_TYPE, nullable=False, server_default="1")
            )
        _publish(bind)
    _backfill(bind)
    _publish(bind)
    _create_index(bind)
    _publish(bind)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    if op.get_bind().dialect.name != "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column(_COLUMN)
        return
    # The table rebuild would cascade deletes and lose the parent index's DESC order.
    op.execute(sa.text("PRAGMA foreign_keys = OFF"))
    op.drop_index(_PARENT_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE, recreate="always") as batch:
        batch.drop_column(_COLUMN)
    op.create_index(
        _PARENT_INDEX,
        _TABLE,
        ["workspace_id", "parent_conversation_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.execute(sa.text("PRAGMA foreign_keys = ON"))
