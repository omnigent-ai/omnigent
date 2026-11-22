"""Project main-chat selection using ordinary sessions and launch settings."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request

from omnigent.db.db_models import current_workspace_id
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, RESERVED_USER_LOCAL
from omnigent.server.routes._auth_helpers import require_access
from omnigent.server.routes._session_create_validation import _authorize_host_for_workspace
from omnigent.server.routes._workspace_validation import WorkspaceValidationError, _ask_host_stat
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.util.uuid import normalize_uuid

MAESTRO_AGENT_NAME = "maestro"
MainSessionState = Literal["absent", "missing", "archived", "active", "unavailable"]
ProjectSessionCreator = Callable[
    [Request, str | None, dict[str, Any], Callable[[str], Awaitable[None]] | None], Awaitable[Any]
]
_CREATION_NAMESPACE = uuid.UUID("21c45865-5a68-480b-a332-8f2a4b47f162")


def creation_key(request: Request, *, fallback: str | None = None) -> str:
    """A stable UUID identifies one explicit submission and its retries."""
    value = request.headers.get("Idempotency-Key", fallback)
    if value is None or len(value) > 64:
        raise OmnigentError("A UUID Idempotency-Key is required", code=ErrorCode.INVALID_INPUT)
    try:
        return uuid.UUID(value).hex
    except ValueError as exc:
        raise OmnigentError(
            "Idempotency-Key must be a UUID", code=ErrorCode.INVALID_INPUT
        ) from exc


def creation_id(kind: str, user_id: str | None, key: str, project_id: str = "") -> str:
    """Derive an owner/tenant-scoped identity without a second idempotency store."""
    return uuid.uuid5(
        _CREATION_NAMESPACE,
        f"{current_workspace_id()}:{user_id or ''}:{kind}:{project_id}:{key}",
    ).hex


def expected_main_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_uuid(value)
    if normalized is None:
        raise OmnigentError("Invalid expected_main_session_id", code=ErrorCode.INVALID_INPUT)
    return normalized


async def main_session_state(
    project: Project,
    conversations: ConversationStore | None,
    permissions: PermissionStore | None,
) -> MainSessionState:
    """Resolve lifecycle and access without changing a stale pointer."""
    if project.main_session_id is None:
        return "absent"
    if conversations is None:
        return "unavailable"
    conversation = await asyncio.to_thread(conversations.get_conversation, project.main_session_id)
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
    return "archived" if conversation.archived else "active"


async def validate_main_assignment(
    project: Project,
    session_id: str,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
) -> str:
    normalized = normalize_uuid(session_id)
    conversation = (
        await asyncio.to_thread(conversations.get_conversation, normalized) if normalized else None
    )
    if conversation is None or conversation.project_id != project.id:
        raise OmnigentError("Project session not found", code=ErrorCode.NOT_FOUND)
    await require_access(project.user_id, conversation.id, LEVEL_OWNER, permissions, conversations)
    if conversation.archived or conversation.parent_conversation_id is not None:
        raise OmnigentError(
            "Choose an active top-level project session", code=ErrorCode.INVALID_INPUT
        )
    return conversation.id


async def maestro_agent_id(request: Request, agents: AgentStore | None) -> str:
    agent = await asyncio.to_thread(agents.get_by_name, MAESTRO_AGENT_NAME) if agents else None
    suppressed = getattr(request.app.state, "suppressed_agent_names", ())
    if agent is None or not agent.operator_authored or agent.name in suppressed:
        raise OmnigentError(
            "Maestro is unavailable; choose an available project agent",
            code=ErrorCode.INVALID_INPUT,
        )
    return agent.id


@dataclass(frozen=True)
class MainSessionCreation:
    project: Project
    session_id: str
    expected_id: str | None
    replay: bool = False


async def prepare_main_creation(
    request: Request,
    *,
    user_id: str | None,
    project_id: str,
    expected_id: str | None,
    projects: ProjectStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    initial_project: bool = False,
) -> MainSessionCreation:
    project = await asyncio.to_thread(projects.get, project_id, user_id=user_id)
    if project is None:
        raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
    key = creation_key(request, fallback=project.id if initial_project else None)
    session_id = creation_id("main-session", user_id, key, project.id)
    expected_id = expected_main_id(expected_id)
    if project.main_session_id == session_id:
        conversation = await asyncio.to_thread(conversations.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError(
                "This creation's session was deleted; start a new submission",
                code=ErrorCode.CONFLICT,
            )
        await require_access(user_id, session_id, LEVEL_OWNER, permissions, conversations)
        if conversation.project_id != project.id:
            raise OmnigentError("Project session not found", code=ErrorCode.NOT_FOUND)
        return MainSessionCreation(project, session_id, expected_id, replay=True)
    if project.main_session_id != expected_id:
        raise OmnigentError(
            "The project's main session changed; reload the project", code=ErrorCode.CONFLICT
        )
    if await asyncio.to_thread(conversations.get_conversation, session_id) is not None:
        raise OmnigentError(
            "Main-chat creation is in progress; retry this submission", code=ErrorCode.CONFLICT
        )
    state = await main_session_state(project, conversations, permissions)
    if state not in ("absent", "missing", "archived"):
        raise OmnigentError(
            "The project already has an active or unavailable main session",
            code=ErrorCode.CONFLICT,
        )
    return MainSessionCreation(project, session_id, expected_id)


async def create_initial_main_session(
    request: Request,
    project: Project,
    *,
    projects: ProjectStore,
    agents: AgentStore | None,
    create_session: ProjectSessionCreator | None,
) -> Project:
    """Project creation uses the same session-create pipeline as the composer."""
    if project.main_session_id is not None:
        return project
    if create_session is None:
        return project
    config = project.config
    agent_id = config.get("agent_id") or await maestro_agent_id(request, agents)
    payload: dict[str, Any] = {
        "project_id": project.id,
        "title": "Main",
        "agent_id": agent_id,
        "project_main_session": True,
        "expected_main_session_id": None,
    }
    for source, target in (
        ("harness", "harness_override"),
        ("model", "model_override"),
        ("effort", "reasoning_effort"),
        ("harness_override", "harness_override"),
        ("model_override", "model_override"),
        ("reasoning_effort", "reasoning_effort"),
        ("workspace", "workspace"),
        ("git", "git"),
        ("sandbox_provider", "sandbox_provider"),
        ("permission_mode", "permission_mode"),
        ("terminal_launch_args", "terminal_launch_args"),
    ):
        if source in config:
            payload[target] = config[source]
    host_id = config.get("host_id")
    registry = getattr(request.app.state, "host_registry", None)
    if not host_id and registry is not None:
        host_id = next(
            (
                candidate
                for candidate in registry.online_host_ids()
                if (connection := registry.get(candidate)) is not None
                and connection.owner in (project.user_id, project.user_id or RESERVED_USER_LOCAL)
                and not connection.registered_with_managed_token
            ),
            None,
        )
    managed = host_id == "__sandbox__" or config.get("host_type") == "managed"
    payload["host_type"] = "managed" if managed else "external"
    if host_id and not managed:
        payload["host_id"] = host_id
        if not config.get("workspace"):
            await _authorize_host_for_workspace(
                user_id=project.user_id,
                host_id=host_id,
                host_store=getattr(request.app.state, "host_store", None),
                host_registry=registry,
            )
            connection = registry.get(host_id) if registry else None
            if registry is None or connection is None:
                raise OmnigentError("Project host is offline", code=ErrorCode.INVALID_INPUT)
            try:
                home = await _ask_host_stat(host_registry=registry, host_conn=connection, path="~")
            except WorkspaceValidationError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
            if not home.get("exists") or home.get("type") != "directory":
                raise OmnigentError(
                    "Project host home is unavailable", code=ErrorCode.INVALID_INPUT
                )
            payload["workspace"] = home.get("canonical_path")
    await create_session(request, project.user_id, payload, None)
    updated = await asyncio.to_thread(projects.get, project.id, user_id=project.user_id)
    if updated is None:
        raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
    return updated
