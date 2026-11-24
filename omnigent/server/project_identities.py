"""Project identities: named persistent roles, each with one active session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_READ
from omnigent.server.routes._auth_helpers import require_access

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.entities.project import ProjectIdentity
    from omnigent.stores import ConversationStore, PermissionStore

IdentitySessionState = Literal["absent", "missing", "archived", "idle", "busy", "unavailable"]
# Built-in identity id for the project's Lead (``projects.main_session_id``); the
# id keeps its original name so stored routing, mentions and links stay valid.
ORCHESTRATOR_IDENTITY_ID = "orchestrator"
# Live statuses meaning a turn is in flight; a new session must wait (single-tracked).
_BUSY_LIVE_STATUSES = frozenset({"running", "waiting"})


async def identity_session_state(
    project: Project,
    session_id: str | None,
    conversations: ConversationStore | None,
    permissions: PermissionStore | None,
) -> IdentitySessionState:
    """Resolve the lifecycle of an identity's active session without changing it."""
    if session_id is None:
        return "absent"
    if conversations is None:
        return "unavailable"
    conversation = await asyncio.to_thread(conversations.get_conversation, session_id)
    if conversation is None:
        return "missing"
    if conversation.project_id != project.id:
        return "unavailable"
    try:
        await require_access(
            project.user_id, conversation.id, LEVEL_READ, permissions, conversations
        )
    except OmnigentError:
        return "unavailable"
    if conversation.archived:
        return "archived"
    return "busy" if conversation.live_status in _BUSY_LIVE_STATUSES else "idle"


def identity_response(identity: ProjectIdentity, state: IdentitySessionState) -> dict[str, Any]:
    """Serialize an identity for the project API."""
    return {
        "id": identity.id,
        "object": "project.identity",
        "project_id": identity.project_id,
        "name": identity.name,
        "builtin": False,
        "instructions": identity.instructions,
        "agent_id": identity.agent_id,
        "model": identity.model,
        "effort": identity.effort,
        "active_session_id": identity.active_session_id,
        "active_session_state": state,
        "created_by_session_id": identity.created_by_session_id,
        "created_at": identity.created_at,
        "updated_at": identity.updated_at,
    }


def orchestrator_identity_response(
    project: Project, state: IdentitySessionState
) -> dict[str, Any]:
    """The built-in Lead identity, backed by the project's main session."""
    return {
        "id": ORCHESTRATOR_IDENTITY_ID,
        "object": "project.identity",
        "project_id": project.id,
        "name": "Lead",
        "builtin": True,
        "instructions": "",
        "agent_id": project.config.get("agent_id"),
        "model": project.config.get("model"),
        "effort": project.config.get("effort"),
        "active_session_id": project.main_session_id,
        "active_session_state": state,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
