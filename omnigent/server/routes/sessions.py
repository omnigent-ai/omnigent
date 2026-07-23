"""Routes for the Sessions API (``/v1/sessions``).

These endpoints expose a thin, harness-agnostic surface over an
agent's conversation: create a session bound to an agent, post events
(messages, tool outputs, interrupts), read a snapshot, and live-tail
the SSE stream. The session is implemented on top of the existing
conversation-item + task + live-stream machinery — this module is a
boundary translation layer, not a new runtime.

Input dispatch (POST /events) persists the item to
``conversation_items`` and forwards to the bound runner over the WS
tunnel. The persist-before-forward order is invariant I1 in
``designs/SESSION_REARCHITECTURE.md`` — a snapshot read immediately
after POST observes the input in ``items``.

The reconnect contract is **snapshot + live tail**, not replay: a
client opens the live stream and ``GET``s the snapshot, then
deduplicates by item id any events that fire between the two reads.
See ``server/API.md`` for the full contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import secrets
import time
import urllib.parse
from collections.abc import Callable
from typing import Annotated, Any

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.responses import Response, StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.cost_plan import (
    reserved_cost_control_keys,
)
from omnigent.db.utils import generate_agent_id
from omnigent.entities import (
    Agent,
    CommentsFingerprint,
    Conversation,
    ErrorData,
    NewConversationItem,
    StoredFile,
    synthesize_conversation_title,
)
from omnigent.entities.conversation import (
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.entities.session_resources import session_resource_view_to_dict
from omnigent.errors import ElicitationDeclinedError, ErrorCode, OmnigentError
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.model_override import validate_model_override
from omnigent.native_coding_agents import (
    native_coding_agent_for_terminal_name,
)
from omnigent.policies.types import (
    PolicyAction,
)
from omnigent.reasoning_effort import (
    EFFORT_CLEAR_VALUES,
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    get_agent_cache,
    get_caps,
    get_policy_store,
    pending_elicitations,
    pending_inputs,
    session_stream,
    user_session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE  # noqa: F401
from omnigent.runtime.policies.builder import (
    any_policies_apply,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.server import presence

# Elicitation-registry state and dataclasses. Tests reach these through this
# facade module (``sessions._ParkedHarnessElicitation`` etc.); re-export them so
# the module namespace matches the pre-split file.
from omnigent.server._elicitation_registry import (  # noqa: F401
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    AuthProvider,
    SharingMode,
    local_single_user_enabled,
    workspace_sharing_blocked,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    prepare_background_session_title,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.host_registry import HostRegistry, RunnerExitReports
from omnigent.server.mcp_pool import ServerMcpPool
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    get_permission_level as _get_permission_level,
)
from omnigent.server.routes._auth_helpers import (
    get_session_owner_id as _get_session_owner_id,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._codex_elicitation import parse_codex_elicitation_request
from omnigent.server.routes._content_type import (
    require_json_content_type,
    require_json_or_multipart_content_type,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._origin import require_trusted_origin

# Shared constants, state, and small dataclasses live in the _sessions.common
# leaf module; import them here so this module and its re-exporters see the same
# objects. The mutable caches are shared by reference across the package.
from omnigent.server.routes._sessions.common import *
from omnigent.server.routes._sessions.common import (  # noqa: F401
    get_server_runner_router,
    set_server_runner_router,
)

# Lower-layer helpers (SSE builders, publishers, persistence, runner-forward
# primitives) live in _sessions.helpers.
from omnigent.server.routes._sessions.helpers import *

# Runner-forward / ASK-gate helpers are patched by tests on this facade module
# (``monkeypatch(sessions.<X>)``). Their real bodies live in the package as
# ``<X>_impl`` and the siblings call a lazy proxy that resolves the attribute
# here at call time, so a facade patch is honored across module boundaries.
# Bind the real bodies here (overriding the star-imported proxies) so the facade
# attribute is the implementation tests replace.
from omnigent.server.routes._sessions.helpers import (
    _build_policy_engine_from_spec_impl as _build_policy_engine_from_spec,  # noqa: F401
)
from omnigent.server.routes._sessions.helpers import (
    _compact_lock_impl as _compact_lock,  # noqa: F401
)
from omnigent.server.routes._sessions.helpers import (
    _forward_session_change_to_runner_impl as _forward_session_change_to_runner,
)
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client_for_resource_access_impl as _get_runner_client_for_resource_access,
)
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client_impl as _get_runner_client,
)
from omnigent.server.routes._sessions.helpers import (
    _launch_runner_on_host_impl as _launch_runner_on_host,
)
from omnigent.server.routes._sessions.helpers import (
    _load_agent_spec_for_session_impl as _load_agent_spec_for_session,
)
from omnigent.server.routes._sessions.helpers import (
    _poll_request_disconnect_impl as _poll_request_disconnect,  # noqa: F401
)
from omnigent.server.routes._sessions.helpers import (
    _publish_sandbox_status_impl as _publish_sandbox_status,
)
from omnigent.server.routes._sessions.helpers import (
    _resolve_harness_impl as _resolve_harness,  # noqa: F401
)
from omnigent.server.routes._sessions.helpers import (
    _signal_terminal_resolved_harness_elicitation_impl as _signal_terminal_resolved_harness_elicitation,  # noqa: E501,F401
)
from omnigent.server.routes._sessions.helpers import (
    _stop_session_via_runner_impl as _stop_session_via_runner,
)
from omnigent.server.routes._sessions.helpers import (
    _wait_for_runner_client_impl as _wait_for_runner_client,
)

# Higher-layer orchestration flows (runner relay, session-event dispatch,
# native-terminal launch, MCP tool calls) live in _sessions.orchestration.
from omnigent.server.routes._sessions.orchestration import *
from omnigent.server.routes._sessions.orchestration import (
    _dispatch_session_event_to_runner_impl as _dispatch_session_event_to_runner,
)
from omnigent.server.routes._sessions.orchestration import (
    _ensure_runner_relay_ready_impl as _ensure_runner_relay_ready,
)
from omnigent.server.routes._sessions.orchestration import (
    _hold_native_ask_gate_impl as _hold_native_ask_gate,
)
from omnigent.server.routes._sessions.orchestration import (
    _kick_managed_wake_impl as _kick_managed_wake,  # noqa: F401
)
from omnigent.server.routes._sessions.orchestration import (
    _publish_runner_recovered_status_impl as _publish_runner_recovered_status,
)
from omnigent.server.schemas import (
    AgentObject,
    AutomaticSessionRenameRequest,
    AutomaticSessionRenameResponse,
    BrowserActionRequestEvent,
    ChildSessionList,
    ConversationDeleted,
    CopiedFile,
    CopyFilesRequest,
    CopyFilesResponse,
    CreatedSessionResponse,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    ErrorDetail,
    GrantPermissionRequest,
    McpServerStartup,
    MCPServerSummary,
    PaginatedList,
    PermissionObject,
    PolicySummary,
    ReadStatePutRequest,
    SessionAgentChangedEvent,
    SessionCreateRequest,
    SessionEventInput,
    SessionForkRequest,
    SessionLabelsResponse,
    SessionList,
    SessionListItem,
    SessionProjectSummary,
    SessionResourceObject,
    SessionResourcePaginatedList,
    SessionResponse,
    SessionSwitchAgentRequest,
    SkillSummary,
    UpdateSessionRequest,
)
from omnigent.session_lifecycle import (
    is_session_closed,
    labels_with_closed_status,
)
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PolicySpec,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import (
    PROJECT_LABEL_KEY,
    ConversationNotFoundError,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.telemetry import emit as _tel_emit
from omnigent.telemetry.events import SessionDeletedEvent as _TelSessionDeletedEvent
from omnigent.telemetry.events import SessionStoppedEvent as _TelSessionStoppedEvent
from omnigent.telemetry.installation_id import get_installation_id as _get_installation_id
from omnigent.tools.client_specified import parse_client_side_tool_specs

# ── Module-level constants (rule 34) ──────────────────────────────


# ── MCP proxy helpers ───────────────────────────────────────────────────────
#
# These module-level functions implement the JSON-RPC 2.0 handlers for
# ``POST /v1/sessions/{session_id}/mcp``.  They live outside the router
# factory so the factory closure stays compact.


def create_sessions_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
    mcp_pool: ServerMcpPool | None = None,  # noqa: ARG001 — retained for API compat
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    comment_store: CommentStore | None = None,
    runner_tunnel_tokens: frozenset[str] | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
    host_registry: HostRegistry | None = None,
    project_store: ProjectStore | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
) -> APIRouter:
    """
    Factory that builds the sessions router.

    Stores are closed over rather than dependency-injected, matching
    the convention established by the other route modules
    (conversations, agents, files).

    :param conversation_store: Store for conversation and item
        persistence.
    :param agent_store: Store for agent lookups by ID.
    :param file_store: Store for file metadata CRUD. Required for
        session-scoped file endpoints (Phase 1c). ``None`` in
        test setups that don't exercise file routes.
    :param artifact_store: Store for binary file content and agent
        bundles. Required for bundled session creation and session
        file upload/download.
    :param runner_router: Router used to validate registered
        runners for ``PATCH /v1/sessions/{id}``. ``None`` only in
        tests that do not exercise runner binding.
    :param auth_provider: Auth provider for user identity
        extraction. ``None`` disables permission checks.
    :param permission_store: Permission store for session-level
        access control. ``None`` disables permission checks.
    :param agent_cache: Optional agent cache for loading parsed specs
        from bundles. Used to populate ``llm_model`` and
        ``context_window`` in :class:`SessionResponse`. ``None`` in
        test setups that don't exercise context-window lookup.
    :param mcp_pool: Unused; retained for API compatibility. MCP
        execution is now delegated to the runner via
        ``POST /v1/sessions/{id}/mcp/execute``. The
        ``POST /v1/sessions/{id}/mcp`` endpoint is enabled whenever
        ``runner_router`` is set.
    :param liveness_lookup: Bulk session-liveness lookup
        (the server's ``_bulk_session_liveness``): maps a list of
        session ids to ``{id: SessionLiveness}``, each carrying
        strict ``runner_online`` and ``host_online``. When provided,
        the ``GET /sessions`` list and ``WS /sessions/updates`` stream
        include both fields per item, and the stream pushes a delta
        when liveness flips, so the web app can stop polling
        ``GET /health``. ``None`` (e.g. in focused tests) omits the
        fields and the client falls back to its ``/health`` poll.
    :param comment_store: Store for per-session review comments. When
        provided, ``GET /sessions`` and ``WS /sessions/updates`` items
        carry the per-session comments fingerprint
        (``comments_count`` / ``comments_updated_at``) so the web app
        can refresh its comment list when another user or the agent
        mutates comments. ``None`` (e.g. in focused tests or servers
        without comments wired) emits the no-comments shape.
    :param runner_tunnel_tokens: The server's runner tunnel-token
        allow-list (same value the tunnel router receives), used to
        authorize runner writes to the policy-owned ``cost_control.*``
        labels on ``PATCH /v1/sessions/{id}``. ``None`` when the
        server has no allow-list (token-bound runner ids are then the
        only accepted proof).
    :param host_registry: Live host tunnels. Lets the filesystem
        endpoints read a session's workspace over its host tunnel when
        the runner is offline, so the file panel stays live without
        waking the agent. ``None`` disables the fallback (the endpoints
        then 503 on an offline runner, as before).
    :param project_store: Store for first-class projects. Required to
        validate ownership when ``PATCH /v1/sessions/{id}`` files a
        session into a project. ``None`` disables the move-into-project
        action (a non-empty ``project_id`` is then rejected as unsupported).
    :param background_title_coordinator: Optional app-owned coordinator for
        semantic title generation after first-turn forwarding. ``None`` disables
        background titles in focused router tests.
    :returns: A configured :class:`APIRouter` exposing the
        ``/sessions`` endpoints.
    """
    router = APIRouter()

    # ── POST /sessions ───────────────────────────────────────────

    @router.post(
        "/sessions",
        status_code=201,
        response_model=None,
        # CSRF hardening: this route dispatches on Content-Type (JSON vs
        # multipart bundled-create), so reject text/plain and other simple
        # types up front while still allowing both legitimate body shapes.
        # The multipart shape is CORS-safelisted, so the content-type guard
        # alone can't stop a cross-site bundle upload — require_trusted_origin
        # closes that gap (allows absent Origin for non-browser SDK/runner
        # clients; in local mode a present Origin must be loopback).
        dependencies=[
            Depends(require_json_or_multipart_content_type),
            Depends(require_trusted_origin),
        ],
    )
    async def create_session(
        request: Request,
    ) -> SessionResponse | CreatedSessionResponse:
        """
        Create a session.

        ``application/json`` preserves the existing contract: bind to
        an already-registered agent by ``agent_id`` and return the full
        session snapshot. ``multipart/form-data`` is the Alpha
        runner-state create path: the request carries a JSON
        ``metadata`` part and a ``bundle`` file part, then the server
        stores the bundle and creates the conversation row plus
        session-scoped agent row in one database transaction.

        :param request: FastAPI request containing either JSON or
            multipart form data.
        :returns: :class:`SessionResponse` for JSON create, or
            :class:`CreatedSessionResponse` for bundled create.
        :raises OmnigentError: If metadata, bundle, or agent lookup
            validation fails, artifact storage is unavailable, or
            database creation fails.
        """
        user_id = _require_user(request, auth_provider)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type == "multipart/form-data":
            result = await _create_bundled_session_from_multipart(request, user_id)
            if permission_store is not None and user_id is not None:
                await asyncio.to_thread(permission_store.ensure_user, user_id)
                await asyncio.to_thread(
                    permission_store.grant, user_id, result.session_id, LEVEL_OWNER
                )
            # Push the new session to this user's other open tabs so it
            # enters the sidebar without a list poll (WS /sessions/updates).
            _announce_session_added(user_id, result.session_id)
            return result

        try:
            payload = await request.json()
            body = SessionCreateRequest.model_validate(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "type": "json_invalid",
                        "loc": ["body"],
                        "msg": "Invalid JSON",
                        "input": None,
                    },
                ],
            ) from exc
        except ValidationError as exc:
            # include_context=False: pydantic v2 puts the RAW exception
            # object in ctx for validator-raised ValueErrors, which
            # JSONResponse cannot serialize — every model_validator 422
            # on this route 500'd as internal_error. The human-readable
            # message survives in each entry's `msg`.
            raise HTTPException(status_code=422, detail=exc.errors(include_context=False)) from exc

        resp = await _create_session_from_existing_agent(
            conversation_store,
            agent_store,
            runner_router,
            body,
            request,
            agent_cache=agent_cache,
            user_id=user_id,
            permission_store=permission_store,
            liveness_lookup=liveness_lookup,
            file_store=file_store,
            artifact_store=artifact_store,
            background_title_coordinator=background_title_coordinator,
        )
        # Notify the runner about the new session so it can resolve
        # the spec and cache sub_agent_name before the first turn.
        # Without this, the runner doesn't know this session exists
        # until the first forwarded event.
        conv = conversation_store.get_conversation(resp.id)
        # Mark the terminal spin-up flag at creation — the earliest
        # possible point — for a host-launched terminal-first session
        # (claude-native / codex-native). The runner's own pending emit
        # arrives much later (after host launch, runner boot, spec
        # resolve, and harness spawn — each a round-trip), so the spinner
        # would otherwise only flash for the sub-second window before the
        # already-spawned terminal resolves. Gated on host_id because the
        # runner only auto-creates (and thus only clears) a terminal for
        # host-launched sessions; a CLI-bound terminal-first session
        # manages its own terminal and would strand the flag. Clears come
        # from the runner's finally, the relay's resource.created
        # self-heal, or the host-launch-failure path below.
        _terminal_first_create = (
            conv is not None
            and body.host_id is not None
            and conv.labels.get(_CLAUDE_NATIVE_UI_LABEL_KEY) == _CLAUDE_NATIVE_UI_LABEL_VALUE
        )
        if _terminal_first_create:
            _publish_terminal_pending(resp.id, True)
        _rc = await _get_runner_client(resp.id, runner_router)
        if _rc is not None and conv is not None:
            try:
                await _rc.post(
                    "/v1/sessions",
                    json={
                        "session_id": resp.id,
                        "agent_id": conv.agent_id,
                        "sub_agent_name": conv.sub_agent_name,
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError):
                _logger.warning(
                    "Failed to notify runner about session %s",
                    resp.id,
                    exc_info=True,
                )
        # Grant the creator ownership BEFORE any host launch so the
        # launch's session-ownership check (shared with
        # POST /v1/hosts/{host_id}/runners via resolve_host_launch)
        # sees the grant.
        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.ensure_user, user_id)
            await asyncio.to_thread(permission_store.grant, user_id, resp.id, LEVEL_OWNER)
            resp.permission_level = await _get_permission_level(user_id, resp.id, permission_store)
        # Push the new session to this user's other open tabs (see the
        # multipart path above for the rationale).
        _announce_session_added(user_id, resp.id)

        # Managed host: schedule a BACKGROUND sandbox provision bound
        # to this session and return immediately — provisioning takes
        # tens of seconds and must not block the create POST. The
        # background task binds host + workspace to the session row
        # and launches the runner once the sandbox host registers; a
        # message POST racing the provision rendezvouses on the
        # tracker entry registered here (see post_event). Config
        # problems and malformed repo workspaces still fail the POST
        # synchronously.
        launch_host_id = body.host_id
        if body.host_type == "managed" and resp.runner_id is None:
            sandbox_config = getattr(request.app.state, "sandbox_config", None)
            host_store_for_managed = getattr(request.app.state, "host_store", None)
            managed_launches = getattr(request.app.state, "managed_launches", None)
            if (
                sandbox_config is None
                or host_store_for_managed is None
                or managed_launches is None
            ):
                raise OmnigentError(
                    "managed hosts are not configured on this server — add a "
                    "'sandbox:' section to the server config",
                    code=ErrorCode.INVALID_INPUT,
                )
            from omnigent.server.auth import RESERVED_USER_LOCAL
            from omnigent.server.managed_hosts import (
                MANAGED_REPO_LABEL_KEY,
                parse_repo_workspace,
            )

            # A managed workspace is a repository URL (schema-
            # validated) the launch clones inside the sandbox; parse
            # it now so a malformed URL is a synchronous 4xx, not a
            # background failure.
            repo = parse_repo_workspace(body.workspace) if body.workspace is not None else None
            if body.workspace is not None:
                # The session row's workspace is overwritten with the
                # CLONED path at bind time; record the raw request
                # value so a sandbox relaunch can re-clone the same
                # repository into the new generation.
                await asyncio.to_thread(
                    conversation_store.set_labels,
                    resp.id,
                    {MANAGED_REPO_LABEL_KEY: body.workspace},
                )
            managed_launches.begin(resp.id)
            # Seed the launch-progress indicator before the background
            # task starts, so the first GET snapshot (the Web UI
            # navigates to the session page immediately after this
            # 201) already carries the "provisioning" stage.
            _publish_sandbox_status(resp.id, "provisioning")
            launch_task = asyncio.create_task(
                _run_managed_launch(
                    session_id=resp.id,
                    # On auth-disabled servers user_id is None; the
                    # sandbox host registers under the reserved local
                    # owner, same as a directly-connected host would.
                    owner=user_id if user_id is not None else RESERVED_USER_LOCAL,
                    sandbox_config=sandbox_config,
                    repo=repo,
                    tracker=managed_launches,
                    conversation_store=conversation_store,
                    host_store=host_store_for_managed,
                    host_registry=getattr(request.app.state, "host_registry", None),
                    tunnel_registry=getattr(request.app.state, "tunnel_registry", None),
                )
            )
            _managed_launch_tasks.add(launch_task)
            launch_task.add_done_callback(_managed_launch_tasks.discard)

        # Host launch: if a host is targeted (caller-supplied or
        # managed) and no runner is bound yet, authorize (caller must
        # own the host AND the session), atomically bind, then launch.
        # Same authorization path as POST /v1/hosts/{host_id}/runners.
        if launch_host_id is not None and resp.runner_id is None:
            host_registry = getattr(request.app.state, "host_registry", None)
            host_store_inst = getattr(request.app.state, "host_store", None)
            if host_registry is not None and host_store_inst is not None:
                from omnigent.host.frames import (
                    HostLaunchRunnerFrame,
                    encode_host_frame,
                )
                from omnigent.runner.identity import token_bound_runner_id
                from omnigent.server.routes._host_launch import resolve_host_launch

                target = await asyncio.to_thread(
                    resolve_host_launch,
                    user_id=user_id,
                    host_id=launch_host_id,
                    session_id=resp.id,
                    host_store=host_store_inst,
                    host_registry=host_registry,
                    conversation_store=conversation_store,
                    permission_store=permission_store,
                )
                conn = target.conn
                binding_token = secrets.token_urlsafe(32)
                runner_id = token_bound_runner_id(binding_token)
                # Atomic bind (WHERE runner_id IS NULL) closes the TOCTOU.
                bound = await asyncio.to_thread(
                    conversation_store.set_runner_id,
                    resp.id,
                    runner_id,
                )
                if not bound:
                    raise OmnigentError(
                        f"Session {resp.id!r} already has a runner bound",
                        code=ErrorCode.CONFLICT,
                    )
                # host_id and workspace were already written by
                # _create_session_from_existing_agent; we only need
                # to set runner_id atomically (above) and send the
                # launch frame.
                request_id = secrets.token_hex(8)
                future: asyncio.Future[dict[str, str | None]] = (
                    asyncio.get_running_loop().create_future()
                )
                conn.pending_launches[request_id] = future
                if resp.workspace is None:  # pragma: no cover — schema guards
                    raise OmnigentError(
                        "session has host_id but no workspace; "
                        "schema constraint should have prevented this",
                        code=ErrorCode.INTERNAL_ERROR,
                    )
                launch_frame = encode_host_frame(
                    HostLaunchRunnerFrame(
                        request_id=request_id,
                        binding_token=binding_token,
                        workspace=resp.workspace,
                        session_id=resp.id,
                        # Already canonical (see _resolve_harness); lets
                        # the host refuse an unconfigured harness before
                        # spawning. None (agent not resolvable) skips the
                        # host-side check.
                        harness=resp.harness,
                    )
                )
                host_registry.send_text(conn, launch_frame)
                try:
                    result = await asyncio.wait_for(future, timeout=30.0)
                except asyncio.TimeoutError:
                    conn.pending_launches.pop(request_id, None)
                    result = {"status": "failed", "error": "host launch timed out"}
                if result.get("status") == "failed":
                    # Lenient on every create-time launch failure, including
                    # an unconfigured harness: the picker's readiness data
                    # can be stale (the user may have run `omnigent setup`
                    # since the host last connected), so we never block the
                    # create. The session opens with the binding intact; the
                    # first message drives the real runner start, and if the
                    # host still refuses there, that path consults the daemon
                    # and persists a transcript error (see post_event's
                    # relaunch branch). No create-time harness gating.
                    _logger.warning(
                        "Host %s failed to launch runner for session %s: %s",
                        launch_host_id,
                        resp.id,
                        result.get("error"),
                    )
                    # The runner never booted, so its pending=False clear
                    # will never fire. Clear the spin-up flag here so a
                    # failed launch doesn't strand the Terminal-pill
                    # spinner. No-op when we never set it.
                    if _terminal_first_create:
                        _publish_terminal_pending(resp.id, False)
                resp.runner_id = runner_id
                resp.host_id = launch_host_id

        return resp

    async def _create_bundled_session_from_multipart(
        request: Request,
        user_id: str | None,
    ) -> CreatedSessionResponse:
        """
        Handle multipart ``POST /v1/sessions`` with inline agent upload.

        :param request: FastAPI request containing ``metadata`` and
            ``bundle`` form parts.
        :param user_id: Authenticated caller, e.g.
            ``"alice@example.com"``. Used to authorize
            ``metadata.parent_session_id`` and enforce
            runner ownership on parent inheritance.
        :returns: :class:`CreatedSessionResponse` with the new
            session id.
        :raises HTTPException: 422 when a required multipart part is
            absent.
        :raises OmnigentError: If metadata or bundle validation
            fails, or ``parent_session_id`` fails authorization.
        """
        if artifact_store is None:
            raise OmnigentError(
                "artifact store is not configured",
                code=ErrorCode.INTERNAL_ERROR,
            )
        form = await request.form()
        metadata = form.get("metadata")
        bundle = form.get("bundle")
        missing = [
            _multipart_missing_detail(field)
            for field, value in (("metadata", metadata), ("bundle", bundle))
            if value is None
        ]
        if missing:
            raise HTTPException(status_code=422, detail=missing)
        if not isinstance(metadata, str):
            raise HTTPException(status_code=422, detail=[_multipart_missing_detail("metadata")])
        if not isinstance(bundle, StarletteUploadFile):
            raise HTTPException(status_code=422, detail=[_multipart_missing_detail("bundle")])
        parsed_metadata = _parse_session_create_metadata(metadata)
        _reject_reserved_cost_control_label_seed(parsed_metadata.labels)
        _reject_server_reserved_label_seed(parsed_metadata.labels)

        inherited_runner_id: str | None = None
        if parsed_metadata.parent_session_id is not None:
            inherited_runner_id = await _authorize_bundled_parent_and_inherit_runner(
                parsed_metadata.parent_session_id,
                user_id=user_id,
                permission_store=permission_store,
                conversation_store=conversation_store,
                runner_router=runner_router,
            )

        bundle_bytes = await bundle.read()
        result = await asyncio.to_thread(
            _create_session_from_bundle,
            conversation_store,
            artifact_store,
            parsed_metadata,
            bundle_bytes,
            inherited_runner_id,
        )
        # Top-level creates (no inherited runner) skip the notify —
        # their runner registers itself later.
        if inherited_runner_id is not None:
            await _notify_runner_of_bundled_child(
                result.session_id,
                result.agent_id,
                runner_router,
            )
        return result

    # ── GET /sessions/projects ────────────────────────────────────
    #
    # MUST be registered before ``GET /sessions/{session_id}``: FastAPI
    # matches routes in registration order, so a literal ``/sessions/projects``
    # would otherwise be captured by the ``{session_id}`` path param and 404
    # as a missing conversation.

    @router.get("/sessions/projects")
    async def list_session_projects(
        request: Request,
    ) -> list[SessionProjectSummary]:
        """
        Return the caller's projects as ``{"id", "name"}`` pairs, ordered
        alphabetically by name.

        Dual-reads both project representations and unions them by name:
        - **First-class projects** (``project_store``) — carry an ``id`` and
          appear even when empty (the whole point of the first-class entity).
        - **Legacy label-projects** (implicit ``omni_project`` label) — exist
          while at least one owned session carries the label; ``id`` is
          ``None`` until such a project is promoted to first-class.

        A name present in both sources collapses to one entry that keeps the
        first-class ``id``. Filing is owner-only, so both halves are scoped to
        the caller (label-projects to their owned sessions, first-class to
        their owned rows) — a project shared to them but owned by another user
        does not surface as one of their own folders.

        :returns: List of :class:`SessionProjectSummary` ordered by name.
        """
        user_id = _require_user(request, auth_provider)

        def _list_union() -> list[SessionProjectSummary]:
            # First-class first so its id wins when a name exists in both.
            by_name: dict[str, SessionProjectSummary] = {}
            if project_store is not None:
                for proj in project_store.list(owner_user_id=user_id):
                    by_name[proj.name] = SessionProjectSummary(id=proj.id, name=proj.name)
            # Legacy path: label-derived projects (id=None unless already first-class).
            for name in conversation_store.list_projects(owned_by=user_id):
                by_name.setdefault(name, SessionProjectSummary(id=None, name=name))
            return [by_name[name] for name in sorted(by_name)]

        return await asyncio.to_thread(_list_union)

    # ── PUT /sessions/{session_id}/read-state ─────────────────────
    #
    # The per-user read-state *write* path. The *read* path is the
    # per-viewer ``viewer_last_seen`` / ``viewer_unread`` fields embedded in
    # the ``GET /v1/sessions`` list items — no separate read endpoint.

    @router.put(
        "/sessions/{session_id}/read-state",
        status_code=204,
    )
    async def put_read_state(
        request: Request,
        session_id: str,
        body: ReadStatePutRequest,
    ) -> Response:
        """
        Set the calling user's read-state for one session.

        Requires ``LEVEL_READ`` on the session in multi-user mode — you can
        only track read-state for sessions you can see. Stores the values
        verbatim (the client enforces the baseline's monotonicity and the
        unread semantics); the server does not interpret them against
        session status. Returns ``204`` — the client already has the
        optimistic state and re-reads the authoritative value on the next
        ``GET /v1/sessions`` poll.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param body: The validated :class:`ReadStatePutRequest`.
        :returns: An empty ``204 No Content`` response.
        :raises OmnigentError: 403 if the caller lacks read access.
        """
        user_id = _require_user(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        _set_read_state(user_id, session_id, body.last_seen, body.unread)
        return Response(status_code=204)

    # ── GET /sessions/{session_id} ───────────────────────────────

    @router.get(
        "/sessions/{session_id}",
        # See create_session for the response_model=None rationale. We keep
        # response_model=None (no response re-validation/serialization) but
        # still advertise the body schema for docs/SDK tooling via responses=.
        response_model=None,
        responses={200: {"model": SessionResponse}},
    )
    async def get_session(
        request: Request,
        response: Response,
        session_id: str,
        include_items: bool = Query(default=True),
        include_liveness: bool = Query(default=True),
        refresh_state: bool = Query(default=False),
    ) -> SessionResponse:
        """
        Return a session snapshot: identity, status, and committed
        items.

        :param request: The incoming FastAPI request (for auth).
        :param response: The FastAPI response (for cache headers).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param include_items: When ``False``, skip the committed-items
            read and return ``items=[]``. The web chat surface passes
            ``False`` because it hydrates the transcript via the
            paginated ``GET /sessions/{id}/items`` endpoint in parallel
            and never reads the snapshot's copy; the items read is the
            single most expensive step of the snapshot build.
        :param include_liveness: When ``False``, skip the runner/host
            liveness lookup and return ``runner_online``/``host_online``
            as ``None``. The web chat surface passes ``False`` because
            it sources liveness from the ``/health`` poll and the WS
            stream, not the snapshot.
        :param refresh_state: When ``True``, refresh runner-derived
            snapshot overlays from the live session instead of serving
            stale AP-process caches. Browser reload/bind requests use
            this to recover from fixed bugs without restarting the AP
            server.
        :returns: The matching :class:`SessionResponse`.
        :raises OmnigentError: 404 if no session exists.
        """
        response.headers["Cache-Control"] = "no-store"
        user_id = _get_user_id(request, auth_provider)
        # Single permission pass: authorize + resolve the display level +
        # fetch the conversation once, then reuse the conversation in the
        # snapshot (the snapshot's read is skipped). Replaces the former
        # require_access + get_permission_level + snapshot-get_conversation
        # sequence, which made ~5-6 separate store round-trips.
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        return await _get_session_snapshot(
            conversation_store,
            session_id,
            access.level,
            agent_store,
            agent_cache,
            conversation=access.conversation,
            liveness_lookup=liveness_lookup if include_liveness else None,
            include_items=include_items,
            runner_exit_reports=runner_exit_reports,
            refresh_state=refresh_state,
            host_store=getattr(request.app.state, "host_store", None),
            sandbox_config=getattr(request.app.state, "sandbox_config", None),
        )

    @router.get(
        "/sessions/{session_id}/labels",
        response_model=SessionLabelsResponse,
    )
    async def get_session_labels(
        request: Request,
        response: Response,
        session_id: str,
    ) -> SessionLabelsResponse:
        """
        Return only the labels for a session.

        Native runner bridge setup needs labels during harness spawn,
        but the full session snapshot also loads history, skills,
        runner status, and agent metadata. This endpoint keeps that
        spawn-time dependency to one authorized conversation read.

        :param request: The incoming FastAPI request (for auth).
        :param response: The FastAPI response (for cache headers).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The session id and labels.
        :raises OmnigentError: 404 if no session exists.
        """
        response.headers["Cache-Control"] = "no-store"
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        return SessionLabelsResponse(
            id=conv.id,
            labels=labels_with_closed_status(conv.labels, conv.title),
        )

    # ── GET /sessions ───────────────────────────────────────────

    @router.get(
        "/sessions",
        response_model=None,
        responses={200: {"model": SessionList}},
    )
    async def list_sessions(
        request: Request,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        agent_name: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at)$"),
        search_query: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
        kind: str = Query(default="default", pattern="^(default|sub_agent|any)$"),
        project: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sessions with cursor-based pagination.

        Sessions are conversations with a non-``None`` ``agent_id``
        — i.e. those created via ``POST /v1/sessions``.
        Conversations without an agent binding are excluded.

        :param limit: Maximum number of sessions to return
            (1-1000, default 20).
        :param after: Cursor — return sessions after this
            session ID in sort order, e.g. ``"conv_abc123"``.
        :param before: Cursor — return sessions before this
            session ID.
        :param agent_id: When set, only return sessions bound
            to this agent, e.g. ``"ag_abc123"``. ``None``
            returns sessions across all agents.
        :param agent_name: When set, only return sessions whose
            bound agent row has this name. This intentionally
            includes session-scoped agents that share a name but
            have distinct bundles. ``None`` disables the filter.
        :param order: Sort direction, ``"desc"`` (newest-first)
            or ``"asc"`` (oldest-first).
        :param sort_by: Column to sort on, ``"created_at"`` or
            ``"updated_at"``.
        :param search_query: Case-insensitive substring filter on
            the session title or conversation content. ``None``
            or empty string disables the filter. A session
            matches if its title contains the query or any of
            its conversation items' text does. Powers the
            sidebar's session search.
        :param include_archived: When ``False`` (default), archived
            sessions are omitted. When ``True``, archived sessions
            are returned alongside active ones (the sidebar groups
            them into an "Archived" section). Powers the sidebar's
            "Show archived" toggle.
        :param kind: Conversation kind to return. ``"default"``
            (the default) returns only top-level user-initiated
            sessions — the sidebar's view. ``"sub_agent"`` returns
            only sub-agent child sessions. ``"any"`` returns both;
            this lets the new-session agent picker discover agents
            that are only bound to sub-agent sessions (e.g. ones
            uploaded via ``sys_session_create``).
        :returns: A :class:`PaginatedList` of
            :class:`SessionListItem`.
        """
        # Empty-string normalization — the UI sends
        # ``?search_query=`` when the search box is cleared and
        # that should behave identically to the param being
        # absent. Keeping the store's contract crisp: ``None``
        # means "no filter", anything else means "search".
        #
        # require_user, not get_user_id: ``accessible_by=None`` below
        # means "no ACL filter", so an unauthenticated request slipping
        # through as None would list EVERY user's sessions. Fail closed
        # with 401 instead (user_id stays None only when auth is
        # disabled entirely — no auth_provider).
        user_id = _require_user(request, auth_provider)
        normalized_query = search_query if search_query else None
        # A specific project folder ("My sessions"-only) must show only the
        # viewer's own sessions — a session shared with them but filed under a
        # like-named project belongs on "Shared with me", not in this folder.
        # Passing owned_by here also scopes the dual-read's first-class half:
        # the store resolves the project NAME to the caller's own project id.
        # The flat list (project=None) and Unfiled (project="") stay unscoped so
        # shared sessions still surface for the "Shared with me" tab.
        owned_by = user_id if project else None
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            agent_id=agent_id,
            agent_name=agent_name,
            accessible_by=user_id,
            owned_by=owned_by,
            has_agent_id=True,
            # The store treats ``None`` as "no kind filter"; the API
            # spells that ``kind=any`` to keep the param required-ish
            # and pattern-validated.
            kind=None if kind == "any" else kind,
            order=order,
            sort_by=sort_by,
            search_query=normalized_query,
            include_archived=include_archived,
            project=project,
        )
        # list_conversations may return rows with agent_id=None for
        # legacy conversations; skip them before building the batch IDs.
        conv_ids = [conv.id for conv in page.data if conv.agent_id is not None]
        if not conv_ids:
            return PaginatedList(
                data=[],
                first_id=page.first_id,
                last_id=page.last_id,
                has_more=page.has_more,
            )
        # Batch-fetch permissions and agent names concurrently.
        # The tasks table has been removed — status comes exclusively from
        # the relay-fed ``_session_status_cache``.
        unique_agent_ids = list({c.agent_id for c in page.data if c.agent_id is not None})
        if permission_store is not None:
            perms_by_conv, agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                asyncio.to_thread(permission_store.list_for_sessions, conv_ids),
                asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                asyncio.to_thread(
                    conversation_store.list_child_conversation_ids_by_parent,
                    conv_ids,
                ),
            )
            user_is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
        else:
            agent_names_by_id, child_ids_by_parent = await asyncio.gather(
                asyncio.to_thread(agent_store.get_names, unique_agent_ids),
                asyncio.to_thread(
                    conversation_store.list_child_conversation_ids_by_parent,
                    conv_ids,
                ),
            )
            perms_by_conv: dict[str, list[SessionPermission]] = {}
            user_is_admin = False
        # In-memory lookup — no I/O, so batching avoids re-acquiring
        # the index's lock per row but otherwise has no DB cost.
        pending_counts = pending_elicitations.counts_for(conv_ids)
        comments_fingerprints = await _comments_fingerprints_for(conv_ids)
        items: list[SessionListItem] = [
            _build_session_list_item(
                conv,
                agent_names_by_id=agent_names_by_id,
                grants=perms_by_conv.get(conv.id, []),
                user_id=user_id,
                user_is_admin=user_is_admin,
                permissions_enabled=permission_store is not None,
                pending_count=pending_counts.get(conv.id, 0),
                child_session_ids=child_ids_by_parent[conv.id],
                comments_fingerprint=comments_fingerprints.get(conv.id),
            )
            for conv in page.data
            if conv.agent_id is not None
        ]
        # The list deliberately does NOT compute per-item liveness
        # (runner_online / host_online). No list consumer reads it: the
        # sidebar no longer surfaces connection state, and the only live
        # consumer — the open-session view — sources liveness from the
        # single-session snapshot, the WS stream, and the /health poll, not
        # from list rows. Skipping it here removes the session-connectivity
        # and hosts-table queries from every GET /v1/sessions.
        return PaginatedList(
            data=[item.model_dump(exclude_none=True) for item in items],
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    async def _comments_fingerprints_for(
        conv_ids: list[str],
    ) -> dict[str, CommentsFingerprint]:
        """
        Batch-fetch comment change fingerprints for the given sessions.

        Shared by the ``GET /v1/sessions`` page builder and
        ``WS /v1/sessions/updates`` so both emit the same
        ``comments_count`` / ``comments_updated_at`` values and the
        stream's diff fires when a comment is added, edited, addressed,
        or deleted.

        :param conv_ids: Session ids to summarize,
            e.g. ``["conv_abc123"]``.
        :returns: Map from session id to its
            :class:`CommentsFingerprint`; empty when no comment store
            is wired. Sessions without comments are absent.
        """
        if comment_store is None or not conv_ids:
            return {}
        return await asyncio.to_thread(comment_store.get_comments_fingerprints, conv_ids)

    # ── WS /sessions/updates ────────────────────────────────────

    async def _fetch_watched_items(
        watched: list[str],
        user_id: str | None,
    ) -> list[dict[str, Any]]:
        """
        Build current list-item payloads for the watched ids.

        Reads exactly the same sources as ``GET /v1/sessions`` (the
        relay-fed status cache plus the conversation store) and enforces
        per-session read access: ids the user cannot access, that don't
        exist, or that aren't sessions (no ``agent_id``) are silently
        omitted. This is the pull the session-updates stream diffs each
        interval — it is a drop-in for the client's former list poll, not
        a new event source, so it carries no new cross-replica semantics.

        When ``liveness_lookup`` is wired, each payload also carries
        ``runner_online`` and ``host_online`` (the same values
        ``GET /health`` and ``GET /v1/sessions`` return), so the client
        can drop its per-session ``/health`` poll for watched sessions.

        :param watched: Conversation ids the client is currently
            displaying, e.g. ``["conv_abc", "conv_def"]``. Already
            deduplicated and length-capped by the caller.
        :param user_id: The authenticated requesting user, or ``None``
            when permissions are disabled, e.g. ``"alice@example.com"``.
        :returns: One JSON-ready dict per accessible, existing watched
            session, in no particular order.
        """
        if not watched:
            return []
        if permission_store is not None:
            perms_by_conv = await asyncio.to_thread(permission_store.list_for_sessions, watched)
            user_is_admin = (
                await asyncio.to_thread(permission_store.is_admin, user_id)
                if user_id is not None
                else False
            )
            accessible = [
                cid
                for cid in watched
                if _permission_level_from_grants(
                    user_id, perms_by_conv.get(cid, []), user_is_admin
                )
                is not None
            ]
        else:
            perms_by_conv = {}
            user_is_admin = False
            accessible = list(watched)
        if not accessible:
            return []

        def _load_sessions(ids: list[str]) -> list[Conversation]:
            """Bulk-load the accessible conversations that are sessions
            (non-null ``agent_id``) in one batched store call, preserving
            the caller's id order for deterministic output."""
            by_id = conversation_store.get_conversations(ids)
            return [
                conv
                for cid in ids
                if (conv := by_id.get(cid)) is not None and conv.agent_id is not None
            ]

        convs = await asyncio.to_thread(_load_sessions, accessible)
        if not convs:
            return []
        unique_agent_ids = list({c.agent_id for c in convs if c.agent_id is not None})
        conv_ids = [c.id for c in convs]
        agent_names_by_id, child_ids_by_parent, comments_fingerprints = await asyncio.gather(
            asyncio.to_thread(agent_store.get_names, unique_agent_ids),
            asyncio.to_thread(
                conversation_store.list_child_conversation_ids_by_parent,
                conv_ids,
            ),
            _comments_fingerprints_for(conv_ids),
        )
        pending_counts = pending_elicitations.counts_for(conv_ids)
        items = [
            _build_session_list_item(
                conv,
                agent_names_by_id=agent_names_by_id,
                grants=perms_by_conv.get(conv.id, []),
                user_id=user_id,
                user_is_admin=user_is_admin,
                permissions_enabled=permission_store is not None,
                pending_count=pending_counts.get(conv.id, 0),
                child_session_ids=child_ids_by_parent[conv.id],
                comments_fingerprint=comments_fingerprints.get(conv.id),
            )
            for conv in convs
        ]
        await _apply_liveness_to_items(items, liveness_lookup)
        # Full-row dumps (every field, nulls included) — NOT exclude_none. The
        # stream is a diff source: the client overlays these onto its cached
        # rows, so a field that cleared to null must arrive as an explicit null
        # (an absent key would leave the stale value in the cache). The client
        # converts null → undefined on apply, so a cleared field lands in the
        # same shape GET /v1/sessions produces (absent), and the
        # ``permission_level === null`` full-access sentinel in the web sidebar
        # is never tripped by a streamed null. The GET list endpoint keeps
        # exclude_none — it replaces whole pages, so it has nothing to clear.
        #
        # search_snippet is excluded: it is search-only (populated just by
        # GET /v1/sessions?search_query=), so this no-query path always has it
        # None. Dumping it as an explicit null would overwrite a snippet the
        # search response put in the client cache, making the palette's match
        # preview flicker away on the next stream tick. Omitting the key leaves
        # the cached snippet untouched.
        return [item.model_dump(exclude={"search_snippet"}) for item in items]

    @router.websocket("/sessions/updates")
    async def session_updates(websocket: WebSocket) -> None:
        """
        Push session-list changes for a client-supplied watch-set.

        Replaces the web app's 4 s HTTP poll of ``GET /v1/sessions``
        with one persistent connection. Protocol (JSON text frames):

        - **client → server**:
          ``{"type": "watch", "session_ids": [...]}`` — the ids the
          client is currently displaying. Sent on connect and re-sent
          whenever the visible set changes (scroll / filter /
          pagination); it fully replaces the prior watch-set. Unknown
          message shapes are ignored for forward compatibility.
        - **server → client**:
          ``{"type": "snapshot", "items": [SessionListItem, ...]}`` once
          per ``watch`` (full state for the new set), then
          ``{"type": "changed", "items": [...]}`` /
          ``{"type": "removed", "ids": [...]}`` deltas as watched
          sessions change, and ``{"type": "heartbeat"}`` when idle.

        Watched-row freshness is pull-based — each interval the server
        re-reads the watched ids (the same read ``GET /v1/sessions`` does)
        and emits only what changed. *Discovery* of sessions the client
        isn't watching yet (created / forked / shared elsewhere) is instead
        push-based: a ``session_added`` event on this user's
        :mod:`user_session_stream` channel makes the server push the new
        session as a ``changed`` frame, which the client reconciles into the
        sidebar. Together these mean an idle list makes zero HTTP polls yet a
        new session still appears within a tick of being created.

        :param websocket: The incoming FastAPI :class:`WebSocket`.
        """
        user_id = auth_provider.get_user_id(websocket) if auth_provider is not None else None
        # When permissions are enabled, an unauthenticated socket can see
        # nothing useful and must not be allowed to probe ids; reject the
        # handshake (mirrors the terminal-attach authorization gate).
        if permission_store is not None and user_id is None:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="authentication required",
            )
        await websocket.accept()

        watched: list[str] = []
        # Last SessionListItem dump sent per id, used to diff. Keyed only
        # by currently-watched ids; pruned when the watch-set narrows.
        last_sent: dict[str, dict[str, Any]] = {}
        last_send_monotonic = time.monotonic()
        # Serializes the read-diff-send-update critical section between the
        # reader (snapshot on watch) and the ticker (interval deltas) so
        # they never interleave updates to ``last_sent``.
        emit_lock = asyncio.Lock()

        async def _send(frame: dict[str, Any]) -> None:
            """
            Serialize and send one frame, stamping the last-send time so
            the heartbeat timer measures idleness from the last real send.

            :param frame: The outgoing frame, e.g.
                ``{"type": "changed", "items": [...]}``. Sent as JSON text.
            """
            nonlocal last_send_monotonic
            # Stamp the active trace context into the frame so a client
            # with browser-side propagation can correlate sidebar updates
            # to the trace that produced them. No-op when no span is
            # active (idle heartbeats/snapshots), keeping the frame
            # wire-identical in the common case.
            from omnigent.runtime import telemetry

            telemetry.record_message_payload(frame)
            telemetry.inject_trace_context(frame)
            await websocket.send_text(json.dumps(frame))
            last_send_monotonic = time.monotonic()

        async def _emit_snapshot() -> None:
            """Send a full snapshot for the current watch-set and reset the
            diff baseline to it."""
            items = await _fetch_watched_items(watched, user_id)
            dumps = {item["id"]: item for item in items}
            last_sent.clear()
            last_sent.update(dumps)
            await _send({"type": "snapshot", "items": list(dumps.values())})

        async def _emit_deltas() -> None:
            """Diff the watched ids against the last frame and send only the
            changes; emit a heartbeat when nothing changed but the link has
            been idle."""
            nonlocal last_send_monotonic
            if watched:
                items = await _fetch_watched_items(watched, user_id)
                current = {item["id"]: item for item in items}
                changed = [dump for cid, dump in current.items() if last_sent.get(cid) != dump]
                # Removed = a still-watched id that no longer resolves (lost
                # access or deleted). De-watched ids are pruned silently
                # below, not reported as removed.
                removed = [cid for cid in watched if cid not in current and cid in last_sent]
                last_sent.clear()
                last_sent.update(current)
                if changed:
                    await _send({"type": "changed", "items": changed})
                if removed:
                    await _send({"type": "removed", "ids": removed})
            if time.monotonic() - last_send_monotonic >= _SESSION_UPDATES_HEARTBEAT_INTERVAL_S:
                await _send({"type": "heartbeat"})

        async def _reader() -> None:
            """Apply incoming watch-set updates and snapshot each one."""
            nonlocal watched
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "watch":
                    # Forward-compatible: ignore frames we don't understand.
                    continue
                ids = msg.get("session_ids")
                if not isinstance(ids, list):
                    continue
                # Dedupe preserving order, keep only strings. Dedupe fully
                # first, then cap — so the truncation count below is the real
                # number of distinct ids dropped, not skewed by duplicates that
                # happen to sit past the cap.
                deduped: list[str] = []
                unique: set[str] = set()
                for cid in ids:
                    if isinstance(cid, str) and cid not in unique:
                        unique.add(cid)
                        deduped.append(cid)
                if len(deduped) > _SESSION_UPDATES_MAX_WATCHED:
                    # Ids past the cap get no push updates and are never reported
                    # "removed" (they aren't watched). The client's low-rate list
                    # reconciliation still covers them, but log the silent drop so
                    # an oversized watch-set is diagnosable rather than invisible.
                    _logger.warning(
                        "session-updates watch-set truncated to %d of %d distinct ids "
                        "for user %r; ids beyond the cap rely on list-poll reconciliation",
                        _SESSION_UPDATES_MAX_WATCHED,
                        len(deduped),
                        user_id,
                    )
                    deduped = deduped[:_SESSION_UPDATES_MAX_WATCHED]
                # The watched set after capping — used to prune baselines for ids
                # the client no longer watches (including any just truncated).
                watched_set = set(deduped)
                # Handle the watch under a span parented on any trace
                # context the browser stamped into the frame, so the
                # snapshot read (and its DB spans) nest under the
                # client-originated trace.
                from omnigent.runtime import telemetry

                with telemetry.consume_frame_span("session_updates.watch", msg):
                    async with emit_lock:
                        watched = deduped
                        # Drop baselines for ids no longer watched so they
                        # can't surface as spurious "removed" later.
                        for stale in [cid for cid in last_sent if cid not in watched_set]:
                            del last_sent[stale]
                        await _emit_snapshot()

        async def _ticker() -> None:
            """Emit deltas / heartbeats on a fixed interval."""
            while True:
                await asyncio.sleep(_SESSION_UPDATES_RESCAN_INTERVAL_S)
                async with emit_lock:
                    try:
                        await _emit_deltas()
                    except WebSocketDisconnect:
                        # The client went away mid-send — the normal terminal
                        # condition. Propagate so the stream tears down and the
                        # reader/ticker pair is cancelled.
                        raise
                    except Exception:  # noqa: BLE001 — a transient tick failure must not tear down a live stream
                        # A transient store/DB read failure must not kill a live
                        # stream and force every watcher to reconnect +
                        # re-snapshot. Log it and try again next interval; the
                        # diff is recomputed from scratch each tick, so a skipped
                        # tick costs at most one delayed delta. (CancelledError
                        # is not an Exception subclass, so cancellation still
                        # propagates.)
                        _logger.warning(
                            "session-updates delta tick failed; retrying next interval",
                            exc_info=True,
                        )

        async def _discovery() -> None:
            """Push sessions newly made accessible to this user — created,
            forked, or shared from elsewhere — so they enter the sidebar
            without a list poll.

            Such ids are NOT in the client's watch-set (the client doesn't
            know about them yet), so the per-interval diff can't surface them.
            This reacts to the create/grant event instead: it fetches the one
            announced id (access-checked, same as the watch path) and pushes
            it. The client reconciles the unknown id into its cache, then
            re-sends its watch-set including it, after which it is tracked
            like any normal watched row. Idle users with no new sessions
            receive nothing — so the zero-traffic property holds."""
            async for evt in user_session_stream.subscribe(_discovery_key(user_id)):
                if not isinstance(evt, dict):
                    continue
                evt_type = evt.get("type")
                if evt_type == "session_added":
                    sid = evt.get("session_id")
                    if not isinstance(sid, str):
                        continue
                    async with emit_lock:
                        # Already watched ⇒ the normal diff already covers it.
                        if sid in watched:
                            continue
                        try:
                            items = await _fetch_watched_items([sid], user_id)
                            if items:
                                await _send({"type": "changed", "items": items})
                        except WebSocketDisconnect:
                            # Client gone mid-send — propagate to tear the stream down.
                            raise
                        except Exception:  # noqa: BLE001 — a failed discovery push must not kill a live stream
                            # A transient read/send failure for one announcement
                            # must not drop the whole stream; the session is still
                            # discoverable on the client's next list reconcile.
                            _logger.warning(
                                "session-updates discovery push failed for %r; "
                                "falling back to list reconcile",
                                sid,
                                exc_info=True,
                            )
                elif evt_type == "hosts_changed":
                    async with emit_lock:
                        try:
                            await _send({"type": "hosts_changed"})
                        except WebSocketDisconnect:
                            raise
                        except Exception:  # noqa: BLE001
                            _logger.warning(
                                "hosts-changed push failed; client will rely on fallback poll",
                                exc_info=True,
                            )

        reader_task = asyncio.create_task(_reader(), name="session-updates-reader")
        ticker_task = asyncio.create_task(_ticker(), name="session-updates-ticker")
        discovery_task = asyncio.create_task(_discovery(), name="session-updates-discovery")
        try:
            done, pending = await asyncio.wait(
                {reader_task, ticker_task, discovery_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                exc = task.exception()
                # A client disconnect is the normal terminal condition; any
                # other exception is a real bug worth surfacing in logs.
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    _logger.warning("session-updates stream task crashed: %r", exc)
        finally:
            with contextlib.suppress(RuntimeError):
                await websocket.close()

    # ── Codex-native goal controls ───────────────────────────────

    from omnigent.server.routes.codex.sessions import register_codex_session_routes

    register_codex_session_routes(
        router,
        conversation_store=conversation_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        runner_exit_reports=runner_exit_reports,
    )

    # ── PATCH /sessions/{session_id} ────────────────────────────

    @router.post(
        "/sessions/{session_id}/auto-title",
        response_model=AutomaticSessionRenameResponse,
    )
    async def automatically_rename_session(
        request: Request,
        session_id: str,
        body: AutomaticSessionRenameRequest,
    ) -> AutomaticSessionRenameResponse:
        """Replace the deterministic first-message title when still current."""
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        if conv.parent_conversation_id is not None:
            return AutomaticSessionRenameResponse(renamed=False, reason="not_top_level")

        title = " ".join(body.title.split())
        if "\n" in body.title or "\r" in body.title or len(title) < 2:
            raise OmnigentError(
                "title must be a single non-empty line",
                code=ErrorCode.INVALID_INPUT,
            )

        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            100,
            None,
            None,
            "asc",
            None,
        )
        seed_title: str | None = None
        for item in page.data:
            seed_title = synthesize_conversation_title(_title_content_from_item(item))
            if seed_title is not None:
                break
        if seed_title is None:
            return AutomaticSessionRenameResponse(renamed=False, reason="no_seed")
        if conv.title != seed_title:
            return AutomaticSessionRenameResponse(renamed=False, reason="title_changed")
        updated = await asyncio.to_thread(
            conversation_store.rename_conversation_if_title_matches,
            session_id,
            seed_title,
            title,
        )
        if updated is None:
            return AutomaticSessionRenameResponse(renamed=False, reason="title_changed")
        return AutomaticSessionRenameResponse(renamed=True, title=updated.title)

    @router.patch(
        "/sessions/{session_id}",
        response_model=None,
        responses={200: {"model": SessionResponse}},
    )
    async def update_session(
        request: Request,
        session_id: str,
        body: UpdateSessionRequest,
    ) -> SessionResponse:
        """
        Update a session's mutable fields. When ``runner_id`` is
        provided, this is the mutable affinity primitive for the Alpha
        runner-state pivot: create-bind, resume-bind, and recover-bind
        all send the currently registered runner id, and the server
        atomically replaces ``conversations.runner_id`` with that
        value using last-write-wins semantics. Title, labels, and
        reasoning-effort updates remain supported for existing
        sessions clients.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param body: The validated :class:`UpdateSessionRequest`.
        :returns: The updated :class:`SessionResponse` snapshot.
        :raises OmnigentError: 400 if the runner is not
            registered; 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        # Filing into a project is owner-only: projects are owner-private, so a
        # session's membership is the owner organizing their own sessions — an
        # editor must not move it. Presence is the signal (``""`` unfiles), so
        # gate on model_fields_set, not a non-None value.
        set_project = "project_id" in body.model_fields_set
        # Archiving/unarchiving is an owner-only lifecycle action: it pairs
        # with a client-driven, owner-gated stop, so an editor must not be
        # able to archive a session (hiding it, and via the client stopping
        # it) when they couldn't issue that stop. Every other field on this
        # endpoint needs only edit. Owner implies edit, so a single check at
        # the level the request actually requires gates both — no redundant
        # second permission-store read for archive/unarchive.
        required_level = LEVEL_OWNER if (body.archived is not None or set_project) else LEVEL_EDIT
        await _require_access(
            user_id, session_id, required_level, permission_store, conversation_store
        )
        if body.archived is True:
            await _best_effort_stop(session_id, conversation_store, runner_router)
        if body.runner_id is not None and permission_store is not None:
            if not check_session_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            ):
                raise OmnigentError(
                    f"Only the session owner can attach a runner to session {session_id!r}. "
                    f"To fork this session instead, run: omnigent run --fork {session_id}",
                    code=ErrorCode.FORBIDDEN,
                )
        if body.labels:
            _reject_server_reserved_label_seed(body.labels)
            # Advisor-owned cost_control.* labels are written only by the
            # session's bound runner; gate them on runner proof BEFORE any
            # store mutation so a rejected request leaves the session untouched.
            _reserved_labels = reserved_cost_control_keys(body.labels)
            if _reserved_labels:
                _conv_for_reserved = await asyncio.to_thread(
                    conversation_store.get_conversation, session_id
                )
                _require_cost_control_label_authority(
                    reserved_keys=_reserved_labels,
                    tunnel_token=request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER),
                    bound_runner_id=(
                        _conv_for_reserved.runner_id if _conv_for_reserved is not None else None
                    ),
                    allowed_tunnel_tokens=runner_tunnel_tokens,
                    multi_user=permission_store is not None,
                )
        collaboration_mode_requested = "collaboration_mode" in body.model_fields_set
        requested_codex_collaboration_mode: str | None = None
        conv_for_collaboration_mode: Conversation | None = None
        if collaboration_mode_requested:
            if body.collaboration_mode is None:
                raise OmnigentError(
                    "collaboration_mode must be a non-empty string",
                    code=ErrorCode.INVALID_INPUT,
                )
            if body.collaboration_mode not in _CODEX_NATIVE_COLLABORATION_MODES:
                raise OmnigentError(
                    "collaboration_mode must be one of "
                    f"{sorted(_CODEX_NATIVE_COLLABORATION_MODES)}",
                    code=ErrorCode.INVALID_INPUT,
                )
            conv_for_collaboration_mode = await asyncio.to_thread(
                conversation_store.get_conversation,
                session_id,
            )
            if conv_for_collaboration_mode is None:
                raise _session_not_found()
            if (
                conv_for_collaboration_mode.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
                != _CODEX_NATIVE_WRAPPER_LABEL_VALUE
            ):
                raise OmnigentError(
                    "collaboration_mode is only supported for codex-native sessions",
                    code=ErrorCode.INVALID_INPUT,
                )
            requested_codex_collaboration_mode = body.collaboration_mode
        labels_to_set = dict(body.labels or {})
        if requested_codex_collaboration_mode is not None:
            labels_to_set[_CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY] = (
                requested_codex_collaboration_mode
            )
        effort = body.reasoning_effort
        clear_effort = effort in EFFORT_CLEAR_VALUES
        if effort is not None and not clear_effort:
            try:
                effort = validate_effort(
                    effort,
                    "session metadata",
                    EFFORT_VALUES,
                )
            except ValueError as exc:
                raise OmnigentError(
                    f"invalid reasoning_effort: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc

        # Empty / whitespace strings are rejected loud — the only
        # clear path is the explicit ``default | off | reset`` alias.
        model_override = body.model_override
        clear_model = (
            isinstance(model_override, str)
            and model_override.strip().lower() in EFFORT_CLEAR_VALUES
        )
        if model_override is not None and not clear_model:
            # Mirror the create path: the persisted value reaches a native
            # CLI as a ``--model`` argv element and the Codex provider
            # ``config.toml`` as a ``model="..."`` field, so it must pass the
            # conservative model-id charset before it is stored. A bare
            # strip()/non-empty check here let shell-/TOML-shaped values
            # through, enabling host RCE via the Codex ``auth.command``.
            if not isinstance(model_override, str):
                raise OmnigentError(
                    "invalid model_override: must be a non-empty string",
                    code=ErrorCode.INVALID_INPUT,
                )
            try:
                model_override = validate_model_override(model_override)
            except ValueError as exc:
                raise OmnigentError(
                    f"invalid model_override: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc

        # Cost-control switch: ``"off"`` is a real stored value here,
        # so the clear signal is an explicit JSON null (field present,
        # value None) rather than a clear alias; an omitted field
        # leaves the stored value unchanged.
        clear_cost_control = (
            "cost_control_mode_override" in body.model_fields_set
            and body.cost_control_mode_override is None
        )
        cost_control_mode_override = _validated_cost_control_mode_override(
            body.cost_control_mode_override
        )

        # Native-terminal pass-through args: ``None`` leaves them
        # unchanged; a provided list (including ``[]``) replaces the
        # stored value wholesale (resume is last-write-wins, never an
        # append). Bounds are validated here so a malformed list fails
        # loud at the route rather than at the DB.
        try:
            terminal_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        if body.runner_id is not None:
            # Empty string is the clear sentinel (None = leave unchanged);
            # used by /clear and /switch to move the runner between sessions.
            if body.runner_id == "":
                try:
                    await asyncio.to_thread(conversation_store.clear_runner_id, session_id)
                except ConversationNotFoundError as exc:
                    raise _session_not_found() from exc
            else:
                runner_id = _registered_runner_id(runner_router, body.runner_id, user_id=user_id)
                try:
                    await asyncio.to_thread(
                        conversation_store.replace_runner_id, session_id, runner_id
                    )
                except ConversationNotFoundError as exc:
                    raise _session_not_found() from exc
                _runner_client = await _get_runner_client(
                    session_id,
                    runner_router,
                )
                # Notify the runner about the session so it can
                # resolve the spec and cache it before the first turn.
                # This is the design doc's "Server POST /v1/sessions
                # (to runner)" step from §7 Flow: session creation.
                conv = conversation_store.get_conversation(
                    session_id,
                )
                if _runner_client is not None and conv is not None:
                    try:
                        runner_init_resp = await _runner_client.post(
                            "/v1/sessions",
                            json={
                                "session_id": session_id,
                                "agent_id": conv.agent_id,
                                "sub_agent_name": conv.sub_agent_name,
                            },
                            timeout=10.0,
                        )
                        if runner_init_resp.status_code < 400:
                            await _publish_runner_recovered_status(session_id, conversation_store)
                    except (httpx.HTTPError, ConnectionError):
                        # ConnectionError covers a tunnel close mid-POST
                        # (same source as the relay's except clause).
                        _logger.warning(
                            "Failed to notify runner about session %s",
                            session_id,
                            exc_info=True,
                        )
                if _runner_client is None:
                    # Runner deregistered between validation and
                    # lookup; PATCH still returns 200 but no
                    # relay starts, so log the silent-skip case.
                    _logger.warning(
                        "PATCH rebind to %s on session %s: no runner "
                        "client resolved; relay not restarted.",
                        runner_id,
                        session_id,
                    )
                # Restart the relay for the new runner; replaces
                # any relay still pointing at the prior runner.
                await _ensure_runner_relay_ready(
                    session_id,
                    runner_id,
                    _runner_client,
                    conversation_store,
                )
        else:
            conv = conv_for_collaboration_mode
            if conv is None:
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
            if conv.agent_id is None:
                raise OmnigentError(
                    "Not a session (no agent binding)",
                    code=ErrorCode.NOT_FOUND,
                )

        updated = await asyncio.to_thread(
            conversation_store.update_conversation,
            session_id,
            title=body.title,
            reasoning_effort=None if clear_effort else effort,
            _unset_reasoning_effort=clear_effort,
            model_override=None if clear_model else model_override,
            _unset_model_override=clear_model,
            cost_control_mode_override=None if clear_cost_control else cost_control_mode_override,
            _unset_cost_control_mode_override=clear_cost_control,
            terminal_launch_args=terminal_launch_args,
            archived=body.archived,
        )
        if updated is None:
            raise _session_not_found()
        # Archiving hides the session from the default view (and its unread
        # dot), so drop its per-user read-state to bound in-memory growth.
        # Only on archive→true; unarchiving leaves it pruned (reads as seen).
        if body.archived is True:
            _prune_session_read_state(session_id)
        # Notify the runner of effort / model changes so harnesses
        # that can't re-read these from store at turn boundaries
        # (today: claude-native, whose ``claude`` binary has
        # ``--effort`` / ``--model`` baked in at spawn) get a chance
        # to propagate them live. Best-effort — persisted values
        # remain the authoritative fallback. Skip both when
        # ``silent`` so bind-time auto-apply doesn't inject visible
        # ``/model X`` items into a fresh pane.
        # Effort and model both go through the unified ``/events``
        # dispatch — Omnigent server stays harness-agnostic; the runner
        # dispatches by harness (claude-native injects the slash
        # command into tmux, other harnesses 204 no-op). See
        # ``_forward_session_change_to_runner`` for the shared
        # runner-client fallback + non-2xx logging.
        live_forward = not body.silent
        if live_forward and (effort is not None or clear_effort):
            await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": "effort_change", "effort": updated.reasoning_effort},
            )
        if live_forward and (model_override is not None or clear_model):
            await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": "model_change", "model": updated.model_override},
            )
            # Append a durable [System: model changed to X] note for sessions
            # whose history Omnigent writes. Gate on the wrapper label (NOT
            # omnigent.ui, which chat-first SDK terminal-view sessions like
            # polly/debby also carry) — see _persist_model_change_note for the
            # full rationale. live_forward (== not silent) already excludes
            # bind-time auto-applies, so only an explicit /model lands a note.
            if not _is_native_terminal_session(updated):
                await _persist_model_change_note(
                    session_id,
                    updated.model_override,
                    conversation_store,
                )
        if requested_codex_collaboration_mode is not None and live_forward:
            _codex_plan_enabled = _codex_plan_mode_enabled(requested_codex_collaboration_mode)
            _runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {
                    "type": "plan_mode_change",
                    "enabled": _codex_plan_enabled,
                },
            )
            _require_collaboration_mode_forward(
                session_id,
                _codex_plan_enabled,
                _runner_result,
            )
        # The project label is special: an empty-string value means "remove
        # from project" (delete the label row) rather than upsert an empty value.
        # Split it out before the bulk upsert so other labels are unaffected.
        if labels_to_set and labels_to_set.get(PROJECT_LABEL_KEY) == "":
            labels_to_set = {k: v for k, v in labels_to_set.items() if k != PROJECT_LABEL_KEY}
            await asyncio.to_thread(conversation_store.delete_label, session_id, PROJECT_LABEL_KEY)
        if labels_to_set:
            await asyncio.to_thread(conversation_store.set_labels, session_id, labels_to_set)
        if requested_codex_collaboration_mode is not None:
            _publish_collaboration_mode(
                session_id,
                requested_codex_collaboration_mode,
            )
        if body.external_session_id is not None:
            try:
                await asyncio.to_thread(
                    conversation_store.set_external_session_id,
                    session_id,
                    body.external_session_id,
                )
            except ConversationNotFoundError as exc:
                # Race: row vanished between the update above and this
                # write. Reuse the NOT_FOUND code for consistency.
                raise _session_not_found() from exc
            except ValueError as exc:
                # Store raises ValueError on attempted overwrite of an
                # already-set external_session_id — surface as
                # invalid_input so the caller (a wrapper bridge) sees a
                # 400 with the conflict explained.
                raise OmnigentError(
                    str(exc),
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        # File into a first-class project (owner-only, gated above). ``""``
        # unfiles; a non-empty id must name a project the caller owns. Filing
        # into another owner's (or a missing) project is rejected as NOT_FOUND
        # — the same 404 the projects API returns, so we don't leak existence.
        if set_project:
            # ``""`` unfiles; a non-empty id files. Explicit JSON ``null`` is
            # not a valid value here (omitting the field is how you leave
            # membership unchanged), so reject it rather than treating it as a
            # destructive unfile.
            if body.project_id is None:
                raise OmnigentError(
                    'project_id must be a project id or "" to unfile; '
                    "omit the field to leave membership unchanged",
                    code=ErrorCode.INVALID_INPUT,
                )
            target_project_id = body.project_id
            if target_project_id == "":
                unfiled = await asyncio.to_thread(
                    conversation_store.set_conversation_project, session_id, None
                )
                if not unfiled:
                    raise _session_not_found()
            else:
                if project_store is None:
                    raise OmnigentError(
                        "Filing a session into a project is not supported by this server",
                        code=ErrorCode.INVALID_INPUT,
                    )
                owned = await asyncio.to_thread(
                    project_store.get, target_project_id, owner_user_id=user_id
                )
                if owned is None:
                    raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
                filed = await asyncio.to_thread(
                    conversation_store.set_conversation_project,
                    session_id,
                    target_project_id,
                )
                if not filed:
                    raise _session_not_found()
        level = await _get_permission_level(user_id, session_id, permission_store)
        return await _get_session_snapshot(
            conversation_store,
            session_id,
            level,
            agent_store,
            agent_cache,
            liveness_lookup=liveness_lookup,
            runner_exit_reports=runner_exit_reports,
        )

    # ── POST /sessions/{source_id}/fork ─────────────────────────

    @router.post(
        "/sessions/{source_id}/fork",
        status_code=201,
        # response_model=None keeps FastAPI from re-validating/serializing
        # the handler's SessionResponse; responses= still advertises the
        # body schema to docs/SDK tooling.
        response_model=None,
        responses={201: {"model": SessionResponse}},
    )
    async def fork_session(
        request: Request,
        source_id: str,
        body: SessionForkRequest,
    ) -> SessionResponse:
        """
        Fork an existing session into a new session.

        Deep-copies the source session's conversation items and
        clones the agent into a new session. When ``body.agent_id``
        is set, the fork binds that built-in agent instead of the
        source's — switching harness (e.g. Claude-SDK → Claude Code,
        or Claude → Codex). The source's model settings carry over
        only within the same provider family; a same-family native
        target also carries conversation history (the runner rebuilds
        its transcript). The REPL/CLI binds the fork to its runner via
        ``PATCH /v1/sessions/{id}`` after creation.

        When ``body.up_to_response_id`` is set, only history up to and
        including that response is copied into the fork (a "fork from
        this response"); a native target then rebuilds its transcript
        from the truncated items instead of resuming the source's full
        native transcript.

        :param request: The incoming FastAPI request (for auth).
        :param source_id: Session/conversation identifier of the
            source session to fork, e.g. ``"conv_abc123"``.
        :param body: The validated :class:`SessionForkRequest`.
        :returns: A :class:`SessionResponse` describing the newly
            created fork (status ``"idle"``).
        :raises OmnigentError: 404 if *source_id* does not exist
            or ``body.agent_id`` is not a bindable built-in agent;
            403 if the caller lacks read access; 400 if the source
            is a sub-agent session, has no agent binding, or
            ``body.up_to_response_id`` names no response in the
            source session.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, source_id, LEVEL_READ, permission_store, conversation_store
        )
        source = access.conversation
        if source is None:
            source = await asyncio.to_thread(conversation_store.get_conversation, source_id)
            if source is None:
                raise OmnigentError(
                    f"Session not found: {source_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if source.kind == "sub_agent":
            raise OmnigentError(
                "Cannot fork a sub-agent session — only top-level sessions can be forked.",
                code=ErrorCode.INVALID_INPUT,
            )
        if source.agent_id is None:
            raise OmnigentError(
                "Source session has no agent binding — cannot fork.",
                code=ErrorCode.INVALID_INPUT,
            )

        source_agent = await asyncio.to_thread(agent_store.get, source.agent_id)
        if source_agent is None:
            raise OmnigentError(
                f"Source agent not found: {source.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # By default the fork clones the source's agent (same harness). When
        # ``body.agent_id`` names a different agent, the fork SWITCHES to it
        # — e.g. fork a Claude-SDK session into Claude Code. Only built-in
        # agents (``session_id IS NULL``) are bindable: a session-scoped
        # agent belongs to one conversation (possibly another user's) and
        # must never be cloned across sessions.
        base_agent = source_agent
        switching_agent = body.agent_id is not None and body.agent_id != source.agent_id
        if switching_agent:
            target_agent = await asyncio.to_thread(agent_store.get, body.agent_id)
            if target_agent is None or target_agent.session_id is not None:
                raise OmnigentError(
                    f"Agent not found or not bindable: {body.agent_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
            base_agent = target_agent

        # Clone params for the fork's session-scoped agent. Created inside
        # fork_conversation's transaction (not agent_store.create): a
        # pre-created row would survive a fork failure as an orphaned
        # session_id=NULL built-in polluting the picker. Session-scoped rows
        # are exempt from the unique built-in-name index, so the clone reuses
        # the source's name verbatim — no "(fork …)" suffix needed.
        cloned_agent_id = generate_agent_id()
        cloned_agent_name = base_agent.name

        # A model id is provider-bound, so the source's model_override /
        # reasoning_effort only carry over when the switch stays in the same
        # provider family. A cross-family switch (or an undeterminable
        # family) resets them; same-agent forks always copy.
        copy_model_settings = True
        if switching_agent:
            copy_model_settings = await asyncio.to_thread(
                _same_provider_family, source_agent, base_agent
            )

        # When the fork binds a NATIVE target, the native CLI won't replay
        # the copied Omnigent transcript on its own — mark the fork so the
        # runner carries history into the native harness. Same-family: clone
        # the source's native transcript when present, else rebuild from the
        # copied Omnigent items. Cross-family: the source's native transcript
        # is the wrong format, so ALWAYS rebuild from the copied Omnigent
        # items (the converters consume Omnigent's normalized item shape, so
        # the source harness doesn't matter). SDK targets replay the
        # transcript as context regardless, so the marker is inert for them.
        # claude/codex/pi native rebuild the transcript (each rebuilds its
        # resumable session file from the copied items, so all three sit in
        # _FORK_HISTORY_NATIVE_HARNESSES); cursor native instead replays prior
        # turns as a text preamble (its conversation is server-backed, so a
        # local store can't be seeded — fork-only, see
        # _agent_carries_cursor_fork_history). The single FORK_CARRY_HISTORY
        # label drives both; the runner branches on harness.
        target_is_cursor = await asyncio.to_thread(_agent_carries_cursor_fork_history, base_agent)
        carry_history_into_native = target_is_cursor or await asyncio.to_thread(
            _agent_carries_native_fork_history, base_agent
        )
        # The source's native session id is only resumable by a target in the
        # SAME provider family — a Claude target can't clone a Codex rollout.
        # Cross-family, the store must skip the fork-source directive so the
        # runner takes the rebuild path instead of a doomed clone attempt
        # (a failed clone launches fresh, losing history). cursor never clones a
        # native session (server-backed; it carries history via the preamble),
        # so it always skips the source directive too.
        resume_source_native_session = (
            not switching_agent or copy_model_settings
        ) and not target_is_cursor

        # On an agent switch, recompute the Web UI presentation labels for
        # the TARGET harness so the clone isn't left in the source's UI mode
        # (e.g. a claude-native source's terminal-first labels would put an
        # SDK clone in terminal mode with a stale interactive terminal).
        # A same-agent fork leaves the copied labels untouched (None).
        presentation_labels = (
            await asyncio.to_thread(_presentation_labels_for_agent, base_agent)
            if switching_agent
            else None
        )

        try:
            new_conv = await asyncio.to_thread(
                conversation_store.fork_conversation,
                source_id,
                title=body.title,
                agent_id=cloned_agent_id,
                cloned_agent_name=cloned_agent_name,
                cloned_agent_bundle_location=base_agent.bundle_location,
                cloned_agent_description=base_agent.description,
                copy_model_settings=copy_model_settings,
                # Launch flags are CLI-specific. On an agent switch the fork may
                # bind a different CLI (e.g. claude-code → pi), whose flag set
                # differs — Claude Code's ``--permission-mode`` makes pi exit at
                # launch (unknown option → ``required_terminal_exited``). Only
                # carry the source's launch args on a same-agent fork.
                copy_terminal_launch_args=not switching_agent,
                carry_history_into_native=carry_history_into_native,
                resume_source_native_session=resume_source_native_session,
                presentation_labels=presentation_labels,
                up_to_response_id=body.up_to_response_id,
            )
        except LookupError as exc:
            raise OmnigentError(
                f"Session not found: {source_id!r}",
                code=ErrorCode.NOT_FOUND,
            ) from exc
        except ValueError as exc:
            # Store raises ValueError when up_to_response_id names no
            # response in the source conversation (stale client state).
            raise OmnigentError(
                str(exc),
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        if permission_store is not None and user_id is not None:
            await asyncio.to_thread(permission_store.ensure_user, user_id)
            await asyncio.to_thread(permission_store.grant, user_id, new_conv.id, LEVEL_OWNER)
        # Push the forked session to this user's other open tabs.
        _announce_session_added(user_id, new_conv.id)

        fork_items = await asyncio.to_thread(
            conversation_store.list_items, new_conv.id, limit=10000
        )
        level = await _get_permission_level(user_id, new_conv.id, permission_store)
        return _build_session_response(
            new_conv,
            fork_items.data,
            "idle",
            permission_level=level,
            last_task_error=None,
            agent_name=base_agent.name,
        )

    # ── POST /sessions/{session_id}/switch-agent ─────────────────

    @router.post(
        "/sessions/{session_id}/switch-agent",
        # response_model=None keeps FastAPI from re-validating/serializing
        # the handler's SessionResponse; responses= still advertises the
        # body schema to docs/SDK tooling.
        response_model=None,
        responses={200: {"model": SessionResponse}},
    )
    async def switch_session_agent(
        request: Request,
        session_id: str,
        body: SessionSwitchAgentRequest,
        background_tasks: BackgroundTasks,
    ) -> SessionResponse:
        """
        Switch an existing session in place to a different agent/harness.

        Unlike fork, this keeps the SAME session — transcript, comments,
        files, host, and workspace are untouched; only the agent/harness
        changes. The current session-scoped agent is replaced by a clone
        of the target built-in, model settings carry over only within the
        same provider family (a model id is provider-bound), the native
        runtime session id is cleared, and the harness-presentation labels
        are recomputed for the target. The next turn cold-starts the new
        harness (rebuilding the native transcript from this session's own
        items for a same-family native target). Only built-in agents are
        bindable, and only while the session is idle.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier to switch,
            e.g. ``"conv_abc123"``.
        :param body: The validated :class:`SessionSwitchAgentRequest`.
        :returns: A :class:`SessionResponse` describing the session after
            the switch (status ``"idle"``).
        :raises OmnigentError: 404 if the session or target agent does
            not exist or the target is not a bindable built-in; 403 if the
            caller lacks edit access; 400 if the session is a sub-agent,
            has no agent binding, or the target bundle can't be loaded;
            409 if a turn is currently running.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        session = access.conversation
        if session is None:
            session = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if session is None:
                raise OmnigentError(
                    f"Session not found: {session_id!r}",
                    code=ErrorCode.NOT_FOUND,
                )
        if session.kind == "sub_agent":
            raise OmnigentError(
                "Cannot switch the agent of a sub-agent session — only top-level "
                "sessions can switch agent.",
                code=ErrorCode.INVALID_INPUT,
            )
        if session.agent_id is None:
            raise OmnigentError(
                "Session has no agent binding — cannot switch agent.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Switching mid-turn would tear the running harness subprocess out
        # from under an active stream. Reject; the caller retries when idle.
        if _session_status_from_cache(session_id) == "running":
            raise OmnigentError(
                "Session is busy — wait for the current turn to finish before switching agent.",
                code=ErrorCode.CONFLICT,
            )

        current_agent = await asyncio.to_thread(agent_store.get, session.agent_id)
        if current_agent is None:
            raise OmnigentError(
                f"Current agent not found: {session.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # Only built-in agents (``session_id IS NULL``) are bindable: a
        # session-scoped agent belongs to one conversation (possibly another
        # user's) and must never be cloned across sessions.
        target_agent = await asyncio.to_thread(agent_store.get, body.agent_id)
        if target_agent is None or target_agent.session_id is not None:
            raise OmnigentError(
                f"Agent not found or not bindable: {body.agent_id!r}",
                code=ErrorCode.NOT_FOUND,
            )

        # Reject a no-op switch to the built-in the session is already running:
        # its session-scoped clone shares the built-in's ``bundle_location``, so
        # switching would delete + re-clone the same agent and tear the terminal
        # down for nothing. The contract is that the target differs from the
        # current agent; the picker already hides the current one, so this only
        # guards a direct API call.
        if target_agent.bundle_location == current_agent.bundle_location:
            raise OmnigentError(
                "Session is already running this agent — pick a different one.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Load the target bundle BEFORE committing so an unloadable spec fails
        # the request with zero mutation — the irreversible part of the switch
        # (deleting the old agent) must not run for a target that can't start.
        try:
            await asyncio.to_thread(
                get_agent_cache().load, target_agent.id, target_agent.bundle_location
            )
        except Exception as exc:
            # Surface any bundle-load failure as a 400 before mutating state.
            raise OmnigentError(
                f"Target agent bundle could not be loaded: {body.agent_id!r}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        # A model id is provider-bound, so model_override / reasoning_effort
        # carry over only within the same provider family. A native target
        # carries history regardless of family: the switch clears
        # external_session_id and drops the fork-source directive, so the
        # runner rebuilds the native transcript from this session's own
        # Omnigent items (a format-agnostic conversion). SDK targets replay
        # the AP transcript as context regardless.
        copy_model_settings = await asyncio.to_thread(
            _same_provider_family, current_agent, target_agent
        )
        # claude/codex/pi native can replay fork history (each rebuilds its
        # resumable session file from the copied items); cursor-native can't
        # (no resumable session file), so don't stamp a carry-history promise
        # it would silently break with a fresh launch.
        carry_history_into_native = await asyncio.to_thread(
            _agent_carries_native_fork_history, target_agent
        )
        presentation_labels = await asyncio.to_thread(_presentation_labels_for_agent, target_agent)

        # Resolve the built-in the session is leaving so the UI can offer a
        # one-click "Switch back". The current agent is a session-scoped clone
        # whose bundle_location was copied verbatim from its source built-in,
        # so match on that. Page through the full template-agent list (not a
        # single bounded scan) so the match isn't missed when there are many
        # built-ins. Best-effort: None when no built-in matches (e.g. its
        # source built-in was removed) → no switch-back offered.
        previous_builtin_id: str | None = None
        _after: str | None = None
        while True:
            _page = await asyncio.to_thread(agent_store.list, 100, _after)
            previous_builtin_id = next(
                (a.id for a in _page.data if a.bundle_location == current_agent.bundle_location),
                None,
            )
            if previous_builtin_id is not None or not _page.has_more or not _page.data:
                break
            _after = _page.last_id

        cloned_agent_id = generate_agent_id()
        cloned_agent_name = f"{target_agent.name} (switch {cloned_agent_id[:10]})"
        try:
            updated = await asyncio.to_thread(
                conversation_store.switch_conversation_agent,
                session_id,
                new_agent_id=cloned_agent_id,
                new_agent_name=cloned_agent_name,
                new_agent_bundle_location=target_agent.bundle_location,
                new_agent_description=target_agent.description,
                copy_model_settings=copy_model_settings,
                carry_history_into_native=carry_history_into_native,
                presentation_labels=presentation_labels,
                previous_builtin_id=previous_builtin_id,
            )
        except LookupError as exc:
            raise OmnigentError(
                f"Session not found: {session_id!r}",
                code=ErrorCode.NOT_FOUND,
            ) from exc

        # Tell every connected client the binding changed so they re-derive
        # session state (presentation labels, bound agent) from a fresh
        # snapshot. Without this, a client that bound before the switch keeps
        # treating the session as the OLD harness — e.g. its status handler
        # clears the optimistic first-message bubble that a native target
        # only reconciles later via session.input.consumed.
        switch_event = SessionAgentChangedEvent(
            type="session.agent_changed",
            conversation_id=session_id,
            agent_id=cloned_agent_id,
            # Clean target name, not the clone row's "<name> (switch ag_…)":
            # the suffix only disambiguates agent rows; clients render
            # agent_name verbatim (same choice as the session snapshot).
            agent_name=target_agent.name,
        )
        session_stream.publish(session_id, switch_event.model_dump())

        # Reset the OLD harness's runner-side resources (async, after the
        # response): close the cached primary OSEnv so the new agent's
        # os_env/sandbox governs the web filesystem/shell endpoints, and tear
        # down the native terminal so it can't shadow the switch-back transcript
        # rebuild. Safe because the switch only runs while the session is idle
        # (doing it mid-turn would wedge the turn); the next access
        # re-materializes from the new agent's spec, preserving the workspace /
        # worktree (cwd comes from the runner workspace).
        background_tasks.add_task(_reset_runner_resources_after_switch, session_id)

        items = await asyncio.to_thread(conversation_store.list_items, session_id, limit=10000)
        level = await _get_permission_level(user_id, session_id, permission_store)
        return _build_session_response(
            updated,
            items.data,
            "idle",
            permission_level=level,
            last_task_error=None,
            agent_name=target_agent.name,
        )

    # ── POST /sessions/{session_id}/hooks/permission-request ─────

    @router.post(
        "/sessions/{session_id}/hooks/permission-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def claude_permission_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Claude Code ``PermissionRequest`` HTTP hook endpoint.

        Receives Claude Code's PermissionRequest hook payload (tool
        name + input the user would otherwise see a TUI prompt for),
        publishes a ``response.elicitation_request`` SSE event on the
        session stream so the web UI's :file:`ApprovalCard` renders
        inline, and long-polls until the verdict arrives via the
        session ``approval`` event path.

        Response shape follows Claude Code's PermissionRequest hook
        contract: ``hookSpecificOutput.decision.behavior`` is
        ``"allow"`` or ``"deny"``. On timeout the endpoint returns
        ``200`` with an empty body — Claude Code treats that as
        "defer to the TUI prompt", which matches the wrapper's
        fail-ask contract (UI unreachable / unattended → fall back
        to terminal-side approval).

        Auth: standard session ACL — the wrapper's outbound headers
        (``ap_auth_headers`` in :func:`build_hook_settings`) carry
        the same Bearer token used for every other Omnigent request. For
        local-server mode (no auth provider), unauth'd calls are
        allowed.

        :param request: FastAPI request — body is Claude Code's
            PermissionRequest payload as JSON.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: Claude PermissionRequest hookSpecificOutput JSON,
            or ``200`` with empty body on timeout (fail-ask).
        :raises OmnigentError: 404 if the session doesn't exist,
            400 if the body fails JSON parse or is missing
            ``tool_name``.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in PermissionRequest hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "PermissionRequest hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            raise OmnigentError(
                "PermissionRequest hook body must include a non-empty 'tool_name' string.",
                code=ErrorCode.INVALID_INPUT,
            )
        tool_input = payload.get("tool_input")
        if tool_input is not None and not isinstance(tool_input, dict):
            raise OmnigentError(
                "PermissionRequest hook body 'tool_input' must be an object when present.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Claude Code's PermissionRequest payload carries no
        # ``tool_use_id`` (verified against a real payload — the field
        # is absent, not merely unstable; the id is only minted when the
        # tool call is emitted, AFTER this permission check). And newer
        # builds can write the transcript ``function_call`` (tool_use)
        # before this hook returns — so neither can correlate/resolve the
        # parked request. The parked wait ends on one of three signals: an
        # explicit web verdict, hook disconnect, or the mirrored
        # ``function_call_output`` (tool_result) for this gated tool,
        # which — unlike the tool_use — is written only AFTER the
        # prompt was answered in the TUI. We pass ``tool_name`` /
        # ``tool_input`` below so that result can be correlated back to
        # THIS prompt (see _signal_terminal_resolved_harness_elicitation).
        cwd = payload.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            cwd = None
        permission_mode = payload.get("permission_mode")
        if permission_mode is not None and not isinstance(permission_mode, str):
            permission_mode = None
        elicitation_id = _client_supplied_hook_elicitation_id(payload, session_id)

        try:
            preview_str = json.dumps(tool_input or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            preview_str = repr(tool_input)
        preview_str = preview_str[:1024]

        # ``extra="allow"`` on ElicitationRequestParams permits
        # extra keyword arguments to ride alongside the MCP
        # standard fields. Use it for Claude-native display and
        # correlation hints rather than minting AP-specific fields
        # on the model; strict MCP clients can ignore unknown fields
        # while AP's UI consumes them.
        # ``tool_name`` rides along so the UI can render the
        # permission card with the gated tool name and distinguish
        # simultaneous prompts from different tools.
        extras: dict[str, Any] = {"tool_name": tool_name}
        if cwd is not None:
            extras["cwd"] = cwd
        if permission_mode is not None:
            extras["permission_mode"] = permission_mode
        # The card offers ONE persistent-approval affordance, picked by
        # the gated tool — the two hints below are mutually exclusive
        # (disjoint eligibility), never two buttons competing on one card.
        #
        # Edit tools → "Accept & allow all edits" (switches the session to
        # acceptEdits via setMode). Stamped only for edit-tool prompts
        # under a still-prompting mode — see _allow_all_edits_eligible.
        # The verdict site re-checks the same predicate before honoring it.
        if _allow_all_edits_eligible(tool_name, permission_mode):
            extras["allow_all_edits"] = True
        # Non-edit eligible tools → "don't ask again" (installs a
        # session-scoped allow rule via addRules). Stamped only when the
        # affordance applies — see _allow_remember_eligible.
        # ``remember_scope`` carries the gated tool and, for WebFetch, the
        # request host so the UI can label the button ("… for github.com"
        # vs "… for WebFetch"); the verdict site re-derives the same scope
        # before honoring the flag, never trusting a client-supplied rule.
        if _allow_remember_eligible(tool_name, permission_mode):
            remember_scope: dict[str, Any] = {"tool": tool_name}
            remember_host = _claude_native_remember_host(tool_name, tool_input)
            if remember_host is not None:
                remember_scope["host"] = remember_host
            extras["remember_scope"] = remember_scope
        # When Claude's built-in AskUserQuestion tool is the one
        # needing permission, the PermissionRequest payload
        # already carries the full questions + options structure
        # in ``tool_input``. Surface it as a structured extra so
        # the UI can render an interactive form WITHOUT having to
        # parse the (truncated) ``content_preview`` JSON blob.
        # ``content_preview`` keeps its 1024-char cap for the
        # binary-card fallback; the structured field is the
        # authoritative source the UI consumes when present.
        if tool_name == "AskUserQuestion":
            ask_payload = _structured_ask_user_question(tool_input)
            if ask_payload is not None:
                extras["ask_user_question"] = ask_payload
        # When the gated tool is ExitPlanMode, ride the full
        # ``tool_input`` through verbatim so the UI can render a
        # dedicated plan-review card. ``content_preview`` is
        # hard-capped at 1024 chars — real plans blow well past it —
        # and the input's shape varies across Claude Code builds
        # (``plan`` markdown, ``allowedPrompts``, ...), so no field
        # filtering: every field the hook carried natively reaches
        # the UI. An empty/absent input stamps nothing, leaving the
        # binary-card fallback.
        if tool_name == "ExitPlanMode" and isinstance(tool_input, dict) and tool_input:
            extras["exit_plan_mode"] = tool_input
        params = ElicitationRequestParams(
            mode="form",
            message=f"Claude wants to call **{tool_name}**",
            requestedSchema=None,
            url=None,
            phase="pre_tool_use",
            policy_name="claude_native_permission",
            content_preview=f"{tool_name}({preview_str})",
            **extras,
        )
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            # Client-minted stable id so a retry re-parks the same elicitation.
            elicitation_id=elicitation_id,
            # Tool identity lets a mirrored tool result for this gated
            # tool resolve the prompt promptly when the user answers in
            # Claude's TUI instead of the web UI (terminal-resolved
            # fast path). ``tool_input`` is the dict from the payload
            # (or None when absent).
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else None,
        )
        if result is None:
            # Disconnect or timeout. Either way Claude is no
            # longer waiting on this response; empty 2xx → Claude
            # defers to its built-in TUI prompt (fail-ask).
            return Response(status_code=status.HTTP_200_OK)

        behavior = "allow" if result.action == "accept" else "deny"
        decision: dict[str, Any] = {"behavior": behavior}
        # A decline can carry feedback typed into the web card (the
        # ExitPlanMode "Reject with feedback" flow). Claude's
        # PermissionRequest decision contract surfaces it via
        # ``decision.message`` — the model sees it as the denial
        # reason, so for a rejected plan Claude stays in plan mode
        # and revises toward the feedback instead of guessing why
        # the plan was refused.
        if behavior == "deny" and isinstance(result.content, dict):
            feedback = result.content.get("feedback")
            if isinstance(feedback, str) and feedback.strip():
                decision["message"] = feedback
        # When the gated tool is AskUserQuestion AND the user accepted
        # with selections, propagate those selections back to Claude
        # via ``decision.updatedInput``. Claude reads
        # ``tool_input.answers`` and skips its TUI picker, returning
        # the supplied selections as the tool result the LLM sees.
        #
        # ``result.content`` is MCP-shaped (a flat ``{[field]: value}``
        # map) — exactly the shape ``tool_input.answers`` expects on
        # AskUserQuestion. Single-select values are strings,
        # multi-select are ``list[str]``; both ride through verbatim.
        if (
            behavior == "allow"
            and tool_name == "AskUserQuestion"
            and isinstance(tool_input, dict)
            and isinstance(result.content, dict)
            and result.content
        ):
            decision["updatedInput"] = {**tool_input, "answers": result.content}
        # "Accept & allow all edits" — the user approved this edit AND
        # asked to auto-accept future edits. Echo a ``setMode`` permission
        # update so Claude Code switches this session into ``acceptEdits``
        # mode, exactly as the native shift+tab toggle does. The
        # ``updatedPermissions`` shape matches the Agent SDK's
        # ``PermissionUpdate`` union (``{type, mode, destination}`` for
        # ``setMode``); ``destination: "session"`` scopes it to this
        # session, so it resets on the next one.
        #
        # Re-check eligibility server-side rather than trusting the
        # client's ``content.allow_all_edits`` flag alone: the flag is
        # only meaningful for the edit-tool / prompting-mode prompts the
        # affordance was offered for. Without this, a client could send
        # the flag on e.g. a Bash prompt and flip the session into
        # ``acceptEdits`` — a mode switch it was never offered.
        if (
            behavior == "allow"
            and isinstance(result.content, dict)
            and result.content.get("allow_all_edits") is True
            and _allow_all_edits_eligible(tool_name, permission_mode)
        ):
            decision["updatedPermissions"] = [
                {
                    "type": "setMode",
                    # The plan card's "Yes, and use auto mode" switches the
                    # session into Claude's ``auto`` mode; the edit-tool
                    # "Accept & allow all edits" keeps the narrower
                    # ``acceptEdits`` (auto-approve edits only).
                    "mode": "auto" if tool_name == "ExitPlanMode" else "acceptEdits",
                    "destination": "session",
                }
            ]
        elif behavior == "allow" and tool_name == "ExitPlanMode":
            # Plan approved WITHOUT auto mode — the card's "Yes,
            # manually approve edits". Pin the session to the prompting
            # ``default`` mode instead of trusting whatever mode
            # Claude's plan-exit restores, so every subsequent edit
            # prompts exactly as the button promised. De-escalation
            # only (most restrictive prompting mode), so no eligibility
            # gate is needed.
            decision["updatedPermissions"] = [
                {"type": "setMode", "mode": "default", "destination": "session"}
            ]
        # "Approve & don't ask again" — the user approved this non-edit
        # tool AND asked to stop prompting for the same scope. Echo an
        # ``addRules`` permission update so Claude Code installs a
        # session-scoped allow rule, exactly as the native TUI's "don't
        # ask again" option does. The shape matches the Agent SDK's
        # ``PermissionUpdate`` union (``addRules``): ``rules`` is a list
        # of ``{toolName, ruleContent?}`` — ``ruleContent`` omitted means
        # the whole tool; ``destination: "session"`` scopes it to this
        # session so it resets on the next one. The claude-native hook
        # forwards this decision verbatim to Claude Code.
        #
        # The host is re-derived server-side from the gated tool's input
        # rather than trusting any client-supplied rule, and gated by the
        # same ``_allow_remember_eligible`` predicate the button was
        # offered under — so a forged ``remember`` flag on an ineligible
        # tool (e.g. an edit tool, which takes the setMode path) can't
        # smuggle in an allow rule. Mutually exclusive with the edit-tool
        # ``allow_all_edits``/ExitPlanMode branches above (disjoint tool
        # sets), so it never overwrites their ``updatedPermissions``.
        if (
            behavior == "allow"
            and isinstance(result.content, dict)
            and result.content.get("remember") is True
            and _allow_remember_eligible(tool_name, permission_mode)
        ):
            rule: dict[str, Any] = {"toolName": tool_name}
            remember_host = _claude_native_remember_host(tool_name, tool_input)
            if remember_host is not None:
                rule["ruleContent"] = f"domain:{remember_host}"
            decision["updatedPermissions"] = [
                {
                    "type": "addRules",
                    "rules": [rule],
                    "behavior": "allow",
                    "destination": "session",
                }
            ]
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            },
        }
        return Response(
            content=json.dumps(body),
            media_type="application/json",
        )

    # ── Proto event-type → internal Phase mapping ────────────────────
    _PROTO_EVENT_TYPE_TO_PHASE: dict[str, Phase] = {
        "PHASE_TOOL_CALL": Phase.TOOL_CALL,
        "PHASE_TOOL_RESULT": Phase.TOOL_RESULT,
        "PHASE_LLM_REQUEST": Phase.LLM_REQUEST,
        "PHASE_LLM_RESPONSE": Phase.LLM_RESPONSE,
        # A native session's UserPromptSubmit hook posts the request phase
        # here (the server-level _evaluate_input_policy skips native message
        # events). The prompt text rides in ``event.data.text``.
        "PHASE_REQUEST": Phase.REQUEST,
    }
    _PHASE_TO_PROTO_ACTION: dict[PolicyAction, str] = {
        PolicyAction.ALLOW: "POLICY_ACTION_ALLOW",
        PolicyAction.DENY: "POLICY_ACTION_DENY",
        PolicyAction.ASK: "POLICY_ACTION_ASK",
    }

    # ── POST /sessions/{session_id}/policies/evaluate ─────────────

    @router.post(
        "/sessions/{session_id}/policies/evaluate",
        # Returns EvaluationResponse JSON; no Pydantic model since the
        # proto-style schema is validated manually.
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def evaluate_policy(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Generic policy evaluation endpoint (proto-compatible).

        Accepts an ``EvaluationRequest`` JSON body whose ``event``
        field carries the phase (``PHASE_TOOL_CALL``,
        ``PHASE_TOOL_RESULT``, ``PHASE_LLM_REQUEST``,
        ``PHASE_LLM_RESPONSE``), the event data, and optional
        context. Returns an ``EvaluationResponse`` with the policy
        verdict (``result``), an optional ``reason``, and optional
        ``data`` for content-rewriting policies.

        Used by Claude Code's ``PreToolUse`` and ``PostToolUse``
        command hooks (via ``omnigent.claude_native_hook``) to
        evaluate admin policies on native tool calls. Also usable
        by any client that speaks the proto-compatible JSON schema.

        :param request: FastAPI request — body is the
            ``EvaluationRequest`` JSON envelope.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: ``EvaluationResponse`` JSON with ``result``,
            ``reason``, and optional ``data``.
        :raises OmnigentError: 404 if the session doesn't exist,
            400 if the body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        is_read_only = access.level is not None and access.level < LEVEL_EDIT
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in policy evaluate body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Policy evaluate body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        event = payload.get("event")
        if not isinstance(event, dict):
            raise OmnigentError(
                "Policy evaluate body must include an 'event' object.",
                code=ErrorCode.INVALID_INPUT,
            )
        event_type = event.get("type")
        phase = _PROTO_EVENT_TYPE_TO_PHASE.get(event_type or "")
        if phase is None:
            raise OmnigentError(
                f"Unknown event type: {event_type!r}. "
                f"Expected one of {list(_PROTO_EVENT_TYPE_TO_PHASE)}.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Optional stable re-attach id for hook retries. Validated but not
        # required — absent on non-retrying callers (old hooks, direct API use).
        raw_elicitation_id = payload.get("_omnigent_elicitation_id")
        hook_elicitation_id: str | None = None
        if raw_elicitation_id is not None:
            if not isinstance(raw_elicitation_id, str) or not (
                _EVALUATE_HOOK_ELICITATION_ID_RE.fullmatch(raw_elicitation_id)
            ):
                raise OmnigentError(
                    "Policy evaluate '_omnigent_elicitation_id' must match "
                    "'elicit_evaluate_' + 32 hex chars.",
                    code=ErrorCode.INVALID_INPUT,
                )
            hook_elicitation_id = raw_elicitation_id
        data = event.get("data") or {}

        conv = conversation_store.get_conversation(session_id)
        if conv is None:
            raise OmnigentError(
                f"Session {session_id!r} not found.",
                code=ErrorCode.NOT_FOUND,
            )
        # Dedup the native request-phase gate. A native session's
        # ``UserPromptSubmit`` hook posts ``PHASE_REQUEST`` here for *every*
        # prompt, but a web-UI prompt was already gated server-side by
        # ``_evaluate_input_policy`` at POST /events (before injection, so no
        # TUI freeze). Re-gating it here would double-prompt the human. A
        # web-UI prompt in flight has a ``pending_inputs`` entry (recorded at
        # dispatch, drained when the forwarder mirrors it back); a prompt
        # typed directly in the TUI has none and never hit POST /events, so it
        # is gated here — the hook is its only request-phase gate. The signal
        # is "is a web prompt in flight", not text correlation (the native
        # transcript gives no reliable id channel — see ``pending_inputs``).
        if phase == Phase.REQUEST and pending_inputs.snapshot_for(session_id):
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                media_type="application/json",
            )
        agent = agent_store.get(conv.agent_id) if conv.agent_id else None
        if agent is None:
            # No agent — no policies. Return unspecified (pass-through).
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_UNSPECIFIED"}),
                media_type="application/json",
            )

        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )

        _caps = get_caps()

        # Fast path: if no policies would fire (no agent guardrails, no
        # session policies, no server-wide defaults), skip the engine build
        # entirely. This avoids conversation-store reads for labels/state/usage
        # on every tool call for the common no-policy case. Session policies are
        # LRU-cached so this check is cheap after the first call per session.
        # Users can add policies mid-session — the cache is invalidated on
        # mutation, so newly added policies are visible on the very next call.
        if not any_policies_apply(
            spec=loaded.spec,
            conversation_id=session_id,
            default_policies=_caps.default_policies,
            policy_store=get_policy_store(),
            phase=phase,
            tool_name=data.get("name") if isinstance(data, dict) else None,
        ):
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                media_type="application/json",
            )

        _host_conn = (
            _caps.policy_llm_connection_factory() if _caps.policy_llm_connection_factory else None
        )

        def _build_engine() -> PolicyEngine:
            """
            Build a policy engine for this session from the loaded spec.

            Re-reads persisted ``session_state`` / usage from the store on
            every call: the engine snapshots that state at construction and
            does not re-query it during ``evaluate``, so a fresh build is the
            only way to observe a concurrent sibling's just-recorded approval.

            :returns: A :class:`PolicyEngine` seeded with the latest
                persisted state for ``session_id``.
            """
            return build_policy_engine(
                spec=loaded.spec,
                conversation_id=session_id,
                conversation_store=conversation_store,
                default_policies=_caps.default_policies,
                policy_store=get_policy_store(),
                server_llm=_caps.llm,
                host_connection=_host_conn,
            )

        engine = _build_engine()
        # Use the turn-initiating human's identity (persisted at forward time)
        # so per-user policies gate on the correct actor even when the HTTP
        # caller is the runner's service-account credential.  Falls back to
        # user_id for direct API callers and native-terminal sessions (whose
        # turns go via _dispatch_session_event_to_runner, which does not write
        # this label).
        turn_actor = conv.labels.get(_TURN_ACTOR_LABEL)
        ctx = _build_evaluation_context(
            phase, data, event, actor=_build_actor(turn_actor or user_id)
        )
        result = await engine.evaluate(ctx, read_only=is_read_only)

        # URL-based elicitation for blocking phases: on a TOOL_CALL or
        # LLM_REQUEST ASK, hold the gate server-side rather than
        # returning ASK. Returning ASK makes the native hook emit
        # ``defer``, which a permissive ``permission_mode``
        # (acceptEdits / bypassPermissions) auto-approves — bypassing
        # the human. Instead we publish the approval elicitation, park
        # until the human resolves it via the resolve URL, and collapse
        # to a hard ALLOW / DENY so the caller never sees ASK.
        # TOOL_CALL, LLM_REQUEST, and REQUEST are the phases that can block
        # before the action proceeds (tool dispatch / LLM call / a native
        # session's user prompt via the UserPromptSubmit hook — which has no
        # ASK primitive of its own, so the server resolves ASK here).
        if result.action == PolicyAction.ASK and phase in (
            Phase.TOOL_CALL,
            Phase.LLM_REQUEST,
            Phase.REQUEST,
        ):
            if is_read_only:
                # Read-only callers must not enter the ASK gate — parking
                # creates an elicitation (a server-side mutation). Return
                # the ASK verdict directly so the caller sees the policy
                # decision without mutating the session.
                pass
            else:
                # Serialize concurrent native ASK gates for this (session, policy)
                # so parallel tool calls that all trip the same checkpoint prompt
                # the human once. The first ASK to win the lock parks; on approve
                # it records a checkpoint. Siblings then rebuild the engine and
                # re-evaluate UNDER the lock against that freshly persisted state —
                # an ALLOW (or now-hard DENY) collapses the ASK and falls through
                # without a second prompt. Held across the human wait by design;
                # a declined ASK records nothing, so siblings legitimately re-ask.
                async with _native_ask_gate_lock(session_id, result.deciding_policy):
                    engine = _build_engine()
                    result = await engine.evaluate(ctx, read_only=is_read_only)
                    if result.action == PolicyAction.ASK and phase in (
                        Phase.TOOL_CALL,
                        Phase.LLM_REQUEST,
                        Phase.REQUEST,
                    ):
                        try:
                            approved = await _hold_native_ask_gate(
                                request,
                                session_id=session_id,
                                phase=phase,
                                data=data,
                                engine=engine,
                                result=result,
                                conversation_store=conversation_store,
                                elicitation_id=hook_elicitation_id,
                            )
                        except ElicitationDeclinedError as exc:
                            # Explicit user decline: interrupt the native
                            # harness BEFORE returning the hook deny so the
                            # Escape key reaches Claude Code's tmux pane first.
                            # By the time the DENY response reaches the hook
                            # subprocess, the abort signal is already queued.
                            # Best-effort: forwarding failures are swallowed.
                            await _forward_session_change_to_runner(
                                session_id,
                                get_server_runner_router(),
                                {"type": "interrupt"},
                            )
                            verdict_body = {
                                "result": "POLICY_ACTION_DENY",
                                "reason": exc.args[0] or "Approval was declined.",
                            }
                            return Response(
                                content=json.dumps(verdict_body),
                                media_type="application/json",
                            )
                        verdict_body: dict[str, Any] = (
                            {"result": "POLICY_ACTION_ALLOW"}
                            if approved
                            else {
                                "result": "POLICY_ACTION_DENY",
                                "reason": result.reason or "Approval was not granted.",
                            }
                        )
                        return Response(
                            content=json.dumps(verdict_body),
                            media_type="application/json",
                        )
                # Re-evaluation collapsed the ASK (a sibling's approval recorded
                # the checkpoint) — fall through to the generic ALLOW/DENY handling
                # below with the rebuilt engine and updated result.

        if result.set_labels and not is_read_only:
            engine.apply_label_writes(result.set_labels)

        resp_body: dict[str, Any] = {
            "result": _PHASE_TO_PROTO_ACTION.get(result.action, "POLICY_ACTION_UNSPECIFIED"),
        }
        if result.reason:
            resp_body["reason"] = result.reason
        if result.data is not None:
            resp_body["data"] = result.data
        # A request-phase HARD DENY (no approve option) — surface the reason as a
        # dismissable tmux popup on the native pane. opencode hard-blocks the
        # prompt by its plugin throwing (rendered as a generic error), so this is
        # the clean explanation; the runner dispatch only pops for opencode
        # (claude/codex already show a clean UserPromptSubmit block). Best-effort.
        if result.action == PolicyAction.DENY and phase == Phase.REQUEST and not is_read_only:
            _spawn_native_blocked_notice_forward(
                session_id, result.reason or "Blocked by policy.", result.deciding_policy
            )
        # A tool-call DENY is decided synchronously here, so nothing else on the
        # stream reflects that the native tool was blocked. Publish a positive
        # signal so observers (web UI, capability bench) see the decision rather
        # than infer it from the blocked tool's absence. Observational, so it is
        # not gated on write access.
        if result.action == PolicyAction.DENY and phase == Phase.TOOL_CALL:
            _publish_policy_denied(session_id, result.reason or "Blocked by policy.", phase.value)
        return Response(
            content=json.dumps(resp_body),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/codex-elicitation-request ─

    @router.post(
        "/sessions/{session_id}/hooks/codex-elicitation-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def codex_elicitation_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Codex app-server elicitation request endpoint.

        Receives server-to-client JSON-RPC request envelopes forwarded
        by ``omnigent codex`` (for example
        ``mcpServer/elicitation/request`` and
        ``item/tool/requestUserInput``), publishes the standard
        ``response.elicitation_request`` session event for the web UI,
        then waits for the session-scoped ``approval`` reply. This uses
        the same registry / publish / cleanup path as the Claude-native
        ``PermissionRequest`` hook so pending badges and disconnect
        handling stay consistent across native harnesses.

        :param request: FastAPI request carrying the Codex JSON-RPC
            request envelope.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: Codex JSON-RPC ``result`` payload for the forwarded
            request, or ``200`` with empty body on timeout/disconnect.
        :raises OmnigentError: 404 if the session does not exist,
            400 if the request envelope is malformed or unsupported.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in Codex elicitation hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Codex elicitation hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        codex_request = parse_codex_elicitation_request(payload)
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=codex_request.params,
            timeout_s=_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=codex_elicitation_id(
                session_id,
                codex_request.method,
                codex_request.request_id,
            ),
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt Codex before returning the
            # deny response, same as the Claude-native path. The await
            # ensures the abort signal reaches Codex before it processes
            # the decline result and lets the LLM continue.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        body = codex_request.build_response(result)
        return Response(
            content=json.dumps(body),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/antigravity-elicitation-request ──

    @router.post(
        "/sessions/{session_id}/hooks/antigravity-elicitation-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def antigravity_elicitation_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Antigravity (agy) elicitation request endpoint.

        Receives ``{"elicitation_id": <str>, "params": <ElicitationRequestParams>}``
        from the interaction bridge (Task 8), which POSTs here when it
        surfaces an agy WAITING interaction for the web UI. Parks the call
        on the shared harness elicitation registry, emits the standard
        ``response.elicitation_request`` SSE event, waits for the session
        ``approval`` verdict, then returns the raw
        :class:`~omnigent.server.schemas.ElicitationResult` so the bridge
        can forward it to agy via ``HandleCascadeUserInteraction``.

        This is intentionally simpler than the Codex hook: the bridge
        (not the endpoint) builds the agy interaction payload via
        ``to_interaction_payload``, so this endpoint only passes back
        the verdict as-is.  The body shape is minimal and symmetric:
        ``elicitation_id`` from the bridge's deterministic id function
        (``agy_elicitation_id``), ``params`` as an
        :class:`~omnigent.server.schemas.ElicitationRequestParams` dict.

        :param request: FastAPI request carrying the agy elicitation body.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: ``ElicitationResult`` JSON on user verdict; ``200`` with
            empty body on timeout/disconnect (bridge interprets as ``None``).
        :raises OmnigentError: 404 if the session does not exist, 400 if
            the request body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in antigravity elicitation hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Antigravity elicitation hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        elicitation_id = payload.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise OmnigentError(
                "Antigravity elicitation hook body must include a non-empty"
                " 'elicitation_id' string.",
                code=ErrorCode.INVALID_INPUT,
            )
        raw_params = payload.get("params")
        if not isinstance(raw_params, dict):
            raise OmnigentError(
                "Antigravity elicitation hook body must include a 'params' object.",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            params = ElicitationRequestParams.model_validate(raw_params)
        except Exception as exc:
            raise OmnigentError(
                f"Invalid 'params' in antigravity elicitation hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_ANTIGRAVITY_NATIVE_ELICITATION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=elicitation_id,
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt the native harness before
            # returning the decline so the abort signal arrives first.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        return Response(
            content=result.model_dump_json(),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/cursor-permission-request ─

    @router.post(
        "/sessions/{session_id}/hooks/cursor-permission-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def cursor_permission_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Cursor-native tool-approval hook (TUI → web elicitation).

        Receives a tool-approval prompt detected on the ``cursor-agent`` TUI
        pane by the runner-side mirror
        (:mod:`omnigent.cursor_native_permissions`), publishes the standard
        ``response.elicitation_request`` event for the web UI, then parks for
        the session ``approval`` verdict — the same registry / publish /
        cleanup path as the Codex- and Claude-native hooks, so pending badges
        and disconnect handling stay consistent across native harnesses. An
        empty ``200`` (no web verdict — the prompt was answered in the TUI, or
        the wait timed out) leaves cursor's native prompt authoritative.

        :param request: FastAPI request carrying the detected prompt
            (``elicitation_id`` plus the ``message`` / ``content_preview`` /
            ``operation_type`` to render).
        :param session_id: Omnigent conversation id from the URL path.
        :returns: An ``ElicitationResult`` (``{"action": …}``) on a web
            verdict, or ``200`` with empty body on TUI-resolution / timeout /
            disconnect.
        :raises OmnigentError: 404 if the session does not exist, 400 if the
            body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in cursor permission hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Cursor permission hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        elicitation_id = payload.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise OmnigentError(
                "Cursor permission hook body must include 'elicitation_id'.",
                code=ErrorCode.INVALID_INPUT,
            )
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = "Cursor wants approval to run a tool"
        content_preview = payload.get("content_preview")
        if not isinstance(content_preview, str):
            content_preview = None
        operation_type = payload.get("operation_type")
        if not isinstance(operation_type, str) or not operation_type:
            operation_type = "tool"
        # Structured AskQuestion payload (cursor's multiple-choice tool): when
        # present, stamp it as the ``ask_user_question`` extra so the web UI
        # renders the interactive form from it directly. ``content_preview`` is
        # hard-capped at 1024 chars, which truncates a multi-question payload and
        # breaks the preview-parse fallback — the structured field has no such
        # cap and is the authoritative source the UI consumes when present.
        extras: dict[str, Any] = {}
        ask_user_question = payload.get("ask_user_question")
        if isinstance(ask_user_question, dict) and isinstance(
            ask_user_question.get("questions"), list
        ):
            extras["ask_user_question"] = ask_user_question
        params = ElicitationRequestParams(
            mode="form",
            message=message,
            requestedSchema=None,
            url=None,
            phase="pre_tool_use",
            policy_name="cursor_native_permission",
            content_preview=content_preview,
            **extras,
        )
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_CURSOR_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=elicitation_id,
            tool_name=f"Cursor({operation_type})",
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt the native harness before
            # returning the decline so the abort signal arrives first.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        return Response(
            content=json.dumps(result.model_dump(exclude_none=True)),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/native-permission-request ─

    @router.post(
        "/sessions/{session_id}/hooks/native-permission-request",
        # Internal harness callback webhook — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(require_json_content_type)],
    )
    async def native_permission_request_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Generic native-TUI tool-approval hook (TUI → web elicitation).

        The vendor-agnostic counterpart of
        :func:`cursor_permission_request_hook`, used by the hermes- and
        goose-native approval mirrors. The runner-side mirror detects the
        vendor's in-terminal approval prompt, POSTs it here, and the server
        publishes ``response.elicitation_request`` and parks for the web verdict
        — the same registry/publish/cleanup path as the cursor/codex/claude
        hooks. An empty ``200`` (TUI answered, or timeout) leaves the vendor's
        native prompt authoritative.

        Unlike the cursor hook, the card label / policy name come from the
        payload (``agent`` / ``policy_name``) so a Hermes or Goose approval is
        labelled as such, not "Cursor".

        :param request: FastAPI request carrying the detected prompt
            (``elicitation_id``, ``message``, ``content_preview``,
            ``operation_type``, optional ``agent`` / ``policy_name``).
        :param session_id: Omnigent conversation id from the URL path.
        :returns: An ``ElicitationResult`` (``{"action": …}``) on a web verdict,
            or ``200`` with empty body on TUI-resolution / timeout / disconnect.
        :raises OmnigentError: 404 if the session does not exist, 400 if the
            body is malformed.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in native permission hook body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Native permission hook body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        elicitation_id = payload.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            raise OmnigentError(
                "Native permission hook body must include 'elicitation_id'.",
                code=ErrorCode.INVALID_INPUT,
            )
        agent = payload.get("agent")
        if not isinstance(agent, str) or not agent:
            agent = "Agent"
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = f"{agent} wants approval to run a tool"
        content_preview = payload.get("content_preview")
        if not isinstance(content_preview, str):
            content_preview = None
        operation_type = payload.get("operation_type")
        if not isinstance(operation_type, str) or not operation_type:
            operation_type = "tool"
        policy_name = payload.get("policy_name")
        if not isinstance(policy_name, str) or not policy_name:
            policy_name = "native_permission"
        params = ElicitationRequestParams(
            mode="form",
            message=message,
            requestedSchema=None,
            url=None,
            phase="pre_tool_use",
            policy_name=policy_name,
            content_preview=content_preview,
        )
        result = await _publish_and_wait_for_harness_elicitation(
            request,
            session_id=session_id,
            params=params,
            timeout_s=_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
            conversation_store=conversation_store,
            elicitation_id=elicitation_id,
            tool_name=f"{agent}({operation_type})",
        )
        if result is None:
            return Response(status_code=status.HTTP_200_OK)
        if result.action == "decline":
            # Explicit user decline: interrupt the native harness before
            # returning the decline so the abort signal arrives first.
            await _forward_session_change_to_runner(
                session_id,
                get_server_runner_router(),
                {"type": "interrupt"},
            )
        return Response(
            content=json.dumps(result.model_dump(exclude_none=True)),
            media_type="application/json",
        )

    # ── GET /sessions/{session_id}/items ─────────────────────────

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
        responses={200: {"model": PaginatedList}},
    )
    async def list_session_items(
        request: Request,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> PaginatedList:
        """
        List items in a session with cursor-based pagination.

        Delegates to the conversation items store — session_id is
        the conversation_id. Same pagination contract as
        ``GET /v1/conversations/{id}/items``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of items to return
            (1-1000, default 100).
        :param after: Cursor — return items after this item ID,
            e.g. ``"msg_abc123"``.
        :param before: Cursor — return items before this item ID.
        :param order: Sort order, ``"asc"`` (chronological,
            default) or ``"desc"``.
        :returns: A :class:`PaginatedList` of conversation items.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        page = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [m.to_api_dict() for m in page.data]
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/child_sessions ────────────────

    @router.get(
        "/sessions/{session_id}/child_sessions",
        response_model=None,
        responses={200: {"model": ChildSessionList}},
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        tool: str | None = Query(default=None),
        session_name: str | None = Query(default=None),
    ) -> PaginatedList:
        """
        List sub-agent (child) sessions under a parent session.

        Returns a page of :class:`ChildSessionSummary` objects
        derived from child conversations (``kind="sub_agent"``,
        ``parent_conversation_id=session_id``) plus each child's
        latest task. Powers the web / REPL debug surfaces' "child
        sessions" panel without parsing parent
        ``function_call_output`` JSON handles. Pagination contract
        matches :func:`list_session_items` so existing client code
        can reuse the same cursor logic.

        :param request: Inbound HTTP request; carries the caller
            identity used to authorize READ on the parent session.
        :param session_id: Parent session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param limit: Maximum number of children to return
            (1-1000, default 20 — sub-agent fan-out is typically
            sparse compared to conversation items).
        :param after: Cursor — return children whose id appears
            after this one in sort order,
            e.g. ``"conv_child123"``.
        :param before: Cursor — return children before this one.
        :param order: Sort direction, ``"desc"`` (newest-first,
            default) or ``"asc"``. Sort column is ``created_at``.
        :param tool: When set, only return children whose title
            starts with this agent type (the segment before the
            ``":"``). Combined with ``session_name`` to form the
            exact title ``"{tool}:{session_name}"`` for server-side
            filtering.
        :param session_name: When set alongside ``tool``, only
            return children whose title matches
            ``"{tool}:{session_name}"`` exactly.
        :returns: A :class:`PaginatedList` of
            :class:`ChildSessionSummary` objects.
        :raises OmnigentError: 403 if the caller lacks READ on
            ``session_id``; 404 if no session exists there.
        """
        user_id = _get_user_id(request, auth_provider)
        # Require READ on the parent before listing its children (no cross-user enumeration).
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        parent = access.conversation
        if parent is None:
            parent = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if parent is None:
            raise _session_not_found()
        title_filter: str | None = None
        if tool and session_name:
            title_filter = f"{tool}:{session_name}"
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
            title=title_filter,
        )
        data = await _child_session_summaries_from_conversations(
            page.data,
            session_id,
            conversation_store,
        )
        return PaginatedList(
            data=data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    # ── GET /sessions/{session_id}/resources ─────────────────────

    @router.get(
        "/sessions/{session_id}/resources",
        response_model=SessionResourcePaginatedList,
        response_model_exclude_none=True,
    )
    async def list_session_resources(
        request: Request,
        session_id: str,
        # Shadows the ``type`` builtin deliberately: FastAPI maps the
        # parameter name to the wire query param, which is ``?type=``.
        type: str | None = Query(default=None),
    ) -> SessionResourcePaginatedList:
        """
        Return the runner-authoritative resource inventory for a session.

        Requires the session to be bound to a runner via
        ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise.
        The server validates the session exists, then proxies to the
        runner's ``GET /v1/sessions/{id}/resources`` endpoint. In
        unit-test / in-process setups with no runner router/client, the
        route falls back to adapting the local terminal registry.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param type: Optional resource-type filter, e.g.
            ``"environment"`` / ``"terminal"`` / ``"file"``. Forwarded
            to the runner (its registry applies it) and honored by the
            local-registry fallback and the file-store merge below.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        runner_client = await _get_runner_client_for_resource_access(session_id)
        if runner_client is not None:
            page = await _proxy_get_session_resources_to_runner(
                runner_client, session_id, resource_type=type
            )
        else:
            from omnigent.entities.session_resources import (
                list_session_resources_from_terminal_registry,
            )
            from omnigent.runtime import get_terminal_registry

            try:
                local_registry = get_terminal_registry()
            except RuntimeError:
                local_registry = None
            resource_page = list_session_resources_from_terminal_registry(
                session_id,
                local_registry,
            )
            # Mirror the runner's ``?type=`` semantics on the fallback so
            # both paths return the same shape for filtered queries.
            local_data = [
                SessionResourceObject.model_validate(
                    session_resource_view_to_dict(resource),
                )
                for resource in resource_page.data
                if type is None or resource.type == type
            ]
            page = SessionResourcePaginatedList(
                data=local_data,
                first_id=local_data[0].id if local_data else None,
                last_id=local_data[-1].id if local_data else None,
                has_more=resource_page.has_more,
            )

        # Files live in the server's file store, not on the runner, so a
        # ``type`` filter for non-file resources must skip the merge.
        if file_store is not None and type in (None, "file"):
            file_page = await asyncio.to_thread(
                file_store.list,
                session_id=session_id,
                limit=1000,
            )
            for stored in file_page.data:
                resource_dict = _stored_file_to_resource(
                    session_id,
                    stored,
                )
                page.data.append(
                    SessionResourceObject.model_validate(resource_dict),
                )
            if page.data:
                page.last_id = page.data[-1].id
                if not page.first_id:
                    page.first_id = page.data[0].id

        return page

    # ── Phase 1b: typed resource collections & terminal lifecycle ──

    async def _validate_session(
        session_id: str,
        request: Request | None = None,
        required_level: int = LEVEL_READ,
    ) -> Conversation:
        """Validate session existence and enforce permission checks.

        :param session_id: Session/conversation identifier.
        :param request: The incoming FastAPI request (for auth).
            When ``None``, permission checks are skipped (internal
            calls only).
        :param required_level: Minimum permission level needed.
        :returns: The matching conversation.
        :raises OmnigentError: 401/403/404 on auth or access failure.
        """
        if request is not None:
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id,
                session_id,
                required_level,
                permission_store,
                conversation_store,
            )
            # _require_access_and_level already fetched the conversation for
            # non-admin callers — reuse it to avoid a second DB round-trip.
            if access.conversation is not None:
                return access.conversation
        # Fallback: no-auth path, admin caller, or permissions disabled.
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        return conv

    async def _proxy_get_to_runner(
        session_id: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Proxy a GET request to the runner and return parsed JSON.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param params: Optional query params forwarded to the runner,
            e.g. ``{"order": "asc"}``. ``None`` sends no query string.
        :returns: Parsed JSON response body.
        :raises HTTPException: 502 on runner failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.get(path, params=params, timeout=10.0)
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        if resp.status_code == 404:
            raise OmnigentError(
                resp.json().get("error", {}).get("message", "Resource not found"),
                code=ErrorCode.NOT_FOUND,
            )
        if resp.status_code != 200:
            try:
                body = resp.json()
                error = body.get("error", {})
                msg = error.get("message") or "runner resource endpoint failed"
            except Exception:  # noqa: BLE001
                msg = "runner resource endpoint failed"
            raise HTTPException(status_code=502, detail=msg)
        return resp.json()

    async def _fs_get_with_host_fallback(
        session_id: str,
        *,
        op: str,
        host_params: dict[str, Any],
        runner_path: str,
        runner_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Serve a filesystem read, falling back to the host when offline.

        Proxies the read to the session's runner as usual. When the
        runner is offline (``RUNNER_UNAVAILABLE``) but the session's host
        is still connected, the read is served from the workspace over
        the host tunnel instead — the file panel stays live without
        waking the agent. The host runs
        :class:`omnigent.workspace_fs.WorkspaceReader` and returns the
        same JSON the runner would, so the response shape is identical.

        :param session_id: Session/conversation identifier.
        :param op: Host-side op name — ``"list_or_read"`` / ``"changes"``
            / ``"diff"`` / ``"search"``.
        :param host_params: Op-specific args for the host reader.
        :param runner_path: Runner-relative URL for the live path.
        :param runner_params: Optional query params for the runner path.
        :returns: The runner-shaped filesystem result.
        :raises OmnigentError: Re-raised runner-offline error when the
            host cannot serve the read either.
        :raises HTTPException: On host-reported filesystem failures.
        """
        try:
            return await _proxy_get_to_runner(session_id, runner_path, params=runner_params)
        except OmnigentError as exc:
            # Only the runner-offline case is a candidate for the host
            # fallback; a real 404 / git error from a live runner must
            # surface unchanged.
            if exc.code != ErrorCode.RUNNER_UNAVAILABLE:
                raise
            runner_offline = exc

        payload = await _read_workspace_via_host(session_id, op, host_params)
        if payload is None:
            # No reachable host either — surface the original offline
            # error (503) so the client shows its reconnect affordance.
            raise runner_offline
        return payload

    async def _read_workspace_via_host(
        session_id: str,
        op: str,
        host_params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read the session's workspace over its host tunnel.

        :param session_id: Session/conversation identifier.
        :param op: Host-side op name.
        :param host_params: Op-specific args for the host reader.
        :returns: The runner-shaped result, or ``None`` when no host is
            bound / connected / reachable (caller falls back to 503).
        :raises HTTPException: On host-reported filesystem failures,
            reproducing the runner's status.
        """
        from omnigent.server.routes._host_filesystem import (
            HostFsError,
            HostFsUnavailableError,
            read_workspace_from_host,
        )

        if host_registry is None:
            return None
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None or not conv.host_id or not conv.workspace:
            return None
        host_conn = host_registry.get(conv.host_id)
        if host_conn is None:
            return None
        try:
            return await read_workspace_from_host(
                host_registry=host_registry,
                host_conn=host_conn,
                op=op,
                workspace=conv.workspace,
                session_id=session_id,
                params=host_params,
            )
        except HostFsUnavailableError:
            return None
        except HostFsError as exc:
            if exc.status == 404:
                raise OmnigentError(exc.message, code=ErrorCode.NOT_FOUND) from exc
            if exc.status == 400:
                # Invalid path is a client error; surface it verbatim like the
                # runner's 400 rather than collapsing it to a 502.
                raise HTTPException(status_code=400, detail=exc.message) from exc
            # Any other host FS failure (e.g. git_status_failed 500) mirrors the
            # runner proxy, which wraps non-200/404 responses as a 502.
            raise HTTPException(status_code=502, detail=exc.message) from exc

    async def _proxy_post_to_runner(
        session_id: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a POST request to the runner and return status + JSON.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param body: JSON body to forward.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.post(
                path,
                json=body,
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    async def _proxy_delete_to_runner(
        session_id: str,
        path: str,
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a DELETE request to the runner and return status + JSON.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.delete(path, timeout=10.0)
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    async def _proxy_put_to_runner(
        session_id: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a PUT request to the runner.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param body: JSON body to forward.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.put(
                path,
                json=body,
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    async def _proxy_patch_to_runner(
        session_id: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a PATCH request to the runner.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param body: JSON body to forward.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.patch(
                path,
                json=body,
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    # Typed collection routes registered BEFORE /{resource_id} so
    # "environments", "terminals", "files" are not captured as ids.

    @router.get(
        "/sessions/{session_id}/resources/environments",
        response_model=None,
    )
    async def list_session_environments(
        request: Request,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Return only environment resources for a session.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :returns: ``PaginatedList`` of environment resources.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/environments"
        return await _proxy_get_to_runner(session_id, path)

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}",
        response_model=None,
    )
    async def get_session_environment(
        request: Request,
        session_id: str,
        environment_id: str,
    ) -> dict[str, Any]:
        """
        Return a single environment resource by id.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Opaque environment resource id,
            e.g. ``"default"``.
        :returns: The environment resource object.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}"
        try:
            return await _proxy_get_to_runner(session_id, path)
        except OmnigentError as exc:
            if exc.code != ErrorCode.RUNNER_UNAVAILABLE:
                raise
            # Runner offline but host-bound: synthesize the default
            # environment so the file panel (which gates on this metadata)
            # keeps browsing the host-served workspace at ``conv.workspace``.
            synthesized = await _synthesize_offline_environment(session_id, environment_id)
            if synthesized is None:
                raise
            return synthesized

    async def _synthesize_offline_environment(
        session_id: str,
        environment_id: str,
    ) -> dict[str, Any] | None:
        """Build a default-environment resource from the bound workspace.

        Used when the runner is offline but the session is host-bound, so
        the file panel's environment probe resolves and browsing can
        proceed against the host-served workspace.

        :param session_id: Session/conversation identifier.
        :param environment_id: Requested environment id; only the default
            environment is synthesized.
        :returns: A minimal environment resource dict with
            ``metadata.root`` set to the workspace path, or ``None`` when
            not applicable (non-default env, no host, no workspace).
        """
        if environment_id != "default" or host_registry is None:
            return None
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None or not conv.host_id or not conv.workspace:
            return None
        if host_registry.get(conv.host_id) is None:
            return None
        return {
            "id": environment_id,
            "object": "session.resource",
            "type": "environment",
            "metadata": {"root": conv.workspace},
        }

    @router.get(
        "/sessions/{session_id}/resources/terminals",
        response_model=None,
    )
    async def list_session_terminals(
        request: Request,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Return only terminal resources for a session.

        The runner endpoint's pagination params (``limit`` / ``after`` /
        ``before`` / ``order``) are forwarded from the incoming query
        string — without this, a client-requested ``order=asc`` (the web
        terminal tabs rely on creation order to keep the session's own
        terminal first) would be silently dropped and the runner's
        ``desc`` default would apply.

        :param request: The incoming FastAPI request (for auth and the
            forwarded query params).
        :param session_id: Session/conversation identifier.
        :returns: ``PaginatedList`` of terminal resources.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/terminals"
        forwarded = {
            key: value
            for key, value in request.query_params.items()
            if key in ("limit", "after", "before", "order")
        }
        return await _proxy_get_to_runner(session_id, path, params=forwarded or None)

    @router.post(
        "/sessions/{session_id}/resources/terminals",
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def create_session_terminal(
        session_id: str,
        request: Request,
    ) -> Any:
        """
        Launch or return an existing terminal resource.

        Preserves ``sys_terminal_launch`` idempotency: an
        already-running ``(terminal, session_key)`` returns the
        existing resource.

        User-initiated creates are gated on the agent's terminal
        access: the requested ``terminal`` must be one of the names
        declared in the agent spec's ``terminals:`` block. Native
        harness bootstrap requests (marked ``ensure_native_terminal``
        or ``bridge_inject_dir`` — the ``omnigent claude`` / ``codex``
        wrappers launching the session's own CLI terminal) are exempt:
        they launch undeclared names via the runner's
        synthesize-from-body path and predate the gate. The markers
        are client-controlled, so the exemption is narrowed to the
        exact shape those wrappers send — a registered native terminal
        name with ``session_key`` ``"main"`` — anything else carrying a
        marker still goes through the declared-name gate (it would
        otherwise be an arbitrary-terminal bypass).

        :param session_id: Session/conversation identifier.
        :param request: JSON body with ``terminal`` and
            ``session_key``.
        :returns: The terminal resource object.
        :raises OmnigentError: 400 when the requested terminal is not
            declared by the agent spec (or the agent has no
            ``terminals:`` block at all).
        """
        conv = await _validate_session(session_id, request, LEVEL_EDIT)
        body = await request.json()
        is_native_bootstrap = (
            bool(body.get("ensure_native_terminal") or body.get("bridge_inject_dir"))
            and native_coding_agent_for_terminal_name(body.get("terminal")) is not None
            and body.get("session_key") == "main"
        )
        if not is_native_bootstrap:
            spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
            declared = list(spec.terminals or {}) if spec is not None else []
            if body.get("terminal") not in declared:
                raise OmnigentError(
                    (
                        f"Terminal {body.get('terminal')!r} is not declared by this "
                        f"agent. Terminals can only be created for agents whose spec "
                        f"declares them; this agent declares: {declared or 'none'}."
                    ),
                    code=ErrorCode.INVALID_INPUT,
                )
        path = f"/v1/sessions/{session_id}/resources/terminals"
        status, payload = await _proxy_post_to_runner(
            session_id,
            path,
            body,
        )
        if status >= 400:
            error = payload.get("error", {})
            # OmnigentError derives http_status from code; pass the runner's code, not a status.
            raise OmnigentError(
                error.get("message", f"Terminal launch failed (runner returned HTTP {status})"),
                code=error.get("code", ErrorCode.INTERNAL_ERROR),
            )
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.created",
            resource_id=payload.get("id", ""),
            resource_type="terminal",
            conversation_store=conversation_store,
            resource=payload,
        )
        return payload

    @router.get(
        "/sessions/{session_id}/resources/terminals/{terminal_id}",
        response_model=None,
    )
    async def get_session_terminal(
        request: Request,
        session_id: str,
        terminal_id: str,
    ) -> dict[str, Any]:
        """
        Return a single terminal resource by id.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: The terminal resource object.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
        return await _proxy_get_to_runner(session_id, path)

    @router.post(
        "/sessions/{session_id}/resources/terminals/{terminal_id}/transfer",
        # Internal terminal transfer — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def transfer_session_terminal(
        request: Request,
        session_id: str,
        terminal_id: str,
    ) -> Any:
        """
        Move a terminal resource to another session without closing it.

        Used by native Claude ``/clear`` rotation: ownership changes
        from the previous conversation to the fresh one while the tmux
        pane keeps running.

        :param request: The incoming FastAPI request (for auth) with
            JSON body ``{"target_session_id": "conv_new"}``.
        :param session_id: Current owning session/conversation id,
            e.g. ``"conv_old"``.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_claude_main"``.
        :returns: The terminal resource object under the target session.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        body = await request.json()
        target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
        if not isinstance(target_session_id, str) or not target_session_id:
            raise OmnigentError(
                "'target_session_id' is required",
                code=ErrorCode.INVALID_INPUT,
            )
        await _validate_session(target_session_id, request, LEVEL_EDIT)

        path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer"
        status, payload = await _proxy_post_to_runner(
            session_id,
            path,
            {"target_session_id": target_session_id},
        )
        if status == 404:
            error = payload.get("error", {})
            raise OmnigentError(
                error.get("message", "Terminal not found"),
                code=ErrorCode.NOT_FOUND,
            )
        if status == 409:
            error = payload.get("error", {})
            raise OmnigentError(
                error.get("message", "Terminal transfer conflict"),
                code=ErrorCode.INVALID_INPUT,
            )
        if status >= 400:
            error = payload.get("error", {})
            # OmnigentError derives http_status from code; pass the runner's code, not a status.
            raise OmnigentError(
                error.get("message", "Terminal transfer failed"),
                code=error.get("code", ErrorCode.INTERNAL_ERROR),
            )

        _publish_and_persist_resource_event(
            session_id,
            "session.resource.deleted",
            resource_id=terminal_id,
            resource_type="terminal",
            conversation_store=conversation_store,
        )
        _publish_and_persist_resource_event(
            target_session_id,
            "session.resource.created",
            resource_id=payload.get("id", ""),
            resource_type="terminal",
            conversation_store=conversation_store,
            resource=payload,
        )
        return payload

    @router.delete(
        "/sessions/{session_id}/resources/terminals/{terminal_id}",
        response_model=None,
    )
    async def delete_session_terminal(
        request: Request,
        session_id: str,
        terminal_id: str,
    ) -> Any:
        """
        Close a terminal resource.

        Delegates to ``TerminalRegistry.close()`` on the runner.
        Returns 404 for unknown terminals.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: Deletion confirmation object.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
        status, payload = await _proxy_delete_to_runner(
            session_id,
            path,
        )
        if status == 404:
            error = payload.get("error", {})
            raise OmnigentError(
                error.get("message", "Terminal not found"),
                code=ErrorCode.NOT_FOUND,
            )
        if status >= 400:
            raise HTTPException(
                status_code=502,
                detail="runner terminal delete failed",
            )
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.deleted",
            resource_id=terminal_id,
            resource_type="terminal",
            conversation_store=conversation_store,
        )
        return payload

    # ── Phase 1c: session-scoped file endpoints ────────────────────

    @router.get(
        "/sessions/{session_id}/resources/files",
        response_model=None,
    )
    async def list_session_files(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        """
        List files owned by a session.

        :param session_id: Session/conversation identifier.
        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :returns: ``PaginatedList`` of session file resources.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        if file_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        page = file_store.list(
            session_id=session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [_stored_file_to_resource(session_id, f) for f in page.data]
        return {
            "object": "list",
            "data": data,
            "first_id": page.first_id,
            "last_id": page.last_id,
            "has_more": page.has_more,
        }

    @router.post(
        "/sessions/{session_id}/resources/files",
        status_code=201,
        response_model=None,
        # CSRF hardening: this route only accepts multipart/form-data, which
        # is CORS-safelisted, so a content-type guard can't stop a cross-site
        # upload. require_trusted_origin closes the gap (allows absent Origin
        # for the non-browser SDK/runner clients; in local mode a present
        # Origin must be loopback).
        dependencies=[Depends(require_trusted_origin)],
    )
    async def upload_session_file(
        request: Request,
        session_id: str,
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, Any]:
        """
        Upload a file into the session file namespace.

        Accepts the multipart upload shape used by session file resources.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param file: The uploaded file (multipart form data).
        :returns: The session file resource object.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        if not file.filename:
            raise OmnigentError(
                "filename is required",
                code=ErrorCode.INVALID_INPUT,
            )
        from omnigent.runtime.content_resolver import (
            MAX_ATTACHMENT_UPLOAD_BYTES,
            _resolve_content_type,
            attachment_text_type_for_extension,
            attachment_upload_limit,
        )

        # Resolve the type from the declared MIME + filename BEFORE reading
        # the body, so an unsupported or oversized upload is rejected without
        # buffering it. Attachments are inlined into the model context as
        # base64 (see content_resolver.resolve_content_references); only
        # images, PDF, and text/code files are usable — others (pptx, docx,
        # zip, …) would be garbled or blow the request size, so reject them.
        content_type = _resolve_content_type(
            file.content_type,
            file.filename,
        )
        type_limit = attachment_upload_limit(content_type)
        if type_limit is None:
            # The browser/OS can mislabel a text/code file as binary (e.g. a
            # .csv reported as application/vnd.ms-excel on Windows). Fall back
            # to the extension — matching the web client's allowlist — and
            # normalize the type so the resolver inlines it as text.
            ext_type = attachment_text_type_for_extension(file.filename)
            if ext_type is not None:
                content_type = ext_type
                type_limit = attachment_upload_limit(content_type)
        if type_limit is None:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Unsupported attachment type '{content_type}'. Only images, "
                    "PDF, and text/code files can be attached."
                ),
            )
        content = await _read_upload_capped(
            file,
            min(type_limit, MAX_ATTACHMENT_UPLOAD_BYTES),
        )
        stored = file_store.create(
            session_id=session_id,
            filename=file.filename,
            bytes=len(content),
            content_type=content_type,
        )
        artifact_store.put(stored.id, content)
        resource = _stored_file_to_resource(session_id, stored)
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.created",
            resource_id=stored.id,
            resource_type="file",
            conversation_store=conversation_store,
            resource=resource,
        )
        return resource

    @router.get(
        "/sessions/{session_id}/resources/files/{file_id}",
        response_model=None,
    )
    async def get_session_file(
        request: Request,
        session_id: str,
        file_id: str,
    ) -> dict[str, Any]:
        """
        Retrieve metadata for a session file resource.

        Verifies that ``file_id`` belongs to ``session_id``.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param file_id: Unique file identifier.
        :returns: The session file resource object.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        if file_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        stored = file_store.get(file_id, session_id=session_id)
        if stored is None:
            raise OmnigentError(
                "File not found",
                code=ErrorCode.NOT_FOUND,
            )
        return _stored_file_to_resource(session_id, stored)

    @router.get(
        "/sessions/{session_id}/resources/files/{file_id}/content",
        response_model=None,
    )
    async def get_session_file_content(
        request: Request,
        session_id: str,
        file_id: str,
    ) -> Response:
        """
        Download raw content of a session file resource.

        :param session_id: Session/conversation identifier.
        :param file_id: Unique file identifier.
        :returns: Response with file bytes and Content-Type.
        """

        await _validate_session(session_id, request, LEVEL_READ)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        stored = file_store.get(file_id, session_id=session_id)
        if stored is None:
            raise OmnigentError(
                "File not found",
                code=ErrorCode.NOT_FOUND,
            )
        content = artifact_store.get(stored.id)
        media_type = mimetypes.guess_type(stored.filename)[0] or "application/octet-stream"
        # The filename and bytes are fully user-controlled. Serving the
        # content inline lets a browser navigating directly to this URL
        # render an uploaded ``evil.html`` as ``text/html`` and execute
        # its script in the server's own origin (stored XSS — acute on
        # the OSS/local server, which has no CSRF/apiproxy boundary).
        # Force a download with ``Content-Disposition: attachment`` and
        # disable MIME sniffing so the response cannot be reinterpreted
        # as an active type.
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": _attachment_disposition(stored.filename),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete(
        "/sessions/{session_id}/resources/files/{file_id}",
        response_model=None,
    )
    async def delete_session_file(
        request: Request,
        session_id: str,
        file_id: str,
    ) -> dict[str, Any]:
        """
        Delete a session file resource and its artifact bytes.

        :param session_id: Session/conversation identifier.
        :param file_id: Unique file identifier.
        :returns: Deletion confirmation object.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        if not file_store.delete(file_id, session_id=session_id):
            raise OmnigentError(
                "File not found",
                code=ErrorCode.NOT_FOUND,
            )
        artifact_store.delete(file_id)
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.deleted",
            resource_id=file_id,
            resource_type="file",
            conversation_store=conversation_store,
        )
        return {
            "id": file_id,
            "object": "session.resource.deleted",
            "deleted": True,
        }

    @router.post(
        "/sessions/{session_id}/resources/files:copy",
        response_model=None,
    )
    async def copy_session_files(
        request: Request,
        session_id: str,
        body: CopyFilesRequest,
    ) -> dict[str, Any]:
        """
        Copy lineage-owned files into this (destination) session.

        Authorizes by spawn lineage: ``body.source_session_id`` must be a
        STRICT ancestor of this session up the ``parent_conversation_id``
        chain — the session may not name itself as the source. Each source
        file is read and re-stored as a new child-scoped row owned by
        ``session_id`` — this preserves the session-scoping invariant (the
        child reads its OWN copy; no cross-session read grant is created).
        Validation is all-or-nothing: an unauthorized source, a missing
        file, or a request past the copy limits copies nothing.

        The request is bounded before any blob is read: the file count and
        the summed ``StoredFile.bytes`` are checked against the copy limits
        during metadata validation, so an over-limit request is rejected
        without buffering a single blob. Within the limits, files are copied
        one at a time (read → create → put) so peak memory is a single blob,
        not the whole batch.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Destination (child) session/conversation id.
        :param body: Source session id plus the file ids to copy.
        :returns: A ``session.files.copied`` object carrying the
            ``{source_file_id: new_file_id}`` mapping.
        """
        from omnigent.server.server_config import (
            copy_file_count_limit,
            copy_total_bytes_limit,
        )

        await _validate_session(session_id, request, LEVEL_EDIT)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )

        # Lineage authorization: the source must be a STRICT ancestor up
        # the parent_conversation_id chain. A session may not name itself
        # as the source — the contract is "copy files down from a parent",
        # and a top-level session has no lineage to copy from.
        if body.source_session_id not in set(
            _ancestor_session_ids(conversation_store, session_id)
        ):
            raise OmnigentError(
                "Source session is not an ancestor of this session",
                code=ErrorCode.FORBIDDEN,
            )

        # Validate every source file WITHOUT reading a blob, enforcing the copy
        # limits before any blob is read. Summing StoredFile.bytes here means
        # an over-count or over-size request is rejected without buffering a
        # single blob — a rejected request never spikes memory. artifact_store
        # .exists() is a cheap metadata probe (S3 HEAD / local stat / DB row),
        # NOT a blob read, so checking it here preserves the original
        # "missing blob surfaces before any child row is created" guarantee
        # without reintroducing the batch prefetch. The blobs themselves are
        # fetched one at a time in the write loop below.
        max_files = copy_file_count_limit()
        max_total_bytes = copy_total_bytes_limit()
        if len(body.file_ids) > max_files:
            raise OmnigentError(
                f"Cannot copy {len(body.file_ids)} files: limit is {max_files}",
                code=ErrorCode.INVALID_INPUT,
            )
        if len(set(body.file_ids)) != len(body.file_ids):
            raise OmnigentError(
                "file_ids must not contain duplicates",
                code=ErrorCode.INVALID_INPUT,
            )
        sources: list[StoredFile] = []
        total_bytes = 0
        for file_id in body.file_ids:
            stored = file_store.get(file_id, session_id=body.source_session_id)
            if stored is None or not artifact_store.exists(stored.id):
                raise OmnigentError(
                    f"File '{file_id}' not found in source session",
                    code=ErrorCode.NOT_FOUND,
                )
            total_bytes += stored.bytes
            if total_bytes > max_total_bytes:
                raise OmnigentError(
                    f"Cannot copy files: total size exceeds limit of {max_total_bytes} bytes",
                    code=ErrorCode.INVALID_INPUT,
                )
            sources.append(stored)

        # Commit the copies one file at a time (read → create → put) so peak
        # memory is a single blob, not the whole batch. If any step fails
        # mid-batch, roll back the rows/blobs already created.
        mapping: dict[str, CopiedFile] = {}
        created: list[str] = []
        copied: list[StoredFile] = []
        try:
            for stored in sources:
                content = artifact_store.get(stored.id)
                new = file_store.create(
                    session_id=session_id,
                    filename=stored.filename,
                    bytes=stored.bytes,
                    content_type=stored.content_type,
                )
                created.append(new.id)
                artifact_store.put(new.id, content)
                # Carry the preserved filename + content_type back so the
                # caller can attach the copy without a follow-up metadata GET.
                mapping[stored.id] = CopiedFile(
                    new_id=new.id,
                    filename=new.filename,
                    content_type=new.content_type,
                )
                copied.append(new)
        except Exception as exc:
            for new_id in created:
                try:
                    file_store.delete(new_id, session_id=session_id)
                except Exception:  # noqa: BLE001 - rollback cleanup is best effort.
                    _logger.warning(
                        "Failed to delete copied file row during rollback: session=%s file_id=%s",
                        session_id,
                        new_id,
                        exc_info=True,
                    )
                try:
                    artifact_store.delete(new_id)
                except Exception:  # noqa: BLE001 - rollback cleanup is best effort.
                    _logger.warning(
                        "Failed to delete copied file blob during rollback: session=%s file_id=%s",
                        session_id,
                        new_id,
                        exc_info=True,
                    )
            raise OmnigentError(
                "Failed to copy files into destination session",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc

        # Resource events fire only after every write lands. Publishing them
        # inside the copy loop would emit (and persist as transcript items)
        # ``session.resource.created`` for early files, then a later write
        # failure would roll back the file rows/blobs without compensating
        # those events — clients would see phantom files that no longer
        # exist. Keep the create + event all-or-nothing together.
        for new in copied:
            _publish_and_persist_resource_event(
                session_id,
                "session.resource.created",
                resource_id=new.id,
                resource_type="file",
                conversation_store=conversation_store,
                resource=_stored_file_to_resource(session_id, new),
            )

        return CopyFilesResponse(
            session_id=session_id,
            mapping=mapping,
        ).model_dump()

    # ── Phase 3: environment filesystem proxy endpoints ──────────

    async def _proxy_fs_response(
        session_id: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        request: Request | None = None,
        required_level: int = LEVEL_EDIT,
        environment_id: str = "default",
        publish_invalidation: bool = True,
    ) -> Any:
        """Proxy a filesystem request to the runner.

        Translates runner error status codes into appropriate
        API-level exceptions.

        :param session_id: Session/conversation identifier.
        :param method: HTTP method.
        :param path: Runner-relative URL path.
        :param body: Optional JSON body.
        :param request: The incoming FastAPI request (for auth).
        :param required_level: Minimum permission level needed.
        :param environment_id: Environment resource id,
            e.g. ``"default"``. Used for the live invalidation event
            after successful mutating filesystem operations.
        :param publish_invalidation: Whether a successful proxied
            mutation should publish ``session.changed_files.invalidated``.
            False for generic shell commands because read-only commands
            are common and cannot be distinguished cheaply here.
        :returns: Parsed JSON response.
        """
        await _validate_session(session_id, request, required_level)
        if method == "GET":
            return await _proxy_get_to_runner(session_id, path)
        if method == "PUT":
            status, payload = await _proxy_put_to_runner(
                session_id,
                path,
                body or {},
            )
        elif method == "PATCH":
            status, payload = await _proxy_patch_to_runner(
                session_id,
                path,
                body or {},
            )
        elif method == "POST":
            status, payload = await _proxy_post_to_runner(
                session_id,
                path,
                body or {},
            )
        elif method == "DELETE":
            status, payload = await _proxy_delete_to_runner(
                session_id,
                path,
            )
        else:
            raise HTTPException(status_code=405)

        if status >= 400:
            error = payload.get("error", {})
            message = error.get("message", "filesystem operation failed")
            if status == 404:
                raise OmnigentError(message, code=ErrorCode.NOT_FOUND)
            raise HTTPException(status_code=status, detail=message)
        if publish_invalidation:
            _publish_changed_files_invalidated(session_id, environment_id)
        return payload

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/filesystem",
        response_model=None,
    )
    async def list_environment_root(
        request: Request,
        session_id: str,
        environment_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> Any:
        """
        List root directory of an environment.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param limit: Maximum number of entries to return (1-1000, default 20).
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: PaginatedList of filesystem entries.
        """
        params: dict[str, str] = {"limit": str(limit), "order": order}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        qs = urllib.parse.urlencode(params)
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem?{qs}"
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="list_or_read",
            host_params={
                "path": "",
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
            },
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/search",
        response_model=None,
    )
    async def search_environment_files(
        request: Request,
        session_id: str,
        environment_id: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> Any:
        """
        Search for files recursively by name/path substring and glob filters.

        Proxies to the runner's search endpoint.  Returns a flat list of
        matching file entries (not directories) whose name or relative path
        contains ``q`` (case-insensitive), optionally scoped by ``include`` /
        ``exclude`` globs.  Requires at least one non-whitespace character in
        ``q`` to prevent accidental full-tree scans.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :param q: Case-insensitive search substring, e.g. ``"test.md"``.
            Must contain at least one non-whitespace character.
        :param include: Comma-separated glob patterns scoping which files are
            returned, e.g. ``"*.ts,src/**"``.
        :param exclude: Comma-separated glob patterns for files to drop,
            e.g. ``"**/node_modules,*.test.ts"``.
        :param limit: Maximum number of results (1-500, default 500).
        :returns: JSON list response with matching filesystem entries.
        """
        params: dict[str, str] = {"q": q, "limit": str(limit)}
        if include is not None:
            params["include"] = include
        if exclude is not None:
            params["exclude"] = exclude
        qs = urllib.parse.urlencode(params)
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/search?{qs}"
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="search",
            host_params={"q": q, "include": include, "exclude": exclude, "limit": limit},
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/changes",
        response_model=None,
    )
    async def list_environment_filesystem_changes(
        request: Request,
        session_id: str,
        environment_id: str,
    ) -> Any:
        """
        List all files changed since session start (flat, registry-backed).

        Returns the watchdog change set for the session — every file
        created, modified, or deleted since the session began, regardless
        of directory depth.  Use for the flat "changed files" view.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :returns: Flat list of changed filesystem entries with ``status``.
        """
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/changes"
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="changes",
            host_params={},
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/diff/{relative_path:path}",
        # Internal (UI diff view) — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def read_environment_file_diff(
        request: Request,
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> Any:
        """
        Return before/after diff content for a changed file.

        Proxies to the runner's diff endpoint and returns before/after
        content strings so the UI can render a diff view.  Returns 404 when
        the file has not been modified this session.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :returns: JSON with ``before`` and ``after`` content strings.
        """
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/diff/{relative_path}"
        )
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="diff",
            host_params={"path": relative_path},
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def read_or_list_environment_path(
        request: Request,
        session_id: str,
        environment_id: str,
        relative_path: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> Any:
        """
        Read a file or list a directory in an environment.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param limit: Maximum number of entries to return for directory
            listings (1-1000, default 20). Ignored for file reads.
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: File content or directory listing.
        """
        params: dict[str, str] = {"limit": str(limit), "order": order}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        qs = urllib.parse.urlencode(params)
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}?{qs}"
        )
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="list_or_read",
            host_params={
                "path": relative_path,
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
            },
            runner_path=path,
        )

    @router.put(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def write_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> Any:
        """
        Write/replace a file in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``content``.
        :returns: Write result.
        """
        body = await request.json()
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}"
        )
        return await _proxy_fs_response(
            session_id,
            "PUT",
            path,
            body,
            request=request,
            environment_id=environment_id,
        )

    @router.patch(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def edit_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> Any:
        """
        Edit a file in an environment via text replacement.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``old_text`` and ``new_text``.
        :returns: Edit result.
        """
        body = await request.json()
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}"
        )
        return await _proxy_fs_response(
            session_id,
            "PATCH",
            path,
            body,
            request=request,
            environment_id=environment_id,
        )

    @router.delete(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def delete_environment_path(
        request: Request,
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> Any:
        """
        Delete a file or directory in an environment.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :returns: Delete result.
        """
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}"
        )
        return await _proxy_fs_response(
            session_id,
            "DELETE",
            path,
            request=request,
            environment_id=environment_id,
        )

    # ── Phase 5: environment shell proxy ─────────────────────────

    @router.post(
        "/sessions/{session_id}/resources/environments/{environment_id}/shell",
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def run_environment_shell(
        session_id: str,
        environment_id: str,
        request: Request,
    ) -> Any:
        """
        Execute a shell command in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param request: JSON body with ``command`` and optional
            ``timeout``.
        :returns: Shell result.
        """
        body = await request.json()
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/shell"
        return await _proxy_fs_response(
            session_id,
            "POST",
            path,
            body,
            request=request,
            environment_id=environment_id,
            publish_invalidation=False,
        )

    # Generic single-resource lookup — registered AFTER typed
    # collections so "environments", "terminals", "files" are not
    # captured as resource_id.

    @router.get(
        "/sessions/{session_id}/resources/{resource_id}",
        response_model=None,
    )
    async def get_session_resource(
        request: Request,
        session_id: str,
        resource_id: str,
    ) -> dict[str, Any]:
        """
        Return a single resource by id from the unified inventory.

        :param session_id: Session/conversation identifier.
        :param resource_id: Opaque resource id.
        :returns: The resource object regardless of type.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/{resource_id}"
        return await _proxy_get_to_runner(session_id, path)

    # ── Embedded-browser action bridge ───────────────────────────

    @router.post(
        "/sessions/{session_id}/browser/action_request",
        # Internal embedded-browser flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def browser_action_request(
        request: Request,
        session_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Park one embedded-browser action and await the renderer result.

        Mints an ``action_id``, parks a Future owned by ``session_id``, publishes
        a ``browser.action_request`` event, and awaits up to
        ``_BROWSER_ACTION_AWAIT_S``; on timeout returns the timeout result (HTTP
        200) so the runner gets a clean tool error. Called by the runner's
        ``browser_*`` dispatch, not the LLM.

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param body: ``{"action": <str>, "args": <dict>}`` where ``action``
            is the ``browser_`` tool name minus the prefix.
        :returns: The renderer's action-result JSON, or the timeout result.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        action = body.get("action")
        args = body.get("args")
        if not isinstance(action, str) or not action:
            raise OmnigentError(
                "browser action_request requires a non-empty 'action'",
                code=ErrorCode.INVALID_INPUT,
            )
        if not isinstance(args, dict):
            args = {}

        action_id = f"baction_{secrets.token_hex(16)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        _browser_action_registry[action_id] = future
        _browser_action_owners[action_id] = session_id
        try:
            event = BrowserActionRequestEvent(
                type="browser.action_request",
                action_id=action_id,
                action=action,
                args=args,
            )
            session_stream.publish(session_id, event.model_dump())
            done, _pending = await asyncio.wait(
                {future},
                timeout=_BROWSER_ACTION_AWAIT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if future in done and not future.cancelled():
                return future.result()
            # Timed out/cancelled with no renderer result (no subscribed app).
            return _BROWSER_ACTION_TIMEOUT_RESULT
        finally:
            # Drop registry entries so a resolved/timed-out action leaks nothing.
            if _browser_action_registry.get(action_id) is future:
                _browser_action_registry.pop(action_id, None)
            _browser_action_owners.pop(action_id, None)
            _browser_action_claims.pop(action_id, None)

    @router.post(
        "/sessions/{session_id}/browser/action_claim/{action_id}",
        # Internal embedded-browser flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def browser_action_claim(
        request: Request,
        session_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        """
        Atomically claim a parked browser action (one winner per action).

        The request event fans out to every subscribed renderer; an atomic
        ``setdefault`` grants exactly one claim so they don't double-execute.
        Winner gets ``{"claimed": true, "claim_token": <token>}``; everyone
        else ``{"claimed": false}``.

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param action_id: The action to claim, e.g. ``"baction_abc123"``.
        :returns: ``{"claimed": true, "claim_token": <str>}`` to the winner,
            ``{"claimed": false}`` to losers or for an unknown/expired action.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        # Unknown / already-resolved action: nothing to claim.
        if _browser_action_owners.get(action_id) != session_id:
            return {"claimed": False}
        # Single-winner lease via atomic setdefault: a losing racer sees the
        # winner's token, not its own, and bails.
        claim_token = secrets.token_hex(16)
        existing = _browser_action_claims.setdefault(action_id, claim_token)
        if existing != claim_token:
            return {"claimed": False}
        return {"claimed": True, "claim_token": claim_token}

    @router.post(
        "/sessions/{session_id}/browser/action_result/{action_id}",
        # Internal embedded-browser flow — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        response_model=None,
    )
    async def browser_action_result(
        request: Request,
        session_id: str,
        action_id: str,
        body: dict[str, Any],
    ) -> dict[str, bool]:
        """
        Deliver a browser action result, resolving the parked Future.

        Guarded by owner + claim-token: the caller must present the token this
        action was leased under, so a renderer that lost the claim race can't
        resolve the Future with stale work (tokenless/mismatched → 403).

        :param request: The inbound request, used for identity extraction.
        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param action_id: The action being resolved, e.g. ``"baction_abc"``.
        :param body: ``{"result": <dict>, "claim_token": <str>}``.
        :returns: ``{"resolved": true}`` when the Future was set,
            ``{"resolved": false}`` when it was already done/gone.
        :raises OmnigentError: 404 if no session exists; 403 on a missing or
            mismatched claim token or an owner mismatch.
        """
        user_id = _get_user_id(request, auth_provider)
        await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        claim_token = body.get("claim_token")
        expected = _browser_action_claims.get(action_id)
        if not isinstance(claim_token, str) or expected is None or claim_token != expected:
            raise OmnigentError(
                "browser action result requires a matching claim_token",
                code=ErrorCode.FORBIDDEN,
            )
        # Only the session that issued the action may resolve it.
        if _browser_action_owners.get(action_id) != session_id:
            raise OmnigentError(
                "browser action is not owned by this session",
                code=ErrorCode.FORBIDDEN,
            )
        future = _browser_action_registry.get(action_id)
        if future is None or future.done():
            return {"resolved": False}
        result = body.get("result")
        future.set_result(result if isinstance(result, dict) else {"result": result})
        return {"resolved": True}

    # ── POST /sessions/{session_id}/events ───────────────────────

    @router.post(
        "/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
        # Internal elicitation flow — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        # response_model=None: the body is a small acknowledgement
        # dict, not a domain model.
        response_model=None,
    )
    async def resolve_elicitation(
        request: Request,
        session_id: str,
        elicitation_id: str,
        body: ElicitationResult,
    ) -> dict[str, bool]:
        """
        Resolve an outstanding elicitation by its URL (URL-based
        elicitation).

        The dedicated, RESTful counterpart to delivering a verdict
        via the ``type == "approval"`` event on
        ``POST /v1/sessions/{id}/events``. An elicitation request
        published in ``mode == "url"`` carries this endpoint's path
        as its ``params.url``; the client hits it directly with the
        MCP :class:`ElicitationResult` body instead of POSTing a
        generic approval event. The verdict routes through the
        shared :func:`_resolve_elicitation`, so resolution semantics
        are identical to the event path.

        The ``elicitation_id`` is taken from the URL rather than the
        body, so the unguessable id (``secrets.token_hex(16)``) is
        the capability scoping the resolution — combined with the
        session-owner ``LEVEL_EDIT`` gate below and the server-side
        ownership check inside :func:`_resolve_elicitation`.

        :param request: The inbound request, used for identity
            extraction.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation to
            resolve, e.g. ``"elicit_abc123"``. Taken from the URL
            path, not the body.
        :param body: The MCP-shaped verdict — ``action``
            (``"accept"`` / ``"decline"`` / ``"cancel"``) plus
            optional form ``content``.
        :returns: ``{"queued": False}`` — resolution is synchronous
            and persists no conversation item.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        _resolve_data = {"elicitation_id": elicitation_id, **body.model_dump(exclude_none=True)}
        await _resolve_elicitation(session_id, _resolve_data, runner_router, conversation_store)
        # Apply any policy writes deferred by the relay tool-call ASK gate
        # (e.g. a cost-budget checkpoint) now that the verdict is in.
        await _apply_pending_policy_ask_writes(
            session_id, conv, conversation_store, agent_store, _resolve_data
        )
        return {"queued": False}

    @router.get(
        "/sessions/{session_id}/elicitations/{elicitation_id}",
        # Internal elicitation flow — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def get_elicitation(
        request: Request,
        session_id: str,
        elicitation_id: str,
    ) -> dict[str, Any]:
        """
        Return the state of a pending elicitation as JSON.

        Used by the frontend's standalone approval page
        (``/approve/:sessionId/:elicitationId``) to fetch the
        elicitation prompt and render approve/reject controls.
        The payload is read from the in-memory
        :mod:`omnigent.runtime.pending_elicitations` index — no
        database persistence required.

        :param request: The inbound request, used for identity
            extraction.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param elicitation_id: Correlation id of the elicitation,
            e.g. ``"elicit_abc123"``.
        :returns: JSON with ``status`` (``"pending"`` or
            ``"resolved"``), and when pending: ``message``,
            ``phase``, ``policy_name``, ``content_preview``.
        :raises OmnigentError: 404 if the session does not exist.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()

        found = pending_elicitations.lookup(elicitation_id)
        if found is None or found[0] != session_id:
            return {"status": "resolved"}

        _conv_id, event = found
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        return {
            "status": "pending",
            "message": params.get("message", "Approval required"),
            "phase": params.get("phase", ""),
            "policy_name": params.get("policy_name", ""),
            "content_preview": params.get("content_preview", ""),
        }

    @router.post(
        "/sessions/{session_id}/events",
        # Internal event ingestion — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        # response_model=None: the body is a small acknowledgement
        # dict, not a domain model.
        response_model=None,
    )
    async def post_event(
        request: Request,
        session_id: str,
        body: SessionEventInput,
    ) -> dict[str, bool | str]:
        """
        Submit a session event (input message, tool output,
        approval, or interrupt).

        Dispatches on ``body.type``:

        - ``"interrupt"`` cancels any active task and publishes a
          ``session.interrupted`` event. Bypasses item persistence.
        - ``"approval"`` resolves an outstanding elicitation
          in-band (see :func:`_dispatch_approval`).
        - ``"external_assistant_message"`` appends and streams an
          assistant message observed outside the Omnigent task runtime,
          without starting or steering a task.
        - ``"external_conversation_item"`` appends and streams a
          completed item observed outside the Omnigent task runtime,
          without starting or steering a task.
        - ``"external_output_text_delta"`` publishes a transient
          ``response.output_text.delta`` event observed outside the
          Omnigent task runtime, without persisting an item or starting /
          steering a task.
        - ``"external_tool_output_delta"`` publishes transient output for
          an in-progress function call without persisting an item.
        - ``"external_output_reasoning_delta"`` publishes a transient
          ``response.reasoning_text.delta`` event (preceded by one
          ``response.reasoning.started`` when ``data.started`` is true)
          observed outside the Omnigent task runtime, without persisting an
          item or starting / steering a task.
        - ``"external_session_interrupted"`` publishes a
          ``session.interrupted`` event observed outside the Omnigent task
          runtime, without persisting an item or starting / steering a
          task.
        - ``"external_elicitation_resolved"`` marks a native
          harness-originated elicitation as resolved elsewhere so
          subscribed clients clear the pending approval card.
        - ``"external_session_status"`` publishes a terminal-observed
          ``session.status`` edge without persisting an item or
          starting/steering a task.
        - ``"external_model_change"`` persists a terminal-observed
          model switch to ``model_override`` and publishes a
          ``session.model`` SSE event so the web picker reflects it.
        - ``"external_model_options"`` records the model catalog a native
          harness's extension reported (its live model registry) into a
          reload-surviving cache and publishes ``session.model_options`` so
          the web picker populates regardless of how the harness authenticated.
        - ``"external_reasoning_effort_change"`` persists a terminal-observed
          thinking-level switch to ``reasoning_effort`` and publishes a
          ``session.reasoning_effort`` SSE event so the web picker reflects it.
        - ``"external_codex_collaboration_mode_change"`` persists the
          Codex app-server collaboration mode kind as an internal session label
          (``omnigent.codex_native.collaboration_mode``).
        - ``"stop_session"`` terminates the live session without
          deleting the conversation (owner-only). Forwarded
          harness-agnostically to the runner, which hard-kills the
          external process for harnesses that have one (claude-native
          kills its tmux pane) and 204s otherwise. Stop is non-sticky:
          it writes no persistent marker, so the next message
          auto-relaunches the session on its (still-online) host via
          the normal message-dispatch relaunch path.
        - ``"message"`` on an ``omnigent claude`` terminal session
          is forwarded to the bound runner for tmux injection only;
          the accepted prompt is persisted later when Claude records
          it in the terminal transcript.
        - Any other (item-typed) event is persisted into
          ``conversation_items`` via the legacy create-or-steer path
          (legacy persist path): if an active
          task is present, the item is delivered into its inbox;
          otherwise a new task is created and started. In both
          cases ``session.input.consumed`` fires with the persisted
          item's id.

        :param session_id: Session/conversation identifier.
        :param body: The validated :class:`SessionEventInput`.
        :returns: ``{"queued": True, "item_id": "..."}`` for
            item-typed events, where ``item_id`` is the persisted
            conversation item id also emitted by
            ``session.input.consumed``; ``{"queued": False}`` for
            control and internal transient events.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        # Validate event type at the route boundary. Anything not in
        # ``_ALLOWED_EVENT_TYPES`` is a client mistake — failing here
        # is far better than silently persisting an item the agent
        # loop will only crash on later when ``parse_item_data`` runs
        # against the payload (rule 15 — fail loud).
        if body.type not in _ALLOWED_EVENT_TYPES:
            raise OmnigentError(
                f"Unknown event type: {body.type!r}. "
                f"Allowed types: {sorted(_ALLOWED_EVENT_TYPES)}",
                code=ErrorCode.INVALID_INPUT,
            )
        # For item types, validate the data payload shape against
        # the item-type's discriminator class. The control types
        # (interrupt, approval) bypass the item-persist path and have
        # their own payload schemas — they skip this check (interrupt
        # has no payload; approval's MCP-shape payload is validated
        # inside ``_dispatch_approval``).
        if body.type not in (
            _INTERRUPT_TYPE,
            _APPROVAL_TYPE,
            _MCP_ELICITATION_TYPE,
            _COMPACT_TYPE,
            _SLASH_COMMAND_TYPE,
            _STOP_SESSION_TYPE,
            _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
            _EXTERNAL_CONVERSATION_ITEM_TYPE,
            _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
            _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE,
            _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE,
            _EXTERNAL_SESSION_INTERRUPTED_TYPE,
            _EXTERNAL_SESSION_SUPERSEDED_TYPE,
            _EXTERNAL_ELICITATION_RESOLVED_TYPE,
            _EXTERNAL_SESSION_STATUS_TYPE,
            _EXTERNAL_SESSION_USAGE_TYPE,
            _EXTERNAL_COMPACTION_STATUS_TYPE,
            _EXTERNAL_MCP_STARTUP_TYPE,
            _EXTERNAL_MODEL_CHANGE_TYPE,
            _EXTERNAL_MODEL_OPTIONS_TYPE,
            _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
            _EXTERNAL_SESSION_TODOS_TYPE,
            _EXTERNAL_SUBAGENT_START_TYPE,
            _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
            _EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE,
        ):
            try:
                parse_item_data(body.type, {"type": body.type, **body.data})
            except (ValueError, TypeError) as exc:
                raise OmnigentError(
                    f"Invalid data payload for event type {body.type!r}: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        # Fail fast on malformed tools at the boundary. The raw dicts
        # (not the parsed objects) are what the runner stores — the
        # parse call is purely a validator.
        if body.tools:
            try:
                parse_client_side_tool_specs(body.tools)
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        # ── Policy evaluation (path-agnostic) ────────────────
        # Evaluate policies BEFORE persistence/runner forwarding so
        # enforcement fires on both paths. On DENY, persist the
        # event (possibly with modified body) through whichever
        # path is active, then return the deny verdict. On ALLOW,
        # fall through to the normal persist/forward path.
        _policy_body = body  # may be replaced by OUTPUT deny
        _actor = _build_actor(user_id)
        # A closed sub-agent session (sys_session_close) rejects new user
        # input — the orchestrator must spawn a fresh session to continue.
        if (
            body.type == "message"
            and body.data.get("role") == "user"
            and is_session_closed(conv.labels, conv.title)
        ):
            raise OmnigentError(
                "Session is closed. Start a new sub-agent session to continue.",
                code=ErrorCode.CONFLICT,
            )
        if (
            body.type == "message"
            and body.data.get("role") == "user"
            and conv.agent_id is not None
        ):
            try:
                _input_verdict = await _evaluate_input_policy(
                    request,
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    agent_store,
                    runner_router,
                    actor=_actor,
                )
            except Exception as _policy_exc:  # noqa: BLE001 — fail-safe for misconfigured policies
                # Policy evaluation crashed (e.g. factory misconfigured).
                # Log and treat as DENY so the session doesn't hang on
                # "working" forever. The full cause is logged for admins;
                # the denial reason returned to (and streamed at) the client
                # stays generic so the raw exception text isn't exposed.
                _logger.warning(
                    "Input policy evaluation failed for %s: %s",
                    session_id,
                    _policy_exc,
                    exc_info=True,
                )
                _input_verdict = {
                    "verdict": "deny",
                    "reason": "Denied by policy (policy evaluation error).",
                }
            if _input_verdict is not None:
                # DENY or ASK — don't forward to runner. Publish a
                # deny sentinel on the session stream so the
                # client/REPL sees feedback.
                reason = _input_verdict.get("reason", "Denied by policy")
                _publish_status(session_id, "running")
                _publish_policy_deny(session_id, reason)
                await _persist_policy_deny_sentinel(
                    session_id,
                    conv,
                    reason,
                    conversation_store,
                    agent_store,
                )
                # Terminal response.completed before idle so live-tail
                # consumers (the headless ``-p`` client) unblock.
                _publish_input_deny_terminal(session_id, conv, reason)
                _publish_status(session_id, "idle")
                # Return the same shape the client expects from POST
                # /events so postEvent doesn't throw on an unexpected
                # response body. queued=False signals the event was
                # handled synchronously (denied, not queued for a turn).
                return {"queued": False, "denied": True, "reason": reason}
        elif body.type == _SLASH_COMMAND_TYPE and conv.agent_id is not None:
            _input_verdict = await _evaluate_input_policy(
                request,
                session_id,
                conv,
                _build_skill_slash_command_policy_body(body),
                conversation_store,
                agent_store,
                runner_router,
            )
            if _input_verdict is not None:
                reason = _input_verdict.get("reason", "Denied by policy")
                _publish_status(session_id, "running")
                _publish_policy_deny(session_id, reason)
                await _persist_policy_deny_sentinel(
                    session_id,
                    conv,
                    reason,
                    conversation_store,
                    agent_store,
                )
                # Terminal response.completed before idle (see message branch).
                _publish_input_deny_terminal(session_id, conv, reason)
                _publish_status(session_id, "idle")
                return {"queued": False, "denied": True, "reason": reason}
        elif (
            body.type == "message"
            and body.data.get("role") == "assistant"
            and conv.agent_id is not None
        ):
            _output_verdict = await _evaluate_output_policy(
                session_id,
                conv,
                body,
                conversation_store,
                agent_store,
                runner_router,
                actor=_actor,
            )
            if _output_verdict is not None:
                if _output_verdict.get("_denied_body") is not None:
                    _policy_body = _output_verdict["_denied_body"]
                    body = _policy_body
                # For OUTPUT DENY, fall through to persist the
                # denied body (with sentinel text). The verdict
                # is returned after persistence below.
                if _output_verdict["verdict"] == "deny":
                    pass  # fall through with modified body
                else:
                    return _output_verdict
        elif body.type == "function_call" and body.data.get("evaluate_policy"):
            _tool_verdict = await _evaluate_tool_call_policy(
                session_id,
                conv,
                body,
                conversation_store,
                agent_store,
                runner_router,
                actor=_actor,
            )
            if _tool_verdict is not None:
                return _tool_verdict
            # ALLOW — return explicit verdict so the request does
            # not fall through to the persist-and-forward path.
            # Policy evaluation requests are queries, not items to
            # persist or relay to the harness (which rejects
            # ``function_call`` as an unknown inbound event type).
            return {"verdict": "allow"}

        if body.type == _INTERRUPT_TYPE:
            _publish_interrupted(session_id)
            # Fence the cancelled turn (see _interrupt_fenced_sessions).
            _interrupt_fenced_sessions.add(session_id)
            runner_client = await _get_runner_client(
                session_id,
                runner_router,
            )
            interrupt_delivered = False
            if runner_client is not None:
                try:
                    interrupt_resp = await runner_client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={"type": "interrupt"},
                        timeout=5.0,
                    )
                    interrupt_delivered = interrupt_resp.status_code < 400
                except (httpx.HTTPError, ConnectionError):
                    # WSTunnelTransport raises bare ConnectionError on tunnel close.
                    _logger.exception(
                        "Interrupt forward failed for %r",
                        session_id,
                    )
            if not interrupt_delivered:
                # The turn keeps running and nothing else lifts the fence —
                # remove it so the turn's remaining output isn't dropped.
                _interrupt_fenced_sessions.discard(session_id)
            return {"queued": False}
        if body.type == _STOP_SESSION_TYPE:
            # Terminating the whole session (not just the current turn)
            # is a lifecycle action; require owner access on top of the
            # LEVEL_EDIT gate above so a shared editor can't kill the
            # owner's session.
            await _require_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            # Fence the cancelled turn, same as interrupt.
            _interrupt_fenced_sessions.add(session_id)
            # Harness-agnostic forward: the runner kills the external
            # process for harnesses that have one (claude-native
            # hard-kills its tmux pane) and 204s otherwise. Unlike the
            # best-effort effort/model_change relay, a failed stop means
            # the session is still alive — so this helper RAISES on a
            # non-2xx / unreachable runner (503) rather than swallowing
            # it, letting the web UI show the stop didn't land instead
            # of closing the dialog as if it succeeded.
            try:
                stop_delivered = await _stop_session_via_runner(session_id, runner_router)
            except Exception:
                # Stop didn't land: the turn keeps running, so lift the
                # fence or its remaining output is dropped forever.
                _interrupt_fenced_sessions.discard(session_id)
                raise
            if not stop_delivered:
                # No runner resolved: nothing else lifts the fence (same as interrupt).
                _interrupt_fenced_sessions.discard(session_id)
            # Host-spawned sessions run on a dedicated runner the host
            # launched for this one session. Killing the pane (above) leaves
            # that runner connected, so GET /health keeps reporting
            # runner_online: true and the web UI never shows the session as
            # disconnected — new messages hang on "working" against a dead
            # pane. Stop the runner too so its tunnel drops and the web UI
            # shows the same "Agent disconnected — click to show reconnect
            # command" banner a CLI-launched session reaches on exit. Read
            # host_id / runner_id from the owner-gated session row so we can
            # only ever stop the runner bound to this session.
            stop_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if stop_conv is not None and stop_conv.host_id and stop_conv.runner_id:
                # Mark the tunnel drop as intentional BEFORE tearing it down so
                # the relay's disconnect handler renders a quiet stopped state
                # rather than "Error · runner_disconnected". Only host-spawned
                # sessions drop the tunnel on Stop; other harnesses leave the
                # runner connected, so there is nothing to suppress for them.
                _intentional_stop_sessions.add(session_id)
                teardown_delivered = await _stop_session_host_runner(
                    session_id,
                    stop_conv.host_id,
                    stop_conv.runner_id,
                    getattr(request.app.state, "host_registry", None),
                )
                if not teardown_delivered:
                    # Best-effort stop did not land (host offline / timeout /
                    # failure): no tunnel drop will follow, so the relay won't
                    # reach the disconnect handler that consumes the marker.
                    # Discard it now so it can't outlive this turn on the
                    # reused per-session relay task and later swallow a genuine
                    # runner_disconnected as a quiet idle.
                    _intentional_stop_sessions.discard(session_id)
            # Stop is non-sticky: no persistent marker is written. The
            # runner tunnel dropping above flips ``runner_online`` to false
            # honestly, and the next message auto-relaunches the session on
            # its (still-online) host via the normal message-dispatch
            # relaunch path below.
            try:
                import hashlib as _hashlib

                _srv_id = _get_installation_id()
                _anon: str | None = None
                if user_id is not None:
                    _salt = f"{_srv_id}:{user_id}" if _srv_id else user_id
                    _anon = _hashlib.sha256(_salt.encode()).hexdigest()[:16]
                _tel_emit(
                    _TelSessionStoppedEvent(
                        session_id=session_id,
                        installation_id=_srv_id,
                        anon_user_id=_anon,
                    )
                )
            except Exception:  # noqa: BLE001 — telemetry is best-effort
                pass
            return {"queued": False}
        if body.type == _APPROVAL_TYPE:
            # Deliver the verdict through the shared resolver: it
            # sets any server-side harness Future (owner-checked),
            # clears the sidebar badge, and forwards
            # to the runner for runner-side (policy) elicitations.
            # The dedicated URL endpoint (``.../elicitations/{eid}/
            # resolve``) routes through the same helper.
            await _resolve_elicitation(session_id, body.data, runner_router, conversation_store)
            # Apply any policy writes deferred by the relay tool-call ASK gate
            # (e.g. a cost-budget checkpoint) now that the verdict is in.
            await _apply_pending_policy_ask_writes(
                session_id, conv, conversation_store, agent_store, body.data
            )
            return {"queued": False}
        if body.type == _MCP_ELICITATION_TYPE:
            # The runner's inline MCP elicitation callback fires when
            # an external MCP server sends ``elicitation/create``
            # during a ``tools/call``. Publish the elicitation as an
            # SSE event (approval card in web UI, y/a/n prompt in
            # REPL) and return the elicitation_id immediately so the
            # runner can park on ``pending_approvals``. The user's
            # verdict arrives later via ``type: "approval"`` →
            # ``_resolve_elicitation`` → ``_forward_approval_to_runner``
            # → runner's ``pending_approvals`` resolves.
            elicit_data = body.data or {}
            elicit_id = f"elicit_{secrets.token_hex(16)}"
            elicit_params = ElicitationRequestParams(
                mode="form",
                message=elicit_data.get("message", ""),
                requestedSchema=elicit_data.get("requestedSchema"),
            )
            event = ElicitationRequestEvent(
                type="response.elicitation_request",
                elicitation_id=elicit_id,
                params=elicit_params,
            )
            _mcp_elicit_payload = event.model_dump()
            session_stream.publish(session_id, _mcp_elicit_payload)
            # Mirror the prompt into ancestor streams so a sub-agent MCP
            # elicitation surfaces in the parent (polly) chat with a
            # ``target_session_id`` pointing back at this child. The
            # verdict still arrives via the generic ``approval`` event,
            # which mirrors the resolved signal back up through
            # ``_resolve_elicitation``.
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                _mcp_elicit_payload,
            )
            return {"queued": False, "elicitation_id": elicit_id}
        if body.type == _COMPACT_TYPE:
            # Unified control dispatch (designs/CLAUDE_NATIVE.md
            # "Control events dispatch on the runner"): forward /compact
            # to the bound runner first, regardless of harness. The
            # runner dispatches by harness — claude-native injects
            # /compact into the tmux pane so Claude Code compacts its
            # own context and returns 200; other harnesses 204 no-op.
            # The Omnigent server stays harness-agnostic: it runs its own
            # in-process compaction only when the runner did NOT handle
            # the control (204 / no runner bound). A 4xx/5xx from the
            # runner (e.g. 503 when the claude-native pane isn't
            # attached) is surfaced as an error rather than silently
            # falling through to AP-side compaction, which would be
            # wrong for a terminal-owned session.
            runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": _COMPACT_TYPE},
            )
            if runner_result is not None and runner_result.status_code == 200:
                return {"queued": False}
            if runner_result is not None and runner_result.status_code != 204:
                raise OmnigentError(
                    f"Compaction failed: runner returned {runner_result.status_code}",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            await _run_compact_locked(
                session_id,
                conv,
                agent_store,
                agent_cache,
            )
            return {"queued": False}
        if body.type == "compaction":
            import uuid as _uuid

            item = NewConversationItem(
                type="compaction",
                response_id=f"compact_{_uuid.uuid4().hex}",
                data=parse_item_data("compaction", body.data),
            )
            await asyncio.to_thread(
                conversation_store.append,
                session_id,
                [item],
            )
            return {"queued": True}
        if body.type == _EXTERNAL_ASSISTANT_MESSAGE_TYPE:
            item_id = await _persist_external_assistant_message(
                session_id,
                body,
                conversation_store,
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_CONVERSATION_ITEM_TYPE:
            item_id = await _persist_external_conversation_item(
                session_id,
                conv,
                body,
                conversation_store,
                created_by=_attribution_user(user_id),
                background_title_coordinator=background_title_coordinator,
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE:
            _publish_external_output_text_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE:
            _publish_external_tool_output_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE:
            _publish_external_output_reasoning_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_INTERRUPTED_TYPE:
            response_id = body.data.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise OmnigentError(
                    "external_session_interrupted data.response_id must be a string",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_interrupted(session_id, response_id=response_id)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_SUPERSEDED_TYPE:
            target_conversation_id = body.data.get("target_conversation_id")
            if not isinstance(target_conversation_id, str) or not target_conversation_id.strip():
                raise OmnigentError(
                    "external_session_superseded requires a non-empty string "
                    "data.target_conversation_id",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_session_superseded(session_id, target_conversation_id.strip())
            return {"queued": False}
        if body.type == _EXTERNAL_ELICITATION_RESOLVED_TYPE:
            elicitation_id = body.data.get("elicitation_id")
            if not isinstance(elicitation_id, str):
                raise OmnigentError(
                    "external_elicitation_resolved requires string data.elicitation_id.",
                    code=ErrorCode.INVALID_INPUT,
                )
            _signal_harness_elicitation_resolved_by_id(session_id, elicitation_id)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_STATUS_TYPE:
            status = body.data.get("status")
            if status not in _EXTERNAL_SESSION_STATUS_VALUES:
                raise OmnigentError(
                    f"external_session_status requires data.status in "
                    f"{sorted(_EXTERNAL_SESSION_STATUS_VALUES)}; got {status!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            response_id = body.data.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise OmnigentError(
                    "external_session_status data.response_id must be a string",
                    code=ErrorCode.INVALID_INPUT,
                )
            # Surface the failure reason a native forwarder carries so a
            # top-level session sees it on its own status edge and persisted
            # last_task_error, not only the sub-agent parent-inbox path.
            output = body.data.get("output")
            status_error: ErrorDetail | None = None
            if status == "failed" and isinstance(output, str) and output.strip():
                status_error = ErrorDetail(
                    code=(
                        "codex_reauth_required"
                        if body.data.get("reauth_required") is True
                        else "codex_turn_error"
                    ),
                    message=output.strip(),
                )
            if status_error is not None:
                await _persist_session_status_error_labels(
                    session_id, status_error, conversation_store
                )
            elif status == "running":
                await _persist_session_status_error_labels(session_id, None, conversation_store)
            # ``None`` (field absent) = no information; leave the sticky
            # tally untouched (the PTY-activity ``idle`` carries none). An
            # explicit ``0`` from a ``Stop`` hook is authoritative and clears
            # the tally, so a finished background shell drops the indicator.
            raw_bg_count = body.data.get("background_task_count")
            bg_count = (
                raw_bg_count
                if isinstance(raw_bg_count, int)
                and not isinstance(raw_bg_count, bool)
                and raw_bg_count >= 0
                else None
            )
            # A sub-agent's background-task ``waiting`` must deliver as ``idle``
            # so the parent's terminal-delivery branch below fires (otherwise
            # the orchestrator hangs); the tally still drives the child spinner.
            effective_status = _subagent_delivery_status(status, bg_count, conv)
            if effective_status != status:
                status = effective_status
                body.data["status"] = status
            _publish_status(
                session_id,
                status,
                status_error,
                response_id=response_id,
                background_task_count=bg_count,
            )
            forward_body = body.model_dump()
            forward_body["data"] = await _enrich_idle_status_with_subagent_output(
                forward_body["data"], status, session_id, conversation_store
            )
            runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                forward_body,
            )
            if (
                conv.kind == "sub_agent"
                and status in {"idle", "failed"}
                and not _is_codex_native_subagent(conv)
            ):
                # Codex-internal children are tracked inside the same
                # app-server thread tree; they have no runner inbox entry
                # to forward terminal status to.
                if runner_result is None:
                    # The child's pinned runner_id is stale — its runner was
                    # relaunched under a new id and only the parent was
                    # rebound, so the child points at a dead runner forever and
                    # this terminal status would 503 indefinitely while the
                    # parent hangs waiting for the child's inbox result. Heal
                    # the binding and re-deliver through the parent's live
                    # runner before failing.
                    recovered = await _recover_subagent_status_forward_via_parent(
                        conv,
                        runner_router,
                        getattr(request.app.state, "tunnel_registry", None),
                        conversation_store,
                        forward_body,
                    )
                    if recovered is not None:
                        runner_result = recovered
                _require_external_status_forward(
                    session_id,
                    status,
                    runner_result,
                )
            return {"queued": False}
        if body.type == _EXTERNAL_COMPACTION_STATUS_TYPE:
            # Terminal-observed compaction edge (claude-native forwarder):
            # republish as the standard compaction SSE so the web UI
            # spinner brackets Claude's real terminal compaction. No token
            # count is available here — the context ring is updated
            # separately by external_session_usage — so completed carries
            # total_tokens=None.
            compaction_status = body.data.get("status")
            if compaction_status not in _EXTERNAL_COMPACTION_STATUS_VALUES:
                raise OmnigentError(
                    f"external_compaction_status requires data.status in "
                    f"{sorted(_EXTERNAL_COMPACTION_STATUS_VALUES)}; got {compaction_status!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            if compaction_status == "in_progress":
                _publish_compaction_in_progress(session_id)
            elif compaction_status == "completed":
                _publish_compaction_completed(session_id, None)
            else:
                _publish_compaction_failed(session_id)
            return {"queued": False}
        if body.type == _EXTERNAL_MCP_STARTUP_TYPE:
            # Harness MCP-server startup progress (codex-native forwarder):
            # republish as a ``session.mcp_startup`` SSE so the web UI shows
            # per-server startup state while the harness boots. Malformed
            # entries are rejected at the boundary — a bogus map would only
            # strand the UI's startup band.
            raw_servers = body.data.get("servers")
            if not isinstance(raw_servers, dict):
                raise OmnigentError(
                    "external_mcp_startup requires data.servers to be an object "
                    f"mapping server names to startup records; got {raw_servers!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            mcp_servers: dict[str, McpServerStartup] = {}
            for server_name, record in raw_servers.items():
                record_status = record.get("status") if isinstance(record, dict) else None
                if not (
                    isinstance(server_name, str)
                    and server_name
                    and record_status in _EXTERNAL_MCP_STARTUP_STATUS_VALUES
                ):
                    raise OmnigentError(
                        "external_mcp_startup server records require a status in "
                        f"{sorted(_EXTERNAL_MCP_STARTUP_STATUS_VALUES)}; got "
                        f"{server_name!r}: {record!r}",
                        code=ErrorCode.INVALID_INPUT,
                    )
                record_error = record.get("error")
                mcp_servers[server_name] = McpServerStartup(
                    status=record_status,
                    error=record_error if isinstance(record_error, str) and record_error else None,
                )
            _publish_mcp_startup(session_id, mcp_servers)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_USAGE_TYPE:
            # Persist the harness-reported cumulative usage so the
            # tool-call cost gate can read the running
            # ``total_cost_usd`` on the next tool call. (Cost budgets
            # now enforce at ``tool_call`` via the PreToolUse hook, not
            # post-hoc here — a logged output cannot be un-logged.)
            await _persist_external_session_usage(
                session_id,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_MODEL_CHANGE_TYPE:
            await _persist_external_model_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_MODEL_OPTIONS_TYPE:
            _persist_external_model_options(session_id, conv, body)
            return {"queued": False}
        if body.type == _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE:
            await _persist_external_reasoning_effort_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE:
            await _persist_external_codex_collaboration_mode_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_TODOS_TYPE:
            _handle_external_session_todos(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_SUBAGENT_START_TYPE:
            child_id = await _persist_external_subagent_start(
                session_id,
                conv,
                body,
                conversation_store,
            )
            # Returned to the claude-native forwarder so it can address
            # subsequent ``external_conversation_item`` /
            # ``external_session_status`` events to the child id.
            return {"queued": False, "child_session_id": child_id}
        if body.type == _EXTERNAL_CODEX_SUBAGENT_START_TYPE:
            child_id = await _persist_external_codex_subagent_start(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False, "child_session_id": child_id}
        if body.type == "function_call_output":
            # A client-side tool's result tunneling back to a parked turn.
            # The harness scaffold resolves the parked tool Future on a
            # ``tool_result`` event (ToolResultEvent {call_id, output}), so
            # translate the session-API ``function_call_output`` into that
            # wire shape and forward to the bound runner, which relays it
            # verbatim to the parked harness. Mirrors the runner's own
            # dispatch_tool_locally tool_result post; the output here came
            # from the caller (a client-side tool) instead of a local
            # dispatch. ``parse_item_data`` above already validated the
            # payload against ``FunctionCallOutputData`` (call_id: str,
            # output: str), so both fields are present strings. Stale
            # call_ids no-op at the scaffold; the harness re-emits the
            # completed function_call + output on resume, so history is
            # written through the normal stream path (no separate persist).
            runner_client = await _get_runner_client(session_id, runner_router)
            if runner_client is None:
                raise OmnigentError(
                    "No runner bound to this session; cannot deliver the tool result.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            try:
                await runner_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "tool_result",
                        "call_id": body.data["call_id"],
                        "output": body.data["output"],
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError) as exc:
                # Fail loud (503), not best-effort: unlike the advisory
                # interrupt-forward, a dropped tool_result leaves the parked
                # turn hanging until it times out. Surfacing the failure lets
                # the caller retry the delivery (the scaffold no-ops if a
                # retry double-delivers a now-stale call_id).
                raise OmnigentError(
                    "Failed to deliver the tool result to the session runner.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                ) from exc
            return {"queued": True, "item_id": body.data["call_id"]}
        # Whether the runner was initially unavailable or was woken below. In
        # that case the session-init handshake may still be racing the first
        # message, even if we reused the original binding instead of launching
        # a replacement.
        _runner_needs_session_init = False
        # Item event (message, function_call_output, etc.).
        if conv.host_id is not None and await _maybe_wake_stale_resumable_managed_sandbox(
            session_id=session_id,
            conv=conv,
            app_state=request.app.state,
            conversation_store=conversation_store,
        ):
            # A resumable managed wake may have re-launched the runner and
            # updated liveness while this handler was holding an old row.
            conv_after_wake = await asyncio.to_thread(
                conversation_store.get_conversation,
                session_id,
            )
            if conv_after_wake is None:
                raise _session_not_found()
            conv = conv_after_wake
            _runner_needs_session_init = True
        runner_client = await _get_runner_client(session_id, runner_router)
        # Managed-launch rendezvous: a ``host_type="managed"`` create
        # returns before the sandbox exists, so the first message (the
        # Web UI auto-sends the composer prompt right after navigate)
        # can land while the background provision is still running.
        # Instead of failing with "no runner bound", wait for the
        # launch to settle: success leaves the session host-bound with
        # its runner tunnel already up (the background task awaits
        # it), failure surfaces the recorded reason.
        if runner_client is None and conv.host_id is None:
            _managed_tracker = getattr(request.app.state, "managed_launches", None)
            _managed_launch = (
                _managed_tracker.get(session_id) if _managed_tracker is not None else None
            )
            if _managed_launch is not None:
                await _await_settled_managed_launch(_managed_launch)
                # The launch bound host_id / workspace / runner_id to
                # the row after this handler's fetch — re-read so the
                # resolution below sees the bound runner.
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise _session_not_found()
                runner_client = await _get_runner_client(session_id, runner_router)
        if runner_client is None and conv.host_id is not None:
            _tunnel_registry = getattr(request.app.state, "tunnel_registry", None)
            _grace_host_reg = getattr(request.app.state, "host_registry", None)
            _grace_host_conn = (
                _grace_host_reg.get(conv.host_id) if _grace_host_reg is not None else None
            )
            # A just-created host session already has a runner_id before
            # the runner's tunnel is registered. The Web UI can post the
            # first message during that gap; wait briefly for the pinned
            # runner before treating it as dead and replacing it — but end
            # that wait early when the runner is not actually coming. The
            # host owns runner-process liveness (it holds the Popen), so we
            # race a ``host.runner_status`` query against the connect grace:
            # a booting runner connects (or reads "alive") and we forward,
            # while one that was stopped, crashed, or lost to a host restart
            # reads "dead"/"unknown" and cuts the wait short so the relaunch
            # below runs at once. A host that is offline, too old to answer,
            # or slow yields no verdict and the grace runs its normal
            # course, so the query only ever speeds up the cold path.
            if conv.runner_id is not None and _HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0:
                _logger.info(
                    "Waiting up to %.1fs for host-bound runner %s to register "
                    "for session %s before relaunch",
                    _HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                    conv.runner_id,
                    session_id,
                )
                if _grace_host_conn is not None:
                    runner_client = await _wait_for_host_bound_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                        host_conn=_grace_host_conn,
                        host_registry=_grace_host_reg,
                    )
                else:
                    # Host tunnel absent: no one to query, so this is the
                    # plain connect grace (unchanged pre-existing behavior).
                    runner_client = await _wait_for_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                    )
            # Runner is dead or still not spawned for a host-bound
            # session. Ask the host to launch one, then re-fetch the
            # runner client and wait briefly for it to connect before
            # forwarding the message. This is the relaunch path a
            # non-sticky Stop relies on: after Stop drops the runner
            # tunnel, the next message lands here and relaunches the
            # session on its still-online host. Gated only on host
            # presence — if the host is offline this falls through to
            # the RUNNER_UNAVAILABLE raise below, the same as a
            # disconnected CLI session.
            _host_reg = getattr(request.app.state, "host_registry", None)
            if runner_client is None and _host_reg is not None:
                _host_conn = _host_reg.get(conv.host_id)
                if _host_conn is not None:
                    launch_attempt = await _launch_runner_on_host(
                        conv,
                        conversation_store,
                        _host_reg,
                        _host_conn,
                    )
                    if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                        # The host refused: the agent's harness isn't
                        # configured there. This message was the real
                        # runner-start attempt, so consume it and record a
                        # transcript error (the host's message names the
                        # fix, `omnigent setup`) the web renders as a
                        # banner — instead of timing out into a generic
                        # RUNNER_UNAVAILABLE. The binding stays so a later
                        # message relaunches once setup is done.
                        item_id = await _persist_host_launch_failure_turn(
                            session_id,
                            conv,
                            body,
                            conversation_store,
                            launch_attempt.error,
                            runner_router,
                            created_by=_attribution_user(user_id),
                        )
                        return {"queued": True, "item_id": item_id}
                    relaunched_runner_id = launch_attempt.runner_id
                else:
                    relaunched_runner_id = None
                    # The host tunnel is gone entirely. A managed
                    # host's sandbox is relaunchable — provision a new
                    # generation under the same host identity and ride
                    # it; an external (laptop) host falls through to
                    # the unavailable raise below.
                    if await _maybe_relaunch_managed_sandbox(
                        session_id=session_id,
                        conv=conv,
                        app_state=request.app.state,
                        conversation_store=conversation_store,
                    ):
                        conv_after_relaunch = await asyncio.to_thread(
                            conversation_store.get_conversation, session_id
                        )
                        if conv_after_relaunch is None:
                            raise _session_not_found()
                        conv = conv_after_relaunch
                        runner_client = await _get_runner_client(session_id, runner_router)
            else:
                relaunched_runner_id = None
            if runner_client is None:
                _logger.info(
                    "Waiting up to %.0fs for host %s to spawn a runner for session %s",
                    _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                    conv.host_id,
                    session_id,
                )
                runner_client = await _wait_for_runner_client(
                    session_id,
                    runner_router,
                    _tunnel_registry,
                    runner_id=relaunched_runner_id,
                    timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                    runner_exit_reports=runner_exit_reports,
                )
            if runner_client is None:
                _runner_needs_session_init = False
            else:
                _runner_needs_session_init = True
        if runner_client is None:
            # A native terminal-session message must NOT be silently
            # dropped when no runner is reachable — the runner crashed
            # before connecting (the daemon couldn't bring it up). Persist
            # the user's message together with the runner-failure error so
            # it survives reload and the banner explains why, becoming the
            # AP-server-as-writer failed turn (same shape as a definitive
            # ensure-probe failure). The cause, when known, is the daemon's
            # exit report keyed by this session's runner_id; otherwise a
            # generic unavailable message. This is safe precisely because
            # the harness will never see it (no desync — there is no live
            # harness). Other event types and non-native sessions still
            # raise: their message would replay to a relaunched runner, so
            # persisting now WOULD desync the store from harness state.
            if body.type == "message" and _is_native_terminal_session(conv):
                exit_cause = (
                    runner_exit_reports.get(conv.runner_id)
                    if runner_exit_reports is not None and conv.runner_id is not None
                    else None
                )
                offline_error = ErrorData(
                    source="execution",
                    code="runner_failed_to_start",
                    message=(
                        exit_cause
                        if exit_cause
                        else (
                            "The runner for this session is not available — "
                            "it may have failed to start. See the host logs."
                        )
                    ),
                )
                item_id = await _persist_native_terminal_failure(
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    offline_error,
                    runner_router,
                    created_by=_attribution_user(user_id),
                )
                return {"queued": True, "item_id": item_id}
            # Raise so the Omnigent server doesn't persist an item the
            # harness will never see. Other event paths (interrupt,
            # approval) are best-effort and silently skip when no
            # runner is bound — item events can't, because that
            # would desync conversation store and harness state.
            raise OmnigentError(
                "No runner bound for session",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        refreshed_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if refreshed_conv is None:
            raise _session_not_found()
        conv = refreshed_conv
        native_terminal_ready = False
        if _runner_needs_session_init:
            # The runner was unavailable when this request began, so its
            # connect callback may still be racing us. Await the handshake
            # so the terminal + transcript forwarder are watching before we
            # inject the message — otherwise a native web message is
            # forwarded into a TUI whose forwarder isn't attached, the
            # round-trip never mirrors back, and the optimistic bubble
            # sticks with no reply (host-restart bug).
            native_terminal_ready = await _ensure_runner_session_initialized(
                session_id,
                conv,
                runner_client,
                conversation_store,
                initializer=getattr(request.app.state, "runner_session_initializer", None),
            )
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )
        _agent = agent_store.get(conv.agent_id) if conv.agent_id else None
        # Determine whether the agent has MCP servers so the runner's
        # proxy_stream handler knows to initialise ProxyMcpManager.
        # agent_cache.load() is O(1) on a warm in-memory cache; the
        # asyncio.to_thread wrapper covers the rare cold-cache path
        # where the bundle is extracted from disk for the first time.
        _has_mcp_servers = False
        if _agent is not None and agent_cache is not None and _agent.bundle_location:
            try:
                _loaded_agent = await asyncio.to_thread(
                    agent_cache.load,
                    _agent.id,
                    _agent.bundle_location,
                )
                _has_mcp_servers = bool(_loaded_agent.spec.mcp_servers)
            except Exception:  # noqa: BLE001 — spec load failure must not break event forwarding
                _logger.warning(
                    "Failed to load agent spec for MCP hint for session=%s",
                    session_id,
                    exc_info=True,
                )
        pending_background_title = prepare_background_session_title(
            coordinator=background_title_coordinator,
            conversation=conv,
            event=body,
        )
        if body.type == _SLASH_COMMAND_TYPE:
            if _agent is None:
                raise OmnigentError(
                    f"Session {session_id!r} has no agent; cannot run slash command",
                    code=ErrorCode.INVALID_INPUT,
                )
            item_id = await _dispatch_skill_slash_command_to_runner(
                session_id,
                conv,
                body,
                conversation_store,
                runner_client,
                agent=_agent,
                has_mcp_servers=_has_mcp_servers,
                created_by=_attribution_user(user_id),
            )
            if pending_background_title is not None:
                pending_background_title.schedule()
            return {"queued": True, "item_id": item_id}
        dispatch = await _dispatch_session_event_to_runner(
            session_id,
            conv,
            body,
            conversation_store,
            runner_client,
            agent_name=_agent.name if _agent else None,
            file_store=file_store,
            artifact_store=artifact_store,
            has_mcp_servers=_has_mcp_servers,
            created_by=_attribution_user(user_id),
            runner_router=runner_router,
            native_terminal_ready=native_terminal_ready,
        )
        if pending_background_title is not None:
            pending_background_title.schedule()
        response: dict[str, Any] = {"queued": True}
        if dispatch.item_id is not None:
            response["item_id"] = dispatch.item_id
        # Native-terminal web message: hand back the pending-input id. It
        # identifies the snapshot's replayed bubble on rebind and is the
        # cleared_pending_id the consume event carries to drop it. Clients
        # may adopt it onto their optimistic bubble for id-based dedupe;
        # the first-party web client keeps its client temp id (React-key
        # stability) and relies on stableKey + FIFO instead.
        if dispatch.pending_id is not None:
            response["pending_id"] = dispatch.pending_id
        return response

    # ── GET /sessions/{session_id}/stream ────────────────────────

    # Live-tail only. Clients reconnect via GET /v1/sessions/{id}
    # for snapshot, then open a new stream; events that fire
    # between are deduped client-side by item id (see API.md).
    @router.get(
        "/sessions/{session_id}/stream",
        # response_model=None: returns StreamingResponse, not a model.
        response_model=None,
        # responses=: surface the SSE union to OpenAPI. The
        # ``text/event-stream`` content entry's schema points at the
        # discriminated union so generated clients know what to
        # expect on the wire. ``scripts/dump_openapi.py`` rewrites
        # this in OpenAPI 3.2's ``itemSchema`` form (the OAS 3.2
        # mechanism for typing each item in a sequential stream)
        # before writing ``openapi.json`` to disk.
        responses={
            200: {
                "description": ("SSE stream of :data:`ServerStreamEvent` frames for the session."),
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/ServerStreamEvent"},
                    },
                },
            },
        },
    )
    async def stream_session(
        request: Request,
        session_id: str,
        idle: bool = False,
    ) -> StreamingResponse:
        """
        Subscribe to the session's live SSE event stream.

        Does NOT replay history; clients reconcile via the snapshot
        endpoint. The generator emits ``[DONE]`` on normal completion
        and uses ``finally`` only for presence cleanup — see
        :func:`_stream_live_events`.

        Holding this stream open registers the caller as a session
        *viewer* (presence): co-viewers' streams receive
        ``session.presence`` events on join/leave/idle edges, and
        this stream's snapshot-on-connect includes the current
        viewer list. Presence is scoped to the session tree's root
        conversation, so viewers of different agents/sub-agents in
        one session see each other. See
        ``omnigent/server/presence.py``.

        :param request: The FastAPI request, used to detect
            disconnect.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param idle: Presence idle flag computed by the web client
            at connect time (tab backgrounded ≥ its debounce). An
            idle *flip* mid-view arrives as a reconnect carrying the
            new value — there is no separate update endpoint.
        :returns: An SSE :class:`StreamingResponse`.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        runner_client = await _get_runner_client(
            session_id,
            runner_router,
        )
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )

        async def _resource_snapshot() -> list[dict[str, Any]]:
            """Gather current resource state to emit as snapshot-on-connect.

            Best-effort: every runner-touching gather is time-boxed and
            guarded so a slow/unavailable runner never blocks the live
            tail. Terminals arrive as ``session.resource.created`` (the
            same shape the web's live handler already consumes); child
            sessions as ``session.child_session.updated``; changed files
            as a single invalidate that triggers a client refetch.

            The in-flight assistant-text replay is NOT read here: it is
            dedup-sensitive and must be captured synchronously at slot
            registration via ``subscribe``'s ``pre_ready_snapshot`` hook,
            before ``ready_event`` suspends. The resource
            gathers below need awaits and are not dedup-sensitive, so they
            stay in this async hook.
            """
            events: list[dict[str, Any]] = []
            try:
                page = await asyncio.to_thread(
                    conversation_store.list_conversations,
                    limit=100,
                    kind="sub_agent",
                    parent_conversation_id=session_id,
                    order="desc",
                    sort_by="created_at",
                )
                summaries = await _child_session_summaries_from_conversations(
                    page.data,
                    session_id,
                    conversation_store,
                )
                for summary in summaries:
                    events.append(
                        {
                            "type": "session.child_session.updated",
                            "conversation_id": session_id,
                            "child_session_id": summary.id,
                            "child": summary.model_dump(mode="json"),
                        }
                    )
            except Exception:  # noqa: BLE001 -- best-effort snapshot; never block live tail
                _logger.debug("snapshot: child sessions failed for %s", session_id, exc_info=True)
            if runner_client is not None:
                try:
                    resp = await asyncio.wait_for(
                        # order=asc: the web cache appends each replayed
                        # ``created`` event, so the replay must arrive in
                        # creation order or the session's own terminal (always
                        # created first) lands behind later agent-launched
                        # ones. limit=1000 (the runner endpoint max) keeps the
                        # oldest-first window from dropping the newest
                        # terminals past the default page of 20.
                        runner_client.get(
                            f"/v1/sessions/{session_id}/resources/terminals",
                            params={"order": "asc", "limit": "1000"},
                        ),
                        timeout=_SNAPSHOT_RUNNER_TIMEOUT_S,
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("data", []):
                            events.append({"type": "session.resource.created", "resource": item})
                except Exception:  # noqa: BLE001 -- best-effort snapshot; never block live tail
                    _logger.debug("snapshot: terminals failed for %s", session_id, exc_info=True)
            # Tell the client to (re)fetch the changed-files list rather
            # than fetching it here (avoids a second runner round-trip).
            events.append(
                {
                    "type": "session.changed_files.invalidated",
                    "session_id": session_id,
                    "environment_id": "default",
                }
            )
            # Current viewer list (full state, includes this stream's own
            # registration) so a joiner never waits for the next presence
            # edge to learn who's here. Scoped to the session tree's root
            # so a sub-agent page sees viewers of every agent in the tree.
            events.append(presence.snapshot(conv.root_conversation_id, session_id))
            return events

        return StreamingResponse(
            _stream_live_events(
                request,
                session_id,
                _resource_snapshot,
                # Presence tracks distinct human actors only — the reserved
                # single-user "local" sentinel maps to None (no tracking),
                # same as message attribution.
                viewer_user_id=_attribution_user(user_id),
                viewer_idle=idle,
                # Scope presence to the tree's root: sub-agent pages open
                # the CHILD conversation's stream, and per-conversation
                # scoping would hide co-viewers on other agents.
                presence_root_id=conv.root_conversation_id,
            ),
            media_type="text/event-stream",
            headers={
                # Keep intermediaries from buffering the SSE stream:
                # ``X-Accel-Buffering: no`` disables nginx-style response
                # buffering so heartbeats and deltas reach the client as
                # they're written (a buffered proxy can delay the 15s
                # heartbeat past a client/idle timeout), and ``no-cache``
                # keeps the long-lived response out of any shared cache.
                # NOTE: this does NOT defeat the Databricks Apps ingress'
                # hard ~5-min HTTP/2 stream-duration cap — that drop is
                # handled by the client's transparent reconnect.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── DELETE /sessions/{session_id} ──────────────────────────────

    @router.delete(
        "/sessions/{session_id}",
        response_model=None,
        responses={200: {"model": ConversationDeleted}},
    )
    async def delete_session(
        request: Request,
        session_id: str,
        delete_branch: bool = False,
    ) -> ConversationDeleted:
        """Delete a session and all associated resources.

        Requires owner-level access. Tears down tasks, runner-side
        resources (environments, terminals), session files, and the
        conversation row.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param delete_branch: Opt-in git cleanup, as a query param
            (``?delete_branch=true``). When ``True`` and the session
            has a server-created worktree (``git_branch`` set), the
            host removes the worktree directory and deletes its branch
            (``git worktree remove --force`` then ``git branch -D``).
            Ignored for sessions with no worktree. Best-effort: a
            cleanup failure does not block the delete. Defaults to
            ``False`` (worktree and branch left untouched). See
            designs/SESSION_GIT_WORKTREE.md.
        :returns: A :class:`ConversationDeleted` confirmation.
        :raises OmnigentError: 404 if no session or no access,
            403 if insufficient permissions.
        """
        user_id = _require_user(request, auth_provider)
        if permission_store is not None and user_id is not None:
            is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
            if not is_admin:
                grant = await asyncio.to_thread(permission_store.get, user_id, session_id)
                if grant is None or grant.level < LEVEL_OWNER:
                    if grant is not None:
                        raise OmnigentError(
                            "Only the session owner can delete this session",
                            code=ErrorCode.FORBIDDEN,
                        )
                    raise OmnigentError(
                        "Conversation not found",
                        code=ErrorCode.NOT_FOUND,
                    )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        await _best_effort_stop(session_id, conversation_store, runner_router)
        # Runner-side resource cleanup is best-effort: if the bound
        # runner is offline or unbound, the session must still be
        # deletable. Server-owned records (files and conversation row
        # below) live independently of the runner, and runner-side
        # resources are gone with the runner anyway.
        runner_client: httpx.AsyncClient | None = None
        try:
            runner_client = await _get_runner_client_for_resource_access(session_id)
        except OmnigentError as exc:
            _logger.info(
                "Skipping runner-side cleanup for %s; proceeding with server-side delete: %s",
                session_id,
                exc,
            )
        if runner_client is not None:
            try:
                await runner_client.delete(
                    f"/v1/sessions/{session_id}/resources",
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError):
                _logger.warning(
                    "Runner cleanup failed for %s, falling back",
                    session_id,
                )
        else:
            import contextlib

            from omnigent.runtime import get_terminal_registry

            with contextlib.suppress(RuntimeError):
                await get_terminal_registry().cleanup_conversation(session_id)
        # Session file cleanup.
        if file_store is not None and artifact_store is not None:
            deleted_file_ids = await asyncio.to_thread(
                file_store.delete_all_for_session, session_id
            )
            for fid in deleted_file_ids:
                await asyncio.to_thread(artifact_store.delete, fid)
        # Opt-in git worktree cleanup: only when delete_branch=true and
        # the session has a server-created worktree. Runs after runner
        # teardown; best-effort (designs/SESSION_GIT_WORKTREE.md).
        if (
            delete_branch
            and conv.git_branch is not None
            and conv.workspace is not None
            and conv.host_id is not None
        ):
            await _remove_session_worktree_best_effort(
                host_id=conv.host_id,
                worktree_path=conv.workspace,
                branch=conv.git_branch,
                delete_branch=True,
                request=request,
                reason="session-delete",
            )
        _interrupt_fenced_sessions.discard(session_id)
        _intentional_stop_sessions.discard(session_id)
        deleted = await conversation_store.delete_conversation(session_id)
        if not deleted:
            raise _session_not_found()
        # The session is gone, so is its launch-progress state. Failed
        # launches are retained in the cache for reload visibility while
        # the session exists; without this eviction every deleted
        # failed-launch session would leak one entry for the process
        # lifetime.
        _session_sandbox_status_cache.pop(session_id, None)
        # Same for MCP startup state: failed/cancelled maps are retained
        # for reload visibility while the session exists, so a session
        # whose MCP startup never settled clean would leak its entry.
        _session_mcp_startup_cache.pop(session_id, None)
        # Same for the extension-pushed model catalog: kept across reloads
        # while the session exists (the extension only pushes on start), so a
        # deleted session would otherwise leak its entry for the process life.
        _pushed_model_options_cache.pop(session_id, None)
        # Drop the deleted session's per-user read-state from every user's
        # caches so they don't accumulate orphan entries for the process
        # lifetime.
        _prune_session_read_state(session_id)
        # Same for the tracker's entry — a deleted session's launch can
        # never be rendezvoused again (access checks 404 first), so a
        # retained failure is dead weight. ``finish`` also settles a
        # still-in-flight entry, releasing any parked message POST into
        # its session re-read (which now correctly 404s); the background
        # task's later ``fail`` on the popped entry is a no-op.
        managed_launches_for_delete = getattr(request.app.state, "managed_launches", None)
        if managed_launches_for_delete is not None:
            managed_launches_for_delete.finish(session_id)
        # Managed-host cleanup: when the session's host is backed by a
        # server-provisioned sandbox (host_type="managed"), terminate
        # the sandbox and delete the host row — which also revokes its
        # launch token. Best-effort by design — the provider's lifetime
        # cap reaps stragglers. External (laptop) hosts have no
        # sandbox_id and are never touched.
        host_store_for_managed = getattr(request.app.state, "host_store", None)
        if conv.host_id is not None and host_store_for_managed is not None:
            bound_host = await asyncio.to_thread(host_store_for_managed.get_host, conv.host_id)
            if bound_host is not None and bound_host.sandbox_id is not None:
                from omnigent.server.managed_hosts import terminate_managed_host

                await terminate_managed_host(
                    bound_host,
                    host_store_for_managed,
                    # Supplies the launcher for the provider-side
                    # terminate; None (config removed since launch)
                    # still deletes the row and revokes the token.
                    getattr(request.app.state, "sandbox_config", None),
                )
        try:
            import hashlib as _hashlib
            import time as _time

            _srv_id = _get_installation_id()
            _anon_d: str | None = None
            if user_id is not None:
                _salt_d = f"{_srv_id}:{user_id}" if _srv_id else user_id
                _anon_d = _hashlib.sha256(_salt_d.encode()).hexdigest()[:16]
            _usage = conv.session_usage or {}
            _duration: float | None = None
            with contextlib.suppress(Exception):
                _duration = _time.time() - conv.created_at
            _tel_emit(
                _TelSessionDeletedEvent(
                    session_id=session_id,
                    installation_id=_srv_id,
                    anon_user_id=_anon_d,
                    duration_seconds=_duration,
                    input_tokens=_usage.get("input_tokens"),
                    output_tokens=_usage.get("output_tokens"),
                    total_cost_usd=_usage.get("total_cost_usd"),
                )
            )
        except Exception:  # noqa: BLE001 — telemetry is best-effort
            pass
        return ConversationDeleted(id=session_id)

    # ── Permission management endpoints ──────────────────────────

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

    # ── Agent sub-resource ────────────────────────────────────────
    # These endpoints expose the session's bound agent metadata
    # and bundle through the session namespace, removing the need
    # for a standalone ``/api/agents`` router.

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
                    SkillSummary(name=s.name, description=s.description)
                    for s in loaded.spec.skills
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
            except Exception:  # noqa: BLE001 — spec load failure must not break agent fetch
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
        except Exception:  # noqa: BLE001 — catch all JSON parse failures
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

    return router
