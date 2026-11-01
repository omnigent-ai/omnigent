"""Owner-authorized orchestration from private sessions using existing delivery."""

from typing import Any

from fastapi import Request

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
from omnigent.server.schemas import SessionStartupInput
from omnigent.stores.conversation_store import (
    PROJECT_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    ConversationStore,
)
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.session_lifecycle import is_session_closed

OUTSIDE_PROJECT_MESSAGE = "That session is outside this session's project"


def _project_root(session: Conversation, conversations: ConversationStore) -> Conversation:
    """Follow children and side chats to the session whose filing governs them."""
    current, seen = session, {session.id}
    while True:
        source_id = current.parent_conversation_id or (
            current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            if current.labels.get(SIDE_CHAT_LABEL_KEY) == "1"
            else None
        )
        source = conversations.get_conversation(source_id) if source_id else None
        if source is None or source.id in seen or len(seen) >= 32:
            return current
        seen.add(source.id)
        current = source


def _project_id_name(request: Request, project_id: str, user_id: str | None) -> str:
    """Name a first-class project, falling back to its id when it cannot be read."""
    project_store = getattr(request.app.state, "project_store", None)
    project = project_store.get(project_id, user_id=user_id) if project_store else None
    return project.name if project is not None else project_id


def _project_name(request: Request, session: Conversation, user_id: str | None) -> str | None:
    """Name a session's project, matching first-class and legacy-label filing alike."""
    if session.project_id is None:
        return session.labels.get(PROJECT_LABEL_KEY)
    return _project_id_name(request, session.project_id, user_id)


def _require_same_project(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    source: Conversation,
    target: Conversation,
) -> None:
    """Refuse a target in another project; unfiled sessions form their own group."""
    source_project = _project_name(request, _project_root(source, conversations), user_id)
    target_project = _project_name(request, _project_root(target, conversations), user_id)
    if source_project != target_project:
        raise OmnigentError(OUTSIDE_PROJECT_MESSAGE, code=ErrorCode.FORBIDDEN)


def _boundary_source(request: Request, conversations: ConversationStore) -> Conversation | None:
    """Return the agent session behind a tool request, or ``None`` when no boundary applies."""
    source_header = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
    if source_header is None or not request.app.state.feature_flags.enabled(Feature.CANVAS):
        return None
    source_id = normalize_uuid(source_header)
    source = conversations.get_conversation(source_id) if source_id else None
    if source is None:
        raise OmnigentError(OUTSIDE_PROJECT_MESSAGE, code=ErrorCode.FORBIDDEN)
    return source


def require_project_read_access(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    target_id: str,
) -> None:
    """Keep an agent's session reads inside its project when Canvas teams are enabled."""
    source = _boundary_source(request, conversations)
    if source is None:
        return
    target_key = normalize_uuid(target_id)
    target = conversations.get_conversation(target_key) if target_key else None
    # A missing target keeps the route's own not-found response.
    if target is not None:
        _require_same_project(request, user_id, conversations, source, target)


def project_list_scope(
    request: Request, user_id: str | None, conversations: ConversationStore
) -> str | None:
    """Return the listing filter for an agent: its project's name, ``""`` when unfiled."""
    source = _boundary_source(request, conversations)
    if source is None:
        return None
    root = _project_root(source, conversations)
    return _project_name(request, root, user_id) or ""


def scheduled_task_in_project(
    request: Request,
    user_id: str | None,
    conversations: ConversationStore,
    *,
    project_id: str | None,
    session_id: str | None,
) -> bool:
    """Say whether an automation stays in the calling agent's project.

    A task continuing a session belongs to that session's project; any other
    task runs in the project it is filed under.
    """
    source = _boundary_source(request, conversations)
    if source is None:
        return True
    source_project = _project_name(request, _project_root(source, conversations), user_id)
    if session_id is not None:
        target_key = normalize_uuid(session_id)
        target = conversations.get_conversation(target_key) if target_key else None
        if target is None:
            return False
        target_project = _project_name(request, _project_root(target, conversations), user_id)
        if target_project != source_project:
            return False
    task_project = _project_id_name(request, project_id, user_id) if project_id else None
    return task_project == source_project or (project_id is None and session_id is not None)


def agent_task_project_id(request: Request, conversations: ConversationStore) -> str | None:
    """Return the project an agent's new automation is filed under, if any."""
    source = _boundary_source(request, conversations)
    return _project_root(source, conversations).project_id if source is not None else None


def inherited_project_fields(
    request: Request,
    conversations: ConversationStore,
    source: Conversation,
) -> dict[str, Any]:
    """Return create fields that file an independent session in its creator's project."""
    if not request.app.state.feature_flags.enabled(Feature.CANVAS):
        return {}
    root = _project_root(source, conversations)
    legacy_project = root.labels.get(PROJECT_LABEL_KEY) if root.project_id is None else None
    return {
        "project_id": root.project_id,
        **({"labels": {PROJECT_LABEL_KEY: legacy_project}} if legacy_project else {}),
    }


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
            # Child access comes from the root, including public/read-only grants.
            grants, cursor = permissions.list_for_session(current.id, limit=2)
            if cursor is not None or any(g.user_id != user_id for g in grants):
                raise OmnigentError(
                    "Shared sessions cannot send messages or launch independent sessions",
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
    if not user_id or permissions is None:
        raise OmnigentError("Session orchestration requires ownership", code=ErrorCode.FORBIDDEN)
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
    if target_id is not None:
        target = require_owned_session(target_id, user_id, conversations, permissions)
        if source.id == target.id:
            raise OmnigentError("Choose another session", code=ErrorCode.INVALID_INPUT)
        if request.app.state.feature_flags.enabled(Feature.CANVAS):
            _require_same_project(request, user_id, conversations, source, target)
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
