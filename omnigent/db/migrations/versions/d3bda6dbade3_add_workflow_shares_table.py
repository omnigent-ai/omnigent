"""add workflow_shares table

Revision ID: d3bda6dbade3
Revises: ffeec66eadb5
Create Date: 2026-11-18 00:00:00.000000

Adds ``workflow_shares``, one row per user or group a saved workflow is shared
with. They may view, run and copy the workflow, but not change it. Session grants
stay in ``session_permissions``. The application deletes the rows with the owning
``workflows`` row.

Additive and unused by earlier releases, so this schema can ship before the
application code that writes it. Rollout and rollback order:

- The upgrade runs at startup. A server started against the upgraded database
  needs a build that knows this revision; older builds refuse to start.
- To roll back to a build without this revision, first downgrade to
  ``ffeec66eadb5`` with a build that knows it. The downgrade drops the table and
  any shares in it, so it is safe only before the first share is saved.

There are no foreign-key constraints (schema Rule R032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "d3bda6dbade3"
down_revision: str | None = "ffeec66eadb5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
# Dialects that store a value of any type in any column, and ignore VARCHAR lengths.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Create the ``workflow_shares`` table."""
    constraints: list[sa.CheckConstraint] = []
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        constraints = [
            # 128 characters at up to four bytes each.
            sa.CheckConstraint(
                "length(CAST(user_id AS BLOB)) <= 512",
                name="ck_workflow_shares_user_id_bytes",
            ),
            sa.CheckConstraint(
                "typeof(workspace_id) = 'integer'",
                name="ck_workflow_shares_workspace_id_integer",
            ),
            sa.CheckConstraint(
                "typeof(level) = 'integer'",
                name="ck_workflow_shares_level_integer",
            ),
        ]
    op.create_table(
        "workflow_shares",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        # The workflow's ID as 16 raw bytes (Uuid16), read back as bare hex.
        sa.Column("workflow_id", Uuid16(), nullable=False),
        # String(128) matches session_permissions.user_id; a user or group ID.
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        # Leads with workspace_id; the prefix serves per-workflow reads and deletes.
        sa.PrimaryKeyConstraint("workspace_id", "workflow_id", "user_id"),
        *constraints,
    )
    # Looks up the workflows shared with one user, for a "shared with me" list.
    op.create_index(
        "ix_workflow_shares_user_id",
        "workflow_shares",
        ["workspace_id", "user_id", "workflow_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``workflow_shares`` index and table."""
    op.drop_index("ix_workflow_shares_user_id", table_name="workflow_shares")
    op.drop_table("workflow_shares")
