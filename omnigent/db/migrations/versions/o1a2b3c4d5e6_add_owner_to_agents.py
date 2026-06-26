"""add owner to agents

Revision ID: o1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-06-26 00:00:00.000000

Adds ``agents.owner`` so agents can be standalone, user-owned resources
managed through the agents CRUD API (``/v1/agents``), independent of any
session. ``owner`` is nullable:

- ``NULL`` for operator-seeded built-in/template agents (global) and for
  session-scoped agents (``session_id`` set; access inherited from the
  session).
- a user id for standalone user agents (``session_id IS NULL``).

Name-uniqueness is re-scoped accordingly: built-ins keep a global unique
name (``session_id IS NULL AND owner IS NULL``), while standalone user
agents are unique per ``(owner, name)`` so different users can each have an
agent with the same name. The old ``ix_agents_template_name`` (unique name
across ALL ``session_id IS NULL`` rows) is replaced by these two partial
indexes.
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

_logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Add nullable ``agents.owner`` and re-scope template-name uniqueness."""
    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(sa.Column("owner", sa.String(length=256), nullable=True))
        batch_op.drop_index("ix_agents_template_name")
        batch_op.create_index(
            "ix_agents_builtin_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("session_id IS NULL AND owner IS NULL"),
            postgresql_where=sa.text("session_id IS NULL AND owner IS NULL"),
        )
        batch_op.create_index(
            "ix_agents_owner_name",
            ["owner", "name"],
            unique=True,
            sqlite_where=sa.text("session_id IS NULL AND owner IS NOT NULL"),
            postgresql_where=sa.text("session_id IS NULL AND owner IS NOT NULL"),
        )
        batch_op.create_index("ix_agents_owner", ["owner"])


def downgrade() -> None:
    """Drop standalone user agents, then restore the prior schema.

    Owned agents (``session_id IS NULL AND owner IS NOT NULL``) have no
    faithful pre-owner representation — and could collide under the
    restored global template-name index — so they are deleted, mirroring
    the session-agent downgrade policy.
    """
    bind = op.get_bind()
    owned_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM agents WHERE session_id IS NULL AND owner IS NOT NULL"),
    ).scalar_one()
    if owned_count:
        _logger.warning("Downgrade will delete %s standalone user agents", owned_count)
    op.execute(sa.text("DELETE FROM agents WHERE session_id IS NULL AND owner IS NOT NULL"))
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_index("ix_agents_owner")
        batch_op.drop_index("ix_agents_owner_name")
        batch_op.drop_index("ix_agents_builtin_name")
        batch_op.create_index(
            "ix_agents_template_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("session_id IS NULL"),
            postgresql_where=sa.text("session_id IS NULL"),
        )
        batch_op.drop_column("owner")
