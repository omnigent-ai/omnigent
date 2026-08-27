"""widen conversation_items.type CHECK for side_question

Revision ID: f2c7a1d9b430
Revises: e5d9bc8ac650
Create Date: 2026-08-26 00:00:00.000000

``/btw`` side questions persist as a new ``side_question`` conversation item
(code 12 in ``omnigent.db.enum_codecs.ITEM_TYPE``). The ``type`` column's
``CHECK`` enumerates the shipped codes, so it has to admit 12 before the
first such item can be written.

Constraint-only change: no data is read or rewritten. ``render_as_batch``
(see migrations/env.py) rebuilds the table on SQLite so the swap lands there
too.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f2c7a1d9b430"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_conversation_items_type"
_CODES_WITH_SIDE_QUESTION = "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)"
_CODES_WITHOUT_SIDE_QUESTION = "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)"


def upgrade() -> None:
    """Admit item type code 12 (``side_question``)."""
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, _CODES_WITH_SIDE_QUESTION)


def downgrade() -> None:
    """Narrow the CHECK back, dropping any side_question rows first.

    Rows written while the wider CHECK was in force would fail the
    narrower one, so they are deleted rather than left to abort the
    rebuild. Side questions are asides the model never saw — losing them
    on a downgrade costs no conversation state.
    """
    op.execute("DELETE FROM conversation_items WHERE type = 12")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, _CODES_WITHOUT_SIDE_QUESTION)
