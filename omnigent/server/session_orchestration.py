"""Owner-authorized orchestration with requester or current-sharing checks."""

from fastapi import Request

from omnigent.db.account_authority import account_generation
from omnigent.db.db_models import normalize_uuid
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.feature_flags import Feature
from omnigent.server.requester import tool_requester
from omnigent.server.schemas import SessionStartupInput
from omnigent.stores.conversation_store import (
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed


def require_owned_session(
    identifier: str,
    user_id: str,
    conversations: ConversationStore,
    permissions: PermissionStore,
    *,
    check_source: bool = False,
) -> Conversation:
    """Resolve ownership and inherited source chains without administrator bypass."""
    return _owned_session_chain(
        identifier, user_id, conversations, permissions, check_source=check_source
    )[0]


def _owned_session_chain(
    identifier: str,
    user_id: str,
    conversations: ConversationStore,
    permissions: PermissionStore,
    *,
    check_source: bool = False,
) -> list[Conversation]:
    """Return the validated source/parent chain, including side-chat sources."""
    session_id = normalize_uuid(identifier)
    session = conversations.get_conversation(session_id) if session_id else None
    current, seen = session, set()
    chain = []
    while current is not None:
        if current.id in seen or len(seen) >= 32 or current.archived:
            break
        if is_session_closed(current.labels, current.title):
            break
        seen.add(current.id)
        chain.append(current)
        if current.parent_conversation_id is not None:
            current = conversations.get_conversation(current.parent_conversation_id)
            continue
        grant = permissions.get(user_id, current.id)
        if grant is None or grant.level != LEVEL_OWNER:
            break
        if check_source:
            source_id = current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            if source_id or current.labels.get(SIDE_CHAT_LABEL_KEY) == "1":
                # The protected source link governs access, not the display marker.
                current = conversations.get_conversation(source_id) if source_id else None
                continue
        return chain
    raise OmnigentError("Session is not owned or available", code=ErrorCode.FORBIDDEN)


def require_orchestration_source(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    *,
    target_id: str | None = None,
) -> Conversation:
    """Check runner, requester, source chain and optional same-owner destination."""
    if not request.app.state.feature_flags.enabled(Feature.SESSION_ORCHESTRATION):
        raise OmnigentError("Session orchestration is disabled", code=ErrorCode.FORBIDDEN)
    if not user_id or permissions is None:
        raise OmnigentError("Session orchestration requires ownership", code=ErrorCode.FORBIDDEN)
    chain = _owned_session_chain(
        request.headers.get(ORCHESTRATION_SOURCE_HEADER, ""),
        user_id,
        conversations,
        permissions,
        check_source=True,
    )
    source = chain[0]
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    if not token or not source.runner_id or token_bound_runner_id(token) != source.runner_id:
        raise OmnigentError(
            "Session orchestration requires the sender's bound runner", code=ErrorCode.FORBIDDEN
        )
    requester = tool_requester(request, source)
    if requester is not None and requester.delegation_policy == "no_edit_sharing":
        try:
            has_other_writers = any(
                permissions.has_other_write_grants(session.id, user_id) for session in chain
            )
        except NotImplementedError as exc:
            raise OmnigentError(
                "Delegation is unavailable: this permission store cannot verify edit sharing. "
                "Implement PermissionStore.has_other_write_grants in the store adapter.",
                code=ErrorCode.FORBIDDEN,
            ) from exc
        if has_other_writers:
            raise OmnigentError(
                "This harness can delegate only while the sending session and its source chain "
                "have no edit sharing. Read-only sharing is allowed.",
                code=ErrorCode.FORBIDDEN,
            )
        # Removing edit access reauthorizes retained work, without rewriting its origin.
        if (
            requester.origin.user_id == user_id
            and requester.origin.account_generation != account_generation(user_id)
        ):
            raise OmnigentError("Requester account is no longer current", code=ErrorCode.FORBIDDEN)
    elif (
        requester is None
        or requester.origin.user_id != user_id
        or requester.origin.account_generation != account_generation(user_id)
    ):
        raise OmnigentError(
            "Only work from a verified owner execution can delegate to other sessions",
            code=ErrorCode.FORBIDDEN,
        )
    if target_id is not None:
        target = require_owned_session(target_id, user_id, conversations, permissions)
        if source.id == target.id:
            raise OmnigentError("Choose another session", code=ErrorCode.INVALID_INPUT)
    return source


def orchestration_message(source_id: str, message: object) -> SessionStartupInput:
    """Build a text-only task with a display label, not proof of message origin."""
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise OmnigentError("Provide a message of 1–8000 characters", code=ErrorCode.INVALID_INPUT)
    return SessionStartupInput(
        type="message",
        data={
            "role": "user",
            "content": [
                {"type": "input_text", "text": f"[Message from session {source_id}]\n{message}"}
            ],
        },
    )
