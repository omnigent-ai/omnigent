"""add live-state columns to omnigent_conversation_metadata

Revision ID: d7f1a2b3c4e5
Revises: a7b3c4d5e6f7
Create Date: 2026-07-14 00:00:00.000000

Adds three per-session live-state columns so any server replica can
serve the sidebar's live fields (they previously lived only in the
in-memory caches of the replica holding the session's runner tunnel):

- ``runner_last_seen``: nullable Integer — epoch seconds the bound
  runner's tunnel was last observed alive. ``runner_online`` is derived
  from freshness (like ``host_is_live``), so a replica/host that dies
  without a graceful disconnect self-corrects after the TTL.
- ``live_status``: nullable SmallInteger — last relay-observed turn
  status (idle/running/waiting/failed; see
  ``enum_codecs.SESSION_LIVE_STATUS``). NULL means no relay has ever
  reported on the session.
- ``pending_elicitation_count``: nullable Integer — outstanding
  elicitation (approval-prompt) count. NULL means never written.

All three are written by the pod holding the runner tunnel. They live on
``omnigent_conversation_metadata`` (Omnigent operational state, beside
``runner_id``/``host_id``), so writes cannot bump
``conversations.updated_at`` — which drives sidebar ordering.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7f1a2b3c4e5"
down_revision: str | None = "a7b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("runner_last_seen", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("live_status", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("pending_elicitation_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("pending_elicitation_count")
        batch_op.drop_column("live_status")
        batch_op.drop_column("runner_last_seen")
