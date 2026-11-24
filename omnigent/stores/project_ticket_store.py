"""Project-private tickets, atomic edits/activity, and a bounded notification outbox."""

from __future__ import annotations

import builtins
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlProject,
    SqlProjectTicket,
    SqlProjectTicketEvent,
    SqlProjectTicketLink,
    SqlProjectTicketRead,
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
    LEGACY_STATUS_CODES,
    NOTIFICATION_AWAITING_WORKER,
    NOTIFICATION_DIGEST,
    NOTIFICATION_ESCALATED,
    NOTIFICATION_PENDING,
    NOTIFICATION_QUEUED,
    NOTIFICATION_SKIPPED,
    STAGE_EDIT_TICKET_MAX,
    TICKET_PAGE_MAX,
    CommentOnTicket,
    CreateTicket,
    MarkTicketSeen,
    TicketActor,
    TicketStage,
    TicketStages,
    UpdateTicket,
    UpdateTicketStages,
    default_ticket_stages,
)
from omnigent.entities.project_ticket_legacy import legacy_ticket_notes
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

TICKET_ACTIVITY_SESSION_MAX = 100
TICKET_SUMMARY_SCAN_MAX = 1000
# Outputs ride along with a comment, so only these kinds count as new activity.
NEW_COMMENT_KINDS = (EVENT_CODES["comment"], EVENT_CODES["execution"])
NEW_UPDATE_KINDS = (EVENT_CODES["created"], EVENT_CODES["updated"])


@dataclass(frozen=True)
class TicketActivitySessions:
    session_ids: list[str]
    has_more: bool


def _identifier(value: str) -> str:
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError as exc:
        raise OmnigentError("Ticket or project not found", code=ErrorCode.NOT_FOUND) from exc


def _stage_id(row: SqlProjectTicket) -> str:
    return row.stage_id or "open"


def _stages(project: SqlProject) -> TicketStages:
    return (
        TicketStages.model_validate(project.ticket_stages)
        if project.ticket_stages is not None
        else default_ticket_stages()
    )


def _stage(configuration: TicketStages, stage_id: str) -> TicketStage:
    for stage in configuration.stages:
        if stage.id == stage_id:
            return stage
    raise OmnigentError("Stage does not belong to this project", code=ErrorCode.INVALID_INPUT)


def _stage_snapshot(stage: TicketStage) -> dict[str, Any]:
    return stage.model_dump(exclude={"instructions"}, exclude_none=True)


def _ticket(row: SqlProjectTicket) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "title": row.title,
        "description": row.description,
        "stage_id": _stage_id(row),
        "labels": row.labels,
        "latest_update": row.latest_update,
        "comment_count": row.comment_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _orchestrator_session_id(project: SqlProject) -> str | None:
    return project.main_session_id or project.legacy_orchestrator_session_id


def _author_audience(
    session_id: str | None, agent_id: str | None, orchestrator_session_id: str | None
) -> str:
    """People and the orchestrator write for the human; worker sessions write agent notes."""
    if session_id is None and agent_id is None:
        return "human"
    if session_id is not None and session_id == orchestrator_session_id:
        return "human"
    return "agents"


def _note_audience(
    row: SqlProjectTicketEvent, payload: dict[str, Any], orchestrator_session_id: str | None
) -> str:
    audience = payload.get("audience")
    if audience in ("human", "agents"):
        return audience
    return _author_audience(row.session_id, row.agent_id, orchestrator_session_id)


NEEDS_HUMAN_LABEL = "needs-human"
_UNDELIVERED = (NOTIFICATION_PENDING, NOTIFICATION_DIGEST, NOTIFICATION_ESCALATED)
_URGENT = (NOTIFICATION_PENDING, NOTIFICATION_ESCALATED)
# Worker activity that counts as answering a person's comment on its ticket.
_WORKER_REPLY_KINDS = ("comment", "execution", "updated")


def _has_needs_human(labels: Any) -> bool:
    return isinstance(labels, list) and any(
        isinstance(label, dict) and label.get("key") == NEEDS_HUMAN_LABEL for label in labels
    )


@dataclass
class _HumanAsk:
    """A span during which a ticket carried needs-human, and the session that added it."""

    start: int
    end: int | None
    session_id: str | None

    def covers(self, session_id: str | None, position: int) -> bool:
        return (
            session_id is not None
            and session_id == self.session_id
            and position > self.start
            and (self.end is None or position < self.end)
        )


def _event_labels(row: SqlProjectTicketEvent) -> Any:
    payload = row.payload
    if row.kind == EVENT_CODES["created"]:
        return payload.get("labels", payload.get("tags"))
    changes = payload.get("changes")
    if not isinstance(changes, dict):
        return None
    return changes.get("labels", changes.get("tags"))


def _human_asks(rows: builtins.list[SqlProjectTicketEvent]) -> builtins.list[_HumanAsk]:
    """Replay label changes, in position order, into needs-human spans."""
    asks: builtins.list[_HumanAsk] = []
    for row in rows:
        labels = _event_labels(row)
        if labels is None:
            continue
        active = asks[-1] if asks and asks[-1].end is None else None
        if _has_needs_human(labels) and active is None:
            asks.append(_HumanAsk(row.position, None, row.session_id))
        elif not _has_needs_human(labels) and active is not None:
            active.end = row.position
    return asks


def _labeled_event(
    row: SqlProjectTicketEvent,
    orchestrator_session_id: str | None,
    *,
    asks_human: bool = False,
) -> dict[str, Any]:
    """An event whose comment payload always says who it is for, including older comments."""
    event = _event(row)
    if row.kind in NEW_COMMENT_KINDS:
        payload = event["payload"]
        event["payload"] = {
            **payload,
            "audience": "human"
            if asks_human
            else _note_audience(row, payload, orchestrator_session_id),
        }
    return event


def _wakes_orchestrator(
    kind: str, payload: dict[str, Any], actor: TicketActor, *, needs_human_changed: bool
) -> bool:
    """Whether an event needs an immediate orchestrator turn rather than the next digest."""
    if actor.session_id is None and actor.agent_id is None:
        return True
    if needs_human_changed:
        return True
    if kind == "created":
        return _has_needs_human(payload.get("labels"))
    if kind == "updated":
        changes = payload.get("changes")
        return isinstance(changes, dict) and "stage_id" in changes
    if kind in ("comment", "execution"):
        return payload.get("audience") == "human" or payload.get("final") is True
    return False


_CODE_OR_LINK = re.compile(r"`[^`]*`|https?://\S+|\S*/\S+")
_IDENTIFIER = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.IGNORECASE)
_PROSE_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]+")
FINAL_UPDATE_MIN_CHARS = 160
FINAL_UPDATE_MIN_PROSE_WORDS = 25


def final_update_problem(text: str) -> str | None:
    """Why a final update doesn't read as written for a person, or None when it does."""
    if len(text.strip()) < FINAL_UPDATE_MIN_CHARS:
        return "too short to cover what was delivered, what it means, caveats and evidence"
    prose = _IDENTIFIER.sub(" ", _CODE_OR_LINK.sub(" ", text))
    words = len(_PROSE_WORD.findall(prose))
    if words < FINAL_UPDATE_MIN_PROSE_WORDS:
        return "mostly IDs, hashes, paths or code rather than plain-language sentences"
    return None


def _require_human_final(text: str) -> None:
    problem = final_update_problem(text)
    if problem is not None:
        raise OmnigentError(
            "A final update is for the human, so it was not posted: it reads as "
            f"{problem}. Write a few plain-language sentences saying what was delivered or "
            "decided, what it means for them, any caveats or follow-ups, and where the "
            "evidence is; keep IDs and SHAs to what they need.",
            code=ErrorCode.INVALID_INPUT,
        )


def _require_complete(project: SqlProject, ticket: SqlProjectTicket) -> None:
    if not _stage(_stages(project), _stage_id(ticket)).is_complete:
        raise OmnigentError(
            "A final update is only for a ticket in its completed stage; "
            "move it there in the same edit or post an ordinary comment",
            code=ErrorCode.INVALID_INPUT,
        )


def _reopens(row: SqlProjectTicketEvent) -> bool:
    """A stage change that left the ticket outside a completed stage."""
    stage = row.payload.get("stage")
    return (
        row.kind == EVENT_CODES["updated"]
        and isinstance(stage, dict)
        and not stage.get("is_complete", False)
    )


def _event(row: SqlProjectTicketEvent) -> dict[str, Any]:
    payload = dict(row.payload)
    # Project historical field names without changing the stored event.
    if row.kind == EVENT_CODES["created"] and "tags" in payload:
        payload["labels"] = payload.pop("tags")
    changes = payload.get("changes")
    if row.kind == EVENT_CODES["updated"] and isinstance(changes, dict) and "tags" in changes:
        changes = dict(changes)
        changes["labels"] = changes.pop("tags")
        payload["changes"] = changes
    if isinstance(changes, dict) and "text" not in payload:
        notes = legacy_ticket_notes(changes)
        if notes:
            payload = {**payload, "text": "\n\n".join(notes.values())}
    return {
        "id": row.id,
        "ticket_id": row.ticket_id,
        "project_id": row.project_id,
        "position": row.position,
        "kind": next(key for key, value in EVENT_CODES.items() if value == row.kind),
        "user_id": row.user_id,
        "session_id": row.session_id,
        "agent_id": row.agent_id,
        "payload": payload,
        "created_at": row.created_at,
        "notification_pending": row.notification_state
        in (*_UNDELIVERED, NOTIFICATION_AWAITING_WORKER),
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
        *,
        needs_human_changed: bool = False,
    ) -> None:
        own = actor.session_id is not None and actor.session_id == _orchestrator_session_id(
            project
        )
        answered = False
        if not own and actor.session_id is not None and kind in _WORKER_REPLY_KINDS:
            # A worker answered: the person's comment now only belongs in the orchestrator digest.
            result = session.execute(
                update(SqlProjectTicketEvent)
                .where(
                    SqlProjectTicketEvent.workspace_id == project.workspace_id,
                    SqlProjectTicketEvent.project_id == project.id,
                    SqlProjectTicketEvent.ticket_id == ticket.id,
                    SqlProjectTicketEvent.notification_state == NOTIFICATION_AWAITING_WORKER,
                )
                .values(notification_state=NOTIFICATION_DIGEST)
            )
            answered = bool(result.rowcount)
        if own:
            state = NOTIFICATION_SKIPPED
        elif (
            answered
            and kind in ("comment", "execution")
            and payload.get("final") is not True
            and not needs_human_changed
        ):
            # The worker's answer to a person is human-facing but needs no orchestrator turn.
            state = NOTIFICATION_DIGEST
        elif _wakes_orchestrator(kind, payload, actor, needs_human_changed=needs_human_changed):
            state = NOTIFICATION_PENDING
        else:
            state = NOTIFICATION_DIGEST
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
                notification_state=state,
            )
        )
        ticket.next_event_position += 1
        ticket.updated_at = now_epoch()

    def require_project(self, project_id: str, *, user_id: str | None) -> None:
        """Raise NOT_FOUND unless ``user_id`` owns the project."""
        with self._session("require_project_access") as session:
            self._project(session, project_id, user_id)

    @classmethod
    def _human_asks(cls, session: Session, ticket: SqlProjectTicket) -> builtins.list[_HumanAsk]:
        return cls._human_asks_for(session, ticket, ticket.id)

    @staticmethod
    def _human_asks_for(
        session: Session, owner: SqlProject | SqlProjectTicket, ticket_id: str
    ) -> builtins.list[_HumanAsk]:
        project_id = owner.id if isinstance(owner, SqlProject) else owner.project_id
        rows = session.scalars(
            select(SqlProjectTicketEvent)
            .where(
                SqlProjectTicketEvent.workspace_id == owner.workspace_id,
                SqlProjectTicketEvent.project_id == project_id,
                SqlProjectTicketEvent.ticket_id == ticket_id,
                SqlProjectTicketEvent.kind.in_(NEW_UPDATE_KINDS),
            )
            .order_by(SqlProjectTicketEvent.position)
        ).all()
        return _human_asks(builtins.list(rows))

    def _comment_audience(
        self,
        session: Session,
        project: SqlProject,
        ticket: SqlProjectTicket,
        actor: TicketActor,
        requested: str | None,
        *,
        adds_needs_human: bool = False,
    ) -> str:
        if requested is not None:
            return requested
        audience = _author_audience(
            actor.session_id, actor.agent_id, _orchestrator_session_id(project)
        )
        if audience == "human" or adds_needs_human:
            return "human"
        # A worker answering a person's comment on its ticket is talking to that person.
        if session.scalar(
            select(SqlProjectTicketEvent.id)
            .where(
                SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
                SqlProjectTicketEvent.project_id == ticket.project_id,
                SqlProjectTicketEvent.ticket_id == ticket.id,
                SqlProjectTicketEvent.notification_state == NOTIFICATION_AWAITING_WORKER,
            )
            .limit(1)
        ):
            return "human"
        # A worker that raised needs-human is talking to the human until it is cleared.
        if _has_needs_human(ticket.labels):
            asks = self._human_asks(session, ticket)
            if asks and asks[-1].covers(actor.session_id, ticket.next_event_position):
                return "human"
        return audience

    def create(self, project_id: str, body: CreateTicket, actor: TicketActor) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            configuration = _stages(project)
            stage = configuration.stages[0]
            now = now_epoch()
            row = SqlProjectTicket(
                workspace_id=current_workspace_id(),
                project_id=project.id,
                id=uuid.uuid4().hex,
                title=body.title,
                description=body.description,
                status=LEGACY_STATUS_CODES.get(stage.id, 0),
                stage_id=stage.id,
                labels=[label.model_dump(exclude_none=True) for label in body.labels],
                latest_update="Created the ticket",
                comment_count=0,
                next_event_position=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            self._append(
                session,
                project,
                row,
                actor,
                "created",
                {**body.model_dump(exclude_none=True), "stage": _stage_snapshot(stage)},
            )
            return _ticket(row)

        return run_write_transaction(self._write, "create_ticket", write)

    def get(self, project_id: str, ticket_id: str, *, user_id: str | None) -> dict[str, Any]:
        with self._session("get_ticket") as session:
            project = self._project(session, project_id, user_id)
            row = self._row(session, project_id, ticket_id)
            complete = _stage(_stages(project), _stage_id(row)).is_complete
            final_update = self._final_update(session, row) if complete else None
            return {
                **_ticket(row),
                "final_update": final_update,
                "needs_final_update": complete and final_update is None,
            }

    @staticmethod
    def _final_update(session: Session, ticket: SqlProjectTicket) -> dict[str, Any] | None:
        """The latest final update since the ticket last entered its completed stage."""
        # Payloads are encrypted, so replay this ticket's edits and comments here.
        rows = session.scalars(
            select(SqlProjectTicketEvent)
            .where(
                SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
                SqlProjectTicketEvent.project_id == ticket.project_id,
                SqlProjectTicketEvent.ticket_id == ticket.id,
                SqlProjectTicketEvent.kind.in_((*NEW_UPDATE_KINDS, *NEW_COMMENT_KINDS)),
            )
            .order_by(SqlProjectTicketEvent.position.desc())
        )
        for row in rows:
            if _reopens(row):
                return None
            if row.kind in NEW_COMMENT_KINDS and row.payload.get("final") is True:
                return {
                    "event_id": row.id,
                    "position": row.position,
                    "text": row.payload.get("text", ""),
                    "user_id": row.user_id,
                    "session_id": row.session_id,
                    "created_at": row.created_at,
                }
        return None

    def list(
        self,
        project_id: str,
        *,
        user_id: str | None,
        stage_id: str | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, TICKET_PAGE_MAX))
        with self._session("list_tickets") as session:
            project = self._project(session, project_id, user_id)
            configuration = _stages(project)
            if stage_id is not None:
                _stage(configuration, stage_id)
            rows = self._stage_rows(
                session,
                project,
                stage_id,
                after=self._row(session, project_id, after) if after is not None else None,
                limit=limit + 1,
            )
            return {
                **configuration.model_dump(),
                "data": [_ticket(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "last_id": rows[min(limit, len(rows)) - 1].id if rows else None,
            }

    def summary(self, project_id: str, *, user_id: str | None) -> dict[str, Any]:
        with self._session("ticket_response_summary") as session:
            project = self._project(session, project_id, user_id)
            # Labels are encrypted; bound the read to the newest tickets and report partial counts.
            labels = session.scalars(
                select(SqlProjectTicket.labels)
                .where(
                    SqlProjectTicket.workspace_id == project.workspace_id,
                    SqlProjectTicket.project_id == project.id,
                )
                .order_by(SqlProjectTicket.created_at.desc(), SqlProjectTicket.id.desc())
                .limit(TICKET_SUMMARY_SCAN_MAX + 1)
            ).all()
            return {
                "needs_response_count": sum(
                    any(label.get("key") == "needs-human" for label in ticket_labels)
                    for ticket_labels in labels[:TICKET_SUMMARY_SCAN_MAX]
                ),
                "is_complete": len(labels) <= TICKET_SUMMARY_SCAN_MAX,
            }

    def _stage_rows(
        self,
        session: Session,
        project: SqlProject,
        stage_id: str | None,
        *,
        limit: int,
        after: SqlProjectTicket | None = None,
        write: bool = False,
    ) -> builtins.list[SqlProjectTicket]:
        # The completed stage only grows, so it lists the most recently created first.
        descending = stage_id is not None and _stages(project).stages[-1].id == stage_id
        stmt = select(SqlProjectTicket).where(
            SqlProjectTicket.workspace_id == project.workspace_id,
            SqlProjectTicket.project_id == project.id,
        )
        if after is not None:
            stmt = stmt.where(
                keyset_after(
                    self._dialect_name,
                    (SqlProjectTicket.created_at, SqlProjectTicket.id),
                    (after.created_at, after.id),
                    descending=descending,
                )
            )
        order = (SqlProjectTicket.created_at, SqlProjectTicket.id)
        stmt = stmt.order_by(*(column.desc() for column in order) if descending else order)
        stmt = stmt.limit(limit)
        if write:
            stmt = stmt.with_for_update()
        index = "ix_project_tickets_created"
        if stage_id is not None:
            stmt = stmt.where(SqlProjectTicket.stage_id == stage_id)
            index = "ix_project_tickets_stage"
        return builtins.list(
            session.scalars(stmt.with_hint(SqlProjectTicket, f"FORCE INDEX ({index})", "mysql"))
        )

    def activity_sessions(
        self, project_id: str, ticket_ids: builtins.list[str], *, user_id: str | None
    ) -> dict[str, TicketActivitySessions]:
        if len(ticket_ids) > TICKET_PAGE_MAX:
            raise OmnigentError("Too many tickets", code=ErrorCode.INVALID_INPUT)
        ticket_ids = sorted({_identifier(ticket_id) for ticket_id in ticket_ids})
        with self._session("read_ticket_activity_sessions") as session:
            project = self._project(session, project_id, user_id)
            if not ticket_ids:
                return {}
            # Each primary-key prefix scan is bounded before the union, including quiet tickets.
            pages = [
                select(SqlProjectTicketSession.ticket_id, SqlProjectTicketSession.session_id)
                .where(
                    SqlProjectTicketSession.workspace_id == project.workspace_id,
                    SqlProjectTicketSession.project_id == project.id,
                    SqlProjectTicketSession.ticket_id == ticket_id,
                )
                .order_by(SqlProjectTicketSession.session_id)
                .limit(TICKET_ACTIVITY_SESSION_MAX + 1)
                .subquery()
                for ticket_id in ticket_ids
            ]
            grouped: dict[str, builtins.list[str]] = {ticket_id: [] for ticket_id in ticket_ids}
            for ticket_id, session_id in session.execute(
                union_all(*(select(page) for page in pages))
            ):
                grouped[ticket_id].append(session_id)
            return {
                ticket_id: TicketActivitySessions(
                    session_ids[:TICKET_ACTIVITY_SESSION_MAX],
                    len(session_ids) > TICKET_ACTIVITY_SESSION_MAX,
                )
                for ticket_id, session_ids in grouped.items()
            }

    def unseen_activity(
        self, project_id: str, ticket_ids: builtins.list[str], *, user_id: str | None
    ) -> dict[str, dict[str, Any]]:
        """Count each ticket's human-facing comments after the viewer's marker, excluding their own."""
        if len(ticket_ids) > TICKET_PAGE_MAX:
            raise OmnigentError("Too many tickets", code=ErrorCode.INVALID_INPUT)
        ids = sorted({_identifier(ticket_id) for ticket_id in ticket_ids})
        viewer = RESERVED_USER_LOCAL if user_id is None else user_id
        event, read = SqlProjectTicketEvent, SqlProjectTicketRead
        # An agent acting for the viewer carries a session; only direct edits are the viewer's own.
        by_others = or_(
            event.session_id.is_not(None),
            event.agent_id.is_not(None),
            event.user_id.is_not(None)
            if user_id is None
            else or_(event.user_id.is_(None), event.user_id != user_id),
        )
        with self._session("read_ticket_unseen_activity") as session:
            project = self._project(session, project_id, user_id)
            result: dict[str, dict[str, Any]] = {
                ticket_id: {
                    "viewer_seen_position": None,
                    "new_comment_count": 0,
                    "first_new_position": None,
                }
                for ticket_id in ids
            }
            if not ids:
                return result
            for ticket_id, seen_position in session.execute(
                select(read.ticket_id, read.seen_position).where(
                    read.workspace_id == project.workspace_id,
                    read.project_id == project.id,
                    read.ticket_id.in_(ids),
                    read.user_id == viewer,
                )
            ):
                result[ticket_id]["viewer_seen_position"] = seen_position
            unseen = and_(
                event.workspace_id == project.workspace_id,
                event.project_id == project.id,
                event.ticket_id.in_(ids),
                event.position > func.coalesce(read.seen_position, -1),
                by_others,
            )
            seen_marker = and_(
                read.workspace_id == event.workspace_id,
                read.project_id == event.project_id,
                read.ticket_id == event.ticket_id,
                read.user_id == viewer,
            )

            def count_new(ticket_id: str, position: int) -> None:
                entry = result[ticket_id]
                entry["new_comment_count"] += 1
                if entry["first_new_position"] is None or position < entry["first_new_position"]:
                    entry["first_new_position"] = position

            # Only human-facing comments count; stage, label and other edits are routine.
            # Who a comment is for lives in its payload, so agent notes are filtered here.
            orchestrator_session_id = _orchestrator_session_id(project)
            asks_by_ticket: dict[str, builtins.list[_HumanAsk]] = {}
            for row in session.scalars(
                select(event)
                .outerjoin(read, seen_marker)
                .where(unseen, event.kind.in_(NEW_COMMENT_KINDS))
            ):
                human = _note_audience(row, row.payload, orchestrator_session_id) == "human"
                if not human and row.session_id is not None:
                    if row.ticket_id not in asks_by_ticket:
                        asks_by_ticket[row.ticket_id] = self._human_asks_for(
                            session, project, row.ticket_id
                        )
                    human = any(
                        ask.covers(row.session_id, row.position)
                        for ask in asks_by_ticket[row.ticket_id]
                    )
                if human:
                    count_new(row.ticket_id, row.position)
            return result

    def mark_seen(
        self, project_id: str, ticket_id: str, body: MarkTicketSeen, *, user_id: str | None
    ) -> dict[str, Any]:
        """Advance the viewer's marker, never backwards and never past the latest activity."""
        viewer = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, user_id)
            require_active_account(session, user_id)
            ticket = self._row(session, project_id, ticket_id)
            position = min(body.seen_position, ticket.next_event_position - 1)
            key = (project.workspace_id, project.id, ticket.id, viewer)
            marker = session.get(SqlProjectTicketRead, key)
            if marker is None:
                marker = SqlProjectTicketRead(
                    workspace_id=project.workspace_id,
                    project_id=project.id,
                    ticket_id=ticket.id,
                    user_id=viewer,
                    seen_position=position,
                    seen_at=now_epoch(),
                )
                session.add(marker)
            elif position > marker.seen_position:
                marker.seen_position = position
                marker.seen_at = now_epoch()
            return {"viewer_seen_position": marker.seen_position}

        try:
            return run_write_transaction(self._write, "mark_ticket_seen", write)
        except IntegrityError:
            # A concurrent first view inserted the marker; merge into it instead.
            return run_write_transaction(self._write, "mark_ticket_seen", write)

    def stages(
        self, project_id: str, *, user_id: str | None, include_usage: bool = False
    ) -> dict[str, Any]:
        with self._session("get_ticket_stages") as session:
            project = self._project(session, project_id, user_id)
            configuration = _stages(project)
            result = configuration.model_dump()
            if include_usage:
                result["occupied_stage_ids"] = [
                    stage.id
                    for stage in configuration.stages
                    if self._stage_rows(session, project, stage.id, limit=1)
                ]
            return result

    def update_stages(
        self, project_id: str, body: UpdateTicketStages, actor: TicketActor
    ) -> dict[str, Any]:
        configuration = TicketStages(stages=body.stages)

        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            before = {stage.id: stage for stage in _stages(project).stages}
            after = {stage.id: stage for stage in configuration.stages}
            removed = before.keys() - after.keys()
            if body.moves.keys() - removed:
                raise OmnigentError(
                    "Move sources must be existing removed stages", code=ErrorCode.INVALID_INPUT
                )
            affected: list[SqlProjectTicket] = []
            for stage_id in sorted(removed):
                rows = self._stage_rows(
                    session,
                    project,
                    stage_id,
                    limit=STAGE_EDIT_TICKET_MAX - len(affected) + 1,
                    write=True,
                )
                affected.extend(rows)
                if len(affected) > STAGE_EDIT_TICKET_MAX:
                    raise OmnigentError(
                        f"This edit affects more than {STAGE_EDIT_TICKET_MAX} tickets. "
                        "Move tickets in smaller groups before changing these stages.",
                        code=ErrorCode.INVALID_INPUT,
                    )
                if rows and stage_id not in body.moves:
                    raise OmnigentError(
                        f"{before[stage_id].name} contains tickets. "
                        "Choose a surviving stage for them.",
                        code=ErrorCode.INVALID_INPUT,
                    )
            project.ticket_stages = configuration.model_dump()
            project.updated_at = now_epoch()
            for row in affected:
                previous = before[_stage_id(row)]
                target = after[body.moves[previous.id]]
                row.stage_id = target.id
                row.status = LEGACY_STATUS_CODES.get(target.id, 2 if target.is_complete else 0)
                row.latest_update = f"Moved to {target.name} (stage removed)"
                self._append(
                    session,
                    project,
                    row,
                    actor,
                    "updated",
                    {
                        "changes": {"stage_id": target.id},
                        "stage": _stage_snapshot(target),
                        "previous_stage": _stage_snapshot(previous),
                        "reason": "stage_removed",
                    },
                )
            return configuration.model_dump()

        return run_write_transaction(self._write, "update_ticket_stages", write)

    def update(
        self, project_id: str, ticket_id: str, body: UpdateTicket, actor: TicketActor
    ) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True, exclude={"comment", "audience", "final"})

        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            row = self._row(session, project_id, ticket_id, write=True)
            target = _stage(_stages(project), body.stage_id) if body.stage_id is not None else None
            adds_needs_human = (
                body.labels is not None
                and _has_needs_human(changes.get("labels"))
                and not _has_needs_human(row.labels)
            )
            needs_human_changed = body.labels is not None and _has_needs_human(
                changes.get("labels")
            ) != _has_needs_human(row.labels)
            for key, value in changes.items():
                setattr(row, key, value)
            if target is not None:
                row.status = LEGACY_STATUS_CODES.get(target.id, 2 if target.is_complete else 0)
            if changes:
                row.latest_update = (
                    f"Moved to {target.name}" if target is not None else "Updated the ticket"
                )[:300]
                self._append(
                    session,
                    project,
                    row,
                    actor,
                    "updated",
                    {"changes": changes, **({"stage": _stage_snapshot(target)} if target else {})},
                    needs_human_changed=needs_human_changed,
                )
            if body.final:
                _require_complete(project, row)
                _require_human_final(body.comment or "")
            if body.comment:
                audience = self._comment_audience(
                    session, project, row, actor, body.audience, adds_needs_human=adds_needs_human
                )
                if body.final:
                    audience = "human"
                self._append(
                    session,
                    project,
                    row,
                    actor,
                    "comment",
                    {
                        "text": body.comment,
                        "audience": audience,
                        **({"final": True} if body.final else {}),
                    },
                )
                row.comment_count += 1
                if audience == "human":
                    row.latest_update = body.comment[:300]
            return _ticket(row)

        return run_write_transaction(self._write, "update_ticket", write)

    def comment(
        self,
        project_id: str,
        ticket_id: str,
        body: CommentOnTicket,
        actor: TicketActor,
        *,
        mentions: builtins.list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            row = self._row(session, project_id, ticket_id, write=True)
            if body.final:
                _require_complete(project, row)
                _require_human_final(body.text)
            audience = (
                "human"
                if body.final
                else self._comment_audience(session, project, row, actor, body.audience)
            )
            payload: dict[str, Any] = {"text": body.text, "audience": audience}
            if body.final:
                payload["final"] = True
            if mentions:
                # The personas this comment cc'd, as {"id", "name"}; shown and delivered.
                payload["mentions"] = mentions
            self._append(session, project, row, actor, body.kind, payload)
            for output in body.outputs:
                self._append(session, project, row, actor, "output", output.model_dump())
            row.comment_count += 1
            # The card previews what the human should read, not the latest agent note.
            if audience == "human":
                row.latest_update = body.text[:300]
            return _ticket(row)

        return run_write_transaction(self._write, "comment_on_ticket", write)

    @staticmethod
    def _latest_ask_position(
        session: Session, ticket: SqlProjectTicket, asks: builtins.list[_HumanAsk]
    ) -> int | None:
        """While needs-human is open, its raiser's latest comment is the question to show."""
        if not _has_needs_human(ticket.labels) or not asks or asks[-1].end is not None:
            return None
        raiser = asks[-1].session_id
        if raiser is None:
            return None
        return session.scalar(
            select(func.max(SqlProjectTicketEvent.position)).where(
                SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
                SqlProjectTicketEvent.project_id == ticket.project_id,
                SqlProjectTicketEvent.ticket_id == ticket.id,
                SqlProjectTicketEvent.kind.in_(NEW_COMMENT_KINDS),
                SqlProjectTicketEvent.session_id == raiser,
            )
        )

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
            project = self._project(session, project_id, user_id)
            orchestrator_session_id = _orchestrator_session_id(project)
            ticket = self._row(session, project_id, ticket_id)
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
            asks = self._human_asks(session, ticket)
            latest_ask = self._latest_ask_position(session, ticket, asks)

            def asks_human(row: SqlProjectTicketEvent) -> bool:
                if row.position == latest_ask:
                    return True
                return any(ask.covers(row.session_id, row.position) for ask in asks)

            return {
                "data": [
                    _labeled_event(row, orchestrator_session_id, asks_human=asks_human(row))
                    for row in rows[:limit]
                ],
                "has_more": len(rows) > limit,
                "last_position": rows[min(limit, len(rows)) - 1].position if rows else after,
            }

    def link_session(
        self,
        project_id: str,
        ticket_id: str,
        session_id: str,
        action: str,
        actor: TicketActor,
        *,
        involvement: str | None = None,
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
                        involvement=involvement or None,
                    )
                )
            elif action == "attach" and link is not None and involvement is not None:
                if (link.involvement or "") == involvement:
                    return _ticket(ticket)
                link.involvement = involvement or None
            elif action == "remove" and link is not None:
                session.delete(link)
            else:
                return _ticket(ticket)
            payload: dict[str, Any] = {"session_id": session_id, "action": action}
            if involvement is not None:
                payload["involvement"] = involvement
                if link is not None and action == "attach":
                    payload["action"] = "updated"
            self._append(
                session,
                project,
                ticket,
                actor,
                "updated",
                payload,
            )
            return _ticket(ticket)

        return run_write_transaction(self._write, "link_ticket_session", write)

    def link_ticket(
        self,
        project_id: str,
        ticket_id: str,
        related_ticket_id: str,
        action: str,
        actor: TicketActor,
    ) -> dict[str, Any]:
        ticket_id, related_ticket_id = _identifier(ticket_id), _identifier(related_ticket_id)
        if ticket_id == related_ticket_id:
            raise OmnigentError("A ticket cannot link to itself", code=ErrorCode.INVALID_INPUT)

        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            first_id, second_id = sorted((ticket_id, related_ticket_id))
            first = self._row(session, project.id, first_id, write=True)
            second = self._row(session, project.id, second_id, write=True)
            key = (project.workspace_id, project.id, first_id, second_id)
            link = session.get(SqlProjectTicketLink, key)
            if action == "attach" and link is None:
                session.add(
                    SqlProjectTicketLink(
                        workspace_id=key[0],
                        project_id=key[1],
                        ticket_id=key[2],
                        related_ticket_id=key[3],
                        created_at=now_epoch(),
                    )
                )
            elif action == "remove" and link is not None:
                session.delete(link)
            else:
                return _ticket(first if first.id == ticket_id else second)
            for current, other in ((first, second), (second, first)):
                self._append(
                    session,
                    project,
                    current,
                    actor,
                    "updated",
                    {
                        "related_ticket_id": other.id,
                        "action": action,
                    },
                )
                current.latest_update = (
                    f"{'Related to' if action == 'attach' else 'Unlinked'} {other.title}"
                )[:300]
            return _ticket(first if first.id == ticket_id else second)

        return run_write_transaction(self._write, "link_related_tickets", write)

    def related(
        self,
        project_id: str,
        ticket_id: str,
        *,
        user_id: str | None,
        after: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, TICKET_PAGE_MAX))
        with self._session("list_related_tickets") as session:
            project = self._project(session, project_id, user_id)
            ticket = self._row(session, project.id, ticket_id)
            scope = (
                SqlProjectTicketLink.workspace_id == project.workspace_id,
                SqlProjectTicketLink.project_id == project.id,
            )
            neighbors = union_all(
                select(SqlProjectTicketLink.related_ticket_id).where(
                    *scope, SqlProjectTicketLink.ticket_id == ticket.id
                ),
                select(SqlProjectTicketLink.ticket_id).where(
                    *scope, SqlProjectTicketLink.related_ticket_id == ticket.id
                ),
            )
            stmt = select(SqlProjectTicket).where(
                SqlProjectTicket.workspace_id == project.workspace_id,
                SqlProjectTicket.project_id == project.id,
                SqlProjectTicket.id.in_(neighbors),
            )
            if after is not None:
                stmt = stmt.where(SqlProjectTicket.id > _identifier(after))
            rows = session.scalars(stmt.order_by(SqlProjectTicket.id).limit(limit + 1)).all()
            return {
                "data": [_ticket(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "last_id": rows[min(limit, len(rows)) - 1].id if rows else None,
            }

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
                    {
                        "session_id": row.session_id,
                        "created_at": row.created_at,
                        "involvement": row.involvement or "",
                    }
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
                SqlProjectTicketEvent.notification_state.in_(_UNDELIVERED)
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

    def pending_notification_projects(self) -> builtins.list[dict[str, Any]]:
        """Privileged delivery worker: projects with undelivered events, urgent or batched."""
        with self._session("pending_ticket_notification_projects") as session:
            urgent = func.max(
                case((SqlProjectTicketEvent.notification_state.in_(_URGENT), 1), else_=0)
            )
            rows = session.execute(
                select(
                    SqlProjectTicketEvent.workspace_id,
                    SqlProjectTicketEvent.project_id,
                    urgent,
                    func.min(SqlProjectTicketEvent.created_at),
                )
                .where(SqlProjectTicketEvent.notification_state.in_(_UNDELIVERED))
                .group_by(SqlProjectTicketEvent.workspace_id, SqlProjectTicketEvent.project_id)
            ).all()
            return [
                {
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "urgent": bool(has_urgent),
                    "oldest_at": oldest_at,
                }
                for workspace_id, project_id, has_urgent, oldest_at in rows
            ]

    def project_pending_notifications(
        self, project_id: str, *, limit: int = 200
    ) -> builtins.list[dict[str, Any]]:
        """Undelivered events for one project in arrival order, each with its ticket title."""
        with self._session("project_pending_ticket_notifications") as session:
            rows = session.execute(
                select(SqlProjectTicketEvent, SqlProjectTicket.title)
                .join(
                    SqlProjectTicket,
                    and_(
                        SqlProjectTicket.workspace_id == SqlProjectTicketEvent.workspace_id,
                        SqlProjectTicket.project_id == SqlProjectTicketEvent.project_id,
                        SqlProjectTicket.id == SqlProjectTicketEvent.ticket_id,
                    ),
                )
                .where(
                    SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                    SqlProjectTicketEvent.project_id == _identifier(project_id),
                    SqlProjectTicketEvent.notification_state.in_(_UNDELIVERED),
                )
                .order_by(
                    SqlProjectTicketEvent.created_at,
                    SqlProjectTicketEvent.ticket_id,
                    SqlProjectTicketEvent.position,
                )
                .limit(limit)
            ).all()
            return [
                {
                    **_event(row),
                    "ticket_title": title,
                    "urgent": row.notification_state in _URGENT,
                    "escalated": row.notification_state == NOTIFICATION_ESCALATED,
                }
                for row, title in rows
            ]

    def ticket_session_ids(self, project_id: str, ticket_id: str) -> builtins.list[str]:
        """Delivery worker: sessions linked to a ticket, oldest link first."""
        with self._session("list_ticket_worker_sessions") as session:
            return builtins.list(
                session.scalars(
                    select(SqlProjectTicketSession.session_id)
                    .where(
                        SqlProjectTicketSession.workspace_id == current_workspace_id(),
                        SqlProjectTicketSession.project_id == _identifier(project_id),
                        SqlProjectTicketSession.ticket_id == _identifier(ticket_id),
                    )
                    .order_by(
                        SqlProjectTicketSession.created_at, SqlProjectTicketSession.session_id
                    )
                ).all()
            )

    def mark_awaiting_worker(self, project_id: str, ticket_id: str, event_id: str) -> None:
        """A person's comment reached the ticket's workers; hold it until one replies."""
        with self._write("mark_ticket_comment_awaiting_worker") as session:
            event = session.get(
                SqlProjectTicketEvent, (current_workspace_id(), project_id, ticket_id, event_id)
            )
            if event is not None and event.notification_state == NOTIFICATION_PENDING:
                event.notification_state = NOTIFICATION_AWAITING_WORKER

    def escalate_unanswered(self, *, before: int) -> int:
        """Privileged delivery worker: comments no worker answered by ``before`` go urgent."""
        with self._write("escalate_unanswered_ticket_comments") as session:
            result = session.execute(
                update(SqlProjectTicketEvent)
                .where(
                    SqlProjectTicketEvent.notification_state == NOTIFICATION_AWAITING_WORKER,
                    SqlProjectTicketEvent.created_at <= before,
                )
                .values(notification_state=NOTIFICATION_ESCALATED)
            )
            return int(result.rowcount or 0)

    def mark_notified_many(self, project_id: str, events: builtins.list[tuple[str, str]]) -> None:
        """Retire a delivered batch of ``(ticket_id, event_id)`` pairs in one transaction."""
        with self._write("mark_ticket_notifications_queued") as session:
            for ticket_id, event_id in events:
                event = session.get(
                    SqlProjectTicketEvent,
                    (current_workspace_id(), project_id, ticket_id, event_id),
                )
                if event is not None and event.notification_state in _UNDELIVERED:
                    event.notification_state = NOTIFICATION_QUEUED

    def mark_notified(self, project_id: str, ticket_id: str, event_id: str) -> None:
        with self._write("mark_ticket_notification_queued") as session:
            event = session.get(
                SqlProjectTicketEvent, (current_workspace_id(), project_id, ticket_id, event_id)
            )
            if event is not None:
                event.notification_state = NOTIFICATION_QUEUED
