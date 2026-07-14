"""Drop partial indexes for MySQL compatibility.

Revision ID: z5a2b3c4d5e6
Revises: z4a2b3c4d5e6
Create Date: 2026-07-09 00:00:00.000000

MySQL has no partial (``WHERE``-predicated) indexes. The four partial indexes
had leaned on ``sqlite_where`` / ``postgresql_where`` being dialect-scoped, so
MySQL silently dropped the predicate and built a full unique index that "spans
all kinds" — correct DDL, but over-restrictive there (session agents/policies
could not reuse names on MySQL). This replaces them with plain indexes that
behave identically on every dialect:

- ``ix_conversations_parent_title_unique`` — kept UNIQUE, predicate dropped.
  The predicate (``parent_conversation_id IS NOT NULL``) was redundant: it
  keys off the same nullable column, and NULLs are distinct in a unique index,
  so top-level conversations (NULL parent) stay exempt. Behavior is identical.
- ``idx_conversations_parent`` — non-unique, predicate dropped. Now indexes
  every parented row instead of only ``kind = sub_agent`` ones; same query
  plan for the child-session listing.
- ``ix_agents_template_name`` (unique, ``kind = template``) → ``ix_agents_name``
  (plain). Template-name uniqueness moves to the store
  (``SqlAlchemyAgentStore.create``); the plain index backs the name lookup.
- ``ix_policies_default_name_cksum`` (unique, ``scope = default``) →
  ``ix_policies_name_cksum`` (plain). Default-name uniqueness is already
  enforced in the store (``add_default`` / ``update_default``); the plain
  index backs its ``name_cksum`` lookup.

Index columns keep the ``workspace_id`` prefix (and ``id`` suffix on the
non-unique ones) that the surrounding indexes use.

Index-only: no columns change, so no batch table-rebuild (and no SQLite
foreign_keys guard) is needed — ``DROP INDEX`` / ``CREATE INDEX`` are native on
every dialect. Downgrade restores the partial indexes (Postgres/SQLite regain
the ``WHERE`` clauses; MySQL never had them).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "z5a2b3c4d5e6"
down_revision: str | None = "z4a2b3c4d5e6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Replace the four partial indexes with plain, MySQL-buildable ones."""
    # conversations: same columns and uniqueness, just no WHERE predicate.
    op.drop_index("idx_conversations_parent", table_name="conversations")
    op.drop_index("ix_conversations_parent_title_unique", table_name="conversations")
    op.create_index(
        "ix_conversations_parent_title_unique",
        "conversations",
        ["workspace_id", "parent_conversation_id", "title"],
        unique=True,
        mysql_length={"title": 512},
    )
    op.create_index(
        "idx_conversations_parent",
        "conversations",
        ["workspace_id", "parent_conversation_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
    )

    # agents: uniqueness moves to the store; keep a plain lookup index.
    op.drop_index("ix_agents_template_name", table_name="agents")
    op.create_index(
        "ix_agents_name", "agents", ["workspace_id", "name", "kind", "id"], unique=False
    )

    # policies: uniqueness already enforced in the store; keep a plain lookup.
    op.drop_index("ix_policies_default_name_cksum", table_name="policies")
    op.create_index(
        "ix_policies_name_cksum", "policies", ["workspace_id", "name_cksum", "id"], unique=False
    )


def downgrade() -> None:
    """Restore the partial indexes (int enum codes: template=1, sub_agent=2, default=1)."""
    op.drop_index("ix_policies_name_cksum", table_name="policies")
    op.create_index(
        "ix_policies_default_name_cksum",
        "policies",
        ["workspace_id", "name_cksum"],
        unique=True,
        sqlite_where=sa.text("scope = 1"),
        postgresql_where=sa.text("scope = 1"),
    )

    op.drop_index("ix_agents_name", table_name="agents")
    op.create_index(
        "ix_agents_template_name",
        "agents",
        ["workspace_id", "name"],
        unique=True,
        sqlite_where=sa.text("kind = 1"),
        postgresql_where=sa.text("kind = 1"),
    )

    op.drop_index("idx_conversations_parent", table_name="conversations")
    op.drop_index("ix_conversations_parent_title_unique", table_name="conversations")
    op.create_index(
        "ix_conversations_parent_title_unique",
        "conversations",
        ["workspace_id", "parent_conversation_id", "title"],
        unique=True,
        sqlite_where=sa.text("parent_conversation_id IS NOT NULL"),
        postgresql_where=sa.text("parent_conversation_id IS NOT NULL"),
        mysql_length={"title": 512},
    )
    op.create_index(
        "idx_conversations_parent",
        "conversations",
        ["workspace_id", "parent_conversation_id", sa.text("created_at DESC"), sa.text("id DESC")],
        unique=False,
        sqlite_where=sa.text("kind = 2"),
        postgresql_where=sa.text("kind = 2"),
    )
