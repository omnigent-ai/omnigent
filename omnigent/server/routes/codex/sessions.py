"""Codex-specific session routes layered on the main Sessions API."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import State

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_plugins import CODEX_NATIVE_CODING_AGENT
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.host_registry import RunnerExitReports
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import require_access as _require_access
from omnigent.server.routes.sessions import (
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _HOST_BOUND_RUNNER_CONNECT_GRACE_S,
    _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
    _ensure_runner_session_initialized,
    _forward_session_change_to_runner,
    _launch_runner_on_host,
    _maybe_relaunch_managed_sandbox,
    _RunnerForwardResult,
    _wait_for_runner_client,
)
from omnigent.server.schemas import (
    ClearCodexGoalResponse,
    CodexGoalResponse,
    SetCodexGoalRequest,
    UpdateCodexGoalStatusRequest,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)


_CODEX_NATIVE_GOAL_ERROR = "codex_native_goal_failed"


def _codex_goal_error(status_code: int, *, detail: str) -> JSONResponse:
    """Build a public Codex goal route error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": _CODEX_NATIVE_GOAL_ERROR, "message": detail}},
    )


def _runner_error_payload(body: str) -> dict[str, Any] | None:
    """Return a structured runner ``{error, detail}`` body if present."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    detail = payload.get("detail")
    if isinstance(error, str) and isinstance(detail, str):
        return {"error": error, "detail": detail}
    return None


def _require_codex_goal_runner_payload(
    session_id: str,
    *,
    action: str,
    runner_result: _RunnerForwardResult | None,
) -> dict[str, Any] | JSONResponse:
    """
    Return the JSON payload from a required Codex goal runner forward.

    Codex goal state lives in Codex app-server, so AP goal routes cannot fall
    back to persisted Omnigent labels. A missing runner, rejected control
    event, or malformed runner body means the UI must keep its prior goal view
    and surface an error.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param action: Human-readable goal operation, e.g. ``"read"``.
    :param runner_result: HTTP result returned by the runner, or ``None``
        when no runner could be reached.
    :returns: Parsed runner JSON payload, e.g. ``{"goal": None}``, or a
        prebuilt error response for transport/upstream failures.
    """
    if runner_result is None:
        return _codex_goal_error(
            503,
            detail=(
                f"Could not {action} Codex goal: no live Codex runner is available "
                f"for session {session_id!r}. Reconnect the session and try again."
            ),
        )
    if not 200 <= runner_result.status_code < 300:
        structured_error = _runner_error_payload(runner_result.body)
        if structured_error is not None:
            return JSONResponse(
                status_code=runner_result.status_code,
                content=structured_error,
            )
        return _codex_goal_error(
            502,
            detail=(
                f"Could not {action} Codex goal: runner returned malformed "
                f"error response with status {runner_result.status_code} "
                f"for session {session_id!r}."
            ),
        )
    try:
        payload = json.loads(runner_result.body)
    except json.JSONDecodeError as exc:
        del exc
        return _codex_goal_error(
            502,
            detail=f"Could not {action} Codex goal: runner returned a malformed response.",
        )
    if not isinstance(payload, dict):
        return _codex_goal_error(
            502,
            detail=f"Could not {action} Codex goal: runner returned a malformed response.",
        )
    return payload


async def _post_codex_goal_event_to_runner(
    session_id: str,
    runner_client: httpx.AsyncClient,
    event: dict[str, Any],
) -> _RunnerForwardResult | None:
    """
    POST a Codex goal control event to a known runner client.

    Used after an auto-relaunch has already resolved a runner client. The
    generic forward helper intentionally re-resolves through the router; this
    helper keeps the retry attached to the runner that just connected.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_client: Runner client resolved for the session.
    :param event: Goal control event, e.g. ``{"type": "goal_get"}``.
    :returns: Runner status/body, or ``None`` if the runner disappeared
        during the retry.
    """
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.warning(
            "Codex goal retry failed after runner relaunch for session=%s type=%r",
            session_id,
            event.get("type"),
            exc_info=True,
        )
        return None
    return _RunnerForwardResult(status_code=resp.status_code, body=resp.text)


async def _wait_for_existing_codex_goal_runner(
    *,
    session_id: str,
    conv: Conversation,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    runner_exit_reports: RunnerExitReports | None,
) -> httpx.AsyncClient | None:
    """
    Wait briefly for a currently-bound runner before relaunching.

    A freshly started host session can have ``runner_id`` persisted before the
    tunnel registration reaches this server. Goal-open should not spawn a
    duplicate runner in that normal startup gap.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row carrying the current ``runner_id``.
    :param runner_router: Runner router for resolving a connected runner.
    :param tunnel_registry: Runner tunnel registry, or ``None`` in tests.
    :param runner_exit_reports: Host runner-exit reports used to stop waiting
        once the runner is known dead.
    :returns: Connected runner client, or ``None`` if the grace wait expires.
    """
    if conv.runner_id is None or _HOST_BOUND_RUNNER_CONNECT_GRACE_S <= 0:
        return None
    return await _wait_for_runner_client(
        session_id,
        runner_router,
        tunnel_registry,
        runner_id=conv.runner_id,
        timeout_s=_HOST_BOUND_RUNNER_CONNECT_GRACE_S,
        runner_exit_reports=runner_exit_reports,
    )


async def _start_codex_goal_runner_on_bound_host(
    *,
    session_id: str,
    conv: Conversation,
    app_state: State,
    conversation_store: ConversationStore,
) -> str | None:
    """
    Ask the session's existing host binding to spawn a runner.

    This does not accept caller-supplied host or workspace input; it only
    reuses the binding already stored on the session. External hosts must be
    online. Managed hosts can relaunch their sandbox generation when the host
    tunnel is gone.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Host-bound conversation row.
    :param app_state: FastAPI app state carrying host registries and managed
        launch trackers.
    :param conversation_store: Store used for runner-id rotation.
    :returns: Runner id expected to connect, or ``None`` if no launch was
        possible.
    :raises OmnigentError: If the host reports a non-retryable harness
        configuration failure or the session disappears.
    """
    host_registry = getattr(app_state, "host_registry", None)
    if host_registry is None:
        return None
    host_conn = host_registry.get(conv.host_id)
    if host_conn is not None:
        launch_attempt = await _launch_runner_on_host(
            conv,
            conversation_store,
            host_registry,
            host_conn,
        )
        if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
            raise OmnigentError(
                launch_attempt.error or "host failed to launch runner: harness not configured",
                code=ErrorCode.HARNESS_NOT_CONFIGURED,
            )
        return launch_attempt.runner_id
    if not await _maybe_relaunch_managed_sandbox(
        session_id=session_id,
        conv=conv,
        app_state=app_state,
        conversation_store=conversation_store,
    ):
        return None
    refreshed_conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        session_id,
    )
    if refreshed_conv is None:
        raise OmnigentError(
            "Session not found",
            code=ErrorCode.NOT_FOUND,
        )
    return refreshed_conv.runner_id


async def _initialize_codex_goal_runner(
    session_id: str,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
) -> None:
    """
    Run the session-init handshake before retrying a goal RPC.

    The Codex goal handlers require the Codex-native app-server bridge loaded
    inside the runner. Session init creates that bridge, matching the message
    relaunch path's ordering.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_client: Connected runner client to initialize.
    :param conversation_store: Store used to reload the post-relaunch
        conversation row.
    :returns: None.
    :raises OmnigentError: If the session disappeared before init.
    """
    refreshed_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if refreshed_conv is None:
        raise OmnigentError(
            "Session not found",
            code=ErrorCode.NOT_FOUND,
        )
    await _ensure_runner_session_initialized(session_id, refreshed_conv, runner_client)


async def _launch_runner_for_codex_goal(
    *,
    session_id: str,
    conv: Conversation,
    request: Request,
    user_id: str | None,
    runner_router: RunnerRouter | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    runner_exit_reports: RunnerExitReports | None,
) -> httpx.AsyncClient | None:
    """
    Relaunch an existing host-bound Codex session for goal controls.

    Goal state lives in Codex app-server, so opening the Goal dialog needs a
    live runner even when no user message is being sent. This mirrors the
    message-dispatch relaunch path but does not let callers choose a host or
    workspace: it only wakes the session's existing binding.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Codex-native conversation row.
    :param request: Incoming FastAPI request; ``request.app.state`` carries
        host and tunnel registries.
    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param runner_router: Runner router for resolving a connected runner.
    :param conversation_store: Store used for runner-id rotation.
    :param permission_store: Permission store for the edit-level wake gate.
    :param runner_exit_reports: Host runner-exit reports used to stop waiting
        once a launched runner is known dead.
    :returns: Connected runner client, or ``None`` when no runner can be
        launched or reached.
    :raises OmnigentError: If the caller lacks edit access or the host
        reports a non-retryable harness configuration failure.
    """
    if conv.host_id is None:
        return None
    await _require_access(
        user_id,
        session_id,
        LEVEL_EDIT,
        permission_store,
        conversation_store,
    )
    tunnel_registry = getattr(request.app.state, "tunnel_registry", None)
    runner_client = await _wait_for_existing_codex_goal_runner(
        session_id=session_id,
        conv=conv,
        runner_router=runner_router,
        tunnel_registry=tunnel_registry,
        runner_exit_reports=runner_exit_reports,
    )
    if runner_client is not None:
        return runner_client
    launched_runner_id = await _start_codex_goal_runner_on_bound_host(
        session_id=session_id,
        conv=conv,
        app_state=request.app.state,
        conversation_store=conversation_store,
    )
    runner_client = await _wait_for_runner_client(
        session_id,
        runner_router,
        tunnel_registry,
        runner_id=launched_runner_id,
        timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
        runner_exit_reports=runner_exit_reports,
    )
    if runner_client is None:
        return None
    await _initialize_codex_goal_runner(session_id, runner_client, conversation_store)
    return runner_client


async def _forward_codex_goal_event(
    *,
    session_id: str,
    conv: Conversation,
    event: dict[str, Any],
    request: Request,
    user_id: str | None,
    runner_router: RunnerRouter | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    runner_exit_reports: RunnerExitReports | None,
) -> _RunnerForwardResult | None:
    """
    Forward a Codex goal event, waking a host-bound runner if needed.

    The first attempt preserves the cheap live-runner path. If no runner can
    be resolved, the helper wakes the session's existing host-bound runner
    using the same relaunch mechanism as message dispatch, initializes the
    session on the new runner, then retries the goal event.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Codex-native conversation row.
    :param event: Goal control event, e.g. ``{"type": "goal_get"}``.
    :param request: Incoming FastAPI request.
    :param user_id: Authenticated user id, or ``None`` when auth is disabled.
    :param runner_router: Runner router for resolving session clients.
    :param conversation_store: Store used for host relaunch.
    :param permission_store: Permission store for the relaunch edit gate.
    :param runner_exit_reports: Host runner-exit report store.
    :returns: Runner forward result, or ``None`` if no runner could be
        reached.
    """
    runner_result = await _forward_session_change_to_runner(
        session_id,
        runner_router,
        event,
    )
    if runner_result is not None:
        return runner_result
    if event.get("type") == "goal_get":
        return None
    runner_client = await _launch_runner_for_codex_goal(
        session_id=session_id,
        conv=conv,
        request=request,
        user_id=user_id,
        runner_router=runner_router,
        conversation_store=conversation_store,
        permission_store=permission_store,
        runner_exit_reports=runner_exit_reports,
    )
    if runner_client is None:
        return None
    return await _post_codex_goal_event_to_runner(session_id, runner_client, event)


async def _require_codex_native_goal_session(
    session_id: str,
    conversation_store: ConversationStore,
) -> Conversation:
    """
    Resolve and validate the Codex-native session targeted by a goal route.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conversation_store: Store used to load the session row.
    :returns: The validated conversation.
    :raises OmnigentError: 404 when the session is missing, or 400 when it is
        not a codex-native UI wrapper session.
    """
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None:
        raise OmnigentError(
            "Session not found",
            code=ErrorCode.NOT_FOUND,
        )
    if (
        conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        != CODEX_NATIVE_CODING_AGENT.wrapper_label
    ):
        raise OmnigentError(
            "codex_goal is only supported for codex-native sessions",
            code=ErrorCode.INVALID_INPUT,
        )
    return conv


def register_codex_session_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    runner_exit_reports: RunnerExitReports | None,
) -> None:
    """Register Codex-native session subresources on the shared router."""

    @router.get(
        "/sessions/{session_id}/codex_goal",
        response_model=CodexGoalResponse,
    )
    async def get_codex_goal(
        request: Request,
        session_id: str,
    ) -> CodexGoalResponse | Response:
        """
        Read the current Codex app-server goal for a Codex-native session.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: Current Codex goal state, or ``goal=None`` when no goal is
            set.
        :raises OmnigentError: 400 for non-Codex sessions, 404 for missing
            sessions, or 503 when no live Codex runner can read the goal.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        conv = await _require_codex_native_goal_session(session_id, conversation_store)
        runner_result = await _forward_codex_goal_event(
            session_id=session_id,
            conv=conv,
            event={"type": "goal_get"},
            request=request,
            user_id=user_id,
            runner_router=runner_router,
            conversation_store=conversation_store,
            permission_store=permission_store,
            runner_exit_reports=runner_exit_reports,
        )
        if runner_result is None:
            return CodexGoalResponse(goal=None)
        runner_payload = _require_codex_goal_runner_payload(
            session_id,
            action="read",
            runner_result=runner_result,
        )
        if isinstance(runner_payload, JSONResponse):
            return runner_payload
        try:
            return CodexGoalResponse.model_validate(runner_payload)
        except ValueError as exc:
            del exc
            return _codex_goal_error(
                502,
                detail="Could not read Codex goal: runner returned a malformed response.",
            )

    @router.put(
        "/sessions/{session_id}/codex_goal",
        response_model=CodexGoalResponse,
    )
    async def set_codex_goal(
        request: Request,
        session_id: str,
        body: SetCodexGoalRequest,
    ) -> CodexGoalResponse | Response:
        """
        Set or replace the current Codex app-server goal.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param body: Goal objective, optional token budget, and optional
            user-selected status.
        :returns: Current Codex goal state after the update.
        :raises OmnigentError: 400 for non-Codex sessions or blank
            objectives, 404 for missing sessions, or 503 when no live Codex
            runner can update the goal.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        conv = await _require_codex_native_goal_session(session_id, conversation_store)
        objective = body.objective.strip()
        if not objective:
            raise OmnigentError(
                "codex_goal objective must be non-empty",
                code=ErrorCode.INVALID_INPUT,
            )
        event: dict[str, Any] = {
            "type": "goal_set",
            "objective": objective,
        }
        if "token_budget" in body.model_fields_set:
            event["token_budget"] = body.token_budget
        if body.status is not None:
            event["status"] = body.status
        runner_payload = _require_codex_goal_runner_payload(
            session_id,
            action="set",
            runner_result=await _forward_codex_goal_event(
                session_id=session_id,
                conv=conv,
                event=event,
                request=request,
                user_id=user_id,
                runner_router=runner_router,
                conversation_store=conversation_store,
                permission_store=permission_store,
                runner_exit_reports=runner_exit_reports,
            ),
        )
        if isinstance(runner_payload, JSONResponse):
            return runner_payload
        try:
            return CodexGoalResponse.model_validate(runner_payload)
        except ValueError as exc:
            del exc
            return _codex_goal_error(
                502,
                detail="Could not set Codex goal: runner returned a malformed response.",
            )

    @router.patch(
        "/sessions/{session_id}/codex_goal/status",
        response_model=CodexGoalResponse,
    )
    async def update_codex_goal_status(
        request: Request,
        session_id: str,
        body: UpdateCodexGoalStatusRequest,
    ) -> CodexGoalResponse | Response:
        """
        Pause or resume the current Codex app-server goal.

        Codex exposes this through ``thread/goal/set`` with only a status
        field. The Omnigent API keeps that distinct from objective/budget
        edits so callers can implement Pause/Resume controls without
        resending the goal text.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param body: Target status. ``"paused"`` pauses the goal and
            ``"active"`` resumes it.
        :returns: Current Codex goal state after the status update.
        :raises OmnigentError: 400 for non-Codex sessions, 404 for missing
            sessions, or 503 when no live Codex runner can update the goal.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        conv = await _require_codex_native_goal_session(session_id, conversation_store)
        runner_payload = _require_codex_goal_runner_payload(
            session_id,
            action="update status",
            runner_result=await _forward_codex_goal_event(
                session_id=session_id,
                conv=conv,
                event={"type": "goal_status", "status": body.status},
                request=request,
                user_id=user_id,
                runner_router=runner_router,
                conversation_store=conversation_store,
                permission_store=permission_store,
                runner_exit_reports=runner_exit_reports,
            ),
        )
        if isinstance(runner_payload, JSONResponse):
            return runner_payload
        try:
            return CodexGoalResponse.model_validate(runner_payload)
        except ValueError as exc:
            del exc
            return _codex_goal_error(
                502,
                detail="Could not update Codex goal status: runner returned a malformed response.",
            )

    @router.delete(
        "/sessions/{session_id}/codex_goal",
        response_model=ClearCodexGoalResponse,
    )
    async def clear_codex_goal(
        request: Request,
        session_id: str,
    ) -> ClearCodexGoalResponse | Response:
        """
        Clear the current Codex app-server goal.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: Whether Codex removed an existing goal.
        :raises OmnigentError: 400 for non-Codex sessions, 404 for missing
            sessions, or 503 when no live Codex runner can clear the goal.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        conv = await _require_codex_native_goal_session(session_id, conversation_store)
        runner_payload = _require_codex_goal_runner_payload(
            session_id,
            action="clear",
            runner_result=await _forward_codex_goal_event(
                session_id=session_id,
                conv=conv,
                event={"type": "goal_clear"},
                request=request,
                user_id=user_id,
                runner_router=runner_router,
                conversation_store=conversation_store,
                permission_store=permission_store,
                runner_exit_reports=runner_exit_reports,
            ),
        )
        if isinstance(runner_payload, JSONResponse):
            return runner_payload
        try:
            return ClearCodexGoalResponse.model_validate(runner_payload)
        except ValueError as exc:
            del exc
            return _codex_goal_error(
                502,
                detail="Could not clear Codex goal: runner returned a malformed response.",
            )
