"""Keep an agent's session reach inside its own project.

A person can open any session of a project shared with them. Their agents can't:
an agent call (it names its calling session) that reaches a session filed in a
different project, to read, copy or change it, gets the same 404 as a session the
person can't see. Sessions the agent started, wherever they're filed, sessions
outside any project, agents outside any project, and session messages (which keep
their own same-owner rule) are unaffected.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import HTTPConnection

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import CREATED_BY_SESSION_LABEL_KEY
from omnigent.util.uuid import normalize_uuid

_MAX_LINEAGE = 32


def _conversation(conversations: ConversationStore, session_id: str | None) -> Any | None:
    normalized = normalize_uuid(session_id) if session_id else None
    return conversations.get_conversation(normalized) if normalized else None


def _lineage(conversations: ConversationStore, conversation: Any) -> list[Any]:
    """The session and its ancestors, nearest first."""
    chain = [conversation]
    while len(chain) < _MAX_LINEAGE and chain[-1].parent_conversation_id:
        parent = conversations.get_conversation(chain[-1].parent_conversation_id)
        if parent is None or any(parent.id == seen.id for seen in chain):
            break
        chain.append(parent)
    return chain


def _project_of(lineage: list[Any]) -> str | None:
    return next((item.project_id for item in lineage if item.project_id), None)


def agent_source(
    connection: HTTPConnection, conversations: ConversationStore
) -> tuple[str, str] | None:
    """The calling agent session and its project; ``None`` for a person or non-project agent."""
    source = _conversation(conversations, connection.headers.get(ORCHESTRATION_SOURCE_HEADER))
    if source is None:
        return None
    project = _project_of(_lineage(conversations, source))
    return (source.id, project) if project else None


def hidden_from_agent(
    conversations: ConversationStore, target: Any, source: tuple[str, str]
) -> bool:
    """Whether *target* is filed in another project and wasn't started by the calling agent."""
    source_id, source_project = source
    if target.project_id == source_project:
        return False
    lineage = _lineage(conversations, target)
    project = target.project_id or _project_of(lineage)
    if not project or project == source_project:
        return False
    started_by_source = any(
        item.id == source_id or (item.labels or {}).get(CREATED_BY_SESSION_LABEL_KEY) == source_id
        for item in lineage
    )
    return not started_by_source


def agent_session_scope(conversations: ConversationStore):
    """An app-wide dependency refusing an agent's reach into another project's session."""

    def refused(connection: HTTPConnection, session_id: str) -> bool:
        source = agent_source(connection, conversations)
        if source is None:
            return False
        target = _conversation(conversations, session_id)
        return target is not None and hidden_from_agent(conversations, target, source)

    async def refuse_other_project_session(connection: HTTPConnection) -> None:
        # ``source_id`` is the session a fork copies.
        session_id = connection.path_params.get("session_id") or connection.path_params.get(
            "source_id"
        )
        if not isinstance(session_id, str):
            return
        if ORCHESTRATION_SOURCE_HEADER not in connection.headers:
            return
        if connection.scope.get("method") == "POST" and connection.url.path.endswith("/events"):
            # Session messages keep their own same-owner rule.
            return
        if await asyncio.to_thread(refused, connection, session_id):
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)

    return refuse_other_project_session
