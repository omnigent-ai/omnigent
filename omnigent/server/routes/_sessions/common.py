"""Shared constants, state, and small dataclasses for the sessions routes.

Leaf module: imports no sibling ``_sessions`` module. Holds the mutable
caches/registries/task-sets and immutable config the helpers and router share,
so a single instance is imported by reference across the package.
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from dataclasses import dataclass
from typing import Any

import cachetools
import httpx
from pydantic import TypeAdapter

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
)
from omnigent.harness_plugins import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
    CURSOR_NATIVE_CODING_AGENT,
    KIRO_NATIVE_CODING_AGENT,
    OPENCODE_NATIVE_CODING_AGENT,
    PI_NATIVE_CODING_AGENT,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.server.schemas import (
    McpServerStartup,
    SandboxStatus,
    ServerStreamEvent,
    SkillSummary,
)
from omnigent.spec.types import (
    StateUpdate,
)

# Pinned to the historical module path so log records keep landing on the
# ``omnigent.server.routes.sessions`` logger after the split into this package.
_logger = logging.getLogger("omnigent.server.routes.sessions")


_INTERRUPT_TYPE: str = "interrupt"


_APPROVAL_TYPE: str = "approval"


_MCP_ELICITATION_TYPE: str = "mcp_elicitation"


_COMPACT_TYPE: str = "compact"


_SLASH_COMMAND_TYPE: str = "slash_command"


_STOP_SESSION_TYPE: str = "stop_session"


_EXTERNAL_ASSISTANT_MESSAGE_TYPE: str = "external_assistant_message"


_EXTERNAL_CONVERSATION_ITEM_TYPE: str = "external_conversation_item"


_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE: str = "external_output_text_delta"


_EXTERNAL_TOOL_OUTPUT_DELTA_TYPE: str = "external_tool_output_delta"


_EXTERNAL_OUTPUT_REASONING_DELTA_TYPE: str = "external_output_reasoning_delta"


_EXTERNAL_SESSION_INTERRUPTED_TYPE: str = "external_session_interrupted"


_EXTERNAL_SESSION_SUPERSEDED_TYPE: str = "external_session_superseded"


_EXTERNAL_ELICITATION_RESOLVED_TYPE: str = "external_elicitation_resolved"


_EXTERNAL_SESSION_STATUS_TYPE: str = "external_session_status"


_EXTERNAL_SESSION_STATUS_VALUES: frozenset[str] = frozenset(
    {"idle", "running", "waiting", "failed"}
)


_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT: int = 1000


_EXTERNAL_COMPACTION_STATUS_TYPE: str = "external_compaction_status"


_EXTERNAL_COMPACTION_STATUS_VALUES: frozenset[str] = frozenset(
    {"in_progress", "completed", "failed"}
)


_EXTERNAL_MCP_STARTUP_TYPE: str = "external_mcp_startup"


_EXTERNAL_MCP_STARTUP_STATUS_VALUES: frozenset[str] = frozenset(
    {"starting", "ready", "failed", "cancelled"}
)


_EXTERNAL_SESSION_USAGE_TYPE: str = "external_session_usage"


_EXTERNAL_MODEL_CHANGE_TYPE: str = "external_model_change"


_EXTERNAL_MODEL_OPTIONS_TYPE: str = "external_model_options"


_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE: str = "external_reasoning_effort_change"


_EXTERNAL_SUBAGENT_START_TYPE: str = "external_subagent_start"


_CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "claude-code-native-ui-subagent"


_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY = "omnigent.claude_native.subagent_id"


_CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY = "omnigent.claude_native.tool_use_id"


_CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY = "omnigent.claude_native.description"


_EXTERNAL_CODEX_SUBAGENT_START_TYPE: str = "external_codex_subagent_start"


_CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE = "codex-native-ui-subagent"


_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.subagent_thread_id"


_CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY = "omnigent.codex_native.parent_thread_id"


_CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY = "omnigent.codex_native.collab_tool_call_id"


_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY = "omnigent.codex_native.prompt"


_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY = "omnigent.codex_native.agent_nickname"


_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY = "omnigent.codex_native.agent_role"


_CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY = "omnigent.codex_native.collaboration_mode"


_EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE: str = "external_codex_collaboration_mode_change"


_CODEX_NATIVE_COLLABORATION_MODES: frozenset[str] = frozenset({"default", "plan"})


_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK = "Codex"


_LAST_CONTEXT_TOKENS_LABEL_KEY: str = "omnigent.last_context_tokens"


_LAST_CONTEXT_WINDOW_LABEL_KEY: str = "omnigent.last_context_window"


_LAST_TASK_ERROR_CODE_LABEL_KEY: str = "omnigent.last_task_error_code"


_LAST_TASK_ERROR_MESSAGE_LABEL_KEY: str = "omnigent.last_task_error_message"


_LABEL_VALUE_MAX_LEN: int = LABEL_VALUE_MAX_LEN


_EXTERNAL_SESSION_TODOS_TYPE: str = "external_session_todos"


_CLAUDE_NATIVE_WRAPPER_LABEL_KEY = "omnigent.wrapper"


_CLAUDE_NATIVE_WRAPPER_LABEL_VALUE = CLAUDE_NATIVE_CODING_AGENT.wrapper_label


_CLAUDE_NATIVE_UI_LABEL_KEY = "omnigent.ui"


_CLAUDE_NATIVE_UI_LABEL_VALUE = "terminal"


_CLAUDE_NATIVE_HARNESS = CLAUDE_NATIVE_CODING_AGENT.harness


_CLAUDE_NATIVE_MODEL = CLAUDE_NATIVE_CODING_AGENT.agent_name


_CODEX_NATIVE_WRAPPER_LABEL_VALUE = CODEX_NATIVE_CODING_AGENT.wrapper_label


_CODEX_NATIVE_HARNESS = CODEX_NATIVE_CODING_AGENT.harness


_CODEX_NATIVE_MODEL = CODEX_NATIVE_CODING_AGENT.agent_name


_OPENCODE_NATIVE_WRAPPER_LABEL_VALUE = OPENCODE_NATIVE_CODING_AGENT.wrapper_label


_CURSOR_NATIVE_WRAPPER_LABEL_VALUE = CURSOR_NATIVE_CODING_AGENT.wrapper_label


_CURSOR_NATIVE_HARNESS = CURSOR_NATIVE_CODING_AGENT.harness


_KIRO_NATIVE_WRAPPER_LABEL_VALUE = KIRO_NATIVE_CODING_AGENT.wrapper_label


_PI_NATIVE_WRAPPER_LABEL_VALUE = PI_NATIVE_CODING_AGENT.wrapper_label


_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S = 30.0


_NATIVE_TERMINAL_START_FAILED_CODE = "native_terminal_start_failed"


_NATIVE_TERMINAL_ENSURE_FAILED_CODE = "native_terminal_ensure_failed"


_NATIVE_POLICY_NOT_ENFORCED_CODE = "native_policy_not_enforced"


_HOST_BOUND_RUNNER_CONNECT_GRACE_S = 10.0


_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S = 30.0


_HOST_RUNNER_STATUS_TIMEOUT_S = 3.0


_MANAGED_RESUMABLE_TUNNEL_STALE_S = 30.0


_RUNNER_CONVICTION_POLL_S = 0.25


_HOST_LAUNCH_RESULT_TIMEOUT_S = 10.0


_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S = 86400.0


_browser_action_registry: dict[str, asyncio.Future[dict[str, Any]]] = {}  # -> parked Future


_browser_action_owners: dict[str, str] = {}  # -> issuing session_id (result POST must match)


_browser_action_claims: dict[str, str] = {}


_BROWSER_ACTION_AWAIT_S = 30.0


_BROWSER_ACTION_TIMEOUT_RESULT: dict[str, Any] = {
    "error": "browser action timed out — is the session open in the Omnigent desktop app?"
}


_CLAUDE_NATIVE_EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)


_CLAUDE_NATIVE_REMEMBER_INELIGIBLE_TOOLS: frozenset[str] = frozenset(
    {"ExitPlanMode", "AskUserQuestion"}
)


_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S = 86400.0


_ANTIGRAVITY_NATIVE_ELICITATION_HOOK_TIMEOUT_S = 86400.0


_CURSOR_NATIVE_PERMISSION_HOOK_TIMEOUT_S = 86400.0


_NATIVE_PERMISSION_HOOK_TIMEOUT_S = 86400.0


_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S = 300.0


_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES = 1024


_HARNESS_ELICITATION_REPARK_GRACE_S = 10.0


_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_[a-z]+_[0-9a-f]{32}$")


_EVALUATE_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_evaluate_[0-9a-f]{32}$")


_RACE_TASK_REAP_TIMEOUT_S = 5.0


_SESSION_STREAM_HEARTBEAT_INTERVAL_S = 15.0


_SNAPSHOT_RUNNER_TIMEOUT_S = 2.0


_RUNNER_RELAY_READY_TIMEOUT_S = 5.0


_RUNNER_FORWARD_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=10.0)


_ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(ITEM_TYPE_TO_DATA_CLS.keys()) | {
    _INTERRUPT_TYPE,
    _APPROVAL_TYPE,
    _MCP_ELICITATION_TYPE,
    _COMPACT_TYPE,
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
}


_SERVER_STREAM_EVENT_ADAPTER: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)


_WATCHER_TASKS: set[asyncio.Task[None]] = set()


_session_status_cache: dict[str, str] = {}


_session_active_response_cache: dict[str, str] = {}


_session_background_task_count_cache: dict[str, int] = {}


_read_last_seen: dict[str, dict[str, int]] = {}


_read_explicit_unread: dict[str, set[str]] = {}


_interrupt_fenced_sessions: set[str] = set()


_intentional_stop_sessions: set[str] = set()


_TERMINAL_RESPONSE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    }
)


_FENCE_EXEMPT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.elicitation_request",
        "response.elicitation_resolved",
    }
)


_SESSION_UPDATES_RESCAN_INTERVAL_S: float = 4.0


_SESSION_UPDATES_HEARTBEAT_INTERVAL_S: float = 30.0


_SESSION_UPDATES_MAX_WATCHED: int = 500


_SHARED_DISCOVERY_KEY = "__all__"


_session_todos_cache: dict[str, list[dict[str, Any]]] = {}


_session_terminal_pending_cache: dict[str, bool] = {}


_session_sandbox_status_cache: dict[str, SandboxStatus] = {}


_session_mcp_startup_cache: dict[str, dict[str, McpServerStartup]] = {}


_runner_skills_cache: dict[str, list[SkillSummary]] = {}


_runner_skills_inflight: dict[str, asyncio.Task[None]] = {}


_model_options_cache: dict[str, list[dict[str, Any]]] = {}


_model_options_inflight: dict[str, asyncio.Task[None]] = {}


_MODEL_OPTIONS_RETRY_DELAYS_S = (0.25, 0.5, 1.0, 2.0, 2.0)


_pushed_model_options_cache: dict[str, list[dict[str, Any]]] = {}


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


_pending_policy_ask_writes: cachetools.LRUCache[str, _PendingPolicyAskWrites] = (
    cachetools.LRUCache(maxsize=512)
)


_TURN_ACTOR_LABEL = "omnigent.turn_actor"


_native_ask_gate_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


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


_runner_relay_tasks: dict[str, _RelayHandle] = {}


_deferred_elicitation_clear_tasks: set[asyncio.Task[None]] = set()


_MODEL_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


_native_popup_forward_tasks: set[asyncio.Task[None]] = set()


_SUBAGENT_FORWARD_RECONNECT_WAIT_S = 5.0


_managed_launch_tasks: set[asyncio.Task[None]] = set()


_RUNNER_SESSION_INIT_TIMEOUT_S = 10.0


_STOP_RUNNER_RESULT_TIMEOUT_S = 10.0


_COMPACT_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


_FORK_HISTORY_NATIVE_HARNESSES: frozenset[str] = frozenset(
    {
        "claude-native",
        "native-claude",
        "codex-native",
        "native-codex",
        "hermes-native",
        "native-hermes",
        "pi-native",
        # qwen-native rebuilds qwen's on-disk chat recording (+ runtime/meta
        # sidecars) from the copied items, so a fork carries history into the
        # qwen TUI (see _build_qwen_fork_recording / write_qwen_session_recording).
        # Only the canonical id is needed — "native-qwen" is aliased to it.
        "qwen-native",
    }
)


_CURSOR_FORK_HISTORY_HARNESSES: frozenset[str] = frozenset(
    {"cursor-native", "native-cursor", "opencode-native", "native-opencode"}
)


_DENY_SENTINEL_PREFIX = "[Denied by policy: "


_MAX_TERMINAL_LAUNCH_ARGS = 256


_MAX_TERMINAL_LAUNCH_ARG_LEN = 4096


COST_CONTROL_OVERRIDE_VALUES = frozenset({"on", "off"})


_CHILD_PREVIEW_LIMIT = 150


_UI_ADDED_AGENT_TITLE_PREFIX = "ui"


_UPLOAD_READ_CHUNK_BYTES: int = 1024 * 1024


# Live runner-owned model catalogs, keyed by wrapper label to route segment.
# Static catalogs bypass this cache so ``refresh_state`` cannot blank them.
_MODEL_OPTIONS_ENDPOINT_BY_WRAPPER: dict[str, str] = {
    _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE: "claude-model-options",
    _CODEX_NATIVE_WRAPPER_LABEL_VALUE: "codex-model-options",
    _OPENCODE_NATIVE_WRAPPER_LABEL_VALUE: "codex-model-options",
    # pi-native is deliberately NOT here: its catalog is PUSHED by the resident
    # extension (``external_model_options`` → ``_pushed_model_options_cache``),
    # not fetched from a runner route, so the picker works in every auth path
    # (Omnigent provider OR pi's own ``/login``) — see ``_fetch_model_options``.
}


# Runner router for the native-terminal approval popup, set once at app startup
# (see :func:`set_server_runner_router`). The tool-policy ASK gate forwards a
# ``cost_approval_popup`` control event to the bound runner from a parked-gate
# background task that carries no FastAPI request / route closure, so it reads
# the router from this module-level global via :func:`get_server_runner_router`.
_server_runner_router: RunnerRouter | None = None


def set_server_runner_router(runner_router: RunnerRouter | None) -> None:
    """Stash the runner router for the native-terminal approval popup.

    Called once from ``create_app`` so the tool-policy ASK gate
    (``_spawn_native_approval_popup_forward``) can reach the bound runner from
    background contexts that do not carry the request / route closure.

    :param runner_router: The session runner router, or ``None`` in in-process
        setups.
    :returns: None.
    """
    global _server_runner_router
    _server_runner_router = runner_router


def get_server_runner_router() -> RunnerRouter | None:
    """Return the runner router stashed by :func:`set_server_runner_router`."""
    return _server_runner_router


__all__ = [
    "COST_CONTROL_OVERRIDE_VALUES",
    "_ALLOWED_EVENT_TYPES",
    "_ANTIGRAVITY_NATIVE_ELICITATION_HOOK_TIMEOUT_S",
    "_APPROVAL_TYPE",
    "_BROWSER_ACTION_AWAIT_S",
    "_BROWSER_ACTION_TIMEOUT_RESULT",
    "_CHILD_PREVIEW_LIMIT",
    "_CLAUDE_NATIVE_DESCRIPTION_LABEL_KEY",
    "_CLAUDE_NATIVE_EDIT_TOOLS",
    "_CLAUDE_NATIVE_HARNESS",
    "_CLAUDE_NATIVE_MESSAGE_TIMEOUT_S",
    "_CLAUDE_NATIVE_MODEL",
    "_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
    "_CLAUDE_NATIVE_REMEMBER_INELIGIBLE_TOOLS",
    "_CLAUDE_NATIVE_SUBAGENT_ID_LABEL_KEY",
    "_CLAUDE_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE",
    "_CLAUDE_NATIVE_TOOL_USE_ID_LABEL_KEY",
    "_CLAUDE_NATIVE_UI_LABEL_KEY",
    "_CLAUDE_NATIVE_UI_LABEL_VALUE",
    "_CLAUDE_NATIVE_WRAPPER_LABEL_KEY",
    "_CLAUDE_NATIVE_WRAPPER_LABEL_VALUE",
    "_CODEX_NATIVE_COLLABORATION_MODES",
    "_CODEX_NATIVE_COLLABORATION_MODE_LABEL_KEY",
    "_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S",
    "_CODEX_NATIVE_HARNESS",
    "_CODEX_NATIVE_MODEL",
    "_CODEX_NATIVE_SUBAGENT_DISPLAY_FALLBACK",
    "_CODEX_NATIVE_SUBAGENT_NICKNAME_LABEL_KEY",
    "_CODEX_NATIVE_SUBAGENT_PARENT_THREAD_ID_LABEL_KEY",
    "_CODEX_NATIVE_SUBAGENT_PROMPT_LABEL_KEY",
    "_CODEX_NATIVE_SUBAGENT_ROLE_LABEL_KEY",
    "_CODEX_NATIVE_SUBAGENT_THREAD_ID_LABEL_KEY",
    "_CODEX_NATIVE_SUBAGENT_TOOL_CALL_ID_LABEL_KEY",
    "_CODEX_NATIVE_SUBAGENT_WRAPPER_LABEL_VALUE",
    "_CODEX_NATIVE_WRAPPER_LABEL_VALUE",
    "_COMPACT_LOCKS",
    "_COMPACT_TYPE",
    "_CURSOR_FORK_HISTORY_HARNESSES",
    "_CURSOR_NATIVE_HARNESS",
    "_CURSOR_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
    "_CURSOR_NATIVE_WRAPPER_LABEL_VALUE",
    "_DENY_SENTINEL_PREFIX",
    "_EVALUATE_HOOK_ELICITATION_ID_RE",
    "_EXTERNAL_ASSISTANT_MESSAGE_TYPE",
    "_EXTERNAL_CODEX_COLLABORATION_MODE_CHANGE_TYPE",
    "_EXTERNAL_CODEX_SUBAGENT_START_TYPE",
    "_EXTERNAL_COMPACTION_STATUS_TYPE",
    "_EXTERNAL_COMPACTION_STATUS_VALUES",
    "_EXTERNAL_CONVERSATION_ITEM_TYPE",
    "_EXTERNAL_ELICITATION_RESOLVED_TYPE",
    "_EXTERNAL_MCP_STARTUP_STATUS_VALUES",
    "_EXTERNAL_MCP_STARTUP_TYPE",
    "_EXTERNAL_MODEL_CHANGE_TYPE",
    "_EXTERNAL_MODEL_OPTIONS_TYPE",
    "_EXTERNAL_OUTPUT_REASONING_DELTA_TYPE",
    "_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE",
    "_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE",
    "_EXTERNAL_SESSION_INTERRUPTED_TYPE",
    "_EXTERNAL_SESSION_STATUS_TYPE",
    "_EXTERNAL_SESSION_STATUS_VALUES",
    "_EXTERNAL_SESSION_SUPERSEDED_TYPE",
    "_EXTERNAL_SESSION_TODOS_TYPE",
    "_EXTERNAL_SESSION_USAGE_TYPE",
    "_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT",
    "_EXTERNAL_SUBAGENT_START_TYPE",
    "_EXTERNAL_TOOL_OUTPUT_DELTA_TYPE",
    "_FENCE_EXEMPT_EVENT_TYPES",
    "_FORK_HISTORY_NATIVE_HARNESSES",
    "_HARNESS_ELICITATION_REPARK_GRACE_S",
    "_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES",
    "_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S",
    "_HOOK_ELICITATION_ID_RE",
    "_HOST_BOUND_RUNNER_CONNECT_GRACE_S",
    "_HOST_LAUNCH_RESULT_TIMEOUT_S",
    "_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S",
    "_HOST_RUNNER_STATUS_TIMEOUT_S",
    "_INTERRUPT_TYPE",
    "_KIRO_NATIVE_WRAPPER_LABEL_VALUE",
    "_LABEL_VALUE_MAX_LEN",
    "_LAST_CONTEXT_TOKENS_LABEL_KEY",
    "_LAST_CONTEXT_WINDOW_LABEL_KEY",
    "_LAST_TASK_ERROR_CODE_LABEL_KEY",
    "_LAST_TASK_ERROR_MESSAGE_LABEL_KEY",
    "_MANAGED_RESUMABLE_TUNNEL_STALE_S",
    "_MAX_TERMINAL_LAUNCH_ARGS",
    "_MAX_TERMINAL_LAUNCH_ARG_LEN",
    "_MCP_ELICITATION_TYPE",
    "_MODEL_OPTIONS_ENDPOINT_BY_WRAPPER",
    "_MODEL_OPTIONS_RETRY_DELAYS_S",
    "_MODEL_TOKEN_KEYS",
    "_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
    "_NATIVE_POLICY_NOT_ENFORCED_CODE",
    "_NATIVE_TERMINAL_ENSURE_FAILED_CODE",
    "_NATIVE_TERMINAL_START_FAILED_CODE",
    "_OPENCODE_NATIVE_WRAPPER_LABEL_VALUE",
    "_PI_NATIVE_WRAPPER_LABEL_VALUE",
    "_RACE_TASK_REAP_TIMEOUT_S",
    "_RUNNER_CONVICTION_POLL_S",
    "_RUNNER_FORWARD_TIMEOUT",
    "_RUNNER_RELAY_READY_TIMEOUT_S",
    "_RUNNER_SESSION_INIT_TIMEOUT_S",
    "_SERVER_STREAM_EVENT_ADAPTER",
    "_SESSION_STREAM_HEARTBEAT_INTERVAL_S",
    "_SESSION_UPDATES_HEARTBEAT_INTERVAL_S",
    "_SESSION_UPDATES_MAX_WATCHED",
    "_SESSION_UPDATES_RESCAN_INTERVAL_S",
    "_SHARED_DISCOVERY_KEY",
    "_SLASH_COMMAND_TYPE",
    "_SNAPSHOT_RUNNER_TIMEOUT_S",
    "_STOP_RUNNER_RESULT_TIMEOUT_S",
    "_STOP_SESSION_TYPE",
    "_SUBAGENT_FORWARD_RECONNECT_WAIT_S",
    "_TERMINAL_RESPONSE_EVENT_TYPES",
    "_TURN_ACTOR_LABEL",
    "_UI_ADDED_AGENT_TITLE_PREFIX",
    "_UPLOAD_READ_CHUNK_BYTES",
    "_WATCHER_TASKS",
    "_MirroredToolCall",
    "_PendingPolicyAskWrites",
    "_RelayHandle",
    "_browser_action_claims",
    "_browser_action_owners",
    "_browser_action_registry",
    "_deferred_elicitation_clear_tasks",
    "_intentional_stop_sessions",
    "_interrupt_fenced_sessions",
    "_logger",
    "_managed_launch_tasks",
    "_model_options_cache",
    "_model_options_inflight",
    "_native_ask_gate_locks",
    "_native_popup_forward_tasks",
    "_pending_policy_ask_writes",
    "_pushed_model_options_cache",
    "_read_explicit_unread",
    "_read_last_seen",
    "_recent_mirrored_tool_calls",
    "_runner_relay_tasks",
    "_runner_skills_cache",
    "_runner_skills_inflight",
    "_server_runner_router",
    "_session_active_response_cache",
    "_session_background_task_count_cache",
    "_session_mcp_startup_cache",
    "_session_sandbox_status_cache",
    "_session_status_cache",
    "_session_terminal_pending_cache",
    "_session_todos_cache",
    "get_server_runner_router",
    "set_server_runner_router",
]


# --- Facade-delegating proxies for runtime bindings patched on the router ---
#
# Tests (and callers) patch these on the historical ``sessions`` facade module,
# e.g. ``monkeypatch.setattr("...routes.sessions.get_agent_cache", fake)``. The
# sibling ``_sessions`` modules resolve the names in their OWN namespace, so a
# facade-level patch would miss them. These proxies resolve the facade attribute
# lazily on every access, so a patch on the facade is honoured everywhere the
# siblings import the name from here. Deliberately NOT in ``__all__`` so the
# facade's ``import *`` never overwrites its real runtime bindings.


def _sessions_facade() -> Any:
    """Return the historical ``sessions`` router module (resolved lazily)."""
    import omnigent.server.routes.sessions as facade

    return facade


class _FacadeCallable:
    """Callable that forwards to the same-named attribute on the facade."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return getattr(_sessions_facade(), self._name)(*args, **kwargs)


class _FacadeAttrProxy:
    """Attribute-access proxy forwarding to the facade's same-named object."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(getattr(_sessions_facade(), self._name), attr)


class _FacadeValue:
    """Comparison proxy for a plain value read off the facade lazily."""

    def __init__(self, name: str) -> None:
        self._name = name

    def _value(self) -> Any:
        return getattr(_sessions_facade(), self._name)

    def __eq__(self, other: object) -> bool:
        return bool(self._value() == other)

    def __ne__(self, other: object) -> bool:
        return bool(self._value() != other)

    def __hash__(self) -> int:
        return hash(self._value())

    def __str__(self) -> str:
        return str(self._value())


get_agent_cache = _FacadeCallable("get_agent_cache")
build_policy_engine = _FacadeCallable("build_policy_engine")
get_caps = _FacadeCallable("get_caps")
session_stream = _FacadeAttrProxy("session_stream")
user_session_stream = _FacadeAttrProxy("user_session_stream")
_ELICITATION_MODE = _FacadeValue("_ELICITATION_MODE")
