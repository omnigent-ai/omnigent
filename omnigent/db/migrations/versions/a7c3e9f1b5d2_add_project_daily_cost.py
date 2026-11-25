"""Add a per-project spend rollup, ``project_daily_cost``.

Revision ID: a7c3e9f1b5d2
Revises: e7a3c5b9d2f4

Additive: a new table that older binaries ignore. It is backfilled from
``session_daily_cost`` with the same attribution the project Summary used
before: a session counts for its own project and its root's, under its root,
in integer micro-dollars. Downgrade drops it; the Summary then reads
``session_daily_cost`` directly again.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a7c3e9f1b5d2"
down_revision: str | None = "e7a3c5b9d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 1000


def _backfill(bind: sa.Connection) -> None:
    costs = sa.table(
        "session_daily_cost",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("conversation_id", Uuid16()),
        sa.column("day_utc", sa.String(10)),
        sa.column("cost_usd", sa.Float()),
    )
    conversations = sa.table(
        "conversations",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("root_conversation_id", Uuid16()),
    )
    own = sa.table(
        "omnigent_conversation_metadata",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("project_id", Uuid16()),
    ).alias("own_meta")
    root = sa.table(
        "omnigent_conversation_metadata",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("project_id", Uuid16()),
    ).alias("root_meta")
    query = (
        sa.select(
            costs.c.workspace_id,
            conversations.c.root_conversation_id,
            costs.c.day_utc,
            costs.c.cost_usd,
            own.c.project_id,
            root.c.project_id,
        )
        .select_from(
            costs.join(
                conversations,
                sa.and_(
                    conversations.c.workspace_id == costs.c.workspace_id,
                    conversations.c.id == costs.c.conversation_id,
                ),
            )
            .outerjoin(
                own,
                sa.and_(
                    own.c.workspace_id == conversations.c.workspace_id,
                    own.c.id == conversations.c.id,
                ),
            )
            .outerjoin(
                root,
                sa.and_(
                    root.c.workspace_id == conversations.c.workspace_id,
                    root.c.id == conversations.c.root_conversation_id,
                ),
            )
        )
        .where(sa.func.length(costs.c.day_utc) == 10)
        .where(sa.or_(own.c.project_id.is_not(None), root.c.project_id.is_not(None)))
    )
    micros: dict[tuple[int, str, str, str], int] = defaultdict(int)
    for workspace_id, root_id, day, cost, own_project, root_project in bind.execute(query):
        if root_id is None or not cost or cost <= 0:
            continue
        for project_id in {own_project, root_project} - {None}:
            micros[(workspace_id, project_id, day, root_id)] += round(cost * 1_000_000)
    target = sa.table(
        "project_daily_cost",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("project_id", Uuid16()),
        sa.column("day_utc", sa.String(10)),
        sa.column("root_id", Uuid16()),
        sa.column("cost_micros", sa.BigInteger()),
        sa.column("updated_at", sa.Integer()),
    )
    now = int(time.time())
    rows = [
        {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "day_utc": day,
            "root_id": root_id,
            "cost_micros": value,
            "updated_at": now,
        }
        for (workspace_id, project_id, day, root_id), value in micros.items()
        if value
    ]
    for start in range(0, len(rows), _BATCH):
        op.bulk_insert(target, rows[start : start + _BATCH])


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    if "project_daily_cost" in tables:
        return
    op.create_table(
        "project_daily_cost",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("day_utc", sa.String(10), nullable=False),
        sa.Column("root_id", Uuid16(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "day_utc", "root_id"),
    )
    if {"session_daily_cost", "conversations", "omnigent_conversation_metadata"} <= set(tables):
        _backfill(bind)


def downgrade() -> None:
    op.drop_table("project_daily_cost")
