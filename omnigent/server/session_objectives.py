"""Session objectives API: what an agent set out to do, and how it went.

The session's agent records objectives and outcomes through its
``sys_record_objective`` and ``sys_record_outcome`` tools, which the runner
sends here. An automation run's prompt goes into a session that carries the
run's ID in :data:`~omnigent.stores.conversation_store.SCHEDULED_TASK_RUN_LABEL_KEY`,
stamped before the prompt is sent; what the agent records then is attributed
to that run, and the run history shows it.

Routes, under ``/v1``:

* ``GET /sessions/{session_id}/objectives`` lists them, oldest first, a page
  at a time (read access).
* ``POST /sessions/{session_id}/objectives`` records an objective (edit access).
* ``PUT /sessions/{session_id}/objectives/outcome`` records an outcome for the
  named objective, or the latest one (edit access).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from omnigent.entities import Conversation
from omnigent.entities.session_objective import (
    OBJECTIVES_PAGE_MAX,
    SessionObjective,
    normalize_objective,
    normalize_outcome,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SCHEDULED_TASK_RUN_LABEL_KEY
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.session_objective_store import (
    SessionObjectiveLimitError,
    SessionObjectiveNotFoundError,
    SessionObjectiveStore,
)
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes

_logger = logging.getLogger(__name__)


def _strict_uuid(value: str) -> str:
    """The bare hex form of a UUID; raises :class:`InvalidUuidError` for anything else."""
    return uuid_to_bytes(value).hex()


# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, SessionObjectiveStore] = {}
_stores_lock = threading.Lock()


def open_store(conversation_store: object) -> SessionObjectiveStore | None:
    """Return the objective store beside the session metadata, or ``None`` without SQL.

    :param conversation_store: The server's conversation store; objectives live
        in the database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = SessionObjectiveStore(location)
            except Exception:
                _logger.warning("Session objective storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


class RecordObjectiveRequest(BaseModel):
    """Body for ``POST /v1/sessions/{session_id}/objectives``."""

    model_config = ConfigDict(extra="forbid")

    objective: str

    @model_validator(mode="after")
    def _check(self) -> RecordObjectiveRequest:
        self.objective = normalize_objective(self.objective)
        return self


class RecordOutcomeRequest(BaseModel):
    """Body for ``PUT /v1/sessions/{session_id}/objectives/outcome``.

    ``objective_id`` names the objective to resolve; without it the latest one
    is resolved (the latest of the automation run the session serves, if any).
    """

    model_config = ConfigDict(extra="forbid")

    success: StrictBool
    emoji: str
    text: str
    objective_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> RecordOutcomeRequest:
        self.emoji, self.text = normalize_outcome(self.emoji, self.text)
        if self.objective_id is not None:
            try:
                self.objective_id = _strict_uuid(self.objective_id)
            except InvalidUuidError as exc:
                raise ValueError("objective_id must be an objective's ID") from exc
        return self


def objective_to_response(objective: SessionObjective) -> dict[str, Any]:
    """Serialize an objective; times are Unix epoch seconds."""
    outcome = objective.outcome
    return {
        "id": objective.id,
        "session_id": objective.conversation_id,
        "objective": objective.objective,
        "created_at": objective.created_at // 1_000_000,
        "scheduled_task_run_id": objective.scheduled_task_run_id,
        "outcome": None
        if outcome is None
        else {
            "success": outcome.success,
            "emoji": outcome.emoji,
            "text": outcome.text,
            "recorded_at": outcome.recorded_at // 1_000_000,
        },
    }


def create_session_objectives_router(
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the session objectives router.

    :param conversation_store: The server's conversation store. Objectives are
        stored in its database; without a SQL backend every route answers 404.
    :param auth_provider: Identifies the caller; ``None`` in single-user mode.
    :param permission_store: Session grants; ``None`` disables enforcement.
    """
    router = APIRouter()

    def _store() -> SessionObjectiveStore:
        store = open_store(conversation_store)
        if store is None:
            raise OmnigentError(
                "Session objectives are not available on this server", code=ErrorCode.NOT_FOUND
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

    def _require_own_session(request: Request, session: Conversation) -> None:
        """A call relayed for a session's agent may record only for that session."""
        source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if source is None:
            return
        try:
            same = _strict_uuid(source.strip()) == _strict_uuid(session.id)
        except InvalidUuidError:
            same = False
        if not same:
            raise OmnigentError(
                "An agent can record objectives only for its own session",
                code=ErrorCode.FORBIDDEN,
            )

    def _run_id(session: Conversation) -> str | None:
        value = session.labels.get(SCHEDULED_TASK_RUN_LABEL_KEY)
        if not value:
            return None
        try:
            return _strict_uuid(value)
        except InvalidUuidError:
            _logger.warning("Session %s names an invalid automation run", session.id)
            return None

    @router.get("/sessions/{session_id}/objectives")
    async def list_session_objectives(
        request: Request,
        session_id: str,
        limit: int = Query(50, ge=1, le=OBJECTIVES_PAGE_MAX),
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List the session's objectives, oldest first; requires read access."""
        session = await _session(request, session_id, LEVEL_READ)
        store = _store()
        try:
            page = await asyncio.to_thread(
                store.list_objectives, session.id, limit=limit, cursor=cursor
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return {
            "data": [objective_to_response(objective) for objective in page.data],
            "next_cursor": page.next_cursor,
        }

    @router.post("/sessions/{session_id}/objectives")
    async def record_session_objective(
        request: Request, session_id: str, body: RecordObjectiveRequest
    ) -> dict[str, Any]:
        """Record an objective; it becomes the session's latest. Requires edit access."""
        session = await _session(request, session_id, LEVEL_EDIT)
        _require_own_session(request, session)
        try:
            objective = await asyncio.to_thread(
                _store().record_objective,
                session.id,
                body.objective,
                scheduled_task_run_id=_run_id(session),
            )
        except SessionObjectiveNotFoundError as exc:
            raise session_not_found() from exc
        except SessionObjectiveLimitError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        return objective_to_response(objective)

    @router.put("/sessions/{session_id}/objectives/outcome")
    async def record_session_objective_outcome(
        request: Request, session_id: str, body: RecordOutcomeRequest
    ) -> dict[str, Any]:
        """Record how an objective went, replacing an earlier outcome for it.

        Requires edit access. 404 when ``objective_id`` names no objective of
        the session.
        """
        session = await _session(request, session_id, LEVEL_EDIT)
        _require_own_session(request, session)
        try:
            objective = await asyncio.to_thread(
                lambda: _store().record_outcome(
                    session.id,
                    success=body.success,
                    emoji=body.emoji,
                    text=body.text,
                    objective_id=body.objective_id,
                    scheduled_task_run_id=_run_id(session),
                )
            )
        except SessionObjectiveNotFoundError as exc:
            raise OmnigentError(
                "Objective not found in this session", code=ErrorCode.NOT_FOUND
            ) from exc
        except SessionObjectiveLimitError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        return objective_to_response(objective)

    return router
