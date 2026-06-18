"""add next_position to conversations

Revision ID: n1a2b3c4d5e6
Revises: m1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000

Adds the maintained item-position allocator to the conversations table:

- ``next_position``: nullable Integer — the next 0-based position to assign
  to an appended conversation item. ``append()`` reads and advances this
  counter instead of scanning ``MAX(conversation_items.position)`` on every
  write, which keeps position assignment O(1) and collision-free under the
  conversation lock.

The column is added nullable with NO server default, so every pre-existing
conversation reads ``NULL``. ``append()`` treats ``NULL`` as "not yet
populated": it falls back to a one-time ``MAX(position)`` scan and then
persists the advanced counter on the conversation row, so the very next
append on that conversation is scan-free. New rows created through the ORM
start at ``0`` via the model-level column default.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n1a2b3c4d5e6"
down_revision: str | None = "m1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column("next_position", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("next_position")
