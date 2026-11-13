"""Add saved workflows.

Revision ID: ce12af3da168
Revises: 5d872cd6632a

``workflows`` holds each saved workflow: its owner, name, description and the
plan a run copies into the session's journal, whose entries carry the
workflow's ID in ``session_journal_entries.workflow_id``. Workflows have no
versions; an edit rewrites the row. The primary key is ``(workspace_id, id)``,
and ``ix_workflows_user_id_name`` lists an owner's workflows by name. The plan
is compressed JSON in a binary column, which the ORM caps at 16,383 stored
bytes. The table has no database defaults: writes name the workspace.

Deployment: apply this migration before a server that uses the table.
Servers that migrate at startup do so on their own. Processes already running
an older build keep working on the migrated schema, since they never touch the
new table. An older build does not start on it, though: startup refuses a
schema revision the build does not know.

Rollback: stop the servers, downgrade with a build that includes this
migration (``OMNIGENT_DB_URL=<uri> alembic -c omnigent/db/alembic.ini
downgrade 5d872cd6632a``), then start the older build. Downgrade drops the
table and every saved workflow.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "ce12af3da168"
down_revision: str | None = "5d872cd6632a"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "workflows"
_INDEX = "ix_workflows_user_id_name"


def upgrade() -> None:
    """Create ``workflows`` and its owner-and-name index."""
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("plan", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(_INDEX, _TABLE, ["workspace_id", "user_id", "name", "id"])


def downgrade() -> None:
    """Drop ``workflows`` and every saved workflow."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
