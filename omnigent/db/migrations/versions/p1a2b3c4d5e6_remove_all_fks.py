"""Remove all FK constraints; application owns relationship cleanup.

Revision ID: p1a2b3c4d5e6
Revises: o1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Drops all 9 remaining FK constraints (8 CASCADE + 1 SET NULL) from the
schema, following internal DB standard Rule R032 that forbids
database-enforced foreign keys.  After this migration the application
is solely responsible for cascading deletes and referential cleanup.

SQLite note: ``batch_alter_table`` with ``recreate="always"`` rebuilds
the table from scratch without the FK, which is the only reliable way
to remove a FK on SQLite (ALTER TABLE DROP CONSTRAINT is not supported).
Both upgrade and downgrade issue ``PRAGMA foreign_keys = OFF`` (guarded by
dialect) around the batch operations so no accidental cascade fires during
the table rebuilds themselves.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p1a2b3c4d5e6"
down_revision: str | None = "o1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _drop_all_fks_on_table(table_name: str, sqlite: bool) -> None:
    """
    Drop all FK constraints on a table.

    SQLite often stores FK constraints without names (name=None) or with
    names that differ from the naming convention. When batch_alter_table
    runs with recreate="always" and a naming_convention, unnamed FKs are
    assigned names by the convention during the rebuild — so we must drop
    them by their convention-derived name, not their original None.

    For each FK we compute the name to drop: use the existing name if set,
    otherwise derive it from the convention: fk_<table>_<column>.
    """
    bind = op.get_bind()
    fks = sa.inspect(bind).get_foreign_keys(table_name)
    with op.batch_alter_table(
        table_name,
        recreate="always" if sqlite else "auto",
        naming_convention=_NAMING_CONVENTION,
    ) as batch_op:
        for fk in fks:
            name = fk["name"]
            if name is None:
                # Derive the name the convention will assign during rebuild.
                col = fk["constrained_columns"][0]
                name = f"fk_{table_name}_{col}"
            batch_op.drop_constraint(name, type_="foreignkey")


def upgrade() -> None:
    """Drop all FK constraints from every affected table."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    for table in (
        "session_permissions",
        "conversations",
        "conversation_items",
        "conversation_labels",
        "policies",
    ):
        _drop_all_fks_on_table(table, sqlite)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Re-add all FK constraints."""
    # MySQL never had these FKs (they were skipped during upgrade due to
    # MySQL 8.0.16+ incompatibilities with CHECK constraints), so nothing
    # to restore on MySQL.
    if op.get_bind().dialect.name == "mysql":
        return
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # policies: re-add FK on session_id → conversations.id (CASCADE)
    with op.batch_alter_table(
        "policies",
        recreate="always" if sqlite else "auto",
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_policies_session_id",
            "conversations",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # conversation_labels: re-add FK on conversation_id → conversations.id (CASCADE)
    with op.batch_alter_table(
        "conversation_labels",
        recreate="always" if sqlite else "auto",
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_conversation_labels_conversation_id",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # conversation_items: re-add FK on conversation_id → conversations.id (CASCADE)
    with op.batch_alter_table(
        "conversation_items",
        recreate="always" if sqlite else "auto",
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_conversation_items_conversation_id",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # conversations: re-add all 4 FKs
    with op.batch_alter_table(
        "conversations",
        recreate="always" if sqlite else "auto",
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_conversations_agent_id",
            "agents",
            ["agent_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_conversations_root_conversation_id",
            "conversations",
            ["root_conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_conversations_parent_conversation_id",
            "conversations",
            ["parent_conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_conversations_host_id_hosts",
            "hosts",
            ["host_id"],
            ["host_id"],
            ondelete="SET NULL",
        )

    # session_permissions: re-add both FKs
    with op.batch_alter_table(
        "session_permissions",
        recreate="always" if sqlite else "auto",
    ) as batch_op:
        batch_op.create_foreign_key(
            "fk_session_permissions_conversation_id",
            "conversations",
            ["conversation_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_session_permissions_user_id",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
