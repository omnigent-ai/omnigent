"""Permission management routes."""

from __future__ import annotations

import asyncio
from typing import TypedDict

from fastapi import (
    APIRouter,
    Query,
    Request,
)
from fastapi.responses import Response

from omnigent.db.account_authority import target_account_scope
from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import (
    Agent,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native.native_coding_agents import native_coding_agent_for_agent_name
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
from omnigent.server.routes._sessions.common import (
    _logger,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _announce_session_added,
)
from omnigent.server.schemas import (
    AgentObject,
    GrantPermissionRequest,
    MCPServerSummary,
    PermissionObject,
    PolicySummary,
    SkillSummary,
)
from omnigent.server.session_sharing import (
    require_public_grant_allowed,
    require_sharing_mode_allows_grant,
)
from omnigent.spec.types import (
    FunctionPolicySpec,
    PolicySpec,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import MAX_GRANTS_PAGE_SIZE, PermissionStore


class _PermissionListResponse(TypedDict):
    permissions: list[PermissionObject]
    next_cursor: str | None


def _session_workspace(conversation_store: ConversationStore, session_id: str) -> str | None:
    """Return the session's recorded working directory, or ``None`` if it has none."""
    conversation = conversation_store.get_conversation(session_id)
    return conversation.workspace if conversation is not None else None


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
        upgrade or downgrade an existing level unless ``ensure_minimum``
        preserves stronger access. Auto-creates the
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
        # Server-wide sharing policy (see SharingMode), for new grants only. Runs off
        # the event loop in every mode: the mode callable can read a file, and
        # RESTRICTED_READ_ONLY also loads the session's workspace from the store.
        await asyncio.to_thread(
            require_sharing_mode_allows_grant,
            request.app.state,
            level=body.level,
            load_workspace=lambda: _session_workspace(conversation_store, session_id),
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
            require_public_grant_allowed(request.app.state, level=body.level)
        target = await asyncio.to_thread(permission_store.get_user, body.user_id)
        with target_account_scope(body.user_id, target.account_generation if target else None):
            existing = await asyncio.to_thread(permission_store.get, body.user_id, session_id)
            if existing is not None and existing.level == LEVEL_OWNER:
                raise OmnigentError(
                    "Cannot modify owner permissions",
                    code=ErrorCode.FORBIDDEN,
                )
            await asyncio.to_thread(permission_store.ensure_user, body.user_id)
            perm = await asyncio.to_thread(
                permission_store.grant,
                body.user_id,
                session_id,
                body.level,
                ensure_minimum=body.ensure_minimum,
            )
        # Push the now-shared session to the GRANTEE's open tabs so it
        # appears in their sidebar without a list poll.
        _announce_session_added(body.user_id, session_id)
        add_audit_attrs(target_user_id=body.user_id, level=perm.level)
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
        """Revoke a permission on a session — someone else's, or your own.

        Two shapes, one endpoint:

        * *Revoke* — removing **another** user's grant is a management
          action and needs manage access.
        * *Leave* — removing **your own** grant ("unshare myself", so a
          shared session stops cluttering your sidebar) needs only read
          access, since giving up access requires no privilege.

        Either way the owner grant is protected: it is what keeps the
        session reachable, so revoking it would orphan the session. That
        guard covers the self case too — an owner cannot leave, and
        archives or deletes instead. A manage-level *grantee* is still a
        guest and may leave. Leaving records no lasting refusal, so the
        owner re-granting brings the session back.

        Idempotent: 204 whether or not a grant existed.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session to revoke access from,
            e.g. ``"conv_abc123"``.
        :param target_user_id: User whose grant to revoke, or the caller's
            own id to leave, e.g. ``"alice@example.com"``.
        :returns: 204 No Content.
        :raises OmnigentError: 404 if no session or no access, 403 when the
            target holds the owner grant, 400 when leaving a sub-agent
            session, 401 if unauthenticated.
        """
        user_id = _require_user(request, auth_provider)
        # Leaving is self-service; revoking someone else is management. Read
        # access is the floor for the self case — enough to prove the session
        # exists and is visible to the caller without leaking others' via
        # 403/404.
        leaving_self = target_user_id == user_id
        await _require_access(
            user_id,
            session_id,
            LEVEL_READ if leaving_self else LEVEL_MANAGE,
            permission_store,
            conversation_store,
        )
        if permission_store is None:
            raise OmnigentError(
                "Permissions not enabled",
                code=ErrorCode.INTERNAL_ERROR,
            )
        # Single-user mode has one identity that owns everything, so a
        # self-revoke there could only strand the local user's own sessions.
        if leaving_self and user_id is None:
            raise OmnigentError(
                "Cannot leave a session on a single-user server.",
                code=ErrorCode.FORBIDDEN,
            )
        # A sub-agent holds no grant of its own — access delegates to its
        # parent — so revoking here would delete nothing and still report
        # success, leaving the row in place. Scoped to the self case: the
        # manager path is unchanged.
        if leaving_self:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is not None and conv.parent_conversation_id is not None:
                raise OmnigentError(
                    "This is a sub-agent session; its access comes from its parent. "
                    f"Leave the parent session {conv.parent_conversation_id!r} instead.",
                    code=ErrorCode.INVALID_INPUT,
                )
        existing = await asyncio.to_thread(permission_store.get, target_user_id, session_id)
        if existing is not None and existing.level == LEVEL_OWNER:
            raise OmnigentError(
                "Cannot leave a session you own. Delete or archive it instead."
                if leaving_self
                else "Cannot revoke owner permissions",
                code=ErrorCode.FORBIDDEN,
            )
        await asyncio.to_thread(permission_store.revoke, target_user_id, session_id)
        # The leaver's own tab drops the row via the client mutation's success
        # handler; their other tabs converge on the next watch-set diff, which
        # reports the now-inaccessible id as removed. No extra push needed.
        add_audit_attrs(target_user_id=target_user_id, left_self=leaving_self)
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
        limit: int = Query(default=100, ge=1, le=MAX_GRANTS_PAGE_SIZE),
        after: str | None = Query(default=None, description="Cursor: user_id to start after"),
    ) -> _PermissionListResponse:
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


def _to_agent_object(
    agent: Agent,
    cache: AgentCache | None,
    *,
    mcp_servers_editable: bool,
    terminals_override: list[str] | None = None,
    mcp_selection: dict[str, str] | None = None,
    mcp_registry_editable: bool = False,
) -> AgentObject:
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
    :param terminals_override: Selected host's shell inventory. Applied only
        when the loaded spec is a recognized native wrapper.
    :param mcp_selection: Session MCP selection labels, when called for a session.
    :param mcp_registry_editable: Whether the caller can change session MCP selections.
    :param mcp_servers_editable: Whether the authenticated caller may mutate
        MCP configuration for the session serving this object.
    :returns: An :class:`AgentObject` for the API response.
    """
    mcp_servers: list[MCPServerSummary] = []
    policies: list[PolicySummary] = []
    skills: list[SkillSummary] = []
    terminals: list[str] = []
    loads_user_settings = True
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
                agent.id, agent.bundle_location, expand_env=agent.operator_authored
            )
            harness = loaded.spec.executor.harness_kind
            if description is None:
                description = loaded.spec.description
            # Declared terminal names, in spec order — the Web UI
            # gates its "new terminal" affordance on this list.
            terminals = (
                list(terminals_override)
                if terminals_override is not None
                and native_coding_agent_for_agent_name(loaded.spec.name) is not None
                else list(loaded.spec.terminals or {})
            )
            # Bundled suggestions stay available while the host catalog loads.
            skills = [
                SkillSummary(name=s.name, description=s.description, display_name=s.display_name)
                for s in loaded.spec.skills
                if s.user_invocable
            ]
            loads_user_settings = loaded.spec.loads_user_settings
            from omnigent.mcp_selection import selected_mcp_spec

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
                for srv in selected_mcp_spec(loaded.spec, mcp_selection or {}).mcp_servers
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
        mcp_registry_editable=mcp_registry_editable,
        mcp_servers_editable=(
            mcp_servers_editable
            and agent.session_id is not None
            and not (harness or "").endswith("-native")
        ),
        policies=policies,
        skills=skills,
        terminals=terminals,
        loads_user_settings=loads_user_settings,
    )
