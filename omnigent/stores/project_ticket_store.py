"""Project-private tickets, atomic edits/activity, and a bounded notification outbox."""

from __future__ import annotations

import builtins
import re
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, case, func, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlConversationMetadata,
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
from omnigent.stores.project_store.access import accessible_project_ids, project_role
from omnigent.stores.project_store.sqlalchemy_store import (
    _decode_config,
    router_session_id_for_row,
)
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

TICKET_ACTIVITY_SESSION_MAX = 100
TICKET_SUMMARY_SCAN_MAX = 1000
# The Inbox lists at most this many open needs-human asks per viewer.
HUMAN_REQUEST_MAX = 100
# A cc stays in the Inbox until read, for at most this long.
CC_WINDOW_SECONDS = 30 * 24 * 3600
# Person-authored comments read per project when looking for cc's.
CC_SCAN_MAX = 200
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
        "requested_by": row.requested_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _orchestrator_session_id(session: Session, project: SqlProject) -> str | None:
    """The project's router session: its Lead by default, or the owner's chosen persona."""
    return router_session_id_for_row(session, project)


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
# Sign-off labels that answer an ask: a person adding one clears needs-human, wakes the asker.
GATE_LABELS = ("risk-ok", "product-ok", "owner-ok", "push-ok")


def _gate_labels_added(
    before: list[dict[str, Any]] | None, after: list[dict[str, Any]] | None
) -> list[str]:
    """The sign-off labels *after* has that *before* didn't, in ``GATE_LABELS`` order."""
    keys_before = {label.get("key") for label in before or [] if isinstance(label, dict)}
    keys_after = {label.get("key") for label in after or [] if isinstance(label, dict)}
    return [key for key in GATE_LABELS if key in keys_after and key not in keys_before]


PRODUCT_OK_LABEL = "product-ok"
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
        if project_role(session, project, user_id) is None:
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
        quiet: bool = False,
    ) -> None:
        own = actor.session_id is not None and actor.session_id == _orchestrator_session_id(
            session, project
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
        if own or quiet:
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

    def completed_moves(
        self, project_id: str, *, user_id: str | None, since_epoch: int | None
    ) -> builtins.list[dict[str, Any]]:
        """Every move of a ticket into a completed stage since *since_epoch* (inclusive).

        Readable by any project member. Each row is ``{"ticket_id", "user_id",
        "session_id", "created_at"}``: who moved it, for per-member counts.
        """
        with self._session("list_ticket_completed_moves") as session:
            self._project(session, project_id, user_id)
            stmt = select(SqlProjectTicketEvent).where(
                SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                SqlProjectTicketEvent.project_id == _identifier(project_id),
                SqlProjectTicketEvent.kind == EVENT_CODES["updated"],
            )
            if since_epoch is not None:
                stmt = stmt.where(SqlProjectTicketEvent.created_at >= since_epoch)
            moves = []
            for row in session.scalars(stmt.order_by(SqlProjectTicketEvent.created_at)):
                payload = row.payload or {}
                changes = payload.get("changes")
                stage = payload.get("stage")
                if not (
                    isinstance(changes, dict)
                    and "stage_id" in changes
                    and isinstance(stage, dict)
                    and stage.get("is_complete") is True
                ):
                    continue
                moves.append(
                    {
                        "ticket_id": row.ticket_id,
                        "user_id": row.user_id,
                        "session_id": row.session_id,
                        "created_at": row.created_at,
                    }
                )
            return moves

    def stage_histories(
        self, project_id: str, *, user_id: str | None
    ) -> builtins.dict[str, builtins.list[dict[str, Any]]]:
        """Each ticket's stages in time order, starting with the one it was created in.

        Readable by any project member. Maps ticket ID to ``{"stage_id",
        "is_complete", "at", "created"}`` entries; ``created`` marks the ticket's
        creation, missing for tickets created before stages were recorded.
        """
        with self._session("list_ticket_stage_histories") as session:
            self._project(session, project_id, user_id)
            stmt = (
                select(SqlProjectTicketEvent)
                .where(
                    SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                    SqlProjectTicketEvent.project_id == _identifier(project_id),
                    SqlProjectTicketEvent.kind.in_(
                        (EVENT_CODES["created"], EVENT_CODES["updated"])
                    ),
                )
                .order_by(SqlProjectTicketEvent.ticket_id, SqlProjectTicketEvent.position)
            )
            histories: dict[str, builtins.list[dict[str, Any]]] = {}
            for row in session.scalars(stmt):
                payload = row.payload or {}
                stage = payload.get("stage")
                if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
                    continue
                if row.kind == EVENT_CODES["updated"]:
                    changes = payload.get("changes")
                    if not isinstance(changes, dict) or "stage_id" not in changes:
                        continue
                elif row.ticket_id in histories:
                    continue
                histories.setdefault(row.ticket_id, []).append(
                    {
                        "stage_id": stage["id"],
                        "is_complete": stage.get("is_complete") is True,
                        "at": row.created_at,
                        "created": row.kind == EVENT_CODES["created"],
                    }
                )
            return histories

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

    def _answers_open_ask(
        self, session: Session, project: SqlProject, ticket: SqlProjectTicket, actor: TicketActor
    ) -> bool:
        """Whether a person's comment answers the ticket's open needs-human ask.

        Mirrors the Inbox: the commenter must be someone the ask is routed to, and a
        person's follow-up on their own ask doesn't answer it.
        """
        from omnigent.server.product_approval import (
            human_request_routing,
            owner_sign_off_pending,
        )

        if actor.session_id is not None or actor.agent_id is not None:
            return False
        if not _has_needs_human(ticket.labels):
            return False
        routing_project = SimpleNamespace(
            user_id=project.user_id, config=_decode_config(project.config)
        )
        _, responders = human_request_routing(routing_project, ticket.labels, ticket.requested_by)
        if owner_sign_off_pending(ticket.labels, ticket.requested_by):
            responders = {project.user_id}
        if actor.user_id not in responders:
            return False
        asks = self._human_asks(session, ticket)
        if asks and asks[-1].end is None and asks[-1].session_id is None:
            raised = session.scalar(
                select(SqlProjectTicketEvent).where(
                    SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
                    SqlProjectTicketEvent.project_id == ticket.project_id,
                    SqlProjectTicketEvent.ticket_id == ticket.id,
                    SqlProjectTicketEvent.position == asks[-1].start,
                )
            )
            if raised is not None and raised.agent_id is None and raised.user_id == actor.user_id:
                return False
        return True

    @staticmethod
    def _sign_offs_left(
        project: SqlProject, ticket: SqlProjectTicket, labels: list[dict[str, Any]] | None
    ) -> list[str]:
        from omnigent.server.product_approval import pending_sign_offs

        routing_project = SimpleNamespace(
            user_id=project.user_id, config=_decode_config(project.config)
        )
        return pending_sign_offs(routing_project, labels, ticket.requested_by)

    def _clear_answered_ask(
        self,
        session: Session,
        project: SqlProject,
        ticket: SqlProjectTicket,
        actor: TicketActor,
        *,
        by_label: str | None = None,
    ) -> None:
        """Drop needs-human after the comment or sign-off label that answered it.

        The agent can add it back. The answering event carries the notification, so
        this label change wakes no one.
        """
        labels = [
            label
            for label in ticket.labels or []
            if not (isinstance(label, dict) and label.get("key") == NEEDS_HUMAN_LABEL)
        ]
        ticket.labels = labels
        self._append(
            session,
            project,
            ticket,
            actor,
            "updated",
            {
                "changes": {"labels": labels},
                **({"cleared_by_label": by_label} if by_label else {"cleared_by_comment": True}),
            },
            quiet=True,
        )

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
            actor.session_id, actor.agent_id, _orchestrator_session_id(session, project)
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

    def create(
        self,
        project_id: str,
        body: CreateTicket,
        actor: TicketActor,
        *,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a ticket; ``requested_by`` defaults to the creating user and never changes."""

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
                requested_by=requested_by or actor.user_id,
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
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """List a page of tickets, optionally only one stage or one requester's."""
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
                requested_by=requested_by,
            )
            page: dict[str, Any] = {
                **configuration.model_dump(),
                "data": [_ticket(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "last_id": rows[min(limit, len(rows)) - 1].id if rows else None,
            }
            if after is None:
                # The first page carries the full match count, so a board lane can show
                # its real size without paging through every ticket.
                page["total"] = (
                    self._stage_count(session, project, stage_id, requested_by=requested_by)
                    if page["has_more"]
                    else len(page["data"])
                )
            return page

    def summary(
        self,
        project_id: str,
        *,
        user_id: str | None,
        created_after: int | None = None,
        include_responses: bool = True,
    ) -> dict[str, Any]:
        """Summarize the board: open human requests and, for new-ticket badges, ticket creation.

        :param created_after: Epoch seconds; when set, also count tickets created after it.
        :param include_responses: False skips the label scan behind ``needs_response_count``,
            leaving only the indexed creation fields.
        """
        with self._session("ticket_response_summary") as session:
            project = self._project(session, project_id, user_id)
            in_project = (
                SqlProjectTicket.workspace_id == project.workspace_id,
                SqlProjectTicket.project_id == project.id,
            )
            newest_first = (SqlProjectTicket.created_at.desc(), SqlProjectTicket.id.desc())
            summary: dict[str, Any] = {}
            if include_responses:
                # Labels are encrypted; bound the read to the newest tickets and report
                # partial counts.
                rows = session.execute(
                    select(SqlProjectTicket.labels, SqlProjectTicket.created_at)
                    .where(*in_project)
                    .order_by(*newest_first)
                    .limit(TICKET_SUMMARY_SCAN_MAX + 1)
                ).all()
                summary["needs_response_count"] = sum(
                    any(label.get("key") == "needs-human" for label in row.labels)
                    for row in rows[:TICKET_SUMMARY_SCAN_MAX]
                )
                summary["is_complete"] = len(rows) <= TICKET_SUMMARY_SCAN_MAX
                summary["latest_created_at"] = rows[0].created_at if rows else None
            else:
                summary["latest_created_at"] = session.scalar(
                    select(SqlProjectTicket.created_at)
                    .where(*in_project)
                    .order_by(*newest_first)
                    .limit(1)
                )
            if created_after is not None:
                summary["created_after_count"] = session.scalar(
                    select(func.count())
                    .select_from(SqlProjectTicket)
                    .where(*in_project, SqlProjectTicket.created_at > created_after)
                )
            return summary

    def session_tickets(
        self, session_ids: builtins.list[str], *, user_id: str | None
    ) -> dict[str, dict[str, Any]]:
        """The one ticket each session works on, for sessions linked to exactly one.

        A session linked to several tickets, like a project's Lead, maps to none, so
        the Inbox keeps sending the viewer to its chat.
        """
        if len(session_ids) > TICKET_PAGE_MAX:
            raise OmnigentError("Too many sessions", code=ErrorCode.INVALID_INPUT)
        try:
            ids = sorted({uuid_to_bytes(value).hex() for value in session_ids})
        except InvalidUuidError as exc:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND) from exc
        if not ids:
            return {}
        link = SqlProjectTicketSession
        with self._session("list_inbox_session_tickets") as session:
            rows = session.execute(
                select(
                    link.session_id,
                    SqlProjectTicket.id,
                    SqlProjectTicket.title,
                    SqlProject.id,
                    SqlProject.name,
                )
                .join(
                    SqlConversationMetadata,
                    (SqlConversationMetadata.workspace_id == link.workspace_id)
                    & (SqlConversationMetadata.id == link.session_id)
                    & (SqlConversationMetadata.project_id == link.project_id),
                )
                .join(
                    SqlProjectTicket,
                    (SqlProjectTicket.workspace_id == link.workspace_id)
                    & (SqlProjectTicket.project_id == link.project_id)
                    & (SqlProjectTicket.id == link.ticket_id),
                )
                .join(
                    SqlProject,
                    (SqlProject.workspace_id == link.workspace_id)
                    & (SqlProject.id == link.project_id),
                )
                .where(
                    link.workspace_id == current_workspace_id(),
                    link.session_id.in_(ids),
                    SqlProject.id.in_(accessible_project_ids(user_id)),
                )
            ).all()
            grouped: dict[str, builtins.list[dict[str, Any]]] = {}
            for session_id, ticket_id, title, project_id, project_name in rows:
                grouped.setdefault(session_id, []).append(
                    {
                        "ticket_id": ticket_id,
                        "title": title,
                        "project_id": project_id,
                        "project_name": project_name,
                    }
                )
            return {
                session_id: tickets[0]
                for session_id, tickets in grouped.items()
                if len(tickets) == 1
            }

    def human_requests(self, *, user_id: str | None) -> dict[str, Any]:
        """Open ``needs-human`` asks across the viewer's projects that are routed to them.

        An ask is routed by :func:`human_request_routing` and stays listed until the
        label is removed or any of its routed people acts on the ticket (a comment
        or an edit), which clears it for all of them.
        """
        from omnigent.server.product_approval import (
            FEATURE_KIND,
            RISK_OK_LABEL,
            human_request_routing,
            owner_sign_off_pending,
            pending_sign_offs,
            product_approvers,
            ticket_kind,
        )

        with self._session("list_viewer_human_requests") as session:
            projects = session.scalars(
                select(SqlProject).where(
                    SqlProject.workspace_id == current_workspace_id(),
                    SqlProject.id.in_(accessible_project_ids(user_id)),
                )
            ).all()
            requests: builtins.list[dict[str, Any]] = []
            complete = True
            for project in projects:
                # Labels are encrypted; bound the scan to the newest tickets like summary().
                rows = session.scalars(
                    select(SqlProjectTicket)
                    .where(
                        SqlProjectTicket.workspace_id == project.workspace_id,
                        SqlProjectTicket.project_id == project.id,
                    )
                    .order_by(SqlProjectTicket.created_at.desc(), SqlProjectTicket.id.desc())
                    .limit(TICKET_SUMMARY_SCAN_MAX + 1)
                ).all()
                complete = complete and len(rows) <= TICKET_SUMMARY_SCAN_MAX
                stages = _stages(project)
                routing_project = SimpleNamespace(
                    user_id=project.user_id, config=_decode_config(project.config)
                )
                for ticket in rows[:TICKET_SUMMARY_SCAN_MAX]:
                    if not _has_needs_human(ticket.labels):
                        continue
                    reason, responders = human_request_routing(
                        routing_project, ticket.labels, ticket.requested_by
                    )
                    if user_id not in responders:
                        continue
                    # Only the owner can clear an ask still waiting on their own sign-off;
                    # otherwise an approver adding product-ok answers a feature's ask.
                    owner_pending = owner_sign_off_pending(ticket.labels, ticket.requested_by)
                    answerers = {project.user_id} if owner_pending else responders
                    # An approver's product-ok answers the ask only when no risk-ok is also owed.
                    risk_owed = RISK_OK_LABEL in pending_sign_offs(
                        routing_project, ticket.labels, ticket.requested_by
                    )
                    product_ok_from = (
                        product_approvers(routing_project)
                        if not owner_pending
                        and not risk_owed
                        and ticket_kind(ticket.labels) == FEATURE_KIND
                        else set()
                    )
                    request = self._open_human_request(
                        session, ticket, answerers, user_id, product_ok_from=product_ok_from
                    )
                    if request is None:
                        continue
                    requests.append(
                        {
                            **request,
                            "project_id": project.id,
                            "project_name": project.name,
                            "ticket_id": ticket.id,
                            "title": ticket.title,
                            "stage": _stage_snapshot(_stage(stages, _stage_id(ticket))),
                            "reason": reason,
                            "responders": sorted(r for r in responders if r is not None),
                        }
                    )
            requests.sort(key=lambda item: item["raised_at"], reverse=True)
            return {
                "data": requests[:HUMAN_REQUEST_MAX],
                "has_more": len(requests) > HUMAN_REQUEST_MAX,
                "is_complete": complete,
            }

    def ticket_cc(self, *, user_id: str | None) -> builtins.list[dict[str, Any]]:
        """Tickets where a person cc'd the viewer in a comment they haven't read yet.

        One entry per ticket, for its newest such comment; opening the ticket past
        it (its read marker) clears it.
        """
        if user_id is None or user_id == RESERVED_USER_LOCAL:
            return []
        event, ticket_row, read = SqlProjectTicketEvent, SqlProjectTicket, SqlProjectTicketRead
        since = now_epoch() - CC_WINDOW_SECONDS
        entries: dict[tuple[str, str], dict[str, Any]] = {}
        with self._session("list_viewer_ticket_cc") as session:
            projects = session.scalars(
                select(SqlProject).where(
                    SqlProject.workspace_id == current_workspace_id(),
                    SqlProject.id.in_(accessible_project_ids(user_id)),
                )
            ).all()
            for project in projects:
                rows = session.execute(
                    select(event, ticket_row)
                    .join(
                        ticket_row,
                        and_(
                            ticket_row.workspace_id == event.workspace_id,
                            ticket_row.project_id == event.project_id,
                            ticket_row.id == event.ticket_id,
                        ),
                    )
                    .outerjoin(
                        read,
                        and_(
                            read.workspace_id == event.workspace_id,
                            read.project_id == event.project_id,
                            read.ticket_id == event.ticket_id,
                            read.user_id == user_id,
                        ),
                    )
                    .where(
                        event.workspace_id == project.workspace_id,
                        event.project_id == project.id,
                        event.kind == EVENT_CODES["comment"],
                        event.session_id.is_(None),
                        event.agent_id.is_(None),
                        event.user_id != user_id,
                        event.created_at >= since,
                        or_(read.seen_position.is_(None), event.position > read.seen_position),
                    )
                    .order_by(event.created_at.desc(), event.position.desc())
                    .limit(CC_SCAN_MAX)
                ).all()
                stages = _stages(project)
                for row, ticket in rows:
                    cc = row.payload.get("cc")
                    if not isinstance(cc, builtins.list) or not any(
                        isinstance(entry, dict) and entry.get("user_id") == user_id for entry in cc
                    ):
                        continue
                    key = (project.id, ticket.id)
                    if key in entries:
                        continue
                    entries[key] = {
                        "project_id": project.id,
                        "project_name": project.name,
                        "ticket_id": ticket.id,
                        "title": ticket.title,
                        "stage": _stage_snapshot(_stage(stages, _stage_id(ticket))),
                        "reason": "cc",
                        "responders": [user_id],
                        "ask_position": row.position,
                        "raised_at": row.created_at,
                        "raised_by_session_id": None,
                        "raised_by_user_id": row.user_id,
                        "question": (row.payload.get("text") or "")[:500],
                    }
        return builtins.list(entries.values())

    def _open_human_request(
        self,
        session: Session,
        ticket: SqlProjectTicket,
        responders: set[str | None],
        viewer: str | None,
        *,
        product_ok_from: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """The ask's raise time and question for *viewer*, or ``None`` once answered.

        A person who raised the ask is asking someone else: it isn't listed for
        them, and their own follow-ups don't answer it. Any other routed person's
        comment or edit answers it for everyone.
        """
        asks = self._human_asks(session, ticket)
        ask = asks[-1] if asks and asks[-1].end is None else None
        start = ask.start if ask is not None else -1
        scope = (
            SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
            SqlProjectTicketEvent.project_id == ticket.project_id,
            SqlProjectTicketEvent.ticket_id == ticket.id,
        )
        by_person = (
            SqlProjectTicketEvent.session_id.is_(None),
            SqlProjectTicketEvent.agent_id.is_(None),
        )
        raised = (
            session.scalar(
                select(SqlProjectTicketEvent).where(
                    *scope, SqlProjectTicketEvent.position == start
                )
            )
            if start >= 0
            else None
        )
        raised_by_person = (
            raised is not None and raised.session_id is None and raised.agent_id is None
        )
        asker = raised.user_id if raised is not None and raised_by_person else None
        if raised_by_person and viewer == asker:
            return None
        answerers = [
            r for r in responders if r is not None and not (raised_by_person and r == asker)
        ]
        answered = (
            session.scalar(
                select(SqlProjectTicketEvent.position)
                .where(
                    *scope,
                    *by_person,
                    SqlProjectTicketEvent.position > start,
                    SqlProjectTicketEvent.kind.in_((*NEW_UPDATE_KINDS, *NEW_COMMENT_KINDS)),
                    SqlProjectTicketEvent.user_id.in_(answerers)
                    if answerers
                    else SqlProjectTicketEvent.user_id.is_(None),
                )
                .limit(1)
            )
            if answerers or not raised_by_person
            else None
        )
        if answered is not None:
            return None
        if product_ok_from and self._product_ok_added_since(
            session, ticket, start, product_ok_from
        ):
            return None
        if raised_by_person:
            # The comment a person sent with the label (or right after) states the question.
            question_position = session.scalar(
                select(func.min(SqlProjectTicketEvent.position)).where(
                    *scope,
                    *by_person,
                    SqlProjectTicketEvent.position >= start,
                    SqlProjectTicketEvent.kind.in_(NEW_COMMENT_KINDS),
                    SqlProjectTicketEvent.user_id == asker
                    if asker is not None
                    else SqlProjectTicketEvent.user_id.is_(None),
                )
            )
        else:
            question_position = self._latest_ask_position(session, ticket, asks)
        question = (
            session.scalar(
                select(SqlProjectTicketEvent).where(
                    *scope, SqlProjectTicketEvent.position == question_position
                )
            )
            if question_position is not None
            else None
        )
        anchor = question if question is not None else raised
        return {
            "ask_position": anchor.position if anchor is not None else None,
            "raised_at": raised.created_at if raised is not None else ticket.updated_at,
            "raised_by_session_id": ask.session_id if ask is not None else None,
            "raised_by_user_id": asker,
            "question": (question.payload.get("text") or "")[:500] if question else None,
        }

    @staticmethod
    def _product_ok_added_since(
        session: Session, ticket: SqlProjectTicket, start: int, approvers: set[str]
    ) -> bool:
        """Whether one of *approvers* added ``product-ok`` after position *start*."""
        rows = session.scalars(
            select(SqlProjectTicketEvent)
            .where(
                SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
                SqlProjectTicketEvent.project_id == ticket.project_id,
                SqlProjectTicketEvent.ticket_id == ticket.id,
                SqlProjectTicketEvent.kind.in_(NEW_UPDATE_KINDS),
            )
            .order_by(SqlProjectTicketEvent.position)
        )
        had = False
        for row in rows:
            labels = _event_labels(row)
            if labels is None:
                continue
            has = isinstance(labels, list) and any(
                isinstance(label, dict) and label.get("key") == PRODUCT_OK_LABEL
                for label in labels
            )
            if (
                has
                and not had
                and row.position > start
                and row.session_id is None
                and row.agent_id is None
                and row.user_id in approvers
            ):
                return True
            had = has
        return False

    def _stage_count(
        self,
        session: Session,
        project: SqlProject,
        stage_id: str | None,
        *,
        requested_by: str | None = None,
    ) -> int:
        """Count the tickets ``_stage_rows`` would list, on the same index."""
        stmt = (
            select(func.count())
            .select_from(SqlProjectTicket)
            .where(
                SqlProjectTicket.workspace_id == project.workspace_id,
                SqlProjectTicket.project_id == project.id,
            )
        )
        if requested_by is not None:
            stmt = stmt.where(SqlProjectTicket.requested_by == requested_by)
        if stage_id is not None:
            stmt = stmt.where(SqlProjectTicket.stage_id == stage_id)
        return int(session.scalar(stmt) or 0)

    def _stage_rows(
        self,
        session: Session,
        project: SqlProject,
        stage_id: str | None,
        *,
        limit: int,
        after: SqlProjectTicket | None = None,
        write: bool = False,
        requested_by: str | None = None,
    ) -> builtins.list[SqlProjectTicket]:
        # The completed stage only grows, so it lists the most recently created first.
        descending = stage_id is not None and _stages(project).stages[-1].id == stage_id
        stmt = select(SqlProjectTicket).where(
            SqlProjectTicket.workspace_id == project.workspace_id,
            SqlProjectTicket.project_id == project.id,
        )
        if requested_by is not None:
            stmt = stmt.where(SqlProjectTicket.requested_by == requested_by)
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
            orchestrator_session_id = _orchestrator_session_id(session, project)
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
        base_changes = body.model_dump(
            exclude_none=True,
            exclude={"comment", "audience", "final", "add_labels", "remove_labels"},
        )

        def write(session: Session) -> dict[str, Any]:
            project = self._project(session, project_id, actor.user_id, write=True)
            row = self._row(session, project_id, ticket_id, write=True)
            changes = dict(base_changes)
            # Label adds and removes apply to the labels as they are now, inside this write.
            if body.has_label_changes:
                try:
                    changes["labels"] = body.labels_after(row.labels)
                except ValueError as exc:
                    raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            touches_labels = "labels" in changes
            target = _stage(_stages(project), body.stage_id) if body.stage_id is not None else None
            adds_needs_human = (
                touches_labels
                and _has_needs_human(changes.get("labels"))
                and not _has_needs_human(row.labels)
            )
            needs_human_changed = touches_labels and _has_needs_human(
                changes.get("labels")
            ) != _has_needs_human(row.labels)
            # A person's sign-off label answers their ask; judged on the labels before it.
            person = actor.session_id is None and actor.agent_id is None
            gates_added = (
                _gate_labels_added(row.labels, changes.get("labels"))
                if touches_labels and person
                else []
            )
            # One sign-off answers the ask only when it was the last one the ticket waited on.
            label_answers_ask = (
                bool(gates_added)
                and self._answers_open_ask(session, project, row, actor)
                and not self._sign_offs_left(project, row, changes.get("labels"))
            )
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
                    {
                        "changes": changes,
                        **({"stage": _stage_snapshot(target)} if target else {}),
                        **({"gate_labels_added": gates_added} if gates_added else {}),
                        **({"answered_ask": True} if label_answers_ask else {}),
                    },
                    needs_human_changed=needs_human_changed,
                )
                if label_answers_ask and _has_needs_human(row.labels):
                    self._clear_answered_ask(session, project, row, actor, by_label=gates_added[0])
            if body.final:
                _require_complete(project, row)
                _require_human_final(body.comment or "")
            if body.comment:
                audience = self._comment_audience(
                    session, project, row, actor, body.audience, adds_needs_human=adds_needs_human
                )
                if body.final:
                    audience = "human"
                # An explicit label list is the person's own call on needs-human.
                answers_ask = not touches_labels and self._answers_open_ask(
                    session, project, row, actor
                )
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
                        **({"cleared_needs_human": True} if answers_ask else {}),
                    },
                )
                if answers_ask:
                    self._clear_answered_ask(session, project, row, actor)
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
        cc: builtins.list[dict[str, str]] | None = None,
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
            if cc:
                # The people this comment cc'd, as {"user_id", "name"}; listed in their Inbox.
                payload["cc"] = cc
            answers_ask = body.kind == "comment" and self._answers_open_ask(
                session, project, row, actor
            )
            if answers_ask:
                payload["cleared_needs_human"] = True
            self._append(session, project, row, actor, body.kind, payload)
            if answers_ask:
                self._clear_answered_ask(session, project, row, actor)
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
            orchestrator_session_id = _orchestrator_session_id(session, project)
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

    def person_comment_context(
        self, project_id: str, ticket_id: str, before_position: int
    ) -> dict[str, Any]:
        """Delivery worker: who a person's comment at *before_position* most likely answers.

        :returns: ``labels``, ``stage_id``, ``asker`` (the session that raised a still-open
            needs-human, if any) and ``commenters`` (sessions that commented earlier, most
            recent first).
        """
        with self._session("ticket_person_comment_context") as session:
            ticket = self._row(session, project_id, ticket_id)
            asks = self._human_asks(session, ticket)
            # The ask open when the person commented, even if their comment then cleared it.
            open_ask = next(
                (
                    ask
                    for ask in reversed(asks)
                    if ask.start < before_position
                    and (ask.end is None or ask.end >= before_position)
                ),
                None,
            )
            authors = session.scalars(
                select(SqlProjectTicketEvent.session_id)
                .where(
                    SqlProjectTicketEvent.workspace_id == ticket.workspace_id,
                    SqlProjectTicketEvent.project_id == ticket.project_id,
                    SqlProjectTicketEvent.ticket_id == ticket.id,
                    SqlProjectTicketEvent.kind.in_(NEW_COMMENT_KINDS),
                    SqlProjectTicketEvent.session_id.is_not(None),
                    SqlProjectTicketEvent.position < before_position,
                )
                .order_by(SqlProjectTicketEvent.position.desc())
                .limit(200)
            ).all()
            return {
                "labels": builtins.list(ticket.labels or []),
                "stage_id": ticket.stage_id,
                "asker": open_ask.session_id if open_ask is not None else None,
                "commenters": builtins.list(dict.fromkeys(authors)),
            }

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
