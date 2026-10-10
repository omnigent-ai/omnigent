"""Routes for reading, installing, and removing agents (``/v1/agents``).

Server agents are the long-lived, shared agents the server provides out of
the box: the seeded ``claude-native-ui`` agent plus anything registered at
startup with ``omnigent server --agent``. ``GET /v1/agents`` lists them for
the new-session picker, which then creates a session with
``POST /v1/sessions {agent_id, host_id, workspace}``. See
``designs/BUILTIN_AGENTS.md``.

User agents belong to one user (``designs/REUSABLE_USER_AGENTS.md``):
``POST /v1/agents`` installs one (``omnigent agent add``),
``GET /v1/agents?scope=user`` lists the caller's own, and
``DELETE /v1/agents/{id}`` removes one. Uploading a bundle with a new
session (multipart ``POST /v1/sessions``) also creates one.
``GET /v1/agents/{id}`` reads a server agent or the caller's own user agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import UploadFile
from starlette.types import Message

from omnigent.db.utils import builtin_agent_id, installed_agent_id
from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.bundles import content_bundle_location, validate_agent_bundle
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.schemas import AgentObject, MCPServerSummary, PaginatedList, SkillSummary
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore

_logger = logging.getLogger(__name__)

# Upper bound on an uploaded install bundle, enforced while the request streams in.
MAX_INSTALL_BUNDLE_BYTES = 100 * 1024 * 1024
# Room for the multipart boundaries and part headers around the bundle.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
# Most agents one ``scope=user`` page returns (the store reads a bounded window).
_USER_AGENTS_PAGE_LIMIT = 50
# How many sessions using an agent we count before reporting "N+".
_IN_USE_COUNT_CAP = 100


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
        # Only operator-authored agents (built-ins, ``--agent``) expand
        # ${VAR} against the server env; user agents are tenant input.
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        )
        if description is None:
            description = loaded.spec.description
        # Declared terminal names, in spec order (mirrors the
        # session-agent endpoint so both report it consistently).
        terminals = list(loaded.spec.terminals or {})
        # Bundled suggestions stay available while the host catalog loads.
        skills = [
            SkillSummary(name=s.name, description=s.description, display_name=s.display_name)
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


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"agent bundle exceeds {MAX_INSTALL_BUNDLE_BYTES // (1024 * 1024)} MiB",
    )


def _body_capped(request: Request) -> Request:
    """Wrap *request* so reading more than the install cap raises 413 mid-stream.

    Multipart parsing spools file parts to disk, so a size check after
    ``form()`` would come too late for a huge (or chunked) upload.
    """
    limit = MAX_INSTALL_BUNDLE_BYTES + _MULTIPART_OVERHEAD_BYTES
    received = 0

    async def receive() -> Message:
        nonlocal received
        message = await request.receive()
        if message["type"] == "http.request":
            received += len(message.get("body", b""))
            if received > limit:
                raise _too_large()
        return message

    return Request(request.scope, receive)


def install_user_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    agent_cache: AgentCache,
    *,
    owner: str | None,
    name: str,
    bundle_bytes: bytes,
) -> Agent:
    """
    Create or update *owner*'s user agent named *name* from a validated bundle.

    The id comes from owner and name (:func:`installed_agent_id`), so a
    reinstall updates the same row in place (stable id, bumped version) and
    the primary key keeps that identity atomic under concurrent installs.
    Sessions using the agent pick up the new bundle on their next turn.

    :param owner: Installing user, or ``None`` on an auth-less server.
    :param name: Agent name from the bundle's spec, e.g. ``"orion"``.
    :param bundle_bytes: Gzipped tarball already checked by
        :func:`validate_agent_bundle`.
    :returns: The created or updated agent.
    :raises OmnigentError: ``CONFLICT`` when a server agent uses the name.
    """
    if agent_store.get_by_name(name) is not None:
        # A same-named user agent would be hidden behind the server agent.
        raise OmnigentError(
            f"{name!r} is a server agent; rename your agent to install it.",
            code=ErrorCode.CONFLICT,
        )
    agent_id = installed_agent_id(owner, name)
    # Named by content, so reinstalling the same files is a no-op however the
    # client tarred them.
    location = content_bundle_location(agent_id, bundle_bytes)
    existing = agent_store.get(agent_id)
    if existing is None:
        artifact_store.put(location, bundle_bytes)
        try:
            # No stored description: the listing reads it from the current
            # bundle's spec, so a reinstall never shows a stale one.
            return agent_store.create_user_agent(agent_id, name, location, owner=owner)
        except IntegrityError:
            # A concurrent install of the same agent won; update that row instead.
            existing = agent_store.get(agent_id)
            if existing is None:
                raise
    if existing.bundle_location == location:
        # Same files: restore the blob if it vanished while the row survived.
        if not artifact_store.exists(location):
            artifact_store.put(location, bundle_bytes)
        return existing
    artifact_store.put(location, bundle_bytes)
    updated = agent_store.update(agent_id, location)
    agent_cache.evict(agent_id)
    if updated is None:  # removed concurrently
        raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
    return updated


def create_builtin_agents_router(
    agent_store: AgentStore,
    agent_cache: AgentCache,
    *,
    artifact_store: ArtifactStore | None = None,
    conversation_store: ConversationStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the ``/v1/agents`` router (server agent list, user agents).

    Mounted with ``prefix="/v1"`` so the final path is ``/v1/agents``.

    :param agent_store: Store whose ``list()`` returns only server agents and
        whose ``*_user_agent*`` methods back user agents.
    :param agent_cache: Cache for loading specs (populates
        ``mcp_servers`` on each agent).
    :param artifact_store: Bundle store for installs; ``None`` disables
        ``POST /v1/agents``.
    :param conversation_store: Counts sessions using an agent before
        removing it; ``None`` skips the check.
    :param auth_provider: Optional auth provider; when set, the caller
        must be authenticated.
    :returns: A FastAPI router exposing the list, detail, install, and remove routes.
    """
    router = APIRouter()

    def require_user_agents() -> None:
        if not agent_store.supports_user_agents:
            raise HTTPException(status_code=404, detail="user agents are not supported")

    @router.get("/agents")
    async def list_builtin_agents(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        scope: str | None = Query(default=None, pattern="^user$"),
    ) -> PaginatedList:
        """List server agents, or with ``scope=user`` the caller's own agents.

        Server agents come from ``agent_store.list()``, which never returns
        user agents. ``scope=user`` returns the caller's user agents newest
        first, at most 50 per page and paged with ``after`` only; ``last_id``
        is the last row the server read, which may not be in ``data``.

        :param request: The incoming FastAPI request (for auth).
        :param limit: Maximum number of agents to return (1-1000; ``scope=user``
            caps it at 50).
        :param after: Cursor: return agents after this id.
        :param before: Cursor: return agents before this id (server agents only).
        :param order: Sort order, ``"asc"`` or ``"desc"`` (server agents only).
        :param scope: ``"user"`` to list the caller's own agents.
        :returns: A :class:`PaginatedList` of agents.
        """
        user_id = _require_user(request, auth_provider)
        if scope != "user":
            page = agent_store.list(limit=limit, after=after, before=before, order=order)
            return PaginatedList(
                data=[_to_agent_object(a, agent_cache) for a in page.data],
                first_id=page.first_id,
                last_id=page.last_id,
                has_more=page.has_more,
            )
        require_user_agents()
        if before is not None or order != "desc":
            raise OmnigentError(
                "scope=user lists newest first and pages with 'after' only",
                code=ErrorCode.INVALID_INPUT,
            )
        page = await asyncio.to_thread(
            agent_store.list_user_agents,
            user_id,
            limit=min(limit, _USER_AGENTS_PAGE_LIMIT),
            after=after,
        )
        data = await asyncio.to_thread(
            lambda: [_to_agent_object(a, agent_cache) for a in page.data]
        )
        return PaginatedList(
            data=data, first_id=page.first_id, last_id=page.last_id, has_more=page.has_more
        )

    @router.get("/agents/{agent_id}")
    async def get_agent(request: Request, agent_id: str) -> AgentObject:
        """Return the list summary for a server agent or the caller's own user agent.

        Missing and inaccessible agents both return 404. Sharing a session
        does not grant access to its owner's agent through this route.

        :param request: The incoming request (for auth).
        :param agent_id: Agent to read.
        :returns: The same redacted :class:`AgentObject` used by the list routes.
        """
        user_id = _require_user(request, auth_provider)
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        if agent is None or not (
            agent.operator_authored
            or (
                agent_store.supports_user_agents
                and agent.kind == "user"
                and agent.created_by == user_id
            )
        ):
            raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
        return await asyncio.to_thread(_to_agent_object, agent, agent_cache)

    # Multipart is CORS-safelisted, so a cross-site form post needs the Origin check.
    @router.post("/agents", dependencies=[Depends(require_trusted_origin)])
    async def install_agent(request: Request) -> AgentObject:
        """Install (or reinstall) the caller's user agent from a bundle.

        Multipart form with one ``bundle`` part: a gzipped tarball, the same
        shape multipart ``POST /v1/sessions`` accepts. The bundle is validated
        like any tenant upload and never executed here.

        :param request: The incoming request carrying the ``bundle`` part.
        :returns: The installed agent as an :class:`AgentObject`.
        """
        user_id = _require_user(request, auth_provider)
        require_user_agents()
        if artifact_store is None:
            raise OmnigentError("artifact store is not configured", code=ErrorCode.INTERNAL_ERROR)
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_INSTALL_BUNDLE_BYTES + _MULTIPART_OVERHEAD_BYTES:
            raise _too_large()
        bundle = (await _body_capped(request).form()).get("bundle")
        if not isinstance(bundle, UploadFile):
            raise HTTPException(status_code=422, detail="multipart part 'bundle' is required")
        if bundle.size is not None and bundle.size > MAX_INSTALL_BUNDLE_BYTES:
            raise _too_large()
        bundle_bytes = await bundle.read()
        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        if spec.name is None:  # validate_agent_bundle rejects this; narrows the type
            raise OmnigentError("agent spec has no name", code=ErrorCode.INVALID_INPUT)
        agent = await asyncio.to_thread(
            install_user_agent,
            agent_store,
            artifact_store,
            agent_cache,
            owner=user_id,
            name=spec.name,
            bundle_bytes=bundle_bytes,
        )
        # Loads (extracts) the just-written bundle; keep it off the event loop.
        return await asyncio.to_thread(_to_agent_object, agent, agent_cache)

    @router.delete("/agents/{agent_id}")
    async def remove_agent(
        request: Request, agent_id: str, force: bool = Query(default=False)
    ) -> Any:
        """Remove one of the caller's user agents.

        Sessions using it keep running while their runner holds the spec, then
        report that the agent no longer exists, so while any exist this returns
        409 with ``sessions_in_use`` unless ``force`` is set. Server agents,
        other users' agents, and unowned rows all read as 404.

        :param request: The incoming request (for auth).
        :param agent_id: User agent to remove.
        :param force: Remove even though sessions still use the agent.
        :returns: ``{"id": agent_id, "deleted": True}``, or the 409 response.
        """
        user_id = _require_user(request, auth_provider)
        require_user_agents()
        agent = await asyncio.to_thread(agent_store.get, agent_id)
        if agent is None or agent.operator_authored or agent.created_by != user_id:
            raise OmnigentError(f"Agent not found: {agent_id!r}", code=ErrorCode.NOT_FOUND)
        if not force and conversation_store is not None:
            in_use = await asyncio.to_thread(
                conversation_store.count_sessions_for_agent, agent.id, _IN_USE_COUNT_CAP + 1
            )
            if in_use:
                count = f"{_IN_USE_COUNT_CAP}+" if in_use > _IN_USE_COUNT_CAP else str(in_use)
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": {
                            "code": "agent_in_use",
                            "message": (
                                f"{count} session(s) still use {agent.name!r}; removing it "
                                "will break them. Retry with force=true to remove anyway."
                            ),
                        },
                        "sessions_in_use": count,
                    },
                )
        # Blobs stay: copies of this agent may share its bundle_location.
        await asyncio.to_thread(agent_store.delete, agent.id)
        agent_cache.evict(agent.id)
        return {"id": agent.id, "deleted": True}

    return router
