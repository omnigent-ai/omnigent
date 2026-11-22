"""Remove the retired ticket response field and its structured history data.

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b

Deploy the application removal first and stop old ticket writers before upgrading.
Each primary-key page commits independently; interrupted cleanup and column removal
can be retried. Only field-only updates are deleted. Other history, authored text,
links, timestamps and event counters are preserved, including gaps in positions.
Downgrade restores an empty nullable column, not the deleted values or events.
"""

from __future__ import annotations

from contextlib import nullcontext

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16
from omnigent.db.envelope import EnvelopedJSON

revision: str = "3d4e5f6a7b8c"
down_revision: str | None = "2c3d4e5f6a7b"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_PAGE_SIZE = 100
_FIELD = "lead_session_id"
_EVENTS = sa.table(
    "project_ticket_events",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("project_id", Uuid16()),
    sa.column("ticket_id", Uuid16()),
    sa.column("id", Uuid16()),
    sa.column("kind", sa.SmallInteger()),
    sa.column("payload", EnvelopedJSON(16383)),
)


def _remove_history_field(connection: sa.Connection) -> None:
    columns = (
        _EVENTS.c.workspace_id,
        _EVENTS.c.project_id,
        _EVENTS.c.ticket_id,
        _EVENTS.c.id,
    )
    key = sa.tuple_(*columns)
    query = (
        sa.select(_EVENTS).with_hint(_EVENTS, "FORCE INDEX (PRIMARY)", "mysql").order_by(*columns)
    )
    after = None
    while True:
        page_query = query if after is None else query.where(key > after)
        with connection.begin():
            page = connection.execute(page_query.limit(_PAGE_SIZE)).mappings().all()
            if not page:
                return
            for row in page:
                payload = dict(row["payload"])
                changed = _FIELD in payload
                payload.pop(_FIELD, None)
                changes = payload.get("changes")
                if isinstance(changes, dict) and _FIELD in changes:
                    changes = {name: value for name, value in changes.items() if name != _FIELD}
                    if changes:
                        payload["changes"] = changes
                    else:
                        del payload["changes"]
                    changed = True
                if not changed:
                    continue
                scope = [column == row[column.name] for column in columns]
                if row["kind"] == 1 and not payload:
                    connection.execute(_EVENTS.delete().where(*scope))
                else:
                    connection.execute(_EVENTS.update().where(*scope).values(payload=payload))
            after = tuple(page[-1][column.name] for column in columns)


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Ticket response-field removal requires an online migration")
    connection = op.get_bind()
    cockroach = connection.dialect.name == "cockroachdb"
    if cockroach:
        connection.commit()
    with (
        nullcontext() if cockroach else op.get_context().autocommit_block(),
        connection.engine.connect().execution_options(
            isolation_level=connection.default_isolation_level
        ) as cleanup,
    ):
        _remove_history_field(cleanup)
    if cockroach:
        connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
    if _FIELD in {
        column["name"] for column in sa.inspect(connection).get_columns("project_tickets")
    }:
        op.drop_column("project_tickets", _FIELD)


def downgrade() -> None:
    op.add_column("project_tickets", sa.Column(_FIELD, Uuid16(), nullable=True))
