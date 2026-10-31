"""index conversation metadata by live status

Revision ID: nn1a2b3c4d5e
Revises: nf12ab34cd56
Create Date: 2026-10-09 00:00:00.000000

Adds ``ix_conversation_metadata_live_status`` on ``(live_status, workspace_id,
id)`` so the interrupted-session recoverer can keyset-page every session that
reads mid-turn (``running``/``waiting``) or ``failed`` across workspaces with a
bounded scan. Those status codes are contiguous, so one range over the index
serves the whole traversal; ``workspace_id, id`` (the primary key) breaks ties.

Deployment: additive; older application code ignores it. On PostgreSQL it
builds ``CONCURRENTLY`` so writes to ``omnigent_conversation_metadata`` continue
during the build. Create it before enabling
``sandbox.auto_resume_interrupted`` (including deployments that apply schema
outside alembic). Roll back by downgrading this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "nf12ab34cd56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_metadata_live_status"
_TABLE = "omnigent_conversation_metadata"


_COLUMNS = ["live_status", "workspace_id", "id"]


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
