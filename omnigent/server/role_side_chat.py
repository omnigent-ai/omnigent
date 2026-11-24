"""Refuse coordination writes from a side chat of a project role, decided from server state.

A side chat forked from the project's Lead session, or from an identity's active
session, keeps the role's persona but not its authority: the main session holds
the role. The runner refuses those tool calls too; this is the server's check,
so a runner that skips its own guard still can't coordinate.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from omnigent.entities import Conversation
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY, Project
from omnigent.entities.project_routing import LEAD_IDENTITY_ID, LEAD_NAME
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.stores.project_store import ProjectStore
from omnigent.util.uuid import normalize_uuid


@dataclass(frozen=True)
class RoleSideChat:
    """Where a role side chat sits: the role's session and the project's Lead session."""

    role_session_id: str
    role_name: str
    lead_session_id: str | None


def side_chat_source_id(conversation: Conversation) -> str | None:
    """The session a side chat was forked from, from server-owned metadata, or ``None``."""
    source = conversation.side_chat_parent_id or (conversation.labels or {}).get(
        SIDE_CHAT_SOURCE_LABEL_KEY
    )
    return normalize_uuid(source) if source else None


def project_role_of_session(
    project: Project,
    session_id: str,
    conversations: ConversationStore,
    projects: ProjectStore,
    *,
    include_instructions: bool = False,
) -> dict[str, str] | None:
    """The role *session_id* holds in *project*: the Lead, an identity, or ``None``.

    An identity's session holds the role only while it is that identity's active session.
    """
    if project.main_session_id and normalize_uuid(project.main_session_id) == session_id:
        role = {"id": LEAD_IDENTITY_ID, "name": LEAD_NAME}
        if include_instructions and project.lead_charter:
            role["instructions"] = project.lead_charter
        return role
    session = conversations.get_conversation(session_id)
    identity_id = (session.labels or {}).get(PROJECT_IDENTITY_LABEL_KEY) if session else None
    if session is None or session.project_id != project.id or not identity_id:
        return None
    identity = projects.get_identity(project.id, identity_id, user_id=project.user_id)
    if identity is None or identity.active_session_id != session.id:
        return None
    role = {"id": identity.id, "name": identity.name}
    if include_instructions and identity.instructions:
        role["instructions"] = identity.instructions
    return role


# How far up a caller's parents and side-chat sources the check looks for a role.
_MAX_LINEAGE_DEPTH = 32


def role_side_chat_of(
    request: Request,
    *,
    user_id: str | None,
    conversations: ConversationStore,
    projects: ProjectStore,
) -> RoleSideChat | None:
    """Where the caller sits when it runs under a role's side chat, else ``None``.

    The caller is the session named by the orchestration-source header; requests
    without it (people using the UI or API) never are. The walk follows
    sub-agent parents and side-chat sources, so a sub-agent of a role's side chat,
    or a side chat of one, is held to the same limit. Lineages deeper than the
    bound are refused, as for archive.
    """
    raw_source = (request.headers.get(ORCHESTRATION_SOURCE_HEADER) or "").strip()
    current_id = normalize_uuid(raw_source) if raw_source else None
    seen: set[str] = set()
    while current_id is not None:
        if current_id in seen or len(seen) >= _MAX_LINEAGE_DEPTH:
            return RoleSideChat(current_id, "role", None)
        seen.add(current_id)
        current = conversations.get_conversation(current_id)
        if current is None:
            return None
        source_id = side_chat_source_id(current)
        if source_id is not None:
            source = conversations.get_conversation(source_id)
            project = (
                projects.get(source.project_id, user_id=user_id)
                if source is not None and source.project_id
                else None
            )
            role = (
                project_role_of_session(project, source_id, conversations, projects)
                if project is not None
                else None
            )
            if role is not None and project is not None:
                lead = normalize_uuid(project.main_session_id) if project.main_session_id else None
                return RoleSideChat(source_id, role["name"], lead)
            current_id = source_id
            continue
        parent = current.parent_conversation_id
        current_id = normalize_uuid(parent) if parent else None
    return None


def refuse_role_side_chat(
    request: Request,
    *,
    action: str,
    user_id: str | None,
    conversations: ConversationStore,
    projects: ProjectStore | None,
) -> None:
    """Raise 403 when a side chat of a project role attempts a coordination write.

    :param action: What was attempted, for the message, e.g. ``"Creating tickets"``.
    :raises OmnigentError: ``FORBIDDEN`` naming the main session to ask instead.
    """
    if projects is None or ORCHESTRATION_SOURCE_HEADER not in request.headers:
        return
    found = role_side_chat_of(
        request, user_id=user_id, conversations=conversations, projects=projects
    )
    if found is None:
        return
    raise OmnigentError(
        f"{action} is refused in a side chat of the project's {found.role_name}. The main "
        f"session holds the role: message it (session {found.role_session_id}) or comment "
        "on the ticket.",
        code=ErrorCode.FORBIDDEN,
    )


def refuse_role_side_chat_message(
    request: Request,
    target_id: str,
    *,
    user_id: str | None,
    conversations: ConversationStore,
    projects: ProjectStore | None,
) -> None:
    """Let a role side chat message only its role's session or the project's Lead.

    :param target_id: The resolved session the message is for.
    :raises OmnigentError: ``FORBIDDEN`` for any other target.
    """
    if projects is None or ORCHESTRATION_SOURCE_HEADER not in request.headers:
        return
    found = role_side_chat_of(
        request, user_id=user_id, conversations=conversations, projects=projects
    )
    target = normalize_uuid(target_id)
    if found is None or target in {found.role_session_id, found.lead_session_id}:
        return
    raise OmnigentError(
        f"A side chat of the project's {found.role_name} may message only its main session "
        f"(session {found.role_session_id}) or the Lead.",
        code=ErrorCode.FORBIDDEN,
    )
