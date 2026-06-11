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
import logging
import mimetypes
import re
import secrets
import time
import urllib.parse
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

import cachetools
import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
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
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.datastructures import UploadFile as StarletteUploadFile

from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.cost_plan import (
    COST_CONTROL_LABEL_NAMESPACE,
    reserved_cost_control_keys,
)
from omnigent.db.utils import generate_agent_id, generate_task_id
from omnigent.entities import (
    Agent,
    CommentsFingerprint,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StoredFile,
    synthesize_conversation_title,
)
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
    FunctionCallData,
    FunctionCallOutputData,
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.entities.session_resources import session_resource_view_to_dict
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.model_override import validate_model_override
from omnigent.policies.types import (
    ElicitationRequest,
    EvaluationContext,
    PolicyAction,
    PolicyResult,
)
from omnigent.reasoning_effort import (
    EFFORT_CLEAR_VALUES,
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runtime import (
    get_agent_cache,
    get_caps,
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
    session_stream,
    user_session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import (
    _ELICITATION_MODE,
    build_elicitation_request_event,
    resolve_ask_timeout,
)
from omnigent.runtime.policies.builder import build_policy_engine, load_session_usage
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server import presence
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
    local_single_user_enabled,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.managed_hosts import (
    ManagedHostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    RepoWorkspace,
)
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
from omnigent.server.routes._host_worktree import CreatedWorktree
from omnigent.server.schemas import (
    AgentObject,
    ChildSessionSummary,
    ConversationDeleted,
    CreatedSessionResponse,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    ErrorDetail,
    ErrorEvent,
    GrantPermissionRequest,
    MCPServerSummary,
    ModelUsage,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    PaginatedList,
    PermissionObject,
    PolicySummary,
    SandboxStatus,
    ServerStreamEvent,
    SessionAgentChangedEvent,
    SessionCreatedEvent,
    SessionCreateMetadata,
    SessionCreateRequest,
    SessionEventInput,
    SessionForkRequest,
    SessionGitOptions,
    SessionInputConsumedEvent,
    SessionInputConsumedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    SessionLabelsResponse,
    SessionListItem,
    SessionModelEvent,
    SessionResourceListPage,
    SessionResourceObject,
    SessionResourcePaginatedList,
    SessionResponse,
    SessionSandboxStatusEvent,
    SessionSkillsEvent,
    SessionStatusEvent,
    SessionSwitchAgentRequest,
    SessionTerminalPendingEvent,
    SessionTodosEvent,
    SessionUsageEvent,
    SkillSummary,
    UpdateSessionRequest,
)
from omnigent.session_lifecycle import (
    is_session_closed,
    labels_with_closed_status,
    title_without_closed_marker,
)
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    Phase,
    PolicySpec,
    StateUpdate,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import (
    ConversationNotFoundError,
    NameAlreadyExistsError,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.tools.client_specified import parse_client_side_tool_specs

_logger = logging.getLogger(__name__)

# ── Module-level constants (rule 34) ──────────────────────────────

# Wire literal for the interrupt input type. Lives here so the
# dispatcher in ``post_event`` matches a single named constant rather
# than an inline string buried in conditional logic.
_INTERRUPT_TYPE: str = "interrupt"

# Wire literal for the approval input type — resolves an outstanding
# elicitation in-band on the session-keyed surface, so a client only
# has to know one URL (``/v1/sessions/{id}/events``) for every
# downward signal.
_APPROVAL_TYPE: str = "approval"
_MCP_ELICITATION_TYPE: str = "mcp_elicitation"

# Wire literal for explicit user-requested context compaction. Unlike
# normal item events, this is a control event: it does not persist a
# user message or dispatch a normal agent turn. The route runs the
# runtime compaction helper directly and publishes the same
# ``response.compaction.in_progress`` event the automatic path emits.
_COMPACT_TYPE: str = "compact"

# Structured visible command item used by the REPL for skill invocations.
# The server handles skill slash commands specially: it persists this
# visible metadata item, then sends the runner a hidden ``message`` with
# the actual skill instructions.
_SLASH_COMMAND_TYPE: str = "slash_command"

# Web-UI-initiated request to terminate a live session without
# deleting its conversation (the transcript stays viewable). The AP
# server stays harness-agnostic and forwards this to the bound runner,
# whose ``/events`` handler kills the external process for harnesses
# that have one (claude-native hard-kills its tmux pane) and 204s for
# in-process harnesses. Owner-only — terminating a session for every
# participant is a lifecycle action on par with delete, not an edit.
_STOP_SESSION_TYPE: str = "stop_session"

# Internal input used by terminal-backed integrations that observe an
# assistant response outside the Omnigent task runtime and need to
# persist/broadcast it into the session transcript without starting a
# duplicate agent turn.
_EXTERNAL_ASSISTANT_MESSAGE_TYPE: str = "external_assistant_message"

# Internal input used by terminal-backed integrations to append a
# semantic item observed outside the Omnigent task runtime. Unlike a
# normal ``message`` POST, this does not create or steer an agent task.
_EXTERNAL_CONVERSATION_ITEM_TYPE: str = "external_conversation_item"

# Internal input used by terminal-backed integrations to publish a live
# assistant text delta observed outside the Omnigent task runtime. The
# payload is transient SSE only and is intentionally not persisted; the
# corresponding completed message still arrives later via
# ``external_conversation_item``.
_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE: str = "external_output_text_delta"

# Internal input used by terminal-backed integrations to publish an
# explicit ``session.interrupted`` edge observed outside the Omnigent
# task runtime. Payload is empty.
_EXTERNAL_SESSION_INTERRUPTED_TYPE: str = "external_session_interrupted"

# Internal input used by Codex-native forwarders to clear a harness
# elicitation that another Codex client already answered. Payload:
# ``{"elicitation_id": "elicit_codex_..."}``.
_EXTERNAL_ELICITATION_RESOLVED_TYPE: str = "external_elicitation_resolved"

# Internal input used by terminal-backed integrations to publish a
# session.status event observed outside the Omnigent task runtime
# (e.g. ``omnigent claude`` mirroring Claude Code's Stop hook into
# the session stream so the web UI's idle/running indicator updates).
# Payload shape: ``{"status": "idle" | "running" | "waiting" | "failed"}``.
_EXTERNAL_SESSION_STATUS_TYPE: str = "external_session_status"
_EXTERNAL_SESSION_STATUS_VALUES: frozenset[str] = frozenset(
    {"idle", "running", "waiting", "failed"}
)
# Native transcript forwarders post completed assistant items immediately
# before ``external_session_status: idle``. Scanning the latest message
# window avoids a full transcript read while still tolerating tool/user
# records after the assistant item.
_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT: int = 1000

# Compaction-progress edge observed inside the Claude Code terminal
# (claude-native forwarder, from the ``PreCompact`` and post-compaction
# ``SessionStart source=compact`` hooks). Publishes the same
# ``response.compaction.in_progress`` / ``response.compaction.completed``
# SSE events the AP-side compaction path emits, so the web UI shows its
# "Compacting conversation…" spinner while Claude runs the real
# compaction in its terminal. Payload: ``{"status": "in_progress" |
# "completed" | "failed"}``. ``completed`` carries no token count — the
# context ring is updated separately by ``external_session_usage``.
_EXTERNAL_COMPACTION_STATUS_TYPE: str = "external_compaction_status"
_EXTERNAL_COMPACTION_STATUS_VALUES: frozenset[str] = frozenset(
    {"in_progress", "completed", "failed"}
)

# Usage update from a terminal-backed runtime (claude-native
# forwarder). Persists ``context_tokens`` / ``context_window`` as
# conversation labels and publishes a ``session.usage`` SSE event.
_EXTERNAL_SESSION_USAGE_TYPE: str = "external_session_usage"

# Active-model switch observed inside the Claude Code terminal (a
# ``/model`` command or the in-TUI picker). Persists ``model_override``
# on the conversation and publishes a ``session.model`` SSE event so the
# web model picker reflects the switch. Payload: ``{"model": "opus"}``.
_EXTERNAL_MODEL_CHANGE_TYPE: str = "external_model_change"

# Subagent-start signal from the claude-native forwarder. Claude Code
# spawns sub-agents internally (Task tool) and writes their transcripts
# to ``~/.claude/projects/.../subagents/agent-<id>.jsonl`` — there is
# no Claude Code hook fired when a sub-agent begins. The forwarder
# polls the on-disk directory and POSTs this event when a new
# ``.meta.json`` appears so the Omnigent server can mint a child Conversation
# row and surface it in the Subagents rail tab. Payload shape:
# ``{"subagent_id": "<claude-side id>", "agent_type": "Explore",
# "description": "...", "tool_use_id": "toolu_..."}``.
_EXTERNAL_SUBAGENT_START_TYPE: str = "external_subagent_start"
# Labels stamped on the new child Conversation. ``subagent_id`` is the
# stable Claude-side identifier used for idempotent retries — two POSTs
# carrying the same ``subagent_id`` resolve to the same child row.
# ``tool_use_id`` links back to the parent transcript's Task tool-use
# block so consumers can correlate sub-agent rows to the call that
# spawned them. The wrapper label distinguishes claude-native
# sub-agents from omnigent-spawned ones at the data layer.
_CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "claude-code-native-ui-subagent"
_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY = "omnigent.claude_native.subagent_id"
_CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY = "omnigent.claude_native.tool_use_id"
# Free-form human-readable description (the ``description`` field of
# the on-disk ``.meta.json``). Not used by the rail's display path
# today — Claude often passes the same string for many parallel
# sub-agents — but preserved as a label so debug surfaces / future
# UI work can read it without re-reading the meta file.
_CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY = "omnigent.claude_native.description"

# Subagent-start signal from the codex-native forwarder. Codex AgentControl
# emits ``collabAgentToolCall`` items when it spawns child threads. The
# forwarder converts each receiver thread into this event so Omnigent can mint a
# child Conversation row and surface it in the Subagents rail.
_EXTERNAL_CODEX_SUBAGENT_START_TYPE: str = "external_codex_subagent_start"
# Wrapper label value distinguishing Codex-internal children from both
# omnigent-spawned and claude-native sub-agents.
_CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "codex-native-ui-subagent"
# Label keys stamped on the child Conversation for display and idempotency.
_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.subagent_thread_id"
_CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.parent_thread_id"
_CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY = "omnigent.codex_native.collab_tool_call_id"
_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY = "omnigent.codex_native.prompt"
_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY = "omnigent.codex_native.agent_nickname"
_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY = "omnigent.codex_native.agent_role"
# Display name fallback when neither nickname nor role is available.
_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK = "Codex"
# Labels read by ``_get_session_snapshot`` to seed the ap-web ring on
# reload for sessions where no Omnigent task carries usage (claude-native).
_LAST_CONTEXT_TOKENS_LABEL_KEY: str = "omnigent.last_context_tokens"
_LAST_CONTEXT_WINDOW_LABEL_KEY: str = "omnigent.last_context_window"

# Todo-list update from the claude-native forwarder. Carries the raw
# todo items captured from PostToolUse/TodoWrite hook events. Payload
# shape: ``{"todos": [{"content": "...", "status": "...", "activeForm": ...}]}``.
_EXTERNAL_SESSION_TODOS_TYPE: str = "external_session_todos"

# Session labels stamped by ``omnigent claude``. A matching session
# is terminal-owned: Omnigent web-chat input must be forwarded to the local
# runner for tmux injection, and rendered transcript items must come
# back through ``external_conversation_item`` only.
_CLAUDE_NATIVE_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_CLAUDE_NATIVE_WRAPPER_LABEL_VALUE = "claude-code-native-ui"
# Marks a session as terminal-first in the Web UI (AppShell renders the
# Claude Code terminal pane via TerminalFirstContext). Stamped alongside
# the wrapper label so a claude-native session — created fresh by the
# new-session picker OR added to an existing session via "Add agent" —
# renders as a terminal without the client having to pass labels.
_CLAUDE_NATIVE_UI_LABEL_KEY = "omnigent.ui"
_CLAUDE_NATIVE_UI_LABEL_VALUE = "terminal"

_CLAUDE_NATIVE_HARNESS = "claude-native"
_CLAUDE_NATIVE_MODEL = "claude-native-ui"
_CODEX_NATIVE_WRAPPER_LABEL_VALUE = "codex-native-ui"
_CODEX_NATIVE_HARNESS = "codex-native"
_CODEX_NATIVE_MODEL = "codex-native-ui"
_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S = 30.0
_NATIVE_TERMINAL_START_FAILED_CODE = "native_terminal_start_failed"
_NATIVE_TERMINAL_ENSURE_FAILED_CODE = "native_terminal_ensure_failed"
# Banner code for the non-fatal notice shown when a native codex session
# starts but tool-call policy enforcement is NOT active (fail-open: codex
# too old, or the policy hook could not be trusted). The runner reports
# the reason once via ``policy_hook_disabled_reason`` in its
# terminal-ensure 200 response.
_NATIVE_POLICY_NOT_ENFORCED_CODE = "native_policy_not_enforced"
_HOST_BOUND_RUNNER_CONNECT_GRACE_S = 3.0
_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S = 30.0
# How often the runner-connect wait re-checks the crash-report store while
# racing the event-driven connect signal. Small enough that conviction is
# detected within a fraction of a second of the daemon's report, without
# busy-spinning.
_RUNNER_CONVICTION_POLL_S = 0.25
# Wait budget for the host's ``host.launch_runner`` RESULT frame on the
# relaunch path. The daemon replies as soon as it has spawned (or refused
# to spawn) the runner — a local CLI-on-PATH + credential check then a
# subprocess fork — so this is short. A refusal here (harness not
# configured) is surfaced as a transcript error; a launch proceeds to the
# longer connect wait below. On timeout we assume "launched" and fall
# through to the connect wait, preserving the prior fire-and-forget
# behavior rather than blocking the turn.
_HOST_LAUNCH_RESULT_TIMEOUT_S = 10.0
# Server-side wait budget for Claude's ``PermissionRequest`` hook. Set
# to one day so a native permission prompt waits ~indefinitely for the
# user to answer in EITHER the web UI or the terminal, rather than
# auto-resolving after a few minutes. The terminal prompt stays usable
# the whole time: answering it closes the hook connection, which the
# disconnect poll catches and resolves the web card. Kept in lockstep
# with the hook subprocess httpx budget (``_PERMISSION_TIMEOUT_S`` in
# ``claude_native_hook``) and Claude Code's own command-hook
# ``timeout`` (set in ``build_hook_settings``) so no single layer caps
# the wait first. Empty 2xx body on timeout → Claude defers to its
# built-in prompt (fail-ask).
_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S = 86400.0

# Tools whose prompts get the "Accept & allow all edits" UI affordance —
# the exact set ``acceptEdits`` mode auto-approves.
_CLAUDE_NATIVE_EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)


def _allow_all_edits_eligible(tool_name: str, permission_mode: str | None) -> bool:
    """
    Whether a claude-native PermissionRequest may offer / honor the
    "Accept & allow all edits" affordance.

    Eligible for file-editing tools under a mode that still prompts,
    and for ``ExitPlanMode`` — accepting a plan with the flag is the
    plan card's "Yes, and use auto mode" option (exit plan mode AND
    switch the session into Claude's ``auto`` mode).
    Already-permissive modes (``acceptEdits`` / ``bypassPermissions``)
    wouldn't prompt at all, so the switch would be inert. Used at BOTH
    the stamp site (drives the UI button) and the verdict site (gates
    the ``setMode`` decision), so the server never honors a
    client-supplied ``allow_all_edits`` flag on a tool/mode the
    affordance was never offered for.

    :param tool_name: The gated tool from Claude's PermissionRequest
        payload, e.g. ``"Edit"`` or ``"Bash"``.
    :param permission_mode: Claude's current permission mode from the
        payload, e.g. ``"default"`` / ``"plan"`` / ``"acceptEdits"`` /
        ``None`` when absent.
    :returns: ``True`` iff the affordance applies.
    """
    return (
        tool_name in _CLAUDE_NATIVE_EDIT_TOOLS or tool_name == "ExitPlanMode"
    ) and permission_mode not in (
        "acceptEdits",
        "bypassPermissions",
    )


# Server-side wait budget for Codex app-server requests forwarded by
# ``omnigent codex``. Held at one day like the Claude permission hook:
# a terminal-side answer ends the wait early via the app-server's
# explicit ``serverRequest/resolved`` notification, so the long park
# never blocks the TUI path — while the old 300s cap silently abandoned
# any prompt a headless sub-agent left unanswered for >5 minutes.
_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S = 86400.0

# ``external_elicitation_resolved`` can arrive just before the matching
# Codex hook registers, and a web verdict can land between a severed
# long-poll and its retry. Pinned, NOT the hook wait budget: Codex ids
# are deterministic per (session, method, rpc id) and rpc ids reset on
# app-server restart, so a long-lived tombstone could replay a stale
# verdict onto an unrelated future prompt. 300s covers both gap kinds;
# the entry cap keeps bogus ids from growing the process forever.
_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S = 300.0
_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES = 1024

# Grace between a verdict-less hook wait ending and the card-clearing
# resolved publish — lets the hook's retry re-park the same id instead
# of wiping a still-blocked prompt; a dead hook still clears after it.
_HARNESS_ELICITATION_REPARK_GRACE_S = 10.0

# Client-supplied re-attach ids, namespaced so they cannot collide
# with Codex deterministic ids or server-minted ids.
_CLAUDE_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_claude_[0-9a-f]{32}$")

# Cap on reaping a cancelled disconnect/terminal-resolved race task in
# the harness-elicitation gate's cleanup. Reaping normally completes in
# one loop tick; the cap exists because a cancellation that lands while
# the target is inside an anyio cancel scope can be coalesced with the
# scope's own cancellation and swallowed, and an unbounded
# ``await race_task`` then wedges the gate for its full timeout (24h on
# the claude hook path).
_RACE_TASK_REAP_TIMEOUT_S = 5.0
# Cadence for ``session.heartbeat`` keepalive events on
# ``GET /v1/sessions/{id}/stream`` (see :func:`_stream_live_events`).
# A stream that sits idle between turns has nothing crossing the wire,
# which lets a half-open socket go undetected for the client's full
# SSE read-timeout (10 minutes in the SDK). 15s mirrors the per-turn
# ``response.heartbeat`` cadence and is short enough to recover from
# a laptop-sleep-induced half-open socket within one user typing-step.
_SESSION_STREAM_HEARTBEAT_INTERVAL_S = 15.0
# Cap each runner-touching snapshot-on-connect gather so a slow/unavailable
# runner can never delay the live tail (the conversation snapshot endpoint
# remains the primary reconcile path).
_SNAPSHOT_RUNNER_TIMEOUT_S = 2.0
# Maximum time Omnigent waits for its runner->AP SSE relay to observe the
# runner stream's ready heartbeat before forwarding a no-replay input
# event. A timeout fails loud instead of accepting a prompt whose fast
# output could be dropped before the relay is subscribed.
_RUNNER_RELAY_READY_TIMEOUT_S = 5.0

# Set of event ``type`` values the route accepts on POST /events.
# Two are special-cased and bypass the normal item-persist path:
#   ``interrupt`` → cancel active task + publish ``session.interrupted``
#   ``approval`` → resolve the outstanding elicitation Future
#   ``external_assistant_message`` → append/broadcast terminal-observed output
#   ``external_conversation_item`` → append/broadcast a terminal-observed item
#   ``external_session_interrupted`` → publish terminal-observed interruption
# Everything else must be a known item type from the conversation
# entity's discriminator map (``message``, ``function_call_output``,
# etc.) so the agent loop can rehydrate it via ``parse_item_data``.
# Anything not in this set is a client mistake — fail loud with 400
# at the route boundary rather than persist an item the consumer can
# only crash on later.
_ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(ITEM_TYPE_TO_DATA_CLS.keys()) | {
    _INTERRUPT_TYPE,
    _APPROVAL_TYPE,
    _MCP_ELICITATION_TYPE,
    _COMPACT_TYPE,
    _STOP_SESSION_TYPE,
    _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
    _EXTERNAL_CONVERSATION_ITEM_TYPE,
    _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
    _EXTERNAL_SESSION_INTERRUPTED_TYPE,
    _EXTERNAL_ELICITATION_RESOLVED_TYPE,
    _EXTERNAL_SESSION_STATUS_TYPE,
    _EXTERNAL_SESSION_USAGE_TYPE,
    _EXTERNAL_COMPACTION_STATUS_TYPE,
    _EXTERNAL_MODEL_CHANGE_TYPE,
    _EXTERNAL_SESSION_TODOS_TYPE,
    _EXTERNAL_SUBAGENT_START_TYPE,
    _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
}

# Validates every dict that crosses the AP→client SSE boundary on
# the session stream. Built once at module load.
_SERVER_STREAM_EVENT_ADAPTER: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)

# Strong-references for per-session task watchers spawned via
# ``asyncio.create_task``. Without this, asyncio's task registry only
# holds a weak reference and the GC can collect a running task before
# it finishes (the well-known RUF006 / Python ``asyncio`` footgun).
# Entries are evicted by a done-callback on the task itself.
_WATCHER_TASKS: set[asyncio.Task[None]] = set()

# Per-session status cache updated by the runner SSE relay.
# Used by _get_session_snapshot.
_session_status_cache: dict[str, str] = {}

# Sessions whose current turn was Stopped: the relay drops the turn's trailing
# response.* output (no forward, no persist). The fence lifts on the next
# turn's "running" status or on any terminal response.* event.
_interrupt_fenced_sessions: set[str] = set()

# Turn-terminal response lifecycle events: the relay flushes buffered
# assistant text on each of these and resets its turn-scoped state.
_TERMINAL_RESPONSE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    }
)

# response.* events that pass the interrupt fence: elicitation lifecycle is
# pending-approvals bookkeeping, not turn output — swallowing a resolved event
# would leak a ghost approval card into every later session snapshot.
_FENCE_EXEMPT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.elicitation_request",
        "response.elicitation_resolved",
    }
)

# ── WS /v1/sessions/updates tuning ──────────────────────────────────
# How often the session-updates stream re-reads each connection's
# watched ids and diffs them against the last frame sent. This is the
# "poll" cadence, but server-side and per-connection: a frame is emitted
# only when something actually changed, so an idle list produces no
# traffic. Replaces the client's former 4 s HTTP poll of GET /v1/sessions.
_SESSION_UPDATES_RESCAN_INTERVAL_S: float = 4.0
# When a rescan produces no changes, emit a lightweight heartbeat frame
# at most this often so intermediaries (e.g. Databricks Apps ingress)
# don't reap the idle WebSocket and the client can detect a dead link.
_SESSION_UPDATES_HEARTBEAT_INTERVAL_S: float = 30.0
# Hard cap on the watch-set size a single connection may register, so a
# misbehaving or malicious client can't make the server fan out an
# unbounded per-interval batch of store reads.
_SESSION_UPDATES_MAX_WATCHED: int = 500
# Discovery key used for unauthenticated / single-user deployments (no
# permission store): every connection subscribes here and every create
# publishes here, so new sessions still push. Safe because in that mode all
# sessions are accessible to everyone, so there is no cross-user isolation to
# preserve. In multi-user mode the key is the authenticated user id instead,
# and a create publishes only to its owner's key.
_SHARED_DISCOVERY_KEY = "__all__"


def _discovery_key(user_id: str | None) -> str:
    """
    Map an (optional) user id to the :mod:`user_session_stream` channel key.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode.
    :returns: ``user_id`` when set, else :data:`_SHARED_DISCOVERY_KEY`.
    """
    return user_id if user_id is not None else _SHARED_DISCOVERY_KEY


def _announce_session_added(user_id: str | None, session_id: str) -> None:
    """
    Push a ``session_added`` discovery event to a user's updates streams.

    Called after a session becomes accessible to ``user_id`` (created, forked,
    or shared) so that user's open tabs surface it without a list poll. A no-op
    when the user has no stream connected.

    :param user_id: The user the session is now accessible to (the owner on
        create/fork, the grantee on share), or ``None`` in single-user mode.
    :param session_id: The newly-accessible session id, e.g. ``"conv_abc123"``.
    """
    user_session_stream.publish(
        _discovery_key(user_id), {"type": "session_added", "session_id": session_id}
    )


# Per-session todo cache updated by external_session_todos events from the
# claude-native forwarder. Used by _build_session_response to populate the
# ``todos`` snapshot field so the panel survives page refresh.
_session_todos_cache: dict[str, list[dict[str, Any]]] = {}

# Per-session terminal-spin-up flag updated by the runner SSE relay from
# ``session.terminal_pending`` events (and self-healed when a real terminal
# resource is created). Used by _build_session_response to populate the
# ``terminal_pending`` snapshot field so a client connecting mid-spin-up
# still sees the Terminal-pill spinner. Only ``True`` entries are stored —
# the key is deleted on clear so the dict never accumulates stale ``False``
# entries for every session that ever spun up a terminal.
_session_terminal_pending_cache: dict[str, bool] = {}
# Managed-sandbox launch progress keyed by session id. Written by
# _publish_sandbox_status as the background launch pipeline advances;
# read by _build_session_response to populate the ``sandbox_status``
# snapshot field so a client opening the session mid-launch sees the
# current stage. Successful launches are evicted on "ready" (absent ==
# no launch in flight); failures are retained — mirroring
# ManagedLaunchTracker — so a reload after a dead launch still shows
# why the sandbox never came up.
_session_sandbox_status_cache: dict[str, SandboxStatus] = {}
# Per-session runner-skills cache + in-flight fetch. The snapshot fetches
# these off its critical path (see _fetch_runner_skills) so the continuous
# poll can't pin the runner's event loop and wedge a turn.
_runner_skills_cache: dict[str, list[SkillSummary]] = {}
_runner_skills_inflight: dict[str, asyncio.Task[None]] = {}


@dataclass
class _MirroredToolCall:
    """
    Tool identity of a forwarder-mirrored ``function_call``.

    Cached by ``call_id`` so a later ``function_call_output`` (which
    carries only ``call_id`` + ``output``) can recover the tool it
    belongs to and correlate it to a parked permission prompt. See
    :data:`_recent_mirrored_tool_calls`.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :param tool_input: Parsed tool arguments, e.g.
        ``{"command": "ls"}``; ``{}`` when the arguments were absent or
        not a JSON object.
    """

    tool_name: str
    tool_input: dict[str, Any]


# call_id -> tool identity for recently mirrored ``function_call``
# items. The forwarder always posts a tool's ``function_call`` before
# its ``function_call_output``, so the entry is present when the output
# arrives. Bounded + LRU-evicting because tool calls are unbounded over
# a session's life and we only need each entry to bridge the gap to its
# own output (seconds). Used by _persist_external_conversation_item to
# drive the terminal-resolved elicitation fast path.
_recent_mirrored_tool_calls: cachetools.LRUCache[str, _MirroredToolCall] = cachetools.LRUCache(
    maxsize=2048
)


@dataclass(frozen=True)
class _PendingPolicyAskWrites:
    """Policy writes deferred until a relay-path tool-call ASK is approved.

    The relay / non-native tool-call gate (:func:`_evaluate_tool_call_policy`)
    parks an ASK as a runner-owned elicitation and returns ``pending`` — it
    cannot apply the deciding policy's ``state_updates`` / ``set_labels``
    inline because the approval happens later, off that request. They are
    stashed here keyed by elicitation id and applied when the matching
    ``approval`` event resolves with ``accept`` (POLICIES.md §7.2: a denied
    ASK leaves no trace). Without this, e.g. a cost-budget soft checkpoint is
    never recorded server-side, so it re-prompts on every subsequent tool
    call. The native-harness path (:func:`_hold_native_ask_gate`) parks
    server-side and applies these inline, so it does not need this.

    :param state_updates: Deferred :class:`StateUpdate` ops to apply on
        approve, or ``None``.
    :param set_labels: Deferred label writes to apply on approve, or ``None``.
    :param from_mcp: ``True`` when created by the ``/mcp`` endpoint's
        first-call ASK path. The MCP retry path applies writes
        itself, so the events handler skips write application for
        these entries to avoid double-applying non-idempotent ops
        (e.g. ``INCREMENT`` state updates for cost-budget counters).
    """

    state_updates: list[StateUpdate] | None
    set_labels: dict[str, str] | None
    from_mcp: bool = False


# elicitation_id -> writes to apply when that relay tool-call ASK is approved.
# Bounded + LRU-evicting: an ASK that is declined via the other entry point or
# times out (its ``approval`` event never arrives) would otherwise leak an
# entry, so the oldest evict. Populated by _evaluate_tool_call_policy, drained
# by _apply_pending_policy_ask_writes on the approval verdict.
_pending_policy_ask_writes: cachetools.LRUCache[str, _PendingPolicyAskWrites] = (
    cachetools.LRUCache(maxsize=512)
)


# (conversation_id, deciding_policy) -> lock serializing native ASK gates.
# When an agent fires several tool calls in parallel, each spawns its own
# PreToolUse hook that lands in the policy-evaluate endpoint concurrently.
# Without serialization every one of them would publish its own approval
# elicitation for the same crossed checkpoint (e.g. a cost-budget warning),
# prompting the human N times for one decision. Holding this lock across the
# human wait lets the first ASK resolve and record its approval, so the
# siblings re-evaluate to ALLOW (against the freshly persisted state) and never
# prompt again. Keyed on the deciding policy so unrelated policies' asks don't
# serialize against each other; keyed on the conversation so different sessions
# stay independent (claude/codex-native sub-agent tool calls share the parent
# conversation id, so this also covers them). A WeakValueDictionary drops a
# lock once no in-flight coroutine references it, bounding the registry without
# an eviction policy that could hand two waiters different lock objects.
_native_ask_gate_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _native_ask_gate_lock(conversation_id: str, deciding_policy: str) -> asyncio.Lock:
    """
    Return the lock serializing native ASK gates for one (session, policy).

    Concurrent native tool calls that all trip the same ASKing policy must
    prompt the human once, not once each. Callers hold the returned lock
    across the entire human-approval wait and re-evaluate the policy under it;
    the first approval records a checkpoint that collapses the siblings to
    ALLOW. Get-or-create is race-free because there is no ``await`` between the
    lookup and the insert (single event loop).

    :param conversation_id: Omnigent conversation id whose ASK gate is being
        serialized, e.g. ``"conv_abc123"``. Sub-agent native tool calls
        evaluate against the parent conversation id, so they share its lock.
    :param deciding_policy: Name of the policy that produced the ASK verdict,
        e.g. ``"session_cost_guard"``. Distinct policies get distinct locks so
        their approval prompts can surface concurrently.
    :returns: A process-wide :class:`asyncio.Lock` shared by every concurrent
        caller for the same ``(conversation_id, deciding_policy)`` pair.
    """
    key = (conversation_id, deciding_policy)
    lock = _native_ask_gate_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _native_ask_gate_locks[key] = lock
    return lock


@dataclass
class _RelayHandle:
    """
    Active SSE relay task plus the runner it streams from.

    :param runner_id: Runner id the task is bound to, e.g.
        ``"runner_abc123"``. Used to detect rebinds to a
        different runner so the stale task can be replaced.
    :param task: The relay coroutine task.
    :param ready: Event set after the relay observes the runner
        stream's ready heartbeat, proving the runner-side
        no-replay subscription is registered.
    """

    runner_id: str
    task: asyncio.Task[None]
    ready: asyncio.Event


# Background SSE relays keyed by session_id (one per session).
_runner_relay_tasks: dict[str, _RelayHandle] = {}


async def _poll_request_disconnect(request: Request) -> None:
    """
    Resolve once Starlette reports the client closed the connection.

    Long-poll routes that park on a verdict (e.g. the Claude-native
    ``PermissionRequest`` hook) use this to detect that the upstream
    client has hung up — Claude closes its HTTP request when its
    TUI prompt receives an answer first, and without this wait the
    handler would sit out the full timeout to notice.

    Blocks on ``request.receive()`` rather than polling
    ``request.is_disconnected()``. The poll variant runs each check
    inside a pre-cancelled anyio ``CancelScope`` (Starlette's
    non-blocking receive idiom); an external ``Task.cancel()`` that
    lands while that scope is unwinding coalesces with the scope's own
    cancellation and is swallowed with it, so the poller survives its
    cancel and the caller's race cleanup blocks on it forever.
    A blocking receive has no cancel scope in its await chain, so
    cancellation always propagates; it is also cheaper than waking
    twice a second.

    :param request: The active FastAPI :class:`Request`. By the time
        the handler parks, the route has consumed the body, so the
        next receive yields only ``http.disconnect``.
    :returns: None when the disconnect is observed. Cancellation
        propagates: callers that race this against a verdict Future
        cancel the wait once the verdict arrives.
    """
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _stored_file_to_resource(
    session_id: str,
    stored: StoredFile,
) -> dict[str, Any]:
    """Convert a :class:`StoredFile` to a session file resource dict.

    Matches the ``session.resource`` shape with ``type: "file"``
    used by the unified inventory and the session-scoped file
    endpoints.

    :param session_id: Owning session/conversation id.
    :param stored: The stored file entity.
    :returns: JSON-serializable resource dict.
    """
    return {
        "id": stored.id,
        "object": "session.resource",
        "type": "file",
        "session_id": session_id,
        "name": stored.filename,
        "metadata": {
            "filename": stored.filename,
            "bytes": stored.bytes,
            "created_at": stored.created_at,
        },
    }


def _publish_and_persist_resource_event(
    session_id: str,
    event_type: str,
    resource_id: str,
    resource_type: str,
    conversation_store: ConversationStore,
    resource: dict[str, Any] | None = None,
) -> None:
    """Publish an SSE event and persist it as a conversation item.

    Emits the event on the live session stream so connected
    clients see it immediately, and appends a ``resource_event``
    conversation item so reconnecting clients discover it in the
    snapshot.

    :param session_id: Session/conversation identifier.
    :param event_type: SSE event type, e.g.
        ``"session.resource.created"``.
    :param resource_id: Opaque id of the affected resource.
    :param resource_type: Kind of resource, e.g. ``"terminal"``.
    :param conversation_store: Store for persisting the item.
    :param resource: Full resource dict for created events.
    """
    from omnigent.entities.conversation import ResourceEventData

    sse_payload: dict[str, Any] = {"type": event_type}
    if event_type == "session.resource.created":
        sse_payload["resource"] = resource or {}
    else:
        sse_payload["resource_id"] = resource_id
        sse_payload["resource_type"] = resource_type
        sse_payload["session_id"] = session_id

    session_stream.publish(session_id, sse_payload)

    item = NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=event_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )
    try:
        conversation_store.append(session_id, [item])
    except (AttributeError, TypeError, ValueError, RuntimeError):
        _logger.debug(
            "Failed to persist resource event for session=%s",
            session_id,
            exc_info=True,
        )


def _structured_ask_user_question(
    tool_input: Any,
) -> dict[str, Any] | None:
    """
    Build a structured AskUserQuestion payload for the elicitation
    params extras.

    Claude's PermissionRequest payload includes the full tool_input
    when the gated tool is AskUserQuestion. Rather than relying on
    the (truncated) ``content_preview`` JSON-string, we extract the
    questions + options here and ship them as a typed structure the
    UI consumes directly.

    The returned shape is the same one the UI's
    :file:`@/lib/askUserQuestion.ts` produces from its preview
    parser — so the front-end can treat both sources uniformly.

    :param tool_input: The ``tool_input`` field from the
        PermissionRequest payload.
    :returns: ``{"questions": [...]}`` on success, or ``None`` when
        the input doesn't carry a usable AskUserQuestion shape (no
        questions, malformed options, etc.) — caller falls back to
        the binary preview-only render.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None
    questions: list[dict[str, Any]] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_text = entry.get("question")
        if not isinstance(question_text, str) or not question_text:
            continue
        options_raw = entry.get("options")
        if not isinstance(options_raw, list):
            continue
        options: list[dict[str, Any]] = []
        for opt in options_raw:
            if isinstance(opt, dict):
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option: dict[str, Any] = {"label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                # ``preview`` is an optional richer snippet some
                # Claude builds attach to an option (rendered as a
                # <pre> below the option list when selected). Ride
                # it through verbatim so the UI can surface it.
                preview = opt.get("preview")
                if isinstance(preview, str) and preview:
                    option["preview"] = preview
                options.append(option)
            elif isinstance(opt, str) and opt:
                options.append({"label": opt})
        if not options:
            continue
        question: dict[str, Any] = {
            "question": question_text,
            "options": options,
            "multiSelect": entry.get("multiSelect") is True,
        }
        header = entry.get("header")
        if isinstance(header, str) and header:
            question["header"] = header
        questions.append(question)
    if not questions:
        return None
    return {"questions": questions}


async def _publish_and_wait_for_harness_elicitation(
    request: Request,
    *,
    session_id: str,
    params: ElicitationRequestParams,
    timeout_s: float,
    conversation_store: ConversationStore | None = None,
    elicitation_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> ElicitationResult | None:
    """
    Publish one harness-originated elicitation and wait for web verdict.

    Mirrors the ``omnigent claude`` permission hook contract: the
    hook parks a server-side Future, publishes the standard
    ``response.elicitation_request`` event, waits until the session
    ``approval`` event resolves the Future, and always publishes
    ``response.elicitation_resolved`` when the upstream wait ends.

    The wait ends on the first of three signals: (1) the web verdict
    Future (session ``approval`` event); (2) the terminal-resolved
    Event, set when a mirrored tool result for this gated tool proves
    the prompt was answered in the native TUI (see
    :func:`_signal_terminal_resolved_harness_elicitation`); or (3)
    upstream disconnect / ``timeout_s``. Only (1) yields a verdict;
    (2) and (3) return ``None`` (fail-ask). (1) and (2) publish
    ``response.elicitation_resolved`` immediately; (3) defers it by
    ``_HARNESS_ELICITATION_REPARK_GRACE_S`` and skips it when the
    caller re-parks the same ``elicitation_id`` (hook retries after a
    severed long-poll reuse their id), so a still-blocked prompt's
    card survives the gap. A caller-supplied id likewise re-attaches
    to a verdict that landed during a gap via the pre-resolved
    tombstone, returned at registration time without re-publishing.

    :param request: FastAPI request object so upstream disconnect can
        be detected.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param params: Elicitation params to publish.
    :param timeout_s: Maximum wait in seconds, e.g. ``300.0``.
    :param conversation_store: Optional store used to mirror
        child-session prompts into ancestor streams. ``None`` keeps
        the prompt scoped to ``session_id`` only.
    :param elicitation_id: Optional precomputed correlation id, e.g.
        ``"elicit_codex_abc123"``. ``None`` mints a random id.
    :param tool_name: Gated tool name, e.g. ``"Bash"``, used to
        correlate a mirrored tool result back to this prompt for the
        terminal-resolved fast path. ``None`` (e.g. Codex) disables
        that correlation; the prompt still resolves via web verdict,
        disconnect, or timeout.
    :param tool_input: Gated tool input, e.g. ``{"command": "ls"}``,
        used with ``tool_name`` to disambiguate the result when several
        same-named prompts are parked at once.
    :returns: Web verdict, or ``None`` on terminal-side resolution,
        timeout, or disconnect.
    """
    if elicitation_id is None:
        elicitation_id = f"elicit_{secrets.token_hex(16)}"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    # ``resolved_elsewhere`` is set when a native-side signal proves the
    # prompt was answered outside the web UI: either a mirrored tool
    # result for this gated tool, or Codex app-server's exact
    # ``serverRequest/resolved`` notification. Raced below so the wait
    # ends promptly without relying on the web verdict or on disconnect
    # detection (unreliable behind the Databricks Apps proxy).
    parked = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        resolved_elsewhere=asyncio.Event(),
    )
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_parked_elicitations[elicitation_id] = parked
    # settled = verdict / terminal-resolved (clear the card now); a
    # severed wait instead defers the clear so a hook retry can re-park.
    published_request = False
    settled = False
    try:
        tombstone = _consume_pre_resolved_harness_elicitation(session_id, elicitation_id)
        if tombstone is not None:
            # Verdict from the un-parked gap; None = terminal answered (fail-ask).
            return tombstone.result
        event = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=params,
        )
        event_payload = event.model_dump()
        session_stream.publish(session_id, event_payload)
        published_request = True
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                event_payload,
            )
        disconnect_task = asyncio.create_task(
            _poll_request_disconnect(request),
        )
        resolved_elsewhere_task = asyncio.create_task(parked.resolved_elsewhere.wait())
        race_tasks = (disconnect_task, resolved_elsewhere_task)
        try:
            done, _pending = await asyncio.wait(
                {future, *race_tasks},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for race_task in race_tasks:
                if not race_task.done():
                    race_task.cancel()
                    # Bounded: a cancellation swallowed inside the race
                    # task (e.g. coalesced into an anyio cancel-scope
                    # unwind) must not convert this cleanup into
                    # an unbounded wait — that wedged the whole request
                    # for the gate's timeout. ``asyncio.wait`` absorbs
                    # the CancelledError outcome; an unreaped task is
                    # logged and abandoned to die with the request.
                    _reaped, still_pending = await asyncio.wait(
                        {race_task},
                        timeout=_RACE_TASK_REAP_TIMEOUT_S,
                    )
                    if still_pending:
                        _logger.warning(
                            "Race task %r for elicitation %s survived its "
                            "cancellation (swallowed cancel); abandoning it.",
                            race_task.get_coro(),
                            elicitation_id,
                        )
        # Only an actual web verdict yields a result; a terminal-side
        # resolution, disconnect, or timeout returns None (fail-ask).
        # Checking ``future in done`` (not ``future.done()``) avoids
        # honoring a verdict that lands in the same tick as a disconnect.
        if future in done and future.exception() is None:
            settled = True
            return future.result()
        settled = parked.resolved_elsewhere.is_set()
        return None
    finally:
        # Pop only our own entries — a hook retry may have re-parked
        # this id with a new future while this wait was unwinding.
        if _harness_elicitation_registry.get(elicitation_id) is future:
            _harness_elicitation_registry.pop(elicitation_id, None)
            _harness_elicitation_owners.pop(elicitation_id, None)
        if _harness_parked_elicitations.get(elicitation_id) is parked:
            _harness_parked_elicitations.pop(elicitation_id, None)
        if published_request and not settled:
            # Severed without an answer — defer the clear (scheduled
            # before any await so handler cancellation can't skip it).
            _schedule_deferred_elicitation_clear(
                session_id,
                elicitation_id,
                conversation_store,
            )
        elif published_request:
            _publish_elicitation_resolved(session_id, elicitation_id)
            if conversation_store is not None:
                await asyncio.to_thread(
                    _publish_elicitation_resolved_to_ancestors,
                    conversation_store,
                    session_id,
                    elicitation_id,
                )


def _signal_terminal_resolved_harness_elicitation(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
) -> None:
    """
    Resolve the parked prompt a mirrored tool result belongs to,
    ending its long-poll promptly.

    Called when the transcript forwarder mirrors a tool result
    (``function_call_output``) for a native session. A tool result is
    only written AFTER the user answered that tool's permission prompt
    in the native terminal — on accept the tool ran and produced output,
    on reject the harness records a rejection result — so its arrival is
    a reliable "the terminal already resolved this" signal.

    Correlation is by tool identity, never positional: a result only
    resolves a parked prompt for the SAME ``tool_name`` in the same
    session. That is what stops an unrelated auto-allowed tool's output
    from clearing a different tool's pending prompt. Among
    same-named parked prompts it prefers an exact ``tool_input`` match;
    if none match exactly but exactly one same-named prompt is parked it
    resolves that one (the hook payload and the transcript can serialize
    identical input differently, so a single unambiguous candidate is
    treated as the match). If several same-named prompts are parked and
    none match by input it stays conservative and resolves none — the
    web verdict or timeout still applies.

    Best-effort and idempotent: a no-op when no parked prompt matches
    (e.g. the web UI already resolved it, the tool needed no permission,
    or it is an unrelated tool). Harness-agnostic by construction —
    keyed on the parked prompt's tool identity, not on a claude-native
    check — so a Codex hook that records ``tool_name`` benefits too.

    :param session_id: Omnigent conversation id whose forwarder mirrored the
        result, e.g. ``"conv_abc123"``.
    :param tool_name: Tool name the result is for, e.g. ``"Bash"``.
    :param tool_input: Tool input the result is for, e.g.
        ``{"command": "ls"}``, or ``None`` if unavailable.
    """
    candidates = [
        parked
        for parked in _harness_parked_elicitations.values()
        if parked.session_id == session_id
        and parked.tool_name == tool_name
        and not parked.resolved_elsewhere.is_set()
    ]
    if not candidates:
        return
    for parked in candidates:
        if parked.tool_input == tool_input:
            parked.resolved_elsewhere.set()
            return
    if len(candidates) == 1:
        candidates[0].resolved_elsewhere.set()


# Strong refs so deferred card-clear tasks aren't GC'd mid-sleep.
_deferred_elicitation_clear_tasks: set[asyncio.Task[None]] = set()


def _schedule_deferred_elicitation_clear(
    session_id: str,
    elicitation_id: str,
    conversation_store: ConversationStore | None,
) -> None:
    """
    Clear one elicitation's approval card after the re-park grace, unless
    a hook retry re-parks the id first.

    A wait severed without an answer (proxy cut, timeout) may still be
    blocked in the native terminal; clearing immediately wiped the only
    surface a headless sub-agent's user can answer from. A hook that
    died for real never re-parks, so the clear still fires after the
    grace and badges don't stick.

    :param session_id: Session that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param elicitation_id: Correlation id whose card may need clearing,
        e.g. ``"elicit_claude_0f3a..."``.
    :param conversation_store: Store used to mirror the clear into
        ancestor streams, or ``None`` to keep it session-local.
    """

    async def _clear_after_grace() -> None:
        """
        Sleep out the grace, then publish the clear unless re-parked.

        :returns: None.
        """
        await asyncio.sleep(_HARNESS_ELICITATION_REPARK_GRACE_S)
        if elicitation_id in _harness_elicitation_registry:
            # Re-parked — the new wait owns the eventual clear.
            return
        _publish_elicitation_resolved(session_id, elicitation_id)
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
            )

    task = asyncio.create_task(_clear_after_grace())
    _deferred_elicitation_clear_tasks.add(task)
    task.add_done_callback(_deferred_elicitation_clear_tasks.discard)


def _client_supplied_hook_elicitation_id(
    payload: dict[str, Any],
    session_id: str,
) -> str | None:
    """
    Validate the hook client's optional re-attach elicitation id.

    The hook mints one stable id per prompt and re-sends it on every
    retry POST, so a severed wait re-parks as the SAME elicitation.
    Client-controlled, so it is constrained to the claude-hook
    namespace and may not collide with another session's parked id.

    :param payload: Parsed PermissionRequest hook body. Reads the
        optional ``_omnigent_elicitation_id`` key.
    :param session_id: Session the hook call is for, e.g.
        ``"conv_abc123"``.
    :returns: The validated id, or ``None`` when the client supplied
        none (the wait mints a random id as before).
    :raises OmnigentError: 400 when the id is malformed or is
        currently parked by a different session.
    """
    raw = payload.get("_omnigent_elicitation_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not _CLAUDE_HOOK_ELICITATION_ID_RE.fullmatch(raw):
        raise OmnigentError(
            "PermissionRequest hook '_omnigent_elicitation_id' must match "
            "'elicit_claude_' + 32 hex chars.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(raw)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation id belongs to a different session.",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


def _consume_pre_resolved_harness_elicitation(
    session_id: str,
    elicitation_id: str,
) -> _PreResolvedHarnessElicitation | None:
    """
    Consume a resolution that arrived before the hook wait registered.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: The consumed tombstone when one matched this session
        (its ``result`` carries the web verdict to honor, or ``None``
        for a terminal-side resolution), or ``None`` when nothing was
        pre-resolved.
    """
    _prune_pre_resolved_harness_elicitations()
    tombstone = _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    if tombstone is None:
        return None
    if tombstone.session_id == session_id:
        return tombstone
    _harness_pre_resolved_elicitations[elicitation_id] = tombstone
    return None


def _prune_pre_resolved_harness_elicitations(now: float | None = None) -> None:
    """
    Prune stale or excess pre-resolved harness elicitation tombstones.

    :param now: Optional wall-clock timestamp from ``time.time()``,
        e.g. ``1710000000.0``. ``None`` reads the current time.
    :returns: None.
    """
    if not _harness_pre_resolved_elicitations:
        return
    now = time.time() if now is None else now
    expired = [
        elicitation_id
        for elicitation_id, tombstone in _harness_pre_resolved_elicitations.items()
        if now - tombstone.created_at > _HARNESS_PRE_RESOLVED_ELICITATION_TTL_S
    ]
    for elicitation_id in expired:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    overflow = (
        len(_harness_pre_resolved_elicitations) - _HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES
    )
    if overflow <= 0:
        return
    oldest = sorted(
        _harness_pre_resolved_elicitations.items(),
        key=lambda item: item[1].created_at,
    )[:overflow]
    for elicitation_id, _tombstone in oldest:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)


def _signal_harness_elicitation_resolved_by_id(
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Resolve or pre-resolve one parked harness elicitation by id.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: None.
    :raises OmnigentError: If the id is malformed or belongs to a
        different session.
    """
    if not elicitation_id:
        raise OmnigentError(
            "external_elicitation_resolved requires data.elicitation_id.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(elicitation_id)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation does not belong to this session.",
            code=ErrorCode.INVALID_INPUT,
        )
    _prune_pre_resolved_harness_elicitations()
    parked = _harness_parked_elicitations.get(elicitation_id)
    if parked is None:
        _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
            session_id=session_id,
            created_at=time.time(),
        )
        _prune_pre_resolved_harness_elicitations()
        return
    parked.resolved_elsewhere.set()


def _format_sse(event_type: str, data: dict[str, Any]) -> str:
    """
    Format an SSE event string for the wire.

    :param event_type: SSE event name, e.g.
        ``"response.output_text.delta"``.
    :param data: The event payload dict.
    :returns: A formatted SSE message string ending in two newlines.
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _permission_level_from_grants(
    user_id: str | None,
    grants: list[SessionPermission],
    is_admin: bool,
) -> int | None:
    """
    Derive a user's permission level from a pre-fetched list of grants.

    Mirrors :func:`omnigent.server.routes._auth_helpers._get_permission_level_sync`
    but operates on grants already held in memory so callers can batch the
    permission-store query across many sessions at once.

    :param user_id: The authenticated user, or ``None`` for unauthenticated
        requests, e.g. ``"alice@example.com"``.
    :param grants: All grants for the session, as returned by
        ``permission_store.list_for_sessions()[conv_id]``.
    :param is_admin: Whether the user holds the admin flag.  Pass the result
        of a single ``permission_store.is_admin(user_id)`` call made once
        for the whole page rather than repeating it per session.
    :returns: Numeric level (1–4), or ``None`` when permissions are disabled
        or the user is unauthenticated.
    """
    if user_id is None:
        return None
    if is_admin:
        return LEVEL_OWNER
    user_grant = next((g for g in grants if g.user_id == user_id), None)
    if user_grant is not None:
        return user_grant.level
    public_grant = next((g for g in grants if g.user_id == RESERVED_USER_PUBLIC), None)
    if public_grant is not None:
        return public_grant.level
    return None


def _owner_from_grants(grants: list[SessionPermission]) -> str | None:
    """
    Find the session owner from a pre-fetched list of grants.

    Mirrors :func:`omnigent.server.routes._auth_helpers.get_session_owner_id`
    but operates on grants already held in memory so callers can batch the
    permission-store query across many sessions at once.

    :param grants: All grants for the session, as returned by
        ``permission_store.list_for_sessions()[conv_id]``.
    :returns: The ``user_id`` of the first grant whose level is at least
        :data:`LEVEL_OWNER`, or ``None`` if no such grant exists.
    """
    return next((g.user_id for g in grants if g.level >= LEVEL_OWNER), None)


def _session_status_from_cache(conversation_id: str) -> Literal["idle", "running", "failed"]:
    """
    Map the relay-fed status cache value to a list-item status.

    The cache stores the fine-grained relay status (``"running"``,
    ``"waiting"``, ``"failed"``, ``"idle"``); the list-item shape
    collapses ``"running"``/``"waiting"`` to ``"running"``. A cache
    miss means no relay has reported on this session, which presents
    as ``"idle"``.

    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"``.
    """
    cached = _session_status_cache.get(conversation_id)
    if cached in ("running", "waiting"):
        return "running"
    if cached == "failed":
        return "failed"
    return "idle"


def _session_status_with_child_rollup(
    conversation_id: str,
    child_session_ids: list[str],
) -> Literal["idle", "running", "failed"]:
    """
    Map a session's cached status plus direct child activity to list status.

    A parent session should read as ``"running"`` in the sidebar while any
    direct sub-agent child is still ``"running"`` or ``"waiting"``, even if
    the parent runner has already gone idle. This keeps every sidebar row
    honest without mounting a child-session query for each row.

    :param conversation_id: Parent session/conversation identifier,
        e.g. ``"conv_parent123"``.
    :param child_session_ids: Direct sub-agent child conversation ids,
        e.g. ``["conv_child1", "conv_child2"]``.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"`` for the
        session-list row.
    """
    own_status = _session_status_from_cache(conversation_id)
    if own_status == "running":
        return "running"
    if any(
        _session_status_cache.get(child_id) in ("running", "waiting")
        for child_id in child_session_ids
    ):
        return "running"
    return own_status


@dataclass(frozen=True)
class SessionLiveness:
    """
    The two honest liveness signals for a single session.

    Returned (keyed by session id) by the server's
    ``_bulk_session_liveness`` / ``_session_liveness`` lookups and
    consumed by the list-item builder, the ``WS /v1/sessions/updates``
    stream, the single-session ``SessionResponse`` snapshot, and
    ``GET /health``. Splitting the old single conflated boolean into
    two fields lets the open-session view distinguish "runner stopped
    but host can relaunch — just send a message" from "host offline —
    reconnect / fork".

    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session. This
        is the sole reachability signal: it does **not** fold in
        host-relaunch optimism (a dead runner on a live host reads
        ``False`` here, not ``True``). A session with no runner
        binding (in-process executor / not yet dispatched) reads
        ``True``.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within ``HOST_LIVENESS_TTL_S``).
        ``True`` when the session's ``host_id`` is in the online-hosts
        set, ``False`` when a ``host_id`` is set but not online, and
        ``None`` when the session has no ``host_id`` (CLI / local).
        Used only to choose what the open view shows when
        ``runner_online`` is ``False``; never participates in the
        reachability decision.
    """

    runner_online: bool
    host_online: bool | None


def _build_session_list_item(
    conv: Conversation,
    *,
    agent_names_by_id: dict[str, str | None],
    grants: list[SessionPermission],
    user_id: str | None,
    user_is_admin: bool,
    permissions_enabled: bool,
    pending_count: int,
    child_session_ids: list[str],
    comments_fingerprint: CommentsFingerprint | None,
) -> SessionListItem:
    """
    Assemble one :class:`SessionListItem` from a conversation row and
    pre-fetched batch data.

    Single source of truth for the list-item shape, shared by the
    ``GET /v1/sessions`` page builder and the ``WS /v1/sessions/updates``
    push stream so the two never drift. The caller is responsible for
    batching the permission grants, agent names, and pending-elicitation
    counts across the whole set and passing the per-conversation slice
    here.

    :param conv: The persisted conversation entity. Must have a
        non-``None`` ``agent_id`` (i.e. be a session, not a plain
        conversation) — the caller filters these out beforehand.
    :param agent_names_by_id: Map from agent id to display name, as
        returned by ``agent_store.get_names()``,
        e.g. ``{"ag_abc": "research-agent"}``.
    :param grants: All permission grants for this conversation, as
        returned by ``permission_store.list_for_sessions()[conv.id]``.
        Empty list when permissions are disabled.
    :param user_id: The authenticated requesting user, or ``None`` when
        unauthenticated / permissions disabled,
        e.g. ``"alice@example.com"``.
    :param user_is_admin: Whether ``user_id`` holds the admin flag, from
        a single ``permission_store.is_admin()`` call made once for the
        whole batch.
    :param permissions_enabled: ``True`` when a permission store is
        wired; gates owner/level population to mirror ``list_sessions``.
    :param pending_count: Number of outstanding elicitations for this
        conversation, from ``pending_elicitations.counts_for()``.
    :param child_session_ids: Direct sub-agent children for this
        conversation, as returned by
        ``conversation_store.list_child_conversation_ids_by_parent()``.
    :param comments_fingerprint: Change-detection summary of this
        conversation's review comments, from
        ``comment_store.get_comments_fingerprints()[conv.id]``. ``None``
        when the conversation has no comments or no comment store is
        wired — emitted as ``comments_count=0`` /
        ``comments_updated_at=None`` so the two states look identical
        on the wire.
    :returns: The assembled :class:`SessionListItem`.
    """
    # ``conv.agent_id`` is guaranteed non-None by the caller (sessions
    # only); assert for the type checker without a runtime branch.
    assert conv.agent_id is not None
    level = _permission_level_from_grants(user_id, grants, user_is_admin)
    owner = _owner_from_grants(grants) if permissions_enabled else None
    return SessionListItem(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_names_by_id.get(conv.agent_id),
        status=_session_status_with_child_rollup(conv.id, child_session_ids),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        title=title_without_closed_marker(conv.title),
        labels=labels_with_closed_status(conv.labels, conv.title),
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        reasoning_effort=conv.reasoning_effort,
        permission_level=level,
        owner=owner,
        external_session_id=conv.external_session_id,
        pending_elicitations_count=pending_count,
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        comments_count=comments_fingerprint.count if comments_fingerprint else 0,
        comments_updated_at=(
            comments_fingerprint.last_updated_at if comments_fingerprint else None
        ),
    )


async def _apply_liveness_to_items(
    items: list[SessionListItem],
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
) -> None:
    """
    Attach runner + host liveness to session-list items when a lookup is
    wired.

    Both ``GET /v1/sessions`` and ``WS /v1/sessions/updates`` use this so
    HTTP reconciliation preserves the same ``runner_online`` /
    ``host_online`` fields that push frames patch into the web cache.

    :param items: Session-list rows to annotate.
    :param liveness_lookup: Bulk liveness lookup from session id to a
        :class:`SessionLiveness` pair, e.g.
        ``{"conv_abc123": SessionLiveness(runner_online=True,
        host_online=None)}``. ``None`` means this server cannot compute
        liveness for list rows, in which case both fields are left
        ``None``.
    :returns: ``None``. Mutates ``items`` in place.
    """
    if liveness_lookup is None or not items:
        return
    liveness = await asyncio.to_thread(liveness_lookup, [item.id for item in items])
    for item in items:
        result = liveness[item.id]
        item.runner_online = result.runner_online
        item.host_online = result.host_online


def _targeted_elicitation_event(
    event: dict[str, Any],
    *,
    target_session_id: str,
) -> dict[str, Any]:
    """
    Return an elicitation event annotated with its resolution target.

    Child-session elicitations can be mirrored into an ancestor's
    chat stream. The mirrored card is rendered in the ancestor
    conversation, but the harness Future still belongs to the child.
    ``target_session_id`` tells clients which session's resolve URL
    should receive the verdict.

    :param event: Original ``response.elicitation_request`` event,
        e.g. ``{"type": "response.elicitation_request",
        "elicitation_id": "elicit_abc", "params": {...}}``.
    :param target_session_id: Session that owns the parked
        elicitation, e.g. ``"conv_child123"``.
    :returns: A shallow event copy with a copied ``params`` dict
        carrying ``target_session_id``.
    """
    mirrored = dict(event)
    params = event.get("params")
    if isinstance(params, dict):
        mirrored["params"] = {**params, "target_session_id": target_session_id}
    else:
        mirrored["params"] = {"target_session_id": target_session_id}
    return mirrored


def _ancestor_session_ids(
    conv_store: ConversationStore,
    session_id: str,
) -> list[str]:
    """
    Return ancestor session ids for a session, nearest parent first.

    :param conv_store: Store used to read conversation parent links.
    :param session_id: Session to walk upward from, e.g.
        ``"conv_child123"``.
    :returns: Ancestor ids in parent-to-root order. Empty when the
        session is top-level or missing.
    """
    ancestors: list[str] = []
    seen = {session_id}
    current = conv_store.get_conversation(session_id)
    while current is not None and current.parent_conversation_id is not None:
        parent_id = current.parent_conversation_id
        if parent_id in seen:
            break
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = conv_store.get_conversation(parent_id)
    return ancestors


def _publish_elicitation_request_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """
    Mirror a child elicitation request into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param event: Original ``response.elicitation_request`` event.
    """
    mirrored = _targeted_elicitation_event(event, target_session_id=session_id)
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        session_stream.publish(ancestor_id, mirrored)


def _publish_elicitation_resolved_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Mirror an elicitation-resolved event into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param elicitation_id: Elicitation correlation id, e.g.
        ``"elicit_abc123"``.
    """
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        _publish_elicitation_resolved(ancestor_id, elicitation_id)


def _publish_subtree_cost_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
) -> None:
    """
    Re-publish each ancestor's subtree-summed cost after a child usage update.

    A sub-agent's spend is persisted on its own child conversation, so an
    ancestor's stored ``session_usage`` doesn't move when the child spends —
    yet the ancestor's displayed "Session cost" reads its own number, so a
    parent's badge would never reflect a running sub-agent. (The policy gate
    already reads the subtree sum via :func:`load_session_usage`; this is the
    display side.) For each ancestor of *session_id*, recompute its subtree
    priced cost and publish a ``session.usage`` event carrying it.

    Sync (does store reads + SSE fan-out); call via
    :func:`asyncio.to_thread`, mirroring the elicitation ancestor-publish
    helpers. ``session_stream.publish`` is safe to call from a worker thread.

    :param conv_store: Store used to discover ancestors and sum each
        ancestor's subtree usage.
    :param session_id: The child session whose usage just changed, e.g.
        ``"conv_child123"``.
    :returns: None.
    """
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        ancestor_usage = load_session_usage(ancestor_id, conv_store)
        subtree_cost = _priced_cost_for_display(ancestor_usage)
        usage_by_model = _usage_by_model_for_display(ancestor_usage)
        if subtree_cost is None and usage_by_model is None:
            # Ancestor's subtree has no priced cost or token usage yet —
            # leave its badge showing "—"/its snapshot value rather than
            # emit $0.00.
            continue
        payload: dict[str, Any] = {
            "type": "session.usage",
            "conversation_id": ancestor_id,
        }
        if subtree_cost is not None:
            payload["total_cost_usd"] = subtree_cost
        if usage_by_model is not None:
            payload["usage_by_model"] = usage_by_model
        event = SessionUsageEvent(**payload)
        session_stream.publish(ancestor_id, event.model_dump(exclude_none=True))


def _descendant_sessions(
    conv_store: ConversationStore,
    session_id: str,
) -> list[Conversation]:
    """
    Return descendant sub-agent conversations for a session.

    :param conv_store: Store used to list conversations.
    :param session_id: Ancestor session id, e.g. ``"conv_root123"``.
    :returns: Sub-agent conversations below ``session_id``. Empty
        for sessions with no descendants.
    """
    descendants: list[Conversation] = []
    queue: deque[str] = deque([session_id])
    seen = {session_id}
    while queue:
        parent_id = queue.popleft()
        after: str | None = None
        while True:
            page = conv_store.list_conversations(
                kind="sub_agent",
                parent_conversation_id=parent_id,
                limit=100,
                after=after,
            )
            for child in page.data:
                if child.id in seen:
                    continue
                seen.add(child.id)
                descendants.append(child)
                queue.append(child.id)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
    return descendants


def _pending_elicitation_snapshot_for_session(
    conv_store: ConversationStore,
    conv: Conversation,
) -> list[dict[str, Any]]:
    """
    Return pending elicitation events visible from a session snapshot.

    The current session's own outstanding prompts are returned first.
    Pending prompts from descendant sub-agents are appended with
    ``params.target_session_id`` so a cold-loaded ancestor chat can
    render and resolve child approvals.
    Duplicate ids are skipped because live mirroring also records the
    ancestor copy in the in-memory index.

    The descendant walk costs one ``list_conversations`` query per
    session in the tree, so it is skipped entirely unless some session
    other than ``conv`` has an outstanding prompt in the in-memory
    index (the common case is none anywhere).

    :param conv_store: Store used to list descendant sub-agents.
    :param conv: Session conversation being snapshotted.
    :returns: Pending elicitation event dicts suitable for
        :class:`SessionResponse.pending_elicitations`.
    """
    events = pending_elicitations.snapshot_for(conv.id)
    if not (set(pending_elicitations.pending_session_ids()) - {conv.id}):
        return events
    seen = {
        event.get("elicitation_id")
        for event in events
        if isinstance(event.get("elicitation_id"), str)
    }
    for child in _descendant_sessions(conv_store, conv.id):
        for event in pending_elicitations.snapshot_for(child.id):
            elicitation_id = event.get("elicitation_id")
            if isinstance(elicitation_id, str) and elicitation_id in seen:
                continue
            if isinstance(elicitation_id, str):
                seen.add(elicitation_id)
            events.append(_targeted_elicitation_event(event, target_session_id=child.id))
    return events


def _build_session_response(
    conv: Conversation,
    items: list[ConversationItem],
    status: Literal["idle", "running", "failed"],
    permission_level: int | None = None,
    llm_model: str | None = None,
    context_window: int | None = None,
    last_total_tokens: int | None = None,
    last_task_error: dict[str, str] | None = None,
    agent_name: str | None = None,
    skills: list[SkillSummary] | None = None,
    runner_online: bool | None = None,
    host_online: bool | None = None,
    pending_elicitation_events: list[dict[str, Any]] | None = None,
    subtree_usage: dict[str, Any] | None = None,
) -> SessionResponse:
    """
    Build a :class:`SessionResponse` from store-side entities.

    ``status`` is derived from the conversation's tasks by the
    caller via :func:`_derive_session_lifecycle` — the conversation
    row itself owns no lifecycle column.

    :param conv: The persisted conversation entity.
    :param items: Committed conversation items in chronological
        order, each a :class:`ConversationItem`.
    :param status: Derived session lifecycle status,
        e.g. ``"running"``.
    :param permission_level: The requesting user's numeric level
        on this session (1=read, 2=edit, 3=manage), or ``None``
        when permissions are disabled.
    :param runner_online: Session-scoped liveness for the bound
        runner/host, e.g. ``False`` for a dead tunneled runner.
        ``None`` when no lookup is wired.
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when not available.
    :param context_window: Context window size in tokens looked up
        from litellm server-side, e.g. ``200_000``. ``None`` when
        the model is not in litellm's registry.
    :param last_total_tokens: Total token count (input + output) from
        the most recently completed task's usage, e.g. ``45231``.
        ``None`` when no task has completed yet. Lets clients seed
        their context-ring on conversation resume without waiting for
        the next ``response.completed`` SSE event.
    :param last_task_error: Error dict from the most recently failed
        task, e.g. ``{"code": "executor_error", "message": "..."}``.
        ``None`` when ``status`` is not ``"failed"`` or the task has
        no stored error.
    :param agent_name: Human-readable agent name, e.g.
        ``"research-agent"``. ``None`` when the agent row is not
        available at snapshot-build time.
    :param skills: Merged skill summaries (bundled + host) for
        the bound agent. ``None`` is treated as the empty list,
        e.g. when the agent spec cannot be loaded.
    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session (see
        :class:`SessionLiveness`). ``None`` when the caller has no
        liveness lookup wired (e.g. focused tests), in which case the
        field is omitted from the API projection.
    :param host_online: Whether the session's host tunnel is live, or
        ``None`` when the session has no ``host_id`` or no lookup is
        wired (see :class:`SessionLiveness`). Used only to decide what
        the open view shows when ``runner_online`` is ``False``.
    :param pending_elicitation_events: Optional precomputed
        outstanding elicitation events. ``None`` reads only the
        current session's entries from the pending-elicitations index.
    :param subtree_usage: Precomputed subtree usage dict (this session
        plus its sub-agent descendants, from
        :func:`load_session_usage`), used to display a cost that
        includes sub-agents, e.g. ``{"total_cost_usd": 11.19}``.
        ``None`` falls back to this conversation's own ``session_usage``
        (correct for childless sessions). Passed by the snapshot path;
        other callers omit it.
    :returns: The :class:`SessionResponse` for the API.
    :raises OmnigentError: If ``conv.agent_id`` is ``None``.
    """
    if conv.agent_id is None:
        raise OmnigentError(
            "Session has no agent binding",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Usage to display for this node: the SUBTREE total (this session + its
    # sub-agents) when the caller computed it, else this conversation's own
    # usage. Shared by the cost indicator and the per-model breakdown so
    # both read the same numbers.
    display_usage = subtree_usage if subtree_usage is not None else (conv.session_usage or {})
    return SessionResponse(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_name,
        status=status,
        created_at=conv.created_at,
        title=title_without_closed_marker(conv.title),
        labels=labels_with_closed_status(conv.labels, conv.title),
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        runner_online=runner_online,
        host_online=host_online,
        reasoning_effort=conv.reasoning_effort,
        items=items,
        permission_level=permission_level,
        sub_agent_name=conv.sub_agent_name,
        parent_session_id=conv.parent_conversation_id,
        root_conversation_id=conv.root_conversation_id,
        llm_model=llm_model,
        harness=_resolve_harness(conv),
        model_override=conv.model_override,
        cost_control_mode_override=conv.cost_control_mode_override,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        # Seed the client's cost indicator on resume. Uses the SUBTREE
        # total (this session + its sub-agents) when the caller computed
        # it, so a parent's badge reflects its sub-agents' spend; falls
        # back to this conversation's own usage otherwise. A priced
        # cumulative total, or None (rendered "—") when never priced.
        total_cost_usd=_priced_cost_for_display(display_usage),
        # Per-model breakdown over the same subtree usage. None (omitted)
        # when no per-model usage was recorded.
        usage_by_model=_usage_by_model_for_display(display_usage),
        last_task_error=last_task_error,
        external_session_id=conv.external_session_id,
        terminal_launch_args=conv.terminal_launch_args,
        # Replay outstanding approval prompts into the snapshot.
        # The live SSE stream has no buffer, so a prompt emitted
        # before the user opened this chat would otherwise never
        # render — the UI rebuilds blocks from the snapshot on
        # cold load, then live-tails. Empty list when nothing is
        # outstanding (the common case).
        pending_elicitations=(
            pending_elicitation_events
            if pending_elicitation_events is not None
            else pending_elicitations.snapshot_for(conv.id)
        ),
        # Replay un-consumed web messages on native-terminal sessions
        # so a client that posted then navigated away / rebound re-
        # hydrates the optimistic bubble. Empty for non-native sessions
        # (their message is already persisted into ``items``).
        pending_inputs=pending_inputs.snapshot_for(conv.id),
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        # Replay the latest todo list for claude-native sessions.
        # Populated by _handle_external_session_todos; empty list for
        # non-claude-native sessions or before the first poll tick.
        todos=_session_todos_cache.get(conv.id, []),
        skills=skills or [],
        # Replay terminal spin-up state so a client connecting while the
        # runner is still creating a terminal-first session's terminal
        # sees the Terminal-pill spinner. Populated by the runner SSE
        # relay; absent (False) for non-terminal-first sessions or once
        # the terminal lands / auto-create fails.
        terminal_pending=_session_terminal_pending_cache.get(conv.id, False),
        # Replay managed-sandbox launch progress so a client opening the
        # session mid-launch (the Web UI navigates here immediately
        # after the non-blocking managed create) sees the provisioning
        # indicator. None for sessions without a managed launch and
        # once the launch succeeds; a failed launch is retained with
        # its reason. Populated by _publish_sandbox_status.
        sandbox_status=_session_sandbox_status_cache.get(conv.id),
    )


def _publish_input_consumed(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Publish a ``session.input.consumed`` event for a just-persisted
    conversation item.

    Mirrors the wire shape consumers depend on for rendering the
    input (user message bubble, tool-result block, etc.) at the
    moment of acceptance.

    :param session_id: The session/conversation identifier whose
        stream should receive the event.
    :param item: The persisted :class:`ConversationItem` carrying
        the canonical ``id`` / ``type`` / ``data`` fields.
    :param cleared_pending_id: When this message drained a
        :mod:`omnigent.runtime.pending_inputs` entry (native-terminal
        web message mirrored back from the transcript), that entry's
        id, e.g. ``"pending_a1b2c3"`` — so clients drop the optimistic
        bubble by id. ``None`` when nothing was drained.
    """
    if item.type == "message" and isinstance(item.data, MessageData) and item.data.is_meta:
        return
    event = SessionInputConsumedEvent(
        type="session.input.consumed",
        data=SessionInputConsumedPayload(
            item_id=item.id,
            type=item.type,
            data=item.data.model_dump() if item.data is not None else {},
            created_by=item.created_by,
            cleared_pending_id=cleared_pending_id,
        ),
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_compaction_in_progress(session_id: str) -> None:
    """
    Publish the standard compaction progress event to a session stream.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    session_stream.publish(
        session_id,
        {"type": "response.compaction.in_progress"},
    )


def _publish_compaction_completed(session_id: str, total_tokens: int | None) -> None:
    """
    Publish the compaction-finished event to a session stream.

    Emitted after :func:`compact_conversation_now` returns
    successfully. Clients that rendered a spinner on the
    ``response.compaction.in_progress`` event should upgrade it to
    the permanent "Conversation compacted" marker on this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        context size, e.g. ``8421``. ``None`` when unavailable.
    """
    payload: dict[str, object] = {"type": "response.compaction.completed"}
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens
    session_stream.publish(session_id, payload)


def _publish_compaction_failed(session_id: str) -> None:
    """
    Publish the compaction-failed event to a session stream.

    Emitted when :func:`compact_conversation_now` raises. Clients
    that rendered a spinner on the
    ``response.compaction.in_progress`` event should dismiss it
    without leaving a permanent marker — the conversation history
    was not modified.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    session_stream.publish(session_id, {"type": "response.compaction.failed"})


def _publish_external_assistant_message(
    session_id: str,
    item: ConversationItem,
    *,
    response_id: str,
    agent_name: str,
) -> None:
    """
    Broadcast an assistant message appended outside the task runtime.

    Terminal-backed integrations such as native Claude produce output
    in a live terminal first, then mirror the semantic text into AP.
    There is no ``agent_task`` to watch, so this helper publishes the
    completed output item directly. The browser reducer renders the
    persisted message content from ``response.output_item.done``;
    emitting synthetic text deltas here would duplicate the same
    transcript item when the snapshot path also sees it.

    :param session_id: Session/conversation identifier.
    :param item: Persisted assistant message item.
    :param response_id: Legacy endpoint response id. The persisted
        item already carries this value, so the publisher does not
        need it separately.
    :param agent_name: Legacy endpoint agent/model name. The
        persisted item already carries this value.
    :returns: None.
    """
    del response_id, agent_name
    api_item = item.to_api_dict()
    event = OutputItemDoneEvent(type="response.output_item.done", item=api_item)
    session_stream.publish(session_id, event.model_dump())


def _resolve_llm_model(conv: Conversation | None) -> str | None:
    """
    Resolve the LLM model identifier from a conversation's agent spec.

    Uses the global agent cache to load the parsed spec and read
    ``spec.llm.model``. Returns ``None`` when the conversation has
    no agent binding or the spec cannot be loaded.

    :param conv: The conversation entity, or ``None``.
    :returns: Model string (e.g. ``"databricks-gpt-5-5"``), or
        ``None`` when unavailable.
    """
    if conv is None or conv.agent_id is None:
        return None
    try:
        from omnigent.runtime import get_agent_cache

        agent_cache = get_agent_cache()
        # The agent store is injected at app startup; access it
        # through the runtime globals.
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        return loaded.spec.llm.model if loaded.spec.llm else None
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        return None


def _resolve_harness(conv: Conversation | None) -> str | None:
    """
    Resolve the canonical harness for a conversation's bound agent.

    Mirrors :func:`_resolve_llm_model`: loads the parsed spec via the agent
    cache and returns the executor's harness
    (``executor.config["harness"]``, else ``executor.type``), canonicalized.
    Surfacing this on :class:`SessionResponse` lets the REPL render the
    active credential for the correct provider *family* — anthropic for
    claude-sdk, openai for codex / openai-agents — instead of guessing the
    family from the model string (which is wrong when the agent declares no
    model, e.g. a generic-provider launcher).

    :param conv: The conversation entity, or ``None``.
    :returns: The canonical harness (e.g. ``"openai-agents"`` or
        ``"claude-sdk"``), or ``None`` when unavailable.
    """
    if conv is None or conv.agent_id is None:
        return None
    try:
        from omnigent.harness_aliases import canonicalize_harness
        from omnigent.runtime import get_agent_cache
        from omnigent.runtime._globals import _agent_store

        if _agent_store is None:
            return None
        agent = _agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        executor = loaded.spec.executor
        harness = executor.config.get("harness") or executor.type
        return canonicalize_harness(harness) or harness
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        return None


def _utc_day(epoch_seconds: int) -> str:
    """
    Convert a Unix epoch timestamp to its UTC calendar day.

    :param epoch_seconds: Unix epoch seconds, e.g. ``1749081600``.
    :returns: The UTC date as ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
    """
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()


def _record_daily_cost(
    conv: Conversation | None,
    delta_usd: float,
    conversation_store: ConversationStore,
) -> None:
    """
    Add a turn's LLM cost to the session owner's daily rollup.

    A no-op when *delta_usd* is not positive or the session has no
    resolvable owner. Attributes the cost to the session creator
    (:meth:`ConversationStore.get_session_owner`) and buckets it by the
    current UTC day, so a session spanning midnight splits its spend
    across both days. Recorded for every priced turn regardless of
    whether the session runs under a policy — the daily rollup is the
    backing store for the per-user daily cost-budget policy, and is now
    populated universally. (This relies on the conversation store
    implementing the daily-cost methods on every deployment that runs
    this code; the earlier policy gate that kept the managed deployment
    from touching an absent ``user_daily_cost`` table is no longer needed
    now that the managed store backs it.)

    :param conv: The conversation row for the session, or ``None``
        (a no-op — no owner to attribute to).
    :param delta_usd: The turn's cost in USD; ``<= 0`` is a no-op.
    :param conversation_store: Store for the owner lookup and the
        daily-cost UPSERT.
    """
    if conv is None or delta_usd <= 0:
        return
    owner = conversation_store.get_session_owner(conv.id)
    if owner is None:
        return
    from omnigent.db.utils import now_epoch

    conversation_store.add_daily_cost(owner, _utc_day(now_epoch()), delta_usd)


def _priced_cost_for_display(usage: dict[str, Any]) -> float | None:
    """
    Extract ``total_cost_usd`` for client display, or ``None`` when unpriced.

    The key is present only when a turn was priced, so its absence ("—" in
    the UI) is distinct from a priced ``$0.00``. The cost-budget policy is
    unaffected — it reads the value with a ``0.0`` default.

    :param usage: A conversation's ``session_usage`` dict, e.g.
        ``{"input_tokens": 1200, "total_cost_usd": 0.42}`` (priced) or
        ``{"input_tokens": 1200}`` (unpriced — no cost key).
    :returns: The cumulative cost in USD when priced, else ``None``.
    """
    if "total_cost_usd" not in usage:
        return None
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        # Defensive: a malformed persisted value must not break the
        # snapshot / SSE emit. Treat it as unpriced.
        return None


def _model_usage_bucket(usage: dict[str, Any], model: str) -> dict[str, float]:
    """
    Get-or-create the per-model usage sub-bucket inside ``usage["by_model"]``.

    The nested ``by_model`` map attributes token/cost usage to the specific
    LLM that produced it, keyed on the raw harness-reported model id (faithful
    and simplest — alias normalization is intentionally deferred). This mutates
    ``usage`` in place, creating ``by_model`` and the per-model dict on first
    use, and returns the model's bucket for the caller to increment / set.

    :param usage: The conversation's mutable ``session_usage`` dict.
    :param model: The raw harness model id, e.g. ``"claude-sonnet-4-6"`` or
        ``"databricks-gpt-5-5"``.
    :returns: The mutable per-model bucket, e.g. ``{"input_tokens": 1200}``.
    """
    by_model = usage.setdefault("by_model", {})
    return by_model.setdefault(model, {})


# Per-model token bucket keys (the five counters stored inside each
# ``by_model[<model>]`` sub-dict). Used by :func:`_usage_by_model_for_display`
# to coerce persisted values to ``int`` and by the native write path to copy
# flat session counters into the per-model bucket.
_MODEL_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _add_model_usage_delta(
    bucket: dict[str, float],
    token_deltas: dict[str, int],
    cost_delta: float | None,
) -> None:
    """
    Add one turn's per-model token/cost deltas into a model bucket (ADD).

    Mirrors the flat-counter increments in :func:`_accumulate_session_usage`
    so the per-model totals stay consistent with the flat totals: every flat
    increment is matched by an increment to exactly one model bucket, so the
    sum of per-model buckets equals the flat total. ``cost_delta`` is added
    only when the turn was priced (``None`` otherwise), preserving the
    "priced ⟺ ``total_cost_usd`` key present" contract at the per-model level.

    :param bucket: The model's mutable bucket from :func:`_model_usage_bucket`.
    :param token_deltas: This turn's per-bucket token counts to add, keyed by
        the same names as :data:`_TOKEN_BREAKDOWN_KEYS`, e.g.
        ``{"input_tokens": 1200, "output_tokens": 340, ...}``.
    :param cost_delta: This turn's priced cost in USD to add, or ``None`` when
        the turn was unpriced (the model's cost key stays absent).
    """
    for key, delta in token_deltas.items():
        bucket[key] = bucket.get(key, 0) + delta
    if cost_delta is not None:
        bucket["total_cost_usd"] = bucket.get("total_cost_usd", 0.0) + cost_delta


def _usage_by_model_for_display(usage: dict[str, Any]) -> dict[str, ModelUsage] | None:
    """
    Project the nested ``by_model`` usage map into typed :class:`ModelUsage`.

    Companion to :func:`_token_breakdown_for_display` for the per-model view:
    reads ``usage["by_model"]`` (the subtree-summed map from
    :func:`load_session_usage`) and builds a ``{model_id: ModelUsage}`` dict
    for the API. Token buckets are coerced to ``int`` and ``total_cost_usd``
    to ``float``; an absent bucket stays ``None`` on the model (so a model
    that was never priced has no cost), and malformed values are skipped.

    :param usage: A subtree-summed usage dict, e.g.
        ``{"input_tokens": 1500, "by_model": {"claude-sonnet-4-6":
        {"input_tokens": 1500, "total_cost_usd": 0.42}}}``.
    :returns: The per-model map, or ``None`` when no per-model usage is
        present (so ``exclude_none`` omits the field entirely).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict) or not by_model:
        return None
    result: dict[str, ModelUsage] = {}
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        fields: dict[str, Any] = {}
        for key in _MODEL_TOKEN_KEYS:
            value = bucket.get(key)
            if value is None:
                continue
            try:
                fields[key] = int(value)
            except (TypeError, ValueError):
                continue
        cost = _priced_cost_for_display(bucket)
        if cost is not None:
            fields["total_cost_usd"] = cost
        result[model] = ModelUsage(**fields)
    return result or None


def _accumulate_session_usage(
    resp_obj: dict[str, Any],
    session_id: str,
    conversation_store: ConversationStore,
) -> float | None:
    """
    Increment the session's cumulative token counters from a
    ``response.completed`` event's usage data.

    Called synchronously from the relay loop. Reads the current
    persisted ``session_usage``, adds the delta from the
    response's ``usage`` field, and writes the updated totals
    back. No-op when the response carries no usage data.

    Cost is computed when the model's per-token pricing is
    available from the MLflow catalog (looked up once per call
    from the response's ``model`` field). The ``total_cost_usd`` key is
    written **only when pricing is available** — an unpriced session
    leaves it absent (its presence is what distinguishes a priced
    ``$0.00`` from "unpriced"; see :func:`_priced_cost_for_display`).

    :param resp_obj: The ``response`` dict from the
        ``response.completed`` SSE event.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store for reading and writing
        the ``session_usage`` column.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or carries no
        usage to accumulate.
    """
    usage_obj = resp_obj.get("usage")
    if not isinstance(usage_obj, dict):
        return None
    input_tokens = usage_obj.get("input_tokens", 0)
    output_tokens = usage_obj.get("output_tokens", 0)
    total_tokens = usage_obj.get("total_tokens", 0)
    if not any((input_tokens, output_tokens, total_tokens)):
        return None

    cache_read_input_tokens = usage_obj.get("cache_read_input_tokens", 0)
    cache_creation_input_tokens = usage_obj.get("cache_creation_input_tokens", 0)

    # Load current cumulative usage from the store.
    conv = conversation_store.get_conversation(session_id)
    current = dict(conv.session_usage) if conv else {}
    current.setdefault("input_tokens", 0)
    current.setdefault("output_tokens", 0)
    current.setdefault("total_tokens", 0)
    current.setdefault("cache_read_input_tokens", 0)
    current.setdefault("cache_creation_input_tokens", 0)

    current["input_tokens"] += input_tokens
    current["output_tokens"] += output_tokens
    current["total_tokens"] += total_tokens
    current["cache_read_input_tokens"] += cache_read_input_tokens
    current["cache_creation_input_tokens"] += cache_creation_input_tokens

    # Compute cost delta if pricing is available for the model. Resolve
    # the model to price with, most-specific first:
    #   1. ``usage.model`` — the model the harness actually used this turn.
    #      Relay executors report it; it's the only signal when the spec
    #      pins no ``llm.model`` (a supervisor that delegates / uses the
    #      harness default), so it's what makes those sessions priceable.
    #   2. the session's ``model_override`` (a ``/model`` switch).
    #   3. the agent spec's ``llm.model`` (the static default).
    # The response's top-level ``model`` is the AGENT NAME, not the LLM
    # model, so it is never used here. The ``total_cost_usd`` key is
    # created only on this priced branch, so an unpriced session never
    # gains a (misleading $0.00) cost key.
    cost_delta = 0.0
    usage_model = usage_obj.get("model")
    llm_model = (
        usage_model
        if isinstance(usage_model, str) and usage_model
        else (conv.model_override if conv and conv.model_override else _resolve_llm_model(conv))
    )
    if llm_model:
        from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

        pricing = fetch_model_pricing(llm_model)
        priced = pricing is not None
        if pricing is not None:
            # Cache-aware: usage_obj carries cache_read/cache_creation
            # token counts when the harness reports them; compute_llm_cost
            # prices them at their own (cheaper read / pricier write) rates.
            cost_delta = compute_llm_cost(usage_obj, pricing)
            current["total_cost_usd"] = current.get("total_cost_usd", 0.0) + cost_delta
        # Per-model attribution (ADD). Tokens are attributed whenever the
        # model is known — including unpriced turns — so the per-model token
        # view is complete; cost is attributed only when this model's turn
        # was priced (passing ``None`` otherwise keeps the model's cost key
        # absent, matching the flat "priced ⟺ key present" contract).
        _add_model_usage_delta(
            _model_usage_bucket(current, llm_model),
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
            },
            cost_delta if priced else None,
        )

    conversation_store.set_session_usage(session_id, current)
    # Per-user daily rollup (policy-gated; this is the per-turn delta).
    _record_daily_cost(conv, cost_delta, conversation_store)
    return _priced_cost_for_display(current)


def _persist_native_cumulative_usage(
    session_id: str,
    data: dict[str, Any],
    conversation_store: ConversationStore,
) -> float | None:
    """
    Persist cumulative cost / token usage reported by a native harness.

    Unlike the Omnigent relay path (:func:`_accumulate_session_usage`), which adds
    per-response *deltas*, native harnesses (claude-native / codex-native)
    report *cumulative* session usage — so this writes with SET semantics, not
    add. The two paths never run for the same session, so they don't conflict.

    Reads explicit cumulative fields from the ``external_session_usage`` event's
    ``data`` (all optional; a no-op when none are present):

    - ``cumulative_cost_usd`` — total session cost for DISPLAY, e.g.
      claude-native forwards Claude Code's own ``cost.total_cost_usd``
      (exact billing; used directly). Stored in ``total_cost_usd``, which
      drives the badge and the per-user daily rollup, so the badge matches
      ``/cost`` in the Claude TUI.
    - ``policy_cost_usd`` — total session cost for ENFORCEMENT (the
      cost-budget gate). claude-native forwards ``max(S, real-time
      transcript estimate)`` here so the gate reflects in-flight sub-agent
      spend while the displayed ``S`` is frozen for the sub-agent's run.
      Stored verbatim in ``policy_cost_usd`` (the policy engine seeds from
      it, falling back to ``total_cost_usd`` when absent). Not fed into the
      daily rollup — that uses the authoritative ``total_cost_usd``.
    - ``cumulative_input_tokens`` / ``cumulative_output_tokens`` — total session
      tokens, e.g. codex-native's ``tokenUsage.total``. When
      ``cumulative_cost_usd`` is absent, cost is computed from these via
      :func:`fetch_model_pricing`.
    - ``cumulative_cache_read_input_tokens`` — the cached portion *included
      in* ``cumulative_input_tokens`` (e.g. codex-native's
      ``tokenUsage.total.cachedInputTokens``). Split out of the input total
      so :func:`compute_llm_cost` prices it at the cache-read rate rather
      than the full input rate. Absent for harnesses that don't report it.
    - ``model`` — LLM model id to price with (e.g. ``"databricks-gpt-5-5"``);
      falls back to the agent spec's model when absent.

    The ``total_cost_usd`` key is written only on the priced branches
    below (exact billing, or token-priced when the model is in the
    catalog), so an unpriced native session leaves it absent — the same
    "priced ⟺ key present" contract the relay path uses. ``policy_cost_usd``
    is written only when the event carries it (claude-native with the
    display/policy split); codex-native and the relay omit it and the
    policy engine falls back to ``total_cost_usd``.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param data: The ``external_session_usage`` event ``data`` dict.
    :param conversation_store: Store for reading and writing ``session_usage``.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or no
        cumulative field was present.
    :raises OmnigentError: When a cumulative field is the wrong type.
    """
    cost = _coerce_cumulative_field(data, "cumulative_cost_usd", numeric=True)
    policy_cost = _coerce_cumulative_field(data, "policy_cost_usd", numeric=True)
    cin = _coerce_cumulative_field(data, "cumulative_input_tokens", numeric=False)
    cout = _coerce_cumulative_field(data, "cumulative_output_tokens", numeric=False)
    ccache = _coerce_cumulative_field(data, "cumulative_cache_read_input_tokens", numeric=False)
    if cost is None and policy_cost is None and cin is None and cout is None:
        return None

    conv = conversation_store.get_conversation(session_id)
    current = dict(conv.session_usage) if conv and conv.session_usage else {}
    # Native usage is cumulative (SET semantics), so the per-turn delta
    # for the daily rollup is new_total - old_total. Capture the old
    # cumulative cost before the fields below overwrite it.
    old_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    if cin is not None:
        # The reported input total is INCLUSIVE of cached tokens (codex's
        # ``inputTokens`` counts cache reads). Split the cached portion into
        # its own bucket so compute_llm_cost prices it at the cache-read rate;
        # ``input_tokens`` keeps only the non-cached remainder (its contract).
        # Clamp cached to the total so a malformed report never makes
        # ``input_tokens`` negative.
        cached = min(int(ccache), int(cin)) if ccache is not None else 0
        current["cache_read_input_tokens"] = cached
        current["input_tokens"] = int(cin) - cached
    if cout is not None:
        current["output_tokens"] = cout
    if cin is not None or cout is not None:
        # ``total_tokens`` reflects the full input (non-cached + cached) plus
        # output, so the split above doesn't shrink the displayed total.
        current["total_tokens"] = (
            int(current.get("input_tokens", 0))
            + int(current.get("cache_read_input_tokens", 0))
            + int(current.get("output_tokens", 0))
        )

    # Resolve the model only when tokens are present — both the token-pricing
    # branch and the per-model attribution below need it, and both are gated on
    # tokens. Resolving lazily avoids calling ``_resolve_llm_model`` (which
    # touches the runtime agent cache) on a cost-only broadcast. Computed once
    # out of the pricing-only branch so attribution works even on an unpriced
    # turn. The raw harness model id wins; falls back to the agent spec's model.
    has_tokens = cin is not None or cout is not None
    model_name = (data.get("model") or _resolve_llm_model(conv)) if has_tokens else None
    if cost is not None:
        current["total_cost_usd"] = float(cost)
    elif has_tokens:
        if isinstance(model_name, str) and model_name:
            from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

            pricing = fetch_model_pricing(model_name)
            if pricing is not None:
                # SET (cumulative) — price the running token totals.
                # ``current`` carries the cache-read split when the harness
                # reports it (codex-native does), so compute_llm_cost prices
                # cache reads at their own rate; it falls back to the input
                # rate for cache tokens when the catalog omits a cache price
                # (e.g. ``databricks-*`` entries today).
                current["total_cost_usd"] = compute_llm_cost(current, pricing)

    # Per-model attribution (SET). Native harnesses report cumulative SESSION
    # totals, not per-model splits, so attribute the running cumulative buckets
    # to the current model. For the usual single-model native session this
    # makes the per-model view equal the flat totals; on a mid-session model
    # switch the current model absorbs the cumulative (splitting deferred —
    # keyed on the raw harness model id). Cost mirrors the flat
    # ``total_cost_usd`` so the per-model cost key is present iff priced.
    # ``model_name`` is only set when tokens are present, so this is skipped
    # for cost-only broadcasts (nothing to attribute per-model).
    if isinstance(model_name, str) and model_name:
        bucket = _model_usage_bucket(current, model_name)
        for key in _MODEL_TOKEN_KEYS:
            if key in current:
                bucket[key] = current[key]
        if "total_cost_usd" in current:
            bucket["total_cost_usd"] = current["total_cost_usd"]

    # Enforcement value (claude-native display/policy split). Stored
    # separately from the displayed ``total_cost_usd`` so the gate can read
    # the real-time figure (incl. in-flight sub-agent spend) while the badge
    # shows the frozen statusLine total. SET semantics, like the rest.
    if policy_cost is not None:
        current["policy_cost_usd"] = float(policy_cost)

    conversation_store.set_session_usage(session_id, current)
    # Per-user daily rollup. Native reports cumulative totals, so the turn's
    # delta is the increase in cumulative cost. Uses the authoritative
    # ``total_cost_usd`` (= statusLine S), NOT ``policy_cost_usd`` — the
    # daily report must reflect real spend, not the real-time gate estimate.
    new_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    _record_daily_cost(conv, new_cost - old_cost, conversation_store)
    return _priced_cost_for_display(current)


def _coerce_cumulative_field(
    data: dict[str, Any],
    key: str,
    *,
    numeric: bool,
) -> float | int | None:
    """
    Read and validate an optional cumulative usage field from event data.

    :param data: The ``external_session_usage`` event ``data`` dict.
    :param key: Field name, e.g. ``"cumulative_input_tokens"``.
    :param numeric: When ``True`` accept any non-negative number (cost);
        when ``False`` require a non-negative int (token counts).
    :returns: The validated value, or ``None`` when the key is absent.
    :raises OmnigentError: When present but the wrong type / negative.
    """
    value = data.get(key)
    if value is None:
        return None
    ok = (
        isinstance(value, (int, float)) if numeric else isinstance(value, int)
    ) and not isinstance(value, bool)
    if not ok or value < 0:
        raise OmnigentError(
            f"external_session_usage data.{key} must be a non-negative "
            f"{'number' if numeric else 'int'}",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


async def _persist_external_session_usage(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> int | None:
    """
    Persist and broadcast a token-usage update from a terminal-backed runtime.

    At least one of ``data.context_tokens`` (non-negative int),
    ``data.context_window`` (positive int), or a cumulative usage field
    (:func:`_persist_native_cumulative_usage`) must be present.

    :param session_id: Session/conversation identifier.
    :param body: External session-usage event body.
    :param conversation_store: Store used to upsert the labels.
    :returns: The persisted ``context_tokens`` when present, else ``None``.
    :raises OmnigentError: On missing / malformed fields.
    """
    raw_tokens = body.data.get("context_tokens")
    if raw_tokens is not None and (not isinstance(raw_tokens, int) or raw_tokens < 0):
        raise OmnigentError(
            "external_session_usage data.context_tokens must be a non-negative int",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_window = body.data.get("context_window")
    if raw_window is not None and (not isinstance(raw_window, int) or raw_window <= 0):
        raise OmnigentError(
            "external_session_usage data.context_window must be a positive int",
            code=ErrorCode.INVALID_INPUT,
        )
    _CUMULATIVE_USAGE_KEYS = (
        "cumulative_cost_usd",
        # ``policy_cost_usd`` alone is a valid post: mid-turn the displayed
        # statusLine total (``cumulative_cost_usd``) is frozen, so the
        # forwarder posts only the advancing real-time enforcement cost.
        "policy_cost_usd",
        "cumulative_input_tokens",
        "cumulative_output_tokens",
    )
    has_cumulative = any(body.data.get(k) is not None for k in _CUMULATIVE_USAGE_KEYS)
    if raw_tokens is None and raw_window is None and not has_cumulative:
        raise OmnigentError(
            "external_session_usage requires at least one of "
            "data.context_tokens, data.context_window, or a cumulative usage field",
            code=ErrorCode.INVALID_INPUT,
        )

    # Native harnesses report cumulative cost / tokens (SET semantics) — distinct
    # from the Omnigent relay's per-response accumulation. Persist this session's
    # own cumulative usage (its priced own-cost return is unused — the badge shows
    # the subtree total computed below, not own cost).
    await asyncio.to_thread(
        _persist_native_cumulative_usage,
        session_id,
        body.data,
        conversation_store,
    )

    label_updates: dict[str, str] = {}
    if raw_tokens is not None:
        label_updates[_LAST_CONTEXT_TOKENS_LABEL_KEY] = str(raw_tokens)
    if raw_window is not None:
        label_updates[_LAST_CONTEXT_WINDOW_LABEL_KEY] = str(raw_window)
    await asyncio.to_thread(
        conversation_store.set_labels,
        session_id,
        label_updates,
    )
    # The displayed cost is this session's SUBTREE total (itself + its
    # sub-agents), matching the GET snapshot. A sub-agent persists its spend on
    # its own child conversation, so broadcasting only this session's own cost
    # would drop a parent's badge back to own-cost on every parent flush and
    # hide in-flight sub-agent spend until the next child flush (the badge would
    # oscillate own ⇄ subtree). For a childless session the subtree is just
    # itself, so this equals own cost — one indexed tree query per flush.
    subtree_usage = await asyncio.to_thread(load_session_usage, session_id, conversation_store)
    subtree_cost = _priced_cost_for_display(subtree_usage)
    usage_by_model = _usage_by_model_for_display(subtree_usage)
    # Only include fields that were sent; the client treats absent
    # fields as "no change" so a window-only update doesn't zero tokens.
    # ``total_cost_usd`` is included only when the subtree is priced
    # (``exclude_none`` strips it otherwise) — an unpriced session keeps
    # showing "—" from the snapshot rather than a misleading $0.00.
    event_payload: dict[str, Any] = {
        "type": "session.usage",
        "conversation_id": session_id,
    }
    if raw_tokens is not None:
        event_payload["context_tokens"] = raw_tokens
    if raw_window is not None:
        event_payload["context_window"] = raw_window
    if subtree_cost is not None:
        event_payload["total_cost_usd"] = subtree_cost
    if usage_by_model is not None:
        event_payload["usage_by_model"] = usage_by_model
    event = SessionUsageEvent(**event_payload)
    session_stream.publish(session_id, event.model_dump(exclude_none=True))
    # This session's usage also moves its ANCESTORS' subtree cost (its spend
    # rolls up into every ancestor), so re-publish each ancestor's subtree cost
    # too — otherwise a grandparent's badge wouldn't reflect a deep descendant.
    # No-op for a top-level session (no ancestors). Threaded: it pages the
    # conversation tree per ancestor.
    await asyncio.to_thread(
        _publish_subtree_cost_to_ancestors,
        conversation_store,
        session_id,
    )
    return raw_tokens


async def _persist_external_model_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast a model switch made inside the terminal.

    Mirrors a ``/model`` change typed into a claude-native session's
    Claude Code pane (or picked via its in-TUI model picker) onto the
    Omnigent session: writes ``model_override`` so the value survives reload
    and publishes a ``session.model`` SSE event so the web picker
    updates live. Unlike the PATCH path
    (:func:`update_session`), this deliberately does NOT forward a
    ``model_change`` back to the runner — the terminal is already on
    the model, so re-injecting ``/model`` would loop.

    No-ops (no write, no event) when the observed model already equals
    the persisted ``model_override`` — the common case on the web→TUI
    round-trip where the web PATCH set the override moments earlier.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` (read at the route
        boundary); ``conv.model_override`` is the dedupe baseline.
    :param body: External model-change event body. ``data.model`` must
        be a non-empty string tier alias, e.g. ``"opus"``.
    :param conversation_store: Store used to upsert ``model_override``.
    :raises OmnigentError: If ``data.model`` is missing or not a
        non-empty string.
    """
    raw_model = body.data.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise OmnigentError(
            "external_model_change requires data.model to be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    model = raw_model.strip()
    if conv.model_override == model:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        model_override=model,
    )
    event = SessionModelEvent(
        type="session.model",
        conversation_id=session_id,
        model=model,
    )
    session_stream.publish(session_id, event.model_dump())


async def _persist_model_change_note(
    session_id: str,
    model_override: str | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Append a ``[System: ...]`` transcript note recording a model switch.

    Records a web/REPL ``/model`` change as a user-role system marker
    (the web UI renders ``[System: ...]`` user messages centered + muted
    via ``SystemMessageView``) so the user gets a durable record in the
    conversation that the switch happened — not just a transient composer
    hint. Persisted through the store as append-only history (does NOT
    start an agent turn, unlike the message-post path) and published over
    SSE so connected clients render it live.

    The caller gates this to **non-native** sessions and to real ``/model``
    commands: claude-native / codex-native manage their model through the
    in-TUI picker / launch flag and must not receive an injected AP-side
    item, and ``silent`` bind-time auto-applies are skipped (see the
    ``live_forward`` guard in ``update_session``). The note is a user-role
    message, so the agent sees it in history on the next turn — consistent
    with other ``[System: ...]`` markers (timer fired, sub-agent done).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param model_override: The new model id, e.g.
        ``"databricks-gpt-5-4"``, or ``None`` when the override was
        cleared back to the agent default.
    :param conversation_store: Store used to append the note item.
    :returns: None.
    """
    text = (
        f"[System: model changed to {model_override}]"
        if model_override is not None
        else "[System: model reset to the agent default]"
    )
    item = NewConversationItem(
        type="message",
        response_id=generate_task_id(),
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    _publish_external_conversation_item(session_id, persisted_items[0])


def _handle_external_session_todos(
    session_id: str,
    body: SessionEventInput,
) -> None:
    """
    Cache and broadcast a todo-list update from the claude-native forwarder.

    Updates the in-memory ``_session_todos_cache`` so subsequent
    ``GET /v1/sessions/{id}`` snapshot calls can populate the ``todos``
    field without a file read. Then publishes a ``session.todos`` SSE event
    so connected ap-web clients update their todo panel immediately.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param body: The ``external_session_todos`` event body. Must have
        ``data.todos`` as a list of todo dicts, e.g.
        ``[{"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing the bug"}]``.
    :raises OmnigentError: When ``data.todos`` is missing or not a list.
    """
    todos = body.data.get("todos")
    if not isinstance(todos, list):
        raise OmnigentError(
            "external_session_todos requires data.todos to be a list",
            code=ErrorCode.INVALID_INPUT,
        )
    # Filter to well-formed items before caching so that malformed entries
    # from a buggy forwarder version don't persist in the snapshot.  The
    # same filter is applied by sse.ts on the live-event path; keeping the
    # two in sync means the snapshot and live panel always show the same set.
    valid_statuses = {"pending", "in_progress", "completed"}
    validated: list[dict[str, Any]] = [
        t
        for t in todos
        if isinstance(t, dict)
        and isinstance(t.get("content"), str)
        and t.get("status") in valid_statuses
        and isinstance(t.get("activeForm"), str)
    ]
    _session_todos_cache[session_id] = validated
    event = SessionTodosEvent(
        type="session.todos",
        conversation_id=session_id,
        todos=validated,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_external_conversation_item(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Broadcast a terminal-observed conversation item.

    User messages use ``session.input.consumed`` so the web UI renders
    them exactly like local/composer messages. Assistant/tool-side
    items use ``response.output_item.done`` because they are already
    completed records from Claude's transcript, not token deltas from
    an active Omnigent task.

    :param session_id: Session/conversation identifier.
    :param item: Persisted conversation item.
    :param cleared_pending_id: For a native user message, the id of the
        optimistic pending-input entry the caller drained for it (so
        clients drop that bubble by id), or ``None``. The drain happens
        at the persist site — see :func:`_persist_external_conversation_item`
        — because it also folds the entry's file blocks into the durable
        item before append.
    :returns: None.
    """
    if item.type == "message" and isinstance(item.data, MessageData) and item.data.is_meta:
        return
    if item.type == "message" and isinstance(item.data, MessageData) and item.data.role == "user":
        _publish_input_consumed(session_id, item, cleared_pending_id=cleared_pending_id)
        return
    event = OutputItemDoneEvent(type="response.output_item.done", item=item.to_api_dict())
    session_stream.publish(session_id, event.model_dump())


def _publish_external_output_text_delta(session_id: str, body: SessionEventInput) -> None:
    """
    Broadcast a terminal-observed assistant text delta.

    Terminal-backed integrations can observe streaming output before
    their completed transcript item is available. This publishes the
    standard Responses-style text-delta SSE event without persisting
    anything; the final assistant message is persisted separately when
    the integration posts ``external_conversation_item``.

    The optional ``message_id`` / ``index`` / ``final`` fields are
    carried through when present (claude-native live streaming) and
    omitted otherwise — ``exclude_none`` keeps the wire shape identical
    to in-process task streaming for callers that don't set them.

    :param session_id: Session/conversation identifier.
    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE`.
    :returns: None.
    :raises OmnigentError: If ``data.delta`` is not a string, or any
        provided ``message_id`` / ``index`` / ``final`` has the wrong
        type.
    """
    delta = body.data.get("delta")
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_output_text_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    message_id = body.data.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        raise OmnigentError(
            "external_output_text_delta data.message_id must be a string",
            code=ErrorCode.INVALID_INPUT,
        )
    index = body.data.get("index")
    # ``bool`` is an ``int`` subclass; reject it explicitly so a stray
    # boolean index is a loud error rather than a silent 0/1.
    if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
        raise OmnigentError(
            "external_output_text_delta data.index must be an integer",
            code=ErrorCode.INVALID_INPUT,
        )
    final = body.data.get("final")
    if final is not None and not isinstance(final, bool):
        raise OmnigentError(
            "external_output_text_delta data.final must be a boolean",
            code=ErrorCode.INVALID_INPUT,
        )
    event = OutputTextDeltaEvent(
        type="response.output_text.delta",
        delta=delta,
        message_id=message_id,
        index=index,
        final=final,
    )
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


def _publish_elicitation_resolved(session_id: str, elicitation_id: str) -> None:
    """
    Universal "approval done" signal — single publish drives both
    sidebar (via :func:`pending_elicitations.record_publish` decrement)
    and the chat-side ``ApprovalCard`` flip on every live subscriber.
    Idempotent on duplicate emissions for the same id.

    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Correlation id, e.g. ``"elicit_abc123"``.
    """
    session_stream.publish(
        session_id,
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": elicitation_id,
        },
    )


async def _forward_approval_to_runner(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
) -> None:
    """
    Forward an approval verdict to the session's bound runner.

    Runner-side elicitations (policy approvals parked in the runner's
    ``_pending_approvals`` dict, scaffold dispatch) resolve when the
    canonical ``approval`` event reaches the runner's ``/events``. The
    server↔runner contract stays the ``approval`` event regardless of
    how the verdict arrived at the server (resolve URL or approval
    event). No-op when no runner is bound (in-process setups). HTTP
    errors are logged, not raised — a dead runner must not fail the
    caller's resolution (the server-side Future was already set).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param data: The approval payload to forward verbatim as the
        event ``data``, e.g. ``{"elicitation_id": "elicit_abc",
        "action": "accept"}``.
    :param runner_router: Router used to resolve the bound runner, or
        ``None`` in in-process setups (forward skipped).
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _APPROVAL_TYPE, "data": data},
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.exception(
            "Approval forward failed for %r",
            session_id,
        )


async def _resolve_elicitation(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
    conversation_store: ConversationStore | None = None,
) -> None:
    """
    Resolve one outstanding elicitation from an approval payload.

    Shared by the two entry points that deliver a verdict for a
    parked elicitation: the ``type == "approval"`` branch of
    ``POST /v1/sessions/{id}/events`` and the dedicated
    ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` URL
    endpoint (URL-based elicitation). Both converge here so
    resolution semantics — server-side harness Future, sidebar
    badge clear, and runner forward — stay identical regardless of
    how the verdict arrived.

    Three effects, in order:

    1. **Server-side harness Future.** Claude-native permission
       hooks (and any other server-parked elicitation) register a
       Future in ``_harness_elicitation_registry``. If one exists
       for this id, is unresolved, and is owned by *this* session
       (cross-user guard), set its result. An
       ownership mismatch silently skips resolution — the runner
       forward below still fires so a runner-side elicitation with
       the same id can reject it on its own terms.
    2. **Sidebar badge clear.** Publish
       ``response.elicitation_resolved`` so every subscribed client
       (other tabs, the REPL TUI) flips its ``ApprovalCard`` and the
       pending-elicitation badge decrements. Idempotent.
    3. **Runner forward.** Runner-side elicitations (policy
       approvals parked in the runner's ``_pending_approvals`` dict)
       resolve when the approval event reaches the runner's
       ``/events``. Forwarded as a canonical ``approval`` event.

    :param session_id: Session/conversation identifier that owns
        the elicitation, e.g. ``"conv_abc123"``.
    :param data: Approval payload carrying the ``elicitation_id``
        correlation key plus the MCP ``ElicitationResult`` fields
        (``action``, optional ``content``), e.g.
        ``{"elicitation_id": "elicit_abc", "action": "accept"}``.
    :param runner_router: Router used to resolve the session's bound
        runner for the forward, or ``None`` in in-process setups
        (the forward is skipped when no runner is bound).
    :param conversation_store: Optional store used to mirror the
        resolved signal into ancestor streams when ``session_id`` is
        a child session. ``None`` keeps the signal scoped locally.
    """
    # Empty-string default is intentional, NOT a fail-loud miss: the
    # resolve-URL caller always supplies the id (it comes from the URL
    # path), but the public ``approval`` event caller may post a
    # malformed body. A missing id degrades gracefully below (no Future
    # matches, no resolved event published) rather than 500-ing the
    # client — the runner forward still fires so the runner can reject.
    elicitation_id = data.get("elicitation_id", "")
    harness_future = _harness_elicitation_registry.get(elicitation_id)
    if harness_future is not None and not harness_future.done():
        # Only the session that owns this elicitation
        # may resolve its server-side Future. A mismatch skips
        # resolution (the runner forward still fires below).
        if _harness_elicitation_owners.get(elicitation_id) == session_id:
            result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
            try:
                harness_future.set_result(
                    ElicitationResult.model_validate(result_payload),
                )
            except ValidationError:
                _logger.warning(
                    "Invalid approval payload for %r",
                    elicitation_id,
                    exc_info=True,
                )
    elif harness_future is None and isinstance(elicitation_id, str) and elicitation_id:
        # Nothing parked (severed long-poll mid-retry, or a runner-side
        # id that just ages out) — tombstone the verdict so a re-park
        # returns it; consume is session-checked, so no cross-session use.
        result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
        try:
            pre_resolved = ElicitationResult.model_validate(result_payload)
        except ValidationError:
            pre_resolved = None
        if pre_resolved is not None:
            _prune_pre_resolved_harness_elicitations()
            _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
                session_id=session_id,
                created_at=time.time(),
                result=pre_resolved,
            )
            _prune_pre_resolved_harness_elicitations()
    # Fan-out for every other subscribed client (other tabs, REPL
    # TUI). Idempotent vs. the runner's own ``wait_for_user_approval``
    # finally / harness hook finally — those also publish for the id.
    if isinstance(elicitation_id, str) and elicitation_id:
        _publish_elicitation_resolved(session_id, elicitation_id)
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
            )
    # Runner-side elicitations (policy approvals, scaffold dispatch)
    # resolve when the canonical approval event reaches the runner.
    await _forward_approval_to_runner(session_id, data, runner_router)


# Fire-and-forget tasks that ask the bound runner to pop a native-terminal
# approval modal for a parked tool-policy ASK. Kept referenced so they aren't
# garbage-collected before the POST completes.
_native_popup_forward_tasks: set[asyncio.Task[None]] = set()


def _spawn_native_approval_popup_forward(
    session_id: str, elicitation_id: str, message: str, policy_name: str | None = None
) -> None:
    """
    Ask the bound runner to pop a native-terminal modal for a parked ASK.

    Fire-and-forget. Forwards the same ``cost_approval_popup`` control event
    the cost gate uses — the runner dispatch + popup launcher are
    policy-agnostic — so a user working in the native terminal can answer a
    parked tool-policy ASK there, not only in the web ApprovalCard. (Native
    tool-policy ASKs were moved server-side, which took them out of the
    TUI; this puts them back.) The popup resolves the SAME elicitation via
    the same resolve endpoint the web card uses, so whichever surface
    answers first releases the gate. Non-native harnesses 204 no-op on the
    runner.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: The parked elicitation's id, e.g. ``"elicit_x"``.
    :param message: The approval reason shown in the popup.
    :param policy_name: Name of the deciding policy, rendered as the
        popup header so it reflects the actual policy rather than a
        hardcoded cost-budget label. ``None`` falls back to a generic
        header on the runner.
    :returns: None. Fire-and-forget: forwarding failures (runner offline,
        no runner bound) are swallowed by ``_forward_session_change_to_runner``
        and never block the gate — the web ApprovalCard remains the surface.
    """

    async def _forward() -> None:
        await _forward_session_change_to_runner(
            session_id,
            _server_runner_router,
            {
                "type": "cost_approval_popup",
                "elicitation_id": elicitation_id,
                "message": message,
                "policy_name": policy_name,
            },
        )

    task = asyncio.create_task(_forward())
    _native_popup_forward_tasks.add(task)
    task.add_done_callback(_native_popup_forward_tasks.discard)


async def _hold_native_ask_gate(
    request: Request,
    *,
    session_id: str,
    phase: Phase,
    data: dict[str, Any],
    engine: PolicyEngine,
    result: PolicyResult,
    conversation_store: ConversationStore,
) -> bool:
    """
    Hold a native-harness tool call until a human resolves the ASK.

    URL-based elicitation for native harnesses. Publishes a
    ``response.elicitation_request`` (the web UI / REPL render the
    approve card) and parks a server-side Future via
    :func:`_publish_and_wait_for_harness_elicitation`, exactly as the
    ``PermissionRequest`` hook does. The human approves through the
    elicitation's resolve URL; this collapses the verdict to a single
    boolean the caller maps to ALLOW / DENY.

    Unlike the old ASK→``defer`` path, the gate lives on the server,
    so a permissive native ``permission_mode`` (``acceptEdits`` /
    ``bypassPermissions``) cannot skip it — the tool call stays
    blocked until a real human verdict. Timeout / disconnect fail
    closed (return ``False`` → DENY).

    On approve, the ASK-accumulated ``set_labels`` / ``state_updates``
    are applied (POLICIES.md §7.2: side effects land only on approve);
    a denied / timed-out ASK leaves no trace.

    :param request: FastAPI request, for upstream-disconnect detection
        inside the parking helper.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param phase: Enforcement phase — :attr:`Phase.TOOL_CALL` here
        (the only phase a native PreToolUse hook can block).
    :param data: The proto event ``data``; for a tool call,
        ``{"name": "Bash", "arguments": {"command": "ls"}}``.
    :param engine: The policy engine, used to resolve the per-policy
        ``ask_timeout`` and to apply approved side effects.
    :param result: The composed ASK :class:`PolicyResult` — carries
        the reason, deciding_policy, and withheld set_labels.
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :returns: ``True`` iff a human accepted; ``False`` on decline /
        cancel / timeout / disconnect (fail closed).
    """
    tool_name = data.get("name")
    tool_input = data.get("arguments")
    params = ElicitationRequestParams(
        mode="form",
        message=result.reason or "Approval required",
        requestedSchema={},
        phase=phase.value,
        policy_name=result.deciding_policy or "unknown",
        content_preview=json.dumps(data)[:1024],
    )
    # Per-policy ``ask_timeout`` override wins over the spec-level default.
    timeout_s = float(resolve_ask_timeout(engine, result))
    # Mint the id up front so we can also surface this ASK in the native
    # terminal (a tmux popup) before parking on the web verdict; both
    # surfaces resolve the same id, so whichever answers first releases the
    # gate.
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    _spawn_native_approval_popup_forward(
        session_id, elicitation_id, params.message, result.deciding_policy
    )
    verdict = await _publish_and_wait_for_harness_elicitation(
        request,
        session_id=session_id,
        params=params,
        timeout_s=timeout_s,
        elicitation_id=elicitation_id,
        conversation_store=conversation_store,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        tool_input=tool_input if isinstance(tool_input, dict) else None,
    )
    approved = verdict is not None and verdict.action == "accept"
    if approved:
        # POLICIES.md §7.2: writes accumulated by the ASKing policy
        # land only on approve.
        if result.set_labels:
            engine.apply_label_writes(result.set_labels)
        if result.state_updates:
            engine.apply_state_updates(result.state_updates)
    return approved


def _parse_external_assistant_message(
    body: SessionEventInput,
) -> tuple[str, str, str]:
    """
    Validate and unpack an external assistant-message event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_ASSISTANT_MESSAGE_TYPE`.
    :returns: ``(agent_name, text, response_id)``.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    agent_name = body.data.get("agent")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise OmnigentError(
            "external_assistant_message requires data.agent",
            code=ErrorCode.INVALID_INPUT,
        )
    text = body.data.get("text")
    if not isinstance(text, str) or not text:
        raise OmnigentError(
            "external_assistant_message requires non-empty data.text",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_assistant_message data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    return agent_name.strip(), text, response_id.strip()


async def _persist_external_assistant_message(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist and broadcast assistant text produced outside Omnigent tasks.

    The event is append-only conversation history. It intentionally
    bypasses the legacy persist path so mirroring a
    Claude terminal response does not create or steer an Omnigent
    agent task.

    :param session_id: Session/conversation identifier.
    :param body: External assistant-message event body.
    :param conversation_store: Store used to append the message.
    :returns: Store-assigned conversation item id.
    """
    agent_name, text, response_id = _parse_external_assistant_message(body)
    item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="assistant",
            agent=agent_name,
            content=[{"type": "output_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    persisted = persisted_items[0]
    _publish_external_assistant_message(
        session_id,
        persisted,
        response_id=response_id,
        agent_name=agent_name,
    )
    return persisted.id


def _parse_external_conversation_item(
    body: SessionEventInput,
) -> NewConversationItem:
    """
    Validate and unpack an external conversation-item event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_CONVERSATION_ITEM_TYPE`.
    :returns: A parsed :class:`NewConversationItem` ready to append.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    item_type = body.data.get("item_type")
    if not isinstance(item_type, str) or item_type not in ITEM_TYPE_TO_DATA_CLS:
        raise OmnigentError(
            "external_conversation_item requires known data.item_type",
            code=ErrorCode.INVALID_INPUT,
        )
    item_data = body.data.get("item_data")
    if not isinstance(item_data, dict):
        raise OmnigentError(
            "external_conversation_item requires object data.item_data",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_conversation_item data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    # NOTE: external conversation items are persisted with a random
    # primary key like any other item — there is no server-side dedup.
    # Producers (the claude-native / codex-native forwarders) are
    # responsible for not re-posting records they have already sent;
    # they no longer emit a ``source_id`` dedup key to the server.
    # Cap a native tool result so a multi-MB output isn't persisted + broadcast as one frame.
    if item_type == "function_call_output" and isinstance(item_data.get("output"), str):
        item_data = {**item_data, "output": cap_tool_output(item_data["output"])}
    try:
        data = parse_item_data(item_type, {"type": item_type, **item_data})
    except (ValueError, TypeError) as exc:
        raise OmnigentError(
            f"Invalid data payload for external item type {item_type!r}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return NewConversationItem(
        type=item_type,
        response_id=response_id.strip(),
        data=data,
    )


def _find_claude_native_subagent_child(
    conversation_store: ConversationStore,
    parent_id: str,
    subagent_id: str,
) -> Conversation | None:
    """
    Look up an existing claude-native sub-agent child by its Claude-
    side ``subagent_id``.

    Used to make :func:`_persist_external_subagent_start` idempotent:
    the forwarder retries on transient HTTP errors, so two POSTs may
    carry the same ``subagent_id`` for the same physical sub-agent —
    we want both to resolve to the same child Conversation row.

    :param conversation_store: Store to query.
    :param parent_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param subagent_id: Stable Claude-side identifier read from
        ``agent-<id>.meta.json``'s directory name, e.g.
        ``"a5c7effac5a9a35ab"``.
    :returns: The matching child :class:`Conversation`, or ``None``
        when no row has been minted for this sub-agent yet.
    """
    # Page through all children so the lookup isn't capped by result
    # ordering. A parent with > 100 sub-agents would otherwise miss the
    # existing row for an older ``subagent_id`` and fall through to
    # ``create_conversation``, which then trips the
    # ``(parent, title)`` unique constraint instead of returning the
    # existing child id.
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.labels.get(_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY) == subagent_id:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def _find_subagent_child_by_title(
    conversation_store: ConversationStore,
    parent_id: str,
    title: str,
) -> Conversation | None:
    """
    Look up an existing sub-agent child by its exact title.

    Recovery path for duplicate-title races: when ``create_conversation``
    trips the ``(parent_conversation_id, title)`` unique index but the
    label-based idempotency lookup missed — the original POST crashed
    after creating the row and before ``set_labels`` ran — the row can
    only be found by the title itself. Native sub-agent titles embed the
    stable harness-side id (e.g. ``"Explore:a5c7effac5a9a35ab"``,
    ``"codex-native-ui-subagent:<thread_id>"``), so an exact title match
    under the same parent identifies the same physical sub-agent.

    :param conversation_store: Store to query.
    :param parent_id: Parent conversation id, e.g. ``"conv_parent987"``.
    :param title: Exact child title, e.g. ``"Explore:a5c7effac5a9a35ab"``.
    :returns: Matching child :class:`Conversation`, or ``None`` when no
        row under *parent_id* carries that title.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.title == title:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def _publish_session_created(
    parent_id: str,
    child_session_id: str,
    agent_id: str | None,
) -> None:
    """
    Emit ``session.created`` on the parent's stream for a child session.

    Clients watching the parent (e.g. the ap-web Subagents rail tab)
    invalidate their ``child_sessions`` cache and re-fetch on this
    event.

    :param parent_id: Parent conversation id, e.g. ``"conv_parent987"``.
    :param child_session_id: The minted (or adopted) child id, e.g.
        ``"conv_child456"``.
    :param agent_id: Agent id stamped on the child (the parent's
        agent), e.g. ``"ag_abc123"``. ``None`` only for legacy parents
        without one.
    """
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id=parent_id,
        child_session_id=child_session_id,
        agent_id=agent_id,
        parent_session_id=parent_id,
    )
    session_stream.publish(parent_id, event.model_dump())


async def _persist_external_subagent_start(
    parent_id: str,
    parent_conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Mint a child :class:`Conversation` row for a claude-native
    sub-agent and emit the parent's ``session.created`` SSE event.

    Claude Code spawns sub-agents internally via its Task tool and
    never POSTs to Omnigent to register them. The forwarder watches the
    parent's on-disk ``subagents/`` directory and calls this handler
    when a new ``.meta.json`` appears. We reuse the parent's
    ``agent_id`` (claude-native sub-agents don't have their own
    omnigent agent), stamp identifying labels, and publish the
    same ``session.created`` event omnigent-spawned children fire
    so the rail's ``child_sessions`` cache invalidates.

    Idempotent: a second POST with the same ``subagent_id`` returns
    the existing child's id without creating a duplicate — via the
    label lookup when the row is fully stamped, or via title-collision
    recovery when an earlier POST died between ``create_conversation``
    and ``set_labels`` (the recovery also re-stamps the labels so the
    row is healed for subsequent deliveries).

    :param parent_id: Parent (claude-native) conversation id,
        e.g. ``"conv_parent987"``.
    :param parent_conv: Pre-fetched parent row — its ``agent_id`` is
        copied onto the child and its labels disambiguate
        claude-native parents from other harnesses.
    :param body: The POST event body. Required ``data`` keys:
        ``subagent_id`` (Claude-side id, e.g. ``"a5c7eff..."``),
        ``agent_type`` (e.g. ``"Explore"``), ``description``
        (free-form, used in the title), ``tool_use_id``
        (e.g. ``"toolu_..."``).
    :param conversation_store: Store used to read existing children
        (for idempotency) and create the new row.
    :returns: The child conversation id, e.g. ``"conv_child456"``.
    :raises OmnigentError: 400 if the payload is missing any of
        the required keys; 400 if the parent has no ``agent_id``
        (claude-native parents always carry one, so this would be
        a corrupted row).
    """
    subagent_id = body.data.get("subagent_id")
    agent_type = body.data.get("agent_type")
    description = body.data.get("description")
    tool_use_id = body.data.get("tool_use_id")
    if not isinstance(subagent_id, str) or not subagent_id:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.subagent_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(agent_type, str) or not agent_type:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.agent_type",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(description, str):
        raise OmnigentError(
            "external_subagent_start requires data.description (string)",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise OmnigentError(
            "external_subagent_start requires non-empty data.tool_use_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if parent_conv.agent_id is None:
        # claude-native parents are always created with an agent_id
        # by ``omnigent claude`` (the synthetic Claude bundle).
        # A null agent_id here means we're being called against a
        # legacy / corrupt row — fail loud rather than silently
        # mint a child without a parent agent.
        raise OmnigentError(
            f"parent session {parent_id!r} has no agent_id; cannot "
            "create a claude-native sub-agent child",
            code=ErrorCode.INVALID_INPUT,
        )

    # Idempotency: a forwarder retry with the same subagent_id must
    # resolve to the same child row, not mint a duplicate. The
    # forwarder also persists its own cursor file so this should be
    # rare, but the network is unreliable and the cursor write
    # happens after the POST.
    existing = await asyncio.to_thread(
        _find_claude_native_subagent_child,
        conversation_store,
        parent_id,
        subagent_id,
    )
    if existing is not None:
        return existing.id

    # Title format mirrors omnigent-spawned children
    # (``"{tool}:{session_name}"``) so the rail's split-on-colon
    # parser surfaces the same ``tool`` shape. The ``session_name``
    # half must be unique per parent because the conversation store
    # has a ``(parent_conversation_id, title)`` unique index — using
    # the description here would collide whenever Claude's LLM
    # passes the same agentType + description for parallel
    # sub-agents (which the Task tool does routinely). The
    # ``subagent_id`` is the only stable per-sub-agent identifier
    # in the meta file, so it goes here. The human-readable
    # description is stored as a label below for downstream surfaces
    # that want it; the rail's ``SubagentsPanel`` already hides the
    # ``session_name`` half so the user only sees ``agent_type``.
    title = f"{agent_type}:{subagent_id}"
    labels = {
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY: subagent_id,
        _CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY: tool_use_id,
        _CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY: description,
    }

    try:
        child = await asyncio.to_thread(
            conversation_store.create_conversation,
            kind="sub_agent",
            title=title,
            parent_conversation_id=parent_id,
            agent_id=parent_conv.agent_id,
            runner_id=parent_conv.runner_id,
            sub_agent_name=agent_type,
        )
    except NameAlreadyExistsError:
        # The (parent, title) unique index fired: the row already exists
        # but the label-based idempotency lookup above missed it — either
        # a concurrent POST won the insert race, or an earlier POST died
        # after create_conversation and before set_labels, leaving an
        # unlabeled row. Without this recovery every forwarder redelivery
        # 500s on the same collision until the forwarder gives up and
        # parks the sub-agent (it then never appears in the rail). Adopt
        # the existing row and re-stamp its labels (idempotent upsert) so
        # the next delivery takes the fast label-lookup path.
        adopted = await asyncio.to_thread(
            _find_subagent_child_by_title,
            conversation_store,
            parent_id,
            title,
        )
        if adopted is None:
            raise
        await asyncio.to_thread(conversation_store.set_labels, adopted.id, labels)
        # The POST that created this orphan died before reaching the
        # ``session.created`` publish below, so live clients (the ap-web
        # Subagents rail) have never heard about the child — emit it now.
        # In the concurrent-race case the winner also published; a
        # duplicate event is a harmless extra cache invalidation.
        _publish_session_created(parent_id, adopted.id, parent_conv.agent_id)
        return adopted.id
    await asyncio.to_thread(conversation_store.set_labels, child.id, labels)
    _publish_session_created(parent_id, child.id, parent_conv.agent_id)
    return child.id


def _find_codex_native_subagent_child(
    conversation_store: ConversationStore,
    parent_id: str,
    thread_id: str,
) -> Conversation | None:
    """
    Look up an existing Codex-native sub-agent child by its Codex thread id.

    Makes ``_persist_external_codex_subagent_start`` idempotent: when the
    forwarder re-posts because it observed both ``item/started`` and
    ``item/completed`` for the same collab item, the second POST returns
    the existing child row rather than creating a duplicate.

    :param conversation_store: Store to query.
    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param thread_id: Codex child thread id, e.g.
        ``"019e8720-98d7-7b23-ac0a-bfb0eb02e0c9"``.
    :returns: Matching child :class:`Conversation`, or ``None`` when no
        row exists for this thread id.
    """
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            kind="sub_agent",
            parent_conversation_id=parent_id,
            limit=100,
            after=after,
        )
        for child in page.data:
            if child.labels.get(_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY) == thread_id:
                return child
        if not page.has_more or page.last_id is None:
            return None
        after = page.last_id


def _codex_subagent_display_tool(labels: dict[str, str]) -> str:
    """
    Return the UI-facing label for a Codex child session.

    Uses the Codex-assigned nickname when available, then the agent
    role, then ``"Codex"`` as a generic fallback.

    :param labels: Conversation labels from a Codex child row.
    :returns: Display label, e.g. ``"auth-auditor"``.
    """
    nickname = labels.get(_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY)
    if nickname:
        return nickname
    role = labels.get(_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY)
    if role:
        return role
    return _CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK


def _is_codex_native_subagent(conv: Conversation) -> bool:
    """
    Return whether a child conversation tracks a Codex internal sub-agent.

    :param conv: Conversation row to inspect.
    :returns: ``True`` when the row carries the codex-native sub-agent
        wrapper label.
    """
    return (
        conv.kind == "sub_agent"
        and conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
        == _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE
    )


def _codex_subagent_labels_from_body(
    thread_id: str,
    body: SessionEventInput,
) -> dict[str, str]:
    """
    Build the label dict for a Codex-native sub-agent child row.

    :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param body: Validated ``external_codex_subagent_start`` event body.
    :returns: Labels to upsert on the child conversation row.
    """
    labels: dict[str, str] = {
        _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE,
        _CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY: thread_id,
    }
    for data_key, label_key in (
        ("parent_thread_id", _CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY),
        ("tool_call_id", _CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY),
        ("prompt", _CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY),
        ("agent_nickname", _CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY),
        ("agent_role", _CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY),
    ):
        value = body.data.get(data_key)
        if isinstance(value, str) and value:
            labels[label_key] = value
    return labels


async def _create_and_publish_codex_child(
    parent_id: str,
    parent_conv: Conversation,
    thread_id: str,
    labels: dict[str, str],
    conversation_store: ConversationStore,
) -> str:
    """
    Create a new Codex child Conversation row and publish ``session.created``.

    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param parent_conv: Parent row whose ``agent_id`` and ``runner_id``
        are inherited by the child.
    :param thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param labels: Labels to stamp on the new child row.
    :param conversation_store: Store used to create the child row.
    :returns: New child conversation id, e.g. ``"conv_child456"``.
    """
    # Stable title so the (parent, title) unique index prevents race-condition
    # duplicate rows when the forwarder retries a failed registration.
    title = f"codex-native-ui-subagent:{thread_id}"
    try:
        child = await asyncio.to_thread(
            conversation_store.create_conversation,
            kind="sub_agent",
            title=title,
            parent_conversation_id=parent_id,
            agent_id=parent_conv.agent_id,
            runner_id=parent_conv.runner_id,
            sub_agent_name=_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK,
        )
    except NameAlreadyExistsError:
        # A concurrent POST (or a retry that arrived before set_labels ran)
        # already created the row — find it and upsert labels instead.
        existing = await asyncio.to_thread(
            _find_codex_native_subagent_child, conversation_store, parent_id, thread_id
        )
        if existing is None:
            # The thread-id label never landed (the original POST died
            # between create_conversation and set_labels), so the label
            # lookup can't see the row. The title embeds the same thread
            # id and must exist for the unique index to have fired — fall
            # back to it so redelivery heals the unlabeled row instead of
            # permanently 500ing.
            existing = await asyncio.to_thread(
                _find_subagent_child_by_title,
                conversation_store,
                parent_id,
                title,
            )
        if existing is not None:
            await asyncio.to_thread(conversation_store.set_labels, existing.id, labels)
            # An orphaned row's creator died before publishing
            # ``session.created``, so live clients have never heard about
            # this child — emit it now. In the concurrent-race case the
            # winner also published; the duplicate is a harmless extra
            # cache invalidation.
            _publish_session_created(parent_id, existing.id, parent_conv.agent_id)
            return existing.id
        raise
    await asyncio.to_thread(conversation_store.set_labels, child.id, labels)
    _publish_session_created(parent_id, child.id, parent_conv.agent_id)
    return child.id


async def _persist_external_codex_subagent_start(
    parent_id: str,
    parent_conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Mint or update a child Conversation for a Codex AgentControl sub-agent.

    Idempotent: repeated POSTs for the same ``thread_id`` return the
    existing child id and upsert any new labels.

    :param parent_id: Parent codex-native conversation id, e.g.
        ``"conv_parent987"``.
    :param parent_conv: Pre-fetched parent row.
    :param body: POST event body with ``data.thread_id`` required.
    :param conversation_store: Store for reading/creating child rows.
    :returns: Child conversation id, e.g. ``"conv_child456"``.
    :raises OmnigentError: If ``thread_id`` is missing or parent has
        no bound agent.
    """
    thread_id = body.data.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise OmnigentError(
            "external_codex_subagent_start requires non-empty data.thread_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if parent_conv.agent_id is None:
        raise OmnigentError(
            f"parent session {parent_id!r} has no agent_id; cannot "
            "create a codex-native sub-agent child",
            code=ErrorCode.INVALID_INPUT,
        )
    existing = await asyncio.to_thread(
        _find_codex_native_subagent_child, conversation_store, parent_id, thread_id
    )
    labels = _codex_subagent_labels_from_body(thread_id, body)
    if existing is not None:
        await asyncio.to_thread(conversation_store.set_labels, existing.id, labels)
        return existing.id
    return await _create_and_publish_codex_child(
        parent_id, parent_conv, thread_id, labels, conversation_store
    )


async def _persist_external_conversation_item(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    created_by: str | None = None,
) -> str:
    """
    Persist and broadcast a conversation item produced outside AP.

    This is the transcript bridge path for native Claude. It appends
    user messages, assistant messages, tool calls, and tool results
    without starting or steering the placeholder Omnigent agent.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for title seeding.
    :param body: External item event body.
    :param conversation_store: Store used to append the item.
    :param created_by: Authenticated identity of the actor whose
        request triggered the forwarder POST, e.g.
        ``"alice@example.com"``. Used to attribute user messages typed
        directly in the native terminal (no pending-input entry exists
        for those). ``None`` in single-user / unauthenticated mode —
        no label is stamped in that case.
    :returns: Store-assigned conversation item id.
    """
    item = _parse_external_conversation_item(body)
    # A native user message round-tripping back from the transcript:
    # drain its optimistic pending-input entry (FIFO) and fold the
    # entry's file blocks (image / file) into the item BEFORE persisting.
    # The transcript is text-only, so without this the image is dropped
    # from durable history and disappears on every reload / navigation.
    cleared_pending_id: str | None = None
    if (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "user"
        and not item.data.is_meta
    ):
        drained = pending_inputs.resolve_oldest(session_id)
        if drained is not None:
            cleared_pending_id = drained.pending_id
            item = _merge_pending_file_blocks(item, drained.content)
            # Apply the original sender's identity recorded at POST time.
            # The transcript forwarder is the single writer here and has no
            # auth context, so the persisted item would otherwise have
            # created_by=None, causing session.input.consumed to broadcast
            # without an author — the label would flash in from the optimistic
            # bubble then disappear once the committed item arrived.
            if drained.created_by is not None and item.created_by is None:
                item = item.model_copy(update={"created_by": drained.created_by})
        elif item.created_by is None and created_by is not None:
            # No pending entry — direct terminal input. Fall back to the
            # identity authenticated on the forwarder's own request.
            item = item.model_copy(update={"created_by": created_by})
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    await _seed_missing_title_from_user_message(conv, item, conversation_store)
    persisted = persisted_items[0]
    _publish_external_conversation_item(
        session_id, persisted, cleared_pending_id=cleared_pending_id
    )
    _drive_terminal_resolved_elicitation(session_id, persisted)
    return persisted.id


def _merge_pending_file_blocks(
    item: NewConversationItem,
    pending_content: list[dict[str, Any]],
) -> NewConversationItem:
    """
    Prepend a pending entry's file blocks onto a user-message item.

    The claude-native transcript mirrors a user message back as
    text-only — ``input_image`` / ``input_file`` blocks are dropped. The
    optimistic pending-input entry still carries them (with real
    ``file_id``s, assigned at upload), so we fold them into the durable
    item here. Without it the image renders only on the optimistic
    bubble and vanishes from history on the next reload.

    No-op when the pending entry has no file blocks, or when the item
    already carries file blocks (defensive — a future transcript that
    does include them must not be doubled).

    :param item: The parsed user-message item about to be persisted.
        Its ``data`` is a :class:`MessageData` whose ``content`` is a
        list of block dicts, e.g. ``[{"type": "input_text",
        "text": "hi"}]``.
    :param pending_content: The drained pending entry's content blocks,
        e.g. ``[{"type": "input_image", "file_id": "file_x",
        "filename": "a.png"}, {"type": "input_text", "text": "hi"}]``.
    :returns: A copy of *item* with the file blocks prepended, or *item*
        unchanged when there is nothing to merge.
    """
    if not isinstance(item.data, MessageData):
        return item
    file_blocks = [
        block
        for block in pending_content
        if isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
    ]
    if not file_blocks:
        return item
    already_has_files = any(
        isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
        for block in item.data.content
    )
    if already_has_files:
        return item
    merged_data = item.data.model_copy(update={"content": [*file_blocks, *item.data.content]})
    return item.model_copy(update={"data": merged_data})


def _message_text(content: list[dict[str, Any]]) -> str | None:
    """
    Extract joined text from message content blocks.

    :param content: Message content blocks, e.g.
        ``[{"type": "output_text", "text": "Done"}]``.
    :returns: Joined text from ``text`` / ``input_text`` fields,
        or ``None`` when no text field exists.
    """
    parts: list[str] = []
    found_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("input_text")
        if isinstance(text, str):
            found_text = True
            parts.append(text)
    return "\n".join(parts) if found_text else None


def _latest_assistant_text_from_store(
    conversation_store: ConversationStore,
    session_id: str,
) -> str | None:
    """
    Return the latest persisted assistant message text for a session.

    Native harnesses mirror completed transcript items to the AP
    server, not necessarily to the runner's in-memory history. This
    helper lets Omnigent forward the durable assistant output with the
    terminal-observed idle edge.

    :param conversation_store: Store used to read conversation items.
    :param session_id: Session/conversation id, e.g.
        ``"conv_child123"``.
    :returns: Latest assistant text, or ``None`` when none is
        persisted yet.
    """
    page = conversation_store.list_items(
        session_id,
        limit=_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT,
        order="desc",
        type="message",
    )
    for item in page.data:
        if not isinstance(item.data, MessageData):
            continue
        if item.data.role != "assistant" or item.data.is_meta:
            continue
        text = _message_text(item.data.content)
        if text is not None:
            return text
    return None


async def _enrich_idle_status_with_subagent_output(
    data: dict[str, Any],
    status: str,
    session_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Attach a native sub-agent's durable assistant text to an idle status edge.

    Shared by both native sub-agent delivery paths (the codex
    ``external_session_status`` POST handler and the claude-native relay
    forward) so the parent inbox result carries the child's output. Native
    harnesses mirror transcript items to the store, not runner memory, so the
    text is read here and forwarded with the idle edge.

    :param data: The ``external_session_status`` ``data`` to enrich, e.g.
        ``{"status": "idle"}``.
    :param status: Status edge; only ``"idle"`` is enriched.
    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conversation_store: Store read for the child's assistant text.
    :returns: ``data`` with ``"output"`` added when an idle edge has a
        persisted assistant message; otherwise unchanged.
    """
    if status != "idle":
        return data
    output = await asyncio.to_thread(
        _latest_assistant_text_from_store,
        conversation_store,
        session_id,
    )
    if output is None:
        return data
    return {**data, "output": output}


@dataclass(frozen=True)
class _RunnerForwardResult:
    """
    HTTP result from forwarding a session-control event to the runner.

    :param status_code: Runner response status, e.g. ``204``.
    :param body: Runner response body text. Empty string when the runner
        returns no body.
    """

    status_code: int
    body: str


def _require_external_status_forward(
    session_id: str,
    status: str,
    runner_result: _RunnerForwardResult | None,
) -> None:
    """
    Fail loudly when required external status forwarding does not land.

    Terminal native sub-agent completion is delivered to the parent
    runner through this forward. Dropping it would leave the parent
    waiting forever with no inbox result.

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param status: External status value, e.g. ``"idle"``.
    :param runner_result: HTTP result returned by the runner, or ``None``
        when no runner could be reached.
    :returns: None.
    :raises OmnigentError: If the runner was unavailable or
        rejected the forwarded status.
    """
    if runner_result is None:
        raise OmnigentError(
            f"Could not reach runner to deliver external_session_status "
            f"{status!r} for sub-agent session {session_id!r}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if runner_result.status_code >= 400:
        detail = runner_result.body[:500]
        suffix = f": {detail}" if detail else ""
        raise OmnigentError(
            f"Runner rejected external_session_status {status!r} for "
            f"sub-agent session {session_id!r} with status "
            f"{runner_result.status_code}{suffix}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _drive_terminal_resolved_elicitation(session_id: str, persisted: ConversationItem) -> None:
    """
    Feed a mirrored tool item into the terminal-resolved fast path.

    A ``function_call`` records its tool identity by ``call_id`` so the
    matching ``function_call_output`` can be correlated back to a parked
    permission prompt. A ``function_call_output`` means the gated tool
    already ran (or was rejected) in the native terminal, so the prompt
    the web UI may still be showing was resolved there — resolve the
    matching parked prompt now instead of waiting for the hook timeout.
    Other item types are ignored.

    :param session_id: Omnigent conversation id the item was mirrored for,
        e.g. ``"conv_abc123"``.
    :param persisted: The stored conversation item the forwarder just
        mirrored via ``external_conversation_item``.
    """
    data = persisted.data
    if persisted.type == "function_call" and isinstance(data, FunctionCallData):
        try:
            parsed = json.loads(data.arguments) if data.arguments else {}
        except json.JSONDecodeError:
            parsed = {}
        _recent_mirrored_tool_calls[data.call_id] = _MirroredToolCall(
            tool_name=data.name,
            tool_input=parsed if isinstance(parsed, dict) else {},
        )
    elif persisted.type == "function_call_output" and isinstance(data, FunctionCallOutputData):
        identity = _recent_mirrored_tool_calls.get(data.call_id)
        if identity is not None:
            _signal_terminal_resolved_harness_elicitation(
                session_id, identity.tool_name, identity.tool_input
            )


def _publish_status(
    session_id: str,
    status: str,
    error: ErrorDetail | None = None,
    response_id: str | None = None,
) -> None:
    """
    Publish a typed :class:`SessionStatusEvent` to the live stream and
    update the cache the list endpoint reads.

    ``status`` must be one of the literals on
    :class:`SessionStatusEvent` (``idle`` / ``running`` / ``waiting``
    / ``failed``); other values fail Pydantic validation rather than
    silently shipping a non-conforming wire shape (rule 15).

    Every publish site funnels through here so the in-memory
    ``_session_status_cache`` stays coherent with the SSE stream.
    Without this, paths that publish but don't write the cache —
    notably the ``external_session_status`` handler used by the
    claude-native forwarder — leave the sidebar stuck on "idle"
    while the chat itself shows "Working…".

    :param session_id: Session/conversation identifier.
    :param status: New session status value.
    :param error: Failure detail to forward on a ``"failed"``
        transition, e.g. ``ErrorDetail(code="runner_error",
        message="turn setup failed: ...")``. ``None`` for every
        non-failed transition. Carrying it lets clients render a
        terminal error line for SETUP-phase failures that never emit
        a ``response.failed`` event.
    :param response_id: Optional response id for terminal-backed status
        edges, e.g. ``"codex_turn_abc123"``.
    """
    # ``failed`` is sticky against a trailing ``idle``. A turn error is
    # terminal — it must not be silently downgraded to ``idle`` by a
    # follow-on quiescence signal. This matters for claude-native: the
    # turn-error edge comes from the ``StopFailure`` hook (→ ``failed``),
    # but the pane then goes quiet, so the PTY-activity watcher emits a
    # trailing ``idle`` ~1s later. Without this guard that ``idle`` would
    # erase the error state before the user could see it. The next
    # ``running`` edge (new activity) clears ``failed`` normally, so the
    # error persists exactly until the session does real work again. No
    # in-process flow performs a legitimate ``failed`` → ``idle``
    # transition (compaction failure publishes ``running`` → ``idle``, not
    # ``failed``), so this is a safe, harness-agnostic invariant.
    if status == "idle" and _session_status_cache.get(session_id) == "failed":
        return
    _session_status_cache[session_id] = status
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status=status,  # type: ignore[arg-type]
        response_id=response_id,
        error=error,
    )
    payload = event.model_dump()
    if response_id is None:
        payload.pop("response_id", None)
    session_stream.publish(session_id, payload)


def _publish_runner_recovered_status(session_id: str) -> None:
    """
    Clear a stale failed session status after runner recovery.

    Native terminal startup failures are sticky against trailing
    ``idle`` PTY-quiescence signals so users can see the error. A
    later runner bind/session-init success is different: it proves AP
    reached a live runner for this session again, so the old failure is
    stale and should not keep the conversation marked failed until the
    next user turn emits ``running``.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    if _session_status_cache.get(session_id) != "failed":
        return
    _session_status_cache[session_id] = "idle"
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status="idle",
        error=None,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_terminal_pending(session_id: str, pending: bool) -> None:
    """
    Publish a typed :class:`SessionTerminalPendingEvent` and update the
    cache the snapshot reads.

    Every relay site that changes the terminal-spin-up flag funnels
    through here so the in-memory ``_session_terminal_pending_cache``
    stays coherent with the SSE stream — a client connecting
    mid-spin-up seeds the spinner from the snapshot's
    ``terminal_pending`` field, while already-connected clients update
    live off this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` while the runner is auto-creating the
        terminal; ``False`` once it lands or auto-create fails.
    """
    # Store only ``True`` entries; delete on clear so the cache never
    # accumulates stale ``False`` entries for every terminal-first session
    # that has ever completed spin-up. The snapshot getter uses
    # ``.get(id, False)`` so absent == False.
    if pending:
        _session_terminal_pending_cache[session_id] = True
    else:
        _session_terminal_pending_cache.pop(session_id, None)
    event = SessionTerminalPendingEvent(
        type="session.terminal_pending",
        conversation_id=session_id,
        pending=pending,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_sandbox_status(session_id: str, stage: str, error: str | None = None) -> None:
    """
    Publish a typed :class:`SessionSandboxStatusEvent` and update the
    cache the snapshot reads.

    Every stage transition of a managed-sandbox launch funnels through
    here so the in-memory ``_session_sandbox_status_cache`` stays
    coherent with the SSE stream — a client opening the session
    mid-launch seeds its progress indicator from the snapshot's
    ``sandbox_status`` field, while already-connected clients update
    live off this event. Thread-safe (``session_stream.publish`` is a
    thread-safe broadcast and the cache write is a single dict
    assignment), so the launch pipeline may call this from the worker
    thread its sandbox exec steps run on.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param stage: The launch stage just entered, e.g.
        ``"provisioning"`` — one of
        :data:`omnigent.server.schemas.SandboxLaunchStage`.
    :param error: Failure detail when *stage* is ``"failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` for non-terminal stages.
    """
    # "ready" evicts: from then on the session looks like any
    # host-bound session and the snapshot carries no launch state.
    # Failures stay cached (mirroring ManagedLaunchTracker retention)
    # so a reload after a dead launch still shows the reason.
    if stage == "ready":
        _session_sandbox_status_cache.pop(session_id, None)
    else:
        _session_sandbox_status_cache[session_id] = SandboxStatus(stage=stage, error=error)
    event = SessionSandboxStatusEvent(
        type="session.sandbox_status",
        conversation_id=session_id,
        stage=stage,
        error=error,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_runner_skills(session_id: str) -> None:
    """
    Publish a typed :class:`SessionSkillsEvent` to the live stream.

    Fired the moment the background runner-skills fetch
    (:func:`_load_runner_skills`) populates the per-session cache, so a
    connected client can re-read the session snapshot and fill its
    slash-command menu instead of waiting for the next bind. Carries no
    payload beyond the conversation id — it is a "skills resolved,
    re-read the snapshot" nudge; the snapshot's cache-backed ``skills``
    field stays the source of truth.

    No-op when no client is subscribed (``session_stream`` has no
    buffer): a client binding later reads the now-warm snapshot directly.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    event = SessionSkillsEvent(
        type="session.skills",
        conversation_id=session_id,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_changed_files_invalidated(session_id: str, environment_id: str = "default") -> None:
    """
    Publish a coarse filesystem-change invalidation to the live stream.

    The event tells web clients to refetch visible filesystem views
    for the environment instead of polling the tree while a session is
    active. It is intentionally coarse because git-mode workspaces can
    only answer "the working tree changed" cheaply, not per-directory
    deltas.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param environment_id: Environment resource id,
        e.g. ``"default"``.
    """
    session_stream.publish(
        session_id,
        {
            "type": "session.changed_files.invalidated",
            "session_id": session_id,
            "environment_id": environment_id,
        },
    )


def _publish_interrupted(session_id: str, response_id: str | None = None) -> None:
    """
    Publish a ``session.interrupted`` event to the live stream.

    The event is co-emitted with ``response.incomplete`` (reason
    ``"user_interrupt"``) by the runtime cancel handler so off-the-
    shelf Responses parsers still close cleanly. This helper is
    responsible only for the session-level signal — not the
    response-level one.

    :param session_id: The session/conversation identifier whose
        stream should receive the event, e.g. ``"conv_abc123"``.
    :param response_id: Optional response id for terminal-backed
        interrupted turns, e.g. ``"codex_turn_abc123"``.
    """
    event = SessionInterruptedEvent(
        type="session.interrupted",
        data=SessionInterruptedPayload(
            requested_at=int(time.time()),
            response_id=response_id,
        ),
    )
    payload = event.model_dump()
    if response_id is None:
        data = payload.get("data")
        if isinstance(data, dict):
            data.pop("response_id", None)
    session_stream.publish(session_id, payload)


async def _get_runner_client(
    session_id: str,
    runner_router: RunnerRouter | None,
) -> httpx.AsyncClient | None:
    """
    Get an HTTP client for the runner bound to a session.

    Uses the ``RunnerRouter`` to resolve the pinned runner. Falls
    back to the in-process runner client for test setups.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or
        ``None`` for in-process setups.
    :returns: An ``httpx.AsyncClient`` pointed at the runner,
        or ``None`` if no runner is available.
    """
    from omnigent.runtime import get_runner_client

    if runner_router is not None:
        try:
            routed = runner_router.client_for_session_resources(
                session_id,
            )
            return routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s",
                session_id,
            )
            return None
    return cast("httpx.AsyncClient | None", get_runner_client())


async def _wait_for_runner_client(
    session_id: str,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    *,
    runner_id: str | None,
    timeout_s: float,
    runner_exit_reports: RunnerExitReports | None = None,
) -> httpx.AsyncClient | None:
    """
    Wait until a runner connects, then resolve the session's runner client.

    The tunnel registry owns the event-driven "runner connected" signal.
    After that signal fires, this helper intentionally resolves through
    :func:`_get_runner_client` instead of constructing a client directly
    from the registry session: the router re-checks the conversation's
    current ``runner_id`` binding and preserves the existing ownership /
    capability checks.

    When ``runner_exit_reports`` is supplied, the wait also ends the
    moment the daemon reports this runner died (``host.runner_exited``).
    That report is the authoritative "this runner is busted" signal — a
    crashed runner can never connect, so waiting out ``timeout_s`` would
    only delay the caller's failure handling. Returning ``None`` on the
    report (same as a timeout) lets the caller persist the failure the
    instant we are convinced, neither speculatively early nor a full
    timeout late.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or ``None`` for
        in-process test setups.
    :param tunnel_registry: The server's ``TunnelRegistry`` instance, or
        ``None`` in test setups without runner tunnels.
    :param runner_id: Runner id expected to connect, e.g.
        ``"runner_0123456789abcdef"``.
    :param timeout_s: Maximum seconds to wait, e.g. ``3.0``.
    :param runner_exit_reports: Crash-report store consulted to abort the
        wait early when this runner is reported dead. ``None`` keeps the
        plain wait-to-timeout behavior.
    :returns: A runner HTTP client if one becomes available, otherwise
        ``None`` (timed out, or the runner was reported dead).
    """
    if runner_id is None:
        return None
    if tunnel_registry is None:
        return await _get_runner_client(session_id, runner_router)
    if runner_exit_reports is None:
        session = await tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s)
        return None if session is None else await _get_runner_client(session_id, runner_router)
    # Race the event-driven connect signal against the crash-report poll;
    # whichever resolves first wins. A report means the runner is busted —
    # stop waiting and let the caller fail the turn now.
    connect_task = asyncio.ensure_future(
        tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s)
    )
    try:
        while not connect_task.done():
            if runner_exit_reports.get(runner_id) is not None:
                return None
            await asyncio.wait({connect_task}, timeout=_RUNNER_CONVICTION_POLL_S)
    finally:
        if not connect_task.done():
            connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect_task
    session = connect_task.result()
    return None if session is None else await _get_runner_client(session_id, runner_router)


async def _validate_session_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    request: Request,
) -> str:
    """
    Validate a session's workspace against the agent's os_env boundary.

    Wraps the seven-step validation in
    :mod:`omnigent.server.routes._workspace_validation` and
    raises :class:`OmnigentError` on failure so the route layer
    converts the error into a 400 response with a clear message.
    See ``designs/SESSION_WORKSPACE_SELECTION.md`` for the full
    semantic spec.

    The caller's host ownership is checked BEFORE the ``host.stat``
    round-trip the validation performs, so a non-owner never reaches
    another user's host (raises 403/404 via ``resolve_host_owner``).

    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``, or ``None`` when auth is disabled.
    :param host_id: Stable host id, e.g. ``"host_a1b2c3d4..."``.
    :param workspace: Absolute path supplied by the caller, e.g.
        ``"/Users/corey/universe/src/foo"``. ``None`` is rejected
        with the "workspace required when host_id is set" message.
    :param agent: The agent the session binds to. Used to load the
        bundle and read ``os_env.cwd`` for boundary computation.
    :param agent_cache: Cache for loading parsed agent specs from
        bundle storage. Required because session-create needs the
        spec; ``None`` is treated as a server config error.
    :param request: FastAPI request; ``request.app.state``
        carries the host registry and host store.
    :returns: The canonicalized workspace path that should be
        stored on the session row, e.g.
        ``"/Users/corey/universe/src/foo"`` (realpath; symlinks
        already resolved by the host).
    :raises OmnigentError: With ``ErrorCode.INVALID_INPUT`` on
        any validation failure (offline host, missing path,
        outside boundary, missing subdir). With
        ``ErrorCode.INTERNAL_ERROR`` if ``agent_cache`` is unset.
    """
    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

    if workspace is None:
        raise OmnigentError(
            "workspace required when host_id is set",
            code=ErrorCode.INVALID_INPUT,
        )
    if not workspace.startswith("/"):
        raise OmnigentError(
            "workspace must be an absolute path starting with /",
            code=ErrorCode.INVALID_INPUT,
        )
    if agent_cache is None:
        # Should never happen in production — the route factory
        # always wires an agent cache. Fail loud rather than
        # silently skipping validation, which would let bad
        # workspaces through.
        raise OmnigentError(
            "workspace validation requires an agent cache",
            code=ErrorCode.INTERNAL_ERROR,
        )

    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )

    # Authorize host ownership FIRST — before loading the agent spec or
    # the host.stat round-trip below. A non-owner must be rejected
    # (403/404 via the shared resolve_host_owner) before we touch the
    # host or even read the agent bundle (cross-user host probe). The
    # returned host also gives the display name for error messages.
    from omnigent.server.routes._host_launch import resolve_host_owner

    host_name: str | None = None
    host_store_inst = getattr(request.app.state, "host_store", None)
    if host_store_inst is not None:
        host = await asyncio.to_thread(
            resolve_host_owner,
            user_id=user_id,
            host_id=host_id,
            host_store=host_store_inst,
        )
        host_name = host.name

    # Read the agent's os_env.cwd — None when the spec has no
    # os_env block (headless agents). Headless agents have no
    # filesystem access at all but still get launched on hosts
    # for sessions that don't need it; treat their cwd as
    # relative-equivalent so the boundary is unrestricted.
    spec_cwd: str | None = None
    if agent.bundle_location is not None:
        try:
            loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
            )
            os_env = getattr(loaded.spec, "os_env", None)
            spec_cwd = getattr(os_env, "cwd", None) if os_env is not None else None
        except Exception as exc:
            _logger.exception("Failed to load agent spec for workspace validation")
            raise OmnigentError(
                f"failed to load agent spec: {exc}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc

    try:
        return await validate_workspace(
            host_registry=host_registry,
            host_id=host_id,
            workspace=workspace,
            spec_cwd=spec_cwd,
            host_name_for_errors=host_name,
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(
            exc.message,
            code=ErrorCode.INVALID_INPUT,
        ) from exc


@dataclass
class _HostLaunchAttempt:
    """
    Outcome of a relaunch ``host.launch_runner`` round-trip.

    :param runner_id: The token-bound runner id minted for this attempt,
        e.g. ``"runner_token_abc123..."``. Always set (the binding is
        rotated before the frame is sent), even when the host refused.
    :param error_code: Structured failure category from the host's result
        frame, e.g. ``"harness_not_configured"``; ``None`` on a successful
        launch, on a timeout waiting for the result, or when the host sent
        no code.
    :param error: Human-readable failure message from the host, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``; ``None`` when there was no error.
    """

    runner_id: str
    error_code: str | None = None
    error: str | None = None


async def _launch_runner_on_host(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_conn: HostConnection,
) -> _HostLaunchAttempt:
    """
    Ask a host to spawn a runner for a session and capture the result.

    Generates a new binding token, writes the runner_id to the session
    row, sends ``host.launch_runner`` (carrying the session's canonical
    harness so the host can refuse an unconfigured one), and waits up to
    :data:`_HOST_LAUNCH_RESULT_TIMEOUT_S` for the host's result frame.
    Does NOT wait for the runner to *connect* — the caller polls for that
    separately; this only captures the spawn/refuse verdict so a
    structured refusal (harness not configured) can be surfaced instead
    of silently timing out as ``RUNNER_UNAVAILABLE``.

    :param conv: The conversation that needs a runner.
    :param conversation_store: Store for updating ``runner_id``.
    :param host_registry: In-memory ``HostRegistry``.
    :param host_conn: The live ``HostConnection`` for the host.
    :returns: The :class:`_HostLaunchAttempt` — the new runner id plus any
        structured refusal from the host.
    """
    from omnigent.host.frames import HostLaunchRunnerFrame, encode_host_frame
    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    new_runner_id = token_bound_runner_id(binding_token)

    await asyncio.to_thread(
        conversation_store.replace_runner_id,
        conv.id,
        new_runner_id,
    )

    # Pull workspace from the session row — populated and validated
    # at session create per designs/SESSION_WORKSPACE_SELECTION.md.
    # The check constraint guarantees workspace is non-NULL when
    # host_id is set, so this assertion is a tripwire for any path
    # that bypassed the validation.
    if conv.workspace is None:  # pragma: no cover — constraint guards
        _logger.error(
            "session %s has host_id=%s but workspace is NULL — schema "
            "constraint should have prevented this",
            conv.id,
            conv.host_id,
        )
        return _HostLaunchAttempt(runner_id=new_runner_id)
    request_id = secrets.token_hex(8)
    launch_future: asyncio.Future[dict[str, str | None]] = (
        asyncio.get_running_loop().create_future()
    )
    host_conn.pending_launches[request_id] = launch_future
    launch_frame = encode_host_frame(
        HostLaunchRunnerFrame(
            request_id=request_id,
            binding_token=binding_token,
            workspace=conv.workspace,
            # Canonical harness (see _resolve_harness) so the host runs the
            # same configuration check it does at create-time launch. None
            # (agent not resolvable) skips the host-side check — fail open.
            harness=_resolve_harness(conv),
        )
    )
    try:
        host_registry.send_text(host_conn, launch_frame)
    except ConnectionError:
        host_conn.pending_launches.pop(request_id, None)
        _logger.warning(
            "Host %s connection lost while launching runner for %s",
            conv.host_id,
            conv.id,
        )
        return _HostLaunchAttempt(runner_id=new_runner_id)
    try:
        result = await asyncio.wait_for(
            launch_future,
            timeout=_HOST_LAUNCH_RESULT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        # No result yet — fall through to the caller's connect wait, which
        # preserves the prior fire-and-forget timing for a slow-but-fine host.
        host_conn.pending_launches.pop(request_id, None)
        return _HostLaunchAttempt(runner_id=new_runner_id)
    if result.get("status") == "failed":
        return _HostLaunchAttempt(
            runner_id=new_runner_id,
            error_code=result.get("error_code"),
            error=result.get("error"),
        )
    return _HostLaunchAttempt(runner_id=new_runner_id)


# Strong references to in-flight background managed-launch tasks.
# asyncio.create_task results are weakly held by the loop; without a
# reference here a long provision could be garbage-collected mid-flight.
# Cancelled at server shutdown via cancel_managed_launch_tasks().
_managed_launch_tasks: set[asyncio.Task[None]] = set()


async def cancel_managed_launch_tasks() -> None:
    """
    Cancel and await every in-flight background managed launch.

    Lifespan-teardown hook: without it, a slow provision outlives the
    ASGI shutdown and dies wherever the loop teardown happens to kill
    it. Cancellation is deterministic teardown of the TASK only — an
    already-provisioned sandbox is not terminated here (there is no
    time budget for provider calls during shutdown); its armed launch
    token expires with the provider lifetime cap that also reaps the
    sandbox.

    :returns: None once every task has settled (cancellations and any
        in-flight failures are absorbed via ``return_exceptions``).
    """
    tasks = list(_managed_launch_tasks)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _run_managed_launch(
    *,
    session_id: str,
    owner: str,
    sandbox_config: ManagedSandboxConfig,
    repo: RepoWorkspace | None,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    relaunch_host: Host | None = None,
) -> None:
    """
    Provision a managed sandbox for a session in the background.

    The ``host_type="managed"`` create returns before the sandbox
    exists; this task carries the rest of the pipeline: provision the
    sandbox + start the host (:func:`launch_managed_host`), bind the
    host + workspace to the session row, launch a runner on the host,
    and wait for that runner's tunnel so a message POST rendezvousing
    on *tracker* can forward immediately once the launch settles.

    The same pipeline serves a sandbox RELAUNCH (*relaunch_host* set):
    a message arriving for a session whose managed sandbox died kicks
    this task with the existing host row, and
    :func:`relaunch_managed_host` provisions a new sandbox generation
    under the same host identity instead of minting a new one.

    Every exit path settles the tracker entry — success via
    ``finish`` (the session then looks like any host-bound session),
    failure via ``fail`` with the reason a waiting message POST
    reports. A session deleted mid-provision is detected at the bind
    step and the fresh sandbox is torn down.

    Server shutdown cancels this task (the lifespan teardown calls
    :func:`cancel_managed_launch_tasks`); an already-provisioned
    sandbox then leaks until the provider's lifetime cap reaps it
    (the armed launch token expires with the same cap).

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on auth-disabled servers).
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Parsed repository-URL workspace to clone inside the
        sandbox, or ``None`` for an empty workspace.
    :param tracker: The app's :class:`ManagedLaunchTracker`; this
        session's entry was registered by the caller.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the
        launched runner's connection. ``None`` in minimal test
        wirings (the rendezvous then settles at frame-send).
    :param relaunch_host: Existing managed host row to relaunch a new
        sandbox generation for, or ``None`` for a first launch (a
        fresh host identity is minted).
    """
    managed = await _provision_managed_sandbox(
        session_id=session_id,
        owner=owner,
        sandbox_config=sandbox_config,
        repo=repo,
        tracker=tracker,
        host_store=host_store,
        relaunch_host=relaunch_host,
    )
    if managed is None:
        return
    await _bind_and_launch_managed_runner(
        session_id=session_id,
        managed=managed,
        sandbox_config=sandbox_config,
        tracker=tracker,
        conversation_store=conversation_store,
        host_store=host_store,
        host_registry=host_registry,
        tunnel_registry=tunnel_registry,
    )


async def _provision_managed_sandbox(
    *,
    session_id: str,
    owner: str,
    sandbox_config: ManagedSandboxConfig,
    repo: RepoWorkspace | None,
    tracker: ManagedLaunchTracker,
    host_store: HostStore,
    relaunch_host: Host | None,
) -> ManagedHostLaunch | None:
    """
    Run the provision phase of a background managed launch.

    Dispatches to :func:`relaunch_managed_host` (existing host row)
    or :func:`launch_managed_host` (fresh identity) and converts any
    failure into a settled tracker entry — the background task has no
    caller to raise to.

    :param session_id: Session/conversation identifier.
    :param owner: User the managed host acts for.
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Repository workspace to clone, or ``None``.
    :param tracker: The app's launch tracker (failed here on error).
    :param host_store: Persistent host registrations.
    :param relaunch_host: Existing host row for a relaunch, or
        ``None`` for a first launch.
    :returns: The launch result, or ``None`` when the launch failed
        (the tracker entry is already settled with the reason).
    """
    from omnigent.server.managed_hosts import launch_managed_host, relaunch_managed_host

    def _on_stage(stage: str) -> None:
        """
        Relay a launch-pipeline stage to the session's progress surface.

        Passed into the launch helpers, which may invoke it from the
        worker thread their sandbox exec steps run on —
        :func:`_publish_sandbox_status` is thread-safe.

        :param stage: The stage just entered, e.g. ``"cloning"``.
        """
        _publish_sandbox_status(session_id, stage)

    try:
        if relaunch_host is not None:
            return await relaunch_managed_host(
                config=sandbox_config,
                host=relaunch_host,
                host_store=host_store,
                repo=repo,
                on_stage=_on_stage,
            )
        return await launch_managed_host(
            config=sandbox_config,
            owner=owner,
            host_store=host_store,
            repo=repo,
            on_stage=_on_stage,
        )
    except HTTPException as exc:
        _logger.warning(
            "Managed sandbox launch failed for session %s: %s",
            session_id,
            exc.detail,
        )
        tracker.fail(session_id, str(exc.detail))
        _publish_sandbox_status(session_id, "failed", str(exc.detail))
        return None
    except Exception:
        # Broad on purpose: this is a fire-and-forget task — an
        # unexpected error must settle the tracker (or a waiting
        # message POST hangs until its timeout) and must not escape
        # as an unhandled-task traceback.
        _logger.exception(
            "Managed sandbox launch crashed for session %s",
            session_id,
        )
        tracker.fail(session_id, "internal error during managed sandbox launch")
        _publish_sandbox_status(
            session_id, "failed", "internal error during managed sandbox launch"
        )
        return None


async def _bind_and_launch_managed_runner(
    *,
    session_id: str,
    managed: ManagedHostLaunch,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
) -> None:
    """
    Bind a provisioned managed host to its session and launch a runner.

    The bind step doubles as the delete-race detector: a session
    deleted while its sandbox provisioned surfaces here as
    ``ConversationNotFoundError``, and the fresh sandbox is torn down
    (the delete route could not see the host binding yet). Settles
    the tracker on every path.

    :param session_id: Session/conversation identifier.
    :param managed: The provision result (host id + workspace).
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the
        launched runner's connection. ``None`` in minimal test
        wirings (the rendezvous then settles at frame-send).
    """
    from omnigent.server.managed_hosts import terminate_managed_host

    try:
        conv = await asyncio.to_thread(
            conversation_store.set_host_id,
            session_id,
            managed.host_id,
            managed.workspace,
        )
    except ConversationNotFoundError:
        # The session was deleted while its sandbox provisioned. The
        # delete route couldn't see the host binding yet, so tear the
        # fresh sandbox down here (deleting the host row also revokes
        # its launch token).
        _logger.info(
            "Session %s was deleted during managed provisioning; "
            "terminating fresh sandbox on host %s",
            session_id,
            managed.host_id,
        )
        host = await asyncio.to_thread(host_store.get_host, managed.host_id)
        if host is not None:
            await terminate_managed_host(host, host_store, sandbox_config)
        tracker.fail(session_id, "session was deleted while its sandbox was provisioning")
        _publish_sandbox_status(
            session_id, "failed", "session was deleted while its sandbox was provisioning"
        )
        return
    # Host bound; what remains is launching the runner and waiting
    # for its tunnel.
    _publish_sandbox_status(session_id, "connecting")
    runner_id: str | None = None
    if host_registry is not None:
        host_conn = host_registry.get(managed.host_id)
        if host_conn is not None:
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
            )
            if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                # The sandbox image should bake in the harness, but if the
                # host refuses, fail the launch loudly (mirroring the
                # delete-during-provisioning path) rather than waiting out
                # the connect timeout for a runner that will never appear.
                reason = launch_attempt.error or "harness not configured on the sandbox host"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
            runner_id = launch_attempt.runner_id
    if runner_id is not None and tunnel_registry is not None:
        # Wait for the runner tunnel before settling so a rendezvoused
        # message POST resolves its runner client on the first try. A
        # timeout still settles successfully — the host is bound, and
        # post_event's normal host-relaunch path owns dead runners.
        await tunnel_registry.wait_for_runner(
            runner_id,
            timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
        )
    tracker.finish(session_id)
    _publish_sandbox_status(session_id, "ready")


async def _await_settled_managed_launch(launch: ManagedLaunch) -> None:
    """
    Block until a managed launch settles, raising its failure.

    The rendezvous a message POST takes when it races a background
    managed launch (create-time provisioning or a dead-sandbox
    relaunch): resolve as soon as the launch settles, surface the
    recorded reason when it failed, and give up with a clear retry
    hint when the launch outlives the rendezvous budget.

    :param launch: The session's tracker entry.
    :raises OmnigentError: 503 when the launch failed or is still
        running at the timeout.
    """
    from omnigent.server.managed_hosts import MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S

    try:
        await asyncio.wait_for(
            launch.settled.wait(),
            timeout=MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise OmnigentError(
            "The session's managed sandbox is still provisioning; try again shortly",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from None
    if launch.error is not None:
        raise OmnigentError(
            f"The session's managed sandbox failed to launch: {launch.error}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


async def _maybe_relaunch_managed_sandbox(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> bool:
    """
    Relaunch a dead managed sandbox for a session, if it has one.

    Called from the message-dispatch relaunch path when the session's
    host tunnel is gone. For an external (laptop) host that is the end
    of the line, but a managed host's sandbox is RELAUNCHABLE: the
    host row is durable, so a new sandbox generation can be provisioned
    under the same host identity — "send a message to wake the
    sandbox", mirroring how a message relaunches a dead runner on a
    live host.

    Single-flighted through the app's :class:`ManagedLaunchTracker`:
    the first message kicks the background relaunch, concurrent and
    later messages rendezvous on the same entry (the check-then-begin
    below has no ``await`` between check and begin, so it is atomic on
    the event loop). A previously FAILED attempt's retained entry is
    replaced — every new message retries.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (``host_id`` set; caller guards).
    :param app_state: ``request.app.state`` — supplies the host store,
        sandbox config, tracker, and registries.
    :param conversation_store: Store holding the session row.
    :returns: ``True`` when a relaunch engaged and settled
        successfully (the session row is re-bound; re-resolve the
        runner client). ``False`` when the host is not a managed
        sandbox or managed hosts are not configured — the caller
        falls through to the normal unavailable handling.
    :raises OmnigentError: 503 when the relaunch failed or timed out.
    """
    host_store = getattr(app_state, "host_store", None)
    sandbox_config = getattr(app_state, "sandbox_config", None)
    tracker = getattr(app_state, "managed_launches", None)
    if host_store is None or sandbox_config is None or tracker is None:
        return False
    if conv.host_id is None:
        return False
    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    if host is None or host.sandbox_provider is None:
        return False
    if await asyncio.to_thread(host_store.is_online, conv.host_id):
        # The host row still reads live (status online with a fresh
        # heartbeat) — the missing tunnel is likely a transient blip
        # on THIS replica and the host will reconnect on its own
        # backoff. Replacing the sandbox now would destroy a healthy
        # workspace; let the message fail unavailable instead. A dead
        # sandbox goes stale within the host liveness TTL, after which
        # the next message lands here and relaunches.
        return False
    launch = tracker.get(session_id)
    if launch is None or launch.settled.is_set():
        _kick_managed_relaunch(
            session_id=session_id,
            conv=conv,
            host=host,
            sandbox_config=sandbox_config,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            app_state=app_state,
        )
        launch = tracker.get(session_id)
    if launch is not None:
        await _await_settled_managed_launch(launch)
    return True


def _kick_managed_relaunch(
    *,
    session_id: str,
    conv: Conversation,
    host: Host,
    sandbox_config: ManagedSandboxConfig,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Register and spawn the background relaunch for a dead sandbox.

    Recovers the session's create-time repository workspace from its
    label so the fresh generation re-clones it, registers the tracker
    entry, and schedules :func:`_run_managed_launch` with the existing
    host row.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (supplies the repo label).
    :param host: The dead managed host row to relaunch.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state`` — supplies the registries.
    """
    from omnigent.server.managed_hosts import MANAGED_REPO_LABEL_KEY, parse_repo_workspace

    # Re-clone the repository the session was created with so the
    # fresh generation's workspace matches the create-time state.
    # The label holds the raw create-time value, already validated
    # by the create's parse — a parse failure here means the label
    # was tampered with, and the relaunch proceeds with an empty
    # workspace rather than dying.
    repo = None
    raw_repo = conv.labels.get(MANAGED_REPO_LABEL_KEY)
    if raw_repo is not None:
        try:
            repo = parse_repo_workspace(raw_repo)
        except ValueError:
            _logger.warning(
                "Session %s has an unparseable %s label (%r); relaunching with an empty workspace",
                session_id,
                MANAGED_REPO_LABEL_KEY,
                raw_repo,
            )
    _logger.info(
        "Managed sandbox for session %s (host %s) is gone; relaunching a new generation",
        session_id,
        conv.host_id,
    )
    tracker.begin(session_id)
    # Seed the relaunch's progress indicator immediately — the user is
    # typically watching the session page when "wake the sandbox" runs.
    _publish_sandbox_status(session_id, "provisioning")
    relaunch_task = asyncio.create_task(
        _run_managed_launch(
            session_id=session_id,
            owner=host.owner,
            sandbox_config=sandbox_config,
            repo=repo,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            tunnel_registry=getattr(app_state, "tunnel_registry", None),
            relaunch_host=host,
        )
    )
    _managed_launch_tasks.add(relaunch_task)
    relaunch_task.add_done_callback(_managed_launch_tasks.discard)


# Matches the create / PATCH handshake timeout — POST /v1/sessions caches
# the spec and (for claude-native) launches the terminal pane + transcript
# forwarder synchronously, which stays well under 10s.
_RUNNER_SESSION_INIT_TIMEOUT_S = 10.0


async def _ensure_runner_session_initialized(
    session_id: str,
    conv: Conversation,
    runner_client: httpx.AsyncClient,
) -> None:
    """
    Drive — and wait for — the runner's session-init handshake.

    Posts ``POST /v1/sessions`` to a freshly (re)launched runner and
    awaits it, so the runner's ``create_session`` completes before the
    caller forwards a message. For a claude-native session that means
    the tmux terminal **and its transcript forwarder are watching**
    before the web message is injected into the TUI — the round-trip
    that promotes the optimistic bubble and streams the reply only
    happens if the forwarder is in place first.

    This closes the host-restart race: today the auto-relaunch /
    resume paths wait only for the runner's *tunnel* to register
    (``runner_client`` becomes non-None), not for the session
    handshake, so the message can be injected before the forwarder
    attaches and is lost. The new / runner-bound paths don't hit this
    because they run the handshake as a distinct step before any
    message (``create_session`` endpoint) or against a from-offset-0
    forwarder.

    The runner's ``create_session`` is idempotent (it skips terminal
    auto-create under a per-session lock when one already exists), so
    this is safe even though ``_on_runner_connect`` (server/app.py)
    also posts ``/v1/sessions`` on the same connection — whichever
    lands first creates the terminal; the other no-ops.

    Best-effort and matching the create / PATCH handshakes: a transport
    error is logged and swallowed (the relay + ``_on_runner_connect``
    are the backstop), but the *await* — the actual fix — still
    serializes the handshake ahead of the caller's message forward.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*; supplies
        ``agent_id`` and ``sub_agent_name`` for the handshake body.
    :param runner_client: Runner client already resolved for
        *session_id* (its tunnel is up).
    :returns: None.
    """
    try:
        resp = await runner_client.post(
            "/v1/sessions",
            json={
                "session_id": session_id,
                "agent_id": conv.agent_id,
                "sub_agent_name": conv.sub_agent_name,
            },
            timeout=_RUNNER_SESSION_INIT_TIMEOUT_S,
        )
        # httpx only raises on transport errors; a 4xx/5xx means create_session
        # likely didn't run (terminal + forwarder not set up), so surface it
        # via the same warning path rather than silently forwarding into a
        # half-initialized runner.
        resp.raise_for_status()
        _publish_runner_recovered_status(session_id)
    except (httpx.HTTPError, ConnectionError):
        _logger.warning(
            "Session-init handshake to runner failed for session %s; "
            "forwarding the message anyway",
            session_id,
            exc_info=True,
        )


async def _get_runner_client_for_resource_access(
    session_id: str,
) -> httpx.AsyncClient | None:
    """Return the authoritative runner client for session resources.

    Requires the session to be bound to a runner via
    ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise. If no
    runner router is configured (unit-test/in-process setups), callers
    may fall back to local registries.
    """
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_router = get_runner_router()
    if runner_router is not None:
        routed_runner = runner_router.client_for_session_resources(session_id)
        return routed_runner.client
    return cast("httpx.AsyncClient | None", get_runner_client())


async def _proxy_get_session_resources_to_runner(
    runner_client: httpx.AsyncClient,
    session_id: str,
    resource_type: str | None = None,
) -> SessionResourcePaginatedList:
    """Proxy ``GET /resources`` to the runner with strict validation.

    :param runner_client: HTTP client bound to the session's runner.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_type: Optional ``?type=`` filter forwarded to the
        runner, e.g. ``"environment"``. ``None`` returns all types.
    :returns: The runner's validated resource page.
    :raises HTTPException: 502 on runner failure or malformed response.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/resources",
            # Runner-side list_session_resources applies the type filter.
            params={"type": resource_type} if resource_type else None,
            timeout=10.0,
        )
        if resp.status_code != 200:
            _logger.warning(
                "session resources: runner returned %d for session=%s",
                resp.status_code,
                session_id,
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint failed",
            )

        try:
            body = resp.json()
            if not isinstance(body, dict):
                raise TypeError("response body must be an object")
            page = SessionResourceListPage.model_validate(body)
        except (TypeError, ValueError, ValidationError) as exc:
            _logger.warning(
                "session resources: malformed runner response for session=%s: %s",
                session_id,
                exc,
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint returned malformed response",
            ) from exc

        return SessionResourcePaginatedList(
            data=page.data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        _logger.warning(
            "session resources: runner call failed for session=%s (%s)",
            session_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail="runner session-resources endpoint unavailable",
        ) from exc


async def _reset_runner_resources_after_switch(session_id: str) -> None:
    """Best-effort reset of the session's runner-side state after a switch.

    Run as a fire-and-forget background task by the switch-agent route. Calls
    the runner's dedicated ``POST /v1/sessions/{id}/reset-state`` endpoint,
    which closes the cached primary OSEnv + terminals AND drops the
    spec-derived session caches. Two reasons:

    1. **Sandbox correctness.** The primary OSEnv (which backs the web-UI
       filesystem / shell endpoints) is materialized once per session from the
       *original* agent's spec and cached. Closing it AND invalidating the
       spec/snapshot caches forces the next access to re-resolve and
       re-materialize from the NEW agent's spec, so those endpoints run
       under the switched-to agent's ``os_env``/sandbox — not the old one.
       (Agent ``sys_os_*`` tool calls already re-derive os_env per call, and
       native terminals re-evaluate the sandbox gate on respawn; this closes
       the remaining stale path.)
    2. **Terminal rebuild.** A lingering native terminal would otherwise shadow
       the switch-back transcript rebuild (auto-create skips while one exists).

    A dedicated endpoint (rather than ``DELETE /resources``) keeps the
    session-deletion contract untouched — deletion never needs the
    switch-specific cache reset.

    A switch only runs while the session is idle, so closing the env + terminal
    here is safe — unlike doing it inside the next turn's dispatch, which wedges
    that turn. cwd is re-derived from the runner's bound workspace, so the
    working directory / git worktree is preserved (only the sandbox changes;
    a ``fork``/``start_in_scratch`` agent gets a fresh scratch copy). The
    claude-native auto-create gate remains the switch-back safety net if this
    call is lost (runner offline, races).

    :param session_id: Session/conversation id just switched, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        runner_client = await _get_runner_client_for_resource_access(session_id)
        if runner_client is None:
            return
        reset_resp = await runner_client.post(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/reset-state",
            timeout=15.0,
        )
        # httpx only raises on transport errors — a 4xx/5xx reset response
        # still returns. A non-2xx means the runner did NOT close the old
        # env, so it must take the failure path below (suppressing the
        # invalidation publish); HTTPStatusError is an httpx.HTTPError.
        reset_resp.raise_for_status()
    except (httpx.HTTPError, HTTPException, OmnigentError, RuntimeError):
        # Best-effort: a runner hiccup must not break the (already-committed)
        # switch. OmnigentError covers the session-not-runner-bound / runner-
        # offline case raised by _get_runner_client_for_resource_access. The
        # auto-create gate rebuilds on switch-back regardless. No
        # changed-files event on this path either: the runner's env cache is
        # still the OLD agent's, so a triggered refetch would re-serve it —
        # and a lost runner rebuilds from the new spec on relaunch anyway.
        _logger.warning(
            "post-switch runner-resource reset failed for session=%s", session_id, exc_info=True
        )
        return
    # The old agent's cached OSEnv is now closed, so a refetch triggered by
    # this event re-materializes filesystem state from the NEW agent's spec.
    # This is what flips the web Files tab when the switch crosses an
    # os_env boundary (none→some shows it, some→none hides it) — the
    # session.agent_changed event fires before the reset and so cannot
    # carry a trustworthy availability signal.
    _publish_changed_files_invalidated(session_id)


def _is_native_terminal_session(conv: Conversation) -> bool:
    """
    Return whether a session is owned by a terminal-native wrapper.

    :param conv: Conversation row for the target session.
    :returns: ``True`` for wrappers whose transcript forwarder is the
        single writer for conversation history.
    """
    return conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY) in {
        _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
        _CODEX_NATIVE_WRAPPER_LABEL_VALUE,
    }


def _native_terminal_runtime(conv: Conversation) -> tuple[str, str, str]:
    """
    Return native terminal runtime strings for a wrapper session.

    :param conv: Conversation row for the target session.
    :returns: ``(display_name, model, harness)``.
    :raises OmnigentError: If the wrapper label is unsupported.
    """
    wrapper = conv.labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    if wrapper == _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE:
        return "Claude", _CLAUDE_NATIVE_MODEL, _CLAUDE_NATIVE_HARNESS
    if wrapper == _CODEX_NATIVE_WRAPPER_LABEL_VALUE:
        return "Codex", _CODEX_NATIVE_MODEL, _CODEX_NATIVE_HARNESS
    raise OmnigentError(
        "Unsupported native terminal session",
        code=ErrorCode.INVALID_INPUT,
    )


def _native_terminal_name_for_harness(harness: str) -> str:
    """
    Return the runner terminal resource name for a native harness.

    :param harness: Native harness identifier, e.g. ``"codex-native"``.
    :returns: Terminal resource name, e.g. ``"codex"``.
    :raises OmnigentError: If *harness* is not a supported native
        terminal harness.
    """
    if harness == _CLAUDE_NATIVE_HARNESS:
        return "claude"
    if harness == _CODEX_NATIVE_HARNESS:
        return "codex"
    raise OmnigentError(
        "Unsupported native terminal session",
        code=ErrorCode.INVALID_INPUT,
    )


def _native_terminal_failure_from_runner_response(
    resp: httpx.Response,
    *,
    display_name: str,
) -> ErrorData:
    """
    Convert a failed runner terminal-ensure response into durable error data.

    The runner's terminal ensure endpoint must return structured
    ``{"error": {"code": ..., "message": ...}}`` for definitive startup
    failures (for example a missing native CLI). Preserve that message
    exactly so the transcript shows the real cause. If the runner returns
    an opaque framework 500 body such as ``"Internal Server Error"``,
    surface an explicit malformed-runner-response error instead of
    inventing a native terminal cause.

    :param resp: Non-2xx response from
        ``POST /v1/sessions/{id}/resources/terminals``.
    :param display_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: Error data suitable for a persisted ``type="error"``
        conversation item.
    """
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw_error = body.get("error")
        if isinstance(raw_error, dict):
            raw_code = raw_error.get("code")
            raw_message = raw_error.get("message")
            if (
                isinstance(raw_code, str)
                and raw_code.strip()
                and isinstance(raw_message, str)
                and raw_message.strip()
            ):
                return ErrorData(
                    source="execution",
                    code=raw_code,
                    message=raw_message,
                )
    return ErrorData(
        source="execution",
        code=_NATIVE_TERMINAL_ENSURE_FAILED_CODE,
        message=(
            f"Native {display_name} terminal ensure failed with malformed "
            f"runner response (HTTP {resp.status_code})."
        ),
    )


def _native_terminal_ensure_transport_error(
    exc: httpx.HTTPError | ConnectionError,
    *,
    display_name: str,
) -> ErrorData:
    """
    Convert runner transport failure during native terminal ensure.

    The message path has exactly one preflight path for native terminal
    readiness. If that path cannot reach the runner, fail the user turn
    explicitly instead of falling back to the old forward-and-wait path.

    :param exc: Transport exception from the ensure request, e.g.
        ``httpx.ConnectError("connection refused")`` or the bare
        ``ConnectionError("tunnel closed before request completed")``
        that ``WSTunnelTransport`` raises on tunnel close.
    :param display_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: Error data suitable for a persisted ``type="error"``
        conversation item.
    """
    detail = str(exc).strip()
    message = f"Native {display_name} terminal ensure request failed."
    if detail:
        message = f"{message} {detail}"
    return ErrorData(
        source="execution",
        code=_NATIVE_TERMINAL_ENSURE_FAILED_CODE,
        message=message,
    )


@dataclass
class _NativeTerminalEnsureOutcome:
    """
    Result of a native terminal readiness probe.

    :param error: Error data when the runner definitively failed to
        create the terminal (fails the turn with a durable banner), or
        ``None`` when the terminal is ready / the failure was not
        definitive.
    :param policy_notice: Human-readable reason that tool-call policy
        enforcement is NOT active for this session (fail-open — codex too
        old or the hook could not be trusted), or ``None`` when
        enforcement is active. Non-fatal: surfaced once as a durable
        banner, never blocks the turn.
    """

    error: ErrorData | None
    policy_notice: str | None


async def _ensure_native_terminal_ready(
    runner_client: httpx.AsyncClient,
    session_id: str,
    conv: Conversation,
) -> _NativeTerminalEnsureOutcome:
    """
    Ask the runner to create or return the native terminal for a message.

    The runner's explicit ``ensure_native_terminal`` endpoint is the
    authoritative readiness check for native user messages. Any non-2xx
    response or transport failure fails this user turn quickly with a
    durable error item; a 2xx response preserves the normal boot grace
    because the runner has accepted responsibility for terminal startup.
    A 2xx response may also carry ``policy_hook_disabled_reason`` — a
    one-shot, non-fatal notice that policy enforcement is inactive — which
    is returned as ``policy_notice`` for the caller to surface as a banner.

    :param runner_client: HTTP client pointed at the session's runner.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row used to identify the native harness.
    :returns: The probe outcome — a definitive ``error`` (terminal could
        not start) and/or a non-fatal ``policy_notice``.
    """
    display_name, _, harness = _native_terminal_runtime(conv)
    terminal_name = _native_terminal_name_for_harness(harness)
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close
        # ("tunnel closed before request completed"); without this clause
        # a runner tunnel drop escaped to the catch-all handler and the
        # web client showed an opaque 500 ``internal_error`` instead of
        # the durable ensure-failure turn error below.
        _logger.warning(
            "%s terminal ensure transport failed for session=%s",
            display_name,
            session_id,
            exc_info=True,
        )
        return _NativeTerminalEnsureOutcome(
            error=_native_terminal_ensure_transport_error(exc, display_name=display_name),
            policy_notice=None,
        )
    if resp.status_code < 400:
        return _NativeTerminalEnsureOutcome(
            error=None,
            policy_notice=_policy_notice_from_ensure_response(resp),
        )
    _logger.warning(
        "%s terminal ensure failed definitively for session=%s status=%s body=%s",
        display_name,
        session_id,
        resp.status_code,
        resp.text[:500],
    )
    return _NativeTerminalEnsureOutcome(
        error=_native_terminal_failure_from_runner_response(resp, display_name=display_name),
        policy_notice=None,
    )


def _policy_notice_from_ensure_response(resp: httpx.Response) -> str | None:
    """
    Extract a non-fatal policy-disabled notice from a 2xx ensure response.

    The runner attaches ``policy_hook_disabled_reason`` (once) to its
    terminal-ensure success body when the session degraded to no policy
    enforcement. A malformed / non-JSON body is treated as "no notice"
    rather than failing the (successful) readiness probe.

    :param resp: The runner's 2xx ensure response.
    :returns: The reason string, or ``None`` when absent / unparseable.
    """
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    reason = body.get("policy_hook_disabled_reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def _publish_error_event(session_id: str, error: ErrorData) -> None:
    """
    Publish a live ``response.error`` event for a persisted error item.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param error: Durable error payload to mirror into SSE.
    :returns: None.
    """
    event = ErrorEvent(
        type="response.error",
        source=error.source,
        error={"code": error.code, "message": error.message},
    )
    session_stream.publish(session_id, event.model_dump())


async def _persist_native_terminal_failure(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    error: ErrorData,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
) -> str:
    """
    Persist a consumed user message and terminal-start error.

    Used when a native terminal definitively cannot start. The AP
    server becomes the writer for this failure turn only: it records
    the user's message so the input is consumed, records a sibling
    ``type="error"`` item so refresh/reconnect can render the banner,
    and publishes the same live error/status events clients already
    understand.

    When the failing session is a native sub-agent, the parent's runner
    is also notified via an ``external_session_status: failed`` forward
    (see :func:`_forward_native_subagent_terminal_failure`). The native
    bypass returns HTTP 200 to the parent's runner ``spawn`` call, so
    without this forward the parent's work entry would stay ``running``
    forever — no harness boots, so no Stop hook ever fires the terminal
    edge the normal completion path relies on.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param error: Error data derived from the runner's ensure response.
    :param runner_router: Router used to resolve the (sub-agent's own)
        runner for the parent-wake forward, or ``None`` in
        in-process / test setups where the global client is used.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :returns: Store-assigned id of the consumed user message item.
    """
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(
        conv,
        user_item,
        conversation_store,
    )
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(
            type="error",
            response_id=turn_id,
            data=error,
        ),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(
        session_id,
        "failed",
        ErrorDetail(code=error.code, message=error.message),
    )
    # A boot failure on a native sub-agent must wake the parent — mirror
    # the normal terminal-status path (publish + forward), gated on
    # ``kind == "sub_agent"`` so top-level native sessions are unaffected.
    await _forward_native_subagent_terminal_failure(
        session_id,
        conv,
        error,
        runner_router,
    )
    return consumed.id


async def _persist_host_launch_failure_turn(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    host_error: str | None,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
) -> str:
    """
    Persist a consumed user message and a host-launch failure error.

    Used when a message arrives for a host-bound session whose runner is
    dead and the host *refuses* to relaunch because the agent's harness
    isn't configured there (the daemon's structured
    ``harness_not_configured`` reply). The message is the real
    runner-start attempt, so — exactly like a native terminal that can't
    boot (:func:`_persist_native_terminal_failure`) — the server records
    the user's message (so the input is consumed, not silently dropped)
    and a sibling ``type="error"`` item carrying the host's message
    (which names the fix, ``omnigent setup``), then publishes the same
    live error/status events the web renders as an error banner. The host
    binding is left intact so a later message relaunches once the user has
    run setup.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param host_error: The host's human-readable refusal, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``. ``None`` falls back to a generic
        ``omnigent setup`` pointer so the banner is never empty.
    :param runner_router: Router used to resolve a sub-agent's runner for
        the parent-wake forward, or ``None`` in in-process / test setups.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :returns: Store-assigned id of the consumed user message item.
    """
    error = ErrorData(
        source="execution",
        # Stable classifier mirroring the host's wire error code, so the
        # web can special-case the banner if it ever wants to.
        code="harness_not_configured",
        message=(
            host_error
            if host_error
            # Defensive fallback: the daemon always sends a message with
            # the code, but the banner must stay actionable if a
            # third-party host omits it.
            else (
                "the agent's harness is not configured on the selected host — run `omnigent setup`"
            )
        ),
    )
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(conv, user_item, conversation_store)
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(type="error", response_id=turn_id, data=error),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(session_id, "failed", ErrorDetail(code=error.code, message=error.message))
    # A host-launched sub-agent that can't configure must wake its parent,
    # the same way a boot failure does — no-ops for top-level sessions.
    await _forward_native_subagent_terminal_failure(session_id, conv, error, runner_router)
    return consumed.id


async def _forward_native_subagent_terminal_failure(
    session_id: str,
    conv: Conversation,
    error: ErrorData,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Wake the parent runner when a native sub-agent fails to boot its terminal.

    Mirrors the terminal-status path's parent-wake (the ``idle`` /
    ``failed`` branch of ``external_session_status`` in
    :func:`post_event`): forward an ``external_session_status: failed``
    edge — carrying the boot error as ``output`` so it lands in the
    parent's inbox — to the sub-agent's own runner, then require the
    forward to land. The runner's ``external_session_status`` handler
    maps ``failed`` to ``mark_subagent_work_terminal(status="failed")``,
    which marks the parent's work entry terminal and wakes the parent.

    No-ops for non-sub-agent sessions and for codex-internal sub-agents
    (tracked inside the same app-server thread tree, with no runner
    inbox entry to forward to — identical to the normal path's
    ``_is_codex_native_subagent`` exclusion).

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conv: Conversation row for the sub-agent session.
    :param error: Boot error to relay to the parent as the turn result.
    :param runner_router: Router used to resolve the sub-agent's runner,
        or ``None`` (then the global client is used).
    :returns: None.
    :raises OmnigentError: If the parent's runner could not be reached
        or rejected the forwarded failure status — dropping it would
        strand the parent waiting forever.
    """
    if conv.kind != "sub_agent" or _is_codex_native_subagent(conv):
        return
    forward_body: dict[str, Any] = {
        "type": _EXTERNAL_SESSION_STATUS_TYPE,
        # ``output`` is the parent-inbox result text on a failed edge
        # (runner: ``output or "...turn failed"``); pass the real error.
        "data": {"status": "failed", "output": error.message},
    }
    runner_result = await _forward_session_change_to_runner(
        session_id,
        runner_router,
        forward_body,
    )
    _require_external_status_forward(session_id, "failed", runner_result)


async def _persist_native_policy_notice(
    session_id: str,
    conversation_store: ConversationStore,
    reason: str,
) -> None:
    """
    Persist + publish a non-fatal "policy not enforced" banner.

    The runner reports (once, via the terminal-ensure success response)
    that a native codex session started but tool-call policy enforcement
    is inactive (fail-open: codex too old, or the policy hook could not be
    trusted). This records a durable ``type="error"`` banner so the web UI
    shows the degraded-security state across refresh/reconnect, and
    mirrors it as a live ``response.error`` event. Unlike
    :func:`_persist_native_terminal_failure` it does NOT consume the user
    message or mark the turn failed — the terminal is up and the message
    still forwards; this is an advisory notice only.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conversation_store: Store used for the durable append.
    :param reason: Human-readable cause from the runner, e.g. ``"Codex CLI
        0.128.0 is older than 0.129.0; upgrade codex to enforce tool-call
        policies."``.
    :returns: None.
    """
    error = ErrorData(
        source="execution",
        code=_NATIVE_POLICY_NOT_ENFORCED_CODE,
        message=f"Tool-call policy enforcement is not active for this session: {reason}",
    )
    persisted = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(
            type="error",
            response_id=generate_task_id(),
            data=error,
        ),
    )
    # Mirror to live clients only when newly persisted (the runner's
    # one-shot flag already prevents re-surfacing; this dedups a same-turn
    # retry against an already-recorded notice).
    if persisted == "persisted":
        _publish_error_event(session_id, error)


def _build_native_terminal_message_event(
    conv: Conversation,
    body: SessionEventInput,
) -> dict[str, Any]:
    """
    Build the runner event that delivers a web message to a native TUI.

    :param conv: Conversation row for the target session.
    :param body: Validated Sessions API message event, e.g.
        ``{"type": "message", "data": {"role": "user",
        "content": [{"type": "input_text", "text": "Hi"}]}}``.
    :returns: Harness ``MessageEvent`` body for the runner-local
        native terminal harness, including ``agent_id`` so the runner
        can resolve the harness spec on the first message.
    :raises OmnigentError: If the event is not a user message.
    """
    display_name, model, harness = _native_terminal_runtime(conv)
    data = parse_item_data(body.type, {"type": body.type, **body.data})
    if not isinstance(data, MessageData) or data.role != "user":
        raise OmnigentError(
            f"{display_name} terminal sessions accept only user message events",
            code=ErrorCode.INVALID_INPUT,
        )
    return {
        "type": "message",
        "role": "user",
        "content": data.content,
        "model": model,
        "harness": harness,
        # The runner resolves the harness from the agent spec keyed by
        # agent_id; the forwarded ``harness`` hint is ignored on the turn
        # path. Without agent_id, the first message of a freshly
        # host-spawned runner (arriving before POST /v1/sessions caches
        # the spec) falls back to the test-only "runner-test-default"
        # harness and is dropped. Match the non-native forward path,
        # which always includes it.
        "agent_id": conv.agent_id,
    }


async def _forward_native_terminal_message(
    runner_client: httpx.AsyncClient,
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
) -> None:
    """
    Forward one Omnigent web-chat message to the native terminal harness.

    The message is intentionally not persisted here. Claude Code
    and Codex record the accepted prompt in their terminal/app-server
    state, and their forwarders later post that terminal-originated
    item back through ``external_conversation_item``.

    :param runner_client: Runner client selected for ``session_id``.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*.
    :param body: Sessions API message event to inject.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references in ``input_image`` / ``input_file``
        content blocks.
    :param artifact_store: Optional binary content store for
        fetching file bytes during resolution.
    :returns: None.
    :raises HTTPException: 502 when the runner or harness rejects
        the injection request.
    """
    display_name, _, _ = _native_terminal_runtime(conv)
    event = _build_native_terminal_message_event(conv, body)
    _logger.info(
        "%s terminal message forward starting: session=%s block_types=%s",
        display_name,
        session_id,
        [block.get("type") for block in event.get("content", []) if isinstance(block, dict)]
        if isinstance(event.get("content"), list)
        else type(event.get("content")).__name__,
    )
    if (
        file_store is not None
        and artifact_store is not None
        and isinstance(event.get("content"), list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        try:
            event["content"] = _resolve_message_content(
                event["content"],
                file_store,
                artifact_store,
                session_id=session_id,
            )
        except (ValueError, KeyError):
            _logger.warning(
                "File reference resolution failed for native session=%s",
                session_id,
                exc_info=True,
            )
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S,
        )
        _logger.info(
            "%s terminal message runner response: session=%s status=%s body=%s",
            display_name,
            session_id,
            resp.status_code,
            resp.text[:500],
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close;
        # map it to the same 502 as an httpx transport failure so a
        # runner tunnel drop mid-forward doesn't escape as an opaque 500.
        _logger.warning(
            "%s terminal message forward failed for session=%s",
            display_name,
            session_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed",
        ) from exc
    if resp.status_code >= 400:
        _logger.warning(
            "%s terminal message forward rejected for session=%s status=%s body=%s",
            display_name,
            session_id,
            resp.status_code,
            resp.text,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed ({resp.status_code})",
        )
    failure = _extract_claude_native_runner_failure(resp)
    if failure is not None:
        _logger.warning(
            "%s terminal message forward failed in runner SSE for session=%s: %s",
            display_name,
            session_id,
            failure,
        )
        raise HTTPException(
            status_code=502,
            detail=f"{display_name} terminal message delivery failed: {failure}",
        )


def _extract_claude_native_runner_failure(resp: httpx.Response) -> str | None:
    """
    Return a harness failure message from a runner SSE response.

    Runner ``POST /v1/sessions/{id}/events`` returns HTTP 200 for a
    syntactically valid harness stream even when the harness emits
    ``response.failed``. Claude-native Omnigent forwarding must treat that
    as failed injection, otherwise the web UI would believe a message
    reached the terminal when ``tmux send-keys`` actually failed.

    :param resp: Completed runner response.
    :returns: Failure message, or ``None`` when no failure event is
        present.
    """
    content_type = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" not in content_type and "response.failed" not in text:
        return None
    for frame in text.split("\n\n"):
        data_lines = [
            line.removeprefix("data:").strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "response.failed":
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if isinstance(message, str) and message:
                return message
            return json.dumps(error, sort_keys=True)
        if isinstance(error, str) and error:
            return error
        return "runner reported response.failed"
    return None


async def _forward_session_change_to_runner(
    session_id: str,
    runner_router: Any,
    event: dict[str, Any],
) -> _RunnerForwardResult | None:
    """
    Best-effort POST a control event to the bound runner.

    Used for control inputs the runner dispatches by harness in its
    ``/v1/sessions/{id}/events`` handler — claude-native injects the
    corresponding slash command into the tmux pane; other harnesses
    return 204 no-op. Two kinds of caller use this:

    * PATCH-driven harness notifications (``effort_change``,
      ``model_change``) — claude-native injects the slash command,
      other harnesses re-read the persisted value at the next turn
      boundary, so they ignore the return value.
    * Explicit ``compact`` — the caller inspects the returned status
      to decide whether the runner handled the control (claude-native,
      200) or the Omnigent server must run its own in-process compaction
      (204 / no runner). See the ``compact`` branch in
      :func:`post_event`.

    Mirrors the interrupt-forward fallback chain: prefer the per-
    session router binding, fall back to the global runner client
    (in-process / test setups where the router hasn't bound the
    session). When neither resolves to a client, the POST is silently
    skipped — the persisted value on the Omnigent side is the authoritative
    fallback, picked up by the next spawn.

    Non-2xx runner responses (e.g. 503 when the tmux pane isn't
    advertised yet) are logged as warnings so the failure surfaces
    in the Omnigent log — otherwise the POST succeeds at the httpx layer
    and the status would be silently dropped.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :param event: The ``/events`` POST body, e.g.
        ``{"type": "effort_change", "effort": "high"}``,
        ``{"type": "model_change", "model": "claude-opus-4-7"}``, or
        ``{"type": "compact"}``.
    :returns: The runner's HTTP status/body, or ``None`` when no
        runner client could be resolved or the POST failed at the
        transport layer (in both cases the AP-side persisted value /
        operation is the authoritative fallback).
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return None
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=5.0,
        )
    except httpx.HTTPError:
        _logger.exception(
            "Session-change forward failed for session=%r type=%r",
            session_id,
            event.get("type"),
        )
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Session-change forward rejected for session=%s type=%r status=%s body=%s",
            session_id,
            event.get("type"),
            resp.status_code,
            resp.text,
        )
    return _RunnerForwardResult(status_code=resp.status_code, body=resp.text)


async def _stop_session_via_runner(
    session_id: str,
    runner_router: Any,
) -> bool:
    """
    Forward a ``stop_session`` request to the bound runner, surfacing
    failures to the caller instead of swallowing them.

    Unlike :func:`_forward_session_change_to_runner` (used for
    ``effort_change`` / ``model_change``, where a dropped forward is
    benign — the runner re-reads the persisted value at the next turn),
    a failed ``stop_session`` means the session is *still alive*. The
    web UI's "Stop session" action is destructive and treats a 2xx as
    success (it closes the confirmation dialog), so a swallowed failure
    would tell the user the session stopped when it did not. This
    helper therefore raises on a transport error or non-2xx runner
    response.

    Runner-client resolution mirrors the best-effort helper's fallback
    chain: prefer the per-session router binding, fall back to the
    global runner client (in-process / test setups). When neither
    resolves to a client there is no live runner bound — the session is
    not running on any runner, so the stop is a no-op success and this
    returns ``False`` without raising (the caller uses that to discard
    the turn fence it installed, since no runner means nothing else
    would ever lift it).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :returns: ``True`` if the stop was delivered to a runner (2xx),
        ``False`` if no runner client resolved (nothing forwarded).
    :raises OmnigentError: ``RUNNER_UNAVAILABLE`` (HTTP 503) if the
        runner could not be reached or reported a non-2xx — e.g. the
        claude-native tmux pane is wedged and ``kill_session`` failed.
        The web UI maps this to a visible "stop failed" state rather
        than closing the dialog as if the session stopped.
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return False
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _STOP_SESSION_TYPE},
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close.
        raise OmnigentError(
            f"Could not reach the runner to stop session {session_id!r}: {exc}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    if resp.status_code >= 400:
        raise OmnigentError(
            f"Runner failed to stop session {session_id!r} "
            f"(status {resp.status_code}): {resp.text}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return True


# How long to wait for the host to acknowledge a ``stop_runner`` before
# giving up. The claude pane is already dead by then (see
# :func:`_stop_session_host_runner`), so a slow/unreachable host only costs
# the "disconnected" UI transition, not session correctness — a short wait
# keeps the web UI's Stop action snappy.
_STOP_RUNNER_RESULT_TIMEOUT_S = 10.0


async def _stop_session_host_runner(
    session_id: str,
    host_id: str,
    runner_id: str,
    host_registry: Any,
) -> None:
    """
    Terminate the host-launched runner backing a host-spawned session.

    "Stop session" on a host-spawned session must end the dedicated runner
    subprocess the host launched for it — there is exactly one runner per
    host-launched session (see ``POST /v1/hosts/{host_id}/runners`` and the
    host-launch branch of session create). Killing the ``claude`` tmux pane
    via :func:`_stop_session_via_runner` is not enough on its own: the
    runner stays connected, so ``GET /health`` keeps reporting
    ``runner_online: true`` for the session and the web UI never shows it as
    disconnected — new messages are accepted and hang on "working" against a
    dead pane.

    Bringing the runner's tunnel down is what flips ``runner_online`` to
    ``false``; ``_on_runner_disconnect`` then marks the session and the web
    UI renders the "Agent disconnected — click to show reconnect command"
    banner, identical to the end state a CLI-launched session reaches when
    its process exits.

    Best-effort by design: the pane is already gone before this runs, so a
    host that is offline, was replaced, or is slow to acknowledge is logged
    and swallowed rather than failing the whole Stop. In the common case —
    the host's ``omnigent host`` tunnel is open while the user drives
    the web UI — the stop is delivered and the runner exits. The runner this
    targets is read from the caller's own (owner-gated) session row, so it
    can only ever stop the runner bound to that session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param host_id: Owning host identifier from the session row, e.g.
        ``"host_a1b2c3d4..."``.
    :param runner_id: Runner bound to the session, e.g.
        ``"runner_token_abc123..."``.
    :param host_registry: The :class:`HostRegistry` tracking live host
        tunnels on this replica, or ``None`` when host support is not wired
        (in-process / test setups without a host tunnel).
    :returns: None.
    """
    if host_registry is None:
        return
    conn = host_registry.get(host_id)
    if conn is None:
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s is offline; "
            "the runner may linger online and the session will not show as "
            "disconnected",
            runner_id,
            session_id,
            host_id,
        )
        return
    from omnigent.host.frames import HostStopRunnerFrame, encode_host_frame

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
    conn.pending_stops[request_id] = future
    stop_frame = encode_host_frame(
        HostStopRunnerFrame(request_id=request_id, runner_id=runner_id),
    )
    try:
        host_registry.send_text(conn, stop_frame)
    except ConnectionError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s connection was replaced",
            runner_id,
            session_id,
            host_id,
        )
        return
    try:
        result = await asyncio.wait_for(
            future,
            timeout=_STOP_RUNNER_RESULT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Host %s did not acknowledge stop of runner %s for session %s",
            host_id,
            runner_id,
            session_id,
        )
        return
    if result.get("status") == "failed":
        _logger.warning(
            "Host %s failed to stop runner %s for session %s: %s",
            host_id,
            runner_id,
            session_id,
            result.get("error"),
        )


def _build_new_item(
    body: SessionEventInput,
    response_id: str,
    created_by: str | None = None,
) -> NewConversationItem:
    """
    Construct a :class:`NewConversationItem` from a POSTed event.

    Validates the data payload via ``parse_item_data`` (the same
    validator the route boundary already invoked) and wraps the
    result with the response_id linkage required by the conversation
    store.

    :param body: Validated event input — guaranteed to be a known
        item type (the route checked ``_ALLOWED_EVENT_TYPES``).
    :param response_id: The task id the new item should be tagged
        with — either the steered active task or a freshly-created
        one.
    :param created_by: Authenticated identity of the actor posting
        the event, recorded for per-message attribution. ``None`` in
        single-user mode.
    :returns: A :class:`NewConversationItem` ready for delivery
        or persistence.
    """
    data = parse_item_data(body.type, {"type": body.type, **body.data})
    return NewConversationItem(
        type=body.type,
        response_id=response_id,
        data=data,
        created_by=created_by,
    )


def _parse_skill_slash_command(body: SessionEventInput) -> tuple[str, str]:
    """
    Validate and unpack a structured skill slash-command event.

    The REPL posts ``type="slash_command"`` for skill invocations.
    Other command kinds are surfaced by terminal transcript bridges
    through ``external_conversation_item`` and are not executable
    session inputs on this route.

    :param body: Validated event input with ``type="slash_command"``
        and data such as ``{"kind": "skill", "name": "grill-me",
        "arguments": "review this plan"}``.
    :returns: ``(skill_name, arguments)`` with whitespace-trimmed
        command name and raw argument text.
    :raises OmnigentError: If the payload is not a skill command
        or is missing a usable skill name / arguments string.
    """
    kind = body.data.get("kind", "skill")
    if kind != "skill":
        raise OmnigentError(
            "slash_command events only support kind='skill'; use the "
            "dedicated control event for built-in commands",
            code=ErrorCode.INVALID_INPUT,
        )
    name = body.data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise OmnigentError(
            "slash_command requires non-empty data.name",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments = body.data.get("arguments", "")
    if not isinstance(arguments, str):
        raise OmnigentError(
            "slash_command data.arguments must be a string",
            code=ErrorCode.INVALID_INPUT,
        )
    return name.strip(), arguments


def _build_skill_slash_command_policy_body(body: SessionEventInput) -> SessionEventInput:
    """
    Build the user-message shape used for input policy evaluation.

    Skill commands inject a hidden meta message containing the full
    skill body, but input guardrails should evaluate the text the user
    actually typed, not the skill instructions maintained by the
    server. This preserves the legacy policy surface of
    ``/<skill> <arguments>`` without making bundled skill content
    policy-sensitive.

    :param body: Validated ``slash_command`` event body with data such
        as ``{"name": "grill-me", "arguments": "review this plan"}``.
    :returns: Synthetic user ``message`` event for policy evaluation.
    :raises OmnigentError: If the slash-command payload is invalid.
    """
    skill_name, arguments = _parse_skill_slash_command(body)
    command_text = f"/{skill_name}" if not arguments else f"/{skill_name} {arguments}"
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": command_text}],
        },
    )


async def _resolve_skill_meta_text_via_runner(
    session_id: str,
    skill_name: str,
    arguments: str,
    runner_client: httpx.AsyncClient,
) -> str:
    """
    Resolve a skill's hidden ``<skill>`` meta text on the bound runner.

    Skill content is runner-owned: the runner reads the ``SKILL.md``
    body and resource files from the skill's directory on its own
    filesystem, so the embedded ``<path>`` and resource listing are
    valid where the harness executes. Wraps
    ``POST /v1/sessions/{id}/skills/resolve``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param skill_name: Exact skill name to resolve, e.g.
        ``"code-review"``.
    :param arguments: Raw argument string typed after the slash
        command, e.g. ``"review this plan"``. Empty when none.
    :param runner_client: HTTP client pointed at the bound runner.
    :returns: The hidden ``<skill>`` meta text for a single
        ``input_text`` block.
    :raises OmnigentError: If the skill is not exposed for the session
        (the runner 404s with the available list), or the runner is
        unreachable / errors while resolving.
    """
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/skills/resolve",
            json={"name": skill_name, "arguments": arguments},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise OmnigentError(
            f"Runner unreachable while resolving skill {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code not in (200, 404):
        raise OmnigentError(
            f"Runner failed to resolve skill {skill_name!r}: HTTP {resp.status_code}",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Parse the body once, guarded: a transport proxy / HTML error page /
    # non-object body must surface as a controlled runner failure, not an
    # uncaught 500.
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
    except ValueError as exc:
        raise OmnigentError(
            f"Runner returned a malformed skill resolution for {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code == 404:
        available = payload.get("available", [])
        raise OmnigentError(
            f"Skill {skill_name!r} not found. Available skills: {available}",
            code=ErrorCode.INVALID_INPUT,
        )
    meta_text = payload.get("meta_text")
    if not isinstance(meta_text, str):
        raise OmnigentError(
            f"Runner returned malformed skill resolution for {skill_name!r}: missing 'meta_text'",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return meta_text


async def _dispatch_skill_slash_command_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent: Agent,
    has_mcp_servers: bool,
    created_by: str | None,
) -> str:
    """
    Persist a skill slash command and forward hidden skill context.

    Skill content is runner-owned: this asks the bound runner to
    resolve the skill (``POST /v1/sessions/{id}/skills/resolve``) into
    its ``<skill>`` meta text, reading the ``SKILL.md`` body and
    resource files from the skill's directory *on the runner* — so the
    embedded ``<path>`` and resource listing are valid where the harness
    executes. The server then persists the result (runner-resolves,
    server-persists). Appends two conversation items with the same
    response id:

    * a visible ``slash_command`` item for the UI transcript;
    * a hidden ``message`` item with ``is_meta=True`` containing the
      full skill instructions for runner history replay.

    Only the hidden message is sent to the runner as input. The visible
    command is published as ``response.output_item.done`` after the
    runner accepts the event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id``.
    :param body: Structured ``slash_command`` event body.
    :param conversation_store: Store used to append both durable
        items.
    :param runner_client: HTTP client pointed at the bound runner.
    :param agent: Agent bound to the conversation.
    :param has_mcp_servers: ``True`` when the agent spec declares MCP
        servers; forwarded unchanged to the runner event.
    :param created_by: Authenticated actor id, e.g.
        ``"alice@example.com"``, or ``None`` in single-user mode.
    :returns: The persisted visible ``slash_command`` item id.
    :raises OmnigentError: If the skill is not exposed for the
        session, or the runner is unreachable while resolving it.
    """
    import uuid

    skill_name, arguments = _parse_skill_slash_command(body)
    meta_text = await _resolve_skill_meta_text_via_runner(
        session_id,
        skill_name,
        arguments,
        runner_client,
    )

    response_id = f"turn_{uuid.uuid4().hex}"
    meta_content = [{"type": "input_text", "text": meta_text}]
    visible_item = NewConversationItem(
        type=_SLASH_COMMAND_TYPE,
        response_id=response_id,
        data=SlashCommandData(
            agent=agent.name,
            kind="skill",
            name=skill_name,
            arguments=arguments,
        ),
        created_by=created_by,
    )
    meta_item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="user",
            content=meta_content,
            is_meta=True,
        ),
        created_by=created_by,
    )
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [visible_item, meta_item],
    )
    visible = persisted_items[0]

    # Mirror the plain-message path's title seeding: a session whose FIRST
    # message is a skill invocation (web landing composer, REPL) would
    # otherwise keep a NULL title and the sidebar falls back to the
    # conversation id. Titled from the typed command ("/debate kafka…"),
    # NOT the hidden meta item — that's the full SKILL.md instruction blob.
    command_text = f"/{skill_name} {arguments}" if arguments else f"/{skill_name}"
    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": command_text}],
        conversation_store,
    )

    runner_body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "content": meta_content,
        "agent_id": conv.agent_id,
        "model": agent.name,
        "has_mcp_servers": has_mcp_servers,
        # The forwarded message carries ``meta_content`` — i.e. the
        # META item (persisted_items[1]), not the user-visible item.
        # Hand the runner that id so a cold-cache reload drops the
        # right persisted copy (see _forward_event_to_runner).
        "persisted_item_id": persisted_items[1].id,
    }
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override

    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=10.0,
        )
        event = OutputItemDoneEvent(type="response.output_item.done", item=visible.to_api_dict())
        session_stream.publish(session_id, event.model_dump())
    except httpx.HTTPError:
        _logger.exception(
            "Forward of skill slash command failed for session=%s; "
            "items persisted, runner picks up on reconnect.",
            session_id,
        )
        _publish_status(session_id, "idle")
    return visible.id


def _title_content_from_item(item: NewConversationItem) -> list[dict[str, Any]]:
    """
    Extract title candidate content blocks from a session item.

    Only user ``message`` items contribute. Tool results and
    assistant-shaped messages return an empty list so callers leave
    the conversation title unchanged.

    :param item: The parsed item being persisted, e.g. a user
        ``"message"`` item with input text content.
    :returns: Content blocks that may contribute to a synthesized
        title, e.g. ``[{"type": "input_text", "text": "Hello"}]``.
    """
    if item.type != "message":
        return []
    if not isinstance(item.data, MessageData):
        return []
    if item.data.role != "user":
        return []
    return item.data.content


async def _seed_missing_title(
    conv: Conversation,
    content: list[dict[str, Any]],
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled conversation's title from message content blocks.

    No-op when the conversation already has a title or the blocks
    yield no usable text. Mutates ``conv.title`` in place on success
    so callers holding the row see the persisted value.

    :param conv: The conversation row for the session.
    :param content: Title-candidate blocks, e.g.
        ``[{"type": "input_text", "text": "/debate kafka vs sqs"}]``.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    if conv.title is not None:
        return
    title = synthesize_conversation_title(content)
    if title is None:
        return
    updated = await asyncio.to_thread(
        conversation_store.update_conversation,
        conv.id,
        title=title,
    )
    if updated is not None:
        conv.title = updated.title


async def _seed_missing_title_from_user_message(
    conv: Conversation,
    item: NewConversationItem,
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled session's title from a user message.

    The app UI creates sessions with ``initial_items=[]`` and posts
    the first user message through ``POST /v1/sessions/{id}/events``.
    This helper also covers callers that pass initial items to
    ``POST /v1/sessions``. Non-user-message items are ignored, and
    already-titled conversations are left unchanged.

    :param conv: The conversation row for the session.
    :param item: The parsed item being persisted.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    await _seed_missing_title(conv, _title_content_from_item(item), conversation_store)


async def _persist_session_event(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist a user event without forwarding to a runner.

    Used when the runner isn't online yet but the session has a
    ``host_id`` — the message is stored so the runner's crash-
    recovery block picks it up from history when it connects.

    :param session_id: Session/conversation identifier.
    :param body: The validated event input.
    :param conversation_store: Store for item persistence.
    :param agent_name: Agent name for title seeding.
    :returns: The store-assigned item id.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        session_id,
    )
    if conv is not None:
        await _seed_missing_title_from_user_message(
            conv,
            item,
            conversation_store,
        )
    item_id = persisted_items[0].id if persisted_items else turn_id
    _publish_external_conversation_item(session_id, persisted_items[0])
    return item_id


async def _forward_event_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    agent_name: str | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
) -> str:
    """
    Persist a user event and forward it to the runner.

    The server persists the item to the conversation store
    (invariant I1: persist-before-forward), publishes acknowledgment
    events, then POSTs the event to the runner's
    ``POST /v1/sessions/{id}/events``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The conversation row for ``session_id``.
    :param body: The validated event input from the client.
    :param conversation_store: Store for item persistence.
    :param runner_client: HTTP client pointed at the runner.
    :param agent_name: Human-readable agent name for the
        ``model`` field on the runner body, e.g. ``"research-agent"``.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary content store for
        resolving ``file_id`` references before forwarding.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint so ``proxy_stream`` knows to load
        the agent spec and initialise :class:`ProxyMcpManager` for
        this turn. ``False`` by default (agents without MCP servers).
    :param created_by: Authenticated identity of the posting actor,
        recorded on the persisted item for attribution.
    :returns: The store-assigned id of the persisted item.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    await _seed_missing_title_from_user_message(
        conv,
        item,
        conversation_store,
    )
    # Don't publish status="running" or input.consumed here —
    # wait until after the forward to the runner succeeds.
    # Publishing early causes the REPL to start its streaming
    # timer before the turn actually starts, showing a
    # premature "working" phase.

    # Resolve file_id references (input_image, input_file) to
    # inline base64 data: URIs before forwarding. The runner and
    # harness don't have access to the server's file store — the
    # LLM endpoint needs the actual content, not an internal ID.
    forwarded_data = dict(body.data)
    if (
        file_store is not None
        and artifact_store is not None
        and "content" in forwarded_data
        and isinstance(forwarded_data["content"], list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        try:
            forwarded_data["content"] = _resolve_message_content(
                forwarded_data["content"],
                file_store,
                artifact_store,
                session_id=session_id,
            )
        except (ValueError, KeyError):
            _logger.warning(
                "File reference resolution failed for session=%s",
                session_id,
                exc_info=True,
            )

    # Flatten SessionEventInput {type, data} into the runner's
    # discriminated-union shape {type, ...data_fields}. The runner's
    # POST handler expects the harness event shape, not the
    # session-API wrapper. Include agent_id so the runner can
    # resolve the harness type and spawn environment.
    runner_body: dict[str, Any] = {
        "type": body.type,
        **forwarded_data,
        "agent_id": conv.agent_id,
        # model tags the ResponseObject for REPL rendering.
        # Use the human-readable agent name when available.
        "model": agent_name or conv.agent_id or "",
        # Signal to proxy_stream that it should initialise
        # ProxyMcpManager and fetch MCP tool schemas for this turn.
        # Only included (and only True) when the agent has MCP
        # servers — False/absent saves the runner from a no-op spec
        # load on every turn for agents without MCP servers.
        "has_mcp_servers": has_mcp_servers,
        # Id of the item just persisted for this turn. On a cold runner
        # cache the runner reloads history (which includes this item in
        # PRE-resolution form) and drops it by id, appending its own
        # resolved copy — id-based dedup, not a role/content guess.
        "persisted_item_id": persisted_items[0].id,
    }
    # Forward request-supplied client-side tool schemas so non-native
    # harnesses can emit (and tunnel) the caller's tools — the runner
    # merges these into the harness tool list (_merge_request_client_tools).
    # Without this the runner only ever sees the spec's builtin/MCP tools
    # and the model can't invoke client-side Read/Write/Glob/etc.
    if body.tools:
        runner_body["tools"] = body.tools
    # Per-event override wins; fall back to the persisted column so a
    # UI / REPL PATCH applies even when the client doesn't repeat
    # model_override on every event. ``is not None`` over ``or`` per
    # the no-invented-defaults rule.
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override

    # The runner's sessions-native POST returns 202 immediately
    # and starts the turn as a background task. No streaming
    # response to drain — events flow through GET /stream.
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=10.0,
        )
        # Publish input.consumed AFTER the forward succeeds —
        # the runner has the message and will start the turn.
        _publish_input_consumed(session_id, persisted_items[0])
    except httpx.HTTPError:
        _logger.exception(
            "Forward to runner failed for session=%s; "
            "event persisted, runner picks up on reconnect.",
            session_id,
        )
        _publish_status(session_id, "idle")

    return persisted_items[0].id


@dataclass
class _SessionEventDispatchResult:
    """
    Outcome of forwarding one item-event to the runner.

    :param item_id: Store-assigned id of the AP-persisted item, e.g.
        ``"item_abc123"``. ``None`` for the claude-native message
        bypass, which persists nothing AP-side.
    :param pending_id: Id of the :mod:`omnigent.runtime.pending_inputs`
        entry recorded for a native-terminal web message, e.g.
        ``"pending_a1b2c3"`` — surfaced to the sender so it can adopt
        the id and dedupe against the snapshot. ``None`` for non-native
        events (already persisted, so no separate pending entry).
    """

    item_id: str | None
    pending_id: str | None


async def _dispatch_session_event_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent_name: str | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
    runner_router: RunnerRouter | None = None,
) -> _SessionEventDispatchResult:
    """
    Forward an item-event to the runner with harness-aware dispatch.

    Callers stay harness-agnostic — the claude-native message bypass
    is encapsulated here. Two dispatch outcomes:

    * **claude-native + ``type == "message"``**: web-chat user
      messages on these sessions must NOT be persisted by the AP
      server. The Omnigent would otherwise persist an AP-side copy AND
      let the transcript forwarder mirror the same message back
      (with its own store-assigned item id), so every web-typed
      prompt would land as two items in the chat panel. We forward
      to the bound runner so the claude-native harness types the
      message into tmux; the transcript forwarder becomes the
      single writer for the conversation history. Returns a result
      with ``item_id=None`` (no AP-side persisted item) and a
      ``pending_id`` for the optimistic-bubble index entry.

    * **All other cases**: persist the item AP-side (invariant I1:
      persist-before-forward) and forward via the harness's
      ``/events`` scaffold. Returns the persisted item id and
      ``pending_id=None``.

    The single-writer invariant is the entire reason the bypass
    exists; do NOT collapse the two branches into a single forward
    that always persists. Doing so on a native session causes
    duplicate items in the chat panel as soon as the transcript
    forwarder mirrors the same prompt back.

    The pending-input entry recorded on the native path bridges the
    transcript round-trip: until the forwarder mirrors the message
    back, it lives nowhere durable, so a client that navigates away /
    rebinds would lose the optimistic bubble. The entry is replayed
    into the snapshot and drained when the message persists (see
    :mod:`omnigent.runtime.pending_inputs`). It is rolled back if the
    forward fails, so a never-delivered message leaves no ghost.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row for *session_id*.
    :param body: Validated event from the client.
    :param conversation_store: Used by the non-native path to
        persist the item.
    :param runner_client: The session's runner client, already
        resolved by the caller via :func:`_get_runner_client`.
    :param agent_name: Human-readable agent name for the
        ``model`` field on non-native forwards.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary store for the same.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint. ``False`` by default.
    :param created_by: Authenticated identity of the posting actor,
        e.g. ``"alice@example.com"``. On the non-native path it is
        recorded directly on the persisted item. On the claude-native
        bypass the transcript forwarder is the single writer, so
        ``created_by`` is stored in the ``pending_inputs`` entry via
        :func:`omnigent.runtime.pending_inputs.record` and applied
        to the item when the forwarder mirrors it back (see
        :func:`_persist_external_conversation_item`).
    :param runner_router: Router used to resolve the runner for the
        native-terminal parent-wake forward when a sub-agent fails to
        boot (see :func:`_persist_native_terminal_failure`). ``None``
        in in-process / test setups where the global client is used.
    :returns: A :class:`_SessionEventDispatchResult` carrying the
        persisted item id (non-native) or the pending-input id
        (claude-native message bypass).
    """
    if body.type == "message" and _is_native_terminal_session(conv):
        # Validate before touching the runner. The ensure probe is only
        # for syntactically valid user messages; assistant/system-shaped
        # inputs should still fail locally without creating terminals.
        _build_native_terminal_message_event(conv, body)
        ensure_outcome = await _ensure_native_terminal_ready(
            runner_client,
            session_id,
            conv,
        )
        if ensure_outcome.error is not None:
            item_id = await _persist_native_terminal_failure(
                session_id,
                conv,
                body,
                conversation_store,
                ensure_outcome.error,
                runner_router,
                created_by=created_by,
            )
            return _SessionEventDispatchResult(item_id=item_id, pending_id=None)
        if ensure_outcome.policy_notice is not None:
            # Terminal is up but policy enforcement is off (fail-open). Post
            # a durable, non-fatal banner; the user message still forwards.
            await _persist_native_policy_notice(
                session_id,
                conversation_store,
                ensure_outcome.policy_notice,
            )
        # Record the optimistic bubble before forwarding so it's known
        # server-side immediately (replayed into the snapshot). Roll it
        # back on any failure/cancellation so a message the TUI never
        # received doesn't replay as a ghost.
        content = body.data.get("content")
        pending_id: str | None = (
            pending_inputs.record(session_id, content, created_by=created_by)
            if isinstance(content, list) and content
            else None
        )
        forwarded = False
        try:
            await _forward_native_terminal_message(
                runner_client,
                session_id,
                conv,
                body,
                file_store=file_store,
                artifact_store=artifact_store,
            )
            forwarded = True
        finally:
            if not forwarded and pending_id is not None:
                pending_inputs.resolve(session_id, pending_id)
        return _SessionEventDispatchResult(item_id=None, pending_id=pending_id)
    item_id = await _forward_event_to_runner(
        session_id,
        conv,
        body,
        conversation_store,
        runner_client,
        agent_name=agent_name,
        file_store=file_store,
        artifact_store=artifact_store,
        has_mcp_servers=has_mcp_servers,
        created_by=created_by,
    )
    return _SessionEventDispatchResult(item_id=item_id, pending_id=None)


def _extract_persistent_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Extract a persistable conversation item from a runner SSE event.

    Returns a ``NewConversationItem`` for:

    - ``response.output_item.done`` events carrying an assistant
      message, function_call, or function_call_output.
    - ``compaction`` events carrying a conversation summary from
      the runner's compaction system.

    Returns ``None`` for all other events (transient deltas, turn
    lifecycle, compaction progress indicators, etc.).

    :param event: Parsed SSE event dict from the runner stream.
    :param response_id: Turn-scoped id from the most recent
        ``response.in_progress`` event. All items persisted within
        the same turn share this id so the web UI can group them
        into a single bubble and pair function_calls with their
        outputs. Falls back to a fresh uuid when unavailable.
    :returns: A ``NewConversationItem`` ready for
        ``conv_store.append()``, or ``None``.
    """
    import uuid

    evt_type = event.get("type")

    if evt_type == "compaction":
        try:
            data = parse_item_data("compaction", event)
        except (ValueError, TypeError):
            _logger.warning("Failed to parse compaction item from SSE")
            return None

        return NewConversationItem(
            type="compaction",
            response_id=f"compact_{uuid.uuid4().hex}",
            data=data,
        )

    if evt_type != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in ("message", "function_call", "function_call_output"):
        return None
    # Skip transient observed function_call events (status
    # ``in_progress`` / ``action_required``).  Only ``completed``
    # function_calls are durable — the scaffold emits them after
    # the dispatch Future resolves.  Persisting interim statuses
    # creates orphan conversation items whose spinners never
    # resolve in the web UI.
    if item_type == "function_call" and item.get("status") != "completed":
        return None
    try:
        data = parse_item_data(item_type, item)
    except (ValueError, TypeError):
        _logger.warning(
            "Failed to parse persistent item from SSE: %s",
            item_type,
        )
        return None

    return NewConversationItem(
        type=item_type,
        response_id=response_id or f"turn_{uuid.uuid4().hex}",
        data=data,
    )


def _resource_event_item_from_sse(
    session_id: str,
    event: dict[str, Any],
) -> NewConversationItem | None:
    """
    Build a ``resource_event`` conversation item from a runner SSE event.

    The runner emits ``session.resource.created`` /
    ``session.resource.deleted`` when an agent tool
    (``sys_terminal_launch`` / ``sys_terminal_close``) materializes or
    tears down a session resource mid-turn. The relay republishes the
    raw event onto the live ``session_stream`` (so connected clients
    update instantly); this helper produces the durable conversation
    item so a client that reconnects mid-turn rediscovers the resource
    in the snapshot — matching the REST resource path
    (:func:`_publish_and_persist_resource_event`).

    Returns ``None`` for every other event type, and for malformed
    resource events (missing id / type) so a bad frame can't poison
    the relay.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param event: Parsed SSE event dict from the runner stream.
    :returns: A ``resource_event`` :class:`NewConversationItem`, or
        ``None``.
    """
    from omnigent.entities.conversation import ResourceEventData

    evt_type = event.get("type")
    if evt_type == "session.resource.created":
        resource = event.get("resource")
        if not isinstance(resource, dict):
            return None
        resource_id = resource.get("id")
        resource_type = resource.get("type")
    elif evt_type == "session.resource.deleted":
        resource = None
        resource_id = event.get("resource_id")
        resource_type = event.get("resource_type")
    else:
        return None

    # Require non-empty id/type. ``isinstance(x, str)`` alone admits
    # ``""``, which would persist a malformed resource_event item the
    # snapshot can't resolve back to a real resource. Drop the frame
    # instead — the snapshot endpoint stays the source of truth.
    if not resource_id or not isinstance(resource_id, str):
        return None
    if not resource_type or not isinstance(resource_type, str):
        return None

    return NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=evt_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )


def _error_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Build a durable ``error`` item from a runner error SSE event.

    The web UI already renders live ``response.error`` and
    ``response.failed`` error payloads as real error banners. This
    helper mirrors turn-scoped payloads into conversation history so the
    banner survives refresh/reconnect.

    A bare ``response.error`` emitted before ``response.in_progress`` is
    a session/startup signal, not a transcript turn. Leaving it live-only
    avoids creating an orphan banner at the top of the transcript; when
    a user sends a message into the failed native terminal, the AP-side
    fast-fail path records that user item and its sibling error in order.

    :param event: Parsed runner SSE event.
    :param response_id: Current response id, e.g. ``"resp_abc123"``.
        ``None`` means no turn is active.
    :returns: A ``type="error"`` item, or ``None`` when the event has
        no structured error payload or is not tied to a turn.
    """
    evt_type = event.get("type")
    raw_error: Any
    source = event.get("source")
    if evt_type == "response.error":
        if response_id is None:
            return None
        raw_error = event.get("error")
    elif evt_type == "response.failed":
        raw_response = event.get("response")
        raw_error = raw_response.get("error") if isinstance(raw_response, dict) else None
        if raw_error is None:
            raw_error = event.get("error")
        source = "execution"
        if response_id is None and isinstance(raw_response, dict):
            raw_response_id = raw_response.get("id")
            if isinstance(raw_response_id, str) and raw_response_id:
                response_id = raw_response_id
    else:
        return None
    if response_id is None:
        return None
    if not isinstance(raw_error, dict):
        return None
    raw_code = raw_error.get("code")
    raw_message = raw_error.get("message")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return None
    if not isinstance(raw_message, str) or not raw_message.strip():
        return None
    if source not in ("llm", "execution", "tool"):
        return None
    return NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(
            source=source,
            code=raw_code,
            message=raw_message,
        ),
    )


async def _relay_persist_error_once(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> Literal["persisted", "duplicate", "skipped", "failed"]:
    """
    Persist a runner error item unless the same error already exists.

    Native terminal startup can fail again on every runner reconnect.
    Dedupe by the visible payload ``(source, code, message)`` only
    when no user message has appeared since the matching error. That
    suppresses reconnect spam while still recording a new error for a
    user-initiated retry against the same broken terminal.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param item: The candidate ``type="error"`` item.
    :returns: ``"persisted"`` if this call appended the item,
        ``"duplicate"`` if a matching recent error already exists,
        ``"skipped"`` if no store or non-error item was provided, or
        ``"failed"`` if the store operation failed.
    """
    if conversation_store is None:
        return "skipped"
    if not isinstance(item.data, ErrorData):
        return "skipped"
    try:
        recent = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=20,
            order="desc",
        )
        for existing in recent.data:
            if (
                existing.type == "message"
                and isinstance(existing.data, MessageData)
                and existing.data.role == "user"
            ):
                break
            if existing.type != "error" or not isinstance(existing.data, ErrorData):
                continue
            if (
                existing.data.source == item.data.source
                and existing.data.code == item.data.code
                and existing.data.message == item.data.message
            ):
                return "duplicate"
        await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
        return "persisted"
    except Exception:
        _logger.exception(
            "Relay error persist failed for session=%s",
            session_id,
        )
        return "failed"


async def _relay_persist(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> None:
    """
    Persist a single conversation item from the relay.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier.
    :param item: The item to persist.
    """
    if conversation_store is None:
        return
    try:
        await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
    except Exception:
        _logger.exception(
            "Relay persist failed for session=%s",
            session_id,
        )


async def _flush_relay_text(
    conversation_store: ConversationStore | None,
    session_id: str,
    text_acc: list[str],
    response_id: str | None,
    model_id: str | None,
) -> None:
    """
    Persist buffered assistant text as a message item and clear the buffer.

    Scaffold harnesses (claude-sdk) stream text deltas with no per-message
    ``output_item.done``, so the relay buffers them. Flushing at each
    text→function_call boundary (not only at ``response.completed``) keeps
    the persisted transcript interleaved — ``[text, tool, text, tool]`` —
    instead of collapsing a turn's narration into one block after its tool
    calls (which renders tools-above-text + run-on text on reload).

    After a confirmed persist the item is also published to the live
    stream as ``response.output_item.done`` (mirroring the native path's
    :func:`_publish_external_conversation_item`). Live clients already
    rendered the text from the deltas; the publish delivers the
    store-assigned item id so they can stamp it onto the streamed block.
    Without it the rendered block stays id-less and every reconnect's
    itemId-keyed reconciliation splices the persisted copy in as a
    duplicate. Clients dedupe the event itself by response id / content
    equality (web ``blockStream.ts`` ``message_done``; the TUI's
    ``_plan_output_item_render`` skips re-rendering when deltas streamed).

    The buffer and the in-flight replay are cleared ONLY after the append
    is confirmed: clearing first would let a reconnect during the persist
    ``await`` see neither the (not-yet-committed) message nor the replay,
    dropping the narration — and a swallowed append failure would lose it
    permanently. On failure the buffers are left intact so the text still
    replays and is retried at the next flush / ``response.completed``.

    :param conversation_store: Store to append to, or ``None`` to skip
        persistence (test parsing path).
    :param session_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param text_acc: Accumulated delta strings; cleared in place on success.
    :param response_id: Turn id so the segment groups with its tool calls.
    :param model_id: Assistant agent label for the message.
    """
    if not text_acc:
        return
    text = "".join(text_acc)
    if not text.strip():
        # Whitespace-only: nothing worth persisting. Drop it so it neither
        # accumulates into the next segment nor replays as an empty bubble.
        text_acc.clear()
        inflight_text.reset_text(session_id)
        return
    if conversation_store is None:
        text_acc.clear()
        return
    import uuid

    try:
        item = NewConversationItem(
            type="message",
            response_id=response_id or f"turn_{uuid.uuid4().hex}",
            data=parse_item_data(
                "message",
                {
                    "type": "message",
                    "role": "assistant",
                    "agent": model_id or "unknown",
                    "content": [{"type": "output_text", "text": text}],
                },
            ),
        )
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
    except Exception:
        # Keep text_acc + the in-flight buffer so the narration isn't lost:
        # it still replays on reconnect and is retried at the next flush.
        _logger.exception(
            "Relay: failed to persist assistant text segment for session=%s",
            session_id,
        )
        return
    # Confirmed persisted — now safe to clear. Synchronous (no await before
    # the next yield), so no reconnect observes the committed message and a
    # stale replay together.
    text_acc.clear()
    inflight_text.reset_text(session_id)
    # Publish the persisted item so live clients learn its store-assigned
    # id and stamp it onto the already-rendered streamed text (see the
    # docstring). Ordered before the boundary item / terminal event the
    # caller publishes next, so the client's text section is still open.
    done_event = OutputItemDoneEvent(
        type="response.output_item.done",
        item=persisted[0].to_api_dict(),
    )
    session_stream.publish(session_id, done_event.model_dump())


async def _relay_runner_stream(
    session_id: str,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    ready: asyncio.Event | None = None,
) -> None:
    """
    Subscribe to the runner's SSE stream and relay events locally.

    Long-lived background task that opens
    ``GET /v1/sessions/{id}/stream`` on the runner and publishes
    each event to the local ``session_stream`` pub-sub. Also
    updates ``_session_status_cache`` from turn lifecycle events
    and persists conversation items (assistant messages, tool
    calls) to the conversation store as they arrive.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_client: HTTP client pointed at the runner.
    :param conversation_store: Store for persisting conversation
        items extracted from the runner's SSE stream.
    :param ready: Optional event set once the runner stream emits its
        ready heartbeat, proving AP's runner-side no-replay subscriber
        slot is registered. ``None`` is accepted for direct unit tests
        that exercise relay parsing/persistence without asserting on
        startup readiness.
    """
    from omnigent.runtime import session_stream

    text_acc: list[str] = []
    current_response_id: str | None = None
    # Model/agent label from the turn header, stamped on text segments
    # flushed at tool-call boundaries (the boundary event carries no model).
    current_model: str | None = None
    # Map tool call_id → response_id so a function_call_output that
    # arrives after a new response.in_progress (different response_id)
    # still pairs with its matching function_call. Without this, the
    # web UI's block stream clears its pending-tool state on the
    # response_id transition and the tool card spinner never resolves.
    tool_call_response_ids: dict[str, str] = {}
    _logger.info("Relay: connecting to runner GET /stream for session=%s", session_id)

    # Read timeout: 3x the runner's session-stream heartbeat interval
    # (15s). Between turns the runner emits ``session.heartbeat`` every
    # 15s to keep proxies from dropping the idle connection. If 3
    # consecutive heartbeats are missed (45s), the connection is likely
    # dead — let the relay exit so ``_ensure_runner_relay`` can restart
    # it on the next ``POST /events``. ``connect`` stays at httpx's
    # default (5s); ``write``/``pool`` are not rate-limiting here.
    _relay_timeout = httpx.Timeout(connect=5.0, read=45.0, write=None, pool=None)
    try:
        async with runner_client.stream(
            "GET",
            f"/v1/sessions/{session_id}/stream",
            timeout=_relay_timeout,
        ) as resp:
            _logger.info("Relay: connected to runner GET /stream for session=%s", session_id)
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    data_line = next(
                        (ln for ln in frame.splitlines() if ln.startswith("data:")),
                        None,
                    )
                    if data_line is None:
                        continue
                    payload = data_line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    # The runner emits session.status events
                    # directly.
                    # Re-publish via _publish_status so the event
                    # gets the conversation_id field required by
                    # SessionStatusEvent's schema. The cache write
                    # happens inside _publish_status itself.
                    # Runner-emitted keepalive — consumed to reset the
                    # read timeout; not forwarded to the session stream
                    # (the Omnigent subscriber generates its own heartbeats).
                    if evt_type == "session.heartbeat":
                        if ready is not None:
                            ready.set()
                        continue

                    # Stopped turn: drop its trailing response.* output (no
                    # forward, no persist) but keep text_acc — the pre-stop
                    # narration the user watched persists at the terminal flush.
                    if session_id in _interrupt_fenced_sessions:
                        if evt_type == "session.status" and event.get("status") == "running":
                            _interrupt_fenced_sessions.discard(session_id)
                        elif evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                            # Terminal proves the stopped turn is over (completed =
                            # the stop lost the race); process it normally.
                            _interrupt_fenced_sessions.discard(session_id)
                        elif (
                            evt_type.startswith("response.")
                            and evt_type not in _FENCE_EXEMPT_EVENT_TYPES
                        ):
                            continue

                    if evt_type == "session.status":
                        status = event.get("status", "")
                        if status:
                            # Forward the runner's failure detail on a
                            # ``failed`` transition so a SETUP-phase
                            # failure (which never emits response.failed)
                            # surfaces a real error message downstream
                            # instead of ending the turn silently.
                            raw_err = event.get("error")
                            status_error = (
                                ErrorDetail.model_validate(raw_err)
                                if isinstance(raw_err, dict)
                                else None
                            )
                            # PTY-activity status is a UI signal only. Terminal
                            # sub-agent delivery rides the Stop/StopFailure hook
                            # via external_session_status (the codex-shared path)
                            # — the PTY idle oscillates on mid-turn lulls and
                            # would deliver a premature, lock-out completion.
                            _publish_status(session_id, status, status_error)
                        if status == "running":
                            text_acc.clear()
                        continue

                    # Terminal spin-up status from the runner's auto-create
                    # path. Re-publish via _publish_terminal_pending so the
                    # event carries conversation_id and the cache write
                    # (read by the snapshot) stays coherent with the stream.
                    if evt_type == "session.terminal_pending":
                        # Use ``is True`` (not bool()) so a malformed frame
                        # with a string like ``"false"`` can't strand the
                        # spinner on — the runner always sends a real bool.
                        _publish_terminal_pending(
                            session_id,
                            event.get("pending") is True,
                        )
                        continue

                    # Track the turn's response_id from lifecycle
                    # events so persisted items share one id.
                    if evt_type == "response.in_progress":
                        resp_obj = event.get("response", {})
                        _rid = resp_obj.get("id")
                        if isinstance(_rid, str) and _rid:
                            current_response_id = _rid
                        _model = resp_obj.get("model")
                        if isinstance(_model, str) and _model:
                            current_model = _model

                    # Accumulate response-scoped (scaffold) text deltas for
                    # persistence. Native message-scoped deltas (with a
                    # message_id) persist via their own output_item.done(message),
                    # so buffering them here would double-persist. Guard on
                    # non-empty str (like inflight_text.record_publish) so a
                    # malformed delta can't break the later "".join(text_acc).
                    if evt_type == "response.output_text.delta" and not event.get("message_id"):
                        _delta = event.get("delta")
                        if isinstance(_delta, str) and _delta:
                            text_acc.append(_delta)

                    # Track tool call_id → response_id so a
                    # function_call_output that arrives under a later
                    # response still pairs with its call.  Done
                    # before _extract_persistent_item_from_sse because
                    # the parse may fail (serialization alias mismatch)
                    # while the mapping is still needed for the live
                    # event patch below.
                    _raw_item = event.get("item")
                    _item = _raw_item if isinstance(_raw_item, dict) else {}
                    _item_type = _item.get("type")
                    _item_call_id = _item.get("call_id")
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and isinstance(_item_call_id, str)
                        and current_response_id is not None
                    ):
                        tool_call_response_ids[_item_call_id] = current_response_id

                    # For function_call_output, use the response_id
                    # of the matching function_call so the web UI
                    # pairs them in the same bubble even when a new
                    # response.in_progress has already overwritten
                    # current_response_id.
                    if (
                        _item_type == "function_call_output"
                        and isinstance(_item_call_id, str)
                        and _item_call_id in tool_call_response_ids
                    ):
                        _persist_rid = tool_call_response_ids[_item_call_id]
                    else:
                        _persist_rid = current_response_id

                    # Flush buffered narration as its own message BEFORE the
                    # function_call it preceded, so the transcript interleaves
                    # [text, tool, text, tool] instead of pooling a turn's text
                    # after its tool calls (tools-above-text + run-on on reload).
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and text_acc
                    ):
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            current_model,
                        )

                    conv_item = _extract_persistent_item_from_sse(
                        event,
                        response_id=_persist_rid,
                    )
                    if conv_item is not None:
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            conv_item,
                        )

                    # On ANY terminal event (not just completed), persist the
                    # final text segment: narration streamed before a failure /
                    # cancel must survive reload too, ordered BEFORE the error
                    # item below and before the publish pops the in-flight
                    # replay entry (flush → publish keeps reload == live).
                    # NB: fenced deltas never reached text_acc (the fence's
                    # continue precedes accumulation), so a post-Stop flush
                    # carries pre-stop narration only.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        _resp_obj = event.get("response")
                        _resp_model = (
                            _resp_obj.get("model") if isinstance(_resp_obj, dict) else None
                        )
                        _final_model = (
                            _resp_model
                            if isinstance(_resp_model, str) and _resp_model
                            else current_model
                        )
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            _final_model,
                        )

                    error_item = _error_item_from_sse(
                        event,
                        response_id=current_response_id,
                    )
                    if error_item is not None:
                        await _relay_persist_error_once(
                            conversation_store,
                            session_id,
                            error_item,
                        )

                    # Persist resource lifecycle events
                    # (session.resource.created / .deleted) emitted by
                    # agent-tool terminal launches/closes so reconnecting
                    # clients rediscover the resource in the snapshot.
                    # The live publish below already updates connected
                    # clients.
                    resource_item = _resource_event_item_from_sse(session_id, event)
                    if resource_item is not None:
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            resource_item,
                        )
                        # Self-heal the spin-up flag: a created terminal is
                        # authoritative proof the session is no longer
                        # "starting up", so clear it even if the runner's
                        # auto-create finally was skipped (e.g. hard kill
                        # between launch and clear). Only fire on a real
                        # state change to avoid redundant stream traffic.
                        if (
                            resource_item.data.event_type == "session.resource.created"
                            and resource_item.data.resource_type == "terminal"
                            and _session_terminal_pending_cache.get(session_id, False)
                        ):
                            _publish_terminal_pending(session_id, False)

                    # Accumulate LLM token usage from the harness
                    # response so policy callables can read
                    # event["context"]["usage"]["total_cost_usd"].
                    if evt_type == "response.completed":
                        # Persist the turn's usage (cost + token buckets) so
                        # policy callables can read
                        # event["context"]["usage"]["total_cost_usd"] and the
                        # subtree roll-up below sees the new totals.
                        _accumulate_session_usage(
                            event.get("response", {}),
                            session_id,
                            conversation_store,
                        )
                        # Push the server-computed cost AND token breakdown
                        # to the web client's session indicator, rolled up
                        # over the spawn subtree. The session's own event
                        # carries its SUBTREE total (this conversation + its
                        # sub-agents), and each ancestor gets its own subtree
                        # total on its own stream — so a supervisor's badge
                        # includes its sub-agents and a parent updates live
                        # when a relay sub-agent spends. Mirrors the native
                        # path (_persist_external_session_usage); the roll-up
                        # was wired for native only, but relay agents (e.g.
                        # claude-sdk) need it too. Cost is included only when
                        # priced; the token breakdown rides along whenever any
                        # bucket is recorded (so an unpriced session still
                        # surfaces tokens). context_tokens/window already ride
                        # on the response.completed event. Threaded: store
                        # reads + SSE fan-out.
                        _subtree_usage = await asyncio.to_thread(
                            load_session_usage,
                            session_id,
                            conversation_store,
                        )
                        _subtree_cost = _priced_cost_for_display(_subtree_usage)
                        _usage_by_model = _usage_by_model_for_display(_subtree_usage)
                        if _subtree_cost is not None or _usage_by_model is not None:
                            _usage_payload: dict[str, Any] = {
                                "type": "session.usage",
                                "conversation_id": session_id,
                            }
                            if _subtree_cost is not None:
                                _usage_payload["total_cost_usd"] = _subtree_cost
                            if _usage_by_model is not None:
                                _usage_payload["usage_by_model"] = _usage_by_model
                            session_stream.publish(
                                session_id,
                                SessionUsageEvent(**_usage_payload).model_dump(exclude_none=True),
                            )
                            await asyncio.to_thread(
                                _publish_subtree_cost_to_ancestors,
                                conversation_store,
                                session_id,
                            )

                    # Reset the turn-scoped response_id on any
                    # terminal event so it doesn't leak to the
                    # next turn.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        current_response_id = None

                    # Patch the live event's response_id for
                    # function_call_output items whose call_id maps
                    # to a known function_call response_id. This
                    # ensures the web UI's block stream pairs the
                    # tool result with its call in the same bubble.
                    if (
                        evt_type == "response.output_item.done"
                        and isinstance(event.get("item"), dict)
                        and event["item"].get("type") == "function_call_output"
                    ):
                        _live_cid = event["item"].get("call_id")
                        if isinstance(_live_cid, str) and _live_cid in tool_call_response_ids:
                            event = {
                                **event,
                                "item": {
                                    **event["item"],
                                    "response_id": tool_call_response_ids[_live_cid],
                                },
                            }
                    if evt_type == "response.elicitation_request":
                        session_stream.publish(session_id, event)
                        await asyncio.to_thread(
                            _publish_elicitation_request_to_ancestors,
                            conversation_store,
                            session_id,
                            event,
                        )
                        continue
                    if evt_type == "response.elicitation_resolved":
                        session_stream.publish(session_id, event)
                        elicitation_id = event.get("elicitation_id")
                        if isinstance(elicitation_id, str) and elicitation_id:
                            await asyncio.to_thread(
                                _publish_elicitation_resolved_to_ancestors,
                                conversation_store,
                                session_id,
                                elicitation_id,
                            )
                        continue
                    session_stream.publish(session_id, event)

    except (httpx.HTTPError, ConnectionError):
        # WSTunnelTransport raises bare ConnectionError on tunnel
        # close; treat the same as HTTPError so the task exits
        # gracefully instead of leaving an unretrieved exception.
        _logger.warning(
            "Relay: ended for session=%s",
            session_id,
            exc_info=True,
        )
    except asyncio.CancelledError:
        raise
    finally:
        _logger.info("Relay: task exiting for session=%s", session_id)
        # Drop any in-flight assistant-text entry so a relay that exits
        # WITHOUT a terminal turn event (runner death / tunnel drop
        # mid-turn, or a rebind cancellation) can't strand it forever.
        # Normal turn-ends already clear via record_publish.
        inflight_text.discard(session_id)
        # Relay ended (runner dropped/rebound): re-discover skills next time.
        # Cancel any in-flight fetch so it can't land stale skills from the
        # dead runner into the cache after this pop.
        _runner_skills_cache.pop(session_id, None)
        inflight = _runner_skills_inflight.pop(session_id, None)
        if inflight is not None:
            inflight.cancel()


def _ensure_runner_relay(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start (or replace) the SSE relay for ``session_id``.

    No-op when a healthy relay is already bound to ``runner_id``.
    When the bound runner changes (last-write-wins PATCH-rebind),
    the stale relay is cancelled and a fresh one is created
    against the new runner.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the new relay subscribes to,
        e.g. ``"runner_abc123"``. ``None`` skips relay
        (in-process path with no runner binding).
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay.
    :param conversation_store: Store for persisting items from
        the runner's SSE stream. ``None`` disables persistence.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    """
    if runner_client is None or runner_id is None:
        _logger.info(
            "Relay: skipping for session=%s (runner_client=%s, runner_id=%s)",
            session_id,
            runner_client is not None,
            runner_id,
        )
        return None
    existing = _runner_relay_tasks.get(session_id)
    if existing is not None:
        if existing.runner_id == runner_id and not existing.task.done():
            _logger.info("Relay: reusing existing for session=%s runner=%s", session_id, runner_id)
            return existing  # same runner, healthy task
        _logger.info(
            "Relay: replacing stale for session=%s (old_runner=%s done=%s)",
            session_id,
            existing.runner_id,
            existing.task.done(),
        )
        if not existing.task.done():
            existing.task.cancel()  # stale binding; replace
    else:
        _logger.info("Relay: creating new for session=%s runner=%s", session_id, runner_id)
    ready = asyncio.Event()
    task = asyncio.create_task(
        _relay_runner_stream(
            session_id,
            runner_client,
            conversation_store,
            ready,
        ),
        name=f"runner-relay-{session_id}",
    )
    handle = _RelayHandle(runner_id=runner_id, task=task, ready=ready)
    _runner_relay_tasks[session_id] = handle

    def _on_done(t: asyncio.Task[None]) -> None:
        # Clear our slot only if it still holds this task — a
        # later rebind may have replaced us.
        current = _runner_relay_tasks.get(session_id)
        if current is not None and current.task is t:
            _runner_relay_tasks.pop(session_id, None)

    task.add_done_callback(_on_done)
    return handle


async def _ensure_runner_relay_ready(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start the runner SSE relay and wait for its subscription ack.

    The runner stream has no replay buffer. For item events, Omnigent must
    subscribe to runner output before it forwards the input event; a
    fast harness can otherwise complete before Omnigent is listening, leaving
    the user with an apparently successful empty response.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the relay should bind to, e.g.
        ``"runner_abc123"``. ``None`` skips relay setup.
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay setup.
    :param conversation_store: Store for persisting relayed items.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    :raises OmnigentError: If the relay cannot observe the
        runner stream's ready heartbeat before the timeout.
    """
    handle = _ensure_runner_relay(
        session_id,
        runner_id,
        runner_client,
        conversation_store,
    )
    if handle is None or handle.ready.is_set():
        return handle
    try:
        await asyncio.wait_for(
            handle.ready.wait(),
            timeout=_RUNNER_RELAY_READY_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        if handle.task.done():
            raise OmnigentError(
                "Runner stream relay exited before becoming ready",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        raise OmnigentError(
            "Timed out waiting for runner stream relay to subscribe",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    return handle


# Per-session compaction locks so concurrent ``/compact`` POSTs
# don't race.
_COMPACT_LOCKS: dict[str, asyncio.Lock] = {}


async def _run_compact_locked(
    session_id: str,
    conv: Conversation,
    agent_store: AgentStore,
    agent_cache: AgentCache | None,
) -> None:
    """
    Run explicit compaction while holding the per-session compact lock.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row.
    :param agent_store: Agent store for spec lookup.
    :param agent_cache: Agent cache for bundle loading.
    """
    if conv.agent_id is None:
        raise OmnigentError("Session has no agent binding", code=ErrorCode.INTERNAL_ERROR)
    if agent_cache is None:
        raise OmnigentError(
            "Compaction is unavailable: agent cache is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Check live status via cache; tasks table has been removed.
    if _session_status_cache.get(session_id) in ("running", "waiting"):
        raise OmnigentError(
            "Cannot compact while a turn is running; cancel or wait for it to finish first",
            code=ErrorCode.CONFLICT,
        )
    agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
    if agent is None or agent.bundle_location is None:
        raise OmnigentError(
            f"Agent not found: {conv.agent_id!r}",
            code=ErrorCode.NOT_FOUND,
        )
    loaded = agent_cache.load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
    spec = loaded.spec
    if spec.llm is not None:
        llm_config = spec.llm
    elif spec.executor.model is not None:
        from omnigent.spec.types import LLMConfig

        llm_config = LLMConfig(model=spec.executor.model, connection=spec.executor.connection)
    else:
        raise OmnigentError(
            "Compaction requires a configured LLM model",
            code=ErrorCode.INVALID_INPUT,
        )
    task_id = f"compact_{int(time.time() * 1000)}"
    _publish_status(session_id, "running")
    _publish_compaction_in_progress(session_id)
    from omnigent.runtime.workflow import compact_conversation_now

    try:
        result = await compact_conversation_now(
            task_id=task_id,
            conversation_id=session_id,
            spec=spec,
            llm_config=llm_config,
            tool_schemas=[],
            preserve_recent_window=1,
        )
    except Exception as exc:
        _logger.exception("Explicit session compaction failed for %s", session_id)
        detail = str(exc) or repr(exc)
        _publish_compaction_failed(session_id)
        _publish_status(session_id, "idle")
        raise OmnigentError(
            f"Compaction failed while generating a summary: {detail}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    _publish_compaction_completed(session_id, result.total_tokens)
    _publish_status(session_id, "idle")


def _agent_provider_family(agent: Agent) -> str | None:
    """Return the provider family of an agent's harness, or ``None``.

    Loads the agent's spec to read its ``harness_kind`` and maps it to a
    provider family (``"anthropic"`` / ``"openai"``). Returns ``None`` when
    the bundle can't be loaded or the harness is unknown — callers treat
    ``None`` as "can't confirm same family".

    :param agent: The agent whose harness family to resolve.
    :returns: ``"anthropic"`` / ``"openai"``, else ``None``.
    """
    from omnigent.onboarding.provider_config import provider_family_for_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → unknown family
        return None
    return provider_family_for_harness(spec.executor.harness_kind)


def _same_provider_family(a: Agent, b: Agent) -> bool:
    """Return whether two agents share a (known) provider family.

    ``False`` when either family is undeterminable, so a fork that can't
    confirm both agents speak the same provider resets model settings and
    skips resuming the source's native session (the runner rebuilds the
    native transcript from Omnigent items instead).

    :param a: First agent (e.g. the fork source's agent).
    :param b: Second agent (e.g. the switch target).
    :returns: ``True`` when both resolve to the same non-``None`` family.
    """
    family_a = _agent_provider_family(a)
    return family_a is not None and family_a == _agent_provider_family(b)


def _agent_is_native(agent: Agent) -> bool:
    """Return whether an agent runs a native CLI harness.

    Loads the agent's spec to read its ``harness_kind``. A native target
    (claude-native / codex-native) is the only case where a fork needs the
    transcript-rebuild carry path — SDK targets replay the Omnigent
    transcript as context on their own. Returns ``False`` when the bundle
    can't be loaded (treated as non-native).

    :param agent: The agent whose harness to classify.
    :returns: ``True`` for a native CLI harness, else ``False``.
    """
    from omnigent.harness_aliases import is_native_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001 — unloadable bundle → treat as non-native
        return False
    return is_native_harness(spec.executor.harness_kind)


def _presentation_labels_for_agent(agent: Agent) -> dict[str, str]:
    """Return the Web UI presentation labels for an agent's harness.

    A native-CLI agent runs **terminal-first** (the inline terminal is the
    main view), gated on ``omnigent.ui == "terminal"`` plus the matching
    ``omnigent.wrapper`` value; an SDK agent runs as plain chat (no such
    labels). Used by the fork route so a switched clone's UI mode matches
    the TARGET harness instead of inheriting the source's — otherwise an SDK
    clone of a claude-native session renders a stale interactive terminal.

    :param agent: The agent the fork will bind.
    :returns: ``{ui: terminal, wrapper: <value>}`` for a native agent, or
        ``{}`` for an SDK agent / undeterminable family (chat mode).
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    if not _agent_is_native(agent):
        return {}
    family = _agent_provider_family(agent)
    wrapper = {
        "anthropic": CLAUDE_NATIVE_WRAPPER_VALUE,
        "openai": CODEX_NATIVE_WRAPPER_VALUE,
    }.get(family or "")
    if wrapper is None:
        return {}
    return {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE, WRAPPER_LABEL_KEY: wrapper}


async def _register_policy_elicitation(
    session_id: str,
    result: PolicyResult,
    arguments_preview: str,
    conversation_store: ConversationStore,
) -> str:
    """
    Publish an elicitation request event on the session stream.

    Approval state lives on the runner (in-memory
    ``_pending_approvals`` dict). The server just publishes the
    ``response.elicitation_request`` SSE event so the client
    sees the approval prompt, and returns the elicitation_id
    so the runner can key its Future on it.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param result: The :class:`PolicyResult` with action=ASK,
        carrying the reason and deciding_policy fields.
    :param arguments_preview: Truncated argument string for
        the elicitation UI preview (max ~1024 chars).
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :returns: The generated elicitation id,
        e.g. ``"elicit_a1b2c3..."``.
    """
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    elicitation = ElicitationRequest(
        message=result.reason or "Approval required",
        requested_schema={},
        phase=Phase.TOOL_CALL.value,
        policy_name=result.deciding_policy or "unknown",
        content_preview=arguments_preview[:1024],
    )
    # Approval state lives on the runner (in-memory
    # _pending_approvals dict of elicitation_id → Future).
    # The server just publishes the elicitation SSE event and
    # returns the elicitation_id. The runner parks on the
    # Future; the client's approval event is forwarded to the
    # runner which resolves it. No server-side state needed.
    _elicit_event = build_elicitation_request_event(
        elicitation_id, elicitation, session_id=session_id
    )
    session_stream.publish(session_id, _elicit_event)
    await asyncio.to_thread(
        _publish_elicitation_request_to_ancestors,
        conversation_store,
        session_id,
        _elicit_event,
    )
    return elicitation_id


def _load_agent_spec_for_session(
    conv: Conversation,
    agent_store: AgentStore,
) -> AgentSpec | None:
    # Split from _build_policy_engine_from_spec so the caller can run the
    # cheap guardrails/default-policy skip check between the two and avoid
    # paying for engine construction when no policy could fire. Both halves
    # are blocking DB/IO, so each is run under asyncio.to_thread.
    if conv.agent_id is None:
        return None
    agent = agent_store.get(conv.agent_id)
    if agent is None:
        return None
    return (
        get_agent_cache()
        .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
        .spec
    )


def _build_policy_engine_from_spec(
    spec: AgentSpec,
    session_id: str,
    conversation_store: ConversationStore,
) -> PolicyEngine:
    caps = get_caps()
    host_connection = (
        caps.policy_llm_connection_factory() if caps.policy_llm_connection_factory else None
    )
    return build_policy_engine(
        spec=spec,
        conversation_id=session_id,
        conversation_store=conversation_store,
        default_policies=caps.default_policies,
        policy_store=get_policy_store(),
        server_llm=caps.llm,
        host_connection=host_connection,
    )


async def _apply_pending_policy_ask_writes(
    session_id: str,
    conv: Conversation,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    data: dict[str, Any],
) -> None:
    """
    Apply (or drop) policy writes stashed for a relay tool-call ASK.

    Called when an ``approval`` verdict resolves a runner-owned policy
    elicitation (both approval entry points — the ``approval`` event and the
    resolve URL — route here via their callers). On ``accept`` the deciding
    policy's stashed ``state_updates`` / ``set_labels`` are persisted by a
    freshly built engine — exactly what the native ``_hold_native_ask_gate``
    path does inline. On any other verdict (decline / cancel / missing) they
    are dropped (POLICIES.md §7.2: a denied ASK leaves no trace). No-op when
    the elicitation has no stashed writes (the common case — most ASKs and
    all non-policy elicitations).

    :param session_id: Session id that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param conv: The session conversation, for the agent / spec lookup.
    :param conversation_store: Store the engine persists session state to.
    :param agent_store: Store for the agent spec lookup.
    :param data: The approval payload, carrying ``elicitation_id`` and the
        verdict ``action`` (e.g. ``{"elicitation_id": "elicit_x",
        "action": "accept"}``).
    :returns: None.
    """
    elicitation_id = data.get("elicitation_id", "")
    pending = _pending_policy_ask_writes.get(elicitation_id)
    if pending is None:
        return
    if data.get("action") != "accept":
        # Declined — remove the stashed writes (POLICIES.md §7.2:
        # a denied ASK leaves no trace).
        _pending_policy_ask_writes.pop(elicitation_id, None)
        return
    if pending.from_mcp:
        # MCP entries: the retry path (POST /mcp with requestState)
        # pops and applies the writes itself. Applying here too would
        # double-apply non-idempotent ops (e.g. INCREMENT state
        # updates for cost-budget counters). Leave the entry for the
        # retry path; it owns cleanup.
        return
    # Non-MCP relay path: pop and apply writes here since no retry
    # will arrive.
    _pending_policy_ask_writes.pop(elicitation_id, None)
    # Resolve the agent spec + build the engine off the event loop: the
    # lookup, cold-cache bundle fetch, and engine construction are all
    # blocking DB/IO.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    # The label/state writes hit the DB synchronously too — keep them
    # off the loop.
    if pending.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, pending.set_labels)
    if pending.state_updates:
        await asyncio.to_thread(engine.apply_state_updates, pending.state_updates)


def _build_actor(user_id: str | None) -> dict[str, str] | None:
    """
    Build the ``actor`` dict for :class:`EvaluationContext`.

    Returns ``{"run_as": user_id}`` when the authenticated user is
    known, ``None`` otherwise (tests, legacy callers without auth).

    :param user_id: Authenticated user email from the request,
        e.g. ``"alice@example.com"``. ``None`` when auth is
        disabled or the caller is unauthenticated.
    :returns: Actor dict or ``None``.
    """
    if user_id is None:
        return None
    return {"run_as": user_id}


def _build_evaluation_context(
    phase: Phase,
    data: dict[str, Any],
    event: dict[str, Any],
    *,
    actor: dict[str, str] | None = None,
) -> EvaluationContext:
    """
    Build an :class:`EvaluationContext` from a proto-style event dict.

    Maps the proto ``Event.data`` shape to the internal convention:

    - ``TOOL_CALL``: ``content = {"name": name, "arguments": args}``,
      ``tool_name = name``.
    - ``TOOL_RESULT``: ``content = {"result": result_str}``,
      ``tool_name`` from ``request_data.name``,
      ``request_data`` from the event's ``request_data`` field.
    - ``REQUEST`` / ``RESPONSE``: ``content = str(data)``.

    :param phase: Internal phase enum.
    :param data: ``event.data`` dict from the proto request.
    :param event: Full event dict (for ``request_data``, ``context``).
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: Ready-to-evaluate context.
    """
    # A native hook may stamp the session's live model into the event context
    # (e.g. the codex hook reads it from ``config.toml`` at gate time — the
    # source of truth for an in-TUI ``/model`` selection). When present, this
    # wins over the engine's server-resolved model (see
    # ``PolicyEngine._inject_model``); ``None`` falls back to that resolution.
    raw_context = event.get("context") or {}
    supplied_model = raw_context.get("model")
    hook_model = supplied_model if isinstance(supplied_model, str) and supplied_model else None
    # The harness, when a native hook stamped it (e.g. the codex hook), so
    # policies can tailor messages to the session's model-switch surface
    # (codex-native is terminal-only). Carried through unchanged — the engine
    # neither resolves nor overrides it.
    supplied_harness = raw_context.get("harness")
    hook_harness = (
        supplied_harness if isinstance(supplied_harness, str) and supplied_harness else None
    )
    if phase == Phase.TOOL_CALL:
        tool_name = data.get("name") or ""
        args = data.get("arguments") or {}
        return EvaluationContext(
            phase=phase,
            content={"name": tool_name, "arguments": args},
            tool_name=tool_name or None,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    if phase == Phase.TOOL_RESULT:
        tool_result = data.get("result", "")
        request_data = event.get("request_data")
        tool_name = None
        if isinstance(request_data, dict):
            tool_name = request_data.get("name")
        return EvaluationContext(
            phase=phase,
            content={
                "result": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
            },
            tool_name=tool_name,
            request_data=request_data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # LLM_REQUEST / LLM_RESPONSE — content is the full request/response dict.
    if phase in (Phase.LLM_REQUEST, Phase.LLM_RESPONSE):
        return EvaluationContext(
            phase=phase,
            content=data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # REQUEST / RESPONSE — content is the user/assistant text.
    text = data.get("text") or data.get("content") or str(data)
    return EvaluationContext(
        phase=phase,
        content=text if isinstance(text, str) else json.dumps(text),
        actor=actor,
        model=hook_model,
        harness=hook_harness,
    )


async def _evaluate_tool_call_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a tool call against TOOL_CALL phase policy rules.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. Returns a verdict dict on DENY or ASK.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``function_call`` event with
        ``evaluate_policy: true``.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        on DENY/ASK.
    """

    tool_name = body.data.get("name")
    if not tool_name or not isinstance(tool_name, str):
        raise OmnigentError(
            "function_call event with evaluate_policy requires a non-empty 'name' field in data",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments_str = body.data.get("arguments", "{}")

    # Resolve agent spec + build engine off the event loop (blocking
    # DB/IO). Tool-call policy always evaluates (no guardrails skip).
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )

    try:
        args_payload = json.loads(arguments_str)
    except (ValueError, TypeError):
        args_payload = arguments_str

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": tool_name, "arguments": args_payload},
        tool_name=tool_name,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — publish elicitation event. Approval state lives
    # on the runner (_pending_approvals dict).
    elicitation_id = await _register_policy_elicitation(
        session_id=session_id,
        result=result,
        arguments_preview=arguments_str,
        conversation_store=conversation_store,
    )
    # The deciding policy's writes (e.g. a cost-budget checkpoint via
    # ``state_updates``) must land ONLY on approve. This relay path returns
    # ``pending`` and the verdict arrives later off-request, so stash them to
    # apply when the matching ``approval`` resolves with accept (see
    # _apply_pending_policy_ask_writes). The native path applies these inline
    # in _hold_native_ask_gate; without this, a relay/non-native session's
    # checkpoint is never recorded and the ASK re-prompts every tool call.
    # Always store an entry even when there are no deferred writes —
    # the MCP retry path checks the pending map to verify the
    # elicitation was genuinely issued by the server.
    _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
        state_updates=result.state_updates,
        set_labels=result.set_labels,
    )
    return {
        "verdict": "pending",
        "elicitation_id": elicitation_id,
        # Spec-resolved approval window; the runner's park honors it.
        "ask_timeout": resolve_ask_timeout(engine, result),
    }


def _extract_user_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from a user message event body.

    Mirrors the logic in ``workflow._extract_user_text`` but
    operates on the raw ``SessionEventInput.data`` dict rather
    than a parsed ``MessageData`` object.

    :param body: The validated ``message`` event with
        ``role: "user"``.
    :returns: Joined text from ``input_text`` / ``text`` content
        blocks. Empty string if no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


async def _evaluate_input_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a user message against INPUT phase policy rules.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW (caller should persist via the active
    path). Returns a verdict dict on DENY or ASK (caller
    should NOT forward to runner).

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through to persist
        path). Verdict dict on DENY/ASK.
    """

    user_text = _extract_user_text_from_event(body)
    if not user_text:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    # Skip only when there are no agent guardrails AND no server-wide
    # default policies AND no session policies. Without this, default/
    # session policies (e.g. deny_pii_in_llm_request added via the UI)
    # are silently skipped for agents without a guardrails: YAML block.
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content=user_text,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — publish elicitation event.
    elicitation_id = await _register_policy_elicitation(
        session_id=session_id,
        result=result,
        arguments_preview=user_text[:1024],
        conversation_store=conversation_store,
    )
    return {
        "verdict": "pending",
        "elicitation_id": elicitation_id,
        # Spec-resolved approval window; the runner's park honors it.
        "ask_timeout": resolve_ask_timeout(engine, result),
    }


def _extract_assistant_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from an assistant message event.

    Mirrors :func:`_extract_user_text_from_event` but for
    assistant messages. Content blocks use ``"text"`` (not
    ``"input_text"``).

    :param body: The validated ``message`` event with
        ``role: "assistant"``.
    :returns: Joined text from content blocks. Empty string if
        no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


_DENY_SENTINEL_PREFIX = "[Denied by policy: "


def _replace_text_in_message_body(
    body: SessionEventInput,
    replacement: str,
) -> SessionEventInput:
    """
    Return a copy of the message body with all text content
    blocks replaced by *replacement*.

    Used by OUTPUT policy DENY to substitute the deny sentinel
    into the persisted message while preserving non-text content
    blocks (images, etc.) and all other body fields.

    :param body: The original assistant message event.
    :param replacement: The deny sentinel text,
        e.g. ``"[Denied by policy: harmful content]"``.
    :returns: A new body with text blocks replaced.
    """
    content = body.data.get("content") or []
    new_content: list[dict[str, Any]] = []
    replaced = False
    for block in content:
        if isinstance(block, dict) and "text" in block:
            if not replaced:
                new_content.append({"type": "output_text", "text": replacement})
                replaced = True
        else:
            new_content.append(block)
    if not replaced:
        new_content.append({"type": "output_text", "text": replacement})
    new_data = {**body.data, "content": new_content}
    return type(body)(type=body.type, data=new_data)


async def _evaluate_output_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate an assistant message against OUTPUT phase policies.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. On DENY, returns a verdict dict with
    ``_denied_body`` — the caller should persist this modified
    body (text replaced with deny sentinel) instead of the
    original.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        with ``_denied_body`` on DENY.
    """

    assistant_text = _extract_assistant_text_from_event(body)
    if not assistant_text:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )
    ctx = EvaluationContext(
        phase=Phase.RESPONSE,
        content=assistant_text,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    # DENY — build the denied body with sentinel text.
    # The caller persists this modified body instead of the
    # original (Option B).
    if result.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
    reason = result.reason or "Denied by policy"
    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    denied_body = _replace_text_in_message_body(body, sentinel)
    return {
        "verdict": "deny",
        "reason": reason,
        "_denied_body": denied_body,
    }


# Runner router for the native-terminal approval popup, set once at app
# startup (see :func:`set_server_runner_router`). The tool-policy ASK gate
# forwards a ``cost_approval_popup`` control event to the bound runner from
# a parked-gate background task that carries no FastAPI request / route
# closure, so it reads the router from this module-level global.
_server_runner_router: RunnerRouter | None = None


def set_server_runner_router(runner_router: RunnerRouter | None) -> None:
    """
    Stash the runner router for the native-terminal approval popup.

    Called once from ``create_app`` so the tool-policy ASK gate
    (:func:`_spawn_native_approval_popup_forward`) can reach the bound
    runner from background contexts that do not carry the request / route
    closure.

    :param runner_router: The session runner router, or ``None`` in
        in-process setups.
    :returns: None.
    """
    global _server_runner_router
    _server_runner_router = runner_router


async def _wake_parent_for_blocked_child(
    parent_id: str,
    child: Conversation,
    notice: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> bool:
    """
    Deliver a parent-wake notice when a sub-agent blocks on an approval.

    Posts the ``[System: …]`` notice as a synthetic user message to the
    parent's ``POST /v1/sessions/{id}/events`` — the same path the runner's
    terminal-completion wake uses, so it starts a continuation turn (idle
    parent) or coalesces with pending input (busy parent). Best-effort: a
    missing parent, missing runner, or transport error is logged and swallowed
    (a dropped wake is no worse than the pre-fix no-wake baseline), but the
    *outcome* is reported back so the notifier can release its per-block
    debounce and let a later publish retry rather than silencing the block.

    :param parent_id: Parent session id, e.g. ``\"conv_parent123\"``.
    :param child: The blocked child :class:`Conversation`; used only for its
        label/id in the notice and logs.
    :param notice: The ``[System: …]`` text to inject into the parent.
    :param conversation_store: Used to load the parent :class:`Conversation`
        and persist the synthetic user message item.
    :param runner_router: Router used to resolve the parent's bound
        runner. ``None`` in in-process setups (the runtime singleton is
        consulted as a fallback).
    :returns: ``True`` when the notice was dispatched to the parent's runner;
        ``False`` when delivery could not happen (parent gone, no runner bound,
        or the forward raised a transport error).
    """
    parent_conv = await asyncio.to_thread(conversation_store.get_conversation, parent_id)
    if parent_conv is None:
        # Parent vanished between publish and wake (cascading-delete race).
        _logger.debug(
            "subagent block notifier: parent %s missing; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    runner_client = await _get_runner_client(parent_id, runner_router)
    if runner_client is None:
        # WARNING (not DEBUG): an unbound parent is the transient-miss case the
        # notifier retries — surface it rather than burying it as routine.
        _logger.warning(
            "subagent block notifier: no runner bound for parent %s; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    # Ensure the parent's SSE relay is live so the wake turn's output is
    # persisted (parity with post_event).
    _ensure_runner_relay(
        parent_id,
        parent_conv.runner_id,
        runner_client,
        conversation_store,
    )
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": notice}],
        },
    )
    try:
        # None args: a system notice carries no agent/files/artifacts; the runner
        # recomputes has_mcp_servers from the parent's cached spec.
        await _dispatch_session_event_to_runner(
            parent_id,
            parent_conv,
            body,
            conversation_store,
            runner_client,
            agent_name=None,
            file_store=None,
            artifact_store=None,
            runner_router=runner_router,
        )
    except (httpx.HTTPError, OmnigentError):
        _logger.warning(
            "subagent block wake POST failed for parent=%s child=%s",
            parent_id,
            child.id,
            exc_info=True,
        )
        return False
    return True


def configure_subagent_block_notifier(
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> Callable[[], None]:
    """
    Install the parent-wake notifier on the elicitation publish path.

    Wires :class:`SubagentBlockNotifier` into
    :mod:`omnigent.runtime.pending_elicitations` so a sub-agent that
    blocks on an approval immediately wakes its immediate parent through
    the same ``/events`` ingest path the runner-side terminal-completion
    wake already uses (see
    :func:`_wake_parent_for_blocked_child`). Top-level sessions (no
    parent) are no-ops; multi-user safety is inherent because the wake
    is delivered to the recorded ``parent_conversation_id`` only, never
    fanned out to collaborators or unrelated sessions.

    :param conversation_store: Store used to resolve a child's
        ``parent_conversation_id`` and to persist the wake message.
    :param runner_router: Router used by the wake to reach the parent's
        bound runner. ``None`` in in-process setups.
    :returns: A callable that uninstalls the observer and cancels any
        in-flight wake futures. Call from the lifespan teardown.
    """
    from omnigent.runtime import pending_elicitations as _pending_elicitations
    from omnigent.runtime.subagent_block_notifier import SubagentBlockNotifier

    loop = asyncio.get_running_loop()

    async def _wake_dispatch(parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Deliver one wake notice (the notifier's injected dispatch).

        :param parent_id: Parent session id.
        :param child: The blocked child :class:`Conversation`.
        :param notice: Pre-formatted ``[System: …]`` text.
        :returns: ``True`` when the notice reached the parent's runner,
            ``False`` when it could not be delivered (so the notifier
            releases the debounce and a re-publish can retry).
        """
        return await _wake_parent_for_blocked_child(
            parent_id,
            child,
            notice,
            conversation_store=conversation_store,
            runner_router=runner_router,
        )

    notifier = SubagentBlockNotifier(
        conversation_store=conversation_store,
        wake_dispatch=_wake_dispatch,
        loop=loop,
    )
    _pending_elicitations.set_elicitation_observer(notifier.observe)

    def _uninstall() -> None:
        """Remove the observer and cancel any outstanding wake futures."""
        _pending_elicitations.set_elicitation_observer(None)
        notifier.close()

    return _uninstall


async def _stream_live_events(
    request: Request,
    session_id: str,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
    viewer_user_id: str | None = None,
    viewer_idle: bool = False,
    presence_root_id: str | None = None,
) -> AsyncIterator[str]:
    """
    Yield SSE-formatted events from the conversation's live stream.

    Events are delivered live from the moment :func:`session_stream.subscribe`
    is invoked forward — there is no buffer and no replay. Events
    published before this generator subscribed are lost; clients
    reconcile pre-subscribe state via the snapshot endpoint
    (``GET /v1/sessions/{id}``) and dedupe by item id.

    On client disconnect the subscribe loop breaks; the
    ``finally`` block emits a ``[DONE]`` sentinel so well-behaved
    SSE consumers see a clean stream termination. The pub-sub
    layer auto-cleans this generator's subscriber slot in its own
    ``finally`` when iteration exits.

    Each emitted dict is validated against
    :data:`ServerStreamEvent` at the wire boundary so a runtime
    that publishes an unmodelled ``type`` fails loud rather than
    serializing an unknown event verbatim.

    The subscribe call passes a ``ready_event`` heartbeat plus
    ``heartbeat_interval_s``. The ready heartbeat is yielded
    immediately after the live-tail subscriber slot is registered,
    before any snapshot hook runs, so clients can wait for a
    concrete subscription acknowledgment before posting a fast
    one-shot turn. The interval heartbeat keeps an idle stream
    emitting ``session.heartbeat`` events on a fixed cadence (see
    :data:`_SESSION_STREAM_HEARTBEAT_INTERVAL_S`). Without that,
    a stream that sits between turns has nothing crossing the wire;
    the client's SSE read-timeout and this route's
    ``request.is_disconnected()`` check (only polled on event
    arrival) both lag for minutes after a half-open socket forms
    (e.g. after a laptop sleep). The heartbeat gives both sides a
    regular byte to fire against.

    :param request: The FastAPI request, used to detect disconnect.
    :param session_id: Session/conversation identifier whose stream
        to subscribe to, e.g. ``"conv_abc123"``.
    :param on_subscribed: Optional snapshot-on-connect hook forwarded to
        :func:`session_stream.subscribe`; its events are yielded ahead of
        the live tail so a fresh client sees current resource state
        without polling. ``None`` (default) keeps the pure live-tail
        shape used by callers that reconcile via the snapshot endpoint.
    :param viewer_user_id: Authenticated identity to register in the
        session's presence registry for this stream's lifetime, e.g.
        ``"alice@example.com"``. ``None`` (default, and the reserved
        single-user sentinel mapped via ``attribution_user``) skips
        presence tracking entirely.
    :param viewer_idle: The viewer's connect-time idle flag (tab
        backgrounded), from the route's ``idle`` query param. Ignored
        when *viewer_user_id* is ``None``.
    :param presence_root_id: Root conversation of the streamed
        session's tree (its ``root_conversation_id``), e.g.
        ``"conv_root123"``. Presence is scoped to the tree's root so
        viewers of different agents/sub-agents in one session see
        each other. Required when *viewer_user_id* is set; ignored
        otherwise.
    :returns: An async iterator of SSE message strings.
    :raises ValueError: If *viewer_user_id* is set without
        *presence_root_id* — a per-conversation presence scope would
        silently split a session's viewers per agent.
    """
    # Presence registers before the subscribe loop: the join broadcast
    # fans out to ALREADY-subscribed co-viewers, while this stream
    # learns the full list (self included) from the snapshot-on-connect
    # presence event — full-state events make that ordering race benign.
    presence_token: str | None = None
    if viewer_user_id is not None:
        if presence_root_id is None:
            raise ValueError("presence_root_id is required when viewer_user_id is set")
        presence_token = presence.connect(
            presence_root_id, session_id, viewer_user_id, viewer_idle
        )
    try:
        async for event in session_stream.subscribe(
            session_id,
            heartbeat_interval_s=_SESSION_STREAM_HEARTBEAT_INTERVAL_S,
            ready_event={"type": "session.heartbeat"},
            # In-flight text replay must be captured synchronously at slot
            # registration (before ``ready_event`` suspends), not in the
            # async ``on_subscribed`` hook, or window deltas double-render.
            # Resource state stays in ``on_subscribed`` — it needs
            # awaits and is not dedup-sensitive.
            pre_ready_snapshot=lambda: inflight_text.snapshot_for(session_id),
            on_subscribed=on_subscribed,
        ):
            if await request.is_disconnected():
                break
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise ValueError(
                    f"session stream event missing string ``type`` field: {event!r}",
                )
            validated = _SERVER_STREAM_EVENT_ADAPTER.validate_python(event)
            yield _format_sse(event_type, validated.model_dump())
    finally:
        # The non-None checks besides presence_token's are type
        # narrowing only: a minted token implies both were set above.
        if (
            presence_token is not None
            and viewer_user_id is not None
            and presence_root_id is not None
        ):
            presence.disconnect(presence_root_id, viewer_user_id, presence_token)
        yield "data: [DONE]\n\n"


# Bounds for per-session native-terminal pass-through args
# (conversations.terminal_launch_args). These are CLI flags for the
# user's own claude / codex binary, so a few hundred short strings is
# already far beyond any real invocation; the caps just keep one
# session row small and bound the work the runner does rebuilding the
# launch command.
_MAX_TERMINAL_LAUNCH_ARGS = 256
_MAX_TERMINAL_LAUNCH_ARG_LEN = 4096


def _validate_terminal_launch_args(value: list[str] | None) -> list[str] | None:
    """
    Validate per-session native-terminal pass-through args.

    Enforces a flat list of strings within bounded count / length.
    The flat-list shape is the security boundary: there is no key for
    a caller to smuggle internal launch wiring (bridge dir, Omnigent URL,
    auth) through — those stay runner-owned (see
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md).

    :param value: The candidate args, e.g.
        ``["--dangerously-skip-permissions"]``, or ``None`` to leave
        unset / unchanged.
    :returns: The validated list unchanged, or ``None`` when *value*
        is ``None``.
    :raises ValueError: If *value* is not a list of strings, exceeds
        :data:`_MAX_TERMINAL_LAUNCH_ARGS` entries, or any entry
        exceeds :data:`_MAX_TERMINAL_LAUNCH_ARG_LEN` characters.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
        raise ValueError("terminal_launch_args must be a list of strings")
    if len(value) > _MAX_TERMINAL_LAUNCH_ARGS:
        raise ValueError(f"terminal_launch_args exceeds {_MAX_TERMINAL_LAUNCH_ARGS} entries")
    for arg in value:
        if len(arg) > _MAX_TERMINAL_LAUNCH_ARG_LEN:
            raise ValueError(
                f"terminal_launch_args entry exceeds {_MAX_TERMINAL_LAUNCH_ARG_LEN} characters"
            )
    return value


# Accepted values for the per-session cost-control switch
# (conversations.cost_control_mode_override): "on" activates the
# spec's configured cost-control mode, "off" disables it. Unset
# (NULL) defers to the spec default.
COST_CONTROL_OVERRIDE_VALUES = frozenset({"on", "off"})


def _validated_cost_control_mode_override(value: str | None) -> str | None:
    """
    Validate a caller-supplied per-session cost-control switch.

    :param value: The candidate value, e.g. ``"on"``, or ``None``
        when the caller did not set / wants to clear the override.
    :returns: The value unchanged when valid, or ``None``.
    :raises OmnigentError: 400 (``invalid_input``) when *value* is
        anything other than ``"on"``, ``"off"``, or ``None``.
    """
    if value is None or value in COST_CONTROL_OVERRIDE_VALUES:
        return value
    raise OmnigentError(
        f"invalid cost_control_mode_override: {value!r} (expected 'on', 'off', or null to clear)",
        code=ErrorCode.INVALID_INPUT,
    )


def _parse_session_create_metadata(metadata: str) -> SessionCreateMetadata:
    """
    Parse the JSON metadata part from bundled session creation.

    :param metadata: Raw JSON string from the multipart form,
        e.g. ``{"title": "debug auth flow"}``.
    :returns: Validated :class:`SessionCreateMetadata`.
    :raises OmnigentError: If the JSON fails the request schema.
    """
    try:
        parsed = SessionCreateMetadata.model_validate_json(metadata)
        reasoning_effort = validate_effort(
            parsed.reasoning_effort,
            "session metadata",
            EFFORT_VALUES,
        )
        # Bounds-check the native-terminal args; raises ValueError
        # (wrapped below) on a malformed or oversized list.
        _validate_terminal_launch_args(parsed.terminal_launch_args)
        return parsed.model_copy(update={"reasoning_effort": reasoning_effort})
    except (ValidationError, ValueError) as exc:
        raise OmnigentError(
            f"invalid session metadata: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _multipart_missing_detail(field: str) -> dict[str, Any]:
    """
    Build a FastAPI-style missing multipart field error.

    :param field: Missing form field name, e.g. ``"bundle"``.
    :returns: A validation-detail dict for HTTP 422 responses.
    """
    return {
        "type": "missing",
        "loc": ["body", field],
        "msg": "Field required",
        "input": None,
    }


def _require_host_conn_for_worktree(host_id: str | None, request: Request) -> HostConnection:
    """
    Resolve the live host connection for a worktree operation.

    :param host_id: Target host id from the session request, e.g.
        ``"host_a1b2c3d4..."``. ``None`` is rejected — git worktree
        creation requires a host (the server has no filesystem).
    :param request: FastAPI request carrying ``app.state.host_registry``.
    :returns: The live :class:`HostConnection` for ``host_id``.
    :raises OmnigentError: ``invalid_input`` when ``host_id`` is
        ``None``; ``internal_error`` when no host registry is
        configured; ``conflict`` when the host is offline.
    """
    if host_id is None:
        raise OmnigentError(
            "git worktree creation requires host_id",
            code=ErrorCode.INVALID_INPUT,
        )
    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        # Server misconfiguration, not bad client input — mirror
        # _validate_session_workspace, which also returns internal_error.
        raise OmnigentError(
            "host registry is not configured; cannot create a worktree",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        raise OmnigentError(
            f"host {host_id!r} is offline; reconnect the host and try again",
            code=ErrorCode.CONFLICT,
        )
    return host_conn


async def _create_session_worktree(
    *,
    host_id: str | None,
    source_repo: str | None,
    git: SessionGitOptions,
    request: Request,
) -> CreatedWorktree:
    """
    Create a git worktree on the host for a new session branch.

    Validates the branch name server-side (the host re-validates), then
    proxies ``host.create_worktree``. The returned worktree path
    becomes the session ``workspace``. See
    designs/SESSION_GIT_WORKTREE.md.

    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
        Required (worktree creation needs a host).
    :param source_repo: Canonical path of the picked source repo (the
        boundary-validated workspace), e.g. ``"/Users/alice/myrepo"``.
        ``None`` is a programming error and fails loud.
    :param git: Validated git options (``branch_name``, optional
        ``base_branch``).
    :param request: FastAPI request carrying the host registry.
    :returns: The created worktree's ``worktree_path`` (to store as
        ``workspace``) and ``branch`` (to store as ``git_branch``).
    :raises OmnigentError: ``invalid_input`` for a bad branch name,
        missing source repo, or a host-reported git failure (duplicate
        branch, bad base ref, not a repo); ``conflict`` when the host is
        offline or unresponsive; ``internal_error`` when no host registry
        is configured.
    """
    from omnigent.host.git_worktree import WorktreeError, validate_branch_name
    from omnigent.server.routes._host_worktree import (
        WorktreeHostUnavailableError,
        WorktreeProxyError,
        create_worktree_on_host,
    )

    if source_repo is None:  # pragma: no cover — host_id guarantees a workspace
        raise OmnigentError(
            "git worktree creation requires a source repository workspace",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        validate_branch_name(git.branch_name)
    except WorktreeError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

    host_conn = _require_host_conn_for_worktree(host_id, request)
    host_registry = request.app.state.host_registry
    try:
        return await create_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            repo_path=source_repo,
            branch_name=git.branch_name,
            base_branch=git.base_branch,
        )
    except WorktreeHostUnavailableError as exc:
        # Host offline / unresponsive — infra, not user input.
        raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
    except WorktreeProxyError as exc:
        # Host-reported git failure (dup branch, bad base, not a repo) —
        # user-correctable input.
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc


async def _remove_session_worktree_best_effort(
    *,
    host_id: str,
    worktree_path: str,
    branch: str,
    delete_branch: bool,
    request: Request,
    reason: str,
) -> None:
    """
    Best-effort removal of a session's git worktree.

    Used for create-rollback (orphan cleanup) and opt-in session-delete
    cleanup. Never raises — a failure is logged so the caller's primary
    operation still completes.

    :param host_id: Host that owns the worktree, e.g.
        ``"host_a1b2c3d4..."``.
    :param worktree_path: Absolute worktree directory to remove on the
        host, e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch checked out in the worktree, e.g.
        ``"feature/login"``.
    :param delete_branch: When ``True``, also run ``git branch -D``
        after removing the worktree directory.
    :param request: FastAPI request carrying the host registry.
    :param reason: Short label for log lines, e.g.
        ``"create-rollback"`` or ``"session-delete"``.
    """
    from omnigent.server.routes._host_worktree import (
        WorktreeProxyError,
        remove_worktree_on_host,
    )

    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        return
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        _logger.warning(
            "Skipping worktree removal (%s) for %s: host %s offline",
            reason,
            worktree_path,
            host_id,
        )
        return
    try:
        await remove_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            worktree_path=worktree_path,
            branch=branch,
            delete_branch=delete_branch,
        )
    except WorktreeProxyError:
        _logger.warning(
            "Best-effort worktree removal (%s) failed for %s",
            reason,
            worktree_path,
            exc_info=True,
        )


def _resolve_subagent_spec(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> AgentSpec | None:
    """
    Load the parent bundle and resolve a child sub-agent's trusted spec.

    This is the single trusted source for any per-sub-agent launch wiring
    the server derives at create time (terminal-first labels, YOLO
    pass-through args). The spec comes from the server-loaded parent
    bundle — never from caller-supplied request fields — so a caller
    cannot smuggle in launch config a sub-agent's own bundle did not
    declare.

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns ``None``).
    :returns: The matching child :class:`AgentSpec`, or ``None`` when the
        cache is absent, the bundle fails to load, or no sub-agent matches.
    """
    if agent_cache is None:
        return None
    from omnigent.runtime.workflow import _find_spec_by_name

    try:
        parent_spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        ).spec
    except Exception:  # noqa: BLE001 -- create-time resolution is best-effort; never block create.
        # A bundle that fails to load here must not break session
        # creation; the session still works, just without the
        # derived labels / launch args.
        _logger.warning(
            "Could not load bundle for agent %s to resolve sub-agent %r spec",
            agent.id,
            sub_agent_name,
            exc_info=True,
        )
        return None
    return _find_spec_by_name(parent_spec, sub_agent_name)


def _spec_harness(spec: AgentSpec) -> str:
    """
    Return the canonical harness identifier for a resolved spec.

    :param spec: A parsed agent / sub-agent spec.
    :returns: The canonical harness id, e.g. ``"claude-native"`` or
        ``"codex-native"``; falls back to ``executor.type`` when no
        ``harness`` is declared.
    """
    from omnigent.harness_aliases import canonicalize_harness

    harness = spec.executor.config.get("harness") or spec.executor.type
    return canonicalize_harness(harness) or harness


def _spec_config_flag_enabled(spec: AgentSpec, key: str) -> bool:
    """
    Read a boolean-ish ``executor.config`` flag, tolerating string coercion.

    The spec parser stringifies every ``executor.config`` value (see
    ``omnigent/spec/parser.py`` — ``{str(k): str(v) ...}``), so a YAML
    ``yolo: true`` arrives here as the string ``"True"``. A naive
    ``bool(value)`` is wrong: ``bool("False")`` is ``True``. This compares
    against the truthy spellings explicitly so only an intentional
    ``true`` / ``True`` enables the flag.

    :param spec: A parsed sub-agent spec.
    :param key: The ``executor.config`` key to read, e.g. ``"yolo"``.
    :returns: ``True`` only when the value is the boolean ``True`` or the
        string ``"true"`` (case-insensitive); ``False`` otherwise
        (including when the key is absent).
    """
    value = spec.executor.config.get(key)
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


def _derive_terminal_launch_args_from_spec(sub_spec: AgentSpec) -> list[str] | None:
    """
    Derive native-terminal YOLO pass-through args from a trusted sub-spec.

    polly's native workers (claude-native / codex-native) launch in a
    headless pane where no human can answer an ApprovalCard, so every
    Edit/Write/Bash that prompts stalls the worker. This translates a
    worker bundle's declared full-bypass intent into the per-session
    ``terminal_launch_args`` the runner already appends to the claude /
    codex argv:

    - claude-native + ``executor.config.permission_mode`` set ->
      ``["--permission-mode", "<value>"]``. The value is passed through
      verbatim so non-YOLO modes (``acceptEdits``, ``plan``, ...) work too;
      YOLO uses ``bypassPermissions``.
    - codex-native + ``executor.config.yolo`` truthy ->
      ``["--dangerously-bypass-approvals-and-sandbox"]``.

    Only the two native harnesses are translated; for any other harness
    (e.g. ``claude-sdk``, whose bypass is set via the SDK ``permissionMode``
    spawn env, not a terminal flag) this returns ``None`` so no terminal
    args are set. ``None`` is also returned when the relevant field is
    absent / falsey.

    :param sub_spec: The trusted child sub-agent spec, resolved from the
        server-loaded parent bundle via :func:`_resolve_subagent_spec`.
    :returns: A flat CLI-arg list to store as the child session's
        ``terminal_launch_args``, or ``None`` when nothing should be set.
    :raises ValueError: If a spec-derived argument violates the same
        bounds enforced for request-supplied ``terminal_launch_args``.
    """
    harness = _spec_harness(sub_spec)
    if harness == _CLAUDE_NATIVE_HARNESS:
        permission_mode = sub_spec.executor.config.get("permission_mode")
        if permission_mode:
            return _validate_terminal_launch_args(["--permission-mode", str(permission_mode)])
        return None
    if harness == _CODEX_NATIVE_HARNESS:
        if _spec_config_flag_enabled(sub_spec, "yolo"):
            return _validate_terminal_launch_args(["--dangerously-bypass-approvals-and-sandbox"])
        return None
    return None


def _native_subagent_wrapper_labels_from_spec(sub_spec: AgentSpec) -> dict[str, str]:
    """
    Resolve terminal-first wrapper labels from an already-loaded sub-spec.

    :param sub_spec: Trusted child sub-agent spec resolved from the
        parent bundle.
    :returns: ``{wrapper_key: value, ui_key: "terminal"}`` for a native
        sub-agent, or ``{}`` when the sub-agent is not native.
    """
    harness = _spec_harness(sub_spec)
    if harness == _CLAUDE_NATIVE_HARNESS:
        return {
            _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
            _CLAUDE_NATIVE_UI_LABEL_KEY: _CLAUDE_NATIVE_UI_LABEL_VALUE,
        }
    if harness == _CODEX_NATIVE_HARNESS:
        return {
            _CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_WRAPPER_LABEL_VALUE,
            _CLAUDE_NATIVE_UI_LABEL_KEY: _CLAUDE_NATIVE_UI_LABEL_VALUE,
        }
    return {}


def _native_subagent_wrapper_labels(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> dict[str, str]:
    """
    Resolve the terminal-first wrapper labels for a native-harness sub-agent.

    A sub-agent dispatched via ``sys_session_send`` whose own spec uses a
    native terminal harness (``claude-native`` / ``codex-native``) must
    render with the Chat/Terminal pill in the web UI, exactly like a
    top-level ``claude-native-ui`` / ``codex-native-ui`` wrapper session.
    The pill is gated on the conversation's ``omnigent.wrapper`` +
    ``omnigent.ui`` labels (see ``ap-web`` ``TerminalFirstContext``), but
    the sub-agent create path never stamps them. This resolves the child
    sub-agent's spec from the parent bundle and returns the labels to stamp,
    or an empty dict when the sub-agent is not native (e.g. ``claude-sdk``).

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns an empty dict).
    :returns: ``{wrapper_key: value, ui_key: "terminal"}`` for a native
        sub-agent, or ``{}`` when not native / not resolvable.
    """
    sub_spec = _resolve_subagent_spec(
        agent=agent,
        sub_agent_name=sub_agent_name,
        agent_cache=agent_cache,
    )
    if sub_spec is None:
        return {}
    return _native_subagent_wrapper_labels_from_spec(sub_spec)


def _reject_reserved_cost_control_label_seed(labels: dict[str, str]) -> None:
    """
    Reject a session-create body that seeds policy-owned labels.

    ``cost_control.*`` is the cost advisor's telemetry namespace and its
    only legitimate writer is the session's bound runner — which cannot
    exist yet at create time, so a seed is always a forgery.

    :param labels: The client-supplied initial labels, e.g.
        ``{"team": "ml"}``.
    :raises OmnigentError: 400 when any ``cost_control.*`` key is
        present.
    """
    reserved = reserved_cost_control_keys(labels)
    if reserved:
        raise OmnigentError(
            f"labels {', '.join(repr(key) for key in reserved)} "
            f"are in the policy-owned {COST_CONTROL_LABEL_NAMESPACE}* "
            "namespace and cannot be set at session creation",
            code=ErrorCode.INVALID_INPUT,
        )


def _require_cost_control_label_authority(
    *,
    reserved_keys: Sequence[str],
    tunnel_token: str | None,
    bound_runner_id: str | None,
    allowed_tunnel_tokens: frozenset[str] | None,
    multi_user: bool,
) -> None:
    """
    Authorize a label write touching the policy-owned ``cost_control.*`` keys.

    These are the cost advisor's telemetry labels, so ordinary session
    editors must not set them via PATCH; the advisor's persist proves
    itself with the runner tunnel binding token (allow-listed, or bound
    to this session's runner id — the tunnel route's trust model).
    Single-user servers skip the check: loopback runners may register
    under stable ids unrelated to any token, and there is no second
    identity to forge against.

    :param reserved_keys: The ``cost_control.*`` keys the request tries
        to write, e.g. ``("cost_control.plan",)``. Quoted in the error.
    :param tunnel_token: Value of the ``X-Omnigent-Runner-Tunnel-Token``
        request header, or ``None`` when absent.
    :param bound_runner_id: The session's current ``runner_id``, or
        ``None`` when no runner is bound.
    :param allowed_tunnel_tokens: The server's tunnel-token allow-list,
        or ``None`` when not configured.
    :param multi_user: ``True`` when the server enforces per-user
        permissions (a permission store is configured).
    :raises OmnigentError: 403 when the caller presents no acceptable
        runner proof on a multi-user server.
    """
    if not multi_user:
        return
    keys = ", ".join(repr(key) for key in reserved_keys)
    token = (tunnel_token or "").strip()
    if token:
        if allowed_tunnel_tokens is not None and token in allowed_tunnel_tokens:
            return
        if bound_runner_id is not None and token_bound_runner_id(token) == bound_runner_id:
            return
    raise OmnigentError(
        f"labels {keys} are in the policy-owned "
        f"{COST_CONTROL_LABEL_NAMESPACE}* namespace; only the session's "
        "bound runner may write them",
        code=ErrorCode.FORBIDDEN,
    )


async def _create_session_from_existing_agent(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    body: SessionCreateRequest,
    request: Request,
    agent_cache: AgentCache | None = None,
    user_id: str | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
) -> SessionResponse:
    """
    Create a session bound to an already-registered agent.

    This preserves the existing JSON ``POST /v1/sessions`` contract:
    clients that uploaded an agent separately still bind by durable
    ``agent_id`` and receive the full session snapshot.

    :param conversation_store: Store for conversation persistence.
    :param agent_store: Store for agent lookup by durable id.
    :param runner_router: Runner router used to validate any initial
        dispatch triggered by ``initial_items``.
    :param body: Validated JSON create request.
    :param agent_cache: Optional cache for loading parsed agent specs
        from bundles, used to populate ``llm_model`` and
        ``context_window`` in the response.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. Used to authorize parent-session
        and agent ownership and enforce runner
        ownership on parent-session inheritance.
    :param permission_store: Permission store for session-access
        checks. Required for authorization of
        ``parent_session_id`` and session-scoped ``agent_id``.
    :param liveness_lookup: Optional session-scoped liveness lookup
        to populate ``SessionResponse.runner_online``.
    :returns: The newly created session snapshot.
    :raises OmnigentError: 404 if no agent matches ``body.agent_id``;
        403/404 if ``parent_session_id`` or session-scoped ``agent_id``
        fails authorization.
    """
    _reject_reserved_cost_control_label_seed(body.labels)

    agent = await asyncio.to_thread(agent_store.get, body.agent_id)
    if agent is None:
        raise OmnigentError(
            f"Agent not found: {body.agent_id!r}",
            code=ErrorCode.NOT_FOUND,
        )

    # Session-scoped agents belong to a specific session.
    # The caller must have at least READ access to that owning
    # session — otherwise they can execute another user's private
    # agent by guessing the raw agent id.
    if agent.session_id is not None:
        await _require_access(
            user_id,
            agent.session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )

    # Authorize parent_session_id before inheriting anything.
    # The caller must own or have READ access to the parent session;
    # otherwise a forged parent link lets them inherit runner
    # bindings and establish a parent-child relationship with a
    # session they don't control.
    if body.parent_session_id is not None:
        await _require_access(
            user_id,
            body.parent_session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )

    # The persisted override reaches a native CLI as a ``--model`` argv
    # element at terminal launch, so reject shell-/flag-shaped values
    # before any row or worktree exists.
    model_override: str | None = None
    if body.model_override is not None:
        try:
            model_override = validate_model_override(body.model_override)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid model_override: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    # Validated before any row exists so a bad value never creates an
    # orphan session; None (unset) defers to the spec default.
    cost_control_mode_override = _validated_cost_control_mode_override(
        body.cost_control_mode_override
    )

    # Inherit runner affinity from the parent session so the child
    # is assigned to the same runner (sub-agent co-location).
    inherited_runner_id: str | None = None
    if body.parent_session_id is not None:
        parent_conv = conversation_store.get_conversation(body.parent_session_id)
        if parent_conv is not None:
            inherited_runner_id = parent_conv.runner_id
            # Defense-in-depth: don't inherit a runner the
            # caller doesn't own.
            if (
                inherited_runner_id is not None
                and user_id is not None
                and runner_router is not None
            ):
                runner_owner = runner_router.runner_owner(inherited_runner_id)
                if runner_owner is not None and runner_owner != user_id:
                    inherited_runner_id = None

    # Workspace validation: if the caller is binding to a host,
    # they must also pass a workspace, and the workspace must
    # satisfy the agent's os_env.cwd boundary on that host (per
    # designs/SESSION_WORKSPACE_SELECTION.md). Done before
    # create_conversation so a bad workspace never produces a row.
    # With git worktree creation, the validated path is the source
    # repo; the worktree it produces becomes the stored workspace.
    canonical_workspace: str | None = body.workspace
    if body.host_id is not None:
        canonical_workspace = await _validate_session_workspace(
            user_id=user_id,
            host_id=body.host_id,
            workspace=body.workspace,
            agent=agent,
            agent_cache=agent_cache,
            request=request,
        )

    # Git worktree creation (optional): the worktree becomes the
    # stored workspace and its branch is recorded.
    git_branch: str | None = None
    if body.git is not None:
        created_worktree = await _create_session_worktree(
            host_id=body.host_id,
            source_repo=canonical_workspace,
            git=body.git,
            request=request,
        )
        canonical_workspace = created_worktree.worktree_path
        git_branch = created_worktree.branch

    # Native-terminal pass-through args.
    #
    # Named sub-agent creates (``body.sub_agent_name`` set) DERIVE these
    # from the trusted, server-loaded sub-spec only — any caller-supplied
    # ``body.terminal_launch_args`` is ignored. This is the YOLO seam: a
    # native worker bundle declaring ``permission_mode`` / ``yolo: true``
    # gets the corresponding full-bypass flag so it can edit in a
    # headless pane, and a caller cannot inject launch wiring by
    # smuggling args through the spawn body.
    #
    # Sessions that resolve their own agent (top-level sessions and the
    # manual Add Agent child flow where ``sub_agent_name`` is null) keep
    # the validated body args (e.g. ``["--permission-mode",
    # "bypassPermissions"]`` from the web permission-mode selector). The
    # flat-list shape plus this bounds check is the security boundary;
    # mirrors the multipart create + PATCH paths.
    sub_spec: AgentSpec | None = None
    if body.sub_agent_name:
        sub_spec = _resolve_subagent_spec(
            agent=agent,
            sub_agent_name=body.sub_agent_name,
            agent_cache=agent_cache,
        )
        try:
            validated_launch_args = (
                _derive_terminal_launch_args_from_spec(sub_spec) if sub_spec is not None else None
            )
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args in sub-agent spec: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    else:
        try:
            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    try:
        conv = conversation_store.create_conversation(
            agent_id=agent.id,
            title=body.title,
            parent_conversation_id=body.parent_session_id,
            runner_id=inherited_runner_id,
            kind="sub_agent" if body.parent_session_id else "default",
            sub_agent_name=body.sub_agent_name,
            host_id=body.host_id,
            workspace=canonical_workspace,
            git_branch=git_branch,
            terminal_launch_args=validated_launch_args,
        )
    except Exception:
        # Broad catch is intentional: ANY create_conversation failure
        # (integrity error, name clash, ...) must trigger orphan-worktree
        # cleanup before the error propagates. We re-raise unchanged
        # below, so nothing is swallowed. git_branch is set only on
        # worktree success.
        if git_branch is not None and canonical_workspace is not None and body.host_id is not None:
            await _remove_session_worktree_best_effort(
                host_id=body.host_id,
                worktree_path=canonical_workspace,
                branch=git_branch,
                delete_branch=True,
                request=request,
                reason="create-rollback",
            )
        raise
    if model_override is not None or cost_control_mode_override is not None:
        # ``create_conversation`` has no override params; reuse the
        # PATCH path's store write before the runner reads the snapshot
        # (the first turn / terminal launch happens only after this
        # create returns and the caller posts a message event).
        updated_conv = await asyncio.to_thread(
            conversation_store.update_conversation,
            conv.id,
            model_override=model_override,
            cost_control_mode_override=cost_control_mode_override,
        )
        if updated_conv is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared while persisting session overrides",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = updated_conv
    # Set wrapper label at creation time if the agent is a native
    # terminal wrapper (claude-native or codex-native), so all messages
    # (including early ones sent before the runner connects) take
    # the native path and avoid double-persistence with the
    # transcript forwarder.
    if agent.name == "claude-native-ui":
        _cn_labels = dict(body.labels) if body.labels else {}
        _cn_labels[_CLAUDE_NATIVE_WRAPPER_LABEL_KEY] = _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE
        # Terminal-first rendering label, so an added Claude Code child
        # shows the terminal pane even though the dialog sends no labels.
        _cn_labels[_CLAUDE_NATIVE_UI_LABEL_KEY] = _CLAUDE_NATIVE_UI_LABEL_VALUE
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _cn_labels)
        conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
    elif agent.name == "codex-native-ui":
        _cx_labels = dict(body.labels) if body.labels else {}
        _cx_labels[_CLAUDE_NATIVE_WRAPPER_LABEL_KEY] = _CODEX_NATIVE_WRAPPER_LABEL_VALUE
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _cx_labels)
        conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
    elif (
        body.sub_agent_name
        and sub_spec is not None
        and (_sa_labels := _native_subagent_wrapper_labels_from_spec(sub_spec))
    ):
        # A native-harness sub-agent (claude-native / codex-native) must
        # render terminal-first with the Chat/Terminal pill, same as a
        # top-level wrapper session. Merge over any caller-supplied labels.
        _merged = dict(body.labels) if body.labels else {}
        _merged.update(_sa_labels)
        await asyncio.to_thread(conversation_store.set_labels, conv.id, _merged)
        conv = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
    elif body.labels:
        await asyncio.to_thread(conversation_store.set_labels, conv.id, body.labels)
    if body.initial_items:
        runner_client = await _get_runner_client(conv.id, runner_router)
        if runner_client is None:
            # No runner bound — persist initial items as history-only
            # seed via the conversation store. No execution fires; the
            # caller is responsible for binding a runner and posting a
            # follow-up event if they want the agent to react.
            # SessionEventInput carries no response_id; this is a
            # pre-execution history seed, so tag all items with a
            # synthetic ``"seed"`` response id. The runner overwrites
            # this on first turn via a normal append path.
            new_items = [
                NewConversationItem(
                    type=item.type,
                    response_id="seed",
                    data=item.data,
                    created_by=_attribution_user(user_id),
                )
                for item in body.initial_items
            ]
            await asyncio.to_thread(conversation_store.append, conv.id, new_items)
        else:
            await _ensure_runner_relay_ready(
                conv.id,
                conv.runner_id,
                runner_client,
                conversation_store,
            )
            for item in body.initial_items:
                await _forward_event_to_runner(
                    conv.id,
                    conv,
                    item,
                    conversation_store,
                    runner_client,
                    agent_name=agent.name,
                    created_by=_attribution_user(user_id),
                )
    # Re-read rather than reusing the local ``conv``: the label-only branch
    # above and ``_forward_event_to_runner`` can mutate the row after it was
    # built, so a fresh read is what keeps the create response current.
    return await _get_session_snapshot(
        conversation_store,
        conv.id,
        agent_store=agent_store,
        agent_cache=agent_cache,
        liveness_lookup=liveness_lookup,
    )


def _create_session_from_bundle(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    bundle_bytes: bytes,
    runner_id: str | None = None,
) -> CreatedSessionResponse:
    """
    Validate, store, and persist a bundled session request.

    Each upload creates a session-scoped agent row, even when a
    template agent with the same spec name already exists. Agent
    names are user-authored labels, not global content identities:
    reusing a template by name would make a fresh ``omnigent run
    <yaml>`` session execute whatever bundle that template currently
    points at, silently discarding the uploaded bundle and coupling
    unrelated users who chose the same name.

    :param conversation_store: Store that owns the atomic
        conversation-plus-agent transaction.
    :param artifact_store: Store for uploaded bundle bytes.
    :param metadata: Validated session metadata. When
        ``metadata.parent_session_id`` is set (already authorized by
        the caller), the session is created as a sub-agent
        child of that conversation.
    :param bundle_bytes: Raw uploaded ``.tar.gz`` agent bundle.
    :param runner_id: Optional runner binding inherited from the
        parent session (caller-resolved, ownership-checked),
        e.g. ``"runner_abc123"``. ``None`` leaves the session
        unbound.
    :returns: Response with the new session id.
    :raises OmnigentError: If bundle validation or agent insert
        integrity checks fail, or the parent session vanished
        between authorization and insert.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    # Enforce the policy-handler allowlist only on a shared /
    # multi-user server. On a trusted single-user/local server,
    # ``omnigent run`` uploads the operator's own bundle through this same
    # path, so custom handlers must keep working (the operator already has
    # code execution — the restriction would add no security there).
    spec = validate_agent_bundle(
        bundle_bytes,
        enforce_handler_allowlist=not local_single_user_enabled(),
    )
    assert spec.name is not None

    agent_id = generate_agent_id()
    agent_bundle_location = bundle_location(agent_id, bundle_bytes)
    try:
        artifact_store.put(agent_bundle_location, bundle_bytes)
    except Exception:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise
    return _persist_stored_session_bundle(
        conversation_store,
        artifact_store,
        metadata,
        agent_id=agent_id,
        agent_name=spec.name,
        agent_bundle_location=agent_bundle_location,
        agent_description=spec.description,
        runner_id=runner_id,
    )


def _persist_stored_session_bundle(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    *,
    agent_id: str,
    agent_name: str,
    agent_bundle_location: str,
    agent_description: str | None,
    runner_id: str | None = None,
) -> CreatedSessionResponse:
    """
    Persist database rows for a bundle already written to artifacts.

    :param conversation_store: Store that owns the atomic
        conversation-plus-agent transaction.
    :param artifact_store: Store for deleting the bundle on failure.
    :param metadata: Validated session metadata. A set
        ``parent_session_id`` creates the conversation as a
        sub-agent child of that session.
    :param agent_id: New agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded spec.
    :param agent_bundle_location: Artifact key for the stored bundle.
    :param agent_description: Optional description from the spec.
    :param runner_id: Optional runner binding inherited from the
        parent session, e.g. ``"runner_abc123"``.
    :returns: Response with the new session id.
    :raises OmnigentError: If the agent insert violates integrity
        checks or the parent session no longer exists.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    try:
        created = conversation_store.create_session_with_agent(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_bundle_location=agent_bundle_location,
            agent_description=agent_description,
            title=metadata.title,
            labels=metadata.labels,
            reasoning_effort=metadata.reasoning_effort,
            workspace=metadata.workspace,
            terminal_launch_args=metadata.terminal_launch_args,
            parent_conversation_id=metadata.parent_session_id,
            runner_id=runner_id,
        )
    except ConversationNotFoundError as exc:
        # Parent was authorized by the caller but vanished (deleted)
        # before the insert transaction ran.
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise OmnigentError(
            str(exc),
            code=ErrorCode.NOT_FOUND,
        ) from exc
    except IntegrityError as exc:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        # Expected integrity failures here are uniqueness collisions:
        # generated agent id, generated conversation id, or
        # agents.session_id. The route maps those to 409.
        raise OmnigentError(
            f"session agent write failed integrity checks: {exc.orig}",
            code=ErrorCode.ALREADY_EXISTS,
        ) from exc
    except SQLAlchemyError:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise
    return CreatedSessionResponse(
        session_id=created.conversation.id,
        agent_id=agent_id,
        agent_name=agent_name,
    )


def _delete_stored_session_bundle_after_failure(
    artifact_store: ArtifactStore,
    agent_bundle_location: str,
) -> None:
    """
    Delete an uploaded bundle after database creation fails.

    Cleanup failures are logged but suppressed so the original
    exception remains the error seen by callers.

    :param artifact_store: Store that contains the uploaded bundle.
    :param agent_bundle_location: Artifact key to delete, e.g.
        ``"ag_abc123/a1b2c3d4"``.
    :returns: None.
    """
    try:
        artifact_store.delete(agent_bundle_location)
    except Exception:  # noqa: BLE001 - cleanup must not mask the original failure.
        _logger.warning(
            "Failed to delete uploaded session bundle %s after rollback",
            agent_bundle_location,
            exc_info=True,
        )


async def _authorize_bundled_parent_and_inherit_runner(
    parent_session_id: str,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> str | None:
    """
    Authorize a bundled create's parent link and resolve runner affinity.

    The caller must have READ access to the parent session
    before inheriting anything, mirroring the JSON create path —
    without this, a forged parent link lets the caller inherit runner
    bindings and parent a session they don't control. On success the
    parent's runner binding is inherited (sub-agent co-location),
    subject to a defense-in-depth ownership check: a runner the
    caller doesn't own is not inherited.

    :param parent_session_id: The requested parent session id,
        e.g. ``"conv_abc123"``.
    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``.
    :param permission_store: Permission store for the access
        check; ``None`` in single-user / no-auth mode.
    :param conversation_store: Store for the parent-conversation read.
    :param runner_router: Router for the runner-ownership check;
        ``None`` skips it.
    :returns: The inherited runner id, or ``None`` when the parent has
        no runner binding or ownership disallows inheritance.
    :raises OmnigentError: 403/404 when the caller may not access the
        parent session.
    """
    await _require_access(
        user_id,
        parent_session_id,
        LEVEL_READ,
        permission_store,
        conversation_store,
    )
    parent_conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        parent_session_id,
    )
    if parent_conv is None:
        return None
    inherited_runner_id = parent_conv.runner_id
    if inherited_runner_id is not None and user_id is not None and runner_router is not None:
        runner_owner = runner_router.runner_owner(inherited_runner_id)
        if runner_owner is not None and runner_owner != user_id:
            return None
    return inherited_runner_id


async def _notify_runner_of_bundled_child(
    session_id: str,
    agent_id: str,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Notify the inherited runner that a bundled child session exists.

    Lets the runner initialize per-session state (inbox queue,
    agent-id cache) before the first forwarded event, mirroring the
    JSON create path's post-create notify. Failures are logged and
    swallowed — the notify is additive and must not fail the create.

    :param session_id: The new child session id, e.g. ``"conv_abc123"``.
    :param agent_id: The child's session-scoped agent id,
        e.g. ``"ag_abc123"``.
    :param runner_router: Router used to resolve the bound runner's
        client; ``None`` falls back to the in-process runner.
    :returns: None.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            "/v1/sessions",
            json={
                "session_id": session_id,
                "agent_id": agent_id,
                "sub_agent_name": None,
            },
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "Failed to notify runner about bundled session %s",
            session_id,
            exc_info=True,
        )


def _registered_runner_id(
    runner_router: RunnerRouter | None,
    raw_runner_id: str,
    *,
    user_id: str | None = None,
) -> str:
    """
    Validate a runner id from ``PATCH /v1/sessions/{id}``.

    When ``user_id`` is provided the function also enforces runner
    ownership: only the user who established the tunnel may
    bind sessions to that runner.

    :param runner_router: Router backed by the live tunnel registry.
        ``None`` means this server cannot bind runners.
    :param raw_runner_id: Runner id from the request body, e.g.
        ``"runner_abc123"``.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. ``None`` skips the ownership
        check (single-user / no-auth mode).
    :returns: Trimmed registered runner id.
    :raises OmnigentError: If the id is empty, the router is
        unavailable, the runner is not registered, or the caller
        does not own the runner.
    """
    runner_id = raw_runner_id.strip()
    if not runner_id:
        raise OmnigentError(
            "runner_id must not be empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if runner_router is None:
        raise OmnigentError(
            "runner router is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if not runner_router.runner_is_online(runner_id):
        raise OmnigentError(
            f"runner {runner_id!r} is not registered",
            code=ErrorCode.INVALID_INPUT,
        )
    # Enforce runner ownership. A caller must own the runner
    # they are trying to bind to a session.
    if user_id is not None:
        runner_owner = runner_router.runner_owner(runner_id)
        if runner_owner is not None and runner_owner != user_id:
            raise OmnigentError(
                f"runner {runner_id!r} is not owned by the requesting user",
                code=ErrorCode.FORBIDDEN,
            )
    return runner_id


_CHILD_PREVIEW_LIMIT = 150


def _latest_message_preview(
    items: list[ConversationItem],
    limit_chars: int = _CHILD_PREVIEW_LIMIT,
) -> str | None:
    """
    Return a single-line text preview from newest-first message items.

    Powers the sub-agent rail row's status line so the user can see what
    the child is saying without opening it. The caller supplies a
    batched newest-first message list for one child; this function joins
    ``input_text`` / ``output_text`` blocks from the first non-meta
    message with text, collapses whitespace, and truncates to
    ``limit_chars``. Hidden meta messages carry durable runner context
    and must never be shown as user-facing previews.

    :param items: Newest-first message items for one conversation.
    :param limit_chars: Max preview length in characters,
        e.g. ``150``.
    :returns: Truncated single-line preview text, e.g.
        ``"I'll search the codebase for references…"``, or ``None``.
    """
    for item in items:
        if not isinstance(item.data, MessageData) or item.data.is_meta:
            continue
        parts: list[str] = []
        for block in item.data.content:
            block_type = block.get("type")
            text = block.get("text")
            if block_type in ("input_text", "output_text") and isinstance(text, str):
                parts.append(text)
        collapsed = " ".join(" ".join(parts).split())
        if not collapsed:
            continue
        if len(collapsed) <= limit_chars:
            return collapsed
        # Trim to one char less than the limit so the trailing ellipsis
        # keeps the field at ``limit_chars`` total.
        return collapsed[: max(0, limit_chars - 1)].rstrip() + "…"
    return None


# Title prefix marking a child session that a user added from the Web UI
# "Add agent" picker (vs. an LLM-spawned sub-agent). Such titles take the
# 3-segment form "ui:<agent_name>:<user_label>"; the leading sentinel keeps
# them from colliding with the 2-segment "<sub_agent_name>:<session_name>"
# titles that sys_session_send writes. The spec validator rejects sub-agent
# names equal to this sentinel (see _validate_agent_names) to preserve the
# disambiguation.
_UI_ADDED_AGENT_TITLE_PREFIX = "ui"


def _child_session_summary_from_conversation(
    conv: Conversation,
    parent_session_id: str,
    last_message_preview: str | None,
) -> ChildSessionSummary:
    """
    Build a :class:`ChildSessionSummary` from a child conversation.

    Parses the canonical sub-agent title format
    ``"{agent_type}:{session_name}"`` written by
    :func:`omnigent.tools.builtins.spawn._spawn_one`, plus the
    3-segment ``"ui:{agent_name}:{user_label}"`` form written by the
    Web UI "Add agent" flow (surfaced as ``tool={agent_name}`` and
    ``session_name={user_label}``). Tolerates malformed/legacy rows:
    if the title is ``None`` or has no colon, ``tool`` falls back to
    the raw title and ``session_name`` is ``None`` — the row is still
    surfaced so debug views can investigate.

    ``busy`` is derived from the relay-fed ``_session_status_cache``
    (the tasks table has been removed). ``agent_id`` and ``agent_name``
    are read from the conversation row directly.

    :param conv: A child :class:`Conversation` row
        (``kind="sub_agent"``) from
        :meth:`ConversationStore.list_conversations`.
    :param parent_session_id: The parent session id from the
        route, e.g. ``"conv_parent987"``. Passed in rather than
        re-reading from ``conv.parent_conversation_id`` to keep
        the helper indifferent to legacy rows where the FK might
        be missing.
    :param last_message_preview: Preview text derived from a batched
        child-message lookup, or ``None`` when no visible message exists.
    :returns: A populated :class:`ChildSessionSummary`.
    """
    display_title = title_without_closed_marker(conv.title)
    labels = labels_with_closed_status(conv.labels, conv.title)
    tool: str | None
    session_name: str | None
    if _is_codex_native_subagent(conv):
        # Codex-native child: surface the Codex-assigned nickname/role as
        # ``tool`` and the raw thread id as ``session_name`` for correlation.
        tool = _codex_subagent_display_tool(labels)
        session_name = labels.get(_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY)
    elif display_title and ":" in display_title:
        head, _, tail = display_title.partition(":")
        if head == _UI_ADDED_AGENT_TITLE_PREFIX and ":" in tail:
            # User-added agent: "ui:<agent_name>:<user_label>". Surface the
            # bound agent as ``tool`` and the user's label as ``session_name``
            # so the Agents rail renders it like any other child row.
            agent_name, _, user_label = tail.partition(":")
            tool = agent_name
            session_name = user_label
        else:
            tool = head
            session_name = tail
    else:
        tool = display_title or None
        session_name = None

    # Derive busy from the relay-fed cache; tasks table is gone.
    cached_status = _session_status_cache.get(conv.id)
    if cached_status in ("running", "waiting"):
        busy = True
    else:
        busy = False

    # For Codex children, fall back to the prompt label as preview when the
    # real transcript has not arrived yet — avoids synthesizing a user message
    # just so the rail has something to show.
    if last_message_preview is None and _is_codex_native_subagent(conv):
        raw_prompt = labels.get(_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY)
        if raw_prompt:
            collapsed = " ".join(raw_prompt.split())
            last_message_preview = collapsed[:_CHILD_PREVIEW_LIMIT] or None

    return ChildSessionSummary(
        id=conv.id,
        parent_session_id=parent_session_id,
        title=display_title,
        tool=tool,
        session_name=session_name,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        # agent_id comes from the conversation row; agent_name and task_id
        # are no longer available from the (removed) tasks table.
        agent_id=conv.agent_id,
        agent_name=None,
        current_task_id=None,
        current_task_status=None,
        busy=busy,
        labels=labels,
        last_message_preview=last_message_preview,
        # Surface the sub-agent's parked-elicitation count from the same
        # in-memory index that feeds the sidebar badge, so the Agents
        # rail can flag a child that's awaiting user input.
        pending_elicitations_count=pending_elicitations.count_for(conv.id),
    )


async def _child_session_summaries_from_conversations(
    children: list[Conversation],
    parent_session_id: str,
    conv_store: ConversationStore,
) -> list[ChildSessionSummary]:
    """
    Build child summaries with one batched message-preview lookup.

    ``ChildSessionSummary.last_message_preview`` needs the latest visible
    message per child. Loading those by calling ``list_items`` once per
    child blocks the event loop and creates N+1 database traffic. This
    helper reads newest message items for all child ids in a worker
    thread, computes previews in memory, then builds summaries without
    further store access.

    :param children: Child conversation rows from
        ``list_conversations(kind="sub_agent")``.
    :param parent_session_id: Parent session id, e.g. ``"conv_parent987"``.
    :param conv_store: Conversation store used for the batched message read.
    :returns: One :class:`ChildSessionSummary` per input child, preserving
        input order.
    """
    if not children:
        return []
    child_ids = [child.id for child in children]
    message_items_by_child = await asyncio.to_thread(
        conv_store.list_latest_message_items_for_conversations,
        child_ids,
        10,
    )
    previews = {
        child_id: _latest_message_preview(message_items)
        for child_id, message_items in message_items_by_child.items()
    }
    return [
        _child_session_summary_from_conversation(
            child,
            parent_session_id,
            previews.get(child.id),
        )
        for child in children
    ]


# ── MCP proxy helpers ───────────────────────────────────────────────────────
#
# These module-level functions implement the JSON-RPC 2.0 handlers for
# ``POST /v1/sessions/{session_id}/mcp``.  They live outside the router
# factory so the factory closure stays compact.


def _mcp_ok_response(rpc_id: int | str | None, result: dict[str, Any]) -> Response:
    """
    Wrap *result* in a JSON-RPC 2.0 success response.

    :param rpc_id: The JSON-RPC request id (may be int, str, or ``None``
        for notifications), e.g. ``1``.
    :param result: The JSON-serialisable result payload, e.g.
        ``{"tools": [...]}``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 envelope.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    return Response(content=body, media_type="application/json")


def _mcp_error_response(
    rpc_id: int | str | None,
    code: int,
    message: str,
) -> Response:
    """
    Wrap an error in a JSON-RPC 2.0 error response.

    :param rpc_id: The JSON-RPC request id. Use ``None`` when the id
        could not be parsed, e.g. ``None``.
    :param code: JSON-RPC error code, e.g. ``-32601`` (method not found)
        or ``-32000`` (application error).
    :param message: Human-readable error description,
        e.g. ``"Method not found: 'unsupported/method'"``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 error envelope.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }
    )
    return Response(content=body, media_type="application/json")


def _mcp_input_required_response(
    rpc_id: int | str | None,
    elicitation_id: str,
    message: str,
    request_state: str,
    session_id: str | None = None,
) -> Response:
    """
    Return an MCP ``InputRequiredResult`` asking the runner to collect
    user approval before retrying the tool call.

    Follows the Multi Round-Trip Requests (MRTR) spec:
    ``https://modelcontextprotocol.io/specification/draft/basic/utilities/mrtr``.
    The ``elicitation_id`` is used as the key in ``inputRequests`` so the
    runner can identify the approval Future without inspecting the opaque
    ``requestState``. When URL-mode is active and ``session_id`` is
    known, adds ``mode``/``url`` to params.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param elicitation_id: Server-minted elicitation id used both as the
        ``inputRequests`` key and inside the opaque ``requestState``,
        e.g. ``"elicit_abc123"``.
    :param message: Human-readable prompt shown to the user,
        e.g. ``"Allow tool sys_os_shell?"``.
    :param request_state: Opaque state blob the client echoes on retry.
        Contains the ``elicitation_id`` and ``session_id`` so the server
        can verify authenticity on retry without server-side storage.
    :param session_id: Session/conversation id for constructing the
        approval page URL, e.g. ``"conv_abc123"``. ``None`` omits the
        URL (form mode).
    :returns: A :class:`Response` carrying the JSON-RPC 2.0
        ``InputRequiredResult`` envelope.
    """

    params: dict[str, Any] = {
        "message": message,
        "requestedSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
        },
    }
    if session_id is not None and _ELICITATION_MODE == "url":
        params["mode"] = "url"
        params["url"] = f"/approve/{session_id}/{elicitation_id}"
    else:
        params["mode"] = "form"

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    elicitation_id: {
                        "method": "elicitation/create",
                        "params": params,
                    }
                },
                "requestState": request_state,
            },
        }
    )
    return Response(content=body, media_type="application/json")


async def _handle_mcp_tools_list(
    rpc_id: int | str | None,
    session_id: str,
    runner_router: RunnerRouter | None,
) -> Response:
    """
    Handle a ``tools/list`` JSON-RPC request for the MCP proxy endpoint.

    Delegates execution to the runner's ``POST
    /v1/sessions/{id}/mcp/execute`` endpoint so that stdio MCP
    subprocesses spawn on the runner's machine (correct ``cwd``,
    env, and tooling). The Omnigent server's role here is routing only —
    policy evaluation happens in ``tools/call``.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id whose agent's tools to list,
        e.g. ``"conv_abc123"``.
    :param runner_router: Router used to get an httpx client pointed
        at the session's runner. ``None`` returns an error.
    :returns: A JSON-RPC 2.0 ``tools/list`` result response, or an
        error response when the runner is unavailable.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    _logger.debug("MCP tools/list: delegating to runner execute for session=%r", session_id)
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={"method": "tools/list", "params": {}},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in data:
        err = data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    result = data.get("result", {})
    # schemas are already in OpenAI function-tool format from RunnerMcpManager;
    # convert back to MCP inputSchema format for the tools/list response since
    # ProxyMcpManager on the runner expects MCP-shaped tools/list output.
    schemas: list[dict[str, Any]] = result.get("schemas", [])
    tools = []
    for schema in schemas:
        # schema shape: {"type": "function", "name": "srv__tool",
        #                "description": "...", "parameters": {...}}
        tools.append(
            {
                "name": schema.get("name", ""),
                "description": schema.get("description", ""),
                "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
            }
        )

    failures: dict[str, str] = result.get("failures", {})
    for srv, msg in failures.items():
        _logger.warning("runner MCP server %r unavailable: %s", srv, msg)

    _logger.debug(
        "MCP tools/list: session=%r returning %d tools, %d failures",
        session_id,
        len(tools),
        len(failures),
    )
    return _mcp_ok_response(rpc_id, {"tools": tools})


async def _handle_mcp_tools_call(
    rpc_id: int | str | None,
    session_id: str,
    params: dict[str, Any],
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
    request: Request | None = None,
) -> Response:
    """
    Handle a ``tools/call`` JSON-RPC request for the MCP proxy endpoint.

    Steps:

    1. Validate the tool name (namespaced like ``github__search`` for MCP
       tools, or bare like ``sys_os_read`` for runner-local tools).
    2. Load session → agent → spec for policy evaluation.
    3. On first call: evaluate TOOL_CALL policy.  On DENY, return error.
       On ASK, emit a ``response.elicitation_request`` SSE event and
       return an MCP ``InputRequiredResult`` so the runner can park for
       user approval and retry per the MRTR spec.
    4. On retry (``requestState`` present in ``params``): verify the
       state, check the user's ``inputResponses``, and proceed if
       approved.
    5. Delegate execution to the runner's ``POST
       /v1/sessions/{id}/mcp/execute`` endpoint via the WS tunnel so
       that stdio MCP subprocesses and runner-local tools execute on the
       runner's machine (correct ``cwd``, environment, and tooling).
    6. Evaluate the TOOL_RESULT policy phase on the returned output;
       replace with a redaction notice on DENY.
    7. Return the result in MCP ``content`` format.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id, e.g. ``"conv_abc123"``.
    :param params: The JSON-RPC ``params`` object.  On first call,
        contains ``"name"`` and ``"arguments"``.  On retry, also
        contains ``"requestState"`` (opaque blob from the server) and
        ``"inputResponses"`` (user's approval decision), e.g.
        ``{"name": "sys_os_shell", "arguments": {}, "requestState": "...",
        "inputResponses": {"elicit_abc": {"action": "accept"}}}``.
    :param conversation_store: Store for session and label state.
    :param agent_store: Store for agent lookup.
    :param runner_router: Router used to get a tunneled client pointed at
        the session's runner. ``None`` returns an error response.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: A JSON-RPC 2.0 response carrying the tool result as MCP
        ``content`` blocks, an ``InputRequiredResult`` on ASK, or an
        error response when the call is denied, the runner is
        unavailable, or the underlying MCP call fails.
    """

    namespaced_name = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments") or {}
    request_state_str: str | None = params.get("requestState")
    input_responses: dict[str, Any] = params.get("inputResponses") or {}
    is_retry = request_state_str is not None

    _logger.debug(
        "MCP tools/call: session=%r tool=%r is_retry=%r",
        session_id,
        namespaced_name,
        is_retry,
    )

    if not namespaced_name:
        return _mcp_error_response(rpc_id, -32000, "Missing tool name in tools/call params")

    # Session → agent → spec (needed for policy evaluation on both paths).
    # All three reads — conversation row, agent row, and the cold-cache
    # bundle fetch + spec parse — are blocking IO. Run them off the event
    # loop so an MCP tool call doesn't stall the single-worker server and
    # serialize concurrent requests behind it.
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None or conv.agent_id is None:
        return _mcp_error_response(
            rpc_id, -32000, f"Session not found or has no agent: {session_id!r}"
        )

    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return _mcp_error_response(rpc_id, -32000, f"Agent not found: {conv.agent_id!r}")

    # Build the policy engine once — used for both TOOL_CALL (first call
    # only) and TOOL_RESULT (both paths). Engine construction reads
    # session-policy specs and labels from the DB, so keep it off-loop too.
    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store
    )

    if is_retry:
        # ── Retry path: user has responded to the elicitation ────────
        # Verify the opaque requestState.
        try:
            state = json.loads(request_state_str)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return _mcp_error_response(rpc_id, -32000, "Invalid requestState: not valid JSON")
        if state.get("session_id") != session_id:
            # Reject cross-session replay.
            return _mcp_error_response(rpc_id, -32000, "requestState session mismatch")

        # ── Fail-closed: re-evaluate TOOL_CALL policy on retry ──────
        # The original retry path trusted the caller-supplied
        # requestState + inputResponses as proof that "policy ran and
        # the user approved." Because requestState is unsigned JSON
        # and inputResponses is caller-controlled, a forged retry
        # could bypass DENY/ASK gates entirely. Re-evaluating the
        # policy on every retry closes this vector: a DENY'd tool
        # stays denied regardless of what the request body claims.
        retry_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        retry_result = await engine.evaluate(retry_ctx)

        _logger.debug(
            "MCP tools/call retry TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            retry_result.action,
            retry_result.reason,
        )

        if retry_result.action == PolicyAction.DENY:
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {retry_result.reason or 'no reason given'}",
            )

        if retry_result.action == PolicyAction.ASK:
            # Policy still requires approval — verify the elicitation
            # was genuinely issued by the server (present in the
            # server-side pending map) and that the user approved it.
            elicitation_id_from_state: str = state.get("elicitation_id", "")
            if elicitation_id_from_state not in _pending_policy_ask_writes:
                # The elicitation_id is not in the server-side map.
                # Either it was forged, already consumed, or expired.
                # Check inputResponses: if the caller claims approval
                # for an unrecognised elicitation, reject it.
                approval: dict[str, Any] = input_responses.get(elicitation_id_from_state) or {}
                if approval.get("action") == "accept":
                    # Claimed approval for an elicitation the server
                    # never issued or already consumed — reject.
                    return _mcp_error_response(
                        rpc_id,
                        -32000,
                        "Elicitation not found or already resolved",
                    )
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            approval = input_responses.get(elicitation_id_from_state) or {}
            if approval.get("action") != "accept":
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            # Recover any policy-transformed args that were serialised into
            # requestState on the initial ASK — the client re-sends the
            # original arguments which we must not use when a transform was set.
            if state.get("transformed_arguments") is not None:
                arguments = state["transformed_arguments"]
            # Apply the deciding policy's deferred writes now that the
            # user approved (POLICIES.md §7.2: only on accept).
            _pending = _pending_policy_ask_writes.pop(elicitation_id_from_state, None)
            if _pending is not None:
                if _pending.set_labels:
                    await asyncio.to_thread(engine.apply_label_writes, _pending.set_labels)
                if _pending.state_updates:
                    await asyncio.to_thread(engine.apply_state_updates, _pending.state_updates)
        else:
            # ALLOW — policy no longer requires approval (e.g. label
            # state changed between the original ASK and this retry).
            # Recover transformed args if present, then fall through.
            if state.get("transformed_arguments") is not None:
                arguments = state["transformed_arguments"]
        # Fall through to execution.
    else:
        # ── First call: evaluate TOOL_CALL policy ────────────────────
        call_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        call_result = await engine.evaluate(call_ctx)

        _logger.debug(
            "MCP tools/call TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            call_result.action,
            call_result.reason,
        )

        if call_result.action == PolicyAction.DENY:
            if call_result.set_labels:
                await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {call_result.reason or 'no reason given'}",
            )

        if call_result.action == PolicyAction.ASK:
            # Emit elicitation SSE event (for REPL approval UI) and return
            # InputRequiredResult per the MCP MRTR spec so the runner can
            # park on the approval Future and retry when the user decides.
            elicitation_id = await _register_policy_elicitation(
                session_id,
                call_result,
                json.dumps(arguments)[:1024],
                conversation_store,
            )
            # Defer the deciding policy's writes (label mutations AND
            # state_updates such as a cost-budget checkpoint) to the
            # approved retry path — POLICIES.md §7.2 lands them only on
            # accept. The approval handler at the top of this function
            # already applies both via ``apply_label_writes`` and
            # ``apply_state_updates``. Mirrors the relay path pattern.
            # Always store an entry even when there are no deferred
            # writes — the retry path checks the pending map to verify
            # the elicitation was genuinely issued by the server. A
            # missing entry causes "Elicitation not found or already
            # resolved" on the retry.
            _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
                state_updates=call_result.state_updates,
                set_labels=call_result.set_labels,
                from_mcp=True,
            )
            request_state_payload: dict[str, Any] = {
                "elicitation_id": elicitation_id,
                "session_id": session_id,
            }
            # If the policy returned transformed args alongside ASK (e.g.
            # PII-redacted arguments), persist them so the retry path can
            # apply them after the user approves — the client re-sends the
            # original arguments, which would silently bypass the transform.
            if call_result.data is not None:
                request_state_payload["transformed_arguments"] = call_result.data
            request_state = json.dumps(request_state_payload)
            return _mcp_input_required_response(
                rpc_id,
                elicitation_id=elicitation_id,
                message=call_result.reason or "Approval required to run this tool",
                request_state=request_state,
                session_id=session_id,
            )
        # ALLOW — apply labels now that we know the action is not ASK.
        if call_result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
        # If the policy returned transformed arguments (e.g.
        # PII-redacted args), use them instead of the originals.
        if call_result.data is not None:
            arguments = call_result.data

    # ── Execute on the runner via WS tunnel ──────────────────────────
    # The runner owns stdio subprocess spawning (correct machine, cwd,
    # and env). We call its /mcp/execute endpoint through the same WS
    # tunnel the runner already opened to the Omnigent server at startup.
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    try:
        from omnigent.runner.tool_dispatch import MCP_PROXY_FORWARD_TIMEOUT_S

        exec_resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": namespaced_name, "arguments": arguments},
            },
            # ``sys_session_send`` returns a running handle immediately; this
            # timeout now protects ordinary runner proxy hangs.
            timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
        )
        exec_resp.raise_for_status()
        exec_data = exec_resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in exec_data:
        err = exec_data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    # ── MRTR: external MCP server needs user input ───────────────
    # The runner returns ``{"result": {"input_required": {...}}}``
    # when the external MCP server sent an ``InputRequiredResult``.
    # Surface each elicitation to the user via the existing SSE
    # infrastructure, gather responses, then retry on the runner.
    mcp_input_required = exec_data.get("result", {}).get("input_required")
    if mcp_input_required is not None:
        if request is None:
            return _mcp_error_response(
                rpc_id, -32000, "MCP server requires elicitation but no request context available"
            )
        input_requests: dict[str, Any] = mcp_input_required.get("inputRequests") or {}
        mcp_request_state: str = mcp_input_required.get("requestState", "")

        # Gather user responses for each inputRequest.
        input_responses: dict[str, Any] = {}
        for eid, req_entry in input_requests.items():
            req_params = req_entry.get("params", {}) if isinstance(req_entry, dict) else {}
            elicit_params = ElicitationRequestParams(
                mode=req_params.get("mode", "form"),
                message=req_params.get("message", "Approval required"),
                requestedSchema=req_params.get("requestedSchema"),
            )
            elicit_result = await _publish_and_wait_for_harness_elicitation(
                request,
                session_id=session_id,
                params=elicit_params,
                timeout_s=300.0,
                conversation_store=conversation_store,
            )
            if elicit_result is None:
                input_responses[eid] = {"action": "decline"}
            else:
                resp_entry: dict[str, Any] = {"action": elicit_result.action}
                if elicit_result.content is not None:
                    resp_entry["content"] = elicit_result.content
                input_responses[eid] = resp_entry

        # Retry on the runner with the user's inputResponses.
        try:
            retry_resp = await runner_client.post(
                f"/v1/sessions/{session_id}/mcp/execute",
                json={
                    "method": "tools/call",
                    "params": {
                        "name": namespaced_name,
                        "arguments": arguments,
                        "inputResponses": input_responses,
                        "requestState": mcp_request_state,
                    },
                },
                timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
            )
            retry_resp.raise_for_status()
            exec_data = retry_resp.json()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Runner MCP retry failed: %s", exc, exc_info=True)
            return _mcp_error_response(rpc_id, -32000, "Runner MCP retry failed.")
        if "error" in exec_data:
            err = exec_data["error"]
            return _mcp_error_response(
                rpc_id, err.get("code", -32000), err.get("message", "unknown error")
            )
        # Multi-round MRTR: the server returned yet another
        # InputRequiredResult on the retry. Return an error rather
        # than looping indefinitely — the user can retry the tool.
        if exec_data.get("result", {}).get("input_required") is not None:
            return _mcp_error_response(
                rpc_id,
                -32000,
                "MCP server requires additional elicitation rounds (not yet supported)",
            )

    output: str = exec_data.get("result", {}).get("output", "")
    _logger.debug(
        "MCP tools/call execute: session=%r tool=%r output_len=%d",
        session_id,
        namespaced_name,
        len(output),
    )

    # ── TOOL_RESULT policy ───────────────────────────────────────────
    result_ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content={"result": output},
        tool_name=namespaced_name,
        request_data={"name": namespaced_name, "arguments": arguments},
        actor=actor,
    )
    result_policy = await engine.evaluate(result_ctx)

    if result_policy.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result_policy.set_labels)

    _logger.debug(
        "MCP tools/call TOOL_RESULT policy: session=%r tool=%r action=%r reason=%r",
        session_id,
        namespaced_name,
        result_policy.action,
        result_policy.reason,
    )

    if result_policy.action == PolicyAction.DENY:
        output = f"[Result suppressed by policy: {result_policy.reason or 'no reason given'}]"
    elif result_policy.data is not None:
        # Policy returned transformed output (e.g. PII-redacted content).
        # The TOOL_RESULT phase contract requires data to be a str; coerce
        # and warn rather than dropping the result if a policy author returns
        # the wrong type (common mistake: returning the full content dict).
        if not isinstance(result_policy.data, str):
            _logger.warning(
                "TOOL_RESULT policy data must be str; got %s — coercing via str()",
                type(result_policy.data).__name__,
            )
        output = (
            result_policy.data if isinstance(result_policy.data, str) else str(result_policy.data)
        )

    return _mcp_ok_response(
        rpc_id,
        {"content": [{"type": "text", "text": output}]},
    )


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
    :returns: A configured :class:`APIRouter` exposing the
        ``/sessions`` endpoints.
    """
    router = APIRouter()

    # ── POST /sessions ───────────────────────────────────────────

    @router.post(
        "/sessions",
        status_code=201,
        response_model=None,
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
            except httpx.HTTPError:
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

    # ── GET /sessions/{session_id} ───────────────────────────────

    @router.get(
        "/sessions/{session_id}",
        # See create_session for the response_model=None rationale.
        response_model=None,
    )
    async def get_session(
        request: Request,
        response: Response,
        session_id: str,
        include_items: bool = Query(default=True),
        include_liveness: bool = Query(default=True),
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
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
        return SessionLabelsResponse(
            id=conv.id,
            labels=labels_with_closed_status(conv.labels, conv.title),
        )

    # ── GET /sessions ───────────────────────────────────────────

    @router.get(
        "/sessions",
        response_model=None,
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
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            agent_id=agent_id,
            agent_name=agent_name,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            order=order,
            sort_by=sort_by,
            search_query=normalized_query,
            include_archived=include_archived,
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
        return [item.model_dump() for item in items]

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
                if not isinstance(evt, dict) or evt.get("type") != "session_added":
                    continue
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

    # ── PATCH /sessions/{session_id} ────────────────────────────

    @router.patch(
        "/sessions/{session_id}",
        response_model=None,
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
        # Archiving/unarchiving is an owner-only lifecycle action: it pairs
        # with a client-driven, owner-gated stop, so an editor must not be
        # able to archive a session (hiding it, and via the client stopping
        # it) when they couldn't issue that stop. Every other field on this
        # endpoint needs only edit. Owner implies edit, so a single check at
        # the level the request actually requires gates both — no redundant
        # second permission-store read for archive/unarchive.
        required_level = LEVEL_OWNER if body.archived is not None else LEVEL_EDIT
        await _require_access(
            user_id, session_id, required_level, permission_store, conversation_store
        )
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
            if not isinstance(model_override, str) or not model_override.strip():
                raise OmnigentError(
                    "invalid model_override: must be a non-empty string",
                    code=ErrorCode.INVALID_INPUT,
                )
            model_override = model_override.strip()

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
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    ) from exc
            else:
                runner_id = _registered_runner_id(runner_router, body.runner_id, user_id=user_id)
                try:
                    await asyncio.to_thread(
                        conversation_store.replace_runner_id, session_id, runner_id
                    )
                except ConversationNotFoundError as exc:
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    ) from exc
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
                            _publish_runner_recovered_status(session_id)
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
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
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
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
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
            # Record the switch as a durable transcript note for in-process
            # (non-native) sessions, where the web ``/model`` command lives.
            # Native sessions are excluded: claude-native uses the picker and
            # codex-native pins its model at launch, so an injected AP-side
            # item there would be a stray/misleading record (see
            # _persist_model_change_note). ``live_forward`` (== not silent)
            # already excludes bind-time auto-applies, so only an explicit
            # /model command lands a note.
            is_native_terminal = (
                updated.labels.get(_CLAUDE_NATIVE_UI_LABEL_KEY) == _CLAUDE_NATIVE_UI_LABEL_VALUE
            )
            if not is_native_terminal:
                await _persist_model_change_note(
                    session_id,
                    updated.model_override,
                    conversation_store,
                )
        if body.labels is not None and body.labels:
            await asyncio.to_thread(conversation_store.set_labels, session_id, body.labels)
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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                ) from exc
            except ValueError as exc:
                # Store raises ValueError on attempted overwrite of an
                # already-set external_session_id — surface as
                # invalid_input so the caller (a wrapper bridge) sees a
                # 400 with the conflict explained.
                raise OmnigentError(
                    str(exc),
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
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
        # response_model=None: handler returns SessionResponse
        # but we suppress the OpenAPI schema injection to match
        # the convention of sibling routes.
        response_model=None,
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

        # Clone the chosen agent's bundle into a fresh session-scoped row so
        # the fork can be reconfigured without mutating the original.
        cloned_agent_id = generate_agent_id()
        await asyncio.to_thread(
            agent_store.create,
            agent_id=cloned_agent_id,
            name=f"{base_agent.name} (fork {cloned_agent_id[:10]})",
            bundle_location=base_agent.bundle_location,
            description=base_agent.description,
        )

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
        carry_history_into_native = await asyncio.to_thread(_agent_is_native, base_agent)
        # The source's native session id is only resumable by a target in the
        # SAME provider family — a Claude target can't clone a Codex rollout.
        # Cross-family, the store must skip the fork-source directive so the
        # runner takes the rebuild path instead of a doomed clone attempt
        # (a failed clone launches fresh, losing history).
        resume_source_native_session = not switching_agent or copy_model_settings

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
                copy_model_settings=copy_model_settings,
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
        # response_model=None: handler returns SessionResponse but we
        # suppress the OpenAPI schema injection to match sibling routes.
        response_model=None,
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
        carry_history_into_native = await asyncio.to_thread(_agent_is_native, target_agent)
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
        response_model=None,
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
        # ``tool_use_id`` is not stable on Claude Code's
        # PermissionRequest payload, and newer builds can write the
        # transcript ``function_call`` (tool_use) before this hook
        # returns — so neither can correlate/resolve the parked
        # request. The parked wait ends on one of three signals: an
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
        # Stamp the "Accept & allow all edits" hint (drives the UI's
        # third button) only for edit-tool prompts under a still-prompting
        # mode — see _allow_all_edits_eligible. The verdict site re-checks
        # the same predicate before honoring the flag.
        if _allow_all_edits_eligible(tool_name, permission_mode):
            extras["allow_all_edits"] = True
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
        data = event.get("data") or {}

        conv = conversation_store.get_conversation(session_id)
        if conv is None:
            raise OmnigentError(
                f"Session {session_id!r} not found.",
                code=ErrorCode.NOT_FOUND,
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
        ctx = _build_evaluation_context(phase, data, event, actor=_build_actor(user_id))
        result = await engine.evaluate(ctx, read_only=is_read_only)

        # URL-based elicitation for blocking phases: on a TOOL_CALL or
        # LLM_REQUEST ASK, hold the gate server-side rather than
        # returning ASK. Returning ASK makes the native hook emit
        # ``defer``, which a permissive ``permission_mode``
        # (acceptEdits / bypassPermissions) auto-approves — bypassing
        # the human. Instead we publish the approval elicitation, park
        # until the human resolves it via the resolve URL, and collapse
        # to a hard ALLOW / DENY so the caller never sees ASK.
        # TOOL_CALL and LLM_REQUEST are the two phases that can block
        # execution before it starts (tool dispatch / LLM call).
        if result.action == PolicyAction.ASK and phase in (Phase.TOOL_CALL, Phase.LLM_REQUEST):
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
                    ):
                        approved = await _hold_native_ask_gate(
                            request,
                            session_id=session_id,
                            phase=phase,
                            data=data,
                            engine=engine,
                            result=result,
                            conversation_store=conversation_store,
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
        return Response(
            content=json.dumps(resp_body),
            media_type="application/json",
        )

    # ── POST /sessions/{session_id}/hooks/codex-elicitation-request ─

    @router.post(
        "/sessions/{session_id}/hooks/codex-elicitation-request",
        response_model=None,
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
        body = codex_request.build_response(result)
        return Response(
            content=json.dumps(body),
            media_type="application/json",
        )

    # ── GET /sessions/{session_id}/items ─────────────────────────

    @router.get(
        "/sessions/{session_id}/items",
        response_model=None,
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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
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
    )
    async def list_child_sessions(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
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
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order=order,
            sort_by="created_at",
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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
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
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
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
        except httpx.HTTPError as exc:
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
        except httpx.HTTPError as exc:
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
        except httpx.HTTPError as exc:
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
        except httpx.HTTPError as exc:
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
        except httpx.HTTPError as exc:
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
        return await _proxy_get_to_runner(session_id, path)

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
        exact shape those wrappers send — ``terminal`` of ``"claude"``
        or ``"codex"`` with ``session_key`` ``"main"`` — anything else
        carrying a marker still goes through the declared-name gate
        (it would otherwise be an arbitrary-terminal bypass).

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
            and body.get("terminal") in ("claude", "codex")
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
        response_model=None,
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
            raise OmnigentError(
                error.get("message", "Terminal transfer failed"),
                code=error.get("code", "internal_error"),
                http_status=status,
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
        content = await file.read()
        from omnigent.runtime.content_resolver import (
            _resolve_content_type,
        )

        content_type = _resolve_content_type(
            file.content_type,
            file.filename,
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
        return Response(content=content, media_type=media_type)

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
        return await _proxy_get_to_runner(session_id, path)

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
        return await _proxy_get_to_runner(session_id, path)

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
        return await _proxy_get_to_runner(session_id, path)

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/diff/{relative_path:path}",
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
        return await _proxy_get_to_runner(session_id, path)

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
        return await _proxy_get_to_runner(session_id, path)

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

    # ── POST /sessions/{session_id}/events ───────────────────────

    @router.post(
        "/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )

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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
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
            _EXTERNAL_SESSION_INTERRUPTED_TYPE,
            _EXTERNAL_ELICITATION_RESOLVED_TYPE,
            _EXTERNAL_SESSION_STATUS_TYPE,
            _EXTERNAL_SESSION_USAGE_TYPE,
            _EXTERNAL_COMPACTION_STATUS_TYPE,
            _EXTERNAL_MODEL_CHANGE_TYPE,
            _EXTERNAL_SESSION_TODOS_TYPE,
            _EXTERNAL_SUBAGENT_START_TYPE,
            _EXTERNAL_CODEX_SUBAGENT_START_TYPE,
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
                session_stream.publish(
                    session_id,
                    {
                        "type": "response.output_text.delta",
                        "delta": f"[Denied by policy: {reason}]",
                    },
                )
                _publish_status(session_id, "idle")
                # Return the same shape the client expects from POST
                # /events so postEvent doesn't throw on an unexpected
                # response body. queued=False signals the event was
                # handled synchronously (denied, not queued for a turn).
                return {"queued": False, "denied": True, "reason": reason}
        elif body.type == _SLASH_COMMAND_TYPE and conv.agent_id is not None:
            _input_verdict = await _evaluate_input_policy(
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
                session_stream.publish(
                    session_id,
                    {
                        "type": "response.output_text.delta",
                        "delta": f"[Denied by policy: {reason}]",
                    },
                )
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
                await _stop_session_host_runner(
                    session_id,
                    stop_conv.host_id,
                    stop_conv.runner_id,
                    getattr(request.app.state, "host_registry", None),
                )
            # Stop is non-sticky: no persistent marker is written. The
            # runner tunnel dropping above flips ``runner_online`` to false
            # honestly, and the next message auto-relaunches the session on
            # its (still-online) host via the normal message-dispatch
            # relaunch path below.
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
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE:
            _publish_external_output_text_delta(session_id, body)
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
            _publish_status(session_id, status, response_id=response_id)
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
            except httpx.HTTPError as exc:
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
        # Item event (message, function_call_output, etc.).
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
                    raise OmnigentError(
                        "Session not found",
                        code=ErrorCode.NOT_FOUND,
                    )
                runner_client = await _get_runner_client(session_id, runner_router)
        # Whether the runner was initially unavailable but became routable
        # below. In that case the session-init handshake may still be
        # racing the first message, even if we reused the original binding
        # instead of launching a replacement.
        _runner_needs_session_init = False
        if runner_client is None and conv.host_id is not None:
            _tunnel_registry = getattr(request.app.state, "tunnel_registry", None)
            # A just-created host session already has a runner_id before
            # the runner's tunnel is registered. The Web UI can post the
            # first message during that gap; wait briefly for the pinned
            # runner before treating it as dead and replacing it.
            if conv.runner_id is not None and _HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0:
                _logger.info(
                    "Waiting up to %.1fs for host-bound runner %s to register "
                    "for session %s before relaunch",
                    _HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                    conv.runner_id,
                    session_id,
                )
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
                            raise OmnigentError(
                                "Session not found",
                                code=ErrorCode.NOT_FOUND,
                            )
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
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
        conv = refreshed_conv
        if _runner_needs_session_init:
            # The runner was unavailable when this request began, so its
            # connect callback may still be racing us. Await the handshake
            # so the terminal + transcript forwarder are watching before we
            # inject the message — otherwise a native web message is
            # forwarded into a TUI whose forwarder isn't attached, the
            # round-trip never mirrors back, and the optimistic bubble
            # sticks with no reply (host-restart bug).
            await _ensure_runner_session_initialized(session_id, conv, runner_client)
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
        )
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
        endpoint. The generator handles disconnects via a
        ``try/finally`` that emits the ``[DONE]`` sentinel in all
        exit paths — see :func:`_stream_live_events`.

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
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )
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
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
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
            except httpx.HTTPError:
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
        deleted = await conversation_store.delete_conversation(session_id)
        if not deleted:
            raise OmnigentError(
                "Session not found",
                code=ErrorCode.NOT_FOUND,
            )
        # The session is gone, so is its launch-progress state. Failed
        # launches are retained in the cache for reload visibility while
        # the session exists; without this eviction every deleted
        # failed-launch session would leak one entry for the process
        # lifetime.
        _session_sandbox_status_cache.pop(session_id, None)
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
        return ConversationDeleted(id=session_id)

    # ── Permission management endpoints ──────────────────────────

    @router.put(
        "/sessions/{session_id}/permissions",
        response_model=None,
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
        if body.user_id == RESERVED_USER_PUBLIC and body.level > LEVEL_READ:
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
    ) -> list[PermissionObject]:
        """List all permission grants on a session.

        Requires manage-level access.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session to list grants for,
            e.g. ``"conv_abc123"``.
        :returns: List of :class:`PermissionObject`.
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
        grants = await asyncio.to_thread(permission_store.list_for_session, session_id)
        return [
            PermissionObject(
                user_id=g.user_id,
                conversation_id=g.conversation_id,
                level=g.level,
            )
            for g in grants
        ]

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
        ``policies``, and ``skills``. If the cache is ``None``, the
        spec is not cached, or the load fails, all are left as empty
        lists rather than raising — the endpoint must not fail because
        one spec can't be read.

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
        if cache is not None:
            try:
                loaded = cache.load(
                    agent.id, agent.bundle_location, expand_env=agent.session_id is None
                )
                harness = loaded.spec.executor.harness_kind
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
            description=agent.description,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
            harness=harness,
            mcp_servers=mcp_servers,
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
        response_model=None,  # Returns a raw Response with application/json
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
            return await _handle_mcp_tools_call(
                rpc_id,
                session_id,
                params,
                conversation_store,
                agent_store,
                runner_router,
                actor=_build_actor(user_id),
                request=request,
            )

        return _mcp_error_response(rpc_id, -32601, f"Method not found: {method!r}")

    return router


async def _fetch_runner_skills(
    runner_client: httpx.AsyncClient | None,
    session_id: str,
) -> list[SkillSummary]:
    """
    Fetch a session's merged skills from its bound runner.

    Skills are runner-owned: the runner discovers them against its own
    filesystem (the spec's bundled skills plus host skills under the
    session's workspace and the runner's ``~/.claude/skills/``). The
    server only overlays the result onto the session snapshot (the web
    composer's slash-command menu).
    Best-effort: a missing/unreachable runner, a non-200, or any
    transport error yields an empty list rather than failing the
    snapshot.

    :param runner_client: HTTP client pointed at the bound runner, or
        ``None`` when no runner is bound.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: Skill summaries (name + one-line description) for the
        session, or ``[]`` when unavailable.
    """
    if runner_client is None:
        return []
    cached = _runner_skills_cache.get(session_id)
    if cached is not None:
        return cached
    # Don't await the runner here: this snapshot is polled continuously
    # (incl. mid-turn), and a per-poll runner round-trip pins the runner's
    # event loop and wedges the turn. Kick one background fetch (single-
    # flight) and return ``[]``; a later poll serves the cached result.
    if session_id not in _runner_skills_inflight:
        task = asyncio.create_task(_load_runner_skills(runner_client, session_id))
        _runner_skills_inflight[session_id] = task
        task.add_done_callback(lambda _t, sid=session_id: _runner_skills_inflight.pop(sid, None))
    return []


async def _load_runner_skills(
    runner_client: httpx.AsyncClient,
    session_id: str,
) -> None:
    """Background single-flight fetch of a session's runner-owned skills.

    Populates :data:`_runner_skills_cache` on success so subsequent
    snapshot polls serve skills without a per-poll runner round-trip. Runs
    off the snapshot's critical path (see :func:`_fetch_runner_skills`).
    Best-effort: transport errors / non-200 / malformed payloads leave the
    cache unset so a later poll retries.

    :param runner_client: HTTP client pointed at the bound runner.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/skills",
            timeout=5.0,
        )
    except httpx.HTTPError:
        _logger.debug("Runner skills query failed for %s", session_id)
        return
    if resp.status_code != 200:
        return
    try:
        raw = resp.json().get("skills", [])
        skills = [SkillSummary(name=s["name"], description=s["description"]) for s in raw]
    except (ValueError, AttributeError, KeyError, TypeError):
        _logger.debug("Runner skills payload malformed for %s", session_id)
        return
    _runner_skills_cache[session_id] = skills
    # Nudge any subscribed client to re-read the (now-warm) snapshot so
    # its slash-command menu fills without waiting for the next bind.
    _publish_runner_skills(session_id)


async def _get_session_snapshot(
    conv_store: ConversationStore,
    session_id: str,
    permission_level: int | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    conversation: Conversation | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    include_items: bool = True,
    runner_exit_reports: RunnerExitReports | None = None,
) -> SessionResponse:
    """
    Read a full session snapshot from the store.

    Centralizes the create/get response building so both endpoints
    return identical projections. The lifecycle ``status`` is
    derived from the relay-fed ``_session_status_cache`` (the tasks
    table has been removed).

    :param conv_store: The conversation store to read from.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param permission_level: The requesting user's numeric level
        on this session, or ``None`` when permissions are disabled.
    :param agent_store: Optional agent store used to look up the
        bound agent's bundle location. ``None`` in legacy call sites
        that don't yet pass it.
    :param agent_cache: Optional agent cache used to load the parsed
        spec from the bundle (provides ``llm_model`` and
        ``context_window``). ``None`` in legacy call sites.
    :param conversation: The already-fetched conversation row to reuse,
        skipping the ``get_conversation`` read. Pass it when the caller
        just authorized the session (which fetched the same row) so the
        snapshot doesn't re-read it. ``None`` reads it here as before.
    :param liveness_lookup: Bulk session-liveness lookup (the server's
        ``_bulk_session_liveness``) used to populate ``runner_online``
        and ``host_online`` on the snapshot. ``None`` (e.g. focused
        tests) leaves both fields ``None`` so the client falls back to
        its ``/health`` poll.
    :param include_items: When ``False``, skip the committed-items read
        and return ``items=[]``. Callers that hydrate the transcript
        through ``GET /sessions/{id}/items`` (the web chat surface)
        pass ``False`` — the items read is the most expensive step of
        the snapshot build and its result would be discarded.
    :returns: The fully populated :class:`SessionResponse`.
    :raises OmnigentError: 404 if no session exists, 500 if the
        underlying conversation has no agent binding
        (see :func:`_build_session_response`).
    """
    conv = conversation
    if conv is None:
        conv = await asyncio.to_thread(conv_store.get_conversation, session_id)
    if conv is None:
        raise OmnigentError(
            "Session not found",
            code=ErrorCode.NOT_FOUND,
        )
    # Return the most recent committed items while preserving the
    # SessionResponse contract that ``items`` is chronological. The
    # store's default page is the oldest 100 (``order="asc"``), which
    # makes long-session reconnects appear stale in clients that use the
    # snapshot directly.
    items: list[ConversationItem] = []
    if include_items:
        items_page = await asyncio.to_thread(
            conv_store.list_items,
            conversation_id=session_id,
            limit=100,
            order="desc",
        )
        items = list(reversed(items_page.data))
    # Resolve the bound runner client once — used for live status (on a
    # status-cache miss) and for runner-owned skill discovery below.
    #
    # Prefer the router (multi-runner deployments wire only
    # ``set_runner_router``; the legacy ``get_runner_client`` singleton
    # stays ``None`` there). Fall back to the legacy singleton for
    # single-runner / in-process tests.
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_client: httpx.AsyncClient | None = None
    runner_router = get_runner_router()
    if runner_router is not None:
        try:
            routed = runner_router.client_for_session_resources(session_id)
            runner_client = routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s on snapshot build",
                session_id,
            )
    if runner_client is None:
        runner_client = get_runner_client()

    status = _session_status_cache.get(session_id)
    if status is None:
        # Cache miss: either the server restarted, or the relay
        # has not yet published the first ``"running"`` event
        # for a freshly bound session (the relay's GET /stream
        # is still in its tunnel handshake). Ask the runner for
        # live status so we don't synthesize a stale ``"idle"``
        # while a turn is actually in flight.
        if runner_client is not None:
            try:
                resp = await runner_client.get(
                    f"/v1/sessions/{session_id}",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    status = resp.json().get("status", "idle")
                    _session_status_cache[session_id] = status
            except httpx.HTTPError:
                _logger.debug(
                    "Runner status query failed for %s",
                    session_id,
                )
        if status is None:
            status = "idle"
    # last_total_tokens and last_task_error come from the context-tokens
    # label written by the forwarder (tasks table has been removed).
    last_total_tokens: int | None = None
    last_task_error: dict[str, str] | None = None
    raw_label = conv.labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY)
    if isinstance(raw_label, str) and raw_label.isdigit():
        last_total_tokens = int(raw_label)
    # Runner-crash durability: if the session's bound runner reported an
    # unexpected exit (host.runner_exited → RunnerExitReports), surface the
    # cause as last_task_error so a reload/late-open still renders the error
    # banner — the live session.status:failed push is gone by then. status
    # already reads "failed" from the cache (set by _on_runner_exited). The
    # report is keyed by the CURRENT runner_id, so a successful relaunch
    # (new token-bound runner_id) naturally stops matching. Access is gated
    # by the session-snapshot's own authorization, so the unscoped get is
    # correct here (the report is this session's own runner).
    if runner_exit_reports is not None and conv.runner_id is not None:
        exit_error = runner_exit_reports.get(conv.runner_id)
        if exit_error is not None:
            last_task_error = {"code": "runner_failed_to_start", "message": exit_error}
            status = "failed"
    llm_model: str | None = None
    context_window: int | None = None
    agent_name: str | None = None
    if agent_store is not None and agent_cache is not None and conv.agent_id is not None:
        try:
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is not None:
                agent_name = agent.name
                if agent.bundle_location is not None:
                    # Offload to a worker thread: on a cold cache this fetches
                    # the bundle from the artifact store and parses the spec —
                    # blocking IO that would otherwise stall the single-worker
                    # event loop on every page-load snapshot.
                    loaded = await asyncio.to_thread(
                        agent_cache.load, agent.id, agent.bundle_location
                    )
                    spec = loaded.spec
                    # Prefer the spec's name over the agent row's: a
                    # switch-created session-scoped clone is named
                    # "<builtin> (switch ag_…)" for row disambiguation,
                    # but clients display agent_name verbatim — the spec
                    # carries the clean identity (e.g. "claude-native-ui").
                    if spec.name:
                        agent_name = spec.name
                    llm_model = spec.executor.model
                    # Size the context ring against whatever the next turn
                    # will actually run. spec.executor.context_window only
                    # applies to spec.model, so it's bypassed when an
                    # override is active.
                    effective_model = (
                        conv.model_override if conv.model_override is not None else llm_model
                    )
                    spec_cw = spec.executor.context_window
                    if spec_cw is not None and conv.model_override is None:
                        context_window = spec_cw
                    elif effective_model is not None:
                        from omnigent.llms.context_window import get_model_context_window

                        # Offload to a worker thread: on a cache-cold
                        # provider catalog this does a blocking HTTP fetch
                        # (and litellm registry lookups are CPU-bound), so
                        # running it inline would stall the single-worker
                        # event loop and serialize every concurrent request
                        # behind this snapshot.
                        context_window = await asyncio.to_thread(
                            get_model_context_window, effective_model
                        )
        except Exception:  # noqa: BLE001 — best-effort; missing agent must not break session fetch
            pass
    # Skills are runner-owned: the bound runner discovers them against its
    # own filesystem (bundled skills + host skills under the session's
    # workspace and ``~/.claude/skills/``) — the host where the harness
    # actually executes and may read a skill's local resource files. The
    # server only overlays the result; best-effort, empty when no runner
    # is bound or it can't be reached.
    skills = await _fetch_runner_skills(runner_client, session_id)
    # Dynamic override from the forwarder (real Claude Code window).
    # Only present after the first statusLine tick; before that the
    # spec default applies.
    raw_window_label = conv.labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY)
    if isinstance(raw_window_label, str) and raw_window_label.isdigit():
        observed = int(raw_window_label)
        if observed > 0:
            context_window = observed
    # Resolve strict runner + host liveness for the open-session view.
    # The lookup hits the conversations + hosts tables, so offload it to
    # a worker thread (mirroring _apply_liveness_to_items). Left None on
    # both fields when no lookup is wired (focused tests).
    runner_online: bool | None = None
    host_online: bool | None = None
    if liveness_lookup is not None:
        liveness = await asyncio.to_thread(liveness_lookup, [session_id])
        result = liveness.get(session_id)
        if result is not None:
            runner_online = result.runner_online
            host_online = result.host_online
    # Subtree usage (this session + its sub-agent descendants) so the
    # displayed cost includes sub-agents — a codex/claude sub-agent's spend
    # is persisted on its own child conversation, not the parent's, so the
    # parent's own session_usage would under-report. Off the event loop
    # because it pages the conversation tree from the store.
    subtree_usage = await asyncio.to_thread(load_session_usage, conv.id, conv_store)
    return _build_session_response(
        conv,
        items,
        status,
        permission_level,
        llm_model=llm_model,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        last_task_error=last_task_error,
        agent_name=agent_name,
        skills=skills,
        runner_online=runner_online,
        host_online=host_online,
        pending_elicitation_events=await asyncio.to_thread(
            _pending_elicitation_snapshot_for_session,
            conv_store,
            conv,
        ),
        subtree_usage=subtree_usage,
    )
