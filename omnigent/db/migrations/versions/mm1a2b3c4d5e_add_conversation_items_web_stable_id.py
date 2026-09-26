"""Add ``web_stable_id`` to ``conversation_items``.

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-09-24 00:00:00.000000

A native-terminal web message is mirrored back by the transcript forwarder
and persisted under a forwarder-derived id, so nothing durable linked the
committed item to the web client's ``stable_id``. A re-send of that stable id
after a server restart (which wipes the in-memory pending index) therefore
pasted the prompt into the pane a second time. This column records the web
stable id on the committed mirror; the dispatch pre-check resolves a re-send
to it with an indexed point lookup instead of forwarding again.

Adding a nullable column and a plain index is safe on SQLite, PostgreSQL,
and MySQL alike; the downgrade's ``drop_column`` uses batch mode for SQLite.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``web_stable_id`` column and its lookup index."""
    op.add_column(
        "conversation_items",
        sa.Column("web_stable_id", Uuid16(), nullable=True),
    )
    op.create_index(
        "ix_conversation_items_web_stable_id",
        "conversation_items",
        ["workspace_id", "conversation_id", "web_stable_id"],
    )


def downgrade() -> None:
    """Drop the ``web_stable_id`` column and its index."""
    op.drop_index("ix_conversation_items_web_stable_id", table_name="conversation_items")
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.drop_column("web_stable_id")
