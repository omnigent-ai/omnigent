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
from typing import TYPE_CHECKING, Annotated, Any

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
    get_agent_cache as _runtime_get_agent_cache,
)
from omnigent.runtime import (
    get_caps as _runtime_get_caps,
)
from omnigent.runtime import (
    get_policy_store,
    pending_elicitations,
    pending_inputs,
    user_session_stream,
)
from omnigent.runtime import (
    session_stream as session_stream,
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
    get_user_id as _auth_get_user_id,
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
# isort: off
from omnigent.server.routes._sessions.common import (
    COST_CONTROL_OVERRIDE_VALUES as COST_CONTROL_OVERRIDE_VALUES,
    _ALLOWED_EVENT_TYPES as _ALLOWED_EVENT_TYPES,
    _ANTIGRAVITY_NATIVE_ELICITATION_HOOK_TIMEOUT_S as _ANTIGRAVITY_NATIVE_ELICITATION_HOOK_TIMEOUT_S,
    _APPROVAL_TYPE as _APPROVAL_TYPE,
    _BROWSER_ACTION_AWAIT_S as _BROWSER_ACTION_AWAIT_S,
    _BROWSER_ACTION_CLAIM_GRACE_S as _BROWSER_ACTION_CLAIM_GRACE_S,
    _BROWSER_ACTION_NO_RENDERER_RESULT as _BROWSER_ACTION_NO_RENDERER_RESULT,
    _BROWSER_ACTION_TIMEOUT_RESULT as _BROWSER_ACTION_TIMEOUT_RESULT,
    _CHILD_PREVIEW_LIMIT as _CHILD_PREVIEW_LIMIT,
    _CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY as _CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY,
    _CLAUDE_NATIVE_EDIT_TOOLS as _CLAUDE_NATIVE_EDIT_TOOLS,
    _CLAUDE_NATIVE_HARNESS as _CLAUDE_NATIVE_HARNESS,
    _CLAUDE_NATIVE_MESSAGE_TIMEOUT_S as _CLAUDE_NATIVE_MESSAGE_TIMEOUT_S,
    _CLAUDE_NATIVE_MODEL as _CLAUDE_NATIVE_MODEL,
    _CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S as _CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
    _CLAUDE_NATIVE_PERMISSION_MODES as _CLAUDE_NATIVE_PERMISSION_MODES,
    _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY as _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY,
    _CLAUDE_NATIVE_REMEMBER_INELIGIBLE_TOOLS as _CLAUDE_NATIVE_REMEMBER_INELIGIBLE_TOOLS,
    _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY as _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY,
    _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE as _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
    _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY as _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY,
    _CLAUDE_NATIVE_UI_LABEL_KEY as _CLAUDE_NATIVE_UI_LABEL_KEY,
    _CLAUDE_NATIVE_UI_LABEL_VALUE as _CLAUDE_NATIVE_UI_LABEL_VALUE,
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
    _CODEX_NATIVE_COLLABORATION_MODES as _CODEX_NATIVE_COLLABORATION_MODES,
    _CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY as _CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY,
    _CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S as _CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S,
    _CODEX_NATIVE_HARNESS as _CODEX_NATIVE_HARNESS,
    _CODEX_NATIVE_MODEL as _CODEX_NATIVE_MODEL,
    _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK as _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK,
    _CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY as _CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY as _CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY as _CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY as _CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY as _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY as _CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY,
    _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE as _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
    _CODEX_NATIVE_WRAPPER_LABEL_VALUE as _CODEX_NATIVE_WRAPPER_LABEL_VALUE,
    _COMPACT_TYPE as _COMPACT_TYPE,
    _CURSOR_FORK_HISTORY_HARNESSES as _CURSOR_FORK_HISTORY_HARNESSES,
    _CURSOR_NATIVE_HARNESS as _CURSOR_NATIVE_HARNESS,
    _CURSOR_NATIVE_PERMISSION_HOOK_TIMEOUT_S as _CURSOR_NATIVE_PERMISSION_HOOK_TIMEOUT_S,
    _CURSOR_NATIVE_WRAPPER_LABEL_VALUE as _CURSOR_NATIVE_WRAPPER_LABEL_VALUE,
    _DENY_SENTINEL_PREFIX as _DENY_SENTINEL_PREFIX,
    _EVALUATE_HOOK_ELICITATION_ID_RE as _EVALUATE_HOOK_ELICITATION_ID_RE,
    _EXTERNAL_ASSISTANT_MESSAGE_TYPE as _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
    _EXTERNAL_CODEX_APPROVAL_MODE_CHANGE_TYPE as _EXTERNAL_CODEX_APPROVAL_MODE_CHANGE_TYPE,
    _EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE as _EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE,
    _EXTERNAL_CODEX_SUBAGENT_START_TYPE as _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
    _EXTERNAL_COMPACTION_STATUS_TYPE as _EXTERNAL_COMPACTION_STATUS_TYPE,
    _EXTERNAL_COMPACTION_STATUS_VALUES as _EXTERNAL_COMPACTION_STATUS_VALUES,
    _EXTERNAL_CONVERSATION_ITEM_TYPE as _EXTERNAL_CONVERSATION_ITEM_TYPE,
    _EXTERNAL_ELICITATION_RESOLVED_TYPE as _EXTERNAL_ELICITATION_RESOLVED_TYPE,
    _EXTERNAL_MCP_STARTUP_STATUS_VALUES as _EXTERNAL_MCP_STARTUP_STATUS_VALUES,
    _EXTERNAL_MCP_STARTUP_TYPE as _EXTERNAL_MCP_STARTUP_TYPE,
    _EXTERNAL_MODEL_CHANGE_TYPE as _EXTERNAL_MODEL_CHANGE_TYPE,
    _EXTERNAL_MODEL_OPTIONS_TYPE as _EXTERNAL_MODEL_OPTIONS_TYPE,
    _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE as _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE,
    _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE as _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
    _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE as _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
    _EXTERNAL_SESSION_INTERRUPTED_TYPE as _EXTERNAL_SESSION_INTERRUPTED_TYPE,
    _EXTERNAL_SESSION_STATUS_TYPE as _EXTERNAL_SESSION_STATUS_TYPE,
    _EXTERNAL_SESSION_STATUS_VALUES as _EXTERNAL_SESSION_STATUS_VALUES,
    _EXTERNAL_SESSION_SUPERSEDED_TYPE as _EXTERNAL_SESSION_SUPERSEDED_TYPE,
    _EXTERNAL_SESSION_TITLE_TYPE as _EXTERNAL_SESSION_TITLE_TYPE,
    _EXTERNAL_SESSION_TODOS_TYPE as _EXTERNAL_SESSION_TODOS_TYPE,
    _EXTERNAL_SESSION_USAGE_TYPE as _EXTERNAL_SESSION_USAGE_TYPE,
    _EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT as _EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT,
    _EXTERNAL_SUBAGENT_START_TYPE as _EXTERNAL_SUBAGENT_START_TYPE,
    _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE as _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE,
    _FENCE_EXEMPT_EVENT_TYPES as _FENCE_EXEMPT_EVENT_TYPES,
    _FORK_HISTORY_NATIVE_HARNESSES as _FORK_HISTORY_NATIVE_HARNESSES,
    _HARNESS_ELICITATION_REPARK_GRACE_S as _HARNESS_ELICITATION_REPARK_GRACE_S,
    _HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES as _HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES,
    _HARNESS_PRE_RESOLVED_ELICITATION_TTL_S as _HARNESS_PRE_RESOLVED_ELICITATION_TTL_S,
    _HOOK_ELICITATION_ID_RE as _HOOK_ELICITATION_ID_RE,
    _HOST_BOUND_RUNNER_CONNECT_GRACE_S as _HOST_BOUND_RUNNER_CONNECT_GRACE_S,
    _HOST_LAUNCH_RESULT_TIMEOUT_S as _HOST_LAUNCH_RESULT_TIMEOUT_S,
    _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S as _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
    _HOST_RUNNER_STATUS_TIMEOUT_S as _HOST_RUNNER_STATUS_TIMEOUT_S,
    _INTERRUPT_TYPE as _INTERRUPT_TYPE,
    _KIRO_NATIVE_WRAPPER_LABEL_VALUE as _KIRO_NATIVE_WRAPPER_LABEL_VALUE,
    _LABEL_VALUE_MAX_LEN as _LABEL_VALUE_MAX_LEN,
    _LAST_CONTEXT_TOKENS_LABEL_KEY as _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY as _LAST_CONTEXT_WINDOW_LABEL_KEY,
    _LAST_TASK_ERROR_CODE_LABEL_KEY as _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY as _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _MANAGED_RESUMABLE_TUNNEL_STALE_S as _MANAGED_RESUMABLE_TUNNEL_STALE_S,
    _MAX_TERMINAL_LAUNCH_ARGS as _MAX_TERMINAL_LAUNCH_ARGS,
    _MAX_TERMINAL_LAUNCH_ARG_LEN as _MAX_TERMINAL_LAUNCH_ARG_LEN,
    _MCP_ELICITATION_TYPE as _MCP_ELICITATION_TYPE,
    _MODEL_OPTIONS_ENDPOINT_BY_WRAPPER as _MODEL_OPTIONS_ENDPOINT_BY_WRAPPER,
    _MODEL_OPTIONS_RETRY_DELAYS_S as _MODEL_OPTIONS_RETRY_DELAYS_S,
    _MODEL_TOKEN_KEYS as _MODEL_TOKEN_KEYS,
    _NATIVE_PERMISSION_HOOK_TIMEOUT_S as _NATIVE_PERMISSION_HOOK_TIMEOUT_S,
    _NATIVE_POLICY_NOT_ENFORCED_CODE as _NATIVE_POLICY_NOT_ENFORCED_CODE,
    _NATIVE_TERMINAL_ENSURE_FAILED_CODE as _NATIVE_TERMINAL_ENSURE_FAILED_CODE,
    _NATIVE_TERMINAL_START_FAILED_CODE as _NATIVE_TERMINAL_START_FAILED_CODE,
    _OPENCODE_NATIVE_WRAPPER_LABEL_VALUE as _OPENCODE_NATIVE_WRAPPER_LABEL_VALUE,
    _PI_NATIVE_WRAPPER_LABEL_VALUE as _PI_NATIVE_WRAPPER_LABEL_VALUE,
    _RACE_TASK_REAP_TIMEOUT_S as _RACE_TASK_REAP_TIMEOUT_S,
    _RUNNER_CONVICTION_POLL_S as _RUNNER_CONVICTION_POLL_S,
    _RUNNER_FORWARD_TIMEOUT as _RUNNER_FORWARD_TIMEOUT,
    _RUNNER_RELAY_READY_TIMEOUT_S as _RUNNER_RELAY_READY_TIMEOUT_S,
    _RUNNER_SESSION_INIT_TIMEOUT_S as _RUNNER_SESSION_INIT_TIMEOUT_S,
    _SERVER_STREAM_EVENT_ADAPTER as _SERVER_STREAM_EVENT_ADAPTER,
    _SESSION_STREAM_HEARTBEAT_INTERVAL_S as _SESSION_STREAM_HEARTBEAT_INTERVAL_S,
    _SESSION_UPDATES_HEARTBEAT_INTERVAL_S as _SESSION_UPDATES_HEARTBEAT_INTERVAL_S,
    _SESSION_UPDATES_MAX_WATCHED as _SESSION_UPDATES_MAX_WATCHED,
    _SESSION_UPDATES_RESCAN_INTERVAL_S as _SESSION_UPDATES_RESCAN_INTERVAL_S,
    _SHARED_DISCOVERY_KEY as _SHARED_DISCOVERY_KEY,
    _SLASH_COMMAND_TYPE as _SLASH_COMMAND_TYPE,
    _SNAPSHOT_RUNNER_TIMEOUT_S as _SNAPSHOT_RUNNER_TIMEOUT_S,
    _STOP_RUNNER_RESULT_TIMEOUT_S as _STOP_RUNNER_RESULT_TIMEOUT_S,
    _STOP_SESSION_TYPE as _STOP_SESSION_TYPE,
    _RETRY_SESSION_TYPE as _RETRY_SESSION_TYPE,
    _SUBAGENT_FORWARD_RECONNECT_WAIT_S as _SUBAGENT_FORWARD_RECONNECT_WAIT_S,
    _TERMINAL_RESPONSE_EVENT_TYPES as _TERMINAL_RESPONSE_EVENT_TYPES,
    _TURN_ACTOR_LABEL as _TURN_ACTOR_LABEL,
    _UI_ADDED_AGENT_TITLE_PREFIX as _UI_ADDED_AGENT_TITLE_PREFIX,
    _UPLOAD_READ_CHUNK_BYTES as _UPLOAD_READ_CHUNK_BYTES,
    _WATCHER_TASKS as _WATCHER_TASKS,
    _MirroredToolCall as _MirroredToolCall,
    _PendingPolicyAskWrites as _PendingPolicyAskWrites,
    _RelayHandle as _RelayHandle,
    _browser_action_claim_events as _browser_action_claim_events,
    _browser_action_claims as _browser_action_claims,
    _browser_action_owners as _browser_action_owners,
    _browser_action_registry as _browser_action_registry,
    _deferred_elicitation_clear_tasks as _deferred_elicitation_clear_tasks,
    _intentional_stop_sessions as _intentional_stop_sessions,
    _interrupt_fenced_sessions as _interrupt_fenced_sessions,
    _logger as _logger,
    _managed_launch_tasks as _managed_launch_tasks,
    _model_options_cache as _model_options_cache,
    _model_options_inflight as _model_options_inflight,
    _model_options_stale as _model_options_stale,
    _native_ask_gate_locks as _native_ask_gate_locks,
    _native_popup_forward_tasks as _native_popup_forward_tasks,
    _pending_policy_ask_writes as _pending_policy_ask_writes,
    _pushed_model_options_cache as _pushed_model_options_cache,
    _read_explicit_unread as _read_explicit_unread,
    _read_last_seen as _read_last_seen,
    _recent_mirrored_tool_calls as _recent_mirrored_tool_calls,
    _runner_relay_tasks as _runner_relay_tasks,
    _runner_skills_cache as _runner_skills_cache,
    _runner_skills_inflight as _runner_skills_inflight,
    _server_host_registry as _server_host_registry,
    _server_runner_router as _server_runner_router,
    _session_active_response_cache as _session_active_response_cache,
    _session_background_task_count_cache as _session_background_task_count_cache,
    _session_background_tasks_cache as _session_background_tasks_cache,
    _session_mcp_startup_cache as _session_mcp_startup_cache,
    _session_sandbox_status_cache as _session_sandbox_status_cache,
    _session_status_cache as _session_status_cache,
    _session_terminal_pending_cache as _session_terminal_pending_cache,
    _session_todos_cache as _session_todos_cache,
    get_server_host_registry as get_server_host_registry,
    get_server_runner_router as get_server_runner_router,
    set_server_host_registry as set_server_host_registry,
    set_server_runner_router as set_server_runner_router,
)

# Lower-layer helpers (SSE builders, publishers, persistence, runner-forward
# primitives) live in _sessions.helpers.
from omnigent.server.routes._sessions.helpers import (
    FILE_CONTENT_CACHE_CONTROL as FILE_CONTENT_CACHE_CONTROL,
    SessionLiveness as SessionLiveness,
    _HostLaunchAttempt as _HostLaunchAttempt,
    _NativeTerminalEnsureOutcome as _NativeTerminalEnsureOutcome,
    _RunnerForwardResult as _RunnerForwardResult,
    _SessionEventDispatchResult as _SessionEventDispatchResult,
    _add_model_usage_delta as _add_model_usage_delta,
    _agent_carries_cursor_fork_history as _agent_carries_cursor_fork_history,
    _agent_carries_native_fork_history_impl as _agent_carries_native_fork_history_impl,
    _agent_is_native_impl as _agent_is_native_impl,
    _agent_provider_family as _agent_provider_family,
    _allow_all_edits_eligible as _allow_all_edits_eligible,
    _allow_remember_eligible as _allow_remember_eligible,
    _ancestor_session_ids as _ancestor_session_ids,
    _announce_session_added as _announce_session_added,
    _apply_liveness_to_items as _apply_liveness_to_items,
    _apply_pending_policy_ask_writes as _apply_pending_policy_ask_writes,
    _attachment_disposition as _attachment_disposition,
    _authorize_bundled_parent_and_inherit_runner as _authorize_bundled_parent_and_inherit_runner,
    _await_settled_managed_launch as _await_settled_managed_launch,
    _background_task_delivery_status as _background_task_delivery_status,
    _build_actor as _build_actor,
    _build_evaluation_context as _build_evaluation_context,
    _build_new_item as _build_new_item,
    _build_skill_slash_command_policy_body as _build_skill_slash_command_policy_body,
    _canonical_tool_input as _canonical_tool_input,
    _child_session_current_task_status_from_cached_status as _child_session_current_task_status_from_cached_status,
    _child_session_summary_from_conversation as _child_session_summary_from_conversation,
    _claude_native_remember_host as _claude_native_remember_host,
    _client_supplied_hook_elicitation_id as _client_supplied_hook_elicitation_id,
    _codex_plan_mode_enabled as _codex_plan_mode_enabled,
    _codex_subagent_display_tool as _codex_subagent_display_tool,
    _codex_subagent_labels_from_body as _codex_subagent_labels_from_body,
    _coerce_cumulative_field as _coerce_cumulative_field,
    _collect_descendant_conversation_ids as _collect_descendant_conversation_ids,
    _consume_pre_resolved_harness_elicitation as _consume_pre_resolved_harness_elicitation,
    _create_and_publish_codex_child as _create_and_publish_codex_child,
    _create_session_worktree as _create_session_worktree,
    _delete_stored_session_bundle_after_failure as _delete_stored_session_bundle_after_failure,
    _derive_terminal_launch_args_from_spec as _derive_terminal_launch_args_from_spec,
    _descendant_sessions as _descendant_sessions,
    _discovery_key as _discovery_key,
    _dispatch_skill_slash_command_to_runner as _dispatch_skill_slash_command_to_runner,
    _emit_server_routing_decision as _emit_server_routing_decision,
    _error_item_from_sse as _error_item_from_sse,
    _evaluate_output_policy as _evaluate_output_policy,
    _extract_assistant_text_from_event as _extract_assistant_text_from_event,
    _extract_claude_native_runner_failure as _extract_claude_native_runner_failure,
    _extract_persistent_item_from_sse as _extract_persistent_item_from_sse,
    _extract_user_text_for_routing as _extract_user_text_for_routing,
    _extract_user_text_from_event as _extract_user_text_from_event,
    _file_content_etag as _file_content_etag,
    _find_claude_native_subagent_child as _find_claude_native_subagent_child,
    _find_codex_native_subagent_child as _find_codex_native_subagent_child,
    _find_subagent_child_by_title as _find_subagent_child_by_title,
    _flush_relay_text as _flush_relay_text,
    _format_sse as _format_sse,
    _forward_approval_to_runner as _forward_approval_to_runner,
    _handle_advise_models_mcp as _handle_advise_models_mcp,
    _handle_external_session_todos as _handle_external_session_todos,
    _handle_mcp_tools_list as _handle_mcp_tools_list,
    _host_model_options_via_registry as _host_model_options_via_registry,
    _if_none_match_matches as _if_none_match_matches,
    _invalidate_runner_backed_snapshot_state as _invalidate_runner_backed_snapshot_state,
    _is_codex_native_subagent as _is_codex_native_subagent,
    _is_kiro_native_session as _is_kiro_native_session,
    _last_task_error_from_labels as _last_task_error_from_labels,
    _latest_assistant_text_from_store as _latest_assistant_text_from_store,
    _latest_message_preview as _latest_message_preview,
    _load_model_options as _load_model_options,
    _load_model_options_from_host as _load_model_options_from_host,
    _load_runner_skills as _load_runner_skills,
    _mcp_error_response as _mcp_error_response,
    _mcp_input_required_response as _mcp_input_required_response,
    _mcp_ok_response as _mcp_ok_response,
    _mcp_tool_result as _mcp_tool_result,
    _merge_pending_file_blocks as _merge_pending_file_blocks,
    _message_text as _message_text,
    _model_options_from_wire as _model_options_from_wire,
    _model_usage_bucket as _model_usage_bucket,
    _multipart_missing_detail as _multipart_missing_detail,
    _native_ask_gate_lock as _native_ask_gate_lock,
    _native_coding_agent_for_agent as _native_coding_agent_for_agent,
    _native_coding_agent_for_session as _native_coding_agent_for_session,
    _native_subagent_wrapper_labels_from_spec as _native_subagent_wrapper_labels_from_spec,
    _native_terminal_ensure_transport_error as _native_terminal_ensure_transport_error,
    _native_terminal_failure_from_runner_response as _native_terminal_failure_from_runner_response,
    _native_terminal_name_for_harness as _native_terminal_name_for_harness,
    _notify_runner_of_bundled_child as _notify_runner_of_bundled_child,
    _owner_from_grants as _owner_from_grants,
    _parse_external_assistant_message as _parse_external_assistant_message,
    _parse_external_conversation_item as _parse_external_conversation_item,
    _parse_session_create_metadata as _parse_session_create_metadata,
    _parse_skill_slash_command as _parse_skill_slash_command,
    _pending_elicitation_snapshot_for_session as _pending_elicitation_snapshot_for_session,
    _permission_level_from_grants as _permission_level_from_grants,
    _persist_external_assistant_message as _persist_external_assistant_message,
    _persist_external_codex_approval_mode_change as _persist_external_codex_approval_mode_change,
    _persist_external_codex_collaboration_mode_change as _persist_external_codex_collaboration_mode_change,
    _persist_external_model_change as _persist_external_model_change,
    _persist_external_model_options as _persist_external_model_options,
    _persist_external_reasoning_effort_change as _persist_external_reasoning_effort_change,
    _persist_external_session_title as _persist_external_session_title,
    _persist_external_subagent_start as _persist_external_subagent_start,
    _persist_native_policy_notice as _persist_native_policy_notice,
    _persist_policy_deny_sentinel as _persist_policy_deny_sentinel,
    _persist_session_status_error_labels as _persist_session_status_error_labels,
    _persist_stored_session_bundle as _persist_stored_session_bundle,
    _policy_notice_from_ensure_response as _policy_notice_from_ensure_response,
    _presentation_labels_for_agent_impl as _presentation_labels_for_agent_impl,
    _priced_cost_for_display as _priced_cost_for_display,
    _provision_managed_sandbox as _provision_managed_sandbox,
    _proxy_get_session_resources_to_runner as _proxy_get_session_resources_to_runner,
    _prune_pre_resolved_harness_elicitations as _prune_pre_resolved_harness_elicitations,
    _prune_session_read_state as _prune_session_read_state,
    _publish_and_persist_resource_event as _publish_and_persist_resource_event,
    _publish_changed_files_invalidated as _publish_changed_files_invalidated,
    _publish_collaboration_mode as _publish_collaboration_mode,
    _publish_compaction_completed as _publish_compaction_completed,
    _publish_compaction_failed as _publish_compaction_failed,
    _publish_compaction_in_progress as _publish_compaction_in_progress,
    _publish_elicitation_request_to_ancestors as _publish_elicitation_request_to_ancestors,
    _publish_elicitation_resolved as _publish_elicitation_resolved,
    _publish_elicitation_resolved_to_ancestors as _publish_elicitation_resolved_to_ancestors,
    _publish_error_event as _publish_error_event,
    _publish_external_assistant_message as _publish_external_assistant_message,
    _publish_external_conversation_item as _publish_external_conversation_item,
    _publish_external_output_reasoning_delta as _publish_external_output_reasoning_delta,
    _publish_external_output_text_delta as _publish_external_output_text_delta,
    _publish_external_tool_output_delta as _publish_external_tool_output_delta,
    _publish_input_consumed as _publish_input_consumed,
    _publish_input_deny_terminal as _publish_input_deny_terminal,
    _publish_interrupted as _publish_interrupted,
    _publish_mcp_startup as _publish_mcp_startup,
    _publish_model_options as _publish_model_options,
    _publish_policy_denied as _publish_policy_denied,
    _publish_policy_deny as _publish_policy_deny,
    _publish_runner_skills as _publish_runner_skills,
    _publish_session_created as _publish_session_created,
    _publish_session_superseded as _publish_session_superseded,
    _publish_status as _publish_status,
    _publish_terminal_pending as _publish_terminal_pending,
    _query_host_runner_status as _query_host_runner_status,
    _read_state_entry as _read_state_entry,
    _read_upload_capped as _read_upload_capped,
    _record_daily_cost as _record_daily_cost,
    _registered_runner_id as _registered_runner_id,
    _reject_reserved_cost_control_label_seed as _reject_reserved_cost_control_label_seed,
    _reject_server_reserved_label_seed as _reject_server_reserved_label_seed,
    _relay_persist as _relay_persist,
    _relay_persist_error_once as _relay_persist_error_once,
    _remove_session_worktree_best_effort as _remove_session_worktree_best_effort,
    _repl_terminal_ui_labels as _repl_terminal_ui_labels,
    _replace_text_in_message_body as _replace_text_in_message_body,
    _require_collaboration_mode_forward as _require_collaboration_mode_forward,
    _require_cost_control_label_authority as _require_cost_control_label_authority,
    _require_declared_subagent as _require_declared_subagent,
    _require_external_status_forward as _require_external_status_forward,
    _require_host_conn_for_worktree as _require_host_conn_for_worktree,
    _require_permission_mode_forward as _require_permission_mode_forward,
    _reset_runner_resources_after_switch_impl as _reset_runner_resources_after_switch_impl,
    _resolve_llm_model as _resolve_llm_model,
    _resolve_skill_meta_text_via_runner as _resolve_skill_meta_text_via_runner,
    _resolve_subagent_spec as _resolve_subagent_spec,
    _resource_event_item_from_sse as _resource_event_item_from_sse,
    _routing_decision_item_from_sse as _routing_decision_item_from_sse,
    _same_provider_family_impl as _same_provider_family_impl,
    _seed_missing_title as _seed_missing_title,
    _seed_missing_title_from_user_message as _seed_missing_title_from_user_message,
    _session_status_from_cache as _session_status_from_cache,
    _session_status_with_child_rollup as _session_status_with_child_rollup,
    _set_read_state as _set_read_state,
    _signal_harness_elicitation_resolved_by_id as _signal_harness_elicitation_resolved_by_id,
    _spec_config_flag_explicitly_disabled as _spec_config_flag_explicitly_disabled,
    _spec_harness as _spec_harness,
    _stop_session_host_runner as _stop_session_host_runner,
    _stored_file_to_resource as _stored_file_to_resource,
    _stream_live_events as _stream_live_events,
    _structured_ask_user_question as _structured_ask_user_question,
    _targeted_elicitation_event as _targeted_elicitation_event,
    _title_content_from_item as _title_content_from_item,
    _truncate_label as _truncate_label,
    _usage_by_model_for_display as _usage_by_model_for_display,
    _utc_day as _utc_day,
    _validate_external_reasoning_effort as _validate_external_reasoning_effort,
    _validate_session_workspace as _validate_session_workspace,
    _validate_terminal_launch_args as _validate_terminal_launch_args,
    _validated_cost_control_mode_override as _validated_cost_control_mode_override,
    _validated_harness_override as _validated_harness_override,
    _validated_harness_override_executor_type as _validated_harness_override_executor_type,
    _wait_for_managed_runner_tunnel as _wait_for_managed_runner_tunnel,
    announce_hosts_changed as announce_hosts_changed,
    cancel_managed_launch_tasks as cancel_managed_launch_tasks,
    prefetch_session_routing_catalogs as prefetch_session_routing_catalogs,
)

# Runner-forward / ASK-gate helpers are patched by tests on this facade module
# (``monkeypatch(sessions.<X>)``). Their real bodies live in the package as
# ``<X>_impl`` and the siblings call a lazy proxy that resolves the attribute
# here at call time, so a facade patch is honored across module boundaries.
# Bind the real bodies here rather than importing the proxies, so the facade
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
from omnigent.server.routes._sessions.orchestration import (
    RUNNER_DISCONNECT_GRACE_S as RUNNER_DISCONNECT_GRACE_S,
    _accumulate_session_usage as _accumulate_session_usage,
    _best_effort_stop as _best_effort_stop,
    _bind_and_launch_managed_runner as _bind_and_launch_managed_runner,
    _build_native_terminal_message_event as _build_native_terminal_message_event,
    _build_session_list_item as _build_session_list_item,
    _build_session_response as _build_session_response,
    _child_session_summaries_from_conversations as _child_session_summaries_from_conversations,
    _create_session_from_bundle as _create_session_from_bundle,
    _create_session_from_existing_agent as _create_session_from_existing_agent,
    _drive_terminal_resolved_elicitation as _drive_terminal_resolved_elicitation,
    _enrich_terminal_status_with_subagent_output as _enrich_terminal_status_with_subagent_output,
    _ensure_native_terminal_ready as _ensure_native_terminal_ready,
    _ensure_runner_relay as _ensure_runner_relay,
    _ensure_runner_session_initialized as _ensure_runner_session_initialized,
    _evaluate_input_policy as _evaluate_input_policy,
    _evaluate_tool_call_policy as _evaluate_tool_call_policy,
    _fetch_model_options as _fetch_model_options,
    _fetch_runner_skills as _fetch_runner_skills,
    _forward_event_to_runner as _forward_event_to_runner,
    _forward_native_subagent_terminal_failure as _forward_native_subagent_terminal_failure,
    _forward_native_terminal_message as _forward_native_terminal_message,
    _get_session_snapshot as _get_session_snapshot,
    _handle_mcp_tools_call as _handle_mcp_tools_call,
    _heal_subagent_runner_binding_via_parent as _heal_subagent_runner_binding_via_parent,
    _is_native_terminal_session as _is_native_terminal_session,
    _kick_managed_relaunch as _kick_managed_relaunch,
    _labels_for_viewer as _labels_for_viewer,
    _maybe_relaunch_managed_sandbox as _maybe_relaunch_managed_sandbox,
    _maybe_wake_stale_resumable_managed_sandbox as _maybe_wake_stale_resumable_managed_sandbox,
    _native_subagent_wrapper_labels as _native_subagent_wrapper_labels,
    _native_terminal_runtime as _native_terminal_runtime,
    _persist_external_codex_subagent_start as _persist_external_codex_subagent_start,
    _persist_external_conversation_item as _persist_external_conversation_item,
    _persist_external_session_usage as _persist_external_session_usage,
    _persist_host_launch_failure_turn as _persist_host_launch_failure_turn,
    _persist_model_change_note as _persist_model_change_note,
    _persist_native_cumulative_usage as _persist_native_cumulative_usage,
    _persist_native_terminal_failure as _persist_native_terminal_failure,
    _persist_session_event as _persist_session_event,
    _persist_skipped_kiro_pending_input as _persist_skipped_kiro_pending_input,
    _publish_and_wait_for_harness_elicitation as _publish_and_wait_for_harness_elicitation,
    _publish_subtree_cost_to_ancestors as _publish_subtree_cost_to_ancestors,
    _recover_subagent_status_forward_via_parent as _recover_subagent_status_forward_via_parent,
    _register_policy_elicitation as _register_policy_elicitation,
    _relay_runner_stream as _relay_runner_stream,
    _resolve_elicitation as _resolve_elicitation,
    _run_managed_launch as _run_managed_launch,
    _run_managed_wake as _run_managed_wake,
    _runner_reject_detail as _runner_reject_detail,
    _schedule_deferred_elicitation_clear as _schedule_deferred_elicitation_clear,
    _spawn_native_approval_popup_forward as _spawn_native_approval_popup_forward,
    _spawn_native_blocked_notice_forward as _spawn_native_blocked_notice_forward,
    _wait_for_host_bound_runner_client as _wait_for_host_bound_runner_client,
    _wake_parent_for_blocked_child as _wake_parent_for_blocked_child,
    configure_subagent_block_notifier as configure_subagent_block_notifier,
    ensure_runner_connected as ensure_runner_connected,
)
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
    _mark_runner_sessions_offline_impl as _mark_runner_sessions_offline,
)
from omnigent.server.routes._sessions.orchestration import (
    _publish_runner_recovered_status_impl as _publish_runner_recovered_status,
)

# isort: on
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
    PolicyAction,
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

if TYPE_CHECKING:
    __all__ = [
        "_agent_carries_native_fork_history",
        "_agent_is_native",
        "_build_policy_engine_from_spec",
        "_dispatch_session_event_to_runner",
        "_ensure_runner_relay_ready",
        "_forward_session_change_to_runner",
        "_get_runner_client",
        "_get_runner_client_for_resource_access",
        "_hold_native_ask_gate",
        "_kick_managed_wake",
        "_launch_runner_on_host",
        "_load_agent_spec_for_session",
        "_mark_runner_sessions_offline",
        "_poll_request_disconnect",
        "_presentation_labels_for_agent",
        "_publish_runner_recovered_status",
        "_publish_sandbox_status",
        "_reset_runner_resources_after_switch",
        "_resolve_harness",
        "_same_provider_family",
        "_signal_terminal_resolved_harness_elicitation",
        "_stop_session_via_runner",
        "_wait_for_runner_client",
    ]


get_agent_cache = _runtime_get_agent_cache
get_caps = _runtime_get_caps
_get_user_id = _auth_get_user_id

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
        runner_tunnel_tokens=runner_tunnel_tokens,
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
