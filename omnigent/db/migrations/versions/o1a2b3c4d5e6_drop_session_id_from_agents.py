"""drop agents.session_id; add agents.kind and ix_conversations_agent_id

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Removes the back-pointer ``agents.session_id`` (FK to ``conversations.id``)
in favour of an explicit ``agents.kind`` column (``'template'`` |
``'session'``) that carries the same distinction without a circular
reference.  The upgrade reads ``session_id`` before dropping it to back-fill
``kind`` correctly.  The forward pointer ``conversations.agent_id`` remains
the authoritative runtime link; ``kind`` is set at row-creation time and
never changes.

Also adds ``ix_conversations_agent_id`` to speed up "find the conversation
that owns this agent" lookups (used in ``replace_agent`` and
``fork_conversation``).

SQLite note: ``conversations.agent_id`` is a FK to ``agents.id`` with
``ON DELETE CASCADE``.  SQLite runs migrations with ``PRAGMA foreign_keys = ON``
so any ``batch_alter_table`` that drops and recreates ``agents`` would
cascade-delete bound conversations.  Both upgrade and downgrade issue
``PRAGMA foreign_keys = OFF`` (SQLite-only, guarded by dialect) before the
batch operations and ``PRAGMA foreign_keys = ON`` after.  ``recreate="always"``
is also set on SQLite and ``"auto"`` on other dialects.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Naming convention used by the prior migration (d7a6b3c91f48) when it
# created fk_agents_session_id and ix_agents_session_id. Passing the same
# convention here lets Alembic locate the constraints by name even on SQLite,
# which may not reflect constraint names reliably without it.
_AGENTS_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}

_logger = logging.getLogger(__name__)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """
    1. Add ``agents.kind`` (nullable, ``recreate="always"`` on SQLite to avoid
       cascade-deleting conversations during the table rebuild).
    2. Back-fill ``kind`` from ``session_id``.
    3. Drop ``session_id`` and its FK/indexes; make ``kind`` NOT NULL; recreate
       ``ix_agents_template_name`` scoped to ``kind = 'template'``.
    4. Add ``ix_conversations_agent_id`` on ``conversations.agent_id``.
    """
    sqlite = _is_sqlite()
    # On SQLite, disable FK enforcement so batch table-rebuilds do not
    # cascade-delete conversations via conversations.agent_id → agents.id.
    # PRAGMA is SQLite-only and must be guarded by dialect.
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # Step 2: add kind as nullable so we can back-fill before making it NOT NULL.
    with op.batch_alter_table("agents", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=16), nullable=True))

    # Step 3: back-fill from session_id while it still exists.
    op.execute(sa.text("UPDATE agents SET kind = 'session' WHERE session_id IS NOT NULL"))
    op.execute(sa.text("UPDATE agents SET kind = 'template' WHERE session_id IS NULL"))
    _logger.info("Upgrade: back-filled agents.kind from session_id")

    # Step 4: drop session_id, make kind NOT NULL, recreate the name index.
    # MySQL requires the FK to be dropped before the index that backs it;
    # on SQLite the table rebuild handles ordering automatically.
    with op.batch_alter_table(
        "agents",
        recreate="always" if sqlite else "auto",
        naming_convention=_AGENTS_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_index("ix_agents_template_name")
        batch_op.drop_constraint("fk_agents_session_id", type_="foreignkey")
        batch_op.drop_index("ix_agents_session_id")
        batch_op.drop_column("session_id")
        batch_op.alter_column("kind", existing_type=sa.String(16), nullable=False)
        batch_op.create_index(
            "ix_agents_template_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("kind = 'template'"),
            postgresql_where=sa.text("kind = 'template'"),
        )

    # Step 5: index for agent-ownership lookups via the forward pointer.
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"])

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """
    Reverse: drop ``kind``, re-add ``session_id`` back-populated from
    ``conversations.agent_id``, and drop ``ix_conversations_agent_id``.
    """
    op.drop_index("ix_conversations_agent_id", table_name="conversations")

    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # Step 1: add session_id as nullable (no FK yet) so we can back-fill.
    with op.batch_alter_table("agents", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=64), nullable=True))

    # Step 2: back-populate from the forward pointer before adding indexes.
    op.execute(
        sa.text(
            "UPDATE agents SET session_id = ("
            "  SELECT id FROM conversations WHERE conversations.agent_id = agents.id LIMIT 1"
            ") WHERE kind = 'session'"
        )
    )
    _logger.info("Downgrade: back-populated agents.session_id from conversations.agent_id")

    # Step 3: drop kind, add FK and indexes now that data is correct.
    with op.batch_alter_table(
        "agents",
        recreate="always" if sqlite else "auto",
        naming_convention=_AGENTS_NAMING_CONVENTION,
    ) as batch_op:
        batch_op.drop_index("ix_agents_template_name")
        batch_op.drop_column("kind")
        batch_op.create_foreign_key(
            "fk_agents_session_id",
            "conversations",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_agents_session_id", ["session_id"], unique=True)
        batch_op.create_index(
            "ix_agents_template_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("session_id IS NULL"),
            postgresql_where=sa.text("session_id IS NULL"),
        )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
