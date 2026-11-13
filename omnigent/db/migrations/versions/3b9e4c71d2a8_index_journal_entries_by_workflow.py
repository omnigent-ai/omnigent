"""Index journal entries by saved workflow.

Revision ID: 3b9e4c71d2a8
Revises: ce12af3da168

``ix_session_journal_entries_workflow`` serves a saved workflow's page, which
lists the workflow's runs in every session, newest first. A run is the plan
entry that named the workflow in ``session_journal_entries.workflow_id``. The
index is ``(workspace_id, workflow_id, kind, created_at, conversation_id,
position)``: non-unique, like the table's other indexes, and holding the whole
primary key.

Deployment: apply this migration before a server that lists workflow runs.
Servers that migrate at startup do so on their own. Building the index reads
the table; on a large journal, apply it outside peak hours. Processes running an
older build keep working on the migrated schema, but an older build does not
start on it: startup refuses a schema revision the build does not know.

Rollback: stop the servers, downgrade with a build that includes this
migration (``OMNIGENT_DB_URL=<uri> alembic -c omnigent/db/alembic.ini
downgrade ce12af3da168``), then start the older build. Downgrade drops the
index; no data is lost.
"""

from __future__ import annotations

from alembic import op

revision: str = "3b9e4c71d2a8"
down_revision: str | None = "ce12af3da168"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "session_journal_entries"
_INDEX = "ix_session_journal_entries_workflow"


def upgrade() -> None:
    """Create the index that lists a workflow's runs."""
    op.create_index(
        _INDEX,
        _TABLE,
        ["workspace_id", "workflow_id", "kind", "created_at", "conversation_id", "position"],
    )


def downgrade() -> None:
    """Drop the index."""
    op.drop_index(_INDEX, table_name=_TABLE)
