"""Routes for discovering and publishing instance-wide template agents.

Built-in agents are the long-lived, shared agents the server provides
out of the box — the seeded ``claude-native-ui`` agent plus anything
registered at startup with ``omnigent server --agent``. They are the
``session_id IS NULL`` rows in ``agent_store``; ``agent_store.list()``
already filters to exactly these. Session-scoped agents (created via
multipart ``POST /v1/sessions``) belong to one conversation and are read
through ``GET /v1/sessions/{id}/agent`` — never here.

The Web UI's new-session picker calls this to discover bindable
built-ins, then creates a session with
``POST /v1/sessions {agent_id, host_id, workspace}``. See
``designs/BUILTIN_AGENTS.md``.

Admins may also create and update non-seeded template agents with multipart
``POST /v1/agents`` and ``PUT /v1/agents/{agent_id}``. Seeded agents remain
read-only, and session-scoped agents stay on their session routes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from sqlalchemy.exc import IntegrityError

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.admin_list import AdminList
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes.session_mcp_servers import reset_runner_session_agent_cache
from omnigent.server.schemas import AgentObject, MCPServerSummary, PaginatedList, SkillSummary
from omnigent.stores import AgentStore, ArtifactStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)
_RUNNER_RESET_PAGE_SIZE = 25


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
        # Bundled skills only — host-discovered skills are runner-owned
        # and unknowable here (no session, no runner). The new-session
        # composer uses this list for its "/" menu.
        skills = [SkillSummary(name=s.name, description=s.description) for s in loaded.spec.skills]
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
    artifact_store: ArtifactStore | None = None,
    conversation_store: ConversationStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    admin_list: AdminList | None = None,
) -> APIRouter:
    """Build the router for template-agent discovery and admin publishing.

    Mounted with ``prefix="/v1"`` so the final path is ``/v1/agents``.

    :param agent_store: Store whose ``list()`` returns only built-in
        (``session_id IS NULL``) agents.
    :param agent_cache: Cache for loading specs (populates
        ``mcp_servers`` on each agent).
    :param artifact_store: Store for uploaded bundles.
    :param conversation_store: Store used to find sessions bound to an updated agent.
    :param runner_router: Router used for best-effort runner cache resets.
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated.
    :param permission_store: Optional permission store for admin checks.
    :param admin_list: File/config-backed administrator roster.
    :returns: A FastAPI router exposing discovery and admin publishing.
    """
    router = APIRouter()

    async def _require_admin(request: Request) -> None:
        if local_single_user_enabled():
            return
        user_id = get_user_id(request, auth_provider)
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        store_admin = permission_store is not None and await asyncio.to_thread(
            permission_store.is_admin, user_id
        )
        if not store_admin and not (admin_list is not None and admin_list.is_admin(user_id)):
            raise OmnigentError(
                "Admin privileges required to publish agents",
                code=ErrorCode.FORBIDDEN,
            )

    def _require_writable_stores() -> ArtifactStore:
        if artifact_store is None:
            raise OmnigentError(
                "Artifact store not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return artifact_store

    async def _reset_bound_runner_caches(agent_id: str) -> None:
        if conversation_store is None:
            return
        after: str | None = None
        while True:
            try:
                page = await asyncio.to_thread(
                    conversation_store.list_conversations,
                    limit=_RUNNER_RESET_PAGE_SIZE,
                    after=after,
                    kind=None,
                    agent_id=agent_id,
                    include_archived=False,
                )
            except Exception:  # noqa: BLE001 - reset is best-effort after durable update
                _logger.warning(
                    "runner agent-cache reset scan failed after agent update for agent=%s",
                    agent_id,
                    exc_info=True,
                )
                return
            results = await asyncio.gather(
                *(
                    reset_runner_session_agent_cache(conversation.id, agent_id, runner_router)
                    for conversation in page.data
                ),
                return_exceptions=True,
            )
            for conversation, result in zip(page.data, results, strict=True):
                if isinstance(result, BaseException):
                    _logger.warning(
                        "runner agent-cache reset failed after agent update "
                        "for session=%s agent=%s",
                        conversation.id,
                        agent_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )
            if not page.has_more or page.last_id is None:
                return
            after = page.last_id

    @router.post(
        "/agents",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def create_agent(
        request: Request,
        bundle: Annotated[UploadFile, File(...)],
    ) -> AgentObject:
        """Create a non-seeded instance-wide template agent."""
        await _require_admin(request)
        store = _require_writable_stores()
        bundle_bytes = await bundle.read()
        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        assert spec.name is not None
        if await asyncio.to_thread(agent_store.get_by_name, spec.name) is not None:
            raise OmnigentError(
                f"Agent with name '{spec.name}' already exists",
                code=ErrorCode.CONFLICT,
            )

        agent_id = generate_agent_id()
        location = bundle_location(agent_id, bundle_bytes)
        await asyncio.to_thread(store.put, location, bundle_bytes)
        try:
            agent = await asyncio.to_thread(agent_store.create, agent_id, spec.name, location)
        except IntegrityError as exc:
            raise OmnigentError(
                f"Agent with name '{spec.name}' already exists",
                code=ErrorCode.CONFLICT,
            ) from exc
        await asyncio.to_thread(
            agent_cache.replace,
            agent.id,
            location,
            bundle_bytes,
            expand_env=True,
        )
        return _to_agent_object(agent, agent_cache)

    @router.put(
        "/agents/{agent_id}",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def update_agent(
        request: Request,
        background_tasks: BackgroundTasks,
        agent_id: str,
        bundle: Annotated[UploadFile, File(...)],
    ) -> AgentObject:
        """Update a non-seeded instance-wide template agent."""
        await _require_admin(request)
        store = _require_writable_stores()
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        if agent is None:
            raise OmnigentError(
                f"Agent not found: {agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )
        if agent.session_id is not None:
            raise OmnigentError(
                "Session-scoped agents cannot be updated through this endpoint",
                code=ErrorCode.INVALID_INPUT,
            )
        if agent.id == builtin_agent_id(agent.name):
            raise OmnigentError(
                "Seeded agents cannot be updated through this endpoint",
                code=ErrorCode.FORBIDDEN,
            )

        bundle_bytes = await bundle.read()
        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        assert spec.name is not None
        if spec.name != agent.name:
            raise OmnigentError(
                f"spec name '{spec.name}' does not match agent "
                f"name '{agent.name}'; name is immutable",
                code=ErrorCode.INVALID_INPUT,
            )

        location = bundle_location(agent.id, bundle_bytes)
        if location == agent.bundle_location:
            return _to_agent_object(agent, agent_cache)

        await asyncio.to_thread(store.put, location, bundle_bytes)
        updated = await asyncio.to_thread(agent_store.update, agent.id, location)
        if updated is None:
            raise OmnigentError(
                f"Agent not found: {agent.id!r}",
                code=ErrorCode.NOT_FOUND,
            )
        await asyncio.to_thread(
            agent_cache.replace,
            agent.id,
            location,
            bundle_bytes,
            expand_env=True,
        )
        background_tasks.add_task(_reset_bound_runner_caches, agent.id)
        return _to_agent_object(updated, agent_cache)

    @router.get("/agents")
    async def list_builtin_agents(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """List built-in agents with cursor-based pagination.

        Returns only built-in agents — ``agent_store.list()`` filters
        ``session_id IS NULL`` — so session-scoped agents never appear.

        :param request: The incoming FastAPI request (for auth).
        :param limit: Maximum number of agents to return (1-1000).
        :param after: Cursor — return agents after this id.
        :param before: Cursor — return agents before this id.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: A :class:`PaginatedList` of built-in agents.
        """
        _require_user(request, auth_provider)
        page = agent_store.list(limit=limit, after=after, before=before, order=order)
        return PaginatedList(
            data=[_to_agent_object(a, agent_cache) for a in page.data],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    return router
