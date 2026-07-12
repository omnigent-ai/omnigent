"""Rebuild every secondary index to include the primary-key columns.

Revision ID: z3a2b3c4d5e6
Revises: z2a2b3c4d5e6
Create Date: 2026-07-08 22:00:00.000000

The storage standard requires every index to contain the table's primary-key
columns. Each table's PK now leads with ``workspace_id`` (the tenant partition
key) followed by the entity id column(s), and every store query filters
``workspace_id``. So each secondary index is rebuilt to:

- **Non-unique indexes** — lead with ``workspace_id`` (every read is
  workspace-scoped) and trail the remaining PK id-columns, which double as the
  keyset tiebreaker / covering column the queries already use.
- **Unique indexes / constraints** — prepend ``workspace_id`` only, so
  uniqueness becomes per-workspace (appending the entity id would make it
  vacuous). ``uq_hosts_token_hash`` is included because ``resolve_launch_token``
  already filters ``workspace_id + token_hash``.

Two column orders are query-driven rather than mechanical:
``ix_session_permissions_conversation_id`` and
``ix_conversation_items_response_id`` place the filtered PK column right after
``workspace_id``. ``ix_comments_created_at`` is dropped — no query sorts
comments globally by ``created_at`` (they are always conversation-scoped).

Plain index rebuilds are simple drop/create on SQLite, PostgreSQL, and MySQL.
The two ``UniqueConstraint`` swaps (``policies``, ``hosts``) cannot be altered
in place on SQLite, so they run in a ``batch_alter_table`` (``recreate="always"``
on SQLite) guarded by the usual ``PRAGMA foreign_keys`` toggle. Partial indexes
are dropped before / recreated after the batch so the rebuild never copies a
stale predicate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z3a2b3c4d5e6"
down_revision: str | None = "z2a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


# Plain (non-unique, non-partial) indexes: (name, table, old_cols, new_cols).
_PLAIN_INDEXES: list[tuple[str, str, list[str], list[str]]] = [
    ("ix_agents_created_at", "agents", ["created_at"], ["workspace_id", "created_at", "id"]),
    ("ix_files_created_at", "files", ["created_at"], ["workspace_id", "created_at", "id"]),
    (
        "ix_files_session_id_created_at",
        "files",
        ["session_id", "created_at", "id"],
        ["workspace_id", "session_id", "created_at", "id"],
    ),
    (
        "ix_account_tokens_expires_at",
        "account_tokens",
        ["expires_at"],
        ["workspace_id", "expires_at", "id"],
    ),
    (
        "ix_session_permissions_conversation_id",
        "session_permissions",
        ["conversation_id"],
        ["workspace_id", "conversation_id", "user_id"],
    ),
    (
        "ix_conversations_created_at",
        "conversations",
        ["created_at"],
        ["workspace_id", "created_at", "id"],
    ),
    (
        "ix_conversations_updated_at",
        "conversations",
        ["updated_at"],
        ["workspace_id", "updated_at", "id"],
    ),
    ("ix_conversations_kind", "conversations", ["kind"], ["workspace_id", "kind", "id"]),
    (
        "ix_conversations_agent_id",
        "conversations",
        ["agent_id"],
        ["workspace_id", "agent_id", "id"],
    ),
    (
        "ix_conversations_root_conversation_id",
        "conversations",
        ["root_conversation_id"],
        ["workspace_id", "root_conversation_id", "id"],
    ),
    (
        "ix_conversations_runner_id",
        "conversations",
        ["runner_id"],
        ["workspace_id", "runner_id", "id"],
    ),
    (
        "ix_conversation_items_response_id",
        "conversation_items",
        ["response_id"],
        ["workspace_id", "conversation_id", "response_id", "id"],
    ),
    (
        "ix_comments_conversation_id",
        "comments",
        ["conversation_id"],
        ["workspace_id", "conversation_id", "created_at", "id"],
    ),
]


def _swap_plain(*, to_new: bool) -> None:
    """Rebuild the plain indexes to their new (or old) column lists."""
    for name, table, old_cols, new_cols in _PLAIN_INDEXES:
        op.drop_index(name, table_name=table)
        op.create_index(name, table, new_cols if to_new else old_cols)


def _create_unique_partial(*, to_new: bool) -> None:
    """Create the unique / partial indexes (non-constraint) at new or old shape."""
    ws = ["workspace_id"] if to_new else []
    op.create_index(
        "ix_agents_template_name",
        "agents",
        [*ws, "name"],
        unique=True,
        sqlite_where=sa.text("kind = 1"),
        postgresql_where=sa.text("kind = 1"),
    )
    op.create_index(
        "ix_conversations_parent_title_unique",
        "conversations",
        [*ws, "parent_conversation_id", "title"],
        unique=True,
        sqlite_where=sa.text("parent_conversation_id IS NOT NULL"),
        postgresql_where=sa.text("parent_conversation_id IS NOT NULL"),
        mysql_length={"title": 512},
    )
    op.create_index(
        "idx_conversations_parent",
        "conversations",
        [*ws, "parent_conversation_id", sa.text("created_at DESC"), sa.text("id DESC")],
        sqlite_where=sa.text("kind = 2"),
        postgresql_where=sa.text("kind = 2"),
    )
    op.create_index(
        "ix_conversation_items_conversation_id_position",
        "conversation_items",
        [*ws, "conversation_id", "position"],
        unique=True,
    )


def _drop_unique_partial() -> None:
    """Drop the unique / partial indexes (non-constraint)."""
    op.drop_index("ix_agents_template_name", table_name="agents")
    op.drop_index("ix_conversations_parent_title_unique", table_name="conversations")
    op.drop_index("idx_conversations_parent", table_name="conversations")
    op.drop_index(
        "ix_conversation_items_conversation_id_position", table_name="conversation_items"
    )


def _rebuild_constraint_tables(*, to_new: bool) -> None:
    """Rebuild the policies / hosts unique constraints and policies indexes.

    The constraint swaps need batch mode on SQLite; the policies partial index
    is dropped before and recreated after the batch so the rebuild never copies
    a stale predicate.
    """
    ws = ["workspace_id"] if to_new else []
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # policies: drop the changing indexes before the table rebuild.
    op.drop_index("ix_policies_created_at", table_name="policies")
    op.drop_index("ix_policies_session_id", table_name="policies")
    op.drop_index("ix_policies_default_name_cksum", table_name="policies")

    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_policies_session_id_name_cksum", type_="unique")
        batch_op.create_unique_constraint(
            "uq_policies_session_id_name_cksum", [*ws, "session_id", "name_cksum"]
        )

    op.create_index("ix_policies_created_at", "policies", [*ws, "created_at", "id"])
    op.create_index("ix_policies_session_id", "policies", [*ws, "session_id", "id"])
    op.create_index(
        "ix_policies_default_name_cksum",
        "policies",
        [*ws, "name_cksum"],
        unique=True,
        sqlite_where=sa.text("scope = 1"),
        postgresql_where=sa.text("scope = 1"),
    )

    with op.batch_alter_table("hosts", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_constraint("uq_hosts_token_hash", type_="unique")
        batch_op.create_unique_constraint("uq_hosts_token_hash", [*ws, "token_hash"])

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def upgrade() -> None:
    """Add the primary-key columns to every secondary index."""
    _swap_plain(to_new=True)
    op.drop_index("ix_comments_created_at", table_name="comments")
    _drop_unique_partial()
    _create_unique_partial(to_new=True)
    _rebuild_constraint_tables(to_new=True)


def downgrade() -> None:
    """Restore the pre-PK-inclusion index shapes."""
    _rebuild_constraint_tables(to_new=False)
    _drop_unique_partial()
    _create_unique_partial(to_new=False)
    op.create_index("ix_comments_created_at", "comments", ["created_at"])
    _swap_plain(to_new=False)
