"""add binding and live-observation ownership to session metadata

Revision ID: d42f66e0a5b1
Revises: mm1a2b3c4d5e
Create Date: 2026-10-16 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d42f66e0a5b1"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"
_COLUMN_NAMES = (
    "runner_binding_epoch",
    "live_status_revision",
    "live_status_observation_id",
    "live_status_response_id",
    "live_status_error_key",
    "live_status_error_labels",
    "terminal_pending",
    "runner_offline_decision",
)


def upgrade() -> None:
    columns = (
        sa.Column("runner_binding_epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("live_status_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("live_status_observation_id", sa.String(64), nullable=True),
        sa.Column("live_status_response_id", sa.LargeBinary(), nullable=True),
        sa.Column("live_status_error_key", sa.String(64), nullable=True),
        sa.Column("live_status_error_labels", sa.LargeBinary(), nullable=True),
        sa.Column("terminal_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("runner_offline_decision", sa.LargeBinary(), nullable=True),
    )
    for column in columns:
        op.add_column(_TABLE, column)


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for column_name in reversed(_COLUMN_NAMES):
            batch_op.drop_column(column_name)
