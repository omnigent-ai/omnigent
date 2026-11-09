"""Bind an agent's request to the session and runner it came from."""

from __future__ import annotations

from fastapi import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, bind_host_is_loopback
from omnigent.server.routes.runner_tunnel import _expected_runner_id_from_headers
from omnigent.server.session_orchestration import require_owned_session
from omnigent.stores import ConversationStore, PermissionStore


def require_agent_origin(
    request: Request,
    *,
    action: str,
    owner: str,
    auth_provider: AuthProvider | None,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    required: bool = False,
) -> str | None:
    """Bind a runner-marked request to the owner's source session and runner.

    :param request: The incoming request.
    :param action: Subject of the error messages, e.g. ``"Inbox posts"``.
    :param owner: The calling user.
    :param auth_provider: The server's auth provider; ``None`` when auth is off.
    :param conversation_store: Store used to load the source session.
    :param permission_store: Store used to confirm the caller owns it.
    :param required: Reject requests without the source-session header.
        Otherwise UI/API callers and older runners, which never send it,
        skip verification.
    :returns: The source session id, or ``None`` when the header is absent.
    :raises OmnigentError: 403 when verification fails.
    """
    source_id = (request.headers.get(ORCHESTRATION_SOURCE_HEADER) or "").strip()
    if required and not source_id:
        raise OmnigentError(f"{action} require a source session", code=ErrorCode.FORBIDDEN)
    if auth_provider is None or owner == RESERVED_USER_LOCAL:
        return source_id or None
    if ORCHESTRATION_SOURCE_HEADER not in request.headers:
        return None
    if not source_id or permission_store is None:
        raise OmnigentError(f"{action} require a source session", code=ErrorCode.FORBIDDEN)
    try:
        source = require_owned_session(
            source_id,
            owner,
            conversation_store,
            permission_store,
        )
    except OmnigentError as exc:
        raise OmnigentError(
            f"{action} require an owned source session",
            code=ErrorCode.FORBIDDEN,
        ) from exc
    is_loopback = request.client is not None and bind_host_is_loopback(request.client.host)
    allowed_tokens = request.app.state.runner_tunnel_tokens
    try:
        try:
            expected_runner_id = _expected_runner_id_from_headers(
                request.headers, allowed_tunnel_tokens=allowed_tokens
            )
        except ValueError:
            if not is_loopback:
                raise
            expected_runner_id = _expected_runner_id_from_headers(request.headers)
        if expected_runner_id is None and allowed_tokens is None and not is_loopback:
            raise ValueError("runner tunnel token is required")
    except ValueError as exc:
        raise OmnigentError(
            f"{action} require an authorized runner token",
            code=ErrorCode.FORBIDDEN,
        ) from exc
    if (
        not source.runner_id
        or (expected_runner_id is not None and expected_runner_id != source.runner_id)
        or (
            RUNNER_TUNNEL_TOKEN_HEADER not in request.headers
            and source.runner_id.startswith("runner_token_")
        )
    ):
        raise OmnigentError(
            f"{action} require the source session's bound runner",
            code=ErrorCode.FORBIDDEN,
        )
    return source_id
