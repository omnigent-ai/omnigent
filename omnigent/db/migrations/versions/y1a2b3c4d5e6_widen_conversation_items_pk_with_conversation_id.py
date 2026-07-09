"""Add conversation_id to the conversation_items primary key.

Revision ID: y1a2b3c4d5e6
Revises: x1a2b3c4d5e6
Create Date: 2026-07-08 00:00:00.000000

Widens the ``conversation_items`` primary key from ``(workspace_id, id)``
to ``(workspace_id, conversation_id, id)``.  ``conversation_id`` slots in
between the tenant partition key and the item id so a single conversation's
items stay contiguous under the workspace prefix, matching the per-conversation
prefix scans that dominate item reads.  ``conversation_id`` is already NOT NULL
and every existing row has one, so the rebuild is a pure key change with no
backfill.

There are no FK constraints in the schema (see ``p1a2b3c4d5e6``), so rebuilding
the primary key is a purely local operation on this one table.

SQLite note: ``batch_alter_table(recreate="always")`` rebuilds the table so the
primary key can change (SQLite cannot alter a PK in place); the new
``create_primary_key`` overrides the reflected key.  On PostgreSQL the existing
named PK is dropped explicitly first (a table can hold only one primary key)
before the wider one is added.  Both paths guard the rebuild with
``PRAGMA foreign_keys`` on SQLite.
"""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Iterator, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "y1a2b3c4d5e6"
down_revision: str | None = "x1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversation_items"
# Primary key before this migration and after it.
_OLD_PK = ["workspace_id", "id"]
_NEW_PK = ["workspace_id", "conversation_id", "id"]


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


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
    """Drop the current ``conversation_items`` PK and install ``new_pk``."""
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
    """Widen the primary key to ``(workspace_id, conversation_id, id)``."""
    _rebuild_pk(_NEW_PK)


def downgrade() -> None:
    """Restore the ``(workspace_id, id)`` primary key."""
    _rebuild_pk(_OLD_PK)
