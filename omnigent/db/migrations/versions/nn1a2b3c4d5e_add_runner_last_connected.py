"""add runner_last_connected to omnigent_conversation_metadata

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-05 00:00:00.000000

Adds a nullable ``runner_last_connected`` column beside ``runner_last_seen``.

Both stamps record when the bound runner's tunnel was last observed, and both
are written by the pod holding the tunnel (on connect and on the periodic
liveness sweep). They differ only on disconnect: ``runner_last_seen`` is
cleared on a graceful tunnel close so the sidebar flips offline immediately,
whereas ``runner_last_connected`` is left intact. A replica deciding whether a
mid-turn runner has actually vanished reads ``runner_last_connected`` so a
sibling replica's brief reconnect blip (which clears ``runner_last_seen``)
cannot look like the runner departing.

Like the other live-state columns it lives on
``omnigent_conversation_metadata`` so writes never bump
``conversations.updated_at`` (which drives sidebar ordering).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("runner_last_connected", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("runner_last_connected")
