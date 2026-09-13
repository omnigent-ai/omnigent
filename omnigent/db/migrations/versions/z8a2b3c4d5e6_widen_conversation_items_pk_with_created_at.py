"""Add created_at to the conversation_items primary key (partition-ready).

Revision ID: z8a2b3c4d5e6
Revises: z7a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Widens the ``conversation_items`` primary key from
``(workspace_id, conversation_id, id)`` to
``(workspace_id, conversation_id, id, created_at)`` and adds ``created_at``
to the ``ix_conversation_items_conversation_id_position`` unique index.

This deployment does not partition the table. The change makes the schema
*partition-ready*: PostgreSQL and MySQL both require the partition key to be
part of the primary key and of every unique index, so a deployment that needs
``PARTITION BY (created_at)`` can do it with pure DDL — no key migration.
``created_at`` trails in both keys so existing per-conversation prefix scans
are unchanged.

``created_at`` is already NOT NULL on every row and is never updated (items
are insert/delete-only), so the rebuild is a pure key change with no
backfill. There are no FK constraints in the schema (see ``p1a2b3c4d5e6``).

Position-uniqueness note: with ``created_at`` in the unique index, the DB
blocks duplicate ``(workspace_id, conversation_id, position)`` only within
the same epoch second. The ``next_position`` counter allocated under
``_lock_conversation`` is (and already was) the real guarantor; it is
monotonic and never reuses a position.

SQLite note: the index is dropped before the ``recreate="always"`` batch
rebuild (so reflection does not recreate the stale shape) and re-created
after. MySQL folds the PK swap and index swap into one ``ALTER TABLE`` so
the copying rebuild happens once.
"""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Iterator, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z8a2b3c4d5e6"
down_revision: str | None = "z7a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversation_items"
_POSITION_INDEX = "ix_conversation_items_conversation_id_position"
# Primary key and unique-index columns before this migration and after it.
_OLD_PK = ["workspace_id", "conversation_id", "id"]
_NEW_PK = ["workspace_id", "conversation_id", "id", "created_at"]
_OLD_INDEX = ["workspace_id", "conversation_id", "position"]
_NEW_INDEX = ["workspace_id", "conversation_id", "position", "created_at"]


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


def _rebuild(pk: list[str], index: list[str]) -> None:
    """Install ``pk`` and the ``index`` shape of the unique position index."""
    dialect = op.get_bind().dialect.name
    sqlite = dialect == "sqlite"

    if dialect == "mysql":
        # MySQL PKs are unnamed; raw DDL folds the PK swap and the unique
        # index swap into a single copying rebuild.
        pk_cols = ", ".join(f"`{c}`" for c in pk)
        index_cols = ", ".join(f"`{c}`" for c in index)
        op.execute(
            sa.text(
                f"ALTER TABLE `{_TABLE}` "
                f"DROP PRIMARY KEY, "
                f"ADD CONSTRAINT `pk_{_TABLE}` PRIMARY KEY ({pk_cols}), "
                f"DROP INDEX `{_POSITION_INDEX}`, "
                f"ADD UNIQUE INDEX `{_POSITION_INDEX}` ({index_cols})"
            )
        )
        return

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # Drop the unique index first so the SQLite batch rebuild does not
    # recreate the stale shape from reflection.
    op.drop_index(_POSITION_INDEX, table_name=_TABLE)

    old_pk_name = None if sqlite else _existing_pk_name(_TABLE)
    with (
        _quiet_pk_override(),
        op.batch_alter_table(_TABLE, recreate="always" if sqlite else "auto") as batch_op,
    ):
        if old_pk_name is not None:
            batch_op.drop_constraint(old_pk_name, type_="primary")
        batch_op.create_primary_key(f"pk_{_TABLE}", pk)

    op.create_index(_POSITION_INDEX, _TABLE, index, unique=True)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def upgrade() -> None:
    """Widen the PK and unique position index with ``created_at``."""
    _rebuild(_NEW_PK, _NEW_INDEX)


def downgrade() -> None:
    """Restore the ``(workspace_id, conversation_id, id)`` key shapes."""
    _rebuild(_OLD_PK, _OLD_INDEX)
