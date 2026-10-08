"""index agents by kind, owner, and creation order

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-10-01 00:00:00.000000

Adds ``ix_agents_kind_owner_created`` on ``(workspace_id, kind, created_by,
created_at, id)`` so a user's own agents (``kind`` = user, ``created_by`` = the
user) can be listed newest first with bounded keyset pagination. ``kind`` and
``created_by`` are equality columns, ``created_at`` gives the order, and ``id``
(the primary-key suffix) breaks ties. See ``designs/REUSABLE_USER_AGENTS.md``.

Deployment: additive; older application code ignores it. On PostgreSQL it
builds ``CONCURRENTLY`` so writes to ``agents`` continue during the build.
Create it before enabling the "my agents" listing (including deployments that
apply schema outside alembic). Roll back by downgrading this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_agents_kind_owner_created"
_TABLE = "agents"


_COLUMNS = ["workspace_id", "kind", "created_by", "created_at", "id"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_INDEX, _TABLE, _COLUMNS)
        return
    # CONCURRENTLY can't run in a transaction; autocommit lets writers proceed.
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an INVALID index that IF NOT EXISTS would keep.
        # Match it only on the table this connection resolves, never a same-named index elsewhere.
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indexrelid::regclass::text FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indrelid = to_regclass(:table) AND c.relname = :name "
                    "AND NOT i.indisvalid"
                ),
                {"table": _TABLE, "name": _INDEX},
            )
            .scalar()
        )
        if invalid:
            # regclass text is quoted and schema-qualified as needed; CONCURRENTLY
            # avoids blocking writers behind an exclusive lock.
            op.execute(f"DROP INDEX CONCURRENTLY {invalid}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} ({', '.join(_COLUMNS)})"
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
