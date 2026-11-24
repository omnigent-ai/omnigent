"""Replace a session's runner between turns so it loads the current tool code.

A runner builds its tool list and tool implementations from the code it
imported at startup, so a long-lived session never sees tools added or changed
later. A reload stops the session's runner once the session is idle and
relaunches it the same way ``retry_session`` does. The new runner imports the
current code and the native harness resumes the same conversation.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.routes._sessions.common import (
    _logger,
    _session_background_task_count_cache,
    _session_status_cache,
)
from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY

if TYPE_CHECKING:
    from fastapi import Request

    from omnigent.entities import Conversation
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.permission_store import PermissionStore

_RELOAD_TOOLS_POLL_S = 2.0
# Consecutive idle polls before reloading: rides out the gap between a turn
# ending and the next queued input starting its own turn.
_RELOAD_TOOLS_IDLE_POLLS = 2
_RELOAD_TOOLS_WAIT_S = 6 * 60 * 60
_MID_TURN_STATUSES = ("running", "waiting")
_RUNNER_STATUS_PAGE = 200

# One scheduled reload per session on this replica; a repeat request replaces it.
_pending_tool_reloads: dict[str, asyncio.Task[None]] = {}


def authorize_tool_reload(
    request: Request | Any,
    user_id: str | None,
    conv: Conversation,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
) -> str:
    """
    Check who may reload *conv*'s tools.

    The owner may reload any of their sessions. An agent, identified by its
    bound runner and orchestration source header, may reload itself or a
    session it created. Nothing else may.

    :returns: ``"owner"`` or ``"agent"``, the kind of caller.
    :raises OmnigentError: ``FORBIDDEN`` for any other caller.
    """
    from omnigent.server.session_orchestration import (
        require_owned_session,
        require_private_bound_source,
    )

    headers = getattr(request, "headers", None)
    source_header = headers.get(ORCHESTRATION_SOURCE_HEADER) if headers is not None else None
    runner_header = headers.get(RUNNER_TUNNEL_TOKEN_HEADER) if headers is not None else None
    if not user_id or permission_store is None:
        raise OmnigentError("Reloading tools requires an owner", code=ErrorCode.FORBIDDEN)
    if source_header:
        source = require_private_bound_source(
            request, user_id, conversation_store, permission_store
        )
        require_owned_session(conv.id, user_id, conversation_store, permission_store)
        created_by = (conv.labels or {}).get(CREATED_BY_SESSION_LABEL_KEY)
        if source.id != conv.id and created_by != source.id:
            raise OmnigentError(
                "An agent can reload only its own session or a session it created",
                code=ErrorCode.FORBIDDEN,
            )
        return "agent"
    if runner_header or not hasattr(request, "url"):
        raise OmnigentError(
            "Runners must name their source session to reload tools",
            code=ErrorCode.FORBIDDEN,
        )
    require_owned_session(conv.id, user_id, conversation_store, permission_store)
    return "owner"


def _session_settled(session_id: str, db_status: str | None) -> bool:
    """Whether *session_id* has no turn, queued input or background work."""
    from omnigent.server.routes._sessions.orchestration import _session_is_starting

    status = _session_status_cache.get(session_id, db_status)
    if status in _MID_TURN_STATUSES or _session_is_starting(session_id):
        return False
    return not _session_background_task_count_cache.get(session_id)


async def _runner_settled(conv: Conversation, conversation_store: ConversationStore) -> bool:
    """Whether *conv* and every other session on its runner are settled."""
    if not _session_settled(conv.id, conv.live_status):
        return False
    if not conv.runner_id:
        return True
    after: str | None = None
    while True:
        page = await asyncio.to_thread(
            conversation_store.list_runner_session_statuses,
            conv.runner_id,
            after=after,
            limit=_RUNNER_STATUS_PAGE,
        )
        for related_id, live_status in page:
            if not _session_settled(related_id, live_status):
                return False
        if len(page) < _RUNNER_STATUS_PAGE:
            return True
        after = page[-1][0]


async def require_reloadable(conv: Conversation, conversation_store: ConversationStore) -> None:
    """
    Refuse sessions whose runner a reload cannot replace.

    :raises OmnigentError: ``INVALID_INPUT`` without a host runner of its own.
    """
    from omnigent.server.routes._sessions.helpers import _owns_host_runner

    if not conv.host_id or not conv.runner_id:
        raise OmnigentError(
            "Reloading tools needs a session running on a host", code=ErrorCode.INVALID_INPUT
        )
    if not await _owns_host_runner(conv, conversation_store):
        raise OmnigentError(
            "This session shares its parent's runner; reload the parent session instead",
            code=ErrorCode.INVALID_INPUT,
        )


def tool_reload_pending(session_id: str) -> bool:
    """Whether a reload is waiting for *session_id* to become idle."""
    task = _pending_tool_reloads.get(session_id)
    return task is not None and not task.done()


def schedule_tool_reload(
    request: Request | Any,
    session_id: str,
    *,
    user_id: str | None,
    conversation_store: ConversationStore,
    runner_router: Any,
    permission_store: PermissionStore | None,
) -> None:
    """
    Reload *session_id*'s runner once it and its runner's sessions are idle.

    Replaces an earlier pending reload of the same session. The wait lives in
    this replica's memory, so a server restart drops a reload that has not fired.
    """
    previous = _pending_tool_reloads.pop(session_id, None)
    if previous is not None and not previous.done():
        previous.cancel()

    async def _reload_when_idle() -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _RELOAD_TOOLS_WAIT_S
        idle_polls = 0
        while True:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None or conv.archived:
                return
            idle_polls = idle_polls + 1 if await _runner_settled(conv, conversation_store) else 0
            if idle_polls >= _RELOAD_TOOLS_IDLE_POLLS:
                break
            if loop.time() >= deadline:
                _logger.warning(
                    "Tool reload for session %s gave up: it never became idle",
                    session_id,
                    extra={"session_id": session_id},
                )
                return
            await asyncio.sleep(_RELOAD_TOOLS_POLL_S)
        await _reload_now(
            request,
            conv,
            user_id=user_id,
            conversation_store=conversation_store,
            runner_router=runner_router,
            permission_store=permission_store,
        )

    task = asyncio.create_task(_reload_when_idle(), name=f"reload-tools:{session_id}")
    _pending_tool_reloads[session_id] = task

    def _done(finished: asyncio.Task[None]) -> None:
        if _pending_tool_reloads.get(session_id) is finished:
            del _pending_tool_reloads[session_id]
        if not finished.cancelled() and finished.exception() is not None:
            _logger.warning(
                "Tool reload for session %s failed",
                session_id,
                exc_info=finished.exception(),
                extra={"session_id": session_id},
            )

    task.add_done_callback(_done)


async def _reload_now(
    request: Request | Any,
    conv: Conversation,
    *,
    user_id: str | None,
    conversation_store: ConversationStore,
    runner_router: Any,
    permission_store: PermissionStore | None,
) -> None:
    """Stop *conv*'s idle runner and relaunch it with the current code."""
    from omnigent.server.routes import sessions as _facade
    from omnigent.server.routes._sessions.orchestration import _stop_host_runner_intentionally
    from omnigent.server.routes.sessions.routes_events import _retry_session_single_flight

    session_id = conv.id
    assert conv.host_id is not None and conv.runner_id is not None
    app_state = request.app.state
    _logger.info(
        "Reloading tools for session %s: replacing runner %s",
        session_id,
        conv.runner_id,
        extra={"session_id": session_id},
    )
    # Ends the native pane; the conversation stays in the harness transcript.
    await _facade._stop_session_via_runner(session_id, runner_router)
    stopped = await _stop_host_runner_intentionally(
        session_id,
        conv.host_id,
        conv.runner_id,
        getattr(app_state, "host_registry", None),
        conversation_store,
        host_session_recoverer=getattr(app_state, "host_session_recoverer", None),
    )
    if not stopped:
        _logger.warning(
            "Tool reload for session %s: host did not confirm the runner stopped",
            session_id,
            extra={"session_id": session_id},
        )
    fresh = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    await _retry_session_single_flight(
        request=request,
        session_id=session_id,
        conversation=fresh,
        conversation_store=conversation_store,
        runner_router=runner_router,
        user_id=user_id,
        permission_store=permission_store,
    )
    startup = getattr(app_state, "startup_inputs", None)
    if startup is not None and await startup.retry(session_id):
        await startup.deliver_pending(request.app, session_id)
