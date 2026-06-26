"""Agents API (``/v1/agents``): built-in discovery + standalone CRUD.

Two kinds of agent are served here, both ``session_id IS NULL`` rows in
``agent_store``:

- **Built-in/template agents** (``owner IS NULL``): the long-lived, shared
  agents the server seeds at startup (``claude-native-ui`` etc.). Read-only,
  listed by ``GET /v1/agents``.
- **Standalone user agents** (``owner`` set): first-class, user-owned agents
  managed independently of any session via full CRUD — ``GET /v1/agents/mine``,
  ``POST /v1/agents``, ``GET/PUT/DELETE /v1/agents/{id}``. They persist across
  sessions and survive session deletion, unlike the session-scoped agents
  created through multipart ``POST /v1/sessions`` (which belong to one
  conversation and are read via ``GET /v1/sessions/{id}/agent``).

The Web UI's new-session picker lists built-ins + the caller's standalone
agents; the sidebar "Agents" section CRUDs the standalone ones. Owner scoping
mirrors sessions: a user only sees/edits/deletes their own agents.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider, local_single_user_enabled
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.schemas import AgentObject, MCPServerSummary, PaginatedList, SkillSummary
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

_logger = logging.getLogger(__name__)


async def _read_bundle_and_description(request: Request) -> tuple[bytes, str | None]:
    """Read the ``bundle`` file part (and optional ``description``) from a
    multipart request.

    Parses the form manually (rather than ``UploadFile``/``Form`` defaults)
    to match the session-bundle upload path and keep handler signatures
    free of call-in-default lint issues.

    :param request: The incoming multipart request.
    :returns: ``(bundle_bytes, description_or_None)``.
    :raises OmnigentError: 400 when the ``bundle`` file part is missing.
    """
    form = await request.form()
    part = form.get("bundle")
    # A string here means the field was sent as a plain value, not a file.
    if part is None or isinstance(part, str):
        raise OmnigentError(
            "a 'bundle' file part (the agent .tar.gz) is required",
            code=ErrorCode.INVALID_INPUT,
        )
    raw = await part.read()
    bundle_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    desc = form.get("description")
    description = desc.strip() if isinstance(desc, str) and desc.strip() else None
    return bundle_bytes, description


def _to_agent_object(
    agent: Agent, agent_cache: AgentCache, *, mcp_servers_editable: bool = False
) -> AgentObject:
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
    :param mcp_servers_editable: Whether the caller may edit this agent's
        MCP servers (``True`` for the owner's standalone agents).
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
        # (session_id is None, owner is None), so ${VAR} expansion against
        # the server env is allowed for them; a tenant-owned standalone
        # agent (owner set) or a session-scoped agent would not expand.
        loaded = agent_cache.load(
            agent.id,
            agent.bundle_location,
            expand_env=agent.session_id is None and agent.owner is None,
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
        mcp_servers_editable=mcp_servers_editable,
        skills=skills,
        terminals=terminals,
    )


def create_builtin_agents_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    *,
    artifact_store: ArtifactStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the ``/v1/agents`` router: built-in discovery + standalone CRUD.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/agents*``.

    :param agent_store: Store of agents (built-ins, standalone, session-scoped).
    :param agent_cache: Cache for loading specs (populates ``mcp_servers``).
    :param artifact_store: Store for agent bundle bytes; required for the
        create/update routes (``None`` disables them — read-only deployment).
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated and standalone agents are owner-scoped.
    :returns: A FastAPI router exposing the agents API.
    """
    router = APIRouter()

    @router.get("/agents")
    async def list_builtin_agents(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """List built-in agents with cursor-based pagination.

        Returns only built-in agents (``session_id IS NULL AND owner IS
        NULL``); session-scoped and standalone user agents never appear
        here (the latter via ``GET /v1/agents/mine``).

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

    # Registered BEFORE ``/agents/{agent_id}`` so the static path wins.
    @router.get("/agents/mine")
    async def list_my_agents(request: Request) -> PaginatedList:
        """List the caller's standalone agents (newest-first).

        These are the user-owned, session-independent agents managed by
        the sidebar "Agents" section. Built-ins and session-scoped agents
        are excluded.

        :param request: The incoming FastAPI request (for auth).
        :returns: A :class:`PaginatedList` of the owner's agents.
        """
        owner = _owner_for(request)
        agents = agent_store.list_for_owner(owner)
        data = [_to_agent_object(a, agent_cache, mcp_servers_editable=True) for a in agents]
        return PaginatedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=False,
        )

    @router.post("/agents")
    async def create_agent(request: Request) -> AgentObject:
        """Create a standalone, owner-scoped agent from an uploaded bundle.

        Multipart form with a ``bundle`` file part (the agent ``.tar.gz``)
        and an optional ``description`` field. The agent persists
        independently of any session (``session_id IS NULL``, ``owner`` =
        caller) — reusable across sessions and unaffected by session
        deletion. The name comes from the bundle's spec; ``description``
        overrides the spec description when given.

        :param request: The incoming FastAPI request (multipart + auth).
        :returns: The created :class:`AgentObject`.
        :raises OmnigentError: 400 on a missing/invalid bundle, 409 on a
            duplicate name for this owner, 500 if bundle storage is
            unavailable.
        """
        if artifact_store is None:
            raise OmnigentError(
                "agent bundle storage is not configured on this server",
                code=ErrorCode.INTERNAL_ERROR,
            )
        owner = _owner_for(request)
        bundle_bytes, description = await _read_bundle_and_description(request)
        spec = validate_agent_bundle(
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        agent_id = generate_agent_id()
        location = bundle_location(agent_id, bundle_bytes)
        artifact_store.put(location, bundle_bytes)
        try:
            created = agent_store.create(
                agent_id=agent_id,
                name=spec.name or agent_id,
                bundle_location=location,
                description=description,
                owner=owner,
            )
        except Exception as exc:
            artifact_store.delete(location)
            raise OmnigentError(
                f"could not create agent {spec.name!r} (duplicate name?): {exc}",
                code=ErrorCode.CONFLICT,
            ) from exc
        return _to_agent_object(created, agent_cache, mcp_servers_editable=True)

    @router.get("/agents/{agent_id}")
    async def get_agent(request: Request, agent_id: str) -> AgentObject:
        """Return a built-in or caller-owned agent.

        :param request: The incoming FastAPI request (for auth).
        :param agent_id: Agent id, e.g. ``"ag_abc123"``.
        :returns: The :class:`AgentObject`.
        :raises OmnigentError: 404 if it does not exist or is not visible
            to the caller (other users' agents and session-scoped agents).
        """
        owner = _owner_for(request)
        agent = _visible_agent_or_404(agent_id, owner)
        editable = agent.owner is not None and agent.owner == owner
        return _to_agent_object(agent, agent_cache, mcp_servers_editable=editable)

    @router.put("/agents/{agent_id}")
    async def update_agent(request: Request, agent_id: str) -> AgentObject:
        """Replace a standalone agent's bundle (owner only).

        Multipart form with a ``bundle`` file part (the replacement
        ``.tar.gz``). Validates + stores it, points the agent row at it,
        and bumps its version (the spec cache re-reads on next load). The
        previous bundle artifact is best-effort deleted. Built-in and
        session-scoped agents are not editable here.

        :param request: The incoming FastAPI request (multipart + auth).
        :param agent_id: Agent id to update.
        :returns: The updated :class:`AgentObject`.
        :raises OmnigentError: 404 if not found/visible, 403 if not
            owned by the caller, 400 on a missing/invalid bundle.
        """
        if artifact_store is None:
            raise OmnigentError(
                "agent bundle storage is not configured on this server",
                code=ErrorCode.INTERNAL_ERROR,
            )
        owner = _owner_for(request)
        agent = _owned_agent_or_error(agent_id, owner)
        bundle_bytes, _ = await _read_bundle_and_description(request)
        validate_agent_bundle(
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        new_location = bundle_location(agent_id, bundle_bytes)
        artifact_store.put(new_location, bundle_bytes)
        old_location = agent.bundle_location
        updated = agent_store.update(agent_id, new_location)
        if updated is None:  # pragma: no cover — owner check already fetched it
            artifact_store.delete(new_location)
            raise OmnigentError("Agent not found", code=ErrorCode.NOT_FOUND)
        if old_location != new_location:
            try:
                artifact_store.delete(old_location)
            except Exception:  # noqa: BLE001 — orphaned old bundle is harmless
                _logger.debug("Failed to delete old bundle %s", old_location, exc_info=True)
        return _to_agent_object(updated, agent_cache, mcp_servers_editable=True)

    @router.delete("/agents/{agent_id}", status_code=204)
    async def delete_agent(request: Request, agent_id: str) -> None:
        """Delete a standalone agent (owner only).

        :param request: The incoming FastAPI request (for auth).
        :param agent_id: Agent id to delete.
        :raises OmnigentError: 404 if not found/visible, 403 if not
            owned by the caller (built-ins/session agents can't be
            deleted here).
        """
        owner = _owner_for(request)
        agent = _owned_agent_or_error(agent_id, owner)
        agent_store.delete(agent_id)
        if artifact_store is not None:
            try:
                artifact_store.delete(agent.bundle_location)
            except Exception:  # noqa: BLE001 — orphaned bundle is harmless
                _logger.debug(
                    "Failed to delete bundle %s", agent.bundle_location, exc_info=True
                )

    def _owner_for(request: Request) -> str:
        """Resolve the caller's owner id (the authenticated user).

        :param request: The incoming request.
        :returns: The user id used as the agent ``owner``. Falls back to
            :data:`RESERVED_USER_LOCAL` in single-user / no-auth mode
            (where ``require_user`` returns ``None``), mirroring the
            session-create path, so standalone agents always have a
            stable non-NULL owner and never collapse into global built-ins.
        :raises OmnigentError: 401 when auth is enabled and the caller is
            unauthenticated (``require_user``'s contract).
        """
        return _require_user(request, auth_provider) or RESERVED_USER_LOCAL

    def _visible_agent_or_404(agent_id: str, owner: str) -> Agent:
        """Return an agent visible to *owner* (built-in or owned), else 404."""
        agent = agent_store.get(agent_id)
        # Hide: missing, session-scoped (read via the session route), or
        # owned by a different user (404, not 403, to avoid enumeration).
        if agent is None or agent.session_id is not None:
            raise OmnigentError("Agent not found", code=ErrorCode.NOT_FOUND)
        if agent.owner is not None and agent.owner != owner:
            raise OmnigentError("Agent not found", code=ErrorCode.NOT_FOUND)
        return agent

    def _owned_agent_or_error(agent_id: str, owner: str) -> Agent:
        """Return an agent the caller owns, else 404 (unseen) / 403 (built-in)."""
        agent = _visible_agent_or_404(agent_id, owner)
        if agent.owner is None:
            raise OmnigentError(
                "Built-in agents cannot be modified",
                code=ErrorCode.FORBIDDEN,
            )
        return agent

    return router
