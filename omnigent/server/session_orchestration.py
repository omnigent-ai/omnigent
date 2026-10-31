"""Owner-authorized orchestration from private sessions and read-only groups."""

from fastapi import Request

from omnigent.db.db_models import normalize_uuid
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.feature_flags import Feature
from omnigent.server.schemas import SessionStartupInput
from omnigent.stores.conversation_store import (
    SESSION_ROLE_LABEL_KEY,
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
    private: bool = False,
    allow_read_only: bool = False,
) -> Conversation:
    """Resolve actual ownership and inherited sharing, without administrator bypass."""
    session_id = normalize_uuid(identifier)
    session = conversations.get_conversation(session_id) if session_id else None
    current, seen = session, set()
    while current is not None:
        if current.id in seen or len(seen) >= 32 or current.archived:
            break
        if is_session_closed(current.labels, current.title):
            break
        seen.add(current.id)
        if current.parent_conversation_id is not None:
            current = conversations.get_conversation(current.parent_conversation_id)
            continue
        grant = permissions.get(user_id, current.id)
        if grant is None or grant.level != LEVEL_OWNER:
            break
        if private:
            # Child access comes from the root, including public/read-only grants.
            cursor = None
            while True:
                grants, cursor = permissions.list_for_session(
                    current.id, after_user_id=cursor, limit=100
                )
                if any(
                    g.user_id != user_id and not (allow_read_only and g.level == LEVEL_READ)
                    for g in grants
                ):
                    raise OmnigentError(
                        "Shared sessions cannot send messages or launch independent sessions",
                        code=ErrorCode.FORBIDDEN,
                    )
                if cursor is None:
                    break
            if current.labels.get(SIDE_CHAT_LABEL_KEY) == "1":
                # Side chats keep their own grants but inherit source restrictions.
                source_id = current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
                current = conversations.get_conversation(source_id) if source_id else None
                continue
        assert session is not None
        return session
    raise OmnigentError("Session is not owned or available", code=ErrorCode.FORBIDDEN)


def require_orchestration_source(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    *,
    target_id: str | None = None,
    related_create: bool = False,
) -> Conversation:
    """Check runner binding, sender sharing and optional same-owner destination."""
    if not request.app.state.feature_flags.enabled(Feature.SESSION_ORCHESTRATION):
        raise OmnigentError("Session orchestration is disabled", code=ErrorCode.FORBIDDEN)
    if not user_id or permissions is None:
        raise OmnigentError("Session orchestration requires ownership", code=ErrorCode.FORBIDDEN)
    source = require_owned_session(
        request.headers.get(ORCHESTRATION_SOURCE_HEADER, ""),
        user_id,
        conversations,
        permissions,
    )
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    if not token or not source.runner_id or token_bound_runner_id(token) != source.runner_id:
        raise OmnigentError(
            "Session orchestration requires the sender's bound runner", code=ErrorCode.FORBIDDEN
        )
    related_enabled = request.app.state.feature_flags.enabled(Feature.RELATED_SESSIONS)
    group_operation = related_create and related_enabled and _full_group_member(source)
    if related_create and not group_operation:
        raise OmnigentError(
            "Related session orchestration is unavailable", code=ErrorCode.FORBIDDEN
        )
    if target_id is not None:
        target = require_owned_session(target_id, user_id, conversations, permissions)
        if source.id == target.id:
            raise OmnigentError("Choose another session", code=ErrorCode.INVALID_INPUT)
        group_operation = (
            related_enabled
            and _full_group_member(source)
            and _full_group_member(target)
            and source.root_conversation_id == target.root_conversation_id
        )
        if group_operation or source.labels.get(SESSION_ROLE_LABEL_KEY) == "peer":
            _require_related_destination(request, source, target, conversations)
    require_owned_session(
        source.id,
        user_id,
        conversations,
        permissions,
        private=True,
        allow_read_only=group_operation,
    )
    return source


def _full_group_member(session: Conversation) -> bool:
    return session.parent_conversation_id is None or (
        session.labels.get(SESSION_ROLE_LABEL_KEY) == "peer"
        and session.parent_conversation_id == session.root_conversation_id
    )


def _require_related_destination(
    request: Request,
    source: Conversation,
    target: Conversation,
    conversations: ConversationStore,
) -> None:
    """Cross-machine peer dispatch requires live managed-host authentication."""
    from omnigent.runner.routing import routing_host_id
    from omnigent.server.sharing_settings import host_is_managed_sandbox

    if source.runner_id and source.runner_id == target.runner_id:
        return
    source_host = routing_host_id(source, conversations)
    target_host = routing_host_id(target, conversations)
    if source_host and source_host == target_host:
        return
    router = getattr(request.app.state, "runner_router", None)
    if target_host and router is not None and router.host_is_on_another_replica(target_host):
        raise OmnigentError(
            "Check the destination sandbox on its host replica", code=ErrorCode.WRONG_REPLICA
        )
    if not host_is_managed_sandbox(getattr(request.app.state, "host_registry", None), target_host):
        raise OmnigentError(
            "Related agents can message their own machine or a connected managed sandbox; "
            "cross-machine dispatch to personal hosts is disabled",
            code=ErrorCode.FORBIDDEN,
        )


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
