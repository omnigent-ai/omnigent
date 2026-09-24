"""Skill discovery for new-session and existing-session composers."""

from __future__ import annotations

import asyncio
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from omnigent.harness_aliases import canonicalize_harness
from omnigent.host.frames import HostSkillsFrame, HostSkillsResultFrame, encode_host_frame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_access_and_level, require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.schemas import SkillSummary
from omnigent.spec.types import AgentSpec
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store import PermissionStore

_SKILLS_TIMEOUT_S = 15.0


class SkillsResponse(BaseModel):
    """User-invocable skill metadata discovered on a host."""

    skills: list[SkillSummary]


def create_skills_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    conversation_store: ConversationStore,
    *,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the discovery route, mounted under ``/v1``."""
    router = APIRouter()

    @router.get("/skills")
    async def get_skills(
        request: Request,
        session_id: str | None = Query(None, min_length=1),
        host_id: str | None = Query(None, min_length=1),
        harness: str | None = Query(None, min_length=1),
        path: str | None = Query(None, min_length=1),
        agent_id: str | None = Query(None, min_length=1),
    ) -> SkillsResponse:
        """Discover skills using a session or an explicit host, harness, and directory.

        ``session_id`` requires session edit access and uses the saved host,
        workspace, and agent bundle, including its filters and sub-agent scope.
        Otherwise, supply ``host_id``, ``harness``, and ``path``; host ownership
        is required. Optional ``agent_id`` applies the selected agent's filter
        and bundled skills, with the same access check as session creation.
        The two forms cannot be combined.
        """
        user_id = require_user(request, auth_provider)
        agent_version = sub_agent_name = None
        spec: AgentSpec | None = None
        host = None
        if session_id is not None:
            if any(value is not None for value in (host_id, harness, path, agent_id)):
                raise HTTPException(
                    status_code=422,
                    detail="Provide session_id alone, or host_id, harness, and path",
                )
            access = await require_access_and_level(
                user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
            )
            conv = access.conversation
            if conv is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise HTTPException(status_code=404, detail="session not found")
            if conv.host_id is None or not conv.workspace:
                raise HTTPException(status_code=409, detail="session host is not ready")
            host_id, path, harness = conv.host_id, conv.workspace, "session"
            agent = (
                await asyncio.to_thread(agent_store.get, conv.agent_id)
                if agent_store is not None and conv.agent_id is not None
                else None
            )
            if agent is None:
                raise HTTPException(status_code=404, detail="session agent not found")
            agent_id, agent_version, sub_agent_name = (
                agent.id,
                str(agent.version),
                conv.sub_agent_name,
            )
        else:
            if host_id is None or harness is None or path is None:
                raise HTTPException(
                    status_code=422, detail="Provide session_id or host_id, harness, and path"
                )
            host = await asyncio.to_thread(
                resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
            )
            harness = canonicalize_harness(harness) or harness
            if agent_id is not None:
                if agent_store is None or agent_cache is None:
                    raise HTTPException(
                        status_code=503, detail="agent discovery is not configured"
                    )
                agent = await validate_session_agent(
                    user_id=user_id,
                    agent_id=agent_id,
                    agent_store=agent_store,
                    permission_store=permission_store,
                    conversation_store=conversation_store,
                )
                loaded = await asyncio.to_thread(
                    agent_cache.load,
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
                spec, agent_version = loaded.spec, str(agent.version)

        conn = host_registry.get(host_id)
        if conn is None:
            if host is None:
                host = await asyncio.to_thread(host_store.get_host, host_id)
            if host is not None:
                raise host_absent_error(host)
            raise HTTPException(status_code=503, detail="session host is offline")
        result = await request_host_skills(
            host_registry=host_registry,
            host_conn=conn,
            harness=harness,
            path=path,
            session_id=session_id,
            agent_id=agent_id,
            agent_version=agent_version,
            sub_agent_name=sub_agent_name,
            skills_filter=spec.skills_filter if spec is not None else "all",
        )
        if result.status != "ok":
            raise HTTPException(
                status_code={"invalid_path": 400, "not_directory": 404}.get(
                    result.error_code or "", 502
                ),
                detail=result.error or "host skill discovery failed",
            )
        if session_id is not None and result.session_id != session_id:
            raise HTTPException(
                status_code=502, detail="update the host to discover session skills"
            )
        if spec is not None and result.agent_id != agent_id:
            raise HTTPException(status_code=502, detail="update the host to discover agent skills")
        skills = [SkillSummary.model_validate(skill) for skill in result.skills]
        if spec is not None:
            # Hidden bundled skills also reserve their names against host collisions.
            reserved = {skill.name for skill in spec.skills}
            skills = [
                SkillSummary(name=skill.name, description=skill.description)
                for skill in spec.skills
                if skill.user_invocable
            ] + [skill for skill in skills if skill.name not in reserved]
        return SkillsResponse(skills=skills)

    return router


async def request_host_skills(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    harness: str,
    path: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    agent_version: str | None = None,
    sub_agent_name: str | None = None,
    skills_filter: str | list[str] = "all",
) -> HostSkillsResultFrame:
    """Request skill metadata over the host tunnel, with bounded waiting and cleanup."""
    request_id = secrets.token_hex(8)
    future: asyncio.Future[HostSkillsResultFrame] = asyncio.get_running_loop().create_future()
    host_conn.pending_skills[request_id] = future
    try:
        host_registry.send_text(
            host_conn,
            encode_host_frame(
                HostSkillsFrame(
                    request_id=request_id,
                    harness=harness,
                    path=path,
                    session_id=session_id,
                    agent_id=agent_id,
                    agent_version=agent_version,
                    sub_agent_name=sub_agent_name,
                    skills_filter=skills_filter,
                )
            ),
        )
        return await asyncio.wait_for(future, timeout=_SKILLS_TIMEOUT_S)
    except ConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"host '{host_conn.host_id}' connection lost",
        ) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"host '{host_conn.host_id}' did not discover skills "
                f"within {_SKILLS_TIMEOUT_S:.0f}s"
            ),
        ) from exc
    finally:
        host_conn.pending_skills.pop(request_id, None)
