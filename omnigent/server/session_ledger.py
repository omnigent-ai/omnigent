"""Session ledger API: what a session's agent plans and records.

A session's ledger is append-only. Its agent writes plans, step statuses and
notes through its ``sys_ledger_*`` tools, which the session's runner sends
here. Entries are attributed to the automation run the session serves, named
by its :data:`~omnigent.stores.conversation_store.SCHEDULED_TASK_RUN_LABEL_KEY`
label. People follow a ledger's plan in the session's Tasks tab.

Routes, under ``/v1``:

* ``GET /sessions/{session_id}/ledger`` returns one page, with the automation
  runs its entries name (read access).
* ``POST /sessions/{session_id}/ledger`` appends an entry. Only the session's
  runner writes, so the call must carry the orchestration source header naming
  the session (edit access).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Literal

from fastapi import APIRouter, Query, Request

from omnigent.entities import Conversation
from omnigent.entities.session_ledger import (
    LEDGER_PAGE_MAX,
    InvalidLedgerEntryError,
    LedgerEntry,
    LedgerEntryTooLargeError,
    StepNotReadyError,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.server.schemas import (
    LedgerEntryKind,
    SessionLedgerAppendRequest,
    SessionLedgerAuthor,
    SessionLedgerEntry,
    SessionLedgerPage,
)
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_ledger_store import LedgerSessionNotFoundError, SessionLedgerStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

_logger = logging.getLogger(__name__)

# Kinds only Omnigent writes; a plan copied from a user-drawn workflow is Omnigent's too.
_OMNIGENT_KINDS = frozenset({"dispatch", "dispatch_result", "plan_status"})

# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, SessionLedgerStore] = {}
_stores_lock = threading.Lock()


def open_store(conversation_store: object) -> SessionLedgerStore | None:
    """Return the ledger store beside the session metadata, or ``None`` without SQL.

    :param conversation_store: The server's conversation store; ledgers live in
        the database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = SessionLedgerStore(location)
            except Exception:
                _logger.warning("Session ledger storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


def _same_session(left: str, right: str) -> bool:
    try:
        return uuid_to_bytes(left.strip()) == uuid_to_bytes(right)
    except InvalidUuidError:
        return False


def entry_to_response(entry: LedgerEntry) -> SessionLedgerEntry:
    """Serialize an entry; its author is derived from its kind and a plan's source."""
    omnigent = entry.kind in _OMNIGENT_KINDS or (
        entry.kind == "plan" and entry.payload.get("source") == "workflow"
    )
    return SessionLedgerEntry(
        id=entry.id,
        position=entry.position,
        created_at=entry.created_at,
        scheduled_task_run_id=entry.scheduled_task_run_id,
        step=entry.step,
        author=SessionLedgerAuthor(type="omnigent" if omnigent else "agent"),
        kind=entry.kind,  # type: ignore[arg-type]
        payload=entry.payload,
    )


def create_session_ledger_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the session ledger router, mounted under ``/v1``.

    :param conversation_store: The server's conversation store. Ledgers are
        stored in its database; without a SQL backend every route answers 404.
    :param auth_provider: Identifies the caller; ``None`` in single-user mode.
    :param permission_store: Session grants; ``None`` disables enforcement.
    """
    router = APIRouter()

    def _store() -> SessionLedgerStore:
        store = open_store(conversation_store)
        if store is None:
            raise OmnigentError(
                "Session ledgers are not available on this server", code=ErrorCode.NOT_FOUND
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

    @router.get("/sessions/{session_id}/ledger", response_model=SessionLedgerPage)
    async def list_session_ledger(
        request: Request,
        session_id: str,
        limit: int = Query(100, ge=1, le=LEDGER_PAGE_MAX),
        after: str | None = None,
        before: str | None = None,
        order: Literal["asc", "desc"] = "asc",
        kind: LedgerEntryKind | None = None,
        step: str | None = None,
    ) -> SessionLedgerPage:
        """One page of the session's ledger by position; requires read access.

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
        return SessionLedgerPage(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=page.has_more,
        )

    @router.post("/sessions/{session_id}/ledger", response_model=SessionLedgerEntry)
    async def append_session_ledger_entry(
        request: Request, session_id: str, body: SessionLedgerAppendRequest
    ) -> SessionLedgerEntry:
        """Append an entry written by the session's agent; only its runner calls this."""
        session = await _session(request, session_id, LEVEL_EDIT)
        source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if source is None or not _same_session(source, session.id):
            raise OmnigentError(
                "Only the session's own agent writes its ledger", code=ErrorCode.FORBIDDEN
            )
        if body.kind == "plan" and set(body.payload) != {"name", "steps"}:
            # The agent's plans are its own; copies of drawn workflows come later.
            raise OmnigentError(
                "invalid_entry: a plan payload has exactly two fields, name and steps",
                code=ErrorCode.INVALID_INPUT,
            )
        store = _store()
        try:
            appended = await asyncio.to_thread(
                lambda: store.append(
                    session.id,
                    body.kind,
                    body.payload,
                    idempotency_key=body.idempotency_key,
                    step=body.step,
                    scheduled_task_run_id=_run_id(session),
                )
            )
        except LedgerSessionNotFoundError as exc:
            raise session_not_found() from exc
        except LedgerEntryTooLargeError as exc:
            raise OmnigentError(f"entry_too_large: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        except StepNotReadyError as exc:
            raise OmnigentError(f"step_not_ready: {exc}", code=ErrorCode.CONFLICT) from exc
        except InvalidLedgerEntryError as exc:
            raise OmnigentError(f"invalid_entry: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        return entry_to_response(appended.entry)

    return router
