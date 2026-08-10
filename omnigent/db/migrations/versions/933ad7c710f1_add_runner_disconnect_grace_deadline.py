"""add runner_disconnect_grace_deadline to omnigent_conversation_metadata

Revision ID: b1c2d3e4f5a6
Revises: a8b9c0d1e2f3
Create Date: 2026-08-11 00:00:00.000000

Adds a fourth per-session live-state column, alongside runner_last_seen /
live_status / pending_elicitation_count (see d7f1a2b3c4e5): a durable,
cross-replica-visible marker for OMN-104 §5.4's post-disconnect reconnect
grace window.

Before this, the grace deadline lived only in the tunnel-holding replica's
in-memory app.state — invisible to any OTHER replica handling a manager
decision request for that session, which would fall through to the
(already-cleared) runner_last_seen freshness check and falsely classify a
runner mid-grace as dead (410 elicitation_not_resolvable) instead of
pending-redelivery (202).

``runner_disconnect_grace_deadline``: nullable Integer — epoch seconds the
grace expires at; NULL when not currently grace-pending. Written by the
replica holding the tunnel on disconnect (same best-effort mirror as
runner_last_seen, via session_live_state.py), cleared on reconnect or once
the grace genuinely expires.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "933ad7c710f1"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(
            sa.Column("runner_disconnect_grace_deadline", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("runner_disconnect_grace_deadline")
