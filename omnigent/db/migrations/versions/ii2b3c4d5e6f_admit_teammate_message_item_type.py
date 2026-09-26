"""Admit the teammate_message conversation-item type.

Revision ID: ii2b3c4d5e6f
Revises: ll1a2b3c4d5e
Create Date: 2026-09-17 00:00:00.000000

Widens ``ck_conversation_items_type`` to admit code 12
(``teammate_message`` — a harness-internal teammate delivery mirrored
from a native transcript, e.g. Claude Code agent teams).

Codes are append-only, so the upgrade changes no data. The downgrade
deletes any ``teammate_message`` (type 12) rows before restoring the
narrower constraint (see :func:`downgrade`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ii2b3c4d5e6f"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_conversation_items_type"
_TABLE = "conversation_items"


def _replace_check_constraint(codes: str) -> None:
    bind = op.get_bind()
    condition = f"type IN ({codes})"
    if bind.dialect.name == "cockroachdb":
        # CRDB must publish the drop before reusing the constraint name.
        existing = {row["name"] for row in sa.inspect(bind).get_check_constraints(_TABLE)}
        if _CONSTRAINT in existing:
            with op.batch_alter_table(_TABLE) as batch_op:
                batch_op.drop_constraint(_CONSTRAINT, type_="check")
            bind.commit()
            bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.create_check_constraint(_CONSTRAINT, condition)
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, condition)


def upgrade() -> None:
    """Recreate the item-type CHECK with code 12 admitted."""
    _replace_check_constraint("1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12")


def downgrade() -> None:
    """Restore the pre-teammate_message CHECK (codes 1-11).

    Any ``teammate_message`` (type 12) rows are deleted first, so the
    narrower constraint re-applies without failing on out-of-range data
    and a rolled-back (older) application never has to decode code 12.
    These are display-only ``NON_CONTENT`` items, reconstructible from the
    native transcript the harness keeps in its own context, so dropping
    them on rollback is a deliberate, data-losing tradeoff for a cleanly
    reversible schema.
    """
    bind = op.get_bind()
    bind.execute(sa.text(f"DELETE FROM {_TABLE} WHERE type = 12"))
    if bind.dialect.name == "cockroachdb":
        # CRDB won't mix this DML with the constraint DDL in one txn;
        # publish the delete before the schema change (mirrors
        # _replace_check_constraint's own commit/isolation dance).
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    _replace_check_constraint("1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11")
