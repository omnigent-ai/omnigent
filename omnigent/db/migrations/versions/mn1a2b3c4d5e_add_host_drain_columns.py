"""Add the durable host launch fence (drain) columns.

Revision ID: mn1a2b3c4d5e
Revises: d0a1f879a01d
Create Date: 2026-11-01 00:00:00.000000

An operator can drain a host: the server refuses every new runner launch on it
and the host acknowledges, per drain generation, once nothing is queued or
mid-spawn. The fence lives on the ``hosts`` row so it survives a server
restart and is checked inside the launch-admission transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "mn1a2b3c4d5e"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN_NAMES = (
    "drain_generation",
    "drain_started_at",
    "drain_requested_by",
    "drain_acked_generation",
    "drain_acked_at",
    "drain_supported",
)


def _columns() -> list[sa.Column[Any]]:
    """Build fresh column objects (a Column can belong to only one table)."""
    return [
        sa.Column("drain_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("drain_started_at", sa.BigInteger(), nullable=True),
        sa.Column("drain_requested_by", sa.String(128), nullable=True),
        sa.Column("drain_acked_generation", sa.BigInteger(), nullable=True),
        sa.Column("drain_acked_at", sa.BigInteger(), nullable=True),
        sa.Column("drain_supported", sa.Boolean(), nullable=True),
    ]


def upgrade() -> None:
    """Add the drain columns, tolerating a schema built from current metadata."""
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("hosts")}
    missing = [column for column in _columns() if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table("hosts") as batch_op:
        for column in missing:
            batch_op.add_column(column)


def downgrade() -> None:
    """Drop the drain columns."""
    with op.batch_alter_table("hosts") as batch_op:
        for name in reversed(_COLUMN_NAMES):
            batch_op.drop_column(name)
