"""Add the outcome an agent records for a scheduled-task run.

Revision ID: e7c3a1b9d240
Revises: d0a1f879a01d

A run's ``status`` says whether it finished, not whether it reached its goal.
These nullable columns will hold what an agent records about the goal:
``outcome_success``, ``outcome_payload`` and ``outcome_recorded_at``. Existing
runs keep NULL (nothing recorded). Nothing reads or writes them yet.

``outcome_success`` is a typed column so runs can later be told apart by it.
The emoji and words can repeat task content, so they go in ``outcome_payload``,
an envelope that is opaque to the database and private to the scheduled-task
store (plaintext today, with room for an encrypted variant). Declared lengths
are not enforced on every engine, so
``ck_scheduled_task_runs_outcome_payload_size`` caps the stored bytes at 2,048.

Recording an outcome finds the session's newest run whose prompt was
delivered, so ``ix_scheduled_task_runs_conversation_status_scheduled`` orders
a session's runs of one status by ``scheduled_at`` (then ``id``). Read
backward, it returns that run without scanning a continued session's history.

Deployment: apply this migration before a server that reads the columns.
Servers that migrate at startup do so on their own. The columns are nullable
and the index is new, so servers without the change keep working on the
migrated schema. Downgrade drops the index, the columns and every recorded
outcome; downgrade only after rolling back the servers that read them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a1b9d240"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Mirrors omnigent.db.db_models.RUN_OUTCOME_PAYLOAD_MAX_BYTES at this revision.
_PAYLOAD_MAX_BYTES = 2048


def upgrade() -> None:
    """Add the nullable outcome columns, their size check and the latest-run index."""
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.add_column(sa.Column("outcome_success", sa.Boolean(), nullable=True))
        batch.add_column(
            sa.Column("outcome_payload", sa.LargeBinary(_PAYLOAD_MAX_BYTES), nullable=True)
        )
        batch.add_column(sa.Column("outcome_recorded_at", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "ck_scheduled_task_runs_outcome_payload_size",
            f"outcome_payload IS NULL OR length(outcome_payload) <= {_PAYLOAD_MAX_BYTES}",
        )
    op.create_index(
        "ix_scheduled_task_runs_conversation_status_scheduled",
        "scheduled_task_runs",
        ["workspace_id", "conversation_id", "status", "scheduled_at", "id"],
    )


def downgrade() -> None:
    """Drop the index, the size check and the outcome columns."""
    op.drop_index(
        "ix_scheduled_task_runs_conversation_status_scheduled", table_name="scheduled_task_runs"
    )
    with op.batch_alter_table("scheduled_task_runs") as batch:
        batch.drop_constraint("ck_scheduled_task_runs_outcome_payload_size", type_="check")
        batch.drop_column("outcome_recorded_at")
        batch.drop_column("outcome_payload")
        batch.drop_column("outcome_success")
