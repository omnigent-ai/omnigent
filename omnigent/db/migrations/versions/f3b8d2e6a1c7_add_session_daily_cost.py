"""Add a per-session, per-UTC-day cost rollup.

Revision ID: f3b8d2e6a1c7
Revises: d4a8e1c7b2f6

Additive: a new table that older binaries ignore. Existing sessions are
backfilled with their recorded cumulative cost on their last-activity day, so
windowed totals start from the best available attribution. Downgrade drops it.
"""

import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision: str = "f3b8d2e6a1c7"
down_revision: str | None = "d4a8e1c7b2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _own_cost(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        value = json.loads(raw).get("total_cost_usd")
        cost = float(value)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    return cost if cost > 0 else 0.0


def _backfill(bind: sa.Connection, tables: list[str]) -> None:
    metadata = sa.table(
        "omnigent_conversation_metadata",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("session_usage", CompressedText()),
    )
    conversations = sa.table(
        "conversations",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("updated_at", sa.Integer()),
    )
    now = int(time.time())
    if "conversations" in tables:
        query = sa.select(
            metadata.c.workspace_id,
            metadata.c.id,
            metadata.c.session_usage,
            conversations.c.updated_at,
        ).select_from(
            metadata.outerjoin(
                conversations,
                sa.and_(
                    conversations.c.workspace_id == metadata.c.workspace_id,
                    conversations.c.id == metadata.c.id,
                ),
            )
        )
    else:
        query = sa.select(
            metadata.c.workspace_id,
            metadata.c.id,
            metadata.c.session_usage,
            sa.literal(None).label("updated_at"),
        )
    rows = []
    for workspace_id, conversation_id, usage, updated_at in bind.execute(query):
        cost = _own_cost(usage)
        if cost <= 0:
            continue
        day = datetime.fromtimestamp(updated_at or now, tz=UTC).date().isoformat()
        rows.append(
            {
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "day_utc": day,
                "cost_usd": cost,
                "updated_at": now,
            }
        )
    if rows:
        target = sa.table(
            "session_daily_cost",
            sa.column("workspace_id", sa.BigInteger()),
            sa.column("conversation_id", Uuid16()),
            sa.column("day_utc", sa.String(10)),
            sa.column("cost_usd", sa.Float()),
            sa.column("updated_at", sa.Integer()),
        )
        op.bulk_insert(target, rows)


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "session_daily_cost" in tables:
        return
    op.create_table(
        "session_daily_cost",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("day_utc", sa.String(10), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "day_utc"),
    )
    if "omnigent_conversation_metadata" in tables:
        _backfill(bind, tables)


def downgrade() -> None:
    op.drop_table("session_daily_cost")
