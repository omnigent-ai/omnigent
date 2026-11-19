"""A project's orchestrator is an ordinary session, not a separate agent runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request

from omnigent.entities import MessageData, NewConversationItem, Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.proactive_agent import PROACTIVE_AGENT_NAME
from omnigent.server.routes._session_create_validation import _authorize_host_for_workspace
from omnigent.server.routes._workspace_validation import WorkspaceValidationError, _ask_host_stat
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.project_store import ProjectStore

ProjectSessionBinder = Callable[[str], Awaitable[None]]
ProjectSessionCreator = Callable[
    [Request, str | None, dict[str, Any], ProjectSessionBinder], Awaitable[Any]
]
ProjectSessionDeleter = Callable[[Request, str], Awaitable[Any]]


async def ensure_project_orchestrator(
    request: Request,
    project: Project,
    *,
    projects: ProjectStore,
    conversations: ConversationStore,
    agents: AgentStore | None,
    create_session: ProjectSessionCreator | None,
    delete_session: ProjectSessionDeleter | None,
) -> Project:
    if project.orchestrator_session_id is not None:
        return project
    if agents is None or create_session is None or delete_session is None:
        raise OmnigentError(
            "Project session creation is not configured", code=ErrorCode.INVALID_INPUT
        )

    agent_id = project.config.get("agent_id")
    if agent_id is None:
        curation = getattr(request.app.state, "picker_curation", None)
        default = getattr(curation, "default_agent_name", None)
        allowed = getattr(curation, "restrict_to_agent_names", ())
        suppressed = getattr(request.app.state, "suppressed_agent_names", ())
        names = [default] if default else []
        names += list(allowed) or [agent.agent_name for agent in NATIVE_CODING_AGENTS]
        for name in names:
            if name in suppressed:
                continue
            agent = await asyncio.to_thread(agents.get_by_name, name)
            if agent is not None and agent.operator_authored:
                agent_id = agent.id
                break
        if agent_id is None and not allowed:
            catalog = await asyncio.to_thread(agents.list, limit=50)
            agent_id = next(
                (
                    agent.id
                    for agent in catalog.data
                    if agent.operator_authored
                    and agent.name not in suppressed
                    and agent.name != PROACTIVE_AGENT_NAME
                ),
                None,
            )
    if agent_id is None:
        raise OmnigentError(
            "Choose an available project agent before creating the orchestrator",
            code=ErrorCode.INVALID_INPUT,
        )
    payload: dict[str, Any] = {
        "project_id": project.id,
        "title": "Orchestrator",
        "agent_id": agent_id,
    }
    host_id = project.config.get("host_id")
    registry = request.app.state.host_registry
    if not host_id:
        # Name-only projects use an owned, connected machine, like the session composer.
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
    managed = host_id == "__sandbox__"
    payload["host_type"] = "managed" if managed else "external"
    if host_id and not managed:
        payload["host_id"] = host_id
        if not project.config.get("workspace"):
            await _authorize_host_for_workspace(
                user_id=project.user_id,
                host_id=host_id,
                host_store=getattr(request.app.state, "host_store", None),
                host_registry=registry,
            )
            connection = registry.get(host_id)
            if connection is None:
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
    for source, target in (("model", "model_override"), ("effort", "reasoning_effort")):
        if project.config.get(source):
            payload[target] = project.config[source]
    session_id: str | None = None
    result: Project | None = None

    async def bind_before_launch(created_id: str) -> None:
        nonlocal session_id, result
        session_id = created_id
        result = await asyncio.to_thread(
            projects.set_orchestrator, project.id, created_id, user_id=project.user_id
        )
        if result.orchestrator_session_id == created_id:
            # Seed history before launch without dispatching an agent turn.
            await asyncio.to_thread(
                conversations.append,
                created_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id="seed",
                        data=MessageData(
                            role="assistant",
                            agent="orchestrator",
                            content=[
                                {
                                    "type": "output_text",
                                    "text": "Hello, what would you like to work on?",
                                }
                            ],
                        ),
                    )
                ],
            )

    try:
        await create_session(request, project.user_id, payload, bind_before_launch)
    except Exception:
        if session_id is not None:
            await asyncio.to_thread(
                projects.clear_orchestrator, project.id, session_id, user_id=project.user_id
            )
            await delete_session(request, session_id)
        raise
    assert result is not None and session_id is not None
    if result.orchestrator_session_id != session_id:
        # Normal teardown also stops any runner launched by this unused session.
        await delete_session(request, session_id)
    return result


def ticket_update_message(project_id: str, event: dict[str, Any]) -> str:
    import json

    return (
        f"Automatic ticket update for project {project_id}. "
        "Use the orchestrator skill to handle it.\n\n"
        f"Ticket {event['ticket_id']} · {event['kind']}\n"
        + json.dumps(event["payload"], ensure_ascii=False)
    )
