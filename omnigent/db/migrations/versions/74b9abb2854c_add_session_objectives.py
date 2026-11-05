"""Add the objectives and outcomes an agent records for its session.

Revision ID: 74b9abb2854c
Revises: e5b2c8d4f1a7

A session's status says whether its turns finished, not whether the agent did
what it set out to do. The new ``session_objectives`` table holds what an
agent records about that: one row per objective, in order, with the outcome
the agent later records for it.

The primary key leads with the session, so a session's objectives stay
together. The objective and outcome words can repeat task content, so they go
in ``objective_payload`` and ``outcome_payload``, envelopes that are opaque
to the database and private to the objective store (plaintext today, with room
for an encrypted variant). Declared lengths are not enforced on every engine,
so CHECK constraints cap each at 2,048 stored bytes, and another keeps an
outcome's success, words and time together. ``outcome_success`` stays a typed
column so objectives can later be counted by it.

``ix_session_objectives_conversation_created`` orders a session's objectives
by ``created_at`` (then ``id``) for paged listing and, read backward, for its
latest objective. ``ix_session_objectives_run_created`` does the same for the
objectives recorded during one automation run, which its run history shows; it
ends with ``conversation_id`` so it holds the whole primary key. The table has
no database defaults: writes name the workspace.

Deployment: apply this migration before a server that uses the table. Servers
that migrate at startup do so on their own. Processes already running an older
build keep working on the migrated schema, since they never touch the new
table. An older build does not start on it, though: startup refuses a schema
revision the build does not know.

Rollback: stop the servers, downgrade with a build that includes this
migration (``OMNIGENT_DB_URL=<uri> alembic -c omnigent/db/alembic.ini
downgrade e5b2c8d4f1a7``), then start the older build. Downgrade drops the
table and every recorded objective and outcome.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "74b9abb2854c"
down_revision: str | None = "e5b2c8d4f1a7"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Mirrors omnigent.db.db_models.SESSION_OBJECTIVE_PAYLOAD_MAX_BYTES at this revision.
_PAYLOAD_MAX_BYTES = 2048


def upgrade() -> None:
    """Create ``session_objectives`` with its checks and ordering indexes."""
    op.create_table(
        "session_objectives",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("scheduled_task_run_id", Uuid16(), nullable=True),
        sa.Column("objective_payload", sa.LargeBinary(_PAYLOAD_MAX_BYTES), nullable=True),
        sa.Column("outcome_success", sa.Boolean(), nullable=True),
        sa.Column("outcome_payload", sa.LargeBinary(_PAYLOAD_MAX_BYTES), nullable=True),
        sa.Column("outcome_recorded_at", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "id"),
        sa.CheckConstraint(
            f"objective_payload IS NULL OR length(objective_payload) <= {_PAYLOAD_MAX_BYTES}",
            name="ck_session_objectives_objective_payload_size",
        ),
        sa.CheckConstraint(
            f"outcome_payload IS NULL OR length(outcome_payload) <= {_PAYLOAD_MAX_BYTES}",
            name="ck_session_objectives_outcome_payload_size",
        ),
        sa.CheckConstraint(
            "(outcome_success IS NULL AND outcome_payload IS NULL "
            "AND outcome_recorded_at IS NULL) OR (outcome_success IS NOT NULL "
            "AND outcome_payload IS NOT NULL AND outcome_recorded_at IS NOT NULL)",
            name="ck_session_objectives_outcome_complete",
        ),
    )
    op.create_index(
        "ix_session_objectives_conversation_created",
        "session_objectives",
        ["workspace_id", "conversation_id", "created_at", "id"],
    )
    op.create_index(
        "ix_session_objectives_run_created",
        "session_objectives",
        ["workspace_id", "scheduled_task_run_id", "created_at", "id", "conversation_id"],
    )


def downgrade() -> None:
    """Drop ``session_objectives`` and its indexes."""
    op.drop_index("ix_session_objectives_run_created", table_name="session_objectives")
    op.drop_index("ix_session_objectives_conversation_created", table_name="session_objectives")
    op.drop_table("session_objectives")
