"""Widen the comments primary key with conversation_id; drop its now-redundant index.

Revision ID: a7f3c1b9e2d4
Revises: c3e8f1a9d2b7
Create Date: 2026-07-21 00:00:00.000000

Widens the ``comments`` primary key from ``(workspace_id, id)`` to
``(workspace_id, conversation_id, id)``.  ``conversation_id`` slots in between
the tenant partition key and the comment id so a single conversation's comments
stay contiguous under the workspace prefix, matching the per-conversation prefix
scans that dominate comment reads (``list_for_conversation``, the fingerprint
aggregate, and the cascade delete).  ``conversation_id`` is already NOT NULL and
every existing row has one, so the rebuild is a pure key change with no backfill.

The wider PK subsumes ``ix_comments_conversation_id``
(``workspace_id, conversation_id, created_at, id``): the ``(workspace_id,
conversation_id)`` prefix it shared with the PK is now covered by the PK itself,
so the secondary index is pure write/space overhead and is dropped.  Its one
extra job — feeding ``list_for_conversation``'s ``ORDER BY created_at, id`` an
index-ordered scan — is given up in favour of a filesort over the (small)
per-conversation comment set.

There are no FK constraints in the schema (see ``p1a2b3c4d5e6``), so rebuilding
the primary key is a purely local operation on this one table.

SQLite note: ``batch_alter_table(recreate="always")`` rebuilds the table so the
primary key can change (SQLite cannot alter a PK in place); the new
``create_primary_key`` overrides the reflected key.  On PostgreSQL the existing
named PK is dropped explicitly first (a table can hold only one primary key)
before the wider one is added.  Both paths guard the rebuild with
``PRAGMA foreign_keys`` on SQLite.  The index is dropped before the rebuild so
the recreate does not carry it forward.
"""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Iterator, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7f3c1b9e2d4"
down_revision: str | None = "c3e8f1a9d2b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "comments"
# Primary key before this migration and after it.
_OLD_PK = ["workspace_id", "id"]
_NEW_PK = ["workspace_id", "conversation_id", "id"]
# Secondary index made redundant by the wider PK.
_INDEX = "ix_comments_conversation_id"
_INDEX_COLS = ["workspace_id", "conversation_id", "created_at", "id"]


def _existing_pk_name(table: str) -> str | None:
    """Reflect the current primary-key constraint name (PostgreSQL path)."""
    return sa.inspect(op.get_bind()).get_pk_constraint(table).get("name")


@contextlib.contextmanager
def _quiet_pk_override() -> Iterator[None]:
    """
    Silence the expected SQLite batch-rebuild warning about the reflected
    primary key not matching the wider one we install. The override is
    intentional here, and this fires on every fresh DB.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*not matching locally specified columns.*",
            category=sa.exc.SAWarning,
        )
        yield


def _rebuild_pk(new_pk: list[str]) -> None:
    """Drop the current ``comments`` PK and install ``new_pk``."""
    dialect = op.get_bind().dialect.name
    sqlite = dialect == "sqlite"

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    if dialect == "mysql":
        # MySQL PKs are unnamed; use raw DDL so batch_alter_table does not
        # try to add a second PRIMARY KEY before the first is dropped.
        pk_col_list = ", ".join(f"`{c}`" for c in new_pk)
        op.execute(
            sa.text(
                f"ALTER TABLE `{_TABLE}` "
                f"DROP PRIMARY KEY, "
                f"ADD CONSTRAINT `pk_{_TABLE}` PRIMARY KEY ({pk_col_list})"
            )
        )
    else:
        old_pk_name = None if sqlite else _existing_pk_name(_TABLE)
        with (
            _quiet_pk_override(),
            op.batch_alter_table(_TABLE, recreate="always" if sqlite else "auto") as batch_op,
        ):
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            batch_op.create_primary_key(f"pk_{_TABLE}", new_pk)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def upgrade() -> None:
    """Drop the redundant index, then widen the PK with conversation_id."""
    # Drop first so the SQLite batch recreate does not carry the index forward.
    op.drop_index(_INDEX, table_name=_TABLE)
    _rebuild_pk(_NEW_PK)


def downgrade() -> None:
    """Restore the (workspace_id, id) PK, then recreate the index."""
    _rebuild_pk(_OLD_PK)
    op.create_index(_INDEX, _TABLE, _INDEX_COLS)
