"""Add durable interrupted-session recovery leases and retry budgets.

Deploy this additive schema before recovery application changes. Older binaries
ignore the table. Roll back the application before downgrading; existing hosts
and conversations are unaffected. The store bounds opaque outcomes below 16 KiB.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "7c91e4a2b6d8"
down_revision: str | None = "nn1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_recovery",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("host_id", Uuid16(), nullable=False),
        sa.Column("episode_key", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.BigInteger(), nullable=False),
        sa.Column("lease_until", sa.BigInteger(), nullable=True),
        sa.Column("claim_id", Uuid16(), nullable=True),
        sa.Column("outcome_json", sa.LargeBinary(), nullable=True),
        sa.Column("terminal_reason", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "session_id", name="pk_session_recovery"),
    )
    op.create_index(
        "ix_session_recovery_host_lease",
        "session_recovery",
        ["workspace_id", "host_id", "lease_until", "session_id"],
    )


def downgrade() -> None:
    op.drop_table("session_recovery")
