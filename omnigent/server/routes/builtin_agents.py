"""Read-only route for discovering built-in agents (``GET /v1/agents``).

The default catalog contains seeded and user-registered templates.
``kind=session`` lists session-bound agents accessible through the caller's
root-session grants. The picker uses both catalogs to select an ``agent_id``
for session creation. See ``designs/BUILTIN_AGENTS.md``.

This is the read-only successor to the removed ``GET /api/agents`` list:
there is intentionally no create/update/delete — agent writes happen
through session creation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Query, Request

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import Agent
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.schemas import AgentObject, MCPServerSummary, PaginatedList, SkillSummary
from omnigent.stores import AgentStore

_logger = logging.getLogger(__name__)


def _to_agent_object(agent: Agent, agent_cache: AgentCache) -> AgentObject:
    """
    Convert a runtime Agent entity to an API-layer AgentObject.

    Loads the spec from cache to populate ``mcp_servers``,
    ``skills``, and (when the stored row has none) the
    ``description``; on any load failure those fall back to empty /
    the stored value rather than failing the whole list — one
    unreadable bundle must not break discovery.

    :param agent: The runtime agent entity, e.g. the seeded
        ``claude-native-ui`` agent.
    :param agent_cache: Cache used to load the agent spec.
    :returns: An :class:`AgentObject` for the API response.
    """
    mcp_servers: list[MCPServerSummary] = []
    skills: list[SkillSummary] = []
    terminals: list[str] = []
    harness: str | None = None
    # Prefer the stored entity's description; fall back to the spec's
    # top-level description when the stored value is unset (single-file
    # YAML agents don't persist it at registration today). Lets the
    # new-session picker show a hover description without a migration.
    description: str | None = agent.description
    try:
        # Built-ins are operator-authored template agents
        # (session_id is None), so ${VAR} expansion against the server
        # env is allowed here; a tenant session-scoped agent would not
        # expand.
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        if description is None:
            description = loaded.spec.description
        # Declared terminal names, in spec order (mirrors the
        # session-agent endpoint so both report it consistently).
        terminals = list(loaded.spec.terminals or {})
        # Bundled suggestions stay available while the host catalog loads.
        skills = [
            SkillSummary(name=s.name, description=s.description)
            for s in loaded.spec.skills
            if s.user_invocable
        ]
        mcp_servers = [
            MCPServerSummary(
                name=srv.name,
                transport=srv.transport,
                description=srv.description,
                url=srv.url,
                headers=dict.fromkeys(srv.headers, "[REDACTED]") if srv.headers else {},
                command=srv.command,
                args=srv.args,
            )
            for srv in loaded.spec.mcp_servers
        ]
        # Kind for the Add Agent picker (Codex vs Claude). Stays None
        # when the bundle can't be loaded (the except below).
        harness = loaded.spec.executor.harness_kind
    except Exception:  # noqa: BLE001 — spec load failure must not break the list
        _logger.debug(
            "Failed to load spec for agent %s; mcp_servers/skills will be empty",
            agent.id,
            exc_info=True,
        )
    return AgentObject(
        id=agent.id,
        name=agent.name,
        version=agent.version,
        description=description,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        harness=harness,
        mcp_servers=mcp_servers,
        mcp_servers_editable=False,
        skills=skills,
        terminals=terminals,
        # Seeded built-ins use a deterministic, name-derived id; an
        # operator/user-registered template (e.g. ``--agent``) uses a
        # random id. The picker protects the former from being shadowed
        # by a same-named ``omnigent run`` upload, but lets a newer
        # upload supersede the latter.
        builtin=agent.session_id is None and agent.id == builtin_agent_id(agent.name),
    )


def create_builtin_agents_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the router for ``GET /v1/agents`` (built-in discovery).

    Mounted with ``prefix="/v1"`` so the final path is ``/v1/agents``.

    :param agent_store: Store whose ``list()`` returns only built-in
        (``session_id IS NULL``) agents.
    :param agent_cache: Cache for loading specs (populates
        ``mcp_servers`` on each agent).
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated.
    :returns: A FastAPI router exposing the read-only list.
    """
    router = APIRouter()

    @router.get("/agents")
    async def list_builtin_agents(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        kind: Literal["template", "session"] = Query(default="template"),
    ) -> PaginatedList:
        """List built-in agents with cursor-based pagination.

        Templates are the default; session agents require access to their owning root.

        :param request: The incoming FastAPI request (for auth).
        :param limit: Maximum number of agents to return (1-1000).
        :param after: Cursor — return agents after this id.
        :param before: Cursor — return agents before this id.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: A :class:`PaginatedList` of built-in agents.
        """
        user_id = _require_user(request, auth_provider)
        page = await asyncio.to_thread(
            agent_store.list,
            limit=limit,
            after=after,
            before=before,
            order=order,
            kind=kind,
            accessible_by=user_id,
        )
        return PaginatedList(
            data=[_to_agent_object(a, agent_cache) for a in page.data],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    return router
