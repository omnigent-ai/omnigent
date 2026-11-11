"""Session journal API: what a session's agent plans and records.

A session's journal is append-only. Its agent writes plans, step statuses and
notes through its ``sys_journal_*`` tools, which the session's runner sends
here. Entries are attributed to the automation run the session serves, named
by its :data:`~omnigent.stores.conversation_store.SCHEDULED_TASK_RUN_LABEL_KEY`
label. People follow a journal's plan in the session's Tasks tab.

Routes, under ``/v1``:

* ``GET /sessions/{session_id}/journal`` returns one page, with the automation
  runs its entries name (read access).
* ``POST /sessions/{session_id}/journal`` appends an entry. Only the session's
  runner writes, so the call must carry the orchestration source header naming
  the session (edit access).
* ``POST /sessions/{session_id}/journal/input-requests`` asks the user about a
  task for ``sys_workflow_request_input``: it marks the task ``needs_input`` and
  raises an elicitation whose answer the runner waits for. Runner-only, like
  the append.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import Any, Literal

from fastapi import APIRouter, Query, Request

from omnigent.entities import Conversation
from omnigent.entities.session_journal import (
    JOURNAL_PAGE_MAX,
    InvalidJournalEntryError,
    JournalEntry,
    JournalEntryTooLargeError,
    StepNotReadyError,
    normalize_status,
    normalize_step_id,
)
from omnigent.entities.workflow_questions import (
    normalize_questions,
    question_form,
    status_text,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.server.schemas import (
    ElicitationRequestEvent,
    ElicitationRequestParams,
    JournalEntryKind,
    SessionJournalAppendRequest,
    SessionJournalAuthor,
    SessionJournalEntry,
    SessionJournalInputRequest,
    SessionJournalInputRequested,
    SessionJournalPage,
)
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_journal_store import JournalSessionNotFoundError, SessionJournalStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

_logger = logging.getLogger(__name__)

# Kinds only Omnigent writes; a plan copied from a user-drawn workflow is Omnigent's too.
_OMNIGENT_KINDS = frozenset({"dispatch", "dispatch_result", "plan_status"})

# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, SessionJournalStore] = {}
_stores_lock = threading.Lock()


def open_store(conversation_store: object) -> SessionJournalStore | None:
    """Return the journal store beside the session metadata, or ``None`` without SQL.

    :param conversation_store: The server's conversation store; journals live in
        the database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = SessionJournalStore(location)
            except Exception:
                _logger.warning("Session journal storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


def _same_session(left: str, right: str) -> bool:
    try:
        return uuid_to_bytes(left.strip()) == uuid_to_bytes(right)
    except InvalidUuidError:
        return False


def entry_to_response(entry: JournalEntry) -> SessionJournalEntry:
    """Serialize an entry; its author is derived from its kind and a plan's source."""
    omnigent = entry.kind in _OMNIGENT_KINDS or (
        entry.kind == "plan" and entry.payload.get("source") == "workflow"
    )
    return SessionJournalEntry(
        id=entry.id,
        position=entry.position,
        created_at=entry.created_at,
        scheduled_task_run_id=entry.scheduled_task_run_id,
        step=entry.step,
        author=SessionJournalAuthor(type="omnigent" if omnigent else "agent"),
        kind=entry.kind,  # type: ignore[arg-type]
        payload=entry.payload,
    )


def create_session_journal_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the session journal router, mounted under ``/v1``.

    :param conversation_store: The server's conversation store. Journals are
        stored in its database; without a SQL backend every route answers 404.
    :param auth_provider: Identifies the caller; ``None`` in single-user mode.
    :param permission_store: Session grants; ``None`` disables enforcement.
    """
    router = APIRouter()

    def _store() -> SessionJournalStore:
        store = open_store(conversation_store)
        if store is None:
            raise OmnigentError(
                "Session journals are not available on this server", code=ErrorCode.NOT_FOUND
            )
        return store

    async def _session(request: Request, session_id: str, level: int) -> Conversation:
        user_id = get_user_id(request, auth_provider)
        access = await require_access_and_level(
            user_id, session_id, level, permission_store, conversation_store
        )
        conversation = access.conversation or await asyncio.to_thread(
            conversation_store.get_conversation, session_id
        )
        if conversation is None:
            raise session_not_found()
        return conversation

    def _run_id(session: Conversation) -> str | None:
        value = session.labels.get(SCHEDULED_TASK_RUN_LABEL_KEY)
        if not value:
            return None
        try:
            return uuid_to_bytes(value).hex()
        except InvalidUuidError:
            _logger.warning("Session %s names an invalid automation run", session.id)
            return None

    @router.get("/sessions/{session_id}/journal", response_model=SessionJournalPage)
    async def list_session_journal(
        request: Request,
        session_id: str,
        limit: int = Query(100, ge=1, le=JOURNAL_PAGE_MAX),
        after: str | None = None,
        before: str | None = None,
        order: Literal["asc", "desc"] = "asc",
        kind: JournalEntryKind | None = None,
        step: str | None = None,
    ) -> SessionJournalPage:
        """One page of the session's journal by position; requires read access.

        ``after`` continues in the sort order and ``before`` goes against it,
        both taking entry IDs, as for conversation items.
        """
        session = await _session(request, session_id, LEVEL_READ)
        store = _store()
        try:
            page = await asyncio.to_thread(
                store.list_entries,
                session.id,
                limit=limit,
                order=order,
                after=after,
                before=before,
                kind=kind,
                step=step,
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        data = [entry_to_response(entry) for entry in page.entries]
        return SessionJournalPage(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=page.has_more,
        )

    async def _agent_session(request: Request, session_id: str) -> Conversation:
        """The session, when the call comes from its own runner."""
        session = await _session(request, session_id, LEVEL_EDIT)
        source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if source is None or not _same_session(source, session.id):
            raise OmnigentError(
                "Only the session's own agent writes its journal", code=ErrorCode.FORBIDDEN
            )
        return session

    async def _append(
        session: Conversation,
        kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        step: str | None,
    ) -> JournalEntry:
        store = _store()
        try:
            appended = await asyncio.to_thread(
                lambda: store.append(
                    session.id,
                    kind,
                    payload,
                    idempotency_key=idempotency_key,
                    step=step,
                    scheduled_task_run_id=_run_id(session),
                )
            )
        except JournalSessionNotFoundError as exc:
            raise session_not_found() from exc
        except JournalEntryTooLargeError as exc:
            raise OmnigentError(f"entry_too_large: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        except StepNotReadyError as exc:
            raise OmnigentError(f"step_not_ready: {exc}", code=ErrorCode.CONFLICT) from exc
        except InvalidJournalEntryError as exc:
            raise OmnigentError(f"invalid_entry: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        return appended.entry

    @router.post("/sessions/{session_id}/journal", response_model=SessionJournalEntry)
    async def append_session_journal_entry(
        request: Request, session_id: str, body: SessionJournalAppendRequest
    ) -> SessionJournalEntry:
        """Append an entry written by the session's agent; only its runner calls this."""
        session = await _agent_session(request, session_id)
        if body.kind == "plan" and set(body.payload) != {"name", "steps"}:
            # The agent's plans are its own; copies of drawn workflows come later.
            raise OmnigentError(
                "invalid_entry: a plan payload has exactly two fields, name and steps",
                code=ErrorCode.INVALID_INPUT,
            )
        entry = await _append(
            session,
            body.kind,
            body.payload,
            idempotency_key=body.idempotency_key,
            step=body.step,
        )
        return entry_to_response(entry)

    @router.post(
        "/sessions/{session_id}/journal/input-requests",
        response_model=SessionJournalInputRequested,
    )
    async def request_session_journal_input(
        request: Request, session_id: str, body: SessionJournalInputRequest
    ) -> SessionJournalInputRequested:
        """Mark a task waiting for the user and ask them; only the session's runner calls this.

        The task gets a ``needs_input`` status naming the first question, and the
        questions go out as an elicitation with the web's question form. A retry
        with the same idempotency key reuses the status and the elicitation ID.
        """
        session = await _agent_session(request, session_id)
        try:
            step = normalize_step_id(body.step)
            questions = normalize_questions(body.questions)
        except InvalidJournalEntryError as exc:
            raise OmnigentError(f"invalid_input: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        plan = await asyncio.to_thread(_store().current_plan, session.id)
        task = next(
            (item for item in (plan.payload["steps"] if plan else []) if item["id"] == step),
            None,
        )
        if task is None:
            raise OmnigentError(
                "invalid_input: the session has no plan; write one first"
                if plan is None
                else f"invalid_input: step {step!r} isn't in the current plan",
                code=ErrorCode.INVALID_INPUT,
            )
        entry = await _append(
            session,
            "status",
            normalize_status("needs_input", text=status_text(questions)),
            idempotency_key=body.idempotency_key,
            step=step,
        )
        key = f"{session.id}:{body.idempotency_key}".encode()
        elicitation_id = f"elicit_journal_{hashlib.sha256(key).hexdigest()[:32]}"
        event = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=ElicitationRequestParams(
                mode="form",
                message=f"Waiting for you on {task['name']}",
                phase="workflow_request_input",
                policy_name="workflow_request_input",
                ask_user_question=question_form(questions),
                journal_step=step,
            ),
        ).model_dump()
        # Through the session stream, so the pending index puts it in the Inbox.
        session_stream.publish(session.id, event)
        from omnigent.server.routes._sessions.helpers import (
            _publish_elicitation_request_to_ancestors,
        )

        await asyncio.to_thread(
            _publish_elicitation_request_to_ancestors, conversation_store, session.id, event
        )
        return SessionJournalInputRequested(
            entry=entry_to_response(entry), elicitation_id=elicitation_id
        )

    return router
