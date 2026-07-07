"""Add policies.scope column ('default' | 'session').

Revision ID: q1a2b3c4d5e6
Revises: p1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Adds an explicit ``scope`` column to the ``policies`` table so queries
can filter by column value instead of checking ``session_id IS NULL``.
This mirrors the ``agents.kind`` column added by ``o1a2b3c4d5e6``.

The upgrade back-fills ``scope`` from ``session_id``:
- rows with ``session_id IS NOT NULL`` → ``scope = 'session'``
- rows with ``session_id IS NULL``     → ``scope = 'default'``

A partial unique index ``ix_policies_default_name`` is also added so
default-policy names are unique at the DB layer (same guarantee that
the application enforced manually before).

SQLite note: same PRAGMA guard / ``recreate="always"`` pattern as
``o1a2b3c4d5e6``.  Two ``batch_alter_table`` passes are needed:
the first adds ``scope`` as nullable (so back-fill can run), the
second makes it NOT NULL.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q1a2b3c4d5e6"
down_revision: str | None = "p1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_logger = logging.getLogger(__name__)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """
    1. Add ``policies.scope`` as nullable (``recreate="always"`` on SQLite).
    2. Back-fill ``scope`` from ``session_id``.
    3. Make ``scope`` NOT NULL; add ``ix_policies_default_name``.
    """
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    # Pass 1: add scope as nullable so we can back-fill before making it NOT NULL.
    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.add_column(sa.Column("scope", sa.String(length=16), nullable=True))

    # Back-fill from session_id.
    op.execute(sa.text("UPDATE policies SET scope = 'session' WHERE session_id IS NOT NULL"))
    op.execute(sa.text("UPDATE policies SET scope = 'default' WHERE session_id IS NULL"))
    _logger.info("Upgrade: back-filled policies.scope from session_id")

    # Pass 2: make scope NOT NULL and add the partial unique index.
    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.alter_column("scope", existing_type=sa.String(16), nullable=False)
        batch_op.create_index(
            "ix_policies_default_name",
            ["name"],
            unique=True,
            sqlite_where=sa.text("scope = 'default'"),
            postgresql_where=sa.text("scope = 'default'"),
        )

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Drop ``policies.scope`` and its partial index."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    with op.batch_alter_table("policies", recreate="always" if sqlite else "auto") as batch_op:
        batch_op.drop_index("ix_policies_default_name")
        batch_op.drop_column("scope")

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
