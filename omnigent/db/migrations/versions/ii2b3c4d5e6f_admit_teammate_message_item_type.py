"""Admit the teammate_message conversation-item type.

Revision ID: ii2b3c4d5e6f
Revises: hh1b2c3d4e5f
Create Date: 2026-09-17 00:00:00.000000

Widens ``ck_conversation_items_type`` to admit code 12
(``teammate_message`` — a harness-internal teammate delivery mirrored
from a native transcript, e.g. Claude Code agent teams). Codes are
append-only; no data changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "ii2b3c4d5e6f"
down_revision: str | None = "hh1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_conversation_items_type"
_TABLE = "conversation_items"


def upgrade() -> None:
    """Recreate the item-type CHECK with code 12 admitted."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT,
            "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)",
        )


def downgrade() -> None:
    """Restore the pre-teammate_message CHECK (codes 1-11)."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT,
            "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)",
        )
