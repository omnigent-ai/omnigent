"""add source_id to conversation_items

Revision ID: f3c7a1d9e2b4
Revises: e5d9bc8ac650
Create Date: 2026-08-30 00:00:00.000000

Persists a native transcript record's stable source identity so an
``external_conversation_item`` retry after a forwarder crash can resolve the
already-committed item instead of appending a duplicate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c7a1d9e2b4"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_conversation_items_source_id"
_INDEX_COLUMNS = ("workspace_id", "conversation_id", "source_id")


def _source_id_column_exists() -> bool:
    """Return whether a prior interrupted attempt already committed the column."""
    if op.get_context().as_sql:
        return False
    return "source_id" in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("conversation_items")
    }


def _repair_postgresql_index_artifact() -> None:
    """Remove an invalid or wrongly-shaped remnant before concurrent creation."""
    if op.get_context().as_sql:
        return
    row = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition "
                "FROM pg_index i WHERE i.indexrelid = to_regclass(:name)"
            ),
            {"name": _INDEX_NAME},
        )
        .mappings()
        .first()
    )
    if row is None:
        return
    definition = str(row["definition"])
    expected_shape = (
        "(workspace_id, conversation_id, source_id)" in definition
        and "WHERE (source_id IS NOT NULL)" in definition
        and not definition.startswith("CREATE UNIQUE INDEX")
    )
    if not row["indisvalid"] or not expected_shape:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")


def _portable_index_status() -> tuple[bool, bool]:
    """Return whether the named index exists and has this revision's shape."""
    if op.get_context().as_sql:
        return False, False
    dialect_name = op.get_context().dialect.name
    for index in sa.inspect(op.get_bind()).get_indexes("conversation_items"):
        if index["name"] != _INDEX_NAME:
            continue
        correct = tuple(index.get("column_names") or ()) == _INDEX_COLUMNS and not bool(
            index.get("unique")
        )
        if dialect_name == "sqlite":
            where = (index.get("dialect_options") or {}).get("sqlite_where")
            predicate = "".join(str(where).lower().split())
            correct = correct and predicate in {
                "source_idisnotnull",
                "(source_idisnotnull)",
            }
        return True, correct
    return False, False


def upgrade() -> None:
    """Add the nullable source identity and its lookup index."""
    if not _source_id_column_exists():
        op.add_column(
            "conversation_items",
            sa.Column("source_id", sa.String(length=512), nullable=True),
        )
    dialect_name = op.get_context().dialect.name
    if dialect_name == "postgresql":
        # A production conversation_items table can be large. PostgreSQL's
        # regular CREATE INDEX blocks writes, while CONCURRENTLY is illegal
        # inside Alembic's migration transaction.
        with op.get_context().autocommit_block():
            _repair_postgresql_index_artifact()
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
                "ON conversation_items (workspace_id, conversation_id, source_id) "
                "WHERE source_id IS NOT NULL"
            )
    else:
        exists, current = _portable_index_status()
        if not current:
            if exists:
                op.drop_index(_INDEX_NAME, table_name="conversation_items")
            if dialect_name == "sqlite":
                op.create_index(
                    _INDEX_NAME,
                    "conversation_items",
                    list(_INDEX_COLUMNS),
                    unique=False,
                    sqlite_where=sa.text("source_id IS NOT NULL"),
                )
            else:
                op.create_index(
                    _INDEX_NAME,
                    "conversation_items",
                    list(_INDEX_COLUMNS),
                    unique=False,
                )


def downgrade() -> None:
    """Remove the source identity lookup and column."""
    if op.get_context().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    else:
        index_exists, _current = _portable_index_status()
        if op.get_context().as_sql or index_exists:
            op.drop_index(_INDEX_NAME, table_name="conversation_items")
    if op.get_context().as_sql or _source_id_column_exists():
        with op.batch_alter_table("conversation_items") as batch_op:
            batch_op.drop_column("source_id")
