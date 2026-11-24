"""Scope agents to a project, so a project role can be a custom agent.

Revision ID: e5c2a9f7d3b1
Revises: c9e5a7b3d1f2

Adds nullable columns, and no data changes:

- ``agents.project_id``: the project a role's agent belongs to. NULL for
  every existing agent (built-ins, uploads, installs), which keep their
  current meaning.
- ``project_identities.project_agent_id``: the seat's project agent. The
  seat keeps ``agent_id``, ``instructions``, ``model`` and ``effort`` with
  their current meaning (the agent it starts from and its persona), so an older binary
  that ignores the new column still runs every role as before.
- ``ix_agents_owner_project_created`` on ``(workspace_id, kind, created_by,
  project_id, created_at, id)``: "my agents" (``project_id IS NULL``) and a
  project's agents both seek it. Built ``CONCURRENTLY`` on PostgreSQL.
  ``ix_agents_kind_owner_created`` stays until older servers no longer use it.

Rollback: older binaries ignore the columns and index. A project agent is a
``kind = user`` row owned by the project owner, so an older binary shows it in
the owner's own agent list, and it stays runnable there. Downgrade drops the
columns and the index; project agents' rows stay as ordinary user agents.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e5c2a9f7d3b1"
down_revision: str | None = "c9e5a7b3d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_agents_owner_project_created"
_COLUMNS = ["workspace_id", "kind", "created_by", "project_id", "created_at", "id"]


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _create_index() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_INDEX, "agents", _COLUMNS)
        return
    # CONCURRENTLY can't run in a transaction; autocommit lets writers proceed.
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an INVALID index that IF NOT EXISTS would keep.
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indexrelid::regclass::text FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indrelid = to_regclass('agents') AND c.relname = :name "
                    "AND NOT i.indisvalid"
                ),
                {"name": _INDEX},
            )
            .scalar()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY {invalid}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON agents ({', '.join(_COLUMNS)})"
        )


def upgrade() -> None:
    if "project_id" not in _columns("agents"):
        op.add_column("agents", sa.Column("project_id", Uuid16(), nullable=True))
    if "project_agent_id" not in _columns("project_identities"):
        op.add_column("project_identities", sa.Column("project_agent_id", Uuid16(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("agents")}
    # PostgreSQL always runs the build: it also repairs an INVALID leftover index.
    if op.get_bind().dialect.name == "postgresql" or _INDEX not in indexes:
        _create_index()


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="agents")
    with op.batch_alter_table("project_identities") as batch:
        batch.drop_column("project_agent_id")
    with op.batch_alter_table("agents") as batch:
        batch.drop_column("project_id")
