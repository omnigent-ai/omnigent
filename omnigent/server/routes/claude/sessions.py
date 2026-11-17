"""Claude-native session routes layered on the main Sessions API."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from pydantic import ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_plugins import CLAUDE_NATIVE_CODING_AGENT
from omnigent.runner.routing import RunnerRouter
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import require_access as _require_access
from omnigent.server.routes.sessions import (
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _forward_session_change_to_runner,
)
from omnigent.server.schemas import ClaudeGoalReadResponse
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

_CLAUDE_GOAL_GET_EVENT = {"type": "claude_goal_get"}
_UNAVAILABLE = ClaudeGoalReadResponse(goal=None, available=False)


async def _read_claude_goal_from_runner(
    session_id: str, runner_router: RunnerRouter | None
) -> ClaudeGoalReadResponse:
    """
    Ask the session's runner for the goal in its Claude transcript.

    An absent or older runner, or a reply in any other shape, reads as
    unavailable so the browser keeps its last snapshot.

    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param runner_router: Router resolving the session's runner client.
    :returns: The runner's goal read, or an unavailable read.
    """
    try:
        result = await _forward_session_change_to_runner(
            session_id, runner_router, dict(_CLAUDE_GOAL_GET_EVENT)
        )
    except OmnigentError as exc:
        _logger.debug("Claude goal read skipped for session=%s: %s", session_id, exc)
        return _UNAVAILABLE
    if result is None or result.status_code != 200:
        return _UNAVAILABLE
    try:
        return ClaudeGoalReadResponse.model_validate(json.loads(result.body))
    except (ValueError, ValidationError):
        return _UNAVAILABLE


def register_claude_session_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Register Claude-native session subresources on the shared router."""

    @router.get(
        "/sessions/{session_id}/claude_goal",
        response_model=ClaudeGoalReadResponse,
    )
    async def get_claude_goal(request: Request, session_id: str) -> ClaudeGoalReadResponse:
        """
        Read the ``/goal`` of a Claude-native session from its runner.

        Claude Code sets and clears goals through its own ``/goal`` command,
        so this route only reads them.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session id, e.g. ``"conv_abc123"``.
        :returns: The current goal and whether the runner could read it.
        :raises OmnigentError: 404 for a missing session or 400 for a session
            that is not Claude-native.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if (
            conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
            != CLAUDE_NATIVE_CODING_AGENT.wrapper_label
        ):
            raise OmnigentError(
                "claude_goal is only supported for claude-native sessions",
                code=ErrorCode.INVALID_INPUT,
            )
        return await _read_claude_goal_from_runner(session_id, runner_router)
