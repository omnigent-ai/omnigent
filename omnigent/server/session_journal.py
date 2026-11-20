"""Session journal API: what a session's agent plans and records.

A session's journal is append-only. Its agent writes plans, step statuses and
notes through its ``sys_journal_*`` tools, which the session's runner sends
here. Entries are attributed to the automation run the session serves, named
by its :data:`~omnigent.stores.conversation_store.SCHEDULED_TASK_RUN_LABEL_KEY`
label. A plan can name the saved workflow it starts a run of; statuses and
notes written while it is current carry that workflow too. People follow a
journal's plan in the session's Tasks tab.

Routes, under ``/v1``:

* ``GET /sessions/{session_id}/journal`` returns one page, with the automation
  runs its entries name (read access).
* ``POST /sessions/{session_id}/journal`` appends an entry. Only the session's
  runner writes: the source header names the session, and a shared server
  verifies its bound runner's tunnel token (edit access).
* ``POST /sessions/{session_id}/journal/input-requests`` asks the user about a
  task for ``sys_workflow_request_input``: it marks the task ``needs_input`` and
  raises an elicitation whose answer the runner waits for. Runner-only, like
  the append.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from omnigent.entities import Conversation
from omnigent.entities.session_journal import (
    JOURNAL_PAGE_MAX,
    InvalidJournalEntryError,
    JournalAppend,
    JournalEntry,
    JournalEntryTooLargeError,
    JournalFullError,
    StepNotReadyError,
    normalize_description,
    normalize_status,
    normalize_step_id,
)
from omnigent.entities.workflow import InvalidWorkflowError, Workflow
from omnigent.entities.workflow_questions import (
    normalize_questions,
    question_form,
    status_text,
    workflow_elicitation_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.journal_markers import (
    record_journal_checkpoint,
    record_plan_marker,
    record_status_marker,
)
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.server.routes.workflow_permissions import readable_workflow
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
from omnigent.server.session_provenance import session_provenance
from omnigent.server.workflow_telemetry import log_journal_entry
from omnigent.server.workflows import open_store as open_workflow_store
from omnigent.server.workflows import require_workflows
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


def _require_store(conversation_store: ConversationStore) -> SessionJournalStore:
    store = open_store(conversation_store)
    if store is None:
        raise OmnigentError(
            "Session journals are not available on this server", code=ErrorCode.NOT_FOUND
        )
    return store


@contextmanager
def _journal_errors() -> Iterator[None]:
    try:
        yield
    except JournalSessionNotFoundError as exc:
        raise session_not_found() from exc
    except JournalEntryTooLargeError as exc:
        raise OmnigentError(f"entry_too_large: {exc}", code=ErrorCode.INVALID_INPUT) from exc
    except StepNotReadyError as exc:
        raise OmnigentError(f"step_not_ready: {exc}", code=ErrorCode.CONFLICT) from exc
    except JournalFullError as exc:
        raise OmnigentError(f"journal_full: {exc}", code=ErrorCode.CONFLICT) from exc
    except InvalidJournalEntryError as exc:
        raise OmnigentError(f"invalid_entry: {exc}", code=ErrorCode.INVALID_INPUT) from exc


async def require_current_workflow_run(
    conversations: ConversationStore, conversation_id: str, plan_id: str | None
) -> None:
    """Reject run controls from a page that still displays an older invocation."""
    if plan_id is None:
        return

    def is_current() -> bool:
        journal = open_store(conversations)
        conversation = conversations.get_conversation(conversation_id)
        return (
            journal is not None
            and conversation is not None
            and not conversation.archived
            and journal.is_current_run(
                conversation_id,
                plan_id,
                conversation.labels.get(SCHEDULED_TASK_RUN_LABEL_KEY),
            )
        )

    if not await asyncio.to_thread(is_current):
        raise OmnigentError(
            "This run is no longer current. Refresh the runs and try again.",
            code=ErrorCode.CONFLICT,
        )


def _same_session(left: str, right: str) -> bool:
    try:
        return uuid_to_bytes(left.strip()) == uuid_to_bytes(right)
    except InvalidUuidError:
        return False


def _require_attended_session(conversations: ConversationStore, session: Conversation) -> None:
    """Follow automation provenance through sub-agents and independently created workers."""
    provenance = session_provenance(conversations, session)
    if provenance.execution_source != "interactive":
        reason = (
            "This session is part of an automation."
            if provenance.execution_source == "automation"
            else "Could not establish that this session is interactive."
        )
        raise OmnigentError(
            f"unattended_input_not_allowed: {reason} "
            "No one can answer questions or approve requests. Do not ask for input or "
            "write needs_input. Choose an option within the user's authorization and "
            "record the decision, or mark the step failed with the blocker. Continue "
            "independent work.",
            code=ErrorCode.CONFLICT,
        )


def entry_to_response(entry: JournalEntry) -> SessionJournalEntry:
    """Serialize an entry; its author is derived from its kind and a plan's source."""
    omnigent = entry.kind in _OMNIGENT_KINDS or (
        entry.kind == "plan" and entry.content.get("source") == "workflow"
    )
    return SessionJournalEntry(
        id=entry.id,
        position=entry.position,
        created_at=entry.created_at,
        scheduled_task_run_id=entry.scheduled_task_run_id,
        step=entry.step,
        author=SessionJournalAuthor(type="omnigent" if omnigent else "agent"),
        kind=entry.kind,  # type: ignore[arg-type]
        content=entry.content,
        workflow_id=entry.workflow_id,
    )


async def load_workflow_for_run(
    conversation_store: ConversationStore,
    workflow_id: str,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
) -> Workflow:
    """Load and validate a saved plan the caller can run before creating its session.

    :param permission_store: Lets someone a workflow is shared with run it; ``None``
        allows only the owner's.
    """
    workflow = await _load_workflow(
        conversation_store, workflow_id, user_id=user_id, permission_store=permission_store
    )
    store = _require_store(conversation_store)
    with _journal_errors():
        await asyncio.to_thread(
            store.validate_initial_plan,
            {"name": workflow.name, "steps": workflow.steps, "source": "workflow"},
        )
    return workflow


def _check_proposed_description(conversation_store: ConversationStore, description: str) -> None:
    """Refuse a proposed description its workflow couldn't store, so Save plan can save it.

    Text the journal itself refuses, such as a lone surrogate, is left for it to report.
    """
    try:
        text = normalize_description(description)
    except InvalidJournalEntryError:
        return
    workflows = open_workflow_store(conversation_store)
    if workflows is None:
        return
    try:
        workflows.check_description(text)
    except InvalidWorkflowError as exc:
        raise OmnigentError(f"invalid_entry: {exc}", code=ErrorCode.INVALID_INPUT) from exc


async def _load_workflow(
    conversation_store: ConversationStore,
    workflow_id: str,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
) -> Workflow:
    """Look up a workflow with saved steps that the caller owns or that is shared with them."""
    workflows = open_workflow_store(conversation_store)
    workflow = None
    try:
        uuid_to_bytes(workflow_id)
    except InvalidUuidError:
        pass
    else:
        if workflows is not None:
            found = await asyncio.to_thread(
                readable_workflow,
                workflows,
                workflow_id,
                user_id,
                permission_store=permission_store,
            )
            workflow = found[0] if found is not None else None
    if workflow is None:
        raise OmnigentError(
            f"workflow_not_found: no saved workflow {workflow_id!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    if not workflow.steps:
        raise OmnigentError(
            "workflow_is_draft: save a plan before running this workflow",
            code=ErrorCode.INVALID_INPUT,
        )

    return workflow


async def initialize_workflow_plan(
    conversation_store: ConversationStore,
    session: Conversation,
    workflow: Workflow,
    *,
    user_id: str | None,
) -> None:
    """Snapshot a saved plan before the session can receive its first input."""
    store = _require_store(conversation_store)
    with _journal_errors():
        appended = await asyncio.to_thread(
            store.append,
            session.id,
            "plan",
            {"name": workflow.name, "steps": workflow.steps, "source": "workflow"},
            idempotency_key="workflow:initial-plan",
            workflow_id=workflow.id,
            scheduled_task_run_id=_run_id(session),
        )
    await _observe_entry(conversation_store, store, session, appended, user_id=user_id)


def _run_id(session: Conversation) -> str | None:
    value = session.labels.get(SCHEDULED_TASK_RUN_LABEL_KEY)
    if not value:
        return None
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError:
        _logger.warning("Session %s names an invalid automation run", session.id)
        return None


async def _observe_entry(
    conversation_store: ConversationStore,
    store: SessionJournalStore,
    session: Conversation,
    appended: JournalAppend,
    *,
    user_id: str | None,
) -> None:
    session_id, entry = session.id, appended.entry
    # Every attempt writes its marker: a retry after a failed first marker
    # adds it, and the marker's stable ID keeps it to one.
    if entry.kind not in ("status", "plan"):
        await record_journal_checkpoint(conversation_store, session_id, entry)
        return
    try:
        page = await asyncio.to_thread(
            store.list_entries, session_id, kind="plan", order="desc", after=entry.id, limit=1
        )
        earlier_plan = page.entries[0] if page.entries else None
        if appended.created:
            await asyncio.to_thread(
                log_journal_entry,
                conversation_store,
                session,
                entry,
                earlier_plan,
                user_id=user_id,
            )
        if entry.kind == "status":
            await record_status_marker(conversation_store, session_id, entry, earlier_plan)
        else:
            await record_plan_marker(conversation_store, session_id, entry, earlier_plan)
    except Exception:  # markers are display metadata; the write already succeeded
        _logger.warning("Could not mark %s in the chat of %s", entry.id, session_id, exc_info=True)


def _entries_to_responses(
    store: SessionJournalStore, entries: Sequence[JournalEntry]
) -> list[SessionJournalEntry]:
    """Include authoritative legacy graph context even when the page omits replans."""
    responses = [entry_to_response(entry) for entry in entries]
    latest = max(
        (entry for entry in entries if entry.kind == "plan"),
        key=lambda entry: entry.position,
        default=None,
    )
    if latest is not None:
        (revision,) = store.plan_graph_revisions([latest])
        for response in responses:
            if response.id == latest.id:
                response.content = {**response.content, "graph_revision": revision}
    return responses


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
    router = APIRouter(dependencies=[Depends(require_workflows)])

    def _store() -> SessionJournalStore:
        return _require_store(conversation_store)

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
        data = await asyncio.to_thread(_entries_to_responses, store, page.entries)
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
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id not in (None, RESERVED_USER_LOCAL):
            # Local single-user runners may use stable IDs unrelated to tunnel tokens.
            token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
            if (
                not token
                or not session.runner_id
                or token_bound_runner_id(token) != session.runner_id
            ):
                raise OmnigentError(
                    "Only the session's bound runner writes its journal", code=ErrorCode.FORBIDDEN
                )
        return session

    async def _append(
        session: Conversation,
        kind: str,
        content: dict[str, Any],
        *,
        idempotency_key: str,
        step: str | None,
        user_id: str | None,
        workflow_id: str | None = None,
        start_run: bool = True,
    ) -> JournalEntry:
        store = _store()
        if kind == "status" and content.get("status") == "needs_input":
            await asyncio.to_thread(_require_attended_session, conversation_store, session)
        with _journal_errors():
            appended = await asyncio.to_thread(
                lambda: store.append(
                    session.id,
                    kind,
                    content,
                    idempotency_key=idempotency_key,
                    step=step,
                    scheduled_task_run_id=_run_id(session),
                    workflow_id=workflow_id,
                    start_run=start_run,
                )
            )
        await _observe_entry(conversation_store, store, session, appended, user_id=user_id)
        return appended.entry

    @router.post("/sessions/{session_id}/journal", response_model=SessionJournalEntry)
    async def append_session_journal_entry(
        request: Request, session_id: str, body: SessionJournalAppendRequest
    ) -> SessionJournalEntry:
        """Append an entry written by the session's agent; only its runner calls this."""
        session = await _agent_session(request, session_id)
        if body.kind == "plan" and set(body.content) - {"description"} != {"name", "steps"}:
            # Only the server can attribute a plan to a saved workflow snapshot.
            raise OmnigentError(
                "invalid_entry: a plan's content has name and steps, and optionally description",
                code=ErrorCode.INVALID_INPUT,
            )
        description = body.content.get("description") if body.kind == "plan" else None
        if isinstance(description, str) and description.strip():
            await asyncio.to_thread(_check_proposed_description, conversation_store, description)
        if body.workflow_id is not None:
            await _require_workflow(request, body.kind, body.workflow_id)
        entry = await _append(
            session,
            body.kind,
            body.content,
            idempotency_key=body.idempotency_key,
            step=body.step,
            user_id=get_user_id(request, auth_provider),
            workflow_id=body.workflow_id,
            start_run=body.start_run,
        )
        return (await asyncio.to_thread(_entries_to_responses, _store(), [entry]))[0]

    async def _require_workflow(request: Request, kind: str, workflow_id: str) -> None:
        """Refuse a workflow on anything but a plan, or one the caller can't run."""
        if kind != "plan":
            raise OmnigentError(
                "invalid_entry: only a plan names a workflow", code=ErrorCode.INVALID_INPUT
            )
        await _load_workflow(
            conversation_store,
            workflow_id,
            user_id=get_user_id(request, auth_provider),
            permission_store=permission_store,
        )

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
            (item for item in (plan.content["steps"] if plan else []) if item["id"] == step),
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
            user_id=get_user_id(request, auth_provider),
        )
        elicitation_id = workflow_elicitation_id(session.id, body.idempotency_key)
        event = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=ElicitationRequestParams(
                mode="form",
                message=f"Waiting for you on {task['name']}",
                phase="workflow_request_input",
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
