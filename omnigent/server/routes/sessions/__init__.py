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
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.runtime.policies.builder import (
    any_policies_apply,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.server import presence

# Elicitation-registry state and dataclasses. Tests reach these through this
# facade module (``sessions._ParkedHarnessElicitation`` etc.); re-export them so
# the module namespace matches the pre-split file.
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
from omnigent.server.routes._sessions.common import (
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
    _agent_carries_native_fork_history_impl as _agent_carries_native_fork_history,
)
from omnigent.server.routes._sessions.helpers import (
    _agent_is_native_impl as _agent_is_native,
)
from omnigent.server.routes._sessions.helpers import (
    _build_policy_engine_from_spec_impl as _build_policy_engine_from_spec,
)
from omnigent.server.routes._sessions.helpers import (
    _compact_lock_impl as _compact_lock,
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
    _poll_request_disconnect_impl as _poll_request_disconnect,
)
from omnigent.server.routes._sessions.helpers import (
    _presentation_labels_for_agent_impl as _presentation_labels_for_agent,
)
from omnigent.server.routes._sessions.helpers import (
    _publish_sandbox_status_impl as _publish_sandbox_status,
)
from omnigent.server.routes._sessions.helpers import (
    _reset_runner_resources_after_switch_impl as _reset_runner_resources_after_switch,
)
from omnigent.server.routes._sessions.helpers import (
    _resolve_harness_impl as _resolve_harness,
)
from omnigent.server.routes._sessions.helpers import (
    _same_provider_family_impl as _same_provider_family,
)
from omnigent.server.routes._sessions.helpers import (
    _signal_terminal_resolved_harness_elicitation_impl as _signal_terminal_resolved_harness_elicitation,
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
    _kick_managed_wake_impl as _kick_managed_wake,
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
    mcp_pool: ServerMcpPool | None = None,
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

    from omnigent.server.routes.sessions.routes_agent import register_agent_routes
    from omnigent.server.routes.sessions.routes_browser import register_browser_routes
    from omnigent.server.routes.sessions.routes_core import register_core_routes
    from omnigent.server.routes.sessions.routes_elicitations import register_elicitations_routes
    from omnigent.server.routes.sessions.routes_events import register_events_routes
    from omnigent.server.routes.sessions.routes_hooks import register_hooks_routes
    from omnigent.server.routes.sessions.routes_items import register_items_routes
    from omnigent.server.routes.sessions.routes_permissions import register_permissions_routes
    from omnigent.server.routes.sessions.routes_resources import register_resources_routes

    register_core_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        file_store=file_store,
        artifact_store=artifact_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        agent_cache=agent_cache,
        liveness_lookup=liveness_lookup,
        comment_store=comment_store,
        runner_tunnel_tokens=runner_tunnel_tokens,
        runner_exit_reports=runner_exit_reports,
        host_registry=host_registry,
        project_store=project_store,
        background_title_coordinator=background_title_coordinator,
    )

    register_hooks_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        agent_cache=agent_cache,
    )

    register_items_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        auth_provider=auth_provider,
        permission_store=permission_store,
    )

    register_resources_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        file_store=file_store,
        artifact_store=artifact_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        host_registry=host_registry,
    )

    register_browser_routes(
        router,
        conversation_store=conversation_store,
        auth_provider=auth_provider,
        permission_store=permission_store,
    )

    register_elicitations_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
    )

    register_events_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        file_store=file_store,
        artifact_store=artifact_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        agent_cache=agent_cache,
        liveness_lookup=liveness_lookup,
        runner_exit_reports=runner_exit_reports,
        host_registry=host_registry,
        background_title_coordinator=background_title_coordinator,
    )

    register_permissions_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        auth_provider=auth_provider,
        permission_store=permission_store,
        agent_cache=agent_cache,
    )

    register_agent_routes(
        router,
        conversation_store=conversation_store,
        agent_store=agent_store,
        artifact_store=artifact_store,
        runner_router=runner_router,
        auth_provider=auth_provider,
        permission_store=permission_store,
        agent_cache=agent_cache,
    )

    return router
