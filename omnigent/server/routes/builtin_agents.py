"""Read-only route for discovering built-in agents (``GET /v1/agents``).

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

This is the read-only successor to the removed ``GET /api/agents`` list:
there is intentionally no create/update/delete — agent writes happen
through session creation.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import Agent
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.schemas import AgentObject, MCPServerSummary, PaginatedList, SkillSummary
from omnigent.spec.validator import icon_looks_path_like
from omnigent.stores import AgentStore

_logger = logging.getLogger(__name__)

# Media type served for each allowed icon suffix (lowercase, leading
# dot). Mirrors ``omnigent.spec.validator._ICON_IMAGE_SUFFIXES`` — a
# suffix the spec layer accepts but this map omits would 404 below.
_ICON_MEDIA_TYPES: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _resolve_icon_file(workdir: Path, icon: str) -> Path | None:
    """Resolve a spec ``icon`` path to a real file strictly under *workdir*.

    Defense-in-depth: the spec layer rejects absolute paths and ``..``
    at parse time, but this endpoint must not rely on that alone. The
    icon is resolved against the agent's extracted config directory and
    the result must stay inside it (following symlinks), or ``None`` is
    returned. ``None`` also covers a missing file.

    :param workdir: The agent's extracted image directory (icon root).
    :param icon: The spec's ``icon`` value, expected to be a
        path-like, agent-dir-relative image path.
    :returns: The resolved, contained file path, or ``None`` when the
        path escapes the directory or no file exists there.
    """
    # Reject absolute paths and parent-dir escapes up front: joining an
    # absolute path onto *workdir* would discard *workdir* entirely, and
    # a ``..`` component can climb out before the containment check.
    normalized = icon.replace("\\", "/")
    rel = Path(normalized)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    base = workdir.resolve()
    candidate = (workdir / rel).resolve()
    if not candidate.is_relative_to(base):
        return None
    if not candidate.is_file():
        return None
    return candidate


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
    # Icon is spec-only (no stored column, no migration): an emoji
    # grapheme or an agent-dir-relative image path. Stays None when the
    # spec declares none or the bundle can't be loaded.
    icon: str | None = None
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
        icon = loaded.spec.icon
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
        icon=icon,
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

    @router.get("/agents/{agent_id}/icon")
    async def get_agent_icon(request: Request, agent_id: str) -> FileResponse:
        """Serve an agent's icon file (read-only).

        Mounted with ``prefix="/v1"`` so the final path is
        ``/v1/agents/{agent_id}/icon``. Only agents whose spec declares
        a *path-like* icon have a file to serve; an emoji icon, an
        unset icon, a missing file, or an unknown agent all yield 404.
        The icon path is resolved strictly under the agent's extracted
        config directory (see :func:`_resolve_icon_file`).

        :param request: The incoming FastAPI request (for auth).
        :param agent_id: The id of the agent whose icon to serve.
        :returns: The icon file with a suffix-derived media type.
        :raises HTTPException: 404 when the agent, its icon, or the
            icon file does not exist, or the icon is an emoji.
        """
        _require_user(request, auth_provider)
        agent = agent_store.get(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        try:
            loaded = agent_cache.load(
                agent.id, agent.bundle_location, expand_env=agent.session_id is None
            )
        except Exception as exc:
            _logger.debug("Failed to load spec for agent %s icon", agent.id, exc_info=True)
            raise HTTPException(status_code=404, detail="agent icon not found") from exc
        icon = loaded.spec.icon
        if icon is None or not icon_looks_path_like(icon):
            raise HTTPException(status_code=404, detail="agent icon not found")
        media_type = _ICON_MEDIA_TYPES.get(Path(icon).suffix.lower())
        if media_type is None:
            raise HTTPException(status_code=404, detail="agent icon not found")
        resolved = _resolve_icon_file(loaded.workdir, icon)
        if resolved is None:
            raise HTTPException(status_code=404, detail="agent icon not found")
        return FileResponse(resolved, media_type=media_type)

    return router
