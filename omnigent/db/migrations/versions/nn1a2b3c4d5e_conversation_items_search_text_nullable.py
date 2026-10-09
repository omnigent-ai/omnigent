"""Make conversation_items.search_text nullable.

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-09-25 00:00:00.000000

Stores that hold item data opaquely return None from _item_search_text and
omit the column on insert; a nullable column keeps those rows and leaves them
out of full-text search.

Deployment: additive and compatible with older application code, which always
writes search_text. Apply it before enabling a store that omits the column.
To roll back, stop such writers first; the downgrade then backfills NULL rows
with '' in bounded primary-key batches and restores NOT NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rows examined per primary-key page while backfilling NULL search_text.
_BACKFILL_BATCH = 1000


def upgrade() -> None:
    """Drop the NOT NULL constraint on conversation_items.search_text."""
    # Batch mode: SQLite cannot alter nullability in place (rebuilds the
    # table); PostgreSQL/MySQL run a plain ALTER.
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.alter_column(
            "search_text",
            existing_type=sa.Text(),
            nullable=True,
        )


def _backfill_null_search_text() -> None:
    """Set search_text = '' on NULL rows in bounded primary-key pages."""
    bind = op.get_bind()
    last: tuple[object, ...] | None = None
    while True:
        if last is None:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, conversation_id, id, created_at, search_text IS NULL "
                    "FROM conversation_items "
                    "ORDER BY workspace_id, conversation_id, id, created_at LIMIT :lim"
                ),
                {"lim": _BACKFILL_BATCH},
            ).fetchall()
        else:
            rows = bind.execute(
                sa.text(
                    "SELECT workspace_id, conversation_id, id, created_at, search_text IS NULL "
                    "FROM conversation_items "
                    "WHERE workspace_id > :ws "
                    "OR (workspace_id = :ws AND conversation_id > :cid) "
                    "OR (workspace_id = :ws AND conversation_id = :cid AND id > :id) "
                    "OR (workspace_id = :ws AND conversation_id = :cid AND id = :id "
                    "AND created_at > :ca) "
                    "ORDER BY workspace_id, conversation_id, id, created_at LIMIT :lim"
                ),
                {
                    "ws": last[0],
                    "cid": last[1],
                    "id": last[2],
                    "ca": last[3],
                    "lim": _BACKFILL_BATCH,
                },
            ).fetchall()
        if not rows:
            break
        for workspace_id, conversation_id, item_id, created_at, missing in rows:
            if not missing:
                continue
            bind.execute(
                sa.text(
                    "UPDATE conversation_items SET search_text = '' "
                    "WHERE workspace_id = :ws AND conversation_id = :cid AND id = :id "
                    "AND created_at = :ca"
                ),
                {"ws": workspace_id, "cid": conversation_id, "id": item_id, "ca": created_at},
            )
        last = tuple(rows[-1][:4])
        if len(rows) < _BACKFILL_BATCH:
            break


def downgrade() -> None:
    """Restore NOT NULL after backfilling NULL rows with an empty string."""
    _backfill_null_search_text()
    with op.batch_alter_table("conversation_items") as batch_op:
        batch_op.alter_column(
            "search_text",
            existing_type=sa.Text(),
            nullable=False,
        )
