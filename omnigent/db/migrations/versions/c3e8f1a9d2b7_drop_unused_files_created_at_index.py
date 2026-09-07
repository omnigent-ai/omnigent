"""Drop the unused ix_files_created_at index.

Revision ID: c3e8f1a9d2b7
Revises: z9a2b3c4d5e6
Create Date: 2026-07-21 00:00:00.000000

``ix_files_created_at`` on ``files`` (``workspace_id, created_at, id``) does not
earn its keep. It only serves a session-less listing (``WHERE workspace_id
ORDER BY created_at, id``), and there is no such caller: every read of a
session's files goes through ``FileStore.list(session_id=...)`` — the agent
``list_files`` tool (in-process and runner-proxied over
``GET /v1/sessions/{id}/resources/files``) and the session-resources route.
Those all filter by ``session_id`` and are served by
``ix_files_session_id_created_at`` (``workspace_id, session_id, created_at,
id``). Global (``session_id IS NULL``) files are only ever surfaced via the
``include_unscoped`` OR query, which also rides the session-scoped index.

So ``ix_files_created_at`` is pure write/space overhead.
``ix_files_session_id_created_at`` is unchanged.

Index-only, no data change. ``DROP``/``CREATE INDEX`` is native on every
dialect (no table rebuild). Downgrade restores the index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c3e8f1a9d2b7"
down_revision: str | None = "z9a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_files_created_at"
_TABLE = "files"


def upgrade() -> None:
    """Drop the unused (workspace_id, created_at, id) index."""
    op.drop_index(_INDEX, table_name=_TABLE)


def downgrade() -> None:
    """Restore the (workspace_id, created_at, id) index."""
    op.create_index(_INDEX, _TABLE, ["workspace_id", "created_at", "id"])
