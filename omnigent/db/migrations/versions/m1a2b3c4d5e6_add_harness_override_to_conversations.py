"""add harness_override to conversations

Revision ID: m1a2b3c4d5e6
Revises: l1a2b3c4d5e6
Create Date: 2026-06-11 00:00:00.000000

Adds the per-session brain-harness override to the conversations table:

- ``harness_override``: nullable String(64) — per-session harness
  override for the bound agent's brain (e.g. ``"pi"`` or
  ``"openai-agents"``). NULL means use the harness declared in the
  agent spec (``executor.config.harness``).

Set once via ``POST /v1/sessions`` (the new-chat harness picker) and
read by the runner when it resolves the harness for the first turn.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m1a2b3c4d5e6"
down_revision: str | None = "l1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column("harness_override", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("harness_override")
