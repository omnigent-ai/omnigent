"""add session_id column to agents

Revision ID: d7a6b3c91f48
Revises: c7f2a1d83e49
Create Date: 2026-05-14 00:00:00.000000

Adds ``agents.session_id`` for the Alpha runner-state pivot. The
column is nullable so existing template agents remain valid. A unique
index enforces that at most one agent row owns a given session, and
template-name uniqueness is narrowed to rows with ``session_id IS
NULL`` so separate sessions can copy the same agent spec.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a6b3c91f48"
down_revision: str | None = "c7f2a1d83e49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AGENTS_NAME_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
_logger = logging.getLogger(__name__)


def _agents_name_unique_constraint_name() -> str:
    """
    Return the reflected unique constraint name for ``agents.name``.

    SQLite reflects the historical unnamed constraint as ``None``;
    Alembic batch mode can still drop it through the naming
    convention below.

    :returns: Constraint name to pass to ``drop_constraint``.
    """
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_unique_constraints("agents"):
        if constraint["column_names"] == ["name"]:
            return constraint["name"] or "uq_agents_name"
    raise RuntimeError("agents.name unique constraint not found")


def upgrade() -> None:
    """
    Add nullable ``agents.session_id`` plus FK and scoped indexes.

    Nullable preserves existing template-agent rows. The unique
    index prevents two agent rows from claiming the same session id
    while still allowing multiple ``NULL`` values on SQLite and
    PostgreSQL. The partial name index preserves registered-agent
    uniqueness without blocking per-session copies of the same spec.
    """
    name_unique_constraint = _agents_name_unique_constraint_name()
    with op.batch_alter_table(
        "agents",
        naming_convention=_AGENTS_NAME_CONVENTION,
    ) as batch_op:
        batch_op.drop_constraint(name_unique_constraint, type_="unique")
        batch_op.add_column(
            sa.Column("session_id", sa.String(length=64), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_agents_session_id",
            "conversations",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_agents_session_id",
            ["session_id"],
            unique=True,
        )
        batch_op.create_index(
            "ix_agents_template_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("session_id IS NULL"),
            postgresql_where=sa.text("session_id IS NULL"),
        )


def downgrade() -> None:
    """
    Delete session-scoped agents and their conversations because the
    pre-PR1 schema cannot represent them, then drop ``agents.session_id``
    and restore global agent-name uniqueness. This is intentionally
    different from PR 6's downgrade policy: PR 6 aborts loudly on
    orphaned state, while PR 1 deletes these rows because the data was
    created by PR 1 and has no faithful pre-PR1 form.
    """
    bind = op.get_bind()
    session_agent_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM agents WHERE session_id IS NOT NULL"),
    ).scalar_one()
    _logger.warning(
        "Downgrading PR1 will delete %s session-scoped agents and their conversations",
        session_agent_count,
    )
    op.execute(
        sa.text(
            "DELETE FROM conversations "
            "WHERE agent_id IN (SELECT id FROM agents WHERE session_id IS NOT NULL)",
        ),
    )
    op.execute(sa.text("DELETE FROM agents WHERE session_id IS NOT NULL"))
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_index("ix_agents_template_name")
        # MySQL requires the FK to be dropped before the index that backs it.
        batch_op.drop_constraint("fk_agents_session_id", type_="foreignkey")
        batch_op.drop_index("ix_agents_session_id")
        batch_op.drop_column("session_id")
        batch_op.create_unique_constraint("uq_agents_name", ["name"])
