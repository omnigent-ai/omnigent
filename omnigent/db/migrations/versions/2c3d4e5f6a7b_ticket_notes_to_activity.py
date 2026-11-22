"""Preserve retired ticket fields as ordinary activity before removing their UI/API.

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a

Stop ticket writers before upgrading. Existing historical updates already retain
most notes; only missing snapshots become System-authored, notification-skipped
comments. Source columns, history, description and ticket timestamps are untouched.
Each read page and ticket import has its own bounded transaction. Stable import
IDs make an interrupted upgrade retryable. Downgrade retains all copied content
and the original columns; there is no destructive schema change.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16
from omnigent.db.envelope import EnvelopedJSON, EnvelopedText
from omnigent.entities.project_ticket_legacy import legacy_ticket_note_id, legacy_ticket_notes

revision: str = "2c3d4e5f6a7b"
down_revision: str | None = "1b2c3d4e5f6a"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_PAGE_SIZE = 100
_TICKETS = sa.table(
    "project_tickets",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("project_id", Uuid16()),
    sa.column("id", Uuid16()),
    sa.column("plan", EnvelopedText(16383)),
    sa.column("review_summary", EnvelopedText(8192)),
    sa.column("completion_summary", EnvelopedText(8192)),
    sa.column("comment_count", sa.Integer()),
    sa.column("next_event_position", sa.Integer()),
)
_EVENTS = sa.table(
    "project_ticket_events",
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("project_id", Uuid16()),
    sa.column("ticket_id", Uuid16()),
    sa.column("id", Uuid16()),
    sa.column("position", sa.Integer()),
    sa.column("kind", sa.SmallInteger()),
    sa.column("user_id", sa.String(128)),
    sa.column("session_id", Uuid16()),
    sa.column("agent_id", Uuid16()),
    sa.column("payload", EnvelopedJSON(16383)),
    sa.column("created_at", sa.BigInteger()),
    sa.column("notification_state", sa.SmallInteger()),
)


def _missing_notes(connection: sa.Connection, ticket: Mapping[str, Any]) -> dict[str, str]:
    missing = legacy_ticket_notes(ticket)
    if not missing:
        return missing
    scope = (
        _EVENTS.c.workspace_id == ticket["workspace_id"],
        _EVENTS.c.project_id == ticket["project_id"],
        _EVENTS.c.ticket_id == ticket["id"],
    )
    imported = {
        legacy_ticket_note_id(
            ticket["workspace_id"], ticket["project_id"], ticket["id"], field, text
        ): field
        for field, text in missing.items()
    }
    with connection.begin():
        existing = (
            connection.execute(
                sa.select(_EVENTS.c.id).where(*scope, _EVENTS.c.id.in_(imported)).limit(3)
            )
            .scalars()
            .all()
        )
    for event_id in existing:
        missing.pop(imported[event_id])
    query = (
        sa.select(_EVENTS.c.position, _EVENTS.c.id, _EVENTS.c.payload)
        .where(*scope, _EVENTS.c.kind == 1)
        .with_hint(_EVENTS, "FORCE INDEX (ix_project_ticket_events_kind)", "mysql")
        .order_by(_EVENTS.c.position.desc(), _EVENTS.c.id.desc())
    )
    before = None
    while missing:
        page_query = (
            query
            if before is None
            else query.where(sa.tuple_(_EVENTS.c.position, _EVENTS.c.id) < before)
        )
        with connection.begin():
            page = connection.execute(page_query.limit(_PAGE_SIZE)).all()
        for event in page:
            changes = event.payload.get("changes")
            if isinstance(changes, dict):
                for field, text in legacy_ticket_notes(changes).items():
                    if missing.get(field) == text:
                        del missing[field]
        if len(page) < _PAGE_SIZE:
            break
        before = (page[-1].position, page[-1].id)
    return missing


def _import_notes(
    connection: sa.Connection, snapshot: Mapping[str, Any], notes: dict[str, str]
) -> None:
    if not notes:
        return
    scope = (
        _TICKETS.c.workspace_id == snapshot["workspace_id"],
        _TICKETS.c.project_id == snapshot["project_id"],
        _TICKETS.c.id == snapshot["id"],
    )
    with connection.begin():
        ticket = (
            connection.execute(sa.select(_TICKETS).where(*scope).with_for_update())
            .mappings()
            .one()
        )
        if any(ticket[field] != snapshot[field] for field in notes):
            raise RuntimeError("Ticket notes changed during conversion; stop writers and retry")
        position = ticket["next_event_position"]
        for field, text in notes.items():
            connection.execute(
                _EVENTS.insert().values(
                    workspace_id=ticket["workspace_id"],
                    project_id=ticket["project_id"],
                    ticket_id=ticket["id"],
                    id=legacy_ticket_note_id(
                        ticket["workspace_id"], ticket["project_id"], ticket["id"], field, text
                    ),
                    position=position,
                    kind=2,
                    user_id=None,
                    session_id=None,
                    agent_id=None,
                    payload={"text": text, "imported": True},
                    created_at=int(time.time()),
                    notification_state=2,
                )
            )
            position += 1
        connection.execute(
            _TICKETS.update()
            .where(*scope)
            .values(
                next_event_position=position,
                comment_count=ticket["comment_count"] + len(notes),
            )
        )


def _backfill(connection: sa.Connection) -> None:
    key = sa.tuple_(_TICKETS.c.workspace_id, _TICKETS.c.project_id, _TICKETS.c.id)
    query = sa.select(_TICKETS).order_by(
        _TICKETS.c.workspace_id, _TICKETS.c.project_id, _TICKETS.c.id
    )
    after = None
    while True:
        page_query = query if after is None else query.where(key > after)
        with connection.begin():
            page = connection.execute(page_query.limit(_PAGE_SIZE)).mappings().all()
        if not page:
            return
        for row in page:
            ticket = {str(key): value for key, value in row.items()}
            _import_notes(connection, ticket, _missing_notes(connection, ticket))
        last = page[-1]
        after = (last["workspace_id"], last["project_id"], last["id"])


def upgrade() -> None:
    if op.get_context().as_sql:
        raise RuntimeError("Ticket note preservation requires an online migration")
    connection = op.get_bind()
    if connection.execute(sa.select(_TICKETS.c.id).limit(1)).first() is None:
        return
    cockroach = connection.dialect.name == "cockroachdb"
    if cockroach:
        connection.commit()
    with (
        nullcontext() if cockroach else op.get_context().autocommit_block(),
        connection.engine.connect().execution_options(
            isolation_level=connection.default_isolation_level
        ) as copy,
    ):
        _backfill(copy)
    if cockroach:
        connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def downgrade() -> None:
    """Ordinary comments and their untouched source columns remain readable on rollback."""
