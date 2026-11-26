"""Add workflow answers.

Revision ID: 4c1d7a9e2b6f
Revises: 47d3e8fc3e25

``workflow_answers`` records how the user answered each
``sys_workflow_request_input`` question set: the request ID the tool returns,
the session and step, the verdict, how the answer reached the server and who
sent it, and the questions with their answers as one storage envelope. The
server writes a row when the elicitation resolves; the session's agent never
writes one, so a lane can check an answer before acting on it. The primary key
is ``(workspace_id, request_id)``, and ``ix_workflow_answers_session`` serves
deleting a session's answers with the session. The table has no database
defaults: writes name the workspace.

Deployment: apply this migration before a server that uses the table. Servers
that migrate at startup do so on their own. Processes already running an older
build keep working on the migrated schema, since they never touch the new
table. An older build does not start on it, though: startup refuses a schema
revision the build does not know.

Rollback: stop the servers, downgrade to ``47d3e8fc3e25`` with a build that
includes this migration, then start the older build. The downgrade drops every
recorded answer; the questions and their journal statuses are unaffected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY

from omnigent.db.db_models import Uuid16

revision: str = "4c1d7a9e2b6f"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "workflow_answers"
_INDEX = "ix_workflow_answers_session"
# Largest stored envelope, per the database guide's per-value limit.
_ENVELOPE = sa.LargeBinary().with_variant(VARBINARY(16_383), "mysql")


def upgrade() -> None:
    """Create ``workflow_answers`` and its session index."""
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("step", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("authenticated_by", sa.String(16), nullable=False),
        sa.Column("answered_by", sa.String(128), nullable=True),
        sa.Column("answered_at", sa.BigInteger(), nullable=False),
        sa.Column("content", _ENVELOPE, nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "request_id"),
    )
    op.create_index(_INDEX, _TABLE, ["workspace_id", "conversation_id", "request_id"])


def downgrade() -> None:
    """Drop ``workflow_answers`` and every recorded answer."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
