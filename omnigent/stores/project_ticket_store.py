"""Project-private tickets, atomic edits/activity, and a bounded notification outbox."""

from __future__ import annotations

import builtins
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlProject,
    SqlProjectTicket,
    SqlProjectTicketEvent,
    SqlProjectTicketSession,
    current_workspace_id,
)
from omnigent.db.keyset import keyset_after
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities.project_ticket import (
    EVENT_CODES,
    NOTIFICATION_PENDING,
    NOTIFICATION_QUEUED,
    NOTIFICATION_SKIPPED,
    STATUS_CODES,
    TICKET_PAGE_MAX,
    CommentOnTicket,
    CreateTicket,
    TicketActor,
    UpdateTicket,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


def _identifier(value: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise OmnigentError("Ticket or project not found", code=ErrorCode.NOT_FOUND) from exc


def _ticket(row: SqlProjectTicket) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "title": row.title,
        "description": row.description,
        "status": next(key for key, value in STATUS_CODES.items() if value == row.status),
        "tags": row.tags,
        "completion_summary": row.completion_summary,
        "latest_update": row.latest_update,
        "comment_count": row.comment_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _event(row: SqlProjectTicketEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "ticket_id": row.ticket_id,
        "project_id": row.project_id,
        "position": row.position,
        "kind": next(key for key, value in EVENT_CODES.items() if value == row.kind),
        "user_id": row.user_id,
        "session_id": row.session_id,
        "agent_id": row.agent_id,
        "payload": row.payload,
        "created_at": row.created_at,
        "notification_pending": row.notification_state == NOTIFICATION_PENDING,
    }


class ProjectTicketStore:
    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._dialect_name = engine.dialect.name
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_tickets"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_tickets", immediate=True
        )

    @staticmethod
    def _project(
        session: Session, project_id: str, user_id: str | None, *, write: bool = False
    ) -> SqlProject:
        stmt = select(SqlProject).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.id == _identifier(project_id),
        )
        project = session.scalar(stmt.with_for_update() if write else stmt)
        if project is None or project.user_id != user_id:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if write:
            require_active_account(session, user_id)
        return project

    @staticmethod
    def _row(
        session: Session, project_id: str, ticket_id: str, *, write: bool = False
    ) -> SqlProjectTicket:
        stmt = select(SqlProjectTicket).where(
            SqlProjectTicket.workspace_id == current_workspace_id(),
            SqlProjectTicket.project_id == _identifier(project_id),
            SqlProjectTicket.id == _identifier(ticket_id),
        )
        row = session.scalar(stmt.with_for_update() if write else stmt)
        if row is None:
            raise OmnigentError("Ticket not found", code=ErrorCode.NOT_FOUND)
        return row

    @staticmethod
    def _append(
        session: Session,
        project: SqlProject,
        ticket: SqlProjectTicket,
        actor: TicketActor,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            SqlProjectTicketEvent(
                workspace_id=project.workspace_id,
                project_id=project.id,
                ticket_id=ticket.id,
                id=uuid.uuid4().hex,
                position=ticket.next_event_position,
                kind=EVENT_CODES[kind],
                user_id=actor.user_id,
                session_id=actor.session_id,
                agent_id=actor.agent_id,
                payload=payload,
                created_at=now_epoch(),
                notification_state=NOTIFICATION_SKIPPED
                if actor.session_id is not None
                and actor.session_id == project.orchestrator_session_id
                else NOTIFICATION_PENDING,
            )
        )
        ticket.next_event_position += 1
        ticket.updated_at = now_epoch()

    def create(self, project_id: str, body: CreateTicket, actor: TicketActor) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            now = now_epoch()
            row = SqlProjectTicket(
                workspace_id=current_workspace_id(),
                project_id=project.id,
                id=uuid.uuid4().hex,
                title=body.title,
                description=body.description,
                status=STATUS_CODES["open"],
                tags=[tag.model_dump(exclude_none=True) for tag in body.tags],
                completion_summary=None,
                latest_update="Created the ticket",
                comment_count=0,
                next_event_position=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            self._append(
                session, project, row, actor, "created", body.model_dump(exclude_none=True)
            )
            return _ticket(row)

        return run_write_transaction(self._write, "create_ticket", write)

    def get(self, project_id: str, ticket_id: str, *, user_id: str | None) -> dict[str, Any]:
        with self._session("get_ticket") as session:
            self._project(session, project_id, user_id)
            return _ticket(self._row(session, project_id, ticket_id))

    def list(
        self,
        project_id: str,
        *,
        user_id: str | None,
        status: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, TICKET_PAGE_MAX))
        with self._session("list_tickets") as session:
            self._project(session, project_id, user_id)
            stmt = select(SqlProjectTicket).where(
                SqlProjectTicket.workspace_id == current_workspace_id(),
                SqlProjectTicket.project_id == _identifier(project_id),
            )
            if status is not None:
                stmt = stmt.where(SqlProjectTicket.status == STATUS_CODES[status])
            if after is not None:
                cursor = self._row(session, project_id, after)
                stmt = stmt.where(
                    keyset_after(
                        self._dialect_name,
                        (SqlProjectTicket.created_at, SqlProjectTicket.id),
                        (cursor.created_at, cursor.id),
                    )
                )
            index = (
                "ix_project_tickets_status" if status is not None else "ix_project_tickets_created"
            )
            rows = session.scalars(
                stmt.with_hint(SqlProjectTicket, f"FORCE INDEX ({index})", "mysql")
                .order_by(SqlProjectTicket.created_at, SqlProjectTicket.id)
                .limit(limit + 1)
            ).all()
            return {
                "data": [_ticket(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "last_id": rows[min(limit, len(rows)) - 1].id if rows else None,
            }

    def update(
        self, project_id: str, ticket_id: str, body: UpdateTicket, actor: TicketActor
    ) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)

        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            row = self._row(session, project_id, ticket_id, write=True)
            if not changes:
                return _ticket(row)
            for key, value in changes.items():
                setattr(row, key, STATUS_CODES[value] if key == "status" else value)
            if body.status is not None and body.status != "done":
                row.completion_summary = None
            row.latest_update = (
                body.completion_summary
                or (
                    f"Moved to {body.status.replace('_', ' ')}"
                    if body.status
                    else "Updated the ticket"
                )
            )[:300]
            self._append(session, project, row, actor, "updated", {"changes": changes})
            return _ticket(row)

        return run_write_transaction(self._write, "update_ticket", write)

    def comment(
        self, project_id: str, ticket_id: str, body: CommentOnTicket, actor: TicketActor
    ) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            row = self._row(session, project_id, ticket_id, write=True)
            self._append(session, project, row, actor, body.kind, {"text": body.text})
            for output in body.outputs:
                self._append(session, project, row, actor, "output", output.model_dump())
            row.comment_count += 1
            row.latest_update = body.text[:300]
            return _ticket(row)

        return run_write_transaction(self._write, "comment_on_ticket", write)

    def events(
        self,
        project_id: str,
        ticket_id: str,
        *,
        user_id: str | None,
        after: int = -1,
        limit: int = 50,
        kind: str | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, TICKET_PAGE_MAX))
        with self._session("list_ticket_events") as session:
            self._project(session, project_id, user_id)
            self._row(session, project_id, ticket_id)
            stmt = select(SqlProjectTicketEvent).where(
                SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                SqlProjectTicketEvent.project_id == _identifier(project_id),
                SqlProjectTicketEvent.ticket_id == _identifier(ticket_id),
                SqlProjectTicketEvent.position > after,
            )
            if kind is not None:
                stmt = stmt.where(SqlProjectTicketEvent.kind == EVENT_CODES[kind])
            index = (
                "ix_project_ticket_events_kind" if kind else "ix_project_ticket_events_position"
            )
            rows = session.scalars(
                stmt.with_hint(SqlProjectTicketEvent, f"FORCE INDEX ({index})", "mysql")
                .order_by(SqlProjectTicketEvent.position, SqlProjectTicketEvent.id)
                .limit(limit + 1)
            ).all()
            return {
                "data": [_event(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "last_position": rows[min(limit, len(rows)) - 1].position if rows else after,
            }

    def link_session(
        self, project_id: str, ticket_id: str, session_id: str, action: str, actor: TicketActor
    ) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            ticket = self._row(session, project_id, ticket_id, write=True)
            key = (current_workspace_id(), project.id, ticket.id, _identifier(session_id))
            link = session.get(SqlProjectTicketSession, key)
            if action == "attach" and link is None:
                session.add(
                    SqlProjectTicketSession(
                        workspace_id=key[0],
                        project_id=key[1],
                        ticket_id=key[2],
                        session_id=key[3],
                        created_at=now_epoch(),
                    )
                )
            elif action == "remove" and link is not None:
                session.delete(link)
            else:
                return _ticket(ticket)
            self._append(
                session,
                project,
                ticket,
                actor,
                "updated",
                {"session_id": session_id, "action": action},
            )
            return _ticket(ticket)

        return run_write_transaction(self._write, "link_ticket_session", write)

    def sessions(
        self,
        project_id: str,
        ticket_id: str,
        *,
        user_id: str | None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, TICKET_PAGE_MAX))
        with self._session("list_ticket_sessions") as session:
            self._project(session, project_id, user_id)
            self._row(session, project_id, ticket_id)
            stmt = select(SqlProjectTicketSession).where(
                SqlProjectTicketSession.workspace_id == current_workspace_id(),
                SqlProjectTicketSession.project_id == _identifier(project_id),
                SqlProjectTicketSession.ticket_id == _identifier(ticket_id),
            )
            if after is not None:
                stmt = stmt.where(SqlProjectTicketSession.session_id > _identifier(after))
            rows = session.scalars(
                stmt.order_by(SqlProjectTicketSession.session_id).limit(limit + 1)
            ).all()
            return {
                "data": [
                    {"session_id": row.session_id, "created_at": row.created_at}
                    for row in rows[:limit]
                ],
                "has_more": len(rows) > limit,
                "last_id": rows[min(limit, len(rows)) - 1].session_id if rows else None,
            }

    def pending_notifications(
        self, *, after: tuple[int, str, str, int, str] | None = None, limit: int = 32
    ) -> builtins.list[dict[str, Any]]:
        """Privileged delivery worker: a bounded cross-workspace outbox scan."""
        with self._session("pending_ticket_notifications") as session:
            columns = (
                SqlProjectTicketEvent.workspace_id,
                SqlProjectTicketEvent.project_id,
                SqlProjectTicketEvent.ticket_id,
                SqlProjectTicketEvent.position,
                SqlProjectTicketEvent.id,
            )
            stmt = select(SqlProjectTicketEvent).where(
                SqlProjectTicketEvent.notification_state == NOTIFICATION_PENDING
            )
            if after is not None:
                stmt = stmt.where(keyset_after(self._dialect_name, columns, after))
            rows = session.scalars(
                stmt.with_hint(
                    SqlProjectTicketEvent,
                    "FORCE INDEX (ix_project_ticket_events_notification)",
                    "mysql",
                )
                .order_by(*columns)
                .limit(min(limit, 32))
            ).all()
            return [{**_event(row), "workspace_id": row.workspace_id} for row in rows]

    def mark_notified(self, project_id: str, ticket_id: str, event_id: str) -> None:
        with self._write("mark_ticket_notification_queued") as session:
            event = session.get(
                SqlProjectTicketEvent, (current_workspace_id(), project_id, ticket_id, event_id)
            )
            if event is not None:
                event.notification_state = NOTIFICATION_QUEUED
