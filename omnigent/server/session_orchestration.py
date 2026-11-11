"""Owner-authorized orchestration from private sessions using existing delivery."""

from fastapi import Request

from omnigent.db.db_models import normalize_uuid
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.feature_flags import Feature
from omnigent.server.schemas import SessionStartupInput
from omnigent.server.sharing_settings import peer_session_messaging_enabled
from omnigent.stores.conversation_store import (
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed


def _shared_with_editors(session_id: str, user_id: str, permissions: PermissionStore) -> bool:
    """Whether anyone besides the owner holds a stored grant that can add input."""
    cursor: str | None = None
    while True:
        grants, cursor = permissions.list_for_session(session_id, after_user_id=cursor)
        if any(g.user_id != user_id and g.level >= LEVEL_EDIT for g in grants):
            return True
        if cursor is None:
            return False


def require_owned_session(
    identifier: str,
    user_id: str,
    conversations: ConversationStore,
    permissions: PermissionStore,
    *,
    private: bool = False,
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
            # Child access comes from the root. Read-only viewers, including the
            # public grant, cannot add input, so they cannot steer other sessions.
            if _shared_with_editors(current.id, user_id, permissions):
                raise OmnigentError(
                    "Sessions shared with edit access cannot send messages, launch "
                    "independent sessions or organize other sessions and projects",
                    code=ErrorCode.FORBIDDEN,
                )
            if current.labels.get(SIDE_CHAT_LABEL_KEY) == "1":
                # Side chats keep their own grants but inherit source restrictions.
                source_id = current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
                current = conversations.get_conversation(source_id) if source_id else None
                continue
        assert session is not None
        return session
    raise OmnigentError("Session is not owned or available", code=ErrorCode.FORBIDDEN)


def require_private_bound_source(
    request: Request,
    user_id: str,
    conversations: ConversationStore,
    permissions: PermissionStore,
) -> Conversation:
    """Resolve the header's source as a private owned session on the caller's runner."""
    source = require_owned_session(
        request.headers.get(ORCHESTRATION_SOURCE_HEADER, ""),
        user_id,
        conversations,
        permissions,
        private=True,
    )
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    if not token or not source.runner_id or token_bound_runner_id(token) != source.runner_id:
        raise OmnigentError(
            "Session orchestration requires the sender's bound runner", code=ErrorCode.FORBIDDEN
        )
    return source


# How many sessions the archive guard walks up from the caller before refusing.
_MAX_ARCHIVE_LINEAGE_DEPTH = 32


def require_archive_outside_source_lineage(
    request: Request, session_id: str, conversations: ConversationStore
) -> None:
    """
    Refuse an agent-tool archive of the calling session or any of its ancestors.

    Archiving stops the target's running sub-agents, so archiving an ancestor
    would stop the caller. Cyclic or over-depth lineages are refused. Requests
    without the orchestration-source header bypass this agent-only guard.
    """
    raw_source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
    if not raw_source:
        return
    target = normalize_uuid(session_id)
    source_id = normalize_uuid(raw_source)
    current = conversations.get_conversation(source_id) if source_id else None
    seen: set[str] = set()
    while current is not None:
        if current.id == target:
            raise OmnigentError(
                "An agent cannot archive its own session or a session it runs under",
                code=ErrorCode.FORBIDDEN,
            )
        if current.id in seen or len(seen) >= _MAX_ARCHIVE_LINEAGE_DEPTH:
            raise OmnigentError(
                "An agent cannot archive another session while its own session's "
                "lineage cannot be checked",
                code=ErrorCode.FORBIDDEN,
            )
        seen.add(current.id)
        next_id = current.parent_conversation_id
        if next_id is None and current.labels.get(SIDE_CHAT_LABEL_KEY) == "1":
            next_id = current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
        current = conversations.get_conversation(next_id) if next_id else None


def require_named_source_private(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
) -> None:
    """
    Refuse an agent-tool write whose named calling session is shared with edit access.

    Requests without the orchestration-source header, and servers without
    per-user sharing (no auth or the local user), are left alone.
    """
    if ORCHESTRATION_SOURCE_HEADER not in request.headers:
        return
    if user_id is None or user_id == RESERVED_USER_LOCAL or permissions is None:
        return
    require_private_bound_source(request, user_id, conversations, permissions)


def require_orchestration_source(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    *,
    target_id: str | None = None,
) -> Conversation:
    """Check the bound runner, private sender and optional same-owner destination."""
    if not request.app.state.feature_flags.enabled(Feature.SESSION_ORCHESTRATION):
        raise OmnigentError("Session orchestration is disabled", code=ErrorCode.FORBIDDEN)
    if target_id is not None and not peer_session_messaging_enabled(
        request.app.state.feature_flags
    ):
        raise OmnigentError("Messaging other sessions is disabled", code=ErrorCode.FORBIDDEN)
    if not user_id or permissions is None:
        raise OmnigentError("Session orchestration requires ownership", code=ErrorCode.FORBIDDEN)
    source = require_private_bound_source(request, user_id, conversations, permissions)
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
