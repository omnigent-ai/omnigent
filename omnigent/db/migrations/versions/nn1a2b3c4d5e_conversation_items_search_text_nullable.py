"""Make conversation_items.search_text nullable.

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-09-25 00:00:00.000000

Stores that hold item data opaquely return None from _item_search_text and
omit the column on insert; a nullable column keeps those rows and leaves them
out of full-text search.

Deployment: additive and compatible with older application code, which always
writes search_text. Apply it before enabling a store that omits the column.
To roll back, stop such writers first; the downgrade then backfills NULL rows
with '' in independently committed primary-key pages and restores NOT NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rows covered per primary-key page while backfilling NULL search_text.
_BACKFILL_BATCH = 1000

_ITEMS = sa.table(
    "conversation_items",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("conversation_id"),
    sa.column("id"),
    sa.column("created_at", sa.Integer()),
    sa.column("search_text", sa.Text()),
)
_KEY = (_ITEMS.c.workspace_id, _ITEMS.c.conversation_id, _ITEMS.c.id, _ITEMS.c.created_at)


def upgrade() -> None:
    """Drop the NOT NULL constraint on conversation_items.search_text."""
    # Batch mode: SQLite cannot alter nullability in place (rebuilds the
    # table); PostgreSQL/MySQL run a plain ALTER.
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.alter_column(
            "search_text",
            existing_type=sa.Text(),
            nullable=True,
        )


def _key_compare(
    dialect: str, key: tuple[object, ...], *, after: bool, inclusive: bool
) -> sa.ColumnElement[bool]:
    """Lexicographic primary-key comparison against *key*.

    A row-value comparison lets SQLite, PostgreSQL and CockroachDB seek the
    primary key; MySQL treats tuple inequalities as filters over a full index
    scan, so it gets the expanded AND/OR form (as ll1a2b3c4d5e does).
    """
    if dialect != "mysql":
        pk = sa.tuple_(*_KEY)
        if after:
            return pk >= key if inclusive else pk > key
        return pk <= key if inclusive else pk < key
    clauses = []
    for index, column in enumerate(_KEY):
        equal = [earlier == key[position] for position, earlier in enumerate(_KEY[:index])]
        closed = inclusive and index == len(_KEY) - 1
        if after:
            compare = column >= key[index] if closed else column > key[index]
        else:
            compare = column <= key[index] if closed else column < key[index]
        clauses.append(sa.and_(*equal, compare))
    return sa.or_(*clauses)


def _commit_page(bind: sa.Connection) -> None:
    """Commit the finished page so the backfill never holds one growing transaction."""
    if bind.dialect.name == "cockroachdb":
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    else:
        with op.get_context().autocommit_block():
            pass


def _backfill_null_search_text() -> None:
    """Set search_text = '' on NULL rows, one committed primary-key page at a time."""
    bind = op.get_bind()
    dialect = bind.dialect.name
    last: tuple[object, ...] | None = None
    while True:
        query = sa.select(*_KEY).order_by(*_KEY).limit(_BACKFILL_BATCH)
        if last is not None:
            query = query.where(_key_compare(dialect, last, after=True, inclusive=False))
        rows = bind.execute(query).fetchall()
        if not rows:
            break
        first, last = (
            tuple(bytes(value) if isinstance(value, memoryview) else value for value in row)
            for row in (rows[0], rows[-1])
        )
        bind.execute(
            _ITEMS.update()
            .where(
                _ITEMS.c.search_text.is_(None),
                _key_compare(dialect, first, after=True, inclusive=True),
                _key_compare(dialect, last, after=False, inclusive=True),
            )
            .values(search_text="")
        )
        if len(rows) < _BACKFILL_BATCH:
            # The bounded final partial page commits together with the ALTER.
            break
        _commit_page(bind)


def downgrade() -> None:
    """Restore NOT NULL after backfilling NULL rows with an empty string."""
    if op.get_context().as_sql:
        raise RuntimeError("Backfilling search_text requires an online migration")
    _backfill_null_search_text()
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.alter_column(
            "search_text",
            existing_type=sa.Text(),
            nullable=False,
        )
