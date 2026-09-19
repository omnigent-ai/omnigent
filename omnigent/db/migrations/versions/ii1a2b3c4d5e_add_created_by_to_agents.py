"""add created_by to agents

Revision ID: ii1a2b3c4d5e
Revises: hh1b2c3d4e5f
Create Date: 2026-09-18 00:00:00.000000

Adds ``created_by`` to the ``agents`` table so agent-code mutations
(``PUT /v1/sessions/{id}/agent`` and the session-scoped MCP-server CRUD
routes) can be restricted to the user who created the agent.

- ``created_by``: nullable ``String(128)`` — the identity of the creating
  user for a session-scoped agent. NULL for template agents (which are
  read-only through those routes), for single-user mode (no identity), and
  for rows created before this migration.

Left nullable with no backfill: pre-existing session-scoped rows keep NULL
and authorize against the owning session's owner at request time, then get
``created_by`` stamped on the first authorized mutation. Template agents keep
NULL permanently and never reach the owner check.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "ii1a2b3c4d5e"
down_revision: str | None = "hh1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from sqlalchemy import Column, String

    with op.batch_alter_table("agents") as batch_op:
        batch_op.add_column(Column("created_by", String(128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("created_by")
