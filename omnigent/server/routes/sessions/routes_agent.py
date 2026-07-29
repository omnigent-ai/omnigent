"""Agent sub-resource routes: get/update session agent, MCP proxy."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
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
    LEVEL_EDIT,
    LEVEL_READ,
    AuthProvider,
    local_single_user_enabled,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._content_type import (
    require_json_content_type,
)
from omnigent.server.routes._sessions.common import *
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import *
from omnigent.server.routes._sessions.orchestration import *
from omnigent.server.routes.sessions.routes_permissions import (
    _policy_description,
    _policy_type,
    _to_agent_object,
)
from omnigent.server.schemas import (
    AgentObject,
    MCPServerSummary,
    PolicySummary,
    SkillSummary,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.permission_store import PermissionStore


def register_agent_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    artifact_store: ArtifactStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register the agent sub-resource routes on router."""

    @router.get("/sessions/{session_id}/agent")
    async def get_session_agent(
        request: Request,
        session_id: str,
    ) -> AgentObject:
        """
        Return the :class:`AgentObject` for the session's bound agent.

        Replaces the standalone ``GET /api/agents/{id}`` endpoint by
        resolving the agent through the session's ``agent_id`` foreign
        key. The caller only needs to know the session id.

        :param request: The incoming FastAPI request.
        :param session_id: Session identifier, e.g.
            ``"conv_abc123"``.
        :returns: The bound agent's :class:`AgentObject`.
        :raises OmnigentError: If the session or agent is not found.
        """
        user_id = _require_user(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = conversation_store.get_conversation(session_id)
            if conv is None:
                raise OmnigentError(
                    f"Session not found: {session_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if conv.agent_id is None:
            raise OmnigentError(
                "Session has no agent binding",
                code=ErrorCode.INTERNAL_ERROR,
            )
        agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent is None:
            raise OmnigentError(
                f"Agent not found: {conv.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )
        return _to_agent_object(agent, agent_cache)

    @router.get(
        "/sessions/{session_id}/agent/contents",
        response_class=Response,
        responses={
            200: {"content": {"application/gzip": {}}},
            404: {"description": "Session or agent not found"},
        },
    )
    async def get_session_agent_contents(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Download the raw ``.tar.gz`` agent bundle for the session's
        bound agent.

        Replaces ``GET /api/agents/{id}/contents``. Runners call this
        on cache miss to fetch the spec + bundled files.

        :param request: The incoming FastAPI request.
        :param session_id: Session identifier, e.g.
            ``"conv_abc123"``.
        :returns: Raw bundle bytes as ``application/gzip``.
        :raises OmnigentError: If the session, agent, or bundle is
            not found.
        """
        user_id = _require_user(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = conversation_store.get_conversation(session_id)
            if conv is None:
                raise OmnigentError(
                    f"Session not found: {session_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if conv.agent_id is None:
            raise OmnigentError(
                "Session has no agent binding",
                code=ErrorCode.INTERNAL_ERROR,
            )
        agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent is None:
            raise OmnigentError(
                f"Agent not found: {conv.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )
        if artifact_store is None:
            raise OmnigentError(
                "Artifact store not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        bundle_bytes = artifact_store.get(agent.bundle_location)
        if bundle_bytes is None:
            raise OmnigentError(
                "Agent bundle not found in artifact store",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return Response(
            content=bundle_bytes,
            media_type="application/gzip",
            headers={
                "X-Agent-Version": str(agent.version),
                "X-Agent-Name": agent.name,
                # Provenance for the runner's env-expansion decision:
                # session-scoped agents are
                # tenant-uploaded and must NOT have ${VAR} expanded
                # against the runner process env; template agents
                # (session_id is None) are operator-authored and may.
                # The runner fails safe (treats a missing header as
                # session-scoped → no expansion).
                "X-Agent-Session-Scoped": "true" if agent.session_id is not None else "false",
            },
        )

    @router.put(
        "/sessions/{session_id}/agent",
    )
    async def update_session_agent(
        request: Request,
        session_id: str,
        bundle: Annotated[UploadFile, File(...)],
    ) -> AgentObject:
        """
        Replace the session's agent bundle with a new upload.

        Validates the new bundle, checks that the spec name matches
        the existing agent, stores the bundle under a
        content-addressed key, updates the agent row, and warm-swaps
        the cache. Idempotent when the bundle content is unchanged.

        :param request: The incoming FastAPI request.
        :param session_id: Session identifier, e.g.
            ``"conv_abc123"``.
        :param bundle: Uploaded ``.tar.gz`` agent bundle file.
        :returns: The updated :class:`AgentObject`.
        :raises OmnigentError: If the session or agent is not found,
            the bundle is invalid, or the spec name doesn't match.
        """
        user_id = _require_user(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = conversation_store.get_conversation(session_id)
            if conv is None:
                raise OmnigentError(
                    f"Session not found: {session_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if conv.agent_id is None:
            raise OmnigentError(
                "Session has no agent binding",
                code=ErrorCode.INTERNAL_ERROR,
            )
        agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent is None:
            raise OmnigentError(
                f"Agent not found: {conv.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # Shared/template agents are read-only here;
        # mirrors the guard in session_mcp_servers._editable_agent.
        if agent.session_id is None:
            raise OmnigentError(
                "Built-in agents are read-only through this endpoint.",
                code=ErrorCode.INVALID_INPUT,
            )

        bundle_bytes = await bundle.read()
        # Run bundle validation (tar extraction + spec parse, both
        # blocking) off the event loop -- mirrors the POST
        # /sessions/bundled path. A malicious bundle that blocks here
        # must not hang the entire server loop. The
        # policy-handler allowlist is enforced only on a
        # shared / multi-user server; a trusted single-user/local server
        # keeps supporting custom handlers (see _create_session_from_bundle).
        spec = await asyncio.to_thread(
            validate_agent_bundle,
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
        if spec.name is None:
            raise OmnigentError("spec missing name", code=ErrorCode.INVALID_INPUT)

        if spec.name != agent.name:
            raise OmnigentError(
                f"spec name '{spec.name}' does not match agent "
                f"name '{agent.name}'; name is immutable",
                code=ErrorCode.INVALID_INPUT,
            )

        new_loc = bundle_location(agent.id, bundle_bytes)

        # Idempotency: same bundle content = no-op
        if new_loc == agent.bundle_location:
            return _to_agent_object(agent, agent_cache)

        if artifact_store is None:
            raise OmnigentError(
                "Artifact store not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        artifact_store.put(new_loc, bundle_bytes)
        updated = await asyncio.to_thread(agent_store.update, agent.id, new_loc)
        if updated is None:
            raise OmnigentError(
                f"Agent not found: {agent.id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        if agent_cache is not None:
            # Only operator-authored template agents
            # (session_id is None) may expand ${VAR} against the server
            # env; tenant session-scoped bundles must not.
            agent_cache.replace(
                agent.id, new_loc, bundle_bytes, expand_env=agent.session_id is None
            )

        return _to_agent_object(updated, agent_cache)

    # ── POST /sessions/{session_id}/mcp ──────────────────────────────────
    # MCP Streamable HTTP proxy endpoint. Only registered when a
    # ``runner_router`` is injected; returns 503 otherwise so test
    # setups that don't wire a runner skip the endpoint cleanly.

    @router.post(
        "/sessions/{session_id}/mcp",
        # Internal MCP proxy — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,  # Returns a raw Response with application/json
        # CSRF hardening: the MCP Streamable HTTP contract already mandates
        # an application/json request body; enforce it so a cross-site
        # text/plain request can't drive JSON-RPC against this proxy.
        dependencies=[Depends(require_json_content_type)],
    )
    async def mcp_proxy(
        session_id: str,
        request: Request,
    ) -> Response:
        """
        MCP Streamable HTTP proxy endpoint.

        Implements the MCP JSON-RPC 2.0 protocol over HTTP.  The AP
        server owns policy enforcement (TOOL_CALL / TOOL_RESULT); the
        runner owns execution via ``POST /v1/sessions/{id}/mcp/execute``
        (reached through the WS tunnel the runner opened at startup).
        This split ensures:

        - Policy runs on the Omnigent server where the ConversationStore and
          label state live.
        - Stdio MCP subprocesses spawn on the runner's machine with the
          correct ``cwd``, environment, and installed tooling.

        Supported methods:

        - ``initialize`` — capability negotiation.
        - ``tools/list`` — list all tools; delegated to runner execute.
        - ``tools/call`` — policy eval on AP, execution on runner.

        :param session_id: Session whose agent's MCP servers to proxy,
            e.g. ``"conv_abc123"``.
        :param request: The incoming FastAPI request. Body must be a
            JSON-RPC 2.0 object.
        :returns: A ``application/json`` JSON-RPC 2.0 response.
        :raises HTTPException: 503 when no ``runner_router`` is configured.
        """
        if runner_router is None:
            raise HTTPException(
                status_code=503,
                detail="MCP proxy requires a runner_router; none configured on this server",
            )

        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )

        # Parse JSON-RPC body. Return a parse-error response (not HTTP
        # 400) on failure — JSON-RPC errors travel in the body.
        try:
            body = await request.json()
        except Exception:
            return _mcp_error_response(None, -32700, "Parse error: invalid JSON")

        if not isinstance(body, dict):
            return _mcp_error_response(None, -32600, "Invalid Request: expected JSON object")

        rpc_id: int | str | None = body.get("id")
        method: str = body.get("method") or ""
        params: dict[str, Any] = body.get("params") or {}

        _logger.debug(
            "MCP proxy: session=%r method=%r rpc_id=%r",
            session_id,
            method,
            rpc_id,
        )

        if method == "initialize":
            # Minimal capability negotiation response. We declare
            # ``tools`` capability so MCP clients know to call
            # ``tools/list`` and ``tools/call``.
            return _mcp_ok_response(
                rpc_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "omnigent-mcp-proxy", "version": "1.0.0"},
                },
            )

        if method == "tools/list":
            return await _handle_mcp_tools_list(
                rpc_id,
                session_id,
                runner_router,
            )

        if method == "tools/call":
            _mcp_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            turn_actor = _mcp_conv.labels.get(_TURN_ACTOR_LABEL) if _mcp_conv is not None else None
            return await _handle_mcp_tools_call(
                rpc_id,
                session_id,
                params,
                conversation_store,
                agent_store,
                runner_router,
                actor=_build_actor(turn_actor or user_id),
                request=request,
            )

        return _mcp_error_response(rpc_id, -32601, f"Method not found: {method!r}")
