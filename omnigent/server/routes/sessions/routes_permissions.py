"""Permission management routes."""

from __future__ import annotations

import asyncio

from fastapi import (
    APIRouter,
    Query,
    Request,
)
from fastapi.responses import Response

from omnigent.entities import (
    Agent,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    SharingMode,
    workspace_sharing_blocked,
)
from omnigent.server.routes._auth_helpers import (
    get_session_owner_id as _get_session_owner_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._sessions.common import *
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import *
from omnigent.server.routes._sessions.orchestration import *
from omnigent.server.schemas import (
    AgentObject,
    GrantPermissionRequest,
    MCPServerSummary,
    PermissionObject,
    PolicySummary,
    SkillSummary,
)
from omnigent.spec.types import (
    FunctionPolicySpec,
    PolicySpec,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore


def register_permissions_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register the permissions routes on router."""

    @router.put(
        "/sessions/{session_id}/permissions",
        response_model=None,
        responses={200: {"model": PermissionObject}},
    )
    async def grant_permission(
        request: Request,
        session_id: str,
        body: GrantPermissionRequest,
    ) -> PermissionObject:
        """Grant or update a permission on a session.

        Requires manage-level access. Upserts the grant — can
        upgrade or downgrade an existing level. Auto-creates the
        grantee user if they don't exist yet.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session to grant access to,
            e.g. ``"conv_abc123"``.
        :param body: The grant request with ``user_id`` and ``level``.
        :returns: The resulting :class:`PermissionObject`.
        :raises OmnigentError: 404 if no session or no access,
            401 if unauthenticated.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_MANAGE, permission_store, conversation_store
        )
        # Server-wide sharing policy gate (see SharingMode). Applied only
        # to *new* grants — revoke/list and owner grants are unaffected.
        # ``getattr`` default keeps a hand-built app (a router mounted without
        # create_app, e.g. in a focused test) from AttributeError-ing; every
        # production path sets these via create_app.
        _sharing_mode = getattr(request.app.state, "sharing_mode", lambda: SharingMode.ON)()
        if _sharing_mode == SharingMode.OFF:
            raise OmnigentError(
                "Sharing has been disabled for this Omnigent server.",
                code=ErrorCode.FORBIDDEN,
            )
        # RESTRICTED_READ_ONLY blocks sharing entirely (even read) for a session
        # whose cwd is a home dir or the filesystem root — that workspace is too
        # broad to expose. Other sessions fall through to the read-only cap.
        if _sharing_mode == SharingMode.RESTRICTED_READ_ONLY:
            _conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if _conv is not None and workspace_sharing_blocked(_conv.workspace):
                raise OmnigentError(
                    "This session's working directory (a home or root directory) "
                    "cannot be shared on this Omnigent server.",
                    code=ErrorCode.FORBIDDEN,
                )
        if (
            _sharing_mode in (SharingMode.READ_ONLY, SharingMode.RESTRICTED_READ_ONLY)
            and body.level > LEVEL_READ
        ):
            raise OmnigentError(
                "Sharing is limited to read-only access on this Omnigent server.",
                code=ErrorCode.FORBIDDEN,
            )
        if permission_store is None:
            raise OmnigentError(
                "Permissions not enabled",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if body.user_id == user_id:
            raise OmnigentError(
                "Cannot modify your own permissions",
                code=ErrorCode.FORBIDDEN,
            )
        if body.user_id == RESERVED_USER_PUBLIC:
            # Public-access kill switch, independent of the sharing_mode gate
            # above (see app.state.public_sharing). Blocks the anyone-with-the
            # -link grant while leaving user-to-user sharing intact. ``getattr``
            # default mirrors the sharing_mode read above (hand-built apps).
            if not getattr(request.app.state, "public_sharing", lambda: True)():
                raise OmnigentError(
                    "Public access has been disabled for this Omnigent server.",
                    code=ErrorCode.FORBIDDEN,
                )
            if body.level > LEVEL_READ:
                raise OmnigentError(
                    "Public access is limited to read-only (level 1)",
                    code=ErrorCode.INVALID_INPUT,
                )
        existing = await asyncio.to_thread(permission_store.get, body.user_id, session_id)
        if existing is not None and existing.level == LEVEL_OWNER:
            raise OmnigentError(
                "Cannot modify owner permissions",
                code=ErrorCode.FORBIDDEN,
            )
        await asyncio.to_thread(permission_store.ensure_user, body.user_id)
        perm = await asyncio.to_thread(
            permission_store.grant, body.user_id, session_id, body.level
        )
        # Push the now-shared session to the GRANTEE's open tabs so it
        # appears in their sidebar without a list poll.
        _announce_session_added(body.user_id, session_id)
        return PermissionObject(
            user_id=perm.user_id,
            conversation_id=perm.conversation_id,
            level=perm.level,
        )

    @router.delete(
        "/sessions/{session_id}/permissions/{target_user_id}",
        status_code=204,
        response_model=None,
    )
    async def revoke_permission(
        request: Request,
        session_id: str,
        target_user_id: str,
    ) -> Response:
        """Revoke a user's permission on a session.

        Requires manage-level access. Cannot revoke your own
        manage grant (prevents orphaned sessions). Returns 204
        whether or not the grant existed (idempotent).

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session to revoke access from,
            e.g. ``"conv_abc123"``.
        :param target_user_id: User whose grant to revoke,
            e.g. ``"alice@example.com"``.
        :returns: 204 No Content.
        :raises OmnigentError: 404 if no session or no access,
            403 if attempting to revoke own manage grant.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_MANAGE, permission_store, conversation_store
        )
        if permission_store is None:
            raise OmnigentError(
                "Permissions not enabled",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if target_user_id == user_id:
            raise OmnigentError(
                "Cannot modify your own permissions",
                code=ErrorCode.FORBIDDEN,
            )
        existing = await asyncio.to_thread(permission_store.get, target_user_id, session_id)
        if existing is not None and existing.level == LEVEL_OWNER:
            raise OmnigentError(
                "Cannot revoke owner permissions",
                code=ErrorCode.FORBIDDEN,
            )
        await asyncio.to_thread(permission_store.revoke, target_user_id, session_id)
        return Response(status_code=204)

    @router.get(
        "/sessions/{session_id}/owner",
        response_model=None,
    )
    async def get_session_owner(
        request: Request,
        session_id: str,
    ) -> dict[str, str | None]:
        """Return the owner of a session.

        Requires read-level access.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session to look up,
            e.g. ``"conv_abc123"``.
        :returns: ``{"owner": "<user_id>"}`` or
            ``{"owner": null}``.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        return {"owner": _get_session_owner_id(session_id, permission_store)}

    @router.get(
        "/sessions/{session_id}/permissions",
        response_model=None,
    )
    async def list_permissions(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None, description="Cursor: user_id to start after"),
    ) -> dict:
        """List permission grants on a session with cursor pagination.

        Requires manage-level access.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session to list grants for,
            e.g. ``"conv_abc123"``.
        :param limit: Max grants to return (1–1000, default 100).
        :param after: Cursor — user_id to start after (exclusive).
        :returns: ``{"permissions": [...], "next_cursor": str|null}``.
        :raises OmnigentError: 404 if no session or no access.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_MANAGE, permission_store, conversation_store
        )
        if permission_store is None:
            raise OmnigentError(
                "Permissions not enabled",
                code=ErrorCode.INTERNAL_ERROR,
            )
        grants, next_cursor = await asyncio.to_thread(
            permission_store.list_for_session, session_id, limit=limit, after_user_id=after
        )
        return {
            "permissions": [
                PermissionObject(
                    user_id=g.user_id,
                    conversation_id=g.conversation_id,
                    level=g.level,
                )
                for g in grants
            ],
            "next_cursor": next_cursor,
        }

    return router


def _policy_type(spec: PolicySpec) -> str:
    """Return ``"function"`` for all policies."""
    if isinstance(spec, FunctionPolicySpec):
        return "function"
    return "unknown"


def _policy_description(spec: PolicySpec) -> str | None:
    """Return a short description for a policy spec.

    Looks up the policy registry for a human-readable
    description; falls back to the callable path.
    """
    if isinstance(spec, FunctionPolicySpec) and spec.function:
        from omnigent.policies.registry import get_entry

        entry = get_entry(spec.function.path)
        return entry.description if entry else spec.function.path
    return None


def _to_agent_object(agent: Agent, cache: AgentCache | None) -> AgentObject:
    """
    Convert a runtime :class:`Agent` entity to an API-layer
    :class:`AgentObject`.

    Loads the agent spec from *cache* to populate ``mcp_servers``,
    ``policies``, ``skills``, and (when the stored row has none) the
    ``description``. If the cache is ``None``, the spec is not
    cached, or the load fails, those fall back to empty lists / the
    stored value rather than raising — the endpoint must not fail
    because one spec can't be read.

    :param agent: The runtime agent entity.
    :param cache: Agent cache, or ``None`` in test setups.
    :returns: An :class:`AgentObject` for the API response.
    """
    mcp_servers: list[MCPServerSummary] = []
    policies: list[PolicySummary] = []
    skills: list[SkillSummary] = []
    terminals: list[str] = []
    # Harness/kind for the UI; None until the spec loads (mirrors the
    # GET /v1/agents catalog so both endpoints report it consistently).
    harness: str | None = None
    # Prefer the stored entity's description; fall back to the spec's
    # top-level description when the stored value is unset (single-file
    # YAML agents don't persist it at registration today). Lets the
    # new-session picker show a hover description without a migration.
    description: str | None = agent.description
    if cache is not None:
        try:
            loaded = cache.load(
                agent.id, agent.bundle_location, expand_env=agent.session_id is None
            )
            harness = loaded.spec.executor.harness_kind
            if description is None:
                description = loaded.spec.description
            # Declared terminal names, in spec order — the Web UI
            # gates its "new terminal" affordance on this list.
            terminals = list(loaded.spec.terminals or {})
            # Bundled skills only (mirrors GET /v1/agents); the merged
            # bundled + host-discovered set lives on the session snapshot.
            skills = [
                SkillSummary(name=s.name, description=s.description) for s in loaded.spec.skills
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
            if loaded.spec.guardrails and loaded.spec.guardrails.policies:
                policies = [
                    PolicySummary(
                        name=ps.name,
                        type=_policy_type(ps),
                        on=[
                            f"{sel.phase.value}:{sel.tool_name}"
                            if sel.tool_name
                            else sel.phase.value
                            for sel in (ps.on or [])
                        ],
                        description=_policy_description(ps),
                    )
                    for ps in loaded.spec.guardrails.policies
                ]
        except Exception:
            _logger.debug(
                "Failed to load spec for agent %s; mcp_servers/policies will be empty",
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
        mcp_servers_editable=(
            agent.session_id is not None and not (harness or "").endswith("-native")
        ),
        policies=policies,
        skills=skills,
        terminals=terminals,
    )
