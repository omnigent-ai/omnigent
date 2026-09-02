"""Runner-local tool dispatch for intercepted action_required events.

Per designs/RUNNER_TOOL_DISPATCH.md, the runner dispatches most tools
locally and relays action_required events upstream UNCHANGED for
visibility (the executor emits ToolCallInProgress/ToolCallObserved for
the REPL but doesn't dispatch itself — it checks should_dispatch_locally
and skips).

Tool categories:
- _OS_ENV_TOOLS: execute through a runner-local OSEnvironment (sys_os_*)
- _REST_TOOLS: call server REST APIs (sys_call_async, sys_cancel_async)
- _FILE_TOOLS: call server file APIs (sys_upload/download/list_files)
- _TERMINAL_TOOLS: runner-local TerminalRegistry
- MCP tools: spec-defined; dispatched via RunnerMcpManager passed
  in by proxy_stream (designs/RUNNER_MCP.md). Not in the static
  allow-list because names vary per spec.
- Client-side tools: tunneled via REPL (deferred)
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import mimetypes
import os
import re
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from omnigent.json_types import JsonObject as _JsonObject

if TYPE_CHECKING:
    from omnigent.inner.datamodel import OSEnvSpec
    from omnigent.inner.os_env import OSEnvironment
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.spec.types import AgentSpec, SkillSpec
    from omnigent.terminals.registry import TerminalRegistry

import httpx

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
)
from omnigent.debug_logging import runner_primary_session_id
from omnigent.harness_aliases import canonicalize_harness
from omnigent.model_override import (
    harness_supports_model_override,
    model_family_mismatch,
    normalize_model_for_provider,
    validate_model_override,
)
from omnigent.native_coding_agents import public_agent_name
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import (
    CLOSED_LABEL_KEY,
    CLOSED_LABEL_VALUE,
    is_session_closed,
    title_without_closed_marker,
)
from omnigent.tools import ToolManager
from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments
from omnigent.tools.builtins.async_inbox import (
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysCancelTaskTool,
    SysReadInboxTool,
)
from omnigent.tools.builtins.download_file import DownloadFileTool
from omnigent.tools.builtins.list_comments import ListCommentsTool
from omnigent.tools.builtins.os_env import (
    SysOsEditTool,
    SysOsReadTool,
    SysOsShellTool,
    SysOsWriteTool,
)
from omnigent.tools.builtins.session_rename import SysSessionRenameTool
from omnigent.tools.builtins.spawn import (
    # Shared contract values with the in-process sys_session_* tools. Imported
    # (not duplicated) so the runner's REST-backed peek clamps to the same
    # bounds the LLM-facing tool schema advertises and tombstones with the
    # same marker the in-process close writes.
    _ACTIVITY_MAX_CHARS,
    _CLOSED_TITLE_INFIX,
    _HISTORY_DEFAULT_TAIL,
    _bound_history_content_chars,
    _clamp_history_content_chars,
    _clamp_tail_items,
)
from omnigent.tools.builtins.sys_terminal import (
    SysTerminalCloseTool,
    SysTerminalLaunchTool,
    SysTerminalListTool,
    SysTerminalReadTool,
    SysTerminalSendTool,
)
from omnigent.tools.builtins.timer import (
    # Shared with the in-process sys_timer_set tool so the runner's firing
    # loop validates the same argument shape and delay ceiling the LLM-facing
    # schema advertises.
    validate_timer_set_args,
)
from omnigent.tools.builtins.update_comment import UpdateCommentTool
from omnigent.tools.builtins.upload_file import UploadFileTool, safe_resolve

_logger = logging.getLogger(__name__)

_EventPublisher = Callable[[str, _JsonObject], None]


class _DynamicCallable(Protocol):
    """Callable loaded from an agent spec's dotted Python path."""

    def __call__(self, **kwargs: object) -> object:
        raise NotImplementedError


class _AsyncDynamicCallable(Protocol):
    """Async callable loaded from an agent spec's dotted Python path."""

    def __call__(self, **kwargs: object) -> Awaitable[object]:
        raise NotImplementedError


def _string_object_dict(value: object) -> _JsonObject | None:
    """Return *value* as a string-keyed object mapping when valid."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast("_JsonObject", value)


def _optional_string(value: object) -> str | None:
    """Return *value* when it is a string, otherwise ``None``."""
    return value if isinstance(value, str) else None


def _json_object_list(value: object) -> list[_JsonObject]:
    """Return the object entries from a JSON array."""
    if not isinstance(value, list):
        return []
    objects: list[_JsonObject] = []
    for entry in value:
        parsed = _string_object_dict(entry)
        if parsed is not None:
            objects.append(parsed)
    return objects


def _string_mapping(value: object) -> dict[str, str] | None:
    """Return the string entries from a mapping-like JSON object."""
    if not isinstance(value, dict):
        return None
    return {
        key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)
    }


_INBOX_OUTPUT_MAX_CHARS = 12000
_OS_ENV_SHELL_DEFAULT_TIMEOUT_S = 120.0
_RUNNER_EXECUTION_TIMEOUT_S = 7200.0
_SUBAGENT_POLICY_STATUSES = frozenset({"completed", "failed"})
_SUBAGENT_INBOX_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SUBAGENT_POLICY_FAILURE_OUTPUT = "[Result suppressed by policy: policy evaluation failed]"
_SESSION_WRAPPER_LABEL_KEY = "omnigent.wrapper"
# Read budget for runner→server message-send POSTs that are gated at the
# recipient's REQUEST phase, which can PARK behind a human-approval ASK gate
# (e.g. session_cost_budget) for the deciding policy's ``ask_timeout``. Held at
# one day (86400s) — matching that default — so the send WAITS for the verdict
# instead of severing the parked gate at a short read timeout (a 30s cut
# previously fail-closed to DENY). Fast connect (30s) so an unreachable server
# still fails out promptly. Guarded by tests/test_ask_timeout_infinite.py.
_ASK_GATE_DELIVERY_READ_TIMEOUT_S: float = 86400.0
_ASK_GATE_DELIVERY_TIMEOUT = httpx.Timeout(_ASK_GATE_DELIVERY_READ_TIMEOUT_S, connect=30.0)

# Read timeouts for the two MCP-proxy hops that carry a tool call back to the
# runner (runner → Omnigent server → runner). ``sys_os_shell`` accepts caller-provided
# timeouts, so these must sit above the runner's execution timeout rather than
# only above the 120-second shell default. Keep the outer hop larger so the
# AP→runner leg fails first with the more specific error when the proxy wedges.
MCP_PROXY_FORWARD_TIMEOUT_S = _RUNNER_EXECUTION_TIMEOUT_S + 30.0
MCP_PROXY_CALL_TIMEOUT_S = _RUNNER_EXECUTION_TIMEOUT_S + 60.0


@dataclass(frozen=True)
class _CancelAsyncToolResult:
    """
    Internal result for local async-task cancellation.

    :param output: Tool output string to return to the LLM.
    :param try_subagent_cancel: Whether no local async task matched,
        so ``sys_cancel_task`` should try the sub-agent work registry
        next.
    """

    output: str
    try_subagent_cancel: bool = False


@dataclass(frozen=True)
class _SubagentInboxEvaluation:
    """
    Result of delayed sub-agent output policy evaluation.

    :param payload: Payload safe to format for ``sys_read_inbox``.
        On fail-closed paths this contains a policy-failure sentinel
        instead of the raw child output.
    :param retry_original: Whether policy evaluation failed before
        producing a terminal verdict, so the original payload should
        be requeued for a future drain attempt.
    """

    payload: _JsonObject
    retry_original: bool = False


# ── Tool sets (Phase 0 reorganization) ─────────────────────
# Use class .name() methods where available for single-source-of-truth.

# Priority 5a: OS env tools — runner-local OSEnvironment-backed execution.
_OS_ENV_TOOLS = frozenset(
    {
        SysOsReadTool.name(),
        SysOsWriteTool.name(),
        SysOsEditTool.name(),
        SysOsShellTool.name(),
    }
)

# Priority 5b: REST-backed tools — runner calls server REST APIs.
# (sys_call_async / sys_cancel_async moved to _ASYNC_INBOX_TOOLS)
_REST_TOOLS: frozenset[str] = frozenset()

# Priority 5c: File tools — runner calls server file APIs.
_FILE_TOOLS = frozenset(
    {
        UploadFileTool.name(),
        DownloadFileTool.name(),
        "list_files",  # from builtins registry; no standalone class
    }
)

# Priority 5d: Terminal tools — runner-local TerminalRegistry.
_TERMINAL_TOOLS = frozenset(
    {
        SysTerminalLaunchTool.name(),
        SysTerminalSendTool.name(),
        SysTerminalReadTool.name(),
        SysTerminalListTool.name(),
        SysTerminalCloseTool.name(),
    }
)

# Priority 5e: Async inbox tools — runner-local, backed by
# per-session asyncio queues (SESSION_REARCHITECTURE Step 7).
_ASYNC_INBOX_TOOLS = frozenset(
    {
        SysCallAsyncTool.name(),
        SysReadInboxTool.name(),
        SysCancelAsyncTool.name(),
    }
)

# Priority 5f: Sub-agent tools. ``sys_session_send`` creates or
# continues child sessions. The read-only observability helpers
# (peek/list/close) dispatch via ``_SESSION_QUERY_TOOLS`` below.
_SUBAGENT_TOOLS = frozenset({"sys_session_send"})
_TURN_ACTOR_LABEL = "omnigent.turn_actor"

# Priority 5f.0a: Session-create write. ``sys_session_create`` spawns a
# child session (parent forced to the caller) from an existing agent_id
# via the JSON POST /v1/sessions create — same server-permission posture
# as _execute_subagent_tool.
_SESSION_CREATE_TOOLS = frozenset({"sys_session_create"})

# Priority 5f.0: Session query tools — peek/list/close/get_info/share. The
# runner has no in-process ConversationStore, so these read/mutate session
# state via the Omnigent server's existing REST endpoints (GET /items, GET
# /child_sessions, GET /sessions/{id}, PATCH /sessions/{id}, PUT
# /sessions/{id}/permissions) over server_client — same channel and security
# posture as _execute_subagent_tool / _execute_comment_tool.
_SESSION_QUERY_TOOLS = frozenset(
    {
        "sys_session_get_history",
        "sys_session_list",
        "sys_session_close",
        "sys_session_get_info",
        "sys_session_share",
    }
)

_SESSION_SELF_WRITE_TOOLS = frozenset({SysSessionRenameTool.name()})

# The title bound the rename tool advertises to the LLM — read once from the
# tool schema so the dispatcher can never drift from the published contract.
_SESSION_RENAME_TITLE_MAX_CHARS: int = SysSessionRenameTool().get_schema()["function"][
    "parameters"
]["properties"]["title"]["maxLength"]

# Grantee sentinel for an anonymous, public read-only share. Mirrors the
# server's RESERVED_USER_PUBLIC; only specs with
# ``agent_session_sharing: public`` may grant it (enforced in
# _session_share_via_rest — the server can't see the agent's sharing
# policy, so the runner is the gate).
_PUBLIC_USER_SENTINEL = "__public__"

# Spec ``agent_session_sharing:`` policy values
# (omnigent.spec.types.SharePolicy) that enable the sys_session_share
# tool. Compared as plain strings since SharePolicy is a str-enum;
# anything else (incl. "none"/absent) is off.
_SHARE_ENABLED_POLICIES = frozenset({"non-public", "public"})
_SHARE_PUBLIC_POLICY = "public"

# Priority 5f.1: web_fetch — translates the LLM-facing query/url
# arguments into a sys_session_send call against the built-in
# ``__web_researcher`` sub-agent, then reuses
# ``_execute_subagent_tool``.
_WEB_FETCH_TOOLS = frozenset({"web_fetch"})

# Priority 5f.1b: web_search — the first-party search builtin. Runner-local
# so non-OpenAI-harness web_search calls resolve to the spec's configured
# backend (google / perplexity / nimble) via WebSearchTool.invoke.
# (OpenAI Responses-compatible harnesses use the native web_search_preview
# passthrough and never reach this path.) Without this entry the call fell
# through to the spec-callable branch and errored "tool unavailable".
_WEB_SEARCH_TOOLS = frozenset({"web_search"})

# nimble_research — Nimble Agent API v2 research runs (start → poll → result).
# Runner-local so a non-OpenAI model's nimble_research call resolves to
# NimbleResearchTool.invoke, the same posture as web_search.
_NIMBLE_RESEARCH_TOOLS = frozenset({"nimble_research"})

# nimble_extract — Nimble Extract Templates (template run → structured JSON).
# Runner-local for the same reason.
_NIMBLE_EXTRACT_TOOLS = frozenset({"nimble_extract"})

# Hindsight long-term memory builtins. Runner-local (like web_search) so that a
# wrapped harness's (claude-sdk / codex / cursor / pi) tool call resolves to the
# spec-configured Hindsight tool via its ``invoke``. Without this entry the call
# falls through to the harness, which has no such tool, and silently no-ops.
_HINDSIGHT_TOOLS = frozenset({"hindsight_retain", "hindsight_recall", "hindsight_reflect"})

# Priority 5f.2: sys_list_models — runner-local because provider resolution
# reads the runner host's config/credentials, same as the spawn paths.
_LIST_MODELS_TOOLS = frozenset({"sys_list_models"})

# Priority 5g: Timer tools — runner-local asyncio.sleep tasks
# (RUNNER_TIMER_DISPATCH.md).
_TIMER_TOOLS = frozenset({"sys_timer_set", "sys_timer_cancel"})

# Priority 5f.3: sys_advise_models — server-side via MCP intercept;
# included in the tool surface only when smart routing is enabled.
_ADVISE_MODELS_TOOLS = frozenset({"sys_advise_models"})

# Priority 5h: Task lifecycle tools — runner-local sys_cancel_task.
# The only cancellable task ids visible to the LLM are async dispatches
# and sub-agent handles; observation happens through sys_read_inbox.
_TASK_LIFECYCLE_TOOLS = frozenset(
    {
        SysCancelTaskTool.name(),
    }
)

# Priority 5i: Skill tools — load_skill and read_skill_file.
# Dispatched locally in the runner so harness subprocesses can
# call them via the action_required → dispatch_tool_locally path.
_SKILL_TOOLS = frozenset({"load_skill", "read_skill_file"})

# Priority 5j: Comment tools — list_comments and update_comment.
# Auto-registered by ToolManager. The runner has no in-process
# CommentStore, so _execute_comment_tool uses server_client REST
# calls (GET/PATCH /v1/sessions/{id}/comments) instead.
_COMMENT_TOOLS = frozenset(
    {
        ListCommentsTool.name(),
        UpdateCommentTool.name(),
    }
)

# Priority 5k: Agent-management reads — sys_agent_get / sys_agent_download /
# sys_agent_list. The runner has no in-process AgentStore/ArtifactStore, so
# these proxy the Omnigent server's REST endpoints (GET /v1/sessions/{id}/agent,
# .../agent/contents, GET /v1/agents, GET /v1/sessions) over server_client.
# sys_agent_download writes the bundle bytes into the agent's local os_env
# cwd so sys_os_* can read it; sys_agent_list also scans that cwd for
# locally-authored configs.
_AGENT_TOOLS = frozenset({"sys_agent_get", "sys_agent_download", "sys_agent_list"})

# Priority 5l: Policy management — sys_add_policy.
# The runner proxies the Omnigent server's session policy REST endpoint.
_POLICY_TOOLS = frozenset({"sys_add_policy", "sys_policy_registry"})

# Priority 5l.1: Scheduled-task management — the runner proxies the Omnigent
# server's /v1/scheduled-tasks REST endpoints (same posture as _POLICY_TOOLS).
_SCHEDULED_TASK_TOOLS = frozenset(
    {
        "sys_scheduled_task_create",
        "sys_scheduled_task_list",
        "sys_scheduled_task_update",
        "sys_scheduled_task_delete",
    }
)

# Priority 5m: Embedded-browser tools.
# Runner dispatch POSTs a blocking action request to the server, which parks a
# Future + publishes ``browser.action_request`` on the session stream; the
# Omnigent desktop renderer claims and executes the action, then POSTs the
# result back. Execution lives HERE (not in Tool.invoke) because the browser
# protocol needs the runner's ``server_client`` and ``ToolContext`` carries
# none. See omnigent/tools/builtins/browser.py for the schema-only classes.
_BROWSER_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    }
)

# Runner-side outer HTTP read timeout for a browser action POST. The read
# budget (60s) MUST exceed the server-side browser-action await (30s) so the
# runner never severs the still-open POST before the server returns either the
# action result JSON or the clean timeout-error JSON. Fast connect (30s) so an
# unreachable server still fails promptly.
_BROWSER_ACTION_TIMEOUT = httpx.Timeout(60.0, connect=30.0)

# Returned as the tool output (HTTP 200 body, not an exception) when the server
# browser-action await elapses with no renderer result — a clear
# "is the session open?" message so the LLM gets a clean, actionable error.
_BROWSER_TIMEOUT_ERROR = (
    '{"error": "browser action timed out — is the session open in the Omnigent desktop app?"}'
)

# Builtin tools the claude-native / codex-native relay advertises to the
# real CLI, beyond the always-relayed ``sys_os_*`` family. Native harnesses
# ignore the harness ``tools`` list, so the relay is their ONLY tool
# surface; this set is the runner-/server-proxied builtin surface that
# rides through the Omnigent ``/mcp`` endpoint (comment, session read/write,
# async inbox, task lifecycle, agent-discovery, and terminal families —
# the same dispatch posture non-native harnesses get via
# ``request.tools``). ``sys_terminal_*`` inherits the spec gate for
# free: the relay only advertises names that ``ToolManager(spec)``
# actually registered, and terminal tools register only when the spec
# declares a non-empty ``terminals:`` block.
# ``sys_os_*`` is intentionally excluded: the
# bridge exposes static ``sys_os_*`` tools and the relay overrides them
# unconditionally for policy enforcement (independent of the spec's
# ``os_env`` gate), so the native relay assembles them separately.
_NATIVE_RELAY_BUILTIN_TOOLS = (
    _COMMENT_TOOLS
    | _SESSION_QUERY_TOOLS
    | _SESSION_SELF_WRITE_TOOLS
    | _ASYNC_INBOX_TOOLS
    | _SUBAGENT_TOOLS
    | _LIST_MODELS_TOOLS
    | _ADVISE_MODELS_TOOLS
    | _SESSION_CREATE_TOOLS
    | _TASK_LIFECYCLE_TOOLS
    | _AGENT_TOOLS
    | _POLICY_TOOLS
    | _SCHEDULED_TASK_TOOLS
    | _TERMINAL_TOOLS
    # ``browser_*`` must ride the native relay: the Omnigent desktop app
    # runs native (claude/codex/pi) sessions, which ignore ``request.tools``
    # and see ONLY this relay surface — without this union member the
    # feature is dead for its real target. The relay still filters
    # ``ToolManager(spec).get_tool_schemas()``, so browser schemas appear
    # only when the spec declares the builtins (see builtins/__init__.py).
    | _BROWSER_TOOLS
    # Memory builtins are relayed to native harnesses too — unlike web_search,
    # native harnesses have no built-in long-term memory of their own.
    | _HINDSIGHT_TOOLS
    # Skills ride the relay for the same reason memory does: ``load_skill`` is
    # what discovers host-scope skills (``.agents/skills`` and friends), and a
    # native session's only tool surface is this relay.
    | _SKILL_TOOLS
)


def build_native_relay_tool_schemas(spec: AgentSpec | None) -> list[_JsonObject]:
    """Build the flat Omnigent tool surface for native harness bridges.

    Returns the same tool set the claude-native / codex-native relay advertises
    and that pi-native registers via ``pi.registerTool``: the spec-gated builtin
    surface (``_NATIVE_RELAY_BUILTIN_TOOLS`` — comment, session read/write,
    agent-discovery, policy, and terminal families) plus the ``sys_os_*`` tools,
    relayed unconditionally so they override any harness-static versions and get
    centralized policy enforcement on the Omnigent server.

    Each entry is a flat ``{"name", "description", "parameters"}`` dict (the
    ``"function"`` sub-dict of an OpenAI tool schema), which is exactly what
    ``pi.registerTool`` and the claude-native relay both consume.

    :param spec: The session's resolved agent spec. ``None`` falls back to the
        always-on read/discovery surface (never the opt-in spawn writes, whose
        gate can't be evaluated without the spec), mirroring the relay.
    :returns: Flat tool schemas for native bridges.
    """
    from omnigent.tools.builtins.agents import (
        SysAgentDownloadTool,
        SysAgentGetTool,
        SysAgentListTool,
    )
    from omnigent.tools.builtins.list_comments import ListCommentsTool
    from omnigent.tools.builtins.os_env import (
        SysOsEditTool,
        SysOsReadTool,
        SysOsShellTool,
        SysOsWriteTool,
    )
    from omnigent.tools.builtins.spawn import (
        SysSessionGetHistoryTool,
        SysSessionGetInfoTool,
        SysSessionListTool,
    )
    from omnigent.tools.builtins.update_comment import UpdateCommentTool

    schemas: list[_JsonObject] = []

    def _append(function_dict: _JsonObject) -> None:
        name = function_dict.get("name")
        if not isinstance(name, str):
            return
        description = function_dict.get("description")
        parameters = _string_object_dict(function_dict.get("parameters")) or {
            "type": "object",
            "properties": {},
        }
        schemas.append(
            {
                "name": name,
                "description": description if isinstance(description, str) else "",
                "parameters": parameters,
            }
        )

    if spec is not None:
        from omnigent.tools.manager import ToolManager

        for schema in ToolManager(spec).get_tool_schemas():
            function = _string_object_dict(schema.get("function"))
            if function is not None and function.get("name") in _NATIVE_RELAY_BUILTIN_TOOLS:
                _append(function)
    else:
        from omnigent.tools.builtins.policy import SysAddPolicyTool, SysPolicyRegistryTool

        for _cls in (
            ListCommentsTool,
            UpdateCommentTool,
            SysSessionListTool,
            SysSessionGetHistoryTool,
            SysSessionGetInfoTool,
            SysSessionRenameTool,
            SysAgentGetTool,
            SysAgentListTool,
            SysAgentDownloadTool,
            SysAddPolicyTool,
            SysPolicyRegistryTool,
        ):
            fallback_schema = _string_object_dict(_cls().get_schema())
            if fallback_schema is None:
                continue
            function = _string_object_dict(fallback_schema.get("function"))
            if function is not None:
                _append(function)

    # OS tools (sys_os_*), relayed unconditionally to override any harness-static
    # versions and centralize policy enforcement. Create a minimal OSEnvironment
    # purely for schema extraction.
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.os_env import create_os_environment

    _os_spec = OSEnvSpec(
        type="caller_process",
        cwd=str(Path.cwd()),
        sandbox=OSEnvSandboxSpec(type="none"),
        fork=False,
    )
    try:
        _os_env = create_os_environment(_os_spec)
        if _os_env is None:
            raise RuntimeError("OSEnvironment factory returned None")
        try:
            for tool in (
                SysOsReadTool(_os_env),
                SysOsWriteTool(_os_env),
                SysOsEditTool(_os_env),
                SysOsShellTool(_os_env),
            ):
                tool_schema = _string_object_dict(tool.get_schema())
                function = (
                    _string_object_dict(tool_schema.get("function")) if tool_schema else None
                )
                if function is not None:
                    _append(function)
        finally:
            _os_env.close()
    except Exception:  # noqa: BLE001 — OS env setup is best-effort for schema only
        _logger.debug(
            "Could not create OSEnvironment for native relay OS tool schemas",
            extra={"session_id": runner_primary_session_id()},
        )

    return schemas


# sys_agent_list: locally-authored agent config YAMLs live under this
# subdirectory of the agent's os_env cwd, so the list tool can find them
# and the agent can read/edit them via sys_os_* (configs are authored with
# sys_os_write, e.g. following the ``build-omnigent`` skill).
_AGENT_CONFIG_SUBDIR = ".omnigent/agent-configs"

# Broad internal page size for discovery fan-out reads.
_AGENT_LIST_PAGE_LIMIT = 1000

_DISCOVERY_LIST_MAX_LIMIT = 100
# Match the existing default ceiling for ``sys_os_shell`` output. Discovery
# tools stay below the same Omnigent-owned budget instead of guessing a
# harness-specific limit.
_DISCOVERY_LIST_OUTPUT_MAX_CHARS = 100_000
_DISCOVERY_CURSOR_MAX_CHARS = 40_000
_DISCOVERY_START = "start"
_DISCOVERY_AT = "at"
_DISCOVERY_END = "end"
_DiscoveryState = tuple[str, str | None]


def _discovery_list_window(
    args: _JsonObject,
    tool_name: str,
    page_sections: tuple[str, ...],
    filters: _JsonObject,
) -> tuple[int | None, dict[str, _DiscoveryState], bool] | str:
    """Validate discovery pagination and decode its opaque continuation cursor."""
    limit = args.get("limit")
    if "limit" in args and (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= _DISCOVERY_LIST_MAX_LIMIT
    ):
        return json.dumps(
            {
                "error": (
                    f"{tool_name}: 'limit' must be an integer between 1 and "
                    f"{_DISCOVERY_LIST_MAX_LIMIT}"
                )
            }
        )
    cursor = args.get("cursor")
    state: dict[str, _DiscoveryState] = dict.fromkeys(page_sections, (_DISCOVERY_START, None))
    if cursor is None:
        return cast(int | None, limit), state, False
    if not isinstance(cursor, str) or not cursor:
        return json.dumps({"error": f"{tool_name}: 'cursor' must be a non-empty string"})
    if len(cursor) > _DISCOVERY_CURSOR_MAX_CHARS:
        return json.dumps({"error": f"{tool_name}: pagination cursor is too long"})
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
    if not isinstance(payload, dict) or set(payload) != {"v", "tool", "filters", "sections"}:
        return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
    if payload.get("v") != 1:
        return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
    if payload.get("tool") != tool_name:
        return json.dumps({"error": f"{tool_name}: cursor was minted by a different tool"})
    if payload.get("filters") != filters:
        return json.dumps({"error": f"{tool_name}: cursor uses different filter arguments"})
    sections = payload.get("sections")
    if not isinstance(sections, dict) or set(sections) != set(page_sections):
        return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
    for name in state:
        value = sections.get(name)
        if not isinstance(value, list) or len(value) != 2:
            return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
        tag, position = value
        if tag in {_DISCOVERY_START, _DISCOVERY_END}:
            if position is not None:
                return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
        elif tag == _DISCOVERY_AT:
            if not isinstance(position, str) or not position:
                return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
        else:
            return json.dumps({"error": f"{tool_name}: invalid pagination cursor"})
        state[name] = (tag, cast(str | None, position))
    return cast(int | None, limit), state, True


def _encode_discovery_cursor(
    tool_name: str,
    filters: _JsonObject,
    state: dict[str, _DiscoveryState],
) -> str | None:
    """Serialize a discovery continuation cursor without exposing its shape."""
    payload = json.dumps(
        {
            "v": 1,
            "tool": tool_name,
            "filters": filters,
            "sections": {name: list(value) for name, value in state.items()},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    cursor = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return cursor if len(cursor) <= _DISCOVERY_CURSOR_MAX_CHARS else None


@dataclass(frozen=True)
class _DiscoveryPage:
    """Rows and continuation state from one discovery source."""

    rows: list[_JsonObject]
    has_more: bool
    next_after: str | None = None
    failed: bool = False


def _parse_discovery_page(body: object) -> _DiscoveryPage:
    """Validate the server envelope before trusting its continuation state."""
    if not isinstance(body, dict):
        return _DiscoveryPage([], False, failed=True)
    data = body.get("data")
    if not isinstance(data, list):
        return _DiscoveryPage([], False, failed=True)
    rows: list[_JsonObject] = []
    for row in data:
        if not isinstance(row, dict):
            return _DiscoveryPage([], False, failed=True)
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            return _DiscoveryPage([], False, failed=True)
        rows.append(row)
    has_more = body.get("has_more", False)
    last_id = body.get("last_id")
    if not isinstance(has_more, bool):
        return _DiscoveryPage([], False, failed=True)
    if last_id is not None and (not isinstance(last_id, str) or not last_id):
        return _DiscoveryPage([], False, failed=True)
    if has_more and last_id is None:
        return _DiscoveryPage([], False, failed=True)
    return _DiscoveryPage(rows, has_more, cast(str | None, last_id))


def _bounded_discovery_result(
    sections: dict[str, list[_JsonObject]],
    *,
    limit: int | None,
    cursor_state: dict[str, _DiscoveryState],
    continued: bool,
    source_pages: dict[str, _DiscoveryPage],
    page_sections: tuple[str, ...] | None = None,
    tool_name: str,
    filters: _JsonObject,
) -> str:
    """Keep a small legacy result intact, otherwise return a fitting page."""
    complete = json.dumps(sections)
    if (
        limit is None
        and not continued
        and not any(page.failed for page in source_pages.values())
        and not any(page.has_more for page in source_pages.values())
        and len(complete) <= _DISCOVERY_LIST_OUTPUT_MAX_CHARS
    ):
        return complete

    requested_limit = limit or _DISCOVERY_LIST_MAX_LIMIT
    paged = page_sections or tuple(sections)

    def _candidate(candidate_limit: int) -> str | None:
        page = dict(sections)
        has_more: dict[str, bool] = {}
        next_state = dict(cursor_state)
        for name in paged:
            rows = sections[name]
            page_rows = rows[:candidate_limit]
            page[name] = page_rows
            source_page = source_pages[name]
            if source_page.failed:
                # A failed read proves neither progress nor exhaustion. Keep
                # the incoming position so the continuation retries it.
                has_more[name] = True
                continue
            has_more[name] = len(rows) > candidate_limit or source_page.has_more
            if not has_more[name]:
                next_state[name] = (_DISCOVERY_END, None)
            elif page_rows:
                if name == "local_configs":
                    position = _optional_string(page_rows[-1].get("path"))
                else:
                    identifier = "agent_id" if name == "builtins" else "session_id"
                    position = _optional_string(page_rows[-1].get(identifier))
                if position is not None:
                    next_state[name] = (_DISCOVERY_AT, position)
            elif source_page.has_more and source_page.next_after is not None:
                next_state[name] = (_DISCOVERY_AT, source_page.next_after)
        metadata: dict[str, object] = {
            "limit": candidate_limit,
            "has_more": has_more,
        }
        if any(has_more.values()):
            cursor = _encode_discovery_cursor(tool_name, filters, next_state)
            if cursor is None:
                return None
            metadata["next_cursor"] = cursor
        return json.dumps({**page, "page": metadata})

    # Cursor size depends on the last returned row, so serialized page size is
    # not monotonic in the row limit. Check the bounded public range directly.
    for candidate_limit in range(requested_limit, 0, -1):
        candidate = _candidate(candidate_limit)
        if candidate is not None and len(candidate) <= _DISCOVERY_LIST_OUTPUT_MAX_CHARS:
            return candidate

    empty_page = _candidate(0)
    if empty_page is None:
        return json.dumps(
            {
                "error": (
                    f"Discovery continuation exceeds the {_DISCOVERY_CURSOR_MAX_CHARS}-character "
                    "cursor limit."
                )
            }
        )
    if len(empty_page) > _DISCOVERY_LIST_OUTPUT_MAX_CHARS:
        kind = "fixed_section"
        oversized_sections = [
            name for name, rows in sections.items() if name not in paged and rows
        ]
    else:
        kind = "paginated_row"
        oversized_sections = [name for name in paged if sections[name]]
    return json.dumps(
        {
            "error": (
                f"Discovery result exceeds the {_DISCOVERY_LIST_OUTPUT_MAX_CHARS}-character "
                "output limit even at the smallest page."
            ),
            "page": {"has_more": {name: source_pages[name].has_more for name in paged}},
            "oversized": {"kind": kind, "sections": oversized_sections},
        }
    )


# Union of all locally-dispatched tools.
_ALL_LOCAL_TOOLS = (
    _OS_ENV_TOOLS
    | _REST_TOOLS
    | _FILE_TOOLS
    | _TERMINAL_TOOLS
    | _ASYNC_INBOX_TOOLS
    | _SUBAGENT_TOOLS
    | _LIST_MODELS_TOOLS
    | _ADVISE_MODELS_TOOLS
    | _SESSION_CREATE_TOOLS
    | _SESSION_QUERY_TOOLS
    | _SESSION_SELF_WRITE_TOOLS
    | _WEB_FETCH_TOOLS
    | _WEB_SEARCH_TOOLS
    | _NIMBLE_RESEARCH_TOOLS
    | _NIMBLE_EXTRACT_TOOLS
    | _HINDSIGHT_TOOLS
    | _TIMER_TOOLS
    | _TASK_LIFECYCLE_TOOLS
    | _SKILL_TOOLS
    | _COMMENT_TOOLS
    | _AGENT_TOOLS
    | _POLICY_TOOLS
    | _SCHEDULED_TASK_TOOLS
)
_PLACEHOLDER_CWDS = (None, "", ".", "./")


def _event_item(event: _JsonObject) -> _JsonObject:
    """Return an event's item object, or an empty object when malformed."""
    return _string_object_dict(event.get("item")) or {}


def is_action_required(event: _JsonObject) -> bool:
    """Check if an SSE event is an action_required tool call."""
    if event.get("type") != "response.output_item.done":
        return False
    item = _event_item(event)
    return item.get("type") == "function_call" and item.get("status") == "action_required"


def get_tool_name(event: _JsonObject) -> str:
    """Extract the tool name from an action_required event."""
    name = _event_item(event).get("name")
    return name if isinstance(name, str) else ""


def get_call_id(event: _JsonObject) -> str:
    """Extract the call_id from an action_required event."""
    call_id = _event_item(event).get("call_id")
    return call_id if isinstance(call_id, str) else ""


def get_arguments(event: _JsonObject) -> str:
    """Extract the arguments JSON string from an action_required event."""
    arguments = _event_item(event).get("arguments")
    return arguments if isinstance(arguments, str) else "{}"


def should_dispatch_locally(tool_name: str) -> bool:
    """Return True if this tool should be dispatched by the runner locally.

    Used by BOTH the runner's proxy_stream (to decide whether to
    dispatch) AND the server-side executor (to skip its own dispatch
    for tools the runner already handled). The executor imports this
    function directly — Phase 5 of RUNNER_TOOL_DISPATCH.md.
    """
    return tool_name in _ALL_LOCAL_TOOLS


def _is_spec_local_python_tool(tool_name: str, agent_spec: AgentSpec | None) -> bool:
    local_tools = agent_spec.local_tools if agent_spec is not None else []
    return any(
        getattr(info, "name", None) == tool_name
        and getattr(info, "language", None) == "python"
        and getattr(info, "path", None)
        for info in local_tools
    )


async def _execute_local_python_tool(
    tool_name: str,
    args: str,
    *,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    runner_workspace: Path | None,
) -> str:
    if agent_spec is None:
        return f"Error: {tool_name} not in local dispatch table (no agent spec)"
    manager = ToolManager(agent_spec, workdir=runner_workspace)
    try:
        workspace = None
        if runner_workspace is not None and conversation_id is not None:
            workspace = runner_workspace / conversation_id
            workspace.mkdir(parents=True, exist_ok=True)
        ctx = ToolContext(
            task_id=task_id or conversation_id or "runner-local-tool",
            agent_id=agent_id or agent_spec.name or "runner-agent",
            workspace=workspace,
            conversation_id=conversation_id,
        )
        return await asyncio.to_thread(manager.call_tool, tool_name, args, ctx)
    except Exception as exc:
        _logger.exception(
            "runner local Python tool dispatch failed for %s",
            tool_name,
            extra={"session_id": conversation_id},
        )
        return f"Error: {type(exc).__name__}: {exc}"
    finally:
        manager.shutdown()


# Cache of resolved callables keyed by dotted path. Avoids
# re-importing on every invocation of the same tool.
_callable_cache: dict[str, _DynamicCallable] = {}


def _resolve_spec_callable(
    tool_name: str,
    agent_spec: AgentSpec | None,
) -> _DynamicCallable | str:
    """
    Look up a custom callable tool in the agent spec and resolve it.

    Returns the callable on success, or an error string on failure.
    Caches resolved callables in :data:`_callable_cache` so
    repeated invocations of the same tool skip the import.

    :param tool_name: Tool name from the LLM, e.g. ``"echo"``.
    :param agent_spec: The session's :class:`AgentSpec`. ``None``
        when no spec is available.
    :returns: The resolved callable, or an error string if the
        tool is not found or the import fails.
    """
    import importlib

    if agent_spec is None:
        return f"Error: {tool_name} not in local dispatch table (no agent spec)"
    local_tools = agent_spec.local_tools or []
    tool_info = next((lt for lt in local_tools if lt.name == tool_name), None)
    if tool_info is None or not tool_info.path:
        return f"Error: {tool_name} not in local dispatch table"
    dotted_path = tool_info.path
    cached = _callable_cache.get(dotted_path)
    if cached is not None:
        return cached
    module_name, _, attr_name = dotted_path.rpartition(".")
    if not module_name or not attr_name:
        return f"Error: {tool_name} has invalid callable path {dotted_path!r}"
    mod = importlib.import_module(module_name)
    fn = getattr(mod, attr_name, None)
    if not callable(fn):
        return f"Error: {tool_name}: module {module_name!r} has no attribute {attr_name!r}"
    resolved = cast("_DynamicCallable", fn)
    _callable_cache[dotted_path] = resolved
    return resolved


async def _execute_spec_callable_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None = None,
) -> str:
    """
    Execute a custom callable tool defined in the agent spec YAML.

    Resolves the dotted Python path via :func:`_resolve_spec_callable`,
    then calls the function with the LLM's arguments as kwargs.
    Sync callables run in a worker thread via ``asyncio.to_thread``
    to avoid blocking the event loop.

    :param tool_name: Tool name from the LLM, e.g. ``"echo"``.
    :param args: Parsed argument dict from the LLM.
    :param agent_spec: The session's :class:`AgentSpec`. ``None``
        when no spec is available (returns an error string).
    :returns: Tool output as a string, or an error message.
    """
    resolved = _resolve_spec_callable(tool_name, agent_spec)
    if isinstance(resolved, str):
        return resolved
    if asyncio.iscoroutinefunction(resolved):
        async_callable = cast("_AsyncDynamicCallable", resolved)
        result = await async_callable(**args)
    else:
        result = await asyncio.to_thread(resolved, **args)
    return str(result) if result is not None else ""


# ── Unity Catalog function dispatch ───────────────────────────
#
# UC function tools are declared with ``catalog_path:`` in the YAML
# and executed via the Databricks SQL Statement Execution API.


def _is_uc_function_tool(
    tool_name: str,
    agent_spec: AgentSpec | None,
) -> bool:
    """
    Check whether *tool_name* is a UC function tool in the spec.

    :param tool_name: Tool name from the LLM, e.g.
        ``"classify_text"``.
    :param agent_spec: The session's :class:`AgentSpec`. ``None``
        when no spec is available.
    :returns: ``True`` if the tool is a
        :attr:`ToolRuntime.UC_FUNCTION` tool.
    """
    if agent_spec is None:
        return False
    local_tools = agent_spec.local_tools
    from omnigent.spec.types import ToolRuntime

    return any(
        lt.name == tool_name and lt.runtime == ToolRuntime.UC_FUNCTION for lt in local_tools
    )


def _resolve_uc_profile(agent_spec: AgentSpec) -> str | None:
    """
    Extract the Databricks profile from the agent spec's executor
    auth configuration.

    Checks ``executor.auth`` (preferred) then falls back to
    ``executor.profile`` (deprecated) and finally
    ``executor.config["profile"]`` (compat bridge).

    :param agent_spec: The session's :class:`AgentSpec`.
    :returns: The profile name, e.g. ``"oss"``, or ``None`` for
        SDK default resolution.
    """
    executor = agent_spec.executor
    # Preferred: executor.auth.profile (DatabricksAuth).
    auth = executor.auth
    auth_profile = getattr(auth, "profile", None)
    if isinstance(auth_profile, str) and auth_profile:
        return auth_profile
    # Deprecated: executor.profile.
    if executor.profile:
        return executor.profile
    # Compat bridge: executor.config["profile"].
    config = _string_object_dict(getattr(executor, "config", None))
    if config is None:
        return None
    profile = config.get("profile")
    return profile if isinstance(profile, str) and profile else None


async def _execute_uc_function_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None = None,
) -> str:
    """
    Execute a Unity Catalog function tool and return the output
    string.

    Resolves the ``catalog_path`` from the spec's ``local_tools``,
    extracts the Databricks profile and warehouse ID from the
    executor config, then delegates to
    :func:`omnigent.runner.uc_function.execute_uc_function`.

    :param tool_name: Tool name from the LLM, e.g.
        ``"classify_text"``.
    :param args: Parsed argument dict from the LLM.
    :param agent_spec: The session's :class:`AgentSpec`. Must not
        be ``None`` (caller checks via :func:`_is_uc_function_tool`
        first).
    :returns: Tool output as a string, or an error message.
    """
    from omnigent.runner.uc_function import execute_uc_function

    if agent_spec is None:
        return f"Error: {tool_name} is not a UC function tool"
    local_tools = agent_spec.local_tools
    tool_info = next((lt for lt in local_tools if lt.name == tool_name), None)
    if tool_info is None or tool_info.catalog_path is None:
        return f"Error: {tool_name} is not a UC function tool"

    profile = _resolve_uc_profile(agent_spec)
    warehouse_id = getattr(tool_info, "warehouse_id", None)

    return await execute_uc_function(
        catalog_path=tool_info.catalog_path,
        args=args,
        profile=profile,
        warehouse_id=warehouse_id,
    )


@dataclass(frozen=True)
class _SubagentLabel:
    """
    Human-facing identity fields for a child session.

    :param agent: Sub-agent tool name, e.g. ``"claude"``. ``None`` means the
        server row did not include a valid tool name.
    :param title: Child session title, e.g. ``"issue-1756"``. ``None`` means
        the server row did not include a valid session title.
    """

    agent: str | None
    title: str | None


def _subagent_label(child: _JsonObject) -> _SubagentLabel:
    """
    Extract child identity fields from a child-session summary.

    :param child: One object from
        ``GET /v1/sessions/{parent}/child_sessions``, e.g.
        ``{"tool": "claude", "session_name": "issue-1"}``.
    :returns: Named child identity fields.
    """
    agent = child.get("tool")
    title = child.get("session_name")
    return _SubagentLabel(
        agent=agent if isinstance(agent, str) and agent else None,
        title=title if isinstance(title, str) and title else None,
    )


def _session_wrapper_label(session_payload: _JsonObject) -> str | None:
    """
    Extract the native terminal wrapper label from a session payload.

    :param session_payload: Session or child-session payload, e.g.
        ``{"labels": {"omnigent.wrapper": "codex-native-ui"}}``.
    :returns: Wrapper label value, or ``None`` when absent.
    """
    labels = _string_object_dict(session_payload.get("labels"))
    if labels is None:
        return None
    wrapper = labels.get(_SESSION_WRAPPER_LABEL_KEY)
    return wrapper if isinstance(wrapper, str) and wrapper else None


def _publish_child_launching_update(
    *,
    parent_session_id: str,
    child_session_id: str,
    title: str,
    tool: str,
    session_name: str,
    publish_event: _EventPublisher | None,
) -> None:
    """
    Publish the honest pre-start child state to the parent stream.

    The child session exists at this point, but no child runtime has emitted
    a busy edge yet. Surfacing ``launching`` prevents the UI/orchestrator from
    mistaking session bookkeeping for a running worker.
    """
    event: _JsonObject = {
        "type": "session.child_session.updated",
        "conversation_id": parent_session_id,
        "child_session_id": child_session_id,
        "child": {
            "id": child_session_id,
            "title": title,
            "tool": tool,
            "session_name": session_name,
            "busy": False,
            "current_task_status": "launching",
        },
    }
    if publish_event is not None:
        publish_event(parent_session_id, event)
        return
    from omnigent.runtime import session_stream

    session_stream.publish(parent_session_id, event)


async def _list_child_sessions(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    limit: int = 100,
    tool: str | None = None,
    session_name: str | None = None,
) -> list[_JsonObject] | str:
    """
    Fetch child-session summaries for a parent session.

    :param server_client: Omnigent server client.
    :param conversation_id: Parent session id, e.g. ``"conv_parent123"``.
    :param limit: Maximum child rows to request, e.g. ``100``.
    :param tool: When set alongside ``session_name``, filter to
        children whose title is ``"{tool}:{session_name}"``
        server-side.
    :param session_name: See ``tool``.
    :returns: List of child summary dicts, or an error string.
    """
    params: dict[str, str | int] = {"limit": limit, "order": "desc"}
    if tool and session_name:
        params["tool"] = tool
        params["session_name"] = session_name
    resp = await server_client.get(
        f"/v1/sessions/{conversation_id}/child_sessions",
        params=params,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        return f"Error: failed to list child sessions: {resp.status_code} {resp.text[:200]}"
    decoded: object = resp.json()
    payload = _string_object_dict(decoded)
    data = payload.get("data") if payload is not None else None
    if not isinstance(data, list):
        return "Error: server child_sessions response missing data list"
    return [item for raw in data if (item := _string_object_dict(raw)) is not None]


async def _find_existing_child_session(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    agent: str,
    title: str,
) -> _JsonObject | str | None:
    """
    Find an existing child session by ``(agent, title)``.

    ``sys_session_send`` promises that repeated sends to the same
    pair continue the existing child. The runner must therefore look
    up the row before trying to create a new one; otherwise the
    server's unique child-title constraint turns a continuation into
    a duplicate-create failure.

    :param server_client: Omnigent server client.
    :param conversation_id: Parent session id, e.g. ``"conv_parent123"``.
    :param agent: Sub-agent name, e.g. ``"claude"``.
    :param title: Caller-chosen child title, e.g. ``"issue-1756"``.
    :returns: Matching child summary, ``None`` when absent, or an error
        string when the server lookup failed.
    """
    children = await _list_child_sessions(
        server_client=server_client,
        conversation_id=conversation_id,
        limit=1,
        tool=agent,
        session_name=title,
    )
    if isinstance(children, str):
        return children
    for child in children:
        raw_labels = _string_object_dict(child.get("labels"))
        labels = (
            {key: value for key, value in raw_labels.items() if isinstance(value, str)}
            if raw_labels is not None
            else None
        )
        title_value = child.get("title")
        session_title = title_value if isinstance(title_value, str) else None
        if is_session_closed(labels, session_title):
            continue
        return child
    return None


def _subagent_message_from_args(args: _JsonObject) -> str | None:
    """
    Extract the user message from ``sys_session_send`` arguments.

    The public ``SysSessionSendTool`` contract accepts ``args`` as a plain
    string. polly also sends an object with ``input`` plus metadata such as
    ``purpose`` so its guardrail can classify headless helper usage.

    :param args: Parsed ``sys_session_send`` arguments, e.g.
        ``{"args": "review this"}`` or
        ``{"args": {"input": "review this", "purpose": "review"}}``.
    :returns: Message text, or ``None`` when the payload is malformed.
    """
    raw_message = args.get("args")
    if isinstance(raw_message, dict):
        raw_input = raw_message.get("input")
        return raw_input if isinstance(raw_input, str) else None
    if isinstance(raw_message, str):
        return raw_message
    return None


async def _session_turn_actor(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
) -> str | None:
    """Return the parent turn actor label for runner-originated callbacks."""
    try:
        resp = await server_client.get(f"/v1/sessions/{conversation_id}", timeout=10.0)
    except (httpx.HTTPError, RuntimeError):
        return None
    if resp.status_code != 200:
        return None
    decoded: object = resp.json()
    payload = _string_object_dict(decoded)
    labels = _string_object_dict(payload.get("labels")) if payload is not None else None
    if labels is None:
        return None
    actor = labels.get(_TURN_ACTOR_LABEL)
    return actor if isinstance(actor, str) and actor else None


async def _post_child_message_event(
    server_client: httpx.AsyncClient,
    session_id: str,
    *,
    content: list[_JsonObject],
    created_by: str | None,
) -> httpx.Response:
    """Post a child message, retrying once without best-effort attribution."""

    def _payload(actor: str | None) -> _JsonObject:
        return {
            "type": "message",
            "data": {
                "role": "user",
                "content": content,
            },
            **({"created_by": actor} if actor is not None else {}),
        }

    resp = await server_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_payload(created_by),
        # This message is gated at the recipient's REQUEST phase, which can
        # PARK on a human ASK (e.g. session_cost_budget) up to the policy's
        # ``ask_timeout``. A 30s read budget severed that park → fail-closed
        # /retry → duplicate cards. Wait for the real verdict (one-day read
        # budget, fast connect); a non-parking eval still returns immediately.
        timeout=_ASK_GATE_DELIVERY_TIMEOUT,
    )
    if created_by is None or resp.status_code != 403:
        return resp

    _logger.debug(
        "Child message POST attribution rejected for session=%s; retrying without actor",
        session_id,
    )
    return await server_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_payload(None),
        timeout=_ASK_GATE_DELIVERY_TIMEOUT,
    )


def _subagent_model_from_args(args: _JsonObject) -> str | None:
    """
    Extract and validate the per-dispatch model from ``sys_session_send`` args.

    The optional ``model`` field lives inside the object form of
    ``args`` (``{"input": ..., "model": ...}``). Malformed values fail
    loud instead of being silently dropped — the value later crosses
    the harness spawn boundary as a ``--model`` argv element.

    :param args: Parsed ``sys_session_send`` arguments, e.g.
        ``{"args": {"input": "fix the bug", "model": "claude-sonnet-4-6"}}``.
    :returns: The validated model id, or ``None`` when absent.
    :raises ValueError: If ``model`` is present but not a string, or
        fails :func:`validate_model_override`.
    """
    raw_message = args.get("args")
    if not isinstance(raw_message, dict):
        return None
    raw_model = raw_message.get("model")
    if raw_model is None:
        return None
    if not isinstance(raw_model, str):
        raise ValueError("'model' must be a string when provided")
    return validate_model_override(raw_model)


async def _inherited_parent_model(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    sub_agent_name: str,
    agent_spec: AgentSpec | None,
    child_harness: str | None,
) -> str | None:
    """
    Resolve the parent session's model for a dispatch that names none.

    A user who selects a model for a session expects the whole session —
    including the sub-agents it fans out to — to run on it; without
    inheritance a dispatch that omits ``args.model`` silently lands on the
    worker/provider default. Inheritance is best-effort and skips quietly
    (unlike the explicit ``args.model`` path, which fails loud) because the
    caller asked for nothing:

    - a sub-agent spec that pins its own ``executor.model`` keeps it — the
      worker's author chose that model deliberately;
    - a child harness without model-override plumbing runs its default;
    - a parent model outside the child harness's family (e.g. a Claude
      selection dispatched to a codex worker) is not forced across vendors.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The parent session id.
    :param sub_agent_name: Name of the sub-agent being dispatched.
    :param agent_spec: Parent agent's spec.
    :param child_harness: The child's resolved harness, e.g. ``"claude-sdk"``.
    :returns: The parent's effective model to inherit, or ``None`` when
        inheritance does not apply.
    """
    sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
    spec_model = getattr(getattr(sub_spec, "executor", None), "model", None)
    if isinstance(spec_model, str) and spec_model:
        return None
    if not harness_supports_model_override(child_harness):
        return None
    try:
        resp = await server_client.get(f"/v1/sessions/{conversation_id}", timeout=10.0)
    except (httpx.HTTPError, RuntimeError):
        return None
    if resp.status_code != 200:
        return None
    snap = _string_object_dict(resp.json())
    if snap is None:
        return None
    # Effective selection: an explicit per-session override wins over the
    # spec/CLI-resolved model; both may be absent.
    raw_model = snap.get("model_override") or snap.get("llm_model")
    if not isinstance(raw_model, str) or not raw_model:
        return None
    try:
        parent_model = validate_model_override(raw_model)
    except ValueError:
        return None
    if child_harness is not None and model_family_mismatch(child_harness, parent_model):
        _logger.debug(
            "sys_session_send: not inheriting parent model %r for sub-agent %r "
            "(family mismatch with harness %s); child runs its default",
            parent_model,
            sub_agent_name,
            child_harness,
            extra={"session_id": runner_primary_session_id()},
        )
        return None
    return parent_model


def _subagent_reasoning_effort_from_args(args: _JsonObject) -> str | None:
    """
    Extract the optional ``reasoning_effort`` from
    ``sys_session_send`` args.

    :param args: Decoded tool arguments, e.g.
        ``{"args": {"input": "fix the bug", "reasoning_effort": "high"}}``.
    :returns: The requested effort, or ``None`` when absent.
    :raises ValueError: If ``reasoning_effort`` is present but is not a
        non-empty string.
    """
    raw_message = args.get("args")
    if not isinstance(raw_message, dict):
        return None
    raw_effort = raw_message.get("reasoning_effort")
    if raw_effort is None:
        return None
    if not isinstance(raw_effort, str) or not raw_effort:
        raise ValueError("'reasoning_effort' must be a non-empty string when provided")
    return raw_effort


def _validate_subagent_reasoning_effort(effort: str, harness: str | None) -> str:
    """
    Validate *effort* against the child harness's declared effort family.

    A harness whose family is ``NONE`` has no effort plumbing, and an
    unrecognized one cannot be checked at all — the caller asked for this
    explicitly, so reject rather than accept-and-drop. (Delivery of a
    persisted effort takes the opposite tack and filters quietly; see the
    reasoning guard in :mod:`omnigent.runner.app`.)

    :param effort: Requested effort, e.g. ``"high"``.
    :param harness: Resolved child harness, e.g. ``"claude-native"``.
    :returns: The canonical effort that harness accepts.
    :raises ValueError: If the harness supports no effort override, or
        the value falls outside its vocabulary.
    """
    from omnigent.reasoning_effort import efforts_for_harness, validate_effort

    canonical = canonicalize_harness(harness) if harness is not None else None
    supported = efforts_for_harness(harness)
    if not supported:
        raise ValueError(
            f"harness {canonical or harness or 'unknown'!r} does not support "
            "a reasoning-effort override"
        )
    validated = validate_effort(effort, canonical or harness or "unknown", supported)
    assert validated is not None
    return validated


def _subagent_file_ids_from_args(args: _JsonObject) -> list[str]:
    """
    Extract the optional ``file_ids`` from ``sys_session_send`` args.

    ``file_ids`` lives only in the object form of ``args``
    (``{"input": ..., "file_ids": [...]}``); the plain-string form
    carries no files. A present-but-malformed value fails loud rather
    than being silently dropped — the ids later drive a parent→child
    file copy whose failure must surface to the caller.

    :param args: Parsed ``sys_session_send`` arguments, e.g.
        ``{"args": {"input": "review", "file_ids": ["file_abc"]}}``.
    :returns: The requested file ids in order, or ``[]`` when absent.
    :raises ValueError: If ``file_ids`` is present but is not a non-empty
        list of unique non-empty strings.
    """
    raw_message = args.get("args")
    if not isinstance(raw_message, dict):
        return []
    raw_ids = raw_message.get("file_ids")
    if raw_ids is None:
        return []
    if not isinstance(raw_ids, list) or not all(isinstance(fid, str) and fid for fid in raw_ids):
        raise ValueError("'file_ids' must be a list of non-empty strings when provided")
    if not raw_ids:
        raise ValueError("'file_ids' must contain at least one file id when provided")
    if len(set(raw_ids)) != len(raw_ids):
        raise ValueError("'file_ids' must not contain duplicate file ids")
    return list(raw_ids)


async def _teardown_failed_child(
    server_client: httpx.AsyncClient,
    child_session_id: str,
    *,
    created_child: bool,
) -> str | None:
    """Undo a failed named-send spawn so it leaves no phantom behind.

    Unregisters the runner-local child/work mappings and, when this send
    just created the server child session, deletes it. Deleting the child
    also reclaims any files copied into it before the failure — leaving an
    empty child behind would poison a retry with the same ``(agent, title)``
    (the next send would attach to the phantom instead of spawning clean)
    and orphan the copied file rows. Used on both the copy/content failure
    and the message-post failure paths so they tear down identically.

    :returns: ``None`` when no server cleanup was needed or cleanup
        succeeded, otherwise a parent-visible warning string.
    """
    from omnigent.runner import app as _runner_app

    _runner_app.unregister_child_session(child_session_id)
    _runner_app.unregister_subagent_work(child_session_id)
    if not created_child:
        return None

    last_error = ""
    for attempt in range(2):
        try:
            resp = await server_client.delete(
                f"/v1/sessions/{child_session_id}",
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 400:
                return None
            last_error = f"{resp.status_code} {resp.text[:200]}"
            if resp.status_code < 500:
                break
        if attempt == 0:
            await asyncio.sleep(0.1)

    _logger.warning(
        "Failed to delete child session after failed spawn: session=%s error=%s",
        child_session_id,
        last_error,
    )
    return (
        "Warning: failed to delete newly-created child session "
        f"{child_session_id!r}; retrying the same named send may attach "
        f"to that orphaned session. Delete error: {last_error}"
    )


@dataclass(frozen=True)
class CopyResult:
    """
    Outcome of building a subagent's first-turn content.

    Exactly one field is set: ``content`` on success, ``error`` on failure.
    Replaces the earlier ``(value, error)`` tuple union — the dispatch path
    branches on ``error is not None`` to tear down the child and surface the
    message to the parent agent.

    :param content: The first-turn content blocks, or ``None`` on failure.
    :param error: A human-readable error string, or ``None`` on success.
    """

    content: list[_JsonObject] | None = None
    error: str | None = None


async def _build_subagent_message_content(
    message: str,
    file_ids: list[str],
    *,
    child_session_id: str,
    parent_session_id: str,
    server_client: httpx.AsyncClient,
) -> CopyResult:
    """
    Build the child's first-turn content, copying parent files first.

    With no ``file_ids`` this returns the single ``input_text`` block the
    text-only path has always sent (byte-for-byte unchanged). With
    ``file_ids`` it copies those files from the parent into the child via
    the lineage-scoped copy endpoint, then appends one file block per
    original id (in order) referencing the MAPPED child-scoped id.

    The block type mirrors ``_resolve_forwarded_message_content``: an
    ``image/*`` content type yields ``input_image``; everything else
    yields ``input_file``. The content type comes straight from the copy
    response (preserved from the source row), so no per-file metadata
    fetch is needed; when the source had no recorded type, the filename is
    the fallback signal.

    :param message: The user message text.
    :param file_ids: Parent-owned source file ids to forward, in order.
    :param child_session_id: Destination (child) session id.
    :param parent_session_id: Source session id (the dispatching runner's
        own session), passed as the copy ``source_session_id``.
    :param server_client: Authenticated Omnigent server client.
    :returns: A :class:`CopyResult` — ``content`` set on success, ``error``
        set when the copy fails (surfaced to the parent agent).
    """
    content: list[_JsonObject] = [{"type": "input_text", "text": str(message)}]
    if not file_ids:
        return CopyResult(content=content)

    try:
        copy_resp = await server_client.post(
            f"/v1/sessions/{child_session_id}/resources/files:copy",
            json={"source_session_id": parent_session_id, "file_ids": file_ids},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return CopyResult(
            error=f"Error: failed to copy files to child: {type(exc).__name__}: {exc}"
        )
    if copy_resp.status_code >= 400:
        return CopyResult(
            error=(
                f"Error: failed to copy files to child: "
                f"{copy_resp.status_code} {copy_resp.text[:200]}"
            )
        )

    decoded: object = copy_resp.json()
    payload = _string_object_dict(decoded)
    mapping = _string_object_dict(payload.get("mapping")) if payload is not None else None
    if mapping is None:
        return CopyResult(error="Error: file copy response missing 'mapping'")

    for old_id in file_ids:
        entry = _string_object_dict(mapping.get(old_id))
        if entry is None:
            return CopyResult(error=f"Error: file copy mapping missing entry for {old_id!r}")
        new_id = entry.get("new_id")
        if not isinstance(new_id, str) or not new_id:
            return CopyResult(error=f"Error: file copy mapping missing new id for {old_id!r}")
        # The copy response preserves the source's content_type, so the
        # image-vs-file split uses the true type — no per-file metadata GET.
        # Fall back to a filename guess only when the source had none.
        raw_content_type = entry.get("content_type")
        content_type = raw_content_type if isinstance(raw_content_type, str) else ""
        if not content_type:
            filename = entry.get("filename")
            guessed, _ = (
                mimetypes.guess_type(filename) if isinstance(filename, str) else (None, None)
            )
            content_type = guessed or ""
        block_type = "input_image" if content_type.startswith("image/") else "input_file"
        content.append({"type": block_type, "file_id": new_id})

    return CopyResult(content=content)


def _find_subagent_spec(sub_agent_name: str, agent_spec: AgentSpec | None) -> AgentSpec | None:
    """
    Look up a named sub-agent's spec in the parent's ``sub_agents`` list.

    :param sub_agent_name: Name of the sub-agent, e.g. ``"claude_code"``.
    :param agent_spec: Parent agent's spec. ``None`` when no spec is
        available.
    :returns: The sub-agent's spec (an :class:`AgentSpec` or structural
        equivalent), or ``None`` when absent.
    """
    if agent_spec is None:
        return None
    # Defensive access: resolvers hand us legacy / test spec objects that are
    # only structurally AgentSpec-shaped, and ``_has_subagent`` routes its
    # existence check through here, so a stub without ``sub_agents`` must miss
    # rather than raise.
    for sub_agent in getattr(agent_spec, "sub_agents", None) or []:
        if getattr(sub_agent, "name", None) == sub_agent_name:
            return sub_agent
    # Built-in web_fetch researcher: ``WebFetchTool.__init__`` appends
    # ``__web_researcher`` to the owner's live ``sub_agents`` in memory but
    # never serializes it, so it is absent from the runner's re-parsed spec.
    # Reconstruct it deterministically -- the same pure builder the
    # child-boot resolver (``workflow.py::_find_spec_by_name``) uses -- so
    # this lookup and the dispatch gate agree the sub-agent exists. Without
    # it, ``_has_subagent`` returns True while ``_subagent_harness`` /
    # ``_subagent_allowed_harnesses`` (both routed through here) return
    # ``None``, i.e. the gate and the spec lookup disagree about the same
    # name. Reconstruction is gated inside the helper on the tree actually
    # declaring ``web_fetch``, so a caller-supplied name cannot coerce an
    # arbitrary parent into a shell-capable researcher.
    from omnigent.tools.builtins.web_fetch import (
        RESEARCHER_NAME,
        reconstruct_researcher_spec,
    )

    if sub_agent_name == RESEARCHER_NAME:
        return reconstruct_researcher_spec(agent_spec)
    return None


def _subagent_harness(sub_agent_name: str, agent_spec: AgentSpec | None) -> str | None:
    """
    Resolve the declared harness for a named sub-agent.

    Mirrors the harness derivation in the runner's
    ``_resolve_harness_config`` (``executor.config["harness"]`` falling
    back to ``executor.type``) for the AP-style ``sub_agents`` spec
    shape. Returns ``None`` when the sub-spec or its executor cannot be
    resolved — callers treat that as "unknown harness" and fail loud.

    :param sub_agent_name: Name of the sub-agent, e.g. ``"claude_code"``.
    :param agent_spec: Parent agent's spec. ``None`` when no spec is
        available.
    :returns: Harness id, e.g. ``"codex-native"``, or ``None``.
    """
    from omnigent.model_catalog import spec_harness

    sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
    return spec_harness(sub_spec) if sub_spec is not None else None


def _subagent_harness_override_from_args(args: _JsonObject) -> str | None:
    """
    Extract a per-dispatch harness override from ``sys_session_send`` args.

    The optional ``harness`` field lives in the object form of ``args``
    (``{"input": ..., "harness": "opencode-native"}``). Returned raw (not
    yet canonicalized) so the caller can validate it against the sub-agent
    allowlist and quote the original spelling in errors.

    :param args: Parsed ``sys_session_send`` arguments.
    :returns: The raw harness override, or ``None`` when absent.
    :raises ValueError: If ``harness`` is present but not a string.
    """
    raw_message = args.get("args")
    if not isinstance(raw_message, dict):
        return None
    raw_harness = raw_message.get("harness")
    if raw_harness is None:
        return None
    if not isinstance(raw_harness, str) or not raw_harness:
        raise ValueError("'harness' must be a non-empty string when provided")
    return raw_harness


def _subagent_cost_budget_from_args(
    args: _JsonObject,
) -> _JsonObject | None:
    """
    Extract and validate the per-dispatch cost budget from ``sys_session_send`` args.

    The optional ``cost_budget`` field is an object with max_cost_usd
    (hard limit) and/or ask_thresholds_usd (soft checkpoints). At least
    one must be present.

    :param args: Parsed ``sys_session_send`` arguments.
    :returns: A dict with max_cost_usd and/or ask_thresholds_usd, or
        ``None`` when absent.
    :raises ValueError: If cost_budget is malformed or values are invalid.
    """
    raw_args = args.get("args")
    if isinstance(raw_args, dict):
        budget = raw_args.get("cost_budget")
        if budget is None:
            return None

        if not isinstance(budget, dict):
            raise ValueError("cost_budget must be an object")

        result: _JsonObject = {}
        max_cost_value: float | None = None

        # Extract and validate max_cost_usd if present.
        if "max_cost_usd" in budget:
            max_cost = budget["max_cost_usd"]
            if max_cost is not None:
                if not isinstance(max_cost, str | int | float):
                    raise ValueError("cost_budget.max_cost_usd must be numeric")
                max_cost_value = float(max_cost)
                if max_cost_value <= 0:
                    raise ValueError("cost_budget.max_cost_usd must be > 0")
                result["max_cost_usd"] = max_cost_value

        # Extract and validate ask_thresholds_usd if present.
        if "ask_thresholds_usd" in budget:
            thresholds = budget["ask_thresholds_usd"]
            if thresholds is not None:
                if not isinstance(thresholds, list):
                    raise ValueError("cost_budget.ask_thresholds_usd must be an array")
                if not all(isinstance(threshold, str | int | float) for threshold in thresholds):
                    raise ValueError("cost_budget.ask_thresholds_usd values must be numeric")
                threshold_values = [float(threshold) for threshold in thresholds]
                if not all(threshold > 0 for threshold in threshold_values):
                    raise ValueError("cost_budget.ask_thresholds_usd values must be > 0")
                # Check that thresholds are less than max if both are set.
                if max_cost_value is not None:
                    if any(threshold >= max_cost_value for threshold in threshold_values):
                        raise ValueError("ask_thresholds_usd values must be < max_cost_usd")
                result["ask_thresholds_usd"] = threshold_values

        # At least one must be present.
        if not result:
            raise ValueError("cost_budget must include max_cost_usd and/or ask_thresholds_usd")
        return result

    return None


def _subagent_allowed_harnesses(
    sub_agent_name: str, agent_spec: AgentSpec | None
) -> frozenset[str]:
    """
    Resolve the canonical harness allowlist a sub-agent opts into.

    Reads ``executor.config.allowed_harnesses`` from the named sub-agent's
    spec — the explicit opt-in that gates ``args.harness``. Each entry is
    canonicalized so a user-facing alias still matches.

    :param sub_agent_name: Name of the sub-agent, e.g. ``"opencode"``.
    :param agent_spec: Parent agent's spec.
    :returns: Canonical allowlisted harness ids (empty when none declared).
    """
    sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
    if sub_spec is None:
        return frozenset()
    raw_allowed: object = sub_spec.executor.config.get("allowed_harnesses")
    if not isinstance(raw_allowed, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        canonicalize_harness(str(entry)) or str(entry)
        for entry in raw_allowed
        if isinstance(entry, str) and entry
    )


def _normalize_subagent_model(
    model: str,
    *,
    sub_agent_name: str,
    agent_spec: AgentSpec | None,
    harness: str | None,
) -> str:
    """
    Localize a per-dispatch model id for the child's resolved provider.

    Runs after the family guard (see
    :func:`omnigent.model_override.normalize_model_for_provider` for
    the ordering rationale): a canonical vendor id is prefixed with
    ``databricks-`` when the child routes through the Databricks
    gateway, and the prefix is stripped for a vendor-direct child. When
    the child's provider cannot be determined, the id passes through
    unchanged — the existing fail-loud harness error stays the net.

    :param model: The validated requested model id, e.g.
        ``"claude-sonnet-4-6"``.
    :param sub_agent_name: Name of the sub-agent being dispatched.
    :param agent_spec: Parent agent's spec. ``None`` skips normalization.
    :param harness: The child's declared harness, e.g. ``"claude-native"``.
    :returns: The id to persist as ``model_override``.
    """
    from omnigent.model_catalog import resolve_model_provider

    sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
    if sub_spec is None or harness is None:
        return model
    # resolve_model_provider is total — undeterminable providers come
    # back as kind "none", which normalize passes through.
    provider = resolve_model_provider(sub_spec, harness)
    normalized = normalize_model_for_provider(model, provider.kind)
    if normalized != model:
        _logger.info(
            "sys_session_send: localized model %r -> %r for sub-agent %r "
            "(harness %s, provider kind %s)",
            model,
            normalized,
            sub_agent_name,
            harness,
            provider.kind,
            extra={"session_id": runner_primary_session_id()},
        )
    return normalized


async def _execute_list_models_tool(*, agent_spec: AgentSpec | None) -> str:
    """
    Dispatch ``sys_list_models``: per-worker model availability.

    Runs the enumeration off the event loop — provider resolution reads
    config files and the listing fetches hit provider HTTP APIs (TTL-
    cached in :mod:`omnigent.model_catalog`).

    :param agent_spec: The calling session's agent spec; its
        ``sub_agents`` define the worker rows.
    :returns: JSON mapping of worker name (plus ``"self"``) to its
        ``{source, verified, models, note}`` row, or an error string.
    """
    if agent_spec is None:
        return "Error: sys_list_models requires an agent spec"
    from omnigent.model_catalog import catalog_for_spec

    catalog = await asyncio.to_thread(catalog_for_spec, agent_spec)
    return json.dumps(catalog)


async def _execute_subagent_tool(
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None = None,
    conversation_id: str | None = None,
    agent_spec: AgentSpec | None = None,
    publish_event: Callable[[str, _JsonObject], None] | None = None,
    session_inbox: asyncio.Queue[_JsonObject] | None = None,
) -> str:
    """
    Dispatch a sub-agent tool call (``sys_session_send``).

    Creates or reuses a child session on the server, registers a
    runner-local launch entry, posts the child message, and returns a
    launching handle immediately. The child work becomes ``running`` only
    after the child runtime emits a real busy status. When it completes,
    runner turn-end bookkeeping pushes a completion payload into the
    parent's ``sys_read_inbox`` queue.

    :param args: Parsed arguments from the LLM. Expected keys:
        ``agent`` (sub-agent name, e.g. ``"researcher"``),
        ``args`` (user message text, or an object with ``input`` plus
        optional ``purpose`` / ``model`` dispatch metadata),
        ``title`` (instance label).
    :param server_client: httpx client pointed at the Omnigent server.
    :param conversation_id: Parent session/conversation ID,
        e.g. ``"conv_abc123"``.
    :param agent_spec: Parent agent's :class:`AgentSpec`. Used
        to resolve sub-agent name to ``agent_id``.
    :param publish_event: Optional callback for publishing child-session
        discovery events to the parent stream.
    :param session_inbox: Parent session's inbox queue for async
        completion delivery.
    :returns: JSON child-session handle, or an error string.
    """
    # Lazy import to avoid circular dependency at module load.
    from omnigent.runner import app as _runner_app

    message = _subagent_message_from_args(args)
    if message is None or not message.strip():
        return "Error: sys_session_send requires non-empty args string or args.input string"
    if server_client is None:
        return "Error: sys_session_send requires server_client"
    if conversation_id is None:
        return "Error: sys_session_send requires conversation_id"
    if session_inbox is not None:
        _runner_app._session_inboxes_ref.setdefault(conversation_id, session_inbox)
    elif conversation_id not in _runner_app._session_inboxes_ref:
        return "Error: sys_session_send requires parent session inbox"

    try:
        model = _subagent_model_from_args(args)
    except ValueError as exc:
        return f"Error: sys_session_send invalid 'model': {exc}"

    try:
        reasoning_effort = _subagent_reasoning_effort_from_args(args)
    except ValueError as exc:
        return f"Error: sys_session_send invalid 'reasoning_effort': {exc}"

    try:
        file_ids = _subagent_file_ids_from_args(args)
    except ValueError as exc:
        return f"Error: sys_session_send invalid 'file_ids': {exc}"

    try:
        harness_override = _subagent_harness_override_from_args(args)
    except ValueError as exc:
        return f"Error: sys_session_send invalid 'harness': {exc}"

    try:
        cost_budget = _subagent_cost_budget_from_args(args)
    except (ValueError, TypeError) as exc:
        return f"Error: sys_session_send invalid 'cost_budget': {exc}"

    # By-session-id mode: post to an existing direct child instead of
    # spawning/continuing a named (agent, title) sub-agent.
    target_session_id = args.get("session_id")
    if isinstance(target_session_id, str) and target_session_id:
        # Fail loud on a double-addressed send. The two modes can point at
        # different children, so silently letting session_id win would
        # misroute the message with no signal to the caller.
        if args.get("agent") or args.get("title"):
            return (
                "Error: sys_session_send received both 'session_id' and "
                "'agent'/'title' — supply exactly one addressing mode"
            )
        if model is not None:
            return (
                "Error: sys_session_send 'model' applies only when a "
                "sub-agent session is first created; it cannot change an "
                "existing session. Re-send without 'model' to continue "
                f"session {target_session_id!r}."
            )
        if file_ids:
            return (
                "Error: sys_session_send 'file_ids' is supported only when "
                "addressing a sub-agent by 'agent'/'title'; it cannot be "
                f"forwarded to an existing session by id ({target_session_id!r})."
            )
        if harness_override is not None:
            return (
                "Error: sys_session_send 'harness' applies only when a "
                "sub-agent session is first created; it cannot change an "
                "existing session. Re-send without 'harness' to continue "
                f"session {target_session_id!r}."
            )
        if reasoning_effort is not None:
            return (
                "Error: sys_session_send 'reasoning_effort' applies only when a "
                "sub-agent session is first created; it cannot change an existing "
                f"session {target_session_id!r}."
            )
        if cost_budget is not None:
            return (
                "Error: sys_session_send 'cost_budget' applies only when a "
                "sub-agent session is first created; it cannot change an "
                "existing session. Re-send without 'cost_budget' to continue "
                f"session {target_session_id!r}."
            )
        dispatch_created_by = await _session_turn_actor(
            server_client=server_client,
            conversation_id=conversation_id,
        )
        return await _send_to_existing_session(
            target_session_id,
            message,
            server_client=server_client,
            conversation_id=conversation_id,
            publish_event=publish_event,
            created_by=dispatch_created_by,
        )

    # Named mode: (agent, title) spawn-or-continue.
    sub_agent_name = args.get("agent")
    llm_title_hint = args.get("title")
    if not isinstance(sub_agent_name, str) or not sub_agent_name:
        return "Error: sys_session_send requires 'agent' (or 'session_id')"
    if llm_title_hint is not None and not isinstance(llm_title_hint, str):
        llm_title_hint = None

    # When the LLM provides a title that matches an existing child's
    # session_name (e.g. the structured name from a prior handle), use
    # it for spawn-or-continue. Otherwise auto-generate a structured
    # name below.
    session_name: str | None = llm_title_hint if llm_title_hint else None

    # Verify the sub-agent exists in the parent spec.
    if not _has_subagent(sub_agent_name, agent_spec):
        return f"Error: sub-agent {sub_agent_name!r} not found in agent spec"

    dispatch_created_by = await _session_turn_actor(
        server_client=server_client,
        conversation_id=conversation_id,
    )

    # Use the PARENT's agent_id — inline sub-agents are part of
    # the same bundle, not separately registered. The runner
    # resolves the sub-agent spec from the parent's sub_agents
    # list when it starts the child turn.
    # Try runner-local cache first, then fall back to server query.
    parent_agent_id = _runner_app.get_session_agent_id(conversation_id)
    if parent_agent_id is None:
        try:
            sess_resp = await server_client.get(
                f"/v1/sessions/{conversation_id}",
                timeout=10.0,
            )
            if sess_resp.status_code == 200:
                parent_agent_id = sess_resp.json().get("agent_id")
        except (httpx.HTTPError, RuntimeError):
            pass
    if parent_agent_id is None:
        return "Error: cannot resolve parent agent_id for sub-agent dispatch"

    # If the LLM provided a title, try to find an existing child.
    existing: _JsonObject | str | None = None
    if session_name:
        existing = await _find_existing_child_session(
            server_client=server_client,
            conversation_id=conversation_id,
            agent=str(sub_agent_name),
            title=session_name,
        )
        if isinstance(existing, str):
            return existing
    assert not isinstance(existing, str)
    created_child = False
    child_wrapper_label: str | None = None
    if existing is not None:
        child_session_id = existing.get("id")
        if not isinstance(child_session_id, str) or not child_session_id:
            return "Error: existing child session is missing id"
        if model is not None:
            # A native child bakes --model in at terminal launch, so a
            # mid-conversation override would be silently ignored there.
            return (
                f"Error: sys_session_send 'model' applies only when a "
                f"sub-agent session is first created; {sub_agent_name!r} "
                f"title {session_name!r} already exists as "
                f"{child_session_id}. Re-send without 'model' to continue "
                "it, or sys_session_close it first to spawn a fresh "
                "session on the requested model."
            )
        if file_ids:
            return (
                f"Error: sys_session_send 'file_ids' applies only when a "
                f"sub-agent session is first created; {sub_agent_name!r} "
                f"title {session_name!r} already exists as "
                f"{child_session_id}. Re-send without 'file_ids' to "
                "continue it, or sys_session_close it first to spawn a "
                "fresh session with the requested files."
            )
        if reasoning_effort is not None:
            return (
                f"Error: sys_session_send 'reasoning_effort' applies only when a "
                f"sub-agent session is first created; {sub_agent_name!r} title "
                f"{session_name!r} already exists as {child_session_id}. Re-send "
                "without 'reasoning_effort' to continue it, or "
                "sys_session_close it first to spawn a fresh session."
            )
        if cost_budget is not None:
            return (
                f"Error: sys_session_send 'cost_budget' applies only when a "
                f"sub-agent session is first created; {sub_agent_name!r} "
                f"title {session_name!r} already exists as "
                f"{child_session_id}. Re-send without 'cost_budget' to "
                "continue it, or sys_session_close it first to spawn a "
                "fresh session with the requested budget."
            )
        child_wrapper_label = _session_wrapper_label(existing)
        existing_work = _runner_app.get_subagent_work(child_session_id)
        if existing_work is not None and existing_work.status in (
            "launching",
            "running",
            "waiting",
        ):
            return (
                f"Error: sub-agent {sub_agent_name!r} title {session_name!r} "
                "already has a launching or running turn. Use a distinct task-based title "
                "for independent parallel work; reuse this title only to continue the same "
                "conversation after completion."
            )
        if existing.get("busy") is True:
            return (
                f"Error: sub-agent {sub_agent_name!r} title {session_name!r} "
                "is already running. Use a distinct task-based title for independent "
                "parallel work; reuse this title only to continue the same conversation "
                "after completion."
            )
    else:
        _auto_ordinal = False
        if not session_name:
            # No title hint — auto-generate a structured session name
            # (e.g. "researcher-1"). Recover ordinals from existing
            # children on first spawn after runner restart to avoid
            # duplicates.
            _all_children = await _list_child_sessions(
                server_client=server_client,
                conversation_id=conversation_id,
                tool=str(sub_agent_name),
            )
            if isinstance(_all_children, str):
                return (
                    f"Error: cannot allocate sub-agent name for "
                    f"{sub_agent_name!r}: failed to list existing "
                    f"children — {_all_children}"
                )
            _runner_app.recover_subagent_ordinals(
                conversation_id,
                str(sub_agent_name),
                _all_children,
            )
            ordinal = _runner_app.next_subagent_ordinal(
                conversation_id,
                str(sub_agent_name),
            )
            session_name = f"{sub_agent_name}-{ordinal}"
            _auto_ordinal = True
        child_harness = _subagent_harness(str(sub_agent_name), agent_spec)
        # Apply an allowlisted per-dispatch harness override. The sub-agent
        # spec must explicitly opt in via executor.config.allowed_harnesses,
        # and the requested harness must canonicalize into OMNIGENT_HARNESSES.
        # NOTE: the server create route (``_validated_harness_override`` in
        # server/routes/sessions.py) independently re-validates a session-create
        # override against the GLOBAL ``OMNIGENT_HARNESSES`` (plus the omnigent
        # executor-type rule), but it does NOT re-check the per-spec
        # ``allowed_harnesses`` allowlist. So this orchestrator-dispatch check is
        # the sole enforcement of that per-spec allowlist; a direct
        # ``POST /v1/sessions`` harness_override is bounded only by the global
        # allowlist.
        harness_override_canonical: str | None = None
        if harness_override is not None:
            from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

            canonical = canonicalize_harness(harness_override) or harness_override
            allowed = _subagent_allowed_harnesses(str(sub_agent_name), agent_spec)
            if not allowed:
                return (
                    f"Error: sys_session_send 'harness' override is not "
                    f"permitted for sub-agent {sub_agent_name!r}: its spec "
                    "declares no executor.config.allowed_harnesses allowlist."
                )
            if canonical not in allowed:
                return (
                    f"Error: sys_session_send 'harness' {harness_override!r} is "
                    f"not allowlisted for sub-agent {sub_agent_name!r}: allowed "
                    f"harnesses are {sorted(allowed)}."
                )
            if canonical not in OMNIGENT_HARNESSES:
                return (
                    f"Error: sys_session_send 'harness' {harness_override!r} is "
                    f"not a known harness; must be one of {sorted(OMNIGENT_HARNESSES)}."
                )
            harness_override_canonical = canonical
            child_harness = canonical
        # Fail loud at dispatch when the child's harness needs a CLI binary
        # that isn't on PATH. Otherwise a missing CLI surfaces only as a lazy
        # first-turn failure (e.g. the pi harness raises ImportError, which the
        # parent sees as a generic "turn failed" inbox item that hides the
        # cause), and the orchestrator may re-dispatch into the same wall. The
        # which-probe here reads the same PATH the harness boot uses, so the
        # verdict can't disagree with the real launch.
        from omnigent.onboarding.harness_install import missing_harness_cli

        if child_harness is not None:
            missing_cli = missing_harness_cli(child_harness)
            if missing_cli is not None:
                # Non-npm CLIs (e.g. cursor-agent) carry an ``install_hint``
                # instead of a ``package``; using the hint avoids an
                # ``npm install -g None`` instruction.
                install = (
                    f"npm install -g {missing_cli.package}"
                    if missing_cli.package
                    else (missing_cli.install_hint or "see the harness's install docs")
                )
                return (
                    f"Error: sub-agent {sub_agent_name!r} can't start on this "
                    f"machine: harness {child_harness!r} needs the "
                    f"{missing_cli.binary!r} CLI on PATH and on a supported "
                    f"version, but it is missing or outdated. "
                    f"Install/upgrade it with: {install} "
                    f"(or don't dispatch to {sub_agent_name!r} here)."
                )
        # Create child session on the server (no initial items —
        # those go via a separate POST so the server forwards them
        # to the runner and triggers a turn).
        create_body: _JsonObject = {
            "agent_id": parent_agent_id,
            "parent_session_id": conversation_id,
            "title": f"{sub_agent_name}:{session_name}",
            "sub_agent_name": sub_agent_name,
        }
        if harness_override_canonical is not None:
            create_body["harness_override"] = harness_override_canonical
        if model is not None:
            # Reject up front when the child harness would silently
            # ignore the persisted override — no silent drops.
            if not harness_supports_model_override(child_harness):
                return (
                    f"Error: sys_session_send 'model' is not supported for "
                    f"sub-agent {sub_agent_name!r}: harness "
                    f"{child_harness or 'unknown'!r} has no model-override "
                    "plumbing. Omit 'model' to use the harness default."
                )
            mismatch = model_family_mismatch(child_harness, model) if child_harness else None
            if mismatch is not None:
                return (
                    f"Error: sys_session_send 'model' rejected for sub-agent "
                    f"{sub_agent_name!r}: {mismatch}"
                )
            # Family guard first (on the requested id, so the error
            # quotes what the caller sent), then mechanical
            # canonical<->gateway-local normalization. The normalized
            # id is what the server persists as model_override.
            create_body["model_override"] = _normalize_subagent_model(
                model,
                sub_agent_name=str(sub_agent_name),
                agent_spec=agent_spec,
                harness=child_harness,
            )
        else:
            # No explicit per-dispatch model: inherit the parent session's
            # selection so the user's chosen model governs the whole session
            # tree. Best-effort — skipped when the sub-agent spec pins its
            # own model, the harness has no override plumbing, or the parent
            # model's family cannot run on the child harness.
            inherited = await _inherited_parent_model(
                server_client=server_client,
                conversation_id=conversation_id,
                sub_agent_name=str(sub_agent_name),
                agent_spec=agent_spec,
                child_harness=child_harness,
            )
            if inherited is not None:
                create_body["model_override"] = _normalize_subagent_model(
                    inherited,
                    sub_agent_name=str(sub_agent_name),
                    agent_spec=agent_spec,
                    harness=child_harness,
                )
        # A dispatch that names no effort inherits the sub-agent spec's
        # ``executor.reasoning_effort``, so a worker's default is declared
        # once in its config instead of depending on the orchestrator
        # remembering to pass it on every dispatch.
        effective_effort = reasoning_effort
        effort_source = "sys_session_send"
        if effective_effort is None:
            sub_spec = _find_subagent_spec(sub_agent_name, agent_spec)
            # ``getattr``: sub-specs also arrive as structural stubs that
            # carry only the fields a caller needed.
            spec_effort = getattr(getattr(sub_spec, "executor", None), "reasoning_effort", None)
            if isinstance(spec_effort, str) and spec_effort:
                effective_effort = spec_effort
                effort_source = f"sub-agent {sub_agent_name!r} spec"
        if effective_effort is not None:
            try:
                create_body["reasoning_effort"] = _validate_subagent_reasoning_effort(
                    effective_effort,
                    child_harness,
                )
            except ValueError as exc:
                return f"Error: invalid 'reasoning_effort' from {effort_source}: {exc}"

        # Best-effort retry for auto-ordinal name collisions: a 409 means
        # another runner (or a restart race) already created a child with
        # this ordinal. Bump and retry. Not watertight — the server's
        # (parent, title) check is SELECT-then-INSERT with no DB unique
        # constraint, so truly concurrent creates can still race past it.
        _max_ordinal_retries = 5 if _auto_ordinal else 0
        _create_timeout_exc: httpx.ReadTimeout | None = None
        resp: httpx.Response | None = None
        try:
            for _ordinal_attempt in range(_max_ordinal_retries + 1):
                resp = await server_client.post("/v1/sessions", json=create_body, timeout=30.0)
                if (
                    resp.status_code == 409
                    and _auto_ordinal
                    and _ordinal_attempt < _max_ordinal_retries
                ):
                    ordinal = _runner_app.next_subagent_ordinal(
                        conversation_id,
                        str(sub_agent_name),
                    )
                    session_name = f"{sub_agent_name}-{ordinal}"
                    create_body["title"] = f"{sub_agent_name}:{session_name}"
                    continue
                break
        except httpx.ReadTimeout as _exc:
            _create_timeout_exc = _exc
        if _create_timeout_exc is not None:
            # The read deadline elapsed after the server may have committed the
            # child session — child_session_id is unknown to us, but the
            # (parent, agent, title) triple is. Reconcile: look up the created
            # child by its expected title so we can reap its process cluster.
            # Guard the entire reconcile path against further transport errors
            # (the server may still be wedged) so any failure still returns
            # the descriptive string instead of propagating.
            _reap_warning: str = ""
            try:
                _leaked = await _find_existing_child_session(
                    server_client=server_client,
                    conversation_id=conversation_id,
                    agent=str(sub_agent_name),
                    title=str(session_name),
                )
                if isinstance(_leaked, dict):
                    _leaked_id = _leaked.get("id") or _leaked.get("session_id")
                    if isinstance(_leaked_id, str) and _leaked_id:
                        _reap_warning = (
                            await _teardown_failed_child(
                                server_client,
                                _leaked_id,
                                created_child=True,
                            )
                            or ""
                        )
            except Exception:  # noqa: BLE001 — reconcile is best-effort; any transport error still returns the descriptive string
                pass
            _suffix = f" {_reap_warning}" if _reap_warning else ""
            return (
                f"Error: timed out waiting for child session create response "
                f"({sub_agent_name!r} / {session_name!r}); the server may have "
                f"committed the session — reaping any orphaned cluster.{_suffix} "
                "Retry the same send to continue in a fresh child."
            )
        if resp is None or resp.status_code >= 400:
            status = resp.status_code if resp is not None else 0
            text = resp.text[:200] if resp is not None else "(no response)"
            return f"Error: failed to create child session: {status} {text}"
        child_data = _string_object_dict(resp.json())
        if child_data is None:
            return "Error: server returned malformed child session data"
        child_session_id = child_data.get("session_id") or child_data.get("id")
        if not isinstance(child_session_id, str) or not child_session_id:
            return "Error: server did not return child session_id"
        child_wrapper_label = _session_wrapper_label(child_data)
        created_child = True

        # Attach a subagent_cost_budget policy to the child when requested.
        # Non-fatal: the child session is still usable without the budget.
        if cost_budget is not None:
            policy_body = {
                "name": "__subagent_cost_budget",
                "type": "python",
                "handler": "omnigent.policies.builtins.cost.subagent_cost_budget",
                "factory_params": cost_budget,  # Dict with max_cost_usd and/or ask_thresholds_usd
                "enabled": True,
            }
            try:
                pol_resp = await server_client.post(
                    f"/v1/sessions/{child_session_id}/policies",
                    json=policy_body,
                    timeout=10.0,
                )
                if pol_resp.status_code >= 400:
                    _logger.warning(
                        "failed to set subagent_cost_budget policy on child %s: %s %s",
                        child_session_id,
                        pol_resp.status_code,
                        pol_resp.text[:200],
                    )
            except httpx.HTTPError:
                _logger.warning(
                    "failed to set subagent_cost_budget policy on child %s",
                    child_session_id,
                    exc_info=True,
                )

    # Publish session.created on the parent's SSE stream so the
    # REPL debug panel and any client subscribers discover the
    # child session. SSE-only (transient); durability comes from
    # the conversation_store row written by the server above.
    if not parent_agent_id:
        return f"Error: missing parent agent_id for child session {child_session_id}"
    from omnigent.server.schemas import SessionCreatedEvent

    if created_child:
        _evt = SessionCreatedEvent(
            type="session.created",
            conversation_id=conversation_id,
            child_session_id=child_session_id,
            agent_id=parent_agent_id,
            parent_session_id=conversation_id,
        )
        # Route through the runner's per-session queue, NOT session_stream
        # directly: in the out-of-process (--server) runner, session_stream
        # has no subscribers (they live in the Omnigent server), so a direct
        # publish here is silently dropped. ``publish_event`` enqueues onto
        # the parent's queue, which the Omnigent server's relay republishes onto
        # session_stream — the same channel terminals use. Falls back
        # to a direct publish only for in-process callers without a queue.
        if publish_event is not None:
            publish_event(conversation_id, _evt.model_dump())
        else:
            from omnigent.runtime import session_stream

            session_stream.publish(conversation_id, _evt.model_dump())

    assert session_name is not None
    # Register the child→parent mapping so the runner can fan out the
    # child's status/preview deltas onto the PARENT's stream (the child's
    # own relay isn't running when only the parent is being viewed). The
    # title/tool/session_name are known here (we set the title above), so
    # even a cold status update carries a display name. Cleaned up when
    # the child session ends.
    _runner_app.register_child_session(
        child_session_id,
        parent_session_id=conversation_id,
        title=f"{sub_agent_name}:{session_name}",
        tool=sub_agent_name,
        session_name=session_name,
    )
    _runner_app.register_subagent_work(
        parent_session_id=conversation_id,
        child_session_id=child_session_id,
        agent=str(sub_agent_name),
        title=session_name,
        wrapper_label=child_wrapper_label,
        created_by=dispatch_created_by,
    )
    _publish_child_launching_update(
        parent_session_id=conversation_id,
        child_session_id=child_session_id,
        title=f"{sub_agent_name}:{session_name}",
        tool=str(sub_agent_name),
        session_name=session_name,
        publish_event=publish_event,
    )

    # Copy any forwarded parent files into the child and build the
    # first-turn content (input_text plus a file block per copied id).
    # On copy failure we surface the error to the parent and post no
    # event — but first undo the registrations made above so a failed
    # spawn doesn't leak a phantom child.
    copy_result = await _build_subagent_message_content(
        message,
        file_ids,
        child_session_id=child_session_id,
        parent_session_id=conversation_id,
        server_client=server_client,
    )
    if copy_result.error is not None:
        teardown_warning = await _teardown_failed_child(
            server_client,
            child_session_id,
            created_child=created_child,
        )
        if teardown_warning is not None:
            return f"{copy_result.error}\n{teardown_warning}"
        return copy_result.error
    message_content = copy_result.content
    assert message_content is not None

    # Send the user message as a separate event so the server's
    # post_event forwards it to the runner and starts the child
    # turn.
    try:
        msg_resp = await _post_child_message_event(
            server_client,
            child_session_id,
            content=message_content,
            created_by=dispatch_created_by,
        )
    except httpx.HTTPError as exc:
        teardown_warning = await _teardown_failed_child(
            server_client,
            child_session_id,
            created_child=created_child,
        )
        error = f"Error: failed to send message to child: {type(exc).__name__}: {exc}"
        if teardown_warning is not None:
            return f"{error}\n{teardown_warning}"
        return error
    if msg_resp.status_code >= 400:
        teardown_warning = await _teardown_failed_child(
            server_client,
            child_session_id,
            created_child=created_child,
        )
        error = (
            f"Error: failed to send message to child: {msg_resp.status_code} {msg_resp.text[:200]}"
        )
        if teardown_warning is not None:
            return f"{error}\n{teardown_warning}"
        return error

    # Return the structured handle mirrored from ``spawn.py``. The debug panel
    # parses this to discover child sessions in the sidebar.
    return json.dumps(
        {
            "task_id": child_session_id,
            "handle_id": child_session_id,
            "conversation_id": child_session_id,
            "kind": "sub_agent",
            "agent": sub_agent_name,
            "title": session_name,
            "status": "launching",
            "message": (
                f"[System: sub-agent {sub_agent_name} title {session_name!r} "
                f"launching as task {child_session_id}. Result will appear in "
                "your inbox; call sys_read_inbox to check or sys_cancel_task "
                "to interrupt it.]"
            ),
        }
    )


async def _send_to_existing_session(
    target_session_id: str,
    message: str,
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    publish_event: Callable[[str, _JsonObject], None] | None = None,
    created_by: str | None = None,
) -> str:
    """
    Post a message to an existing direct-child session, return a handle.

    The by-session-id mode of ``sys_session_send``. **Child-only**: the
    target must be a direct child of the caller (its
    ``parent_session_id`` equals ``conversation_id``), so a caller can
    only drive sessions inside its own subtree — never a sibling or an
    unrelated session it merely has access to. Looks the target up to
    verify parentage (404 → ``session_not_found``; wrong parent or
    denied read → ``session_out_of_tree``), registers the child→parent
    fan-out and work mappings, posts the message, and returns a
    ``running`` handle immediately — the completion lands in the parent's
    ``sys_read_inbox`` queue, matching named-mode send.

    :param target_session_id: The existing child session id, e.g.
        ``"conv_abc123"``.
    :param message: The user message text to post.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's own session id — the required
        parent of the target.
    :returns: JSON handle on success; a JSON/text error otherwise.
    """
    from omnigent.runner import app as _runner_app

    try:
        snap = await server_client.get(f"/v1/sessions/{target_session_id}", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return f"Error: sys_session_send failed to look up session: {exc}"
    if snap.status_code == 404:
        return json.dumps({"error": "session_not_found", "conversation_id": target_session_id})
    if snap.status_code in (401, 403):
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_session_id})
    if snap.status_code != 200:
        return f"Error: sys_session_send lookup returned {snap.status_code}"
    snap_data = snap.json()
    if snap_data.get("parent_session_id") != conversation_id:
        return json.dumps(
            {
                "error": "session_out_of_tree",
                "conversation_id": target_session_id,
                "message": (
                    "target is not a direct child of the calling session; "
                    "sys_session_send by session_id is child-only."
                ),
            }
        )
    if is_session_closed(snap_data.get("labels"), snap_data.get("title")):
        return json.dumps(
            {
                "error": "session_closed",
                "conversation_id": target_session_id,
                "message": "target sub-agent session is closed; create a new session to continue.",
            }
        )
    parsed = _parse_session_title(snap_data.get("title"))
    agent_label = parsed.agent or "agent"
    existing_work = _runner_app.get_subagent_work(target_session_id)
    if existing_work is not None and existing_work.status in ("launching", "running", "waiting"):
        return (
            f"Error: session {target_session_id!r} already has a launching or running turn; "
            "wait for completion before sending again"
        )
    if snap_data.get("busy") is True:
        return (
            f"Error: session {target_session_id!r} is already running; "
            "wait for completion before sending again"
        )
    _runner_app.register_child_session(
        target_session_id,
        parent_session_id=conversation_id,
        title=snap_data.get("title") or "",
        tool=agent_label,
        session_name=parsed.title or "",
    )
    _runner_app.register_subagent_work(
        parent_session_id=conversation_id,
        child_session_id=target_session_id,
        agent=agent_label,
        title=parsed.title or "",
        wrapper_label=_session_wrapper_label(snap_data),
        created_by=created_by,
    )
    _publish_child_launching_update(
        parent_session_id=conversation_id,
        child_session_id=target_session_id,
        title=snap_data.get("title") or "",
        tool=agent_label,
        session_name=parsed.title or "",
        publish_event=publish_event,
    )

    try:
        msg_resp = await _post_child_message_event(
            server_client,
            target_session_id,
            content=[{"type": "input_text", "text": message}],
            created_by=created_by,
        )
    except httpx.HTTPError as exc:
        _runner_app.unregister_child_session(target_session_id)
        _runner_app.unregister_subagent_work(target_session_id)
        return f"Error: failed to send message to child: {type(exc).__name__}: {exc}"
    if msg_resp.status_code >= 400:
        _runner_app.unregister_child_session(target_session_id)
        _runner_app.unregister_subagent_work(target_session_id)
        return (
            f"Error: failed to send message to child: {msg_resp.status_code} {msg_resp.text[:200]}"
        )

    return json.dumps(
        {
            "task_id": target_session_id,
            "handle_id": target_session_id,
            "conversation_id": target_session_id,
            "kind": "sub_agent",
            "agent": agent_label,
            "title": parsed.title,
            "status": "launching",
            "message": (
                f"[System: sub-agent {agent_label} title {parsed.title!r} "
                f"launching as task {target_session_id}. Result will appear in "
                "your inbox; call sys_read_inbox to check or sys_cancel_task "
                "to interrupt it.]"
            ),
        }
    )


def _build_session_create_body(
    agent_id: str,
    conversation_id: str,
    title: object,
    message: object,
    model: object = None,
    reasoning_effort: object = None,
) -> _JsonObject:
    """
    Build the JSON ``POST /v1/sessions`` body for ``sys_session_create``.

    ``parent_session_id`` is hard-forced to ``conversation_id`` — this is
    what makes the write child-only (an orchestrator cannot create a
    top-level or sibling session). A non-empty ``title``, ``message``,
    ``model``, and ``reasoning_effort`` are included when provided; the
    message becomes the child's first queued user turn via
    ``initial_items``.

    ``model`` and ``reasoning_effort`` are passed through unvalidated:
    this path never resolves the child's harness (the agent is named by
    id and resolved server-side), so neither can be checked against the
    harness's capabilities here. The server validates both against their
    vocabularies at create.

    :param agent_id: The existing agent to launch, e.g. ``"ag_abc123"``.
    :param conversation_id: The caller's session id — the forced parent.
    :param title: Optional session label; included only when a non-empty
        string.
    :param message: Optional first user message; included only when a
        non-empty string.
    :param model: Optional model override, e.g. ``"databricks-glm-5-2"``;
        written as ``model_override`` on the session.
    :param reasoning_effort: Optional reasoning level, e.g. ``"high"``;
        written as ``reasoning_effort`` on the session.
    :returns: The JSON request body.
    """
    body: _JsonObject = {
        "agent_id": agent_id,
        "parent_session_id": conversation_id,
    }
    if isinstance(title, str) and title:
        body["title"] = title
    if isinstance(model, str) and model:
        body["model_override"] = model
    if isinstance(reasoning_effort, str) and reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if isinstance(message, str) and message:
        body["initial_items"] = [
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": message}]},
            }
        ]
    return body


def _finalize_created_session(
    data: _JsonObject,
    *,
    conversation_id: str,
    agent_id: str,
    title: object,
    publish_event: Callable[[str, _JsonObject], None] | None,
) -> str:
    """
    Register fan-out, emit ``session.created``, and build the handle.

    Records the child→parent mapping so the child's status/preview
    deltas fan out onto the caller's stream, publishes a transient
    ``session.created`` event (durability comes from the server's
    conversation row), and returns the handle the orchestrator uses to
    drive / monitor the child.

    :param data: The :class:`SessionResponse` JSON from the create call.
    :param conversation_id: The caller (parent) session id.
    :param agent_id: The launched agent id, e.g. ``"ag_abc123"``.
    :param title: The caller-supplied title (or non-str when absent).
    :param publish_event: Callback that enqueues an SSE event on the
        caller's outbound queue; ``None`` for in-process callers.
    :returns: JSON handle ``{conversation_id, kind, agent_id,
        agent_name, title, status}``.
    """
    from omnigent.runner import app as _runner_app
    from omnigent.server.schemas import SessionCreatedEvent

    child_id = data.get("id")
    if not isinstance(child_id, str) or not child_id:
        return json.dumps({"error": "server did not return child session id"})
    agent_name = data.get("agent_name")
    agent_label = agent_name if isinstance(agent_name, str) and agent_name else "agent"
    label = title if isinstance(title, str) else ""
    _runner_app.register_child_session(
        child_id,
        parent_session_id=conversation_id,
        title=label,
        tool=agent_label,
        session_name=label,
    )
    evt = SessionCreatedEvent(
        type="session.created",
        conversation_id=conversation_id,
        child_session_id=child_id,
        agent_id=agent_id,
        parent_session_id=conversation_id,
    )
    if publish_event is not None:
        publish_event(conversation_id, evt.model_dump())
    return json.dumps(
        {
            "conversation_id": child_id,
            "kind": "sub_agent",
            "agent_id": agent_id,
            "agent_name": data.get("agent_name"),
            "title": title if isinstance(title, str) else None,
            "status": data.get("status") or "created",
        }
    )


async def _execute_session_create(
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
    publish_event: Callable[[str, _JsonObject], None] | None,
    agent_spec: AgentSpec | None = None,
    runner_workspace: Path | None = None,
) -> str:
    """
    Create a child session (``sys_session_create``).

    Two modes, split on the provided argument (exactly one required):

    - ``agent_id`` — spawn from an existing agent via the JSON
      ``POST /v1/sessions`` create.
    - ``config_path`` — upload a NEW agent from local disk (an agent
      config YAML, agent directory, or pre-built ``.tar.gz`` bundle
      inside the caller's working directory) via the multipart
      ``POST /v1/sessions`` create.

    Both modes force ``parent_session_id`` to the caller (child-only).
    The child inherits the caller's runner (server-side affinity), so a
    queued initial message starts a turn immediately. Returns a handle
    the orchestrator can monitor (``sys_session_get_history`` /
    ``sys_session_get_info``) or drive (``sys_session_send`` by
    ``conversation_id``) — unlike named-mode send, it does NOT block on
    the child turn.

    Maps a 404 to ``agent_not_found`` and 401/403 to ``access_denied``.

    :param args: Parsed arguments; exactly one of ``agent_id`` /
        ``config_path`` required, ``title`` / ``message`` optional.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        returns an error string.
    :param conversation_id: The caller's session id — the forced parent;
        ``None`` returns an error string.
    :param publish_event: SSE publish callback for ``session.created``.
    :param agent_spec: The calling agent's spec, used (with
        ``conversation_id`` / ``runner_workspace``) to resolve the
        os_env cwd that ``config_path`` is read from.
    :param runner_workspace: The runner's workspace dir, authoritative
        for the os_env cwd when present.
    :returns: JSON handle on success; a JSON error object otherwise.
    """
    if server_client is None:
        return json.dumps({"error": "sys_session_create requires server access"})
    if conversation_id is None:
        return json.dumps({"error": "sys_session_create requires a session id"})
    agent_id = args.get("agent_id")
    config_path = args.get("config_path")
    has_agent_id = isinstance(agent_id, str) and bool(agent_id)
    has_config_path = isinstance(config_path, str) and bool(config_path)
    if has_agent_id == has_config_path:
        # Fail loud on both-or-neither: the two modes create different
        # agents, so silently preferring one would mislaunch.
        return json.dumps(
            {
                "error": (
                    "sys_session_create requires exactly one of 'agent_id' "
                    "(existing agent) or 'config_path' (new agent from a "
                    "local config)"
                )
            }
        )
    if has_config_path:
        # The multipart create carries only the config bundle, so an effort
        # passed here would never reach the child. Refuse instead of dropping it.
        if args.get("reasoning_effort") is not None:
            return json.dumps(
                {
                    "error": (
                        "sys_session_create 'reasoning_effort' is supported only "
                        "with 'agent_id'; the 'config_path' create cannot carry "
                        "it. Set the effort in the config you upload instead."
                    )
                }
            )
        return await _session_create_from_config_path(
            str(config_path),
            args,
            server_client=server_client,
            conversation_id=conversation_id,
            publish_event=publish_event,
            agent_spec=agent_spec,
            runner_workspace=runner_workspace,
        )
    body = _build_session_create_body(
        str(agent_id),
        conversation_id,
        args.get("title"),
        args.get("message"),
        model=args.get("model"),
        reasoning_effort=args.get("reasoning_effort"),
    )
    try:
        resp = await server_client.post("/v1/sessions", json=body, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_create failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "agent_not_found", "agent_id": agent_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "agent_id": agent_id})
    if resp.status_code >= 400:
        return json.dumps(
            {"error": f"sys_session_create returned {resp.status_code}", "detail": resp.text[:200]}
        )
    data = resp.json()
    if not isinstance(data.get("id"), str) or not data["id"]:
        return json.dumps({"error": "server did not return a child session id"})
    return _finalize_created_session(
        data,
        conversation_id=conversation_id,
        agent_id=str(agent_id),
        title=args.get("title"),
        publish_event=publish_event,
    )


def _bundle_local_agent_source(source: Path) -> bytes:
    """
    Build gzipped agent-bundle bytes from a local source path.

    Handles the same source shapes as the CLI bundler: a standalone
    agent YAML file or an agent directory is materialized into a
    uniform bundle directory and tarred; any other file (e.g. a
    pre-built ``.tar.gz``) passes through as raw bytes for the
    server's bundle validation to accept or reject.

    Unlike the CLI bundler, no ``${VAR}`` env expansion is performed:
    expanding from the runner process environment would leak runner
    secrets into the uploaded bundle. Configs with unresolved env
    references fail loud in the server's spec validation instead.

    :param source: Local agent config YAML, agent directory, or
        bundle file, e.g.
        ``Path("/work/.omnigent/agent-configs/helper.yaml")``.
    :returns: Gzipped tarball bytes for the multipart ``bundle`` part.
    :raises FileNotFoundError: If ``source`` does not exist.
    """
    import io
    import tarfile

    from omnigent.spec import materialize_bundle

    if source.is_file() and source.suffix.lower() not in {".yaml", ".yml"}:
        return source.read_bytes()
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for file_path in sorted(bundle_dir.rglob("*")):
                if file_path.is_file():
                    tf.add(
                        str(file_path),
                        arcname=str(file_path.relative_to(bundle_dir)),
                    )
        return buf.getvalue()


async def _post_child_first_message(
    child_session_id: str,
    message: str,
    server_client: httpx.AsyncClient,
) -> str | None:
    """
    Queue a bundle-created child's first user message.

    Posted as a separate event so the server's post_event forwards it
    to the runner and starts the child turn (same pattern as
    named-mode ``sys_session_send``).

    :param child_session_id: The new child session id,
        e.g. ``"conv_abc123"``.
    :param message: The first user message text.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``None`` on success; a JSON error string (carrying the
        created ``conversation_id`` so the orchestrator can retry via
        ``sys_session_send``) on failure.
    """
    try:
        msg_resp = await server_client.post(
            f"/v1/sessions/{child_session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                },
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return json.dumps(
            {
                "error": f"child session created but message failed: {exc}",
                "conversation_id": child_session_id,
            }
        )
    if msg_resp.status_code >= 400:
        return json.dumps(
            {
                "error": (
                    "child session created but message failed: "
                    f"{msg_resp.status_code} {msg_resp.text[:200]}"
                ),
                "conversation_id": child_session_id,
            }
        )
    return None


async def _upload_config_bundle(
    config_path: str,
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    agent_spec: AgentSpec | None,
    runner_workspace: Path | None,
) -> _JsonObject | str:
    """
    Resolve, bundle, and upload a local agent config as a child session.

    Reads ``config_path`` from the caller's os_env working directory
    (containment-checked, mirroring the ``sys_agent_download`` write
    guard), bundles it, and proxies the multipart
    ``POST /v1/sessions`` create with ``parent_session_id`` forced to
    the caller.

    :param config_path: Caller-supplied path to the agent config YAML,
        agent directory, or ``.tar.gz`` bundle, relative to the os_env
        cwd, e.g. ``".omnigent/agent-configs/helper.yaml"``.
    :param args: Parsed tool arguments; optional ``title``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's session id — the forced parent.
    :param agent_spec: The calling agent's spec, for os_env resolution.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: The parsed ``CreatedSessionResponse`` dict on success; a
        JSON error string otherwise.
    """
    os_spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
    assert os_spec.cwd is not None
    resolved_cwd = Path(os_spec.cwd).resolve()
    source = (resolved_cwd / config_path).resolve()
    if not source.is_relative_to(resolved_cwd):
        return json.dumps(
            {"error": "sys_session_create config_path escapes the working directory"}
        )
    if not source.exists():
        return json.dumps({"error": "config_not_found", "config_path": config_path})
    try:
        bundle_bytes = await asyncio.to_thread(_bundle_local_agent_source, source)
    except Exception as exc:  # noqa: BLE001 — disk/tar errors become a typed tool error.
        return json.dumps({"error": f"sys_session_create failed to bundle config: {exc}"})

    metadata: _JsonObject = {"parent_session_id": conversation_id}
    title = args.get("title")
    if isinstance(title, str) and title:
        metadata["title"] = title
    try:
        resp = await server_client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(metadata)},
            files={"bundle": (f"{source.name}.tar.gz", bundle_bytes, "application/gzip")},
            timeout=60.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_create failed: {exc}"})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "config_path": config_path})
    if resp.status_code >= 400:
        return json.dumps(
            {"error": f"sys_session_create returned {resp.status_code}", "detail": resp.text[:200]}
        )
    data: _JsonObject = resp.json()
    return data


async def _session_create_from_config_path(
    config_path: str,
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    publish_event: Callable[[str, _JsonObject], None] | None,
    agent_spec: AgentSpec | None,
    runner_workspace: Path | None,
) -> str:
    """
    Bundle-mode ``sys_session_create``: upload a new agent and launch it.

    Delegates the resolve/bundle/upload pipeline to
    :func:`_upload_config_bundle`, validates the server's
    ``CreatedSessionResponse``, queues the optional first ``message``
    via :func:`_post_child_first_message`, and returns the
    orchestrator handle.

    :param config_path: Caller-supplied path to the agent config YAML,
        agent directory, or ``.tar.gz`` bundle, relative to the os_env
        cwd, e.g. ``".omnigent/agent-configs/helper.yaml"``.
    :param args: Parsed tool arguments; optional ``title`` /
        ``message``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The caller's session id — the forced parent.
    :param publish_event: SSE publish callback for ``session.created``.
    :param agent_spec: The calling agent's spec, for os_env resolution.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: JSON handle on success; a JSON error object otherwise.
    """
    data = await _upload_config_bundle(
        config_path,
        args,
        server_client=server_client,
        conversation_id=conversation_id,
        agent_spec=agent_spec,
        runner_workspace=runner_workspace,
    )
    if isinstance(data, str):
        return data
    child_session_id = data.get("session_id")
    if not isinstance(child_session_id, str) or not child_session_id:
        return json.dumps({"error": "server did not return a child session id"})
    created_agent_id = data.get("agent_id")
    if not isinstance(created_agent_id, str) or not created_agent_id:
        # CreatedSessionResponse.agent_id is a required field — a
        # missing value is a server contract violation, not a
        # recoverable state.
        return json.dumps(
            {
                "error": "server did not return the created agent id",
                "conversation_id": child_session_id,
            }
        )

    message = args.get("message")
    if isinstance(message, str) and message:
        message_error = await _post_child_first_message(child_session_id, message, server_client)
        if message_error is not None:
            return message_error

    return _finalize_created_session(
        # Adapt the multipart CreatedSessionResponse shape to the
        # session-snapshot keys _finalize_created_session reads.
        {
            "id": child_session_id,
            "agent_name": data.get("agent_name"),
            "status": "created",
        },
        conversation_id=conversation_id,
        agent_id=created_agent_id,
        title=args.get("title"),
        publish_event=publish_event,
    )


async def _execute_web_fetch_tool(
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
    agent_spec: AgentSpec | None,
    task_id: str | None,
    publish_event: Callable[[str, _JsonObject], None] | None = None,
    session_inbox: asyncio.Queue[_JsonObject] | None = None,
) -> str:
    """
    Dispatch a ``web_fetch`` tool call.

    Translates the user-facing ``query`` / ``url`` arguments into
    a ``sys_session_send`` invocation against the built-in
    ``__web_researcher`` sub-agent, then delegates to
    :func:`_execute_subagent_tool`. The session name embeds
    ``task_id`` so concurrent ``web_fetch`` calls from the same
    parent don't collide on the
    ``(parent_conversation_id, title)`` unique index that
    ``_execute_subagent_tool`` ultimately exercises via
    ``POST /v1/sessions``.

    :param args: Parsed LLM arguments — ``query`` (required) and
        optional ``url``.
    :param server_client: httpx client pointed at the Omnigent server.
    :param conversation_id: Parent session id,
        e.g. ``"conv_abc123"``.
    :param agent_spec: Parent agent's spec — used by the inner
        ``_execute_subagent_tool`` to resolve the sub-agent.
    :param task_id: Calling task id; used to discriminate parallel
        ``web_fetch`` invocations from the same parent.
    :param session_inbox: Parent inbox queue for delayed sub-agent
        completion delivery.
    :returns: The researcher's findings, or an error string.
    """
    from omnigent.tools.builtins.web_fetch import (
        RESEARCHER_NAME,
        build_web_fetch_prompt,
    )

    query = args.get("query")
    if not query:
        return "Error: 'query' parameter is required."
    url = args.get("url")
    prompt = build_web_fetch_prompt(str(query), str(url) if url else None)

    # Embed task_id so each web_fetch from the same parent gets a
    # distinct child conversation (the server enforces a partial
    # unique index on (parent_conversation_id, title) where
    # title="<tool>:<session>").
    session_name = f"web_fetch_{task_id or 'anon'}"

    return await _execute_subagent_tool(
        {
            "agent": RESEARCHER_NAME,
            "args": prompt,
            "title": session_name,
        },
        server_client=server_client,
        conversation_id=conversation_id,
        agent_spec=agent_spec,
        publish_event=publish_event,
        session_inbox=session_inbox,
    )


def _web_search_config_from_spec(agent_spec: AgentSpec | None) -> dict[str, str]:
    """
    Return the ``web_search`` builtin's config dict from the parent spec.

    Mirrors ``ToolManager._register_builtin_tools``: scans
    ``spec.tools.builtins`` for the entry named ``"web_search"`` and returns
    its ``config`` (``search_provider`` + credentials). Empty dict when the
    builtin is declared as a bare string or absent.

    :param agent_spec: Parent agent's spec, or ``None``.
    :returns: The web_search config dict, e.g.
        ``{"search_provider": "nimble", "api_key": "..."}``.
    """
    if agent_spec is None:
        return {}
    tools = getattr(agent_spec, "tools", None)
    builtins = getattr(tools, "builtins", None) or []
    for entry in builtins:
        if getattr(entry, "name", None) == "web_search":
            return getattr(entry, "config", None) or {}
    return {}


async def _execute_web_search_tool(
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """
    Dispatch a ``web_search`` tool call to the spec's configured backend.

    Builds ``WebSearchTool`` from the spec's ``web_search`` builtin config and
    runs its synchronous ``invoke`` off the event loop (the backend makes a
    blocking HTTP call).

    ``llm_provider`` comes from the same helper session setup uses
    (:func:`~omnigent.llms.routing.web_search_native_passthrough_provider`),
    so dispatch preserves the session-setup invariants: on OpenAI
    Responses-compatible harnesses the native ``web_search_preview``
    passthrough is kept (``invoke()`` raises its fence if a function call
    ever reaches this path — defensive, the provider executes server-side);
    every other harness/model runs in function-tool mode.

    :param args: Parsed LLM arguments — ``query`` (required).
    :param agent_spec: Parent agent's spec; carries the web_search config + model.
    :param conversation_id: Parent session id, threaded into the context.
    :param task_id: Calling task id, threaded into the context.
    :param agent_id: Calling agent id, threaded into the context.
    :returns: The formatted search results, or an error string.
    """
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.web_search import WebSearchTool

    config = _web_search_config_from_spec(agent_spec)
    # Same helper as ToolManager._create_web_search, so dispatch and
    # session-setup advertisement cannot drift.
    from omnigent.llms.routing import web_search_native_passthrough_provider

    executor = getattr(agent_spec, "executor", None)
    llm_provider = web_search_native_passthrough_provider(
        getattr(executor, "model", None), getattr(executor, "harness_kind", None)
    )
    tool = WebSearchTool(config=config, llm_provider=llm_provider)
    ctx = ToolContext(
        task_id=task_id or "web_search",
        agent_id=agent_id or "web_search",
        conversation_id=conversation_id,
    )
    return await asyncio.to_thread(tool.invoke, json.dumps(args), ctx)


def _nimble_research_config_from_spec(agent_spec: AgentSpec | None) -> dict[str, str]:
    """
    Return the ``nimble_research`` builtin's config dict from the parent spec.

    Mirrors :func:`_web_search_config_from_spec`: scans ``spec.tools.builtins``
    for the entry named ``"nimble_research"`` and returns its ``config``
    (credentials + agent instance id + polling knobs). Empty dict when the
    builtin is a bare string or absent.

    :param agent_spec: Parent agent's spec, or ``None``.
    :returns: The nimble_research config dict, e.g.
        ``{"api_key": "...", "agent_id": "wsa_..."}``.
    """
    if agent_spec is None:
        return {}
    tools = getattr(agent_spec, "tools", None)
    builtins = getattr(tools, "builtins", None) or []
    for entry in builtins:
        if getattr(entry, "name", None) == "nimble_research":
            return getattr(entry, "config", None) or {}
    return {}


async def _execute_nimble_research_tool(
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """
    Dispatch a ``nimble_research`` tool call to Nimble's Agent API v2.

    Builds ``NimbleResearchTool`` from the spec's ``nimble_research`` builtin config
    and runs its synchronous ``invoke`` off the event loop (the backend blocks
    on the start → poll → result lifecycle), mirroring
    :func:`_execute_web_search_tool`.

    :param args: Parsed LLM arguments — ``task`` (required), ``effort`` (optional).
    :param agent_spec: Parent agent's spec; carries the nimble_research config.
    :param conversation_id: Parent session id, threaded into the context.
    :param task_id: Calling task id, threaded into the context.
    :param agent_id: Calling agent id, threaded into the context.
    :returns: The JSON envelope, or an error string.
    """
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.nimble_research import NimbleResearchTool

    config = _nimble_research_config_from_spec(agent_spec)
    tool = NimbleResearchTool(config=config)
    ctx = ToolContext(
        task_id=task_id or "nimble_research",
        agent_id=agent_id or "nimble_research",
        conversation_id=conversation_id,
    )
    return await asyncio.to_thread(tool.invoke, json.dumps(args), ctx)


def _nimble_extract_config_from_spec(agent_spec: AgentSpec | None) -> dict[str, str]:
    """
    Return the ``nimble_extract`` builtin's config dict from the parent spec.

    Mirrors :func:`_nimble_research_config_from_spec`: scans
    ``spec.tools.builtins`` for the entry named ``"nimble_extract"`` and
    returns its ``config`` (credentials + optional template/timeout). Empty
    dict when the builtin is a bare string or absent.

    :param agent_spec: Parent agent's spec, or ``None``.
    :returns: The nimble_extract config dict, e.g.
        ``{"api_key": "...", "template": "google_search"}``.
    """
    if agent_spec is None:
        return {}
    tools = getattr(agent_spec, "tools", None)
    builtins = getattr(tools, "builtins", None) or []
    for entry in builtins:
        if getattr(entry, "name", None) == "nimble_extract":
            return getattr(entry, "config", None) or {}
    return {}


async def _execute_nimble_extract_tool(
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """
    Dispatch a ``nimble_extract`` tool call to Nimble's Extract Templates run
    endpoint.

    Builds ``NimbleExtractTool`` from the spec's ``nimble_extract`` builtin
    config and runs its synchronous ``invoke`` off the event loop (the backend
    makes a blocking HTTP call), mirroring :func:`_execute_web_search_tool`.

    :param args: Parsed LLM arguments — ``params`` (required object).
    :param agent_spec: Parent agent's spec; carries the nimble_extract config.
    :param conversation_id: Parent session id, threaded into the context.
    :param task_id: Calling task id, threaded into the context.
    :param agent_id: Calling agent id, threaded into the context.
    :returns: The structured JSON, or an error string.
    """
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.nimble_extract import NimbleExtractTool

    config = _nimble_extract_config_from_spec(agent_spec)
    tool = NimbleExtractTool(config=config)
    ctx = ToolContext(
        task_id=task_id or "nimble_extract",
        agent_id=agent_id or "nimble_extract",
        conversation_id=conversation_id,
    )
    return await asyncio.to_thread(tool.invoke, json.dumps(args), ctx)


def _hindsight_config_from_spec(agent_spec: AgentSpec | None, tool_name: str) -> dict[str, str]:
    """
    Return a Hindsight builtin's config dict from the parent spec.

    Mirrors ``ToolManager._register_builtin_tools``: scans ``spec.tools.builtins``
    for the entry named *tool_name* (e.g. ``"hindsight_recall"``) and returns its
    ``config`` (api_key, bank_id, etc.). Empty dict when declared bare or absent.

    :param agent_spec: Parent agent's spec, or ``None``.
    :param tool_name: The Hindsight tool name to look up.
    :returns: The builtin's config dict.
    """
    if agent_spec is None:
        return {}
    tools = getattr(agent_spec, "tools", None)
    builtins = getattr(tools, "builtins", None) or []
    for entry in builtins:
        if getattr(entry, "name", None) == tool_name:
            return getattr(entry, "config", None) or {}
    return {}


async def _execute_hindsight_tool(
    args: _JsonObject,
    *,
    tool_name: str,
    agent_spec: AgentSpec | None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> str:
    """
    Dispatch a Hindsight memory tool call (retain / recall / reflect).

    Builds the tool from the spec's builtin config and runs its synchronous
    ``invoke`` off the event loop (it makes a blocking HTTP call to Hindsight).
    The bank is resolved inside the tool from ``config.bank_id`` → ``ctx.agent_id``
    → ``ctx.conversation_id``, so the real ``agent_id`` is threaded through here.

    :param args: Parsed LLM arguments (``content`` for retain, ``query`` otherwise).
    :param tool_name: The Hindsight tool name being dispatched.
    :param agent_spec: Parent agent's spec; carries the Hindsight builtin config.
    :param conversation_id: Parent session id, threaded into the context.
    :param task_id: Calling task id, threaded into the context.
    :param agent_id: Calling agent id — the default memory bank.
    :returns: The tool's string result, or an error string.
    """
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins import get_builtin_tool

    config = _hindsight_config_from_spec(agent_spec, tool_name)
    tool = get_builtin_tool(tool_name, config)
    if tool is None:
        return f"Hindsight tool {tool_name!r} is not available."
    ctx = ToolContext(
        task_id=task_id or tool_name,
        agent_id=agent_id or tool_name,
        conversation_id=conversation_id,
    )
    return await asyncio.to_thread(tool.invoke, json.dumps(args), ctx)


def _has_subagent(
    sub_agent_name: str,
    agent_spec: AgentSpec | None,
) -> bool:
    """
    Check whether a sub-agent name exists in the parent spec.

    Searches both ``sub_agents`` (AP-style spec) and ``tools``
    dict (omnigent inner loader) for a matching name.

    :param sub_agent_name: Name of the sub-agent, e.g.
        ``"researcher"``.
    :param agent_spec: Parent agent's spec. ``None`` when no
        spec is available.
    :returns: ``True`` if the sub-agent is declared.
    """
    if agent_spec is None:
        return False
    # AP-style spec: sub_agents list, including the synthesized
    # ``__web_researcher``. Routed through the single resolver so the gate
    # and every downstream harness / allowlist lookup agree about whether a
    # name exists (see :func:`_find_subagent_spec`).
    if _find_subagent_spec(sub_agent_name, agent_spec) is not None:
        return True
    # Omnigent inner loader: tools dict with AgentTool entries
    tools = getattr(agent_spec, "tools", None)
    if isinstance(tools, dict) and sub_agent_name in tools:
        return True
    return False


# ── Timer dispatch (RUNNER_TIMER_DISPATCH.md) ─────────────────
# Argument validation and the delay ceiling live in the timer builtin
# (``validate_timer_set_args``) so this firing path and the LLM-facing
# schema stay in lockstep.


async def _execute_timer_set(
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None = None,
    conversation_id: str | None = None,
) -> str:
    """
    Schedule a timer that fires after a delay.

    :param args: Parsed arguments. Keys: ``seconds`` (number),
        ``repeat`` (bool, default False), ``note`` (optional str).
    :param server_client: httpx client for persisting firings.
    :param conversation_id: Session the timer belongs to, e.g.
        ``"conv_abc123"``.
    :returns: JSON string with ``timer_id`` and ``status``.
    """
    from omnigent.runner import app as _app

    validated = validate_timer_set_args(args)
    if isinstance(validated, str):
        return json.dumps({"error": validated})
    seconds, repeat, note = validated
    if server_client is None or conversation_id is None:
        return json.dumps({"error": "timer requires server_client and conversation_id"})

    timer_id = f"timer_{uuid.uuid4().hex}"
    task = asyncio.create_task(
        _timer_loop(
            timer_id=timer_id,
            conversation_id=conversation_id,
            seconds=seconds,
            repeat=repeat,
            note=note,
            server_client=server_client,
        ),
        name=f"timer-{timer_id}",
    )
    _app.register_timer(conversation_id, timer_id, task)
    return json.dumps(
        {
            "timer_id": timer_id,
            "status": "scheduled",
            "seconds": seconds,
            "repeat": repeat,
            "note": note,
        }
    )


async def _timer_loop(
    *,
    timer_id: str,
    conversation_id: str,
    seconds: float,
    repeat: bool,
    note: str | None,
    server_client: httpx.AsyncClient,
) -> None:
    """
    Background loop: sleep then fire timer notifications.

    :param timer_id: Unique timer id, e.g. ``"timer_a1b2..."``.
    :param conversation_id: Session to fire into.
    :param seconds: Delay between firings.
    :param repeat: Loop indefinitely when True.
    :param note: Optional note echoed in firing text.
    :param server_client: httpx client for persistence.
    """
    from omnigent.runner import app as _app

    try:
        while True:
            await asyncio.sleep(seconds)
            text = f"[System: timer {timer_id} fired]"
            if note:
                text += f"\nnote: {note!r}"
            try:
                resp = await server_client.post(
                    f"/v1/sessions/{conversation_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "is_meta": True,
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                    timeout=30.0,
                )
                # httpx does not raise on 4xx/5xx by default; treat those
                # as delivery failures so they share the warning path below.
                resp.raise_for_status()
            except (httpx.HTTPError, asyncio.TimeoutError):
                _logger.warning(
                    "Timer %s firing persist failed for %s",
                    timer_id,
                    conversation_id,
                    exc_info=True,
                    extra={"session_id": conversation_id},
                )
            if not repeat:
                break
    except asyncio.CancelledError:
        return
    finally:
        _app.unregister_timer(conversation_id, timer_id)


async def _execute_timer_cancel(
    args: _JsonObject,
    *,
    conversation_id: str | None = None,
) -> str:
    """
    Cancel a previously scheduled timer by ``timer_id``.

    :param args: Parsed arguments with ``timer_id`` (string).
    :param conversation_id: Session the timer belongs to.
    :returns: JSON with ``status`` ``"cancelled"`` or ``"not_found"``.
    """
    from omnigent.runner import app as _app

    timer_id = args.get("timer_id")
    if not isinstance(timer_id, str) or not timer_id:
        return json.dumps({"error": "timer_id is required"})
    if conversation_id is None:
        return json.dumps({"error": "timer_cancel requires conversation_id"})
    cancelled = _app.cancel_timer(conversation_id, timer_id)
    return json.dumps({"timer_id": timer_id, "status": "cancelled" if cancelled else "not_found"})


async def _execute_comment_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``list_comments`` and ``update_comment``.

    The runner is a separate subprocess from the Omnigent server and has no
    in-process ``CommentStore``. This handler uses ``server_client`` to
    call the Omnigent server's REST API (``GET/PATCH
    /v1/sessions/{id}/comments``), following the same pattern as the
    file tools.

    :param tool_name: ``"list_comments"`` or ``"update_comment"``.
    :param arguments: JSON-encoded arguments string from the LLM.
    :param conversation_id: Current session id, e.g.
        ``"conv_abc123"``. Required for per-session comment scoping.
    :param server_client: HTTP client pointed at the Omnigent server.
        ``None`` if unavailable (returns an error string).
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})

    try:
        args: _JsonObject = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})
    base = f"/v1/sessions/{conversation_id}/comments"

    if tool_name == ListCommentsTool.name():
        params: dict[str, str] = {}
        path = args.get("path")
        if isinstance(path, str) and path:
            params["path"] = path
        try:
            resp = await server_client.get(base, params=params, timeout=30.0)
            if resp.status_code != 200:
                return json.dumps({"error": f"list_comments returned {resp.status_code}"})
            all_comments: list[_JsonObject] = resp.json()
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"list_comments failed: {exc}"})
        # The server's GET endpoint only supports ?path= filtering;
        # apply status filter client-side.
        status_filter = _optional_string(args.get("status"))
        if status_filter is not None:
            all_comments = [c for c in all_comments if c.get("status") == status_filter]
        return json.dumps({"comments": all_comments})

    # update_comment
    comment_id = _optional_string(args.get("comment_id"))
    status = _optional_string(args.get("status"))
    if not comment_id:
        return json.dumps({"error": "missing required argument: comment_id"})
    if not status:
        return json.dumps({"error": "missing required argument: status"})
    _valid_statuses = {"draft", "addressed"}
    if status not in _valid_statuses:
        return json.dumps(
            {"error": f"invalid status {status!r}; must be one of {sorted(_valid_statuses)}"}
        )
    try:
        resp = await server_client.patch(
            f"{base}/{comment_id}",
            json={"status": status},
            timeout=30.0,
        )
        if resp.status_code == 404:
            return json.dumps({"error": f"comment not found: {comment_id}"})
        if resp.status_code != 200:
            return json.dumps({"error": f"update_comment returned {resp.status_code}"})
        return json.dumps({"comment": resp.json()})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"update_comment failed: {exc}"})


async def _execute_browser_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
) -> str:
    """
    Runner-local handler for the ``browser_*`` embedded-browser tools.

    Does the blocking round-trip that drives the Omnigent desktop app's
    embedded browser: POST ``/v1/sessions/{conversation_id}/browser/
    action_request`` with ``{action, args}`` (where ``action`` is the
    tool name minus the ``browser_`` prefix) and return the server's JSON
    response verbatim as the tool output. The server parks a Future,
    publishes ``browser.action_request`` on the session stream, and
    resolves the Future when the winning renderer POSTs the action
    result — so this POST stays open until the action completes or the
    server's 30s browser-action await elapses.

    Mirrors the ask-gate ``server_client.post`` pattern in
    ``_execute_subagent_tool`` (with a much shorter read budget — see
    ``_BROWSER_ACTION_TIMEOUT``). On the runner-side read timeout
    (should not fire before the server returns its own clean timeout JSON,
    since read(60) > server await(30)), returns the same timeout-error JSON
    so the LLM always sees a clean tool error rather than an exception.

    :param tool_name: The browser tool name, e.g. ``"browser_navigate"``.
    :param args: Parsed tool arguments from the LLM, e.g.
        ``{"url": "https://example.com"}``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: Current session id, e.g. ``"conv_abc123"``.
    :returns: The server action-result JSON string, or a timeout/error JSON.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})

    # Strip the ``browser_`` prefix so the wire ``action`` matches the
    # frozen contract (navigate / snapshot / click / type / screenshot).
    action = tool_name[len("browser_") :]
    try:
        resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/browser/action_request",
            json={"action": action, "args": args},
            timeout=_BROWSER_ACTION_TIMEOUT,
        )
    except httpx.ReadTimeout:
        # The server should return its own clean timeout JSON well before this
        # fires (read(60) > server await(30)); this is the belt-and-suspenders
        # path if the server itself stalls.
        return _BROWSER_TIMEOUT_ERROR
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"{tool_name} failed: {type(exc).__name__}: {exc}"})
    if resp.status_code >= 400:
        return json.dumps({"error": f"{tool_name} returned {resp.status_code}: {resp.text[:200]}"})
    return resp.text


async def _execute_policy_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``sys_add_policy`` and ``sys_policy_registry``.

    ``sys_policy_registry`` proxies ``GET /v1/policy-registry`` so the
    agent can browse available builtin policies before picking one.

    ``sys_add_policy`` proxies ``POST /v1/sessions/{id}/policies``.
    Two modes: (1) CEL expression — ``expression`` + ``reason`` are
    translated into the ``cel_policy`` builtin factory; (2) builtin —
    ``handler`` + ``factory_params`` are forwarded as-is.

    :param tool_name: ``"sys_add_policy"`` or ``"sys_policy_registry"``.
    :param arguments: JSON-encoded arguments string from the LLM.
    :param conversation_id: Current session id, e.g.
        ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})

    if tool_name == "sys_policy_registry":
        return await _execute_list_policies(server_client)

    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})

    try:
        args: _JsonObject = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})

    return await _execute_add_policy(args, conversation_id, server_client)


async def _execute_list_policies(
    server_client: httpx.AsyncClient,
) -> str:
    """
    Proxy ``GET /v1/policy-registry`` and return the list.

    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON string with the policy registry entries.
    """
    try:
        resp = await server_client.get("/v1/policy-registry", timeout=30.0)
        if resp.status_code != 200:
            return json.dumps({"error": f"server returned {resp.status_code}"})
        data = resp.json().get("data", [])
        return json.dumps({"policies": data})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_policy_registry failed: {exc}"})


async def _execute_add_policy(
    args: _JsonObject,
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Proxy ``POST /v1/sessions/{id}/policies`` to create a policy.

    Forwards ``handler`` and ``factory_params`` from the tool
    arguments directly to the session policy API as
    ``type="python"``.

    :param args: Parsed tool arguments from the LLM.
    :param conversation_id: Current session id.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON string — created policy or error.
    """
    handler = args.get("handler")
    if not handler:
        return json.dumps(
            {"error": "sys_add_policy requires 'handler' (dotted path from sys_policy_registry)"}
        )
    payload: _JsonObject = {
        "name": args.get("name", ""),
        "type": "python",
        "handler": handler,
    }
    fp = args.get("factory_params")
    if fp is not None:
        payload["factory_params"] = fp

    try:
        resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies",
            json=payload,
            timeout=30.0,
        )
        if resp.status_code not in (200, 201):
            body = resp.text[:500]
            return json.dumps(
                {
                    "error": f"server returned {resp.status_code}",
                    "details": body,
                }
            )
        result = resp.json()
        return json.dumps(
            {
                "policy_id": result.get("id"),
                "name": result.get("name"),
                "type": result.get("type"),
                "handler": result.get("handler"),
                "enabled": result.get("enabled"),
                "message": f"Policy '{result.get('name')}' created successfully.",
            }
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_add_policy failed: {exc}"})


# Fields the create tool forwards to POST /v1/scheduled-tasks.
_SCHEDULED_TASK_CREATE_FIELDS = (
    "name",
    "prompt",
    "rrule",
    "agent_id",
    "timezone",
    "model_override",
    "reasoning_effort",
    "permission_mode",
    "workspace",
    "host_id",
)
# Fields the update tool forwards to PATCH /v1/scheduled-tasks/{id}.
_SCHEDULED_TASK_UPDATE_FIELDS = (
    "name",
    "prompt",
    "rrule",
    "agent_id",
    "timezone",
    "model_override",
    "reasoning_effort",
    "permission_mode",
    "max_cost_usd",
    "workspace",
    "host_id",
    "state",
)
_SCHEDULED_TASK_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _scheduled_task_url(task_id: object) -> str | None:
    """Return a safe scheduled-task URL path for a canonical id."""
    if not isinstance(task_id, str) or not _SCHEDULED_TASK_ID_RE.fullmatch(task_id):
        return None
    return f"/v1/scheduled-tasks/{task_id.lower()}"


async def _execute_scheduled_task_tool(
    tool_name: str,
    arguments: str,
    *,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for the ``sys_scheduled_task_*`` family.

    The runner has no in-process ScheduledTaskStore, so these tools proxy the
    Omnigent server's ``/v1/scheduled-tasks`` REST endpoints over
    ``server_client`` — same posture as :func:`_execute_policy_tool` /
    :func:`_execute_session_query_tool`. Ownership + RRULE validation are
    enforced server-side.

    :param tool_name: One of the ``sys_scheduled_task_*`` names.
    :param arguments: JSON-encoded arguments string from the LLM.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        returns an error string.
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    try:
        args: _JsonObject = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})

    try:
        if tool_name == "sys_scheduled_task_list":
            resp = await server_client.get("/v1/scheduled-tasks", timeout=30.0)
        elif tool_name == "sys_scheduled_task_create":
            payload = {k: args[k] for k in _SCHEDULED_TASK_CREATE_FIELDS if k in args}
            resp = await server_client.post("/v1/scheduled-tasks", json=payload, timeout=30.0)
        elif tool_name in ("sys_scheduled_task_update", "sys_scheduled_task_delete"):
            task_id = args.get("scheduled_task_id")
            if not task_id:
                return json.dumps({"error": f"{tool_name} requires 'scheduled_task_id'"})
            task_url = _scheduled_task_url(task_id)
            if task_url is None:
                return json.dumps(
                    {"error": f"{tool_name} requires canonical 32-character hex scheduled_task_id"}
                )
            if tool_name == "sys_scheduled_task_delete":
                resp = await server_client.delete(task_url, timeout=30.0)
            else:
                payload = {k: args[k] for k in _SCHEDULED_TASK_UPDATE_FIELDS if k in args}
                resp = await server_client.patch(task_url, json=payload, timeout=30.0)
        else:  # pragma: no cover — routing guarantees a known name
            return json.dumps({"error": f"unknown scheduled-task tool {tool_name!r}"})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{tool_name} failed: {exc}"})

    if resp.status_code >= 400:
        return json.dumps(
            {"error": f"server returned {resp.status_code}", "details": resp.text[:500]}
        )
    return json.dumps(resp.json())


@dataclass
class _ParsedTitle:
    """
    A child-session title split into its agent + instance components.

    :param agent: The agent/tool segment, e.g. ``"researcher"`` or
        ``"claude-native-ui"``; ``None`` when the title has no colon
        (a top-level/legacy row that isn't a sub-agent).
    :param title: The instance label segment, e.g. ``"auth"`` or
        ``"1"``; ``None`` in the same no-colon case.
    """

    agent: str | None
    title: str | None


def _parse_session_title(raw_title: str | None) -> _ParsedTitle:
    """
    Split a child-session title into agent + instance label.

    Mirrors the server's ``_child_session_summary_from_conversation``
    parse: the canonical ``"<agent>:<title>"`` form written by
    ``sys_session_send``, plus the 3-segment ``"ui:<agent>:<label>"``
    form written by the Web UI "Add agent" flow. Legacy closed suffixes
    are stripped before parsing so display/tool output stays human
    readable. Returns both fields ``None`` when the title has no colon
    (a top-level conversation that is not a sub-agent).

    :param raw_title: The conversation ``title``, e.g.
        ``"researcher:auth"`` or ``"ui:claude-native-ui:1"``; may be
        ``None``.
    :returns: The parsed agent/title pair.
    """
    display_title = title_without_closed_marker(raw_title)
    if not display_title or ":" not in display_title:
        return _ParsedTitle(agent=None, title=None)
    head, _, tail = display_title.partition(":")
    if head == "ui" and ":" in tail:
        agent, _, label = tail.partition(":")
        return _ParsedTitle(agent=agent, title=label)
    return _ParsedTitle(agent=head, title=tail)


def _truncate_activity(
    text: str | None,
    *,
    max_chars: int = _ACTIVITY_MAX_CHARS,
) -> str | None:
    """
    Truncate text to ``max_chars`` to bound peek prompt size.

    :param text: The text to truncate, or ``None``.
    :param max_chars: Maximum characters retained before the marker.
    :returns: The (possibly truncated) text, or ``None`` when the input
        is ``None``.
    """
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " [truncated]"


def _text_from_api_content(content: object) -> str:
    """
    Join the text blocks of an API message ``content`` array.

    :param content: The ``content`` field of an API message item — a
        list of blocks like ``{"type": "output_text", "text": "..."}``.
    :returns: The concatenated text, or ``""`` when there is none.
    """
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(parts)


def _project_api_item(
    item: _JsonObject,
    *,
    max_chars: int = _ACTIVITY_MAX_CHARS,
) -> _JsonObject:
    """
    Project a REST API conversation item into the compact peek shape.

    Mirrors :func:`omnigent.tools.builtins.spawn._project_activity_item`
    but reads the API item JSON returned by
    ``GET /v1/sessions/{id}/items`` (``ConversationItem.to_api_dict()``)
    rather than the in-process entity, so the harness peek result reads
    the same as the in-process tool's.

    :param item: One API item dict from the items endpoint.
    :param max_chars: Maximum characters retained in each content field.
    :returns: A compact dict — ``{type, tool, args}`` for tool calls,
        ``{type, output}`` for tool results, ``{type, role, text}`` for
        messages.
    """
    itype = _optional_string(item.get("type"))
    if itype == "function_call":
        return {
            "type": "function_call",
            "tool": _optional_string(item.get("name")),
            "args": _truncate_activity(
                _optional_string(item.get("arguments")),
                max_chars=max_chars,
            ),
        }
    if itype == "function_call_output":
        output = item.get("output")
        rendered = output if isinstance(output, str) else json.dumps(output)
        return {
            "type": "function_call_output",
            "output": _truncate_activity(rendered, max_chars=max_chars),
        }
    if itype == "message":
        return {
            "type": "message",
            "role": _optional_string(item.get("role")),
            "text": _truncate_activity(
                _text_from_api_content(item.get("content")),
                max_chars=max_chars,
            ),
        }
    return {"type": itype}


async def _execute_session_query_tool(
    tool_name: str,
    arguments: str,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
    agent_spec: AgentSpec | None = None,
) -> str:
    """
    Runner-local handler for ``sys_session_get_history`` / ``sys_session_list`` /
    ``sys_session_close``.

    The runner is a separate subprocess from the Omnigent server and has no
    in-process ``ConversationStore`` (same constraint as
    :func:`_execute_comment_tool`). These tools therefore dispatch to the
    Omnigent server's existing REST endpoints over ``server_client``:

    - ``sys_session_list`` → ``GET /v1/sessions/{caller}/child_sessions``
    - ``sys_session_get_history`` → ``GET /v1/sessions/{target}/items``
    - ``sys_session_get_info`` → ``GET /v1/sessions/{target}`` (plus a
      best-effort ``GET /v1/runners/{id}/status`` for connectivity)
    - ``sys_session_close`` → ``GET`` the target snapshot then ``PATCH
      /v1/sessions/{target}`` with a tombstoned title
    - ``sys_session_share`` → ``PUT /v1/sessions/{target}/permissions``
      with the grantee + numeric level

    Output shapes mirror the in-process tools in
    :mod:`omnigent.tools.builtins.spawn` so the LLM sees identical
    results regardless of executor. No new identity handling is
    introduced: access control is whatever the server already enforces
    on those endpoints for ``server_client`` — the same posture as
    :func:`_execute_subagent_tool`.

    :param tool_name: ``"sys_session_get_history"``, ``"sys_session_list"``,
        ``"sys_session_close"``, ``"sys_session_get_info"``, or
        ``"sys_session_share"``.
    :param arguments: JSON-encoded arguments string from the LLM, e.g.
        ``'{"conversation_id": "conv_abc123", "tail_items": 5}'``.
    :param conversation_id: The calling session id, e.g. ``"conv_root1"``;
        used as the parent for ``sys_session_list``.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        if unavailable (returns an error string).
    :param agent_spec: The session's :class:`AgentSpec`. Used only by
        ``sys_session_share`` to read the spec's
        ``agent_session_sharing:`` policy (the server can't see it, so
        the runner is the gate). ``None`` when no spec is available —
        sharing then fails closed.
    :returns: Tool output JSON string matching the in-process tool shape.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a session id"})
    try:
        args: _JsonObject = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return json.dumps({"error": f"{tool_name}: malformed JSON arguments"})

    if tool_name == "sys_session_list":
        agent_name = args.get("agent_name")
        window = _discovery_list_window(
            args,
            tool_name,
            ("sessions",),
            {"agent_name": agent_name if isinstance(agent_name, str) and agent_name else None},
        )
        if isinstance(window, str):
            return window
        limit, cursor_state, continued = window
        return await _session_list_via_rest(
            conversation_id,
            server_client,
            agent_name,
            limit=limit,
            cursor_state=cursor_state,
            continued=continued,
        )
    if tool_name == "sys_session_get_history":
        return await _session_get_history_via_rest(args, server_client)
    if tool_name == "sys_session_get_info":
        return await _session_get_info_via_rest(args, conversation_id, server_client)
    if tool_name == "sys_session_share":
        return await _session_share_via_rest(args, conversation_id, server_client, agent_spec)
    return await _session_close_via_rest(args, conversation_id, server_client)


async def _runner_online_or_none(
    runner_id: str | None,
    server_client: httpx.AsyncClient,
) -> bool | None:
    """
    Resolve a runner's live connectivity via ``GET /v1/runners/{id}/status``.

    Best-effort: returns ``None`` when no runner is bound or the status
    lookup fails, so ``sys_session_get_info`` degrades to "connectivity
    unknown" rather than erroring on a transient runner-status hiccup.

    :param runner_id: The session's bound runner id, or ``None``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``True``/``False`` from the status endpoint, or ``None``
        when unbound or the lookup is inconclusive.
    """
    if not runner_id:
        return None
    try:
        resp = await server_client.get(f"/v1/runners/{runner_id}/status", timeout=30.0)
    except Exception:  # noqa: BLE001
        return None
    if resp.status_code != 200:
        return None
    online = resp.json().get("online")
    return online if isinstance(online, bool) else None


async def _session_get_info_via_rest(
    args: _JsonObject,
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Return a session's metadata snapshot via ``GET /v1/sessions/{id}``.

    Resolves the target from ``args["session_id"]`` (falling back to the
    caller's own ``conversation_id`` when omitted), fetches the session
    snapshot, and projects the metadata fields — status, title, agent
    binding, runner binding, host, reasoning effort, effective model,
    parent linkage, workspace / git branch, persisted last-activity time,
    and the outstanding approval prompts (the prompts themselves plus a
    count). Runner connectivity
    is resolved best-effort via
    ``GET /v1/runners/{id}/status`` (``runner_online`` is ``None`` when
    the lookup fails or no runner is bound). The full transcript is
    intentionally omitted — that is what ``sys_session_get_history`` returns.

    Maps a 404 to ``session_not_found`` and 401/403 to ``access_denied``
    (the server denied the read, so from the caller's vantage the target
    is one it may not see).

    :param args: Parsed tool arguments; optional ``session_id``.
    :param conversation_id: The caller's own session id, used as the
        default target when ``session_id`` is omitted.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON metadata object, or a JSON error object.
    """
    raw_target = args.get("session_id") or conversation_id
    if not isinstance(raw_target, str) or not raw_target:
        return json.dumps(
            {"error": "sys_session_get_info requires a non-empty 'session_id' string"}
        )
    try:
        resp = await server_client.get(
            f"/v1/sessions/{raw_target}",
            params={"include_items": "false", "include_liveness": "false"},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_get_info failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "session_not_found", "session_id": raw_target})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": raw_target})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_session_get_info returned {resp.status_code}"})
    snap: _JsonObject = resp.json()
    pending_value = snap.get("pending_elicitations")
    pending = pending_value if isinstance(pending_value, list) else []
    snap_agent_name = _optional_string(snap.get("agent_name"))
    snap_runner_id = _optional_string(snap.get("runner_id"))
    return json.dumps(
        {
            "session_id": snap.get("id"),
            "status": snap.get("status"),
            # Persisted conversation activity is distinct from lifecycle
            # status: repeated polls with an unchanged value let an
            # orchestrator detect a running session that is not advancing.
            "last_activity_at": snap.get("updated_at"),
            "title": snap.get("title"),
            "agent_id": snap.get("agent_id"),
            # Present the public agent name: a native-UI wrapper session
            # (e.g. ``pi-native-ui``) reports its clean display name (``Pi``)
            # so the internal ``-native-ui`` wrapper name never leaks to the
            # model answering "what agent are you?". Non-wrapper names are
            # unchanged.
            "agent_name": public_agent_name(snap_agent_name),
            "runner_id": snap.get("runner_id"),
            "runner_online": await _runner_online_or_none(snap_runner_id, server_client),
            "host_id": snap.get("host_id"),
            "parent_session_id": snap.get("parent_session_id"),
            "sub_agent_name": snap.get("sub_agent_name"),
            "reasoning_effort": snap.get("reasoning_effort"),
            # Effective model: a per-session override wins over the
            # agent spec's default; both may be None when unset.
            "model": snap.get("model_override") or snap.get("llm_model"),
            "workspace": snap.get("workspace"),
            "git_branch": snap.get("git_branch"),
            # The outstanding approval prompts themselves (original
            # elicitation-request event dicts), plus a count for quick
            # status checks. Surfacing the prompts — not just a tally —
            # lets the orchestrator see what each blocked session is
            # waiting on.
            "pending_elicitations": pending,
            "pending_elicitation_count": len(pending),
        }
    )


def _omnigent_error_message(resp: httpx.Response) -> str | None:
    """
    Extract the human-readable message from an Omnigent error response.

    The server renders :class:`omnigent.errors.OmnigentError` as
    ``{"error": {"code": ..., "message": ...}}`` (see the exception
    handler in ``omnigent/server/app.py``). Return that ``message`` so a
    tool can surface the server's own explanation rather than a bare
    status code; return ``None`` when the body is not that envelope (a
    non-JSON body, or a differently-shaped payload) so the caller can
    fall back to a generic message.

    :param resp: The HTTP response whose body to parse, e.g. a 400 from
        ``PUT /v1/sessions/{id}/permissions``.
    :returns: The ``error.message`` string, or ``None`` if absent.
    """
    try:
        body = resp.json()
    except ValueError:
        # Non-JSON error body (e.g. an HTML proxy page) — no detail to surface.
        return None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message")
            if isinstance(message, str) and message:
                return message
    return None


async def _session_share_via_rest(
    args: _JsonObject,
    conversation_id: str,
    server_client: httpx.AsyncClient,
    agent_spec: AgentSpec | None,
) -> str:
    """
    Grant a user access to a session via ``PUT /v1/sessions/{id}/permissions``.

    Resolves the target from ``args["session_id"]`` (falling back to the
    caller's own ``conversation_id`` when omitted), maps the friendly
    ``level`` name to the server's numeric level, and PUTs the grant.
    Same channel and security posture as the other session REST tools:
    the server enforces that ``server_client``'s identity holds
    manage-level access on the target (the session owner does), and caps
    public (``__public__``) grants at read.

    The spec's ``agent_session_sharing:`` policy is enforced HERE, in the
    runner, because the server cannot see it: an ``agent_session_sharing:
    none`` (or unknown) spec refuses every grant, and an
    ``agent_session_sharing: non-public`` spec refuses ``__public__``.
    This is the real gate — tool *advertisement* is gated in the
    ToolManager, but an unadvertised-yet-named call must still be denied
    so a prompt-injected agent can't escalate by emitting the tool name.

    Maps 404 to ``session_not_found`` and 401/403 to ``access_denied``;
    other 4xx/5xx surface the server's own error message when present
    (e.g. the "public is read-only" rejection of a ``__public__`` grant
    above read level) instead of a bare status code.

    :param args: Parsed tool arguments. Requires ``user_id`` (grantee
        email or ``"__public__"``); optional ``level`` (``"read"``
        default / ``"edit"`` / ``"manage"``) and ``session_id``.
    :param conversation_id: The caller's own session id, used as the
        default target when ``session_id`` is omitted.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_spec: The session's :class:`AgentSpec`; its
        ``agent_session_sharing`` policy gates this call. ``None`` (or
        ``agent_session_sharing: none``) fails closed — no grant is
        attempted.
    :returns: JSON ``{"shared": true, ...}`` on success, or a JSON
        error object.
    """
    target = args.get("session_id") or conversation_id
    if not isinstance(target, str) or not target:
        return json.dumps({"error": "sys_session_share requires a non-empty 'session_id' string"})
    user_id = args.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return json.dumps({"error": "sys_session_share requires a non-empty 'user_id'"})
    # Enforce the spec's ``agent_session_sharing:`` policy (SharePolicy is
    # a str-enum, so compare its value directly). ``none``/absent disables
    # the feature; ``__public__`` requires the ``public`` tier specifically.
    share_policy = getattr(agent_spec, "agent_session_sharing", None)
    if share_policy not in _SHARE_ENABLED_POLICIES:
        return json.dumps(
            {
                "error": (
                    "sys_session_share: session sharing is not enabled for this "
                    "agent (set agent_session_sharing: non-public or "
                    "agent_session_sharing: public in the spec)"
                ),
                "session_id": target,
            }
        )
    if user_id == _PUBLIC_USER_SENTINEL and share_policy != _SHARE_PUBLIC_POLICY:
        return json.dumps(
            {
                "error": (
                    "sys_session_share: public ('__public__') sharing is not "
                    "enabled for this agent (requires agent_session_sharing: "
                    "public); grant a specific user instead"
                ),
                "session_id": target,
            }
        )
    # Friendly level name -> the server's numeric permission level
    # (GrantPermissionRequest accepts 1=read, 2=edit, 3=manage).
    level_by_name = {"read": 1, "edit": 2, "manage": 3}
    level_name = args.get("level", "read")
    if level_name not in level_by_name:
        return json.dumps(
            {"error": f"sys_session_share: level must be one of {sorted(level_by_name)}"}
        )
    try:
        resp = await server_client.put(
            f"/v1/sessions/{target}/permissions",
            json={"user_id": user_id, "level": level_by_name[level_name]},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_share failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "session_not_found", "session_id": target})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": target})
    if resp.status_code >= 400:
        # Surface the server's own message when present — e.g. the 400
        # rejecting a __public__ grant above read level carries "Public
        # access is limited to read-only (level 1)", which is far more
        # actionable for the agent than a bare status code.
        detail = _omnigent_error_message(resp)
        if detail is not None:
            return json.dumps(
                {"error": detail, "status_code": resp.status_code, "session_id": target}
            )
        return json.dumps({"error": f"sys_session_share returned {resp.status_code}"})
    return json.dumps(
        {"shared": True, "session_id": target, "user_id": user_id, "level": level_name}
    )


async def _execute_agent_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    runner_workspace: Path | None,
) -> str:
    """
    Runner-local handler for ``sys_agent_get`` / ``sys_agent_download``.

    The runner has no in-process ``AgentStore`` / ``ArtifactStore``, so
    these proxy the Omnigent server's REST endpoints over ``server_client``:

    - ``sys_agent_get`` → ``GET /v1/sessions/{id}/agent`` (project the
      :class:`~omnigent.server.schemas.AgentObject`)
    - ``sys_agent_download`` → ``GET /v1/sessions/{id}/agent/contents``,
      write the ``.tar.gz`` into the agent's local os_env cwd, return the
      path
    - ``sys_agent_list`` → ``GET /v1/agents`` + ``GET /v1/sessions`` +
      local-config scan (no ``session_id``)

    :param tool_name: ``"sys_agent_get"``, ``"sys_agent_download"``, or
        ``"sys_agent_list"``.
    :param args: Parsed tool arguments; ``session_id`` required for
        get/download, ignored for list.
    :param server_client: HTTP client pointed at the Omnigent server; ``None``
        returns an error string.
    :param agent_spec: The running agent's spec — used (with
        ``conversation_id`` / ``runner_workspace``) to resolve the
        os_env cwd that ``sys_agent_download`` writes into and
        ``sys_agent_list`` scans for local configs.
    :param conversation_id: The caller's session id, for os_env cwd
        resolution, e.g. ``"conv_abc123"``.
    :param runner_workspace: The runner's workspace dir, authoritative
        for the os_env cwd when present.
    :returns: Tool output JSON string.
    """
    if server_client is None:
        return json.dumps({"error": f"{tool_name} requires server access"})
    if tool_name == "sys_agent_list":
        window = _discovery_list_window(
            args,
            tool_name,
            ("builtins", "session_agents", "local_configs"),
            {},
        )
        if isinstance(window, str):
            return window
        limit, cursor_state, continued = window
        return await _agent_list_via_rest(
            server_client,
            agent_spec=agent_spec,
            conversation_id=conversation_id,
            runner_workspace=runner_workspace,
            limit=limit,
            cursor_state=cursor_state,
            continued=continued,
        )
    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return json.dumps({"error": f"{tool_name} requires a non-empty 'session_id' string"})
    if tool_name == "sys_agent_get":
        return await _agent_get_via_rest(session_id, server_client)
    return await _agent_download_via_rest(
        session_id,
        args,
        server_client,
        agent_spec=agent_spec,
        conversation_id=conversation_id,
        runner_workspace=runner_workspace,
    )


async def _agent_get_via_rest(
    session_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Return a session's bound-agent metadata via ``GET .../agent``.

    Projects the :class:`~omnigent.server.schemas.AgentObject` fields
    the orchestrator cares about: agent id, name, version, description,
    harness, MCP server summaries, and guardrail policy summaries. Maps a
    404 to ``agent_not_found`` and 401/403 to ``access_denied``.

    :param session_id: The session whose bound agent to inspect, e.g.
        ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON agent-metadata object, or a JSON error object.
    """
    try:
        resp = await server_client.get(f"/v1/sessions/{session_id}/agent", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_agent_get failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "agent_not_found", "session_id": session_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": session_id})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_agent_get returned {resp.status_code}"})
    agent: _JsonObject = resp.json()
    return json.dumps(
        {
            "session_id": session_id,
            "agent_id": agent.get("id"),
            "name": agent.get("name"),
            "version": agent.get("version"),
            "description": agent.get("description"),
            "harness": agent.get("harness"),
            "mcp_servers": agent.get("mcp_servers") or [],
            "policies": agent.get("policies") or [],
        }
    )


def _agent_bundle_filename(
    dest_filename: object,
    agent_name: str,
    agent_version: str,
) -> str | None:
    """
    Resolve the local filename for a downloaded agent bundle.

    Uses the caller's ``dest_filename`` when given, else defaults to
    ``"<agent_name>-v<version>.tar.gz"``. The result must be a bare
    filename — any path separator (a traversal attempt) is rejected by
    returning ``None`` so the caller surfaces an error rather than
    writing outside the working directory.

    :param dest_filename: Caller-supplied filename, or ``None`` to use
        the default. Anything non-str is treated as absent.
    :param agent_name: Agent name from the ``X-Agent-Name`` header, e.g.
        ``"researcher"``.
    :param agent_version: Agent version from the ``X-Agent-Version``
        header, e.g. ``"3"``.
    :returns: A safe bare filename, or ``None`` when ``dest_filename``
        contains a path separator or is ``"."`` / ``".."``.
    """
    if isinstance(dest_filename, str) and dest_filename:
        if "/" in dest_filename or "\\" in dest_filename or dest_filename in (".", ".."):
            return None
        return dest_filename
    safe_name = (
        "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in agent_name) or "agent"
    )
    return f"{safe_name}-v{agent_version}.tar.gz"


async def _agent_download_via_rest(
    session_id: str,
    args: _JsonObject,
    server_client: httpx.AsyncClient,
    *,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    runner_workspace: Path | None,
) -> str:
    """
    Download a session's agent bundle and write it to the agent's disk.

    Fetches the ``.tar.gz`` from ``GET /v1/sessions/{id}/agent/contents``
    and writes the bytes into the agent's os_env working directory — the
    same cwd the agent's ``sys_os_*`` tools operate on (resolved via
    :func:`_effective_runner_os_env_spec`, so a ``caller_process``
    os_env's cwd is the ``runner_workspace`` or the per-conversation
    tmpdir). The default filename is ``"<agent_name>-v<version>.tar.gz"``
    (from the ``X-Agent-*`` response headers); a caller-supplied
    ``dest_filename`` overrides it. Returns the written path so the
    orchestrator can extract (``sys_os_shell``) and read
    (``sys_os_read``) the bundle.

    NOTE: writing through the resolved cwd is correct for the default
    ``caller_process`` os_env (a real local directory). A non-local
    sandbox whose filesystem differs from the runner's would not see the
    file; such os_env types are out of scope for v1 agent download.

    Maps a 404 to ``agent_not_found`` and 401/403 to ``access_denied``.

    :param session_id: The session whose agent bundle to download.
    :param args: Parsed tool arguments; optional ``dest_filename``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_spec: The running agent's spec, for os_env resolution.
    :param conversation_id: The caller's session id, for os_env cwd.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :returns: JSON ``{path, agent_name, agent_version, bytes_written}``,
        or a JSON error object.
    """
    try:
        resp = await server_client.get(f"/v1/sessions/{session_id}/agent/contents", timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_agent_download failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "agent_not_found", "session_id": session_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "access_denied", "session_id": session_id})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_agent_download returned {resp.status_code}"})
    agent_name = resp.headers.get("X-Agent-Name", "agent")
    agent_version = resp.headers.get("X-Agent-Version", "0")
    filename = _agent_bundle_filename(args.get("dest_filename"), agent_name, agent_version)
    if filename is None:
        return json.dumps(
            {"error": "sys_agent_download dest_filename must be a bare filename, not a path"}
        )
    spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
    assert spec.cwd is not None
    cwd = Path(spec.cwd)
    await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
    # Resolve symlinks on the realized cwd and confirm the destination
    # stays within it before writing. ``filename`` is already a bare name
    # (``_agent_bundle_filename`` rejects separators), but a symlinked cwd
    # could still redirect the write outside the sandbox — realpath the
    # parent and check containment, matching the sys_os_write pattern.
    resolved_cwd = cwd.resolve()
    dest = (resolved_cwd / filename).resolve()
    if not dest.is_relative_to(resolved_cwd):
        return json.dumps(
            {"error": "sys_agent_download resolved destination escapes the working directory"}
        )
    await asyncio.to_thread(dest.write_bytes, resp.content)
    return json.dumps(
        {
            "path": str(dest),
            "agent_name": agent_name,
            "agent_version": agent_version,
            "bytes_written": len(resp.content),
        }
    )


async def _agent_list_fetch(
    path: str,
    server_client: httpx.AsyncClient,
    *,
    after: str | None,
    limit: int,
) -> _DiscoveryPage:
    """
    Fetch one cursor page of a paginated list endpoint.

    Best-effort: returns ``[]`` on transport error or non-200 so a single
    failing source degrades ``sys_agent_list`` to "that section is empty"
    rather than failing the whole call.

    :param path: The list endpoint path, e.g. ``"/v1/agents"`` or
        ``"/v1/sessions"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param after: Server cursor from the previous page, if any.
    :param limit: Maximum number of source rows to fetch.
    :returns: Rows and server continuation metadata.
    """
    try:
        params: dict[str, str | int] = {"limit": limit, "order": "desc"}
        if after is not None:
            params["after"] = after
        resp = await server_client.get(path, params=params, timeout=30.0)
    except Exception:  # noqa: BLE001
        return _DiscoveryPage([], False, failed=True)
    if resp.status_code != 200:
        return _DiscoveryPage([], False, failed=True)
    try:
        body = resp.json()
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _DiscoveryPage([], False, failed=True)
    return _parse_discovery_page(body)


def _scan_local_agent_configs(configs_dir: Path) -> list[_JsonObject]:
    """
    Scan a directory for locally-authored agent config YAMLs.

    Reads each ``*.yaml`` under ``configs_dir`` (the agent-config subdir
    of the os_env cwd), extracting ``name`` and ``description`` for the
    listing. Files that don't parse to a mapping are skipped (defensive —
    a stray non-config YAML shouldn't break the scan). Returns ``[]``
    when the directory doesn't exist yet (no configs authored).

    :param configs_dir: The agent-config directory to scan, e.g.
        ``<cwd>/.omnigent/agent-configs``.
    :returns: ``[{"name", "path", "description"}, ...]``, sorted by path.
    """
    import yaml

    if not configs_dir.is_dir():
        return []
    entries: list[_JsonObject] = []
    for path in sorted(configs_dir.glob("*.yaml")):
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(loaded, dict):
            continue
        entries.append(
            {
                "name": loaded.get("name"),
                "path": str(path),
                "description": loaded.get("description"),
            }
        )
    return entries


#: Budget for the confinement lookup. Short on purpose: a slow or wedged
#: server must not hold an agent listing open, and an unanswered lookup
#: fails open to the unfiltered list.
_SPAWN_FAMILY_TIMEOUT_S = 5.0

#: Resolved confinement per session. Routing state is stamped at create and
#: never changes, so one lookup serves every later ``sys_agent_list``. Only
#: sessions with routing armed locally reach it, and only answered lookups
#: are stored, so a fail-open miss is retried next call.
_spawn_family_cache: dict[str, str | None] = {}


async def _spawn_family(
    server_client: httpx.AsyncClient,
    conversation_id: str | None,
) -> str | None:
    """Return the model family a session's spawns are confined to.

    Only a session running Smart Routing on a PINNED harness confines
    them: its spawns are routed inside its own family, so an agent from
    another family has no candidate model it could be routed onto and
    must never be offered as spawnable in the first place. An
    auto-harness session hands the family choice to the router, and a
    plain session is not routed at all — both see the whole surface, byte
    for byte as before.

    The runner-local routing class answers for those two cases without
    touching the server, so a plain session — the overwhelming majority —
    pays nothing for a feature it does not use. Only a locally pinned
    routed session spends the one cached lookup that reads the
    subagent-routing switch.

    Best-effort: an unreadable or slow session read fails open to "no
    confinement", because a discovery listing must not block on the
    routing lookup. The child-create gate is the enforcement.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The calling session's id, or ``None``.
    :returns: ``"claude"`` / ``"gpt"`` / ``"pi"`` when the caller's spawns
        are confined to that family, else ``None``.
    """
    from types import SimpleNamespace

    from omnigent.runner.subagent_routing import (
        auto_harness_session,
        harness_family,
        session_routing_class,
        subagent_routing_enabled,
    )

    if conversation_id is None:
        return None
    local = session_routing_class(conversation_id)
    if not local.routing_enabled or local.auto_harness:
        return None
    if conversation_id in _spawn_family_cache:
        return _spawn_family_cache[conversation_id]
    try:
        resp = await server_client.get(
            f"/v1/sessions/{conversation_id}",
            # Only the routing fields are read, so skip the history and
            # runner-liveness work the full snapshot would do.
            params={"include_items": "false", "include_liveness": "false"},
            timeout=_SPAWN_FAMILY_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 — discovery must not fail on this read
        return None
    if resp.status_code != 200:
        return None
    snapshot = _string_object_dict(resp.json())
    if snapshot is None:
        return None
    family: str | None = None
    if subagent_routing_enabled(_optional_string(snapshot.get("subagent_routing_override"))):
        harness = _optional_string(snapshot.get("harness"))
        labels = _string_object_dict(snapshot.get("labels")) or {}
        # The shared predicate reads the same two fields off a conversation row.
        row = SimpleNamespace(
            labels={key: value for key, value in labels.items() if isinstance(value, str)},
            harness_override=harness,
        )
        if not auto_harness_session(row):
            family = harness_family(harness)
    _spawn_family_cache[conversation_id] = family
    return family


def forget_spawn_family(conversation_id: str) -> None:
    """Drop the cached spawn confinement for a finished session.

    :param conversation_id: Session/conversation identifier.
    """
    _spawn_family_cache.pop(conversation_id, None)


def _in_spawn_family(builtins: list[_JsonObject], family: str | None) -> list[_JsonObject]:
    """Drop built-in agents *family* cannot serve.

    :param builtins: Projected ``builtins`` rows, each carrying ``harness``.
    :param family: The caller's confined family, or ``None`` to keep all.
    :returns: The rows a session confined to *family* may spawn. An agent
        whose harness has no family (unknown, or multi-family like pi) is
        kept: nothing proves it is out of family.
    """
    if family is None:
        return builtins
    from omnigent.runner.subagent_routing import harness_family

    kept: list[_JsonObject] = []
    for row in builtins:
        harness = row.get("harness")
        row_family = harness_family(harness) if isinstance(harness, str) else None
        if row_family is None or row_family == family:
            kept.append(row)
    return kept


async def _agent_list_via_rest(
    server_client: httpx.AsyncClient,
    *,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    runner_workspace: Path | None,
    limit: int | None,
    cursor_state: dict[str, _DiscoveryState],
    continued: bool,
) -> str:
    """
    List launchable agents across built-ins, session-bound, and local.

    Fans out three independent reads — each degrades to an empty section
    on failure rather than failing the whole call:

    - ``builtins``: ``GET /v1/agents`` (template agents), projected to
      ``{agent_id, name, description, harness}``.
    - ``session_agents``: ``GET /v1/sessions``, projected to
      ``{session_id, agent_id, agent_name, status}`` so the caller can
      launch the agent directly (``sys_session_create`` by
      ``agent_id``) or ``sys_agent_get`` / ``sys_agent_download`` a
      chosen session.
    - ``local_configs``: a scan of the os_env cwd's agent-config subdir
      (YAMLs authored with ``sys_os_write`` per the agent-authoring
      skill), projected to ``{name, path, description}``.

    On a pinned Smart Routing session the ``builtins`` section is confined
    to the caller's own model family (:func:`_spawn_family`) — an agent it
    could never route a spawn onto is not a launchable agent for it. The
    other two sections carry no harness to filter on; the child-create gate
    refuses those.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_spec: The running agent's spec, for os_env cwd
        resolution of the local-config scan.
    :param conversation_id: The caller's session id, for os_env cwd.
    :param runner_workspace: The runner workspace, authoritative cwd.
    :param limit: Optional maximum rows returned from each source. When
        omitted, the complete legacy result is preserved while it fits.
    :param cursor_state: Opaque continuation positions for each source.
    :returns: The legacy complete JSON result while it fits, otherwise a
        bounded page with continuation metadata.
    """
    source_limit = limit or _AGENT_LIST_PAGE_LIMIT
    builtins_page = (
        _DiscoveryPage([], False)
        if cursor_state["builtins"][0] == _DISCOVERY_END
        else await _agent_list_fetch(
            "/v1/agents",
            server_client,
            after=cursor_state["builtins"][1],
            limit=source_limit,
        )
    )
    sessions_page = (
        _DiscoveryPage([], False)
        if cursor_state["session_agents"][0] == _DISCOVERY_END
        else await _agent_list_fetch(
            "/v1/sessions",
            server_client,
            after=cursor_state["session_agents"][1],
            limit=source_limit,
        )
    )
    spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
    assert spec.cwd is not None
    configs_dir = Path(spec.cwd) / _AGENT_CONFIG_SUBDIR
    local_configs = await asyncio.to_thread(_scan_local_agent_configs, configs_dir)
    local_state, local_after = cursor_state["local_configs"]
    if local_state == _DISCOVERY_END:
        remaining_configs = []
    elif local_after is not None:
        remaining_configs = [
            row for row in local_configs if str(row.get("path", "")) > local_after
        ]
    else:
        remaining_configs = local_configs
    listing = _project_agent_list(
        builtins_page.rows,
        sessions_page.rows,
        remaining_configs[:source_limit],
    )
    listing["builtins"] = _in_spawn_family(
        listing["builtins"], await _spawn_family(server_client, conversation_id)
    )
    return _bounded_discovery_result(
        listing,
        limit=limit,
        cursor_state=cursor_state,
        continued=continued,
        tool_name="sys_agent_list",
        filters={},
        source_pages={
            "builtins": builtins_page,
            "session_agents": sessions_page,
            "local_configs": _DiscoveryPage(
                listing["local_configs"],
                len(listing["local_configs"]) < len(remaining_configs),
            ),
        },
    )


def _project_agent_list(
    builtins_raw: list[_JsonObject],
    sessions_raw: list[_JsonObject],
    local_configs: list[_JsonObject],
) -> dict[str, list[_JsonObject]]:
    """
    Project the three raw ``sys_agent_list`` sources into the tool result.

    Built-in :class:`AgentObject` rows are projected to
    ``{agent_id, name, description, harness}`` (note ``id`` → ``agent_id``
    for naming consistency with the rest of the surface); session rows to
    ``{session_id, agent_id, agent_name, status}``; local configs pass
    through unchanged.

    :param builtins_raw: ``data`` rows from ``GET /v1/agents``.
    :param sessions_raw: ``data`` rows from ``GET /v1/sessions``.
    :param local_configs: Entries from :func:`_scan_local_agent_configs`.
    :returns: ``{builtins, session_agents, local_configs}``.
    """
    builtins: list[_JsonObject] = [
        {
            "agent_id": a.get("id"),
            "name": a.get("name"),
            "description": a.get("description"),
            "harness": a.get("harness"),
        }
        for a in builtins_raw
    ]
    session_agents: list[_JsonObject] = [
        {
            "session_id": s.get("id"),
            "agent_id": s.get("agent_id"),
            "agent_name": s.get("agent_name"),
            "status": s.get("status"),
        }
        for s in sessions_raw
    ]
    return {
        "builtins": builtins,
        "session_agents": session_agents,
        "local_configs": local_configs,
    }


async def _session_list_via_rest(
    conversation_id: str,
    server_client: httpx.AsyncClient,
    agent_name: object = None,
    *,
    limit: int | None,
    cursor_state: dict[str, _DiscoveryState],
    continued: bool,
) -> str:
    """
    Return the two-view session list: ``sub_agents`` + global ``sessions``.

    ``sub_agents`` is the caller's named-sub-agent view (children, plus
    parent/siblings when the caller is itself a child) — see
    :func:`_collect_sub_agents`. ``sessions`` is the **global**,
    permission-bounded list of every session the caller can access, each
    annotated with status + runner connectivity, optionally filtered by
    ``agent_name`` — see :func:`_collect_global_sessions`. Both are
    best-effort: a failure in either view yields an empty list for it
    rather than failing the whole call.

    :param conversation_id: The caller session id, e.g. ``"conv_root1"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_name: Optional agent-name filter for the global
        ``sessions`` view; ignored for ``sub_agents``.
    :param limit: Optional maximum rows returned from the global sessions view. When
        omitted, the complete legacy result is preserved while it fits.
    :param cursor_state: Opaque continuation position for the global sessions view.
    :returns: The legacy complete JSON result while it fits, otherwise a
        bounded page with continuation metadata.
    """
    sub_agents = await _collect_sub_agents(conversation_id, server_client)
    sessions_page = (
        _DiscoveryPage([], False)
        if cursor_state["sessions"][0] == _DISCOVERY_END
        else await _collect_global_sessions(
            server_client,
            agent_name,
            after=cursor_state["sessions"][1],
            limit=limit or _AGENT_LIST_PAGE_LIMIT,
        )
    )
    return _bounded_discovery_result(
        {"sub_agents": cast(list[_JsonObject], sub_agents), "sessions": sessions_page.rows},
        limit=limit,
        cursor_state=cursor_state,
        continued=continued,
        tool_name="sys_session_list",
        filters={"agent_name": agent_name if isinstance(agent_name, str) and agent_name else None},
        source_pages={"sessions": sessions_page},
        page_sections=("sessions",),
    )


async def _rename_current_session_via_rest(
    args: _JsonObject,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Rename the calling session through the server API.

    Session naming is metadata, never a prerequisite for the user's turn.
    Every failure therefore becomes a tool-result envelope so a missing route,
    unavailable server, or malformed response cannot abort the harness session.
    """
    if server_client is None:
        return json.dumps({"error": "sys_session_rename requires server access"})
    if conversation_id is None:
        return json.dumps({"error": "sys_session_rename requires a session id"})
    title = args.get("title")
    if not isinstance(title, str):
        return json.dumps({"error": "sys_session_rename requires a string 'title'"})
    # Enforce exactly the bounds the tool schema advertises to the LLM.
    max_chars = _SESSION_RENAME_TITLE_MAX_CHARS
    if len(title) < 2 or len(title) > max_chars:
        return json.dumps({"error": f"sys_session_rename title must be 2-{max_chars} characters"})
    if "\n" in title or "\r" in title:
        return json.dumps({"error": "sys_session_rename title must be a single line"})
    normalized_title = " ".join(title.split())
    if len(normalized_title) < 2:
        return json.dumps({"error": f"sys_session_rename title must be 2-{max_chars} characters"})
    # Only a top-level session may rename itself: a sub-agent's title is its
    # (parent, title) continuation address for sys_session_send, so a child
    # rename would corrupt sibling addressing. Refuse explicitly, matching
    # the old seed-gated endpoint's response for child sessions.
    try:
        info_response = await server_client.get(
            f"/v1/sessions/{conversation_id}",
            # Skip the transcript and the runner/host liveness lookup — the
            # probe only needs parent_session_id.
            params={"include_items": "false", "include_liveness": "false"},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_rename failed: {exc}"})
    if info_response.status_code >= 400:
        return json.dumps(
            {
                "error": f"sys_session_rename returned {info_response.status_code}",
                "detail": info_response.text[:200],
            }
        )
    try:
        info_payload = info_response.json()
    except ValueError as exc:
        return json.dumps({"error": f"sys_session_rename returned invalid JSON: {exc}"})
    # Fail closed: only a payload that positively shows a parentless session
    # may proceed — a malformed or version-skewed snapshot must not let a
    # child rename slip through and corrupt its continuation address. Exactly
    # None means top-level; a non-empty string means child; anything else
    # (empty string, wrong type) is malformed and blocks the rename.
    if not isinstance(info_payload, dict) or "parent_session_id" not in info_payload:
        return json.dumps(
            {"error": "sys_session_rename could not verify the session is top-level"}
        )
    parent_session_id = info_payload["parent_session_id"]
    if parent_session_id is not None:
        if isinstance(parent_session_id, str) and parent_session_id:
            return json.dumps({"renamed": False, "title": None, "reason": "not_top_level"})
        return json.dumps(
            {"error": "sys_session_rename could not verify the session is top-level"}
        )
    try:
        response = await server_client.patch(
            f"/v1/sessions/{conversation_id}",
            json={"title": normalized_title},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_rename failed: {exc}"})
    if response.status_code >= 400:
        return json.dumps(
            {
                "error": f"sys_session_rename returned {response.status_code}",
                "detail": response.text[:200],
            }
        )
    try:
        payload = response.json()
    except ValueError as exc:
        return json.dumps({"error": f"sys_session_rename returned invalid JSON: {exc}"})
    if not isinstance(payload, dict):
        return json.dumps({"error": "sys_session_rename returned a non-object response"})
    updated_title = payload.get("title")
    if not isinstance(updated_title, str):
        return json.dumps({"error": "sys_session_rename response omitted the updated title"})
    return json.dumps({"renamed": True, "title": updated_title, "reason": None})


async def _collect_sub_agents(
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> list[dict[str, str | None]]:
    """
    Collect the caller's named-sub-agent view via ``GET .../child_sessions``.

    Returns ``[{"agent", "title", "conversation_id"}, ...]``, skipping
    closed and titleless/colonless rows so they never re-surface to the
    LLM. Includes the caller's own children and, when the caller is
    itself a child (e.g. a user-added agent), its parent (surfaced as
    ``agent="main"``) and its siblings — so an added agent can still
    discover ``main`` and its session-mates. Best-effort: a failed
    lookup yields ``[]`` (or own-children-only) rather than raising.

    :param conversation_id: The caller session id.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The sub-agent entries.
    """
    try:
        resp = await server_client.get(
            f"/v1/sessions/{conversation_id}/child_sessions",
            params={"limit": 100},
            timeout=30.0,
        )
    except Exception:  # noqa: BLE001
        return []
    if resp.status_code != 200:
        return []
    result = _child_rows_to_entries(resp.json().get("data", []))

    # If the caller is itself a child, surface main + siblings too.
    parent_id = await _session_parent_id(conversation_id, server_client)
    if parent_id is not None:
        result.append({"agent": "main", "title": None, "conversation_id": parent_id})
        try:
            sib_resp = await server_client.get(
                f"/v1/sessions/{parent_id}/child_sessions",
                params={"limit": 100},
                timeout=30.0,
            )
            if sib_resp.status_code == 200:
                for entry in _child_rows_to_entries(sib_resp.json().get("data", [])):
                    # Exclude the caller itself from its own sibling list.
                    if entry["conversation_id"] != conversation_id:
                        result.append(entry)
        except Exception:  # noqa: BLE001
            _logger.debug(
                "sys_session_list sibling enrichment failed for parent %s",
                parent_id,
                exc_info=True,
                extra={"session_id": conversation_id},
            )
    return result


async def _resolve_runner_online_map(
    rows: list[_JsonObject],
    server_client: httpx.AsyncClient,
) -> dict[str, bool | None]:
    """
    Resolve live connectivity for the unique runners bound across rows.

    Checks each distinct ``runner_id`` once (sessions frequently share a
    runner) so the status round-trips scale with the number of runners,
    not the number of sessions. Best-effort per runner via
    :func:`_runner_online_or_none`.

    :param rows: Session rows from ``GET /v1/sessions``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: Map of ``runner_id`` → online bool (or ``None`` if the
        lookup was inconclusive).
    """
    unique_ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        rid = r.get("runner_id")
        if isinstance(rid, str) and rid and rid not in seen:
            seen.add(rid)
            unique_ids.append(rid)
    results = await asyncio.gather(
        *(_runner_online_or_none(rid, server_client) for rid in unique_ids)
    )
    # strict=True: results is gathered in unique_ids order, so lengths
    # match by construction — assert it rather than silently truncating.
    return dict(zip(unique_ids, results, strict=True))


async def _collect_global_sessions(
    server_client: httpx.AsyncClient,
    agent_name: object,
    *,
    after: str | None,
    limit: int,
) -> _DiscoveryPage:
    """
    Fetch the global session list via ``GET /v1/sessions``, with connectivity.

    Projects each accessible session to ``{session_id, agent_name, title,
    status, runner_id, runner_online, parent_session_id}``.
    ``runner_online`` is resolved once per unique bound runner (see
    :func:`_resolve_runner_online_map`). An optional ``agent_name``
    filters the list server-side. Permission-bounded by the server (the
    runner's request carries the owning user's identity). Best-effort:
    returns ``[]`` on a fetch failure.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param agent_name: Optional agent-name filter; applied only when a
        non-empty string.
    :param after: Server cursor from the previous page, if any.
    :param limit: Maximum number of source rows to fetch.
    :returns: Projected global session entries and continuation metadata.
    """
    params: dict[str, str | int] = {"limit": limit, "order": "desc"}
    if isinstance(agent_name, str) and agent_name:
        params["agent_name"] = agent_name
    if after is not None:
        params["after"] = after
    try:
        resp = await server_client.get("/v1/sessions", params=params, timeout=30.0)
    except Exception:  # noqa: BLE001
        return _DiscoveryPage([], False, failed=True)
    if resp.status_code != 200:
        return _DiscoveryPage([], False, failed=True)
    try:
        body = resp.json()
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _DiscoveryPage([], False, failed=True)
    page = _parse_discovery_page(body)
    if page.failed:
        return page
    rows = page.rows
    online = await _resolve_runner_online_map(rows, server_client)
    return _DiscoveryPage(
        [
            {
                "session_id": r.get("id"),
                # Hide the internal ``-native-ui`` wrapper name (e.g.
                # ``pi-native-ui`` -> ``Pi``) in the global listing too, matching
                # ``sys_session_get_info``. The server-side ``agent_name`` filter
                # above still receives the caller's raw argument unchanged.
                "agent_name": public_agent_name(_optional_string(r.get("agent_name"))),
                "title": r.get("title"),
                "status": r.get("status"),
                "runner_id": r.get("runner_id"),
                "runner_online": online.get(_optional_string(r.get("runner_id")) or ""),
                "parent_session_id": r.get("parent_session_id"),
            }
            for r in rows
        ],
        page.has_more,
        page.next_after,
    )


def _child_rows_to_entries(
    rows: list[_JsonObject],
) -> list[dict[str, str | None]]:
    """
    Map ``child_sessions`` rows to ``sys_session_list`` entries.

    Skips closed and titleless/colonless rows. The server already
    parses ``tool``/``session_name`` from the title (including the
    ``"ui:<agent>:<label>"`` form), so those are reused.

    :param rows: ``data`` rows from ``GET .../child_sessions``.
    :returns: ``[{"agent", "title", "conversation_id"}, ...]``.
    """
    entries: list[dict[str, str | None]] = []
    for row in rows:
        title = _optional_string(row.get("title"))
        labels = _string_mapping(row.get("labels"))
        if not title or ":" not in title or is_session_closed(labels, title):
            continue
        entries.append(
            {
                "agent": _optional_string(row.get("tool")),
                "title": _optional_string(row.get("session_name")),
                "conversation_id": _optional_string(row.get("id")),
            }
        )
    return entries


async def _session_parent_id(
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str | None:
    """
    Return a session's ``parent_session_id`` (None if top-level/unknown).

    Used to decide whether the caller is itself a child — i.e. a
    user-added agent that should also see ``main`` + siblings. Best-
    effort: returns ``None`` on any read failure rather than raising.

    :param conversation_id: The session to inspect.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The parent session id, or ``None``.
    """
    try:
        snap = await server_client.get(f"/v1/sessions/{conversation_id}", timeout=30.0)
    except Exception:  # noqa: BLE001
        return None
    if snap.status_code != 200:
        return None
    parent = snap.json().get("parent_session_id")
    return parent if isinstance(parent, str) and parent else None


async def _session_get_history_via_rest(
    args: _JsonObject,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Read a target session's recent items via ``GET .../items``.

    Mirrors :class:`SysSessionGetHistoryTool`: returns
    ``{"conversation_id", "agent", "title", "items"}`` with items in
    chronological order. The target's ``agent``/``title`` come from its
    session snapshot. Maps a 404 to ``session_not_found`` and a
    403/401 to ``session_out_of_tree`` (the server denied read access,
    so from the caller's vantage the target is outside the sessions it
    may read).

    :param args: Parsed tool arguments; requires ``conversation_id``,
        optional ``tail_items`` and ``content_max_chars``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON peek result, or a JSON error object.
    """
    target_id = args.get("conversation_id")
    if not isinstance(target_id, str) or not target_id:
        return json.dumps(
            {"error": "sys_session_get_history requires a non-empty 'conversation_id' string"}
        )
    tail_items = _clamp_tail_items(args.get("tail_items", _HISTORY_DEFAULT_TAIL))
    if isinstance(tail_items, str):
        return tail_items
    content_max_chars = _clamp_history_content_chars(
        args.get("content_max_chars", _ACTIVITY_MAX_CHARS),
    )
    if isinstance(content_max_chars, str):
        return content_max_chars
    content_max_chars = _bound_history_content_chars(
        tail_items=tail_items,
        content_max_chars=content_max_chars,
    )
    try:
        resp = await server_client.get(
            f"/v1/sessions/{target_id}/items",
            params={"limit": tail_items, "order": "desc"},
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_get_history failed: {exc}"})
    if resp.status_code == 404:
        return json.dumps({"error": "session_not_found", "conversation_id": target_id})
    if resp.status_code in (401, 403):
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_id})
    if resp.status_code != 200:
        return json.dumps({"error": f"sys_session_get_history returned {resp.status_code}"})
    data: list[_JsonObject] = resp.json().get("data", [])
    # ``order="desc"`` returns newest-first; reverse to chronological so
    # the LLM reads top-to-bottom (matches the in-process peek).
    items: list[_JsonObject] = [
        _project_api_item(it, max_chars=content_max_chars) for it in reversed(data)
    ]
    meta = await _fetch_peek_meta(target_id, server_client)
    # A parked elicitation never lands in the conversation store (it
    # lives only in the Omnigent server's pending-elicitations index, replayed
    # on the snapshot), so append the snapshot's outstanding prompts
    # after the stored tail — they are the sub-agent's most recent act.
    items.extend(
        pending_elicitations.project_for_peek(event) for event in meta.pending_elicitations
    )
    return json.dumps(
        {
            "conversation_id": target_id,
            "agent": meta.agent,
            "title": meta.title,
            "items": items,
        }
    )


async def _fetch_close_target(
    target_id: str,
    server_client: httpx.AsyncClient,
) -> _JsonObject | str:
    """
    Fetch + status-classify the close target's session snapshot.

    :param target_id: The conversation id to close, e.g. ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The parsed snapshot dict on HTTP 200; otherwise a JSON
        error string (``session_not_found`` for 404,
        ``session_out_of_tree`` for 401/403, a generic status error
        otherwise) suitable for returning verbatim to the LLM.
    """
    try:
        snap = await server_client.get(f"/v1/sessions/{target_id}", timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_close failed: {exc}"})
    if snap.status_code == 404:
        return json.dumps({"error": "session_not_found", "conversation_id": target_id})
    if snap.status_code in (401, 403):
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_id})
    if snap.status_code != 200:
        return json.dumps({"error": f"sys_session_close returned {snap.status_code}"})
    body = _string_object_dict(snap.json())
    if body is None:
        return json.dumps({"error": "sys_session_close returned malformed session data"})
    return body


async def _close_tree_scope_error(
    target_snap: _JsonObject,
    caller_conversation_id: str,
    target_id: str,
    server_client: httpx.AsyncClient,
) -> str | None:
    """
    Enforce the close tool's spawn-tree gate over REST.

    Mirrors the in-process :func:`_resolve_session_call` check: the
    target must share the caller's ``root_conversation_id`` and must be
    a sub-agent (have a parent). The caller's own root is resolved via
    its session snapshot — a session can always read itself, so this is
    a 200 on the happy path; a non-200 is surfaced as an error rather
    than failing open. A ``None`` root on either side is treated as
    out-of-tree (never a match).

    :param target_snap: The close target's session snapshot dict (from
        :func:`_fetch_close_target`), carrying ``root_conversation_id``
        and ``parent_session_id``.
    :param caller_conversation_id: The calling session's own id, e.g.
        ``"conv_caller"``.
    :param target_id: The target conversation id, echoed into errors,
        e.g. ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``None`` when the target is in-tree and a sub-agent;
        otherwise a JSON error string (``session_out_of_tree`` or
        ``session_not_a_sub_agent``).
    """
    try:
        caller_snap = await server_client.get(
            f"/v1/sessions/{caller_conversation_id}", timeout=30.0
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_close failed: {exc}"})
    if caller_snap.status_code != 200:
        return json.dumps(
            {
                "error": "sys_session_close could not resolve caller session "
                f"{caller_conversation_id!r}"
            }
        )
    caller_root = caller_snap.json().get("root_conversation_id")
    target_root = target_snap.get("root_conversation_id")
    if caller_root is None or target_root != caller_root:
        return json.dumps({"error": "session_out_of_tree", "conversation_id": target_id})
    if target_snap.get("parent_session_id") is None:
        return json.dumps({"error": "session_not_a_sub_agent", "conversation_id": target_id})
    return None


async def _session_close_via_rest(
    args: _JsonObject,
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> str:
    """
    Close a target sub-agent via ``GET`` snapshot + ``PATCH`` metadata.

    Mirrors :class:`SysSessionCloseTool` — including its tree-scoping:
    close is a write, so the target MUST share the caller's spawn tree
    (same ``root_conversation_id``) and MUST itself be a sub-agent (have
    a parent). Without this the REST path would let an agent tombstone
    any session it merely has edit access to — e.g. a sub-agent in one
    of the caller's *other*, unrelated spawn trees — which the in-process
    path forbids. The gate lives in the close tool (via
    :func:`_close_tree_scope_error`), not the PATCH route, because the
    route is a general title/metadata mutator; only the close tool
    carries the spawn-tree contract.

    On success marks the child with ``omnigent.closed=true`` and
    rewrites its internal title to ``"<agent>:<title>:closed:<id>"`` so
    future ``sys_session_send`` calls with the same ``(agent, title)``
    create a fresh child.

    :param args: Parsed tool arguments; requires ``conversation_id``.
    :param conversation_id: The calling session's own id, e.g.
        ``"conv_caller"``. Used to resolve the caller's spawn-tree root
        for the tree-scope check.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON ``{"closed": true, ...}`` on success; a JSON error
        object otherwise: ``session_not_found`` (404),
        ``session_out_of_tree`` (403/401, or the target's root differs
        from the caller's), or ``session_not_a_sub_agent`` (the target
        is a top-level session, not a sub-agent).
    """
    target_id = args.get("conversation_id")
    if not isinstance(target_id, str) or not target_id:
        return json.dumps(
            {"error": "sys_session_close requires a non-empty 'conversation_id' string"}
        )
    target_snap = await _fetch_close_target(target_id, server_client)
    if isinstance(target_snap, str):
        return target_snap
    scope_error = await _close_tree_scope_error(
        target_snap, conversation_id, target_id, server_client
    )
    if scope_error is not None:
        return scope_error
    parsed = _parse_session_title(_optional_string(target_snap.get("title")))
    if parsed.agent is None or parsed.title is None:
        return json.dumps({"error": "session_not_a_sub_agent", "conversation_id": target_id})
    new_title = f"{parsed.agent}:{parsed.title}{_CLOSED_TITLE_INFIX}{target_id}"
    try:
        patch = await server_client.patch(
            f"/v1/sessions/{target_id}",
            json={
                "title": new_title,
                "labels": {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
                # archived=True triggers the server's _spawn_archive_stop reaper,
                # which stops the child's harness / tmux / bridge cluster.
                # Without this the child's OS-process cluster is never reaped.
                "archived": True,
            },
            timeout=30.0,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"sys_session_close failed: {exc}"})
    if patch.status_code != 200:
        return json.dumps({"error": f"sys_session_close returned {patch.status_code}"})
    return json.dumps(
        {
            "closed": True,
            "conversation_id": target_id,
            "agent": parsed.agent,
            "title": parsed.title,
        }
    )


@dataclass
class _PeekMeta:
    """
    Session metadata peek reads off the target's ``GET /v1/sessions/{id}``.

    :param agent: Parsed agent/tool segment of the title, e.g.
        ``"researcher"``; ``None`` when the title isn't sub-agent-shaped.
    :param title: Parsed instance label segment, e.g. ``"auth"``;
        ``None`` in the same case.
    :param pending_elicitations: Outstanding
        ``response.elicitation_request`` event payloads the target is
        parked on, replayed on the snapshot from the Omnigent server's
        :mod:`omnigent.runtime.pending_elicitations` index. Empty list
        when the target has none (or the snapshot couldn't be read).
    """

    agent: str | None
    title: str | None
    pending_elicitations: list[_JsonObject]


async def _fetch_peek_meta(
    target_id: str,
    server_client: httpx.AsyncClient,
) -> _PeekMeta:
    """
    Fetch a session's title + pending elicitations for peek output.

    One snapshot read serves both peek's ``agent``/``title`` labels and
    the parked-elicitation items it appends. Best-effort: returns empty
    fields when the snapshot can't be read, so a miss degrades
    gracefully (peek still returns the stored item tail) rather than
    failing the whole call.

    :param target_id: The session whose snapshot to read.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: The parsed title plus any outstanding elicitation
        payloads (all empty/``None`` on any miss).
    """
    try:
        snap = await server_client.get(f"/v1/sessions/{target_id}", timeout=30.0)
    except Exception:  # noqa: BLE001
        return _PeekMeta(agent=None, title=None, pending_elicitations=[])
    if snap.status_code != 200:
        return _PeekMeta(agent=None, title=None, pending_elicitations=[])
    body = _string_object_dict(snap.json())
    if body is None:
        return _PeekMeta(agent=None, title=None, pending_elicitations=[])
    parsed = _parse_session_title(_optional_string(body.get("title")))
    raw_pending = body.get("pending_elicitations")
    pending = _json_object_list(raw_pending)
    return _PeekMeta(agent=parsed.agent, title=parsed.title, pending_elicitations=pending)


async def execute_tool(
    *,
    tool_name: str,
    arguments: str,
    server_client: httpx.AsyncClient | None = None,
    terminal_registry: TerminalRegistry | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    agent_spec: AgentSpec | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    runner_workspace: Path | None = None,
    mcp_manager: RunnerMcpManager | None = None,
    session_inbox: asyncio.Queue[_JsonObject] | None = None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None = None,
    harness_client: httpx.AsyncClient | None = None,
    publish_event: Callable[[str, _JsonObject], None] | None = None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """
    Execute a tool and return the output string.

    Pure execution — does NOT post the result to the harness.
    Used by ``dispatch_tool_locally`` (which adds the harness
    POST) and by ``_spawn_async_tool`` background tasks (which
    push to the inbox queue instead).

    :param tool_name: Tool to execute, e.g. ``"sys_os_shell"``.
    :param arguments: JSON-encoded arguments string.
    :param publish_event: Callback that puts an SSE event on the
        runner's per-session outbound queue. ``None`` from
        dispatch sites that don't need event emission (e.g.
        async background tools).
    :param resource_registry: Optional session-resource registry used to
        observe tool-launched terminals through the same lifecycle path as
        runner-launched terminals.
    :param filesystem_registry: Optional registry for tracking agent
        file modifications. Forwarded to ``_execute_os_env_tool``
        so that ``sys_os_write`` and ``sys_os_edit`` calls record changed
        paths for the ``GET …/changes`` endpoint. ``sys_os_shell`` is
        not tracked — shell side-effects cannot be attributed to a session.
    :returns: Tool output string.
    """
    if not arguments.strip():
        return json.dumps({"error": "malformed JSON arguments"})
    args, error = parse_json_object_arguments(arguments)
    if error is not None:
        return json.dumps({"error": error})
    assert args is not None

    try:
        if mcp_manager is not None:
            # All MCP tool calls are routed through the AP server's
            # /mcp endpoint, which enforces TOOL_CALL and TOOL_RESULT
            # policies centrally before forwarding to the runner's
            # /mcp/execute. No runner-side policy gate needed.
            if agent_spec is None:
                return "Error: agent_spec not available for MCP dispatch"
            output = await mcp_manager.call_tool(agent_spec, tool_name, args)
        elif tool_name in _OS_ENV_TOOLS:
            output = await _execute_os_env_tool(
                tool_name,
                args,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                runner_workspace=runner_workspace,
                filesystem_registry=filesystem_registry,
            )
        elif tool_name in _REST_TOOLS:
            output = await _execute_rest_tool(
                tool_name,
                args,
                server_client,
                agent_id=agent_id,
                conversation_id=conversation_id,
            )
        elif tool_name in _FILE_TOOLS:
            output = await _execute_file_tool(
                tool_name,
                args,
                server_client,
                conversation_id=conversation_id,
                agent_spec=agent_spec,
                runner_workspace=runner_workspace,
            )
        elif tool_name in _TERMINAL_TOOLS:
            output = await _execute_terminal_tool(
                tool_name,
                args,
                terminal_registry=terminal_registry,
                resource_registry=resource_registry,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
                runner_workspace=runner_workspace,
                session_inbox=session_inbox,
                publish_event=publish_event,
            )
        elif tool_name in _ASYNC_INBOX_TOOLS:
            output = await _execute_async_inbox_tool(
                tool_name,
                args,
                session_inbox=session_inbox,
                session_async_tasks=session_async_tasks,
                harness_client=harness_client,
                server_client=server_client,
                terminal_registry=terminal_registry,
                resource_registry=resource_registry,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
                agent_name=agent_name,
                runner_workspace=runner_workspace,
                mcp_manager=mcp_manager,
                filesystem_registry=filesystem_registry,
            )
        elif tool_name in _SUBAGENT_TOOLS:
            output = await _execute_subagent_tool(
                args,
                server_client=server_client,
                conversation_id=conversation_id,
                agent_spec=agent_spec,
                publish_event=publish_event,
                session_inbox=session_inbox,
            )
        elif tool_name in _LIST_MODELS_TOOLS:
            output = await _execute_list_models_tool(agent_spec=agent_spec)
        elif tool_name in _SESSION_CREATE_TOOLS:
            output = await _execute_session_create(
                args,
                server_client=server_client,
                conversation_id=conversation_id,
                publish_event=publish_event,
                agent_spec=agent_spec,
                runner_workspace=runner_workspace,
            )
        elif tool_name in _SESSION_SELF_WRITE_TOOLS:
            output = await _rename_current_session_via_rest(
                args,
                conversation_id,
                server_client,
            )
        elif tool_name in _SESSION_QUERY_TOOLS:
            output = await _execute_session_query_tool(
                tool_name,
                arguments,
                conversation_id=conversation_id,
                server_client=server_client,
                agent_spec=agent_spec,
            )
        elif tool_name in _WEB_FETCH_TOOLS:
            output = await _execute_web_fetch_tool(
                args,
                server_client=server_client,
                conversation_id=conversation_id,
                agent_spec=agent_spec,
                task_id=task_id,
                publish_event=publish_event,
                session_inbox=session_inbox,
            )
        elif tool_name in _WEB_SEARCH_TOOLS:
            output = await _execute_web_search_tool(
                args,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
            )
        elif tool_name in _NIMBLE_RESEARCH_TOOLS:
            output = await _execute_nimble_research_tool(
                args,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
            )
        elif tool_name in _NIMBLE_EXTRACT_TOOLS:
            output = await _execute_nimble_extract_tool(
                args,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
            )
        elif tool_name in _HINDSIGHT_TOOLS:
            output = await _execute_hindsight_tool(
                args,
                tool_name=tool_name,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
            )
        elif tool_name in _TIMER_TOOLS:
            if tool_name == "sys_timer_set":
                output = await _execute_timer_set(
                    args,
                    server_client=server_client,
                    conversation_id=conversation_id,
                )
            else:
                output = await _execute_timer_cancel(
                    args,
                    conversation_id=conversation_id,
                )
        elif tool_name in _TASK_LIFECYCLE_TOOLS:
            output = await _execute_task_lifecycle_tool(
                args,
                session_async_tasks=session_async_tasks,
                conversation_id=conversation_id,
                server_client=server_client,
            )
        elif tool_name in _SKILL_TOOLS:
            output = _execute_skill_tool(
                tool_name,
                args,
                agent_spec=agent_spec,
                runner_workspace=runner_workspace,
            )
        elif tool_name in _COMMENT_TOOLS:
            output = await _execute_comment_tool(
                tool_name,
                arguments,
                conversation_id=conversation_id,
                server_client=server_client,
            )
        elif tool_name in _AGENT_TOOLS:
            output = await _execute_agent_tool(
                tool_name,
                args,
                server_client=server_client,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                runner_workspace=runner_workspace,
            )
        elif tool_name in _POLICY_TOOLS:
            output = await _execute_policy_tool(
                tool_name,
                arguments,
                conversation_id=conversation_id,
                server_client=server_client,
            )
        elif tool_name in _SCHEDULED_TASK_TOOLS:
            output = await _execute_scheduled_task_tool(
                tool_name,
                arguments,
                server_client=server_client,
            )
        elif tool_name in _BROWSER_TOOLS:
            output = await _execute_browser_tool(
                tool_name,
                args,
                server_client=server_client,
                conversation_id=conversation_id,
            )
        elif _is_spec_local_python_tool(tool_name, agent_spec):
            output = await _execute_local_python_tool(
                tool_name,
                arguments,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
                runner_workspace=runner_workspace,
            )
        elif _is_uc_function_tool(tool_name, agent_spec):
            output = await _execute_uc_function_tool(tool_name, args, agent_spec=agent_spec)
        else:
            output = await _execute_spec_callable_tool(tool_name, args, agent_spec=agent_spec)
    except Exception as exc:
        _logger.exception(
            "tool %s failed",
            tool_name,
            extra={"session_id": conversation_id},
        )
        output = f"Error: {type(exc).__name__}: {exc}"

    return output


# Per-session leading-edge throttle for changed-files invalidation
# signals. A file-mutating tool publishes at most one
# ``session.changed_files.invalidated`` per this window; the web's
# react-query invalidation coalesces bursts and the end-of-turn trailing
# refetch backstops the final state. Leading (not trailing) so there is
# no timer to manage on the dispatch path.
_CHANGED_FILES_SIGNAL_THROTTLE_S = 0.75
# Bound the throttle map so a long-lived runner with churny session ids
# can't grow it without limit. Clearing past the cap only risks one extra
# (harmless) signal for sessions whose timestamp is dropped.
_CHANGED_FILES_SIGNAL_MAX_TRACKED = 4096
_changed_files_last_signal: dict[str, float] = {}
# Tools that can mutate the workspace filesystem. ``sys_os_shell`` is
# included because git-mode change detection derives from `git status`
# and shell edits are otherwise untracked.
_CHANGED_FILES_TOOLS = frozenset(
    {SysOsWriteTool.name(), SysOsEditTool.name(), SysOsShellTool.name()}
)


def _maybe_signal_changed_files(
    conversation_id: str | None,
    publish_event: Callable[[str, _JsonObject], None] | None,
    *,
    now: float,
) -> None:
    """Publish a throttled ``session.changed_files.invalidated`` event.

    Tells the web to refetch the changed-files list (a coarse "something
    changed" signal — per-file events aren't available for git-mode
    workspaces). Leading-edge throttle keyed by session collapses a
    multi-file turn to roughly one refetch trigger.

    :param conversation_id: Session id, or ``None`` (no-op).
    :param publish_event: Per-session SSE emitter, or ``None`` (no-op).
    :param now: Monotonic timestamp, e.g. ``loop.time()``.
    """
    if conversation_id is None or publish_event is None:
        return
    last = _changed_files_last_signal.get(conversation_id, 0.0)
    if now - last < _CHANGED_FILES_SIGNAL_THROTTLE_S:
        return
    if len(_changed_files_last_signal) > _CHANGED_FILES_SIGNAL_MAX_TRACKED:
        _changed_files_last_signal.clear()
    _changed_files_last_signal[conversation_id] = now
    publish_event(
        conversation_id,
        {
            "type": "session.changed_files.invalidated",
            "session_id": conversation_id,
            "environment_id": "default",
        },
    )


async def dispatch_tool_locally(
    *,
    tool_name: str,
    call_id: str,
    arguments: str,
    response_id: str,
    harness_client: httpx.AsyncClient,
    server_client: httpx.AsyncClient | None = None,
    terminal_registry: TerminalRegistry | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    agent_spec: AgentSpec | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    runner_workspace: Path | None = None,
    mcp_manager: RunnerMcpManager | None = None,
    session_inbox: asyncio.Queue[_JsonObject] | None = None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None = None,
    publish_event: Callable[[str, _JsonObject], None] | None = None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """Execute a tool locally and PATCH the result to the harness.

    :param runner_workspace: Optional CLI launch workspace used to
        resolve placeholder cwd values for runner-owned filesystem
        tools.
    :param mcp_manager: When set, dispatch via
        :meth:`RunnerMcpManager.call_tool`. Caller (proxy_stream)
        passes this only for MCP-owned tools.
    :param session_inbox: Per-session asyncio queue for async tool
        completions. ``sys_call_async`` pushes results here;
        ``sys_read_inbox`` drains it.
    :param session_async_tasks: Per-session dict of handle_id →
        ``(Task, cancel_event)`` tuple. Used by ``sys_cancel_async``
        to signal cancellation via the event.
    :param filesystem_registry: Optional registry for tracking agent
        file modifications. Forwarded to ``execute_tool`` so that
        ``sys_os_write`` and ``sys_os_edit`` calls record changed paths
        for the ``GET …/changes`` endpoint.
    :param resource_registry: Optional session-resource registry used to
        observe tool-launched terminals.
    :returns: The tool output string.
    """
    output = await execute_tool(
        tool_name=tool_name,
        arguments=arguments,
        server_client=server_client,
        terminal_registry=terminal_registry,
        resource_registry=resource_registry,
        agent_spec=agent_spec,
        conversation_id=conversation_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_name=agent_name,
        runner_workspace=runner_workspace,
        mcp_manager=mcp_manager,
        session_inbox=session_inbox,
        session_async_tasks=session_async_tasks,
        harness_client=harness_client,
        filesystem_registry=filesystem_registry,
        publish_event=publish_event,
    )

    # A file-mutating tool just ran — nudge the web to refetch the
    # changed-files list (throttled, coalesced client-side).
    if tool_name in _CHANGED_FILES_TOOLS:
        _maybe_signal_changed_files(
            conversation_id,
            publish_event,
            now=asyncio.get_running_loop().time(),
        )

    # POST the result back to the harness as a ``tool_result``
    # event on the session-keyed events endpoint. ``conversation_id``
    # is required: the harness validates the URL segment against
    # its own runner-stamped value and fails 404 on mismatch —
    # without an id we'd be unable to form a valid URL. Fail loud
    # per ``designs/DESIGN_PRINCIPLES.md`` rather than substituting
    # a synthetic default. ``response_id`` is unused at the URL /
    # body level (the harness has at most one in-flight turn so the
    # ``call_id`` alone keys the parked Future) — kept on the
    # function signature for symmetry with callers that track it.
    del response_id  # see comment above — intentionally unused
    if not conversation_id:
        raise ValueError(
            "dispatch_tool_locally requires conversation_id to POST the "
            "harness session-keyed URL; got None/empty"
        )
    try:
        resp = await harness_client.post(
            f"/v1/sessions/{conversation_id}/events",
            json={"type": "tool_result", "call_id": call_id, "output": output},
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "Runner local dispatch tool_result event failed for %s (call_id=%s): %s",
            tool_name,
            call_id,
            exc,
            extra={"session_id": conversation_id},
        )

    return output


# ── OS env tools (OSEnvironment-backed) ──────────────────


def _clone_os_env_spec(spec: OSEnvSpec) -> OSEnvSpec:
    """Return a defensive copy of an OSEnvSpec-like object.

    Uses :func:`dataclasses.replace` so any field added to
    :class:`OSEnvSandboxSpec` or :class:`OSEnvSpec` in the future is
    carried over automatically. Mutable list fields are copied
    explicitly so the clone and the original don't alias the same
    list (which would let one caller's later mutation leak into the
    other's view — a real hazard when the same parent spec is reused
    across many runner-local sys_os_* dispatches).

    Symmetric with :func:`omnigent.inner.terminal._clone_sandbox_spec`;
    both fixes close the same class of bug where hand-enumerated
    field copies silently drop newly-added security-critical fields
    such as ``egress_rules`` and ``egress_allow_private_destinations``.
    """
    sandbox = getattr(spec, "sandbox", None)
    sandbox_copy = None
    if sandbox is not None:
        sandbox_copy = dataclasses.replace(
            sandbox,
            read_paths=list(sandbox.read_paths) if sandbox.read_paths is not None else None,
            write_paths=list(sandbox.write_paths) if sandbox.write_paths is not None else None,
            write_files=list(sandbox.write_files) if sandbox.write_files is not None else None,
            cwd_allow_hidden=(
                list(sandbox.cwd_allow_hidden) if sandbox.cwd_allow_hidden is not None else None
            ),
            env_passthrough=(
                list(sandbox.env_passthrough) if sandbox.env_passthrough is not None else None
            ),
            egress_rules=list(sandbox.egress_rules) if sandbox.egress_rules is not None else None,
        )
    return dataclasses.replace(spec, sandbox=sandbox_copy)


def _runner_default_os_env_cwd(conversation_id: str | None) -> str:
    """Return the cwd for a default runner-owned primary OSEnv."""
    safe_conv = "default"
    if conversation_id:
        safe_conv = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in conversation_id
        )
    root = Path(
        os.environ.get(
            "OMNIGENT_RUNNER_OS_ENV_ROOT",
            str(Path(tempfile.gettempdir()) / "omnigent-runner-os-envs"),
        )
    )
    cwd = root / safe_conv / "workspace"
    cwd.mkdir(parents=True, exist_ok=True)
    return str(cwd)


def _effective_runner_os_env_spec(
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    runner_workspace: Path | None = None,
) -> OSEnvSpec:
    """
    Build the OSEnvSpec used by runner-local sys_os_* dispatch.

    Precedence (per
    designs/SESSION_WORKSPACE_SELECTION.md "How this maps onto runtime"):

    - When ``runner_workspace`` is set, it ALWAYS wins — whether
      the spec's cwd is relative, absolute, or unset. The runner
      workspace is the authoritative starting cwd for both
      CLI-launched sessions (CLI captures ``os.getcwd()`` and
      passes it via ``OMNIGENT_RUNNER_WORKSPACE``) and
      host-launched sessions (host applies the validated picked
      directory). The agent's spec ``cwd`` is treated as a
      boundary at session-create time, not a runtime override.
    - When ``runner_workspace`` is unset (pure local runs without
      the env var), the spec's cwd applies, with placeholder
      values (``.``, ``./``, ``""``, ``None``) substituted by
      a per-conversation tmpdir as before.

    :param agent_spec: Agent spec resolved for the current turn, or
        ``None`` when dispatch only has request-body hints.
    :param conversation_id: Conversation id used to derive the
        per-conversation fallback workspace, e.g. ``"conv_123"``.
    :param runner_workspace: Authoritative runtime cwd for the
        runner, sourced from ``OMNIGENT_RUNNER_WORKSPACE``.
        Overrides the spec's cwd when set.
    :returns: An ``OSEnvSpec`` with a concrete cwd.
    """
    from omnigent.inner.datamodel import OSEnvSpec

    configured = getattr(agent_spec, "os_env", None) if agent_spec is not None else None
    if configured is not None:
        spec = _clone_os_env_spec(configured)
        if runner_workspace is not None:
            # Runner workspace is authoritative — overrides whatever
            # the spec declared (relative or absolute).
            spec.cwd = str(runner_workspace)
        elif spec.cwd in _PLACEHOLDER_CWDS:
            # No runner workspace; spec is relative — fall back to
            # the per-conversation tmpdir so multiple sessions
            # don't collide on a shared default cwd.
            spec.cwd = _runner_default_os_env_cwd(conversation_id)
        return spec
    cwd = (
        str(runner_workspace)
        if runner_workspace is not None
        else _runner_default_os_env_cwd(conversation_id)
    )
    return OSEnvSpec(type="caller_process", cwd=cwd)


async def _seed_os_env_snapshot(
    os_env: OSEnvironment,
    path: str,
    filesystem_registry: FilesystemRegistry,
    conversation_id: str,
) -> None:
    """Seed the diff snapshot with *path*'s current content before a write or edit.

    Reads the file via *os_env* and passes the content to
    :meth:`~omnigent.runtime.filesystem_registry.FilesystemRegistry.seed_snapshot`
    so the before/after diff endpoint can show the original content.
    Silently skips when the file does not yet exist (new-file creates have no
    baseline) or when any other read error occurs.

    :param os_env: The :class:`~omnigent.inner.os_env.OSEnvironment` used for
        the current tool dispatch — reused to avoid opening a second connection.
    :param path: Path argument forwarded from the tool call, e.g. ``"src/foo.py"``.
    :param filesystem_registry: Registry that stores the snapshot.
    :param conversation_id: Session scope for the snapshot, e.g. ``"conv_abc123"``.
    """
    try:
        existing = await os_env.read(path=path, offset=1, limit=None)
        if isinstance(existing, dict) and "content" in existing:
            filesystem_registry.seed_snapshot(
                path, existing["content"], session_id=conversation_id
            )
    except Exception:  # noqa: BLE001
        pass  # file does not exist yet or unreadable — no baseline to capture


async def _execute_os_env_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None = None,
    conversation_id: str | None = None,
    runner_workspace: Path | None = None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """
    Execute sys_os_* through a runner-local OSEnvironment.

    :param tool_name: Built-in OS tool name, e.g. ``"sys_os_read"``.
    :param args: Parsed tool-call arguments.
    :param agent_spec: Agent spec resolved for the current turn, or
        ``None`` when unavailable.
    :param conversation_id: Conversation id used for the fallback
        workspace, e.g. ``"conv_123"``.
    :param runner_workspace: Optional CLI launch workspace used for
        placeholder cwd values in remote app sessions.
    :param filesystem_registry: Optional registry for tracking agent
        file modifications. When provided, ``sys_os_write`` and
        ``sys_os_edit`` calls record changed paths so the
        ``GET …/changes`` endpoint can surface them. ``sys_os_shell``
        is not tracked — shell side-effects cannot be attributed to a
        session.
    :returns: Serialized tool result string.
    """
    from omnigent.inner.os_env import _DEFAULT_READ_LIMIT, create_os_environment

    os_env = None
    try:
        os_env = create_os_environment(
            _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
        )
        if os_env is None:
            return "Error: unable to create OSEnvironment"

        if tool_name == SysOsReadTool.name():
            result = await os_env.read(
                path=cast("str", args.get("path", "")),
                offset=cast("int", args.get("offset", 1)),
                # Unspecified limit → agent-tool default (2 000 lines).
                # None is now "unlimited" in _read_impl, so we must be explicit.
                # Use is-None check (not `or`) so that invalid values like 0 are
                # forwarded to os_env.read for validation rather than silently
                # replaced with the default.
                limit=cast(
                    "int | None",
                    lv if (lv := args.get("limit")) is not None else _DEFAULT_READ_LIMIT,
                ),
            )
        elif tool_name == SysOsWriteTool.name():
            _path = cast("str", args.get("path", ""))
            if filesystem_registry is not None and conversation_id is not None:
                await _seed_os_env_snapshot(os_env, _path, filesystem_registry, conversation_id)
            result = await os_env.write(
                path=_path,
                content=cast("str", args.get("content", "")),
            )
            if filesystem_registry is not None and conversation_id is not None:
                # _write_impl returns {"created": True} when the file did not
                # previously exist, {"created": False} for an overwrite.
                was_created = isinstance(result, dict) and result.get("created") is True
                status = "created" if was_created else "modified"
                filesystem_registry.record_change(_path, status, conversation_id)
        elif tool_name == SysOsEditTool.name():
            _path = cast("str", args.get("path", ""))
            if filesystem_registry is not None and conversation_id is not None:
                await _seed_os_env_snapshot(os_env, _path, filesystem_registry, conversation_id)
            result = await os_env.edit(
                path=_path,
                old_text=cast("str | None", args.get("oldText") or args.get("old_string")),
                new_text=cast("str | None", args.get("newText") or args.get("new_string")),
                edits=cast("list[dict[str, str]] | None", args.get("edits")),
            )
            if filesystem_registry is not None and conversation_id is not None:
                filesystem_registry.record_change(_path, "modified", conversation_id)
        elif tool_name == SysOsShellTool.name():
            result = await os_env.shell(
                command=cast("str", args.get("command", "")),
                timeout=cast("int | None", args.get("timeout")),
            )
        else:
            return f"Error: {tool_name} not implemented"
    except Exception as exc:
        _logger.exception(
            "runner OSEnvironment dispatch failed for %s",
            tool_name,
            extra={"session_id": conversation_id},
        )
        return json.dumps({"error": str(exc)})
    finally:
        if os_env is not None:
            os_env.close()

    return json.dumps(result)


# ── REST-backed tools (Phase 1) ──────────────────────────


async def _execute_rest_tool(
    tool_name: str,
    args: _JsonObject,
    server_client: httpx.AsyncClient | None,
    agent_id: str | None = None,
    conversation_id: str | None = None,
) -> str:
    """Execute a REST-backed tool by calling server APIs.

    Uses the ``/v1/sessions`` API: creates a child session,
    posts a message event to kick off the turn, and returns the
    session_id as the handle. Cancellation sends an interrupt
    event to the child session.

    :param tool_name: The tool to execute, e.g.
        ``"sys_call_async"``.
    :param args: Tool arguments from the LLM.
    :param server_client: httpx client pointed at the Omnigent server.
    :param agent_id: Durable agent id, e.g. ``"ag_abc123"``.
        Required from the session context.
    :param conversation_id: Parent conversation id, e.g.
        ``"conv_abc123"``. Used to look up the runner binding
        on the parent session so the child session can be bound
        to the same runner.
    :returns: JSON result string for the LLM.
    """
    if server_client is None:
        return f"Error: {tool_name} requires server access"

    if tool_name == SysCallAsyncTool.name():
        # agent_id must be provided by the session context.
        resolved_agent_id = agent_id
        if resolved_agent_id is None:
            return "Error: sys_call_async requires agent_id from the session context"

        input_items = args.get("input") or [{"role": "user", "content": args.get("prompt", "")}]
        try:
            # Create a child session bound to the same agent.
            create_resp = await server_client.post(
                "/v1/sessions",
                json={"agent_id": resolved_agent_id},
                timeout=30.0,
            )
            if create_resp.status_code not in (200, 201):
                return (
                    f"Error: sys_call_async session create returned "
                    f"{create_resp.status_code}: {create_resp.text[:200]}"
                )
            session_id = create_resp.json()["id"]

            # Bind to the parent's runner so event forwarding works.
            if conversation_id is not None:
                try:
                    parent_resp = await server_client.get(
                        f"/v1/sessions/{conversation_id}",
                        timeout=10.0,
                    )
                    if parent_resp.status_code == 200:
                        parent_runner = parent_resp.json().get("runner_id")
                        if parent_runner:
                            await server_client.patch(
                                f"/v1/sessions/{session_id}",
                                json={"runner_id": parent_runner},
                                timeout=10.0,
                            )
                except httpx.HTTPError:
                    _logger.debug(
                        "sys_call_async: failed to bind runner for child session %s",
                        session_id,
                        exc_info=True,
                    )

            # Post the message event to start the turn.
            content = input_items
            if isinstance(content, str):
                content = [{"type": "input_text", "text": content}]
            event_body: _JsonObject = {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": content,
                },
            }
            event_resp = await server_client.post(
                f"/v1/sessions/{session_id}/events",
                json=event_body,
                timeout=30.0,
            )
            if event_resp.status_code >= 400:
                return (
                    f"Error: sys_call_async event post returned "
                    f"{event_resp.status_code}: {event_resp.text[:200]}"
                )
            return json.dumps(
                {
                    "handle_id": session_id,
                    # Compatibility alias for older clients; remove in 0.8.0.
                    "task_id": session_id,
                    "status": "running",
                }
            )
        except Exception as exc:  # noqa: BLE001
            return f"Error: sys_call_async failed: {exc}"

    if tool_name == SysCancelAsyncTool.name():
        # ``task_id`` fallback supports older clients; remove in 0.8.0.
        handle_id = args.get("handle_id") or args.get("task_id", "")
        try:
            resp = await server_client.post(
                f"/v1/sessions/{handle_id}/events",
                json={"type": "interrupt", "data": {}},
                timeout=30.0,
            )
            if resp.status_code in (200, 201, 202):
                return f"Cancelled async handle {handle_id}"
            return f"Error: sys_cancel_async returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: sys_cancel_async failed: {exc}"

    return f"Error: {tool_name} not implemented in REST dispatch"


# ── File tools (Phase 1) ──────────────────────────────────


async def _execute_file_tool(
    tool_name: str,
    args: _JsonObject,
    server_client: httpx.AsyncClient | None,
    *,
    conversation_id: str | None,
    agent_spec: AgentSpec | None = None,
    runner_workspace: Path | None = None,
) -> str:
    """
    Execute a file tool by calling session-scoped server file APIs.

    :param tool_name: File tool name, e.g. ``"upload_file"``.
    :param args: Parsed tool arguments.
    :param server_client: HTTP client for the Omnigent server.
    :param conversation_id: Owning session/conversation id,
        e.g. ``"conv_abc123"``.
    :param agent_spec: Agent spec resolved for the current turn, used
        (with ``runner_workspace``) to derive the workspace root that
        an ``upload_file`` path is resolved against. ``None`` falls back
        to the per-conversation default workspace.
    :param runner_workspace: Authoritative runtime cwd for the runner,
        sourced from ``OMNIGENT_RUNNER_WORKSPACE``. Combined with
        ``agent_spec`` to compute the workspace containment boundary
        for ``upload_file``.
    :returns: Tool result string.
    """
    if server_client is None:
        return f"Error: {tool_name} requires server access"
    if conversation_id is None:
        return f"Error: {tool_name} requires a session id"
    files_path = f"/v1/sessions/{conversation_id}/resources/files"

    if tool_name == UploadFileTool.name():
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return "Error: sys_upload_file failed: empty path"
        # Resolve the agent-supplied path against the session workspace
        # (the same cwd the sys_os_* tools operate in) and reject any
        # path that escapes it. The read happens in the un-sandboxed
        # runner process, so without this containment an agent could
        # exfiltrate arbitrary host files. Mirrors the
        # builtin UploadFileTool's safe_resolve / sys_agent_download
        # containment checks.
        os_spec = _effective_runner_os_env_spec(agent_spec, conversation_id, runner_workspace)
        assert os_spec.cwd is not None
        workspace = Path(os_spec.cwd)
        try:
            resolved = safe_resolve(path, workspace)
        except ValueError as exc:
            return f"Error: sys_upload_file failed: {exc}"
        filename = resolved.name
        try:
            with open(resolved, "rb") as f:
                content = f.read()
            resp = await server_client.post(
                files_path,
                files={"file": (filename, content)},
                timeout=60.0,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                return json.dumps({"file_id": data.get("id"), "filename": filename})
            return f"Error: upload returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: sys_upload_file failed: {exc}"

    if tool_name == DownloadFileTool.name():
        file_id = args.get("file_id", "")
        try:
            resp = await server_client.get(
                f"{files_path}/{file_id}/content",
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.text
            return f"Error: download returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: {DownloadFileTool.name()} failed: {exc}"

    if tool_name == "list_files":
        try:
            resp = await server_client.get(files_path, timeout=30.0)
            if resp.status_code == 200:
                return json.dumps(resp.json())
            return f"Error: list_files returned {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            return f"Error: list_files failed: {exc}"

    return f"Error: {tool_name} not implemented in file dispatch"


# ── Terminal tools (Phase 2) ──────────────────────────────


async def _execute_terminal_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    terminal_registry: TerminalRegistry | None,
    resource_registry: SessionResourceRegistry | None = None,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    runner_workspace: Path | None = None,
    session_inbox: asyncio.Queue[_JsonObject] | None = None,
    publish_event: Callable[[str, _JsonObject], None] | None = None,
) -> str:
    """Execute a terminal tool using the runner's TerminalRegistry.

    :param runner_workspace: Optional CLI launch workspace passed
        into ``ToolContext.workspace`` for terminal cwd resolution.
    :param session_inbox: Per-session queue drained by
        ``sys_read_inbox``. Accepted at the dispatcher boundary but
        no longer threaded into the launch tool — kept for callers
        that still pass it.
    :param publish_event: Per-session SSE emitter (the runner's
        ``_publish_event``). When set, a fresh ``sys_terminal_launch``
        emits ``session.resource.created`` and a successful
        ``sys_terminal_close`` emits ``session.resource.deleted`` so
        the web rail updates mid-turn instead of waiting for the
        response-end terminals-cache invalidation. ``None`` for
        in-process callers / tests that don't relay.
    :param resource_registry: Optional session-resource registry used to
        observe fresh launches as auxiliary terminal resources.
    """
    import asyncio

    if terminal_registry is None:
        return "Error: terminal_registry not available in runner"
    if agent_spec is None:
        return "Error: agent_spec not available for terminal dispatch"
    if conversation_id is None:
        return "Error: conversation_id required for terminal tools"

    from omnigent.tools.base import ToolContext

    ctx = ToolContext(
        task_id=task_id or "unknown",
        agent_id=agent_id or "unknown",
        workspace=runner_workspace,
        conversation_id=conversation_id,
    )

    del session_inbox
    if tool_name == SysTerminalLaunchTool.name():
        tool_instance: Tool = SysTerminalLaunchTool(
            spec=agent_spec,
            registry=terminal_registry,
        )
    elif tool_name == SysTerminalSendTool.name():
        tool_instance = SysTerminalSendTool(registry=terminal_registry)
    elif tool_name == SysTerminalReadTool.name():
        tool_instance = SysTerminalReadTool(registry=terminal_registry)
    elif tool_name == SysTerminalListTool.name():
        tool_instance = SysTerminalListTool(registry=terminal_registry)
    elif tool_name == SysTerminalCloseTool.name():
        tool_instance = SysTerminalCloseTool(registry=terminal_registry)
    else:
        return f"Error: unknown terminal tool {tool_name}"

    arguments_str = json.dumps(args)

    # Terminal tools use blocking tmux APIs; bridge via to_thread.
    try:
        output = await asyncio.to_thread(tool_instance.invoke, arguments_str, ctx)
    except Exception as exc:  # noqa: BLE001
        return f"Error: {tool_name} failed: {type(exc).__name__}: {exc}"

    # Surface the resource lifecycle on the live SSE stream. The
    # tool ran in the runner process, where ``session_stream`` (the
    # AP-server pub-sub the web UI subscribes to) has no subscribers;
    # ``publish_event`` is the runner's own per-session queue, which
    # the Omnigent server's relay republishes onto ``session_stream``.
    if publish_event is not None and tool_name in (
        SysTerminalLaunchTool.name(),
        SysTerminalCloseTool.name(),
    ):
        await _emit_terminal_resource_event(
            tool_name=tool_name,
            output=output,
            args=args,
            conversation_id=conversation_id,
            terminal_registry=terminal_registry,
            resource_registry=resource_registry,
            publish_event=publish_event,
        )
    return output


async def _emit_terminal_resource_event(
    *,
    tool_name: str,
    output: str,
    args: _JsonObject,
    conversation_id: str,
    terminal_registry: TerminalRegistry,
    resource_registry: SessionResourceRegistry | None,
    publish_event: Callable[[str, _JsonObject], None],
) -> None:
    """Emit a ``session.resource.{created,deleted}`` event for a terminal tool.

    Parses the terminal tool's JSON envelope and pushes a matching
    SSE event onto ``publish_event`` so live subscribers (the web
    rail) see tool-launched / tool-closed terminals immediately. The
    event shapes match the REST resource path
    (:func:`omnigent.server.routes.sessions._publish_and_persist_resource_event`)
    so the AP-server relay and the web UI handle both surfaces
    identically.

    Best-effort: a malformed / error envelope, an unexpected status,
    or a registry miss is a silent no-op — the snapshot endpoint
    (``GET /resources/terminals``) plus the response-end cache
    invalidation remain the source of truth for reconnecting clients.

    :param tool_name: The terminal tool name, e.g.
        ``"sys_terminal_launch"`` or ``"sys_terminal_close"``.
    :param output: The tool's JSON-encoded result envelope, e.g.
        ``{"terminal": "bash", "session": "s1", "status": "launched"}``.
    :param args: Parsed launch / close arguments — fallback source
        for ``terminal`` / ``session`` if the envelope omits them.
    :param conversation_id: Owning conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_registry: The runner's ``TerminalRegistry``,
        used to look up the live instance for a fresh launch.
    :param resource_registry: Optional session-resource registry used to
        observe fresh launches as auxiliary terminal resources.
    :param publish_event: The runner's per-session SSE emitter.
    """
    try:
        envelope = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(envelope, dict):
        return
    terminal_name = envelope.get("terminal") or args.get("terminal")
    session_key = envelope.get("session") or args.get("session")
    if not isinstance(terminal_name, str) or not isinstance(session_key, str):
        return

    status = envelope.get("status")
    if tool_name == SysTerminalLaunchTool.name() and status == "launched":
        await _publish_terminal_created_event(
            conversation_id=conversation_id,
            terminal_name=terminal_name,
            session_key=session_key,
            terminal_registry=terminal_registry,
            resource_registry=resource_registry,
            publish_event=publish_event,
        )
    elif tool_name == SysTerminalCloseTool.name() and status == "closed":
        _publish_terminal_deleted_event(
            conversation_id=conversation_id,
            terminal_name=terminal_name,
            session_key=session_key,
            publish_event=publish_event,
        )


async def _publish_terminal_created_event(
    *,
    conversation_id: str,
    terminal_name: str,
    session_key: str,
    terminal_registry: TerminalRegistry,
    resource_registry: SessionResourceRegistry | None,
    publish_event: Callable[[str, _JsonObject], None],
) -> None:
    """Build and publish ``session.resource.created`` for a fresh launch.

    Looks up the live :class:`TerminalInstance` from the registry and
    projects it through :func:`terminal_resource_view` so the wire
    shape exactly matches the REST resource path. A registry miss
    (the instance vanished between launch and lookup) is a silent
    no-op.

    :param conversation_id: Owning conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_name: Terminal spec name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param terminal_registry: The runner's ``TerminalRegistry``.
    :param resource_registry: Optional session-resource registry used to
        observe the launched terminal as auxiliary.
    :param publish_event: The runner's per-session SSE emitter.
    """
    from omnigent.entities.session_resources import session_resource_view_to_dict

    instance = terminal_registry.get(conversation_id, terminal_name, session_key)
    if instance is None:
        return
    if resource_registry is not None:
        try:
            view = await resource_registry.observe_auxiliary_terminal(
                conversation_id,
                terminal_name,
                session_key,
                instance,
            )
        except Exception:
            _logger.exception(
                "Failed to observe tool-launched terminal: session=%s terminal=%s:%s",
                conversation_id,
                terminal_name,
                session_key,
                extra={"session_id": conversation_id},
            )
            return
        resource = session_resource_view_to_dict(view)
    else:
        from omnigent.entities.session_resources import terminal_resource_view
        from omnigent.terminals.registry import TerminalListEntry

        entry = TerminalListEntry(
            terminal_name=terminal_name,
            session_key=session_key,
            instance=instance,
        )
        resource = session_resource_view_to_dict(terminal_resource_view(conversation_id, entry))
    publish_event(
        conversation_id,
        {"type": "session.resource.created", "resource": resource},
    )

    # Legacy fallback for callers that do not have a SessionResourceRegistry:
    # start the runner-side pane-activity watcher here so the web "active"
    # badge still works. Normal runner dispatch uses observe_auxiliary_terminal
    # above, which owns the watcher and terminal-exit lifecycle semantics.
    if resource_registry is not None:
        return
    resource_id = resource["id"]
    if isinstance(resource_id, str) and resource_id:
        loop = asyncio.get_running_loop()

        def _on_activity() -> None:
            event: _JsonObject = {
                "type": "session.terminal.activity",
                "session_id": conversation_id,
                "terminal_id": resource_id,
            }
            loop.call_soon_threadsafe(
                publish_event,
                conversation_id,
                event,
            )

        instance.start_idle_watcher_thread(on_activity=_on_activity)


def _publish_terminal_deleted_event(
    *,
    conversation_id: str,
    terminal_name: str,
    session_key: str,
    publish_event: Callable[[str, _JsonObject], None],
) -> None:
    """Build and publish ``session.resource.deleted`` for a closed terminal.

    The delete event carries only the deterministic resource id (no
    instance lookup needed), matching the shape the REST resource
    path emits via ``_publish_and_persist_resource_event``.

    :param conversation_id: Owning conversation id, e.g.
        ``"conv_abc123"``.
    :param terminal_name: Terminal spec name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param publish_event: The runner's per-session SSE emitter.
    """
    from omnigent.entities.session_resources import terminal_resource_id

    publish_event(
        conversation_id,
        {
            "type": "session.resource.deleted",
            "resource_id": terminal_resource_id(terminal_name, session_key),
            "resource_type": "terminal",
            "session_id": conversation_id,
        },
    )


# ── Async inbox tools (Step 7) ───────────────────────────


async def _execute_async_inbox_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    session_inbox: asyncio.Queue[_JsonObject] | None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    server_client: httpx.AsyncClient | None,
    terminal_registry: TerminalRegistry | None,
    resource_registry: SessionResourceRegistry | None,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    agent_name: str | None,
    runner_workspace: Path | None,
    mcp_manager: RunnerMcpManager | None,
    filesystem_registry: FilesystemRegistry | None = None,
    harness_client: httpx.AsyncClient | None = None,
) -> str:
    """
    Runner-local dispatch for async inbox tools.

    Backed by per-session ``asyncio.Queue`` (SESSION_REARCHITECTURE
    Step 7).

    :param tool_name: Tool name, e.g. ``"sys_read_inbox"``.
    :param args: Parsed JSON arguments from the LLM.
    :param session_inbox: Per-session completion queue.
    :param session_async_tasks: Per-session handle_id →
        ``(Task, cancel_event)`` tuple map.
    :param filesystem_registry: Optional registry for tracking file
        changes made by tools spawned via ``sys_call_async``.
        Forwarded to ``_spawn_async_tool`` so that async OS-env tool
        calls record paths for the ``GET …/changes`` endpoint.
    :param resource_registry: Optional session-resource registry used by
        async terminal-tool launches.
    :param harness_client: Unused; kept for caller compatibility.
    :returns: Tool output string.
    """
    del harness_client
    if tool_name == SysReadInboxTool.name():
        return await _drain_inbox(
            session_inbox,
            server_client=server_client,
            conversation_id=conversation_id,
        )

    if tool_name == SysCallAsyncTool.name():
        return _spawn_async_tool(
            args,
            session_inbox=session_inbox,
            session_async_tasks=session_async_tasks,
            server_client=server_client,
            terminal_registry=terminal_registry,
            resource_registry=resource_registry,
            agent_spec=agent_spec,
            conversation_id=conversation_id,
            task_id=task_id,
            agent_id=agent_id,
            agent_name=agent_name,
            runner_workspace=runner_workspace,
            mcp_manager=mcp_manager,
            filesystem_registry=filesystem_registry,
        )

    if tool_name == SysCancelAsyncTool.name():
        return _cancel_async_tool(
            args,
            session_async_tasks=session_async_tasks,
        )

    return f"Error: {tool_name} not implemented in async inbox dispatch"


def _format_terminal_idle_item(
    payload: _JsonObject,
) -> str:
    """
    Render a terminal-idle inbox item for ``sys_read_inbox``.

    :param payload: Canonical terminal-idle inbox payload.
    :returns: Human-readable inbox line.
    :raises ValueError: If the payload is missing required fields or
        top-level and content identities disagree.
    """
    payload_type = payload.get("type")
    source = payload.get("source")
    session = payload.get("session")
    content = payload.get("content")
    if payload_type != "terminal_idle":
        raise ValueError("terminal-idle inbox payload must have type 'terminal_idle'")
    if not isinstance(source, str) or not source:
        raise ValueError("terminal-idle inbox payload requires non-empty string source")
    if not isinstance(session, str) or not session:
        raise ValueError("terminal-idle inbox payload requires non-empty string session")
    if not isinstance(content, dict):
        raise ValueError("terminal-idle inbox payload requires object content")
    if content.get("status") != "idle":
        raise ValueError("terminal-idle inbox payload content.status must be 'idle'")
    if content.get("terminal") != source or content.get("session") != session:
        raise ValueError(
            "terminal-idle inbox payload content terminal/session must match source/session"
        )
    return f"[System: inbox item terminal_idle — terminal {source}:{session} is idle]"


def _truncate_inbox_output(output: object) -> str:
    """
    Convert an inbox payload output to bounded text.

    :param output: Raw payload output, e.g. ``"done"`` or an error
        object converted by the caller.
    :returns: Text capped for LLM delivery.
    """
    text = output if isinstance(output, str) else str(output)
    if len(text) <= _INBOX_OUTPUT_MAX_CHARS:
        return text
    return (
        text[:_INBOX_OUTPUT_MAX_CHARS].rstrip()
        + f"\n...[truncated {len(text) - _INBOX_OUTPUT_MAX_CHARS} chars]"
    )


def _format_async_task_item(payload: _JsonObject) -> str:
    """
    Render a completed/failed/cancelled async-task inbox payload.

    :param payload: Async-task payload with ``handle_id``,
        ``tool_name``, ``status``, ``output`` keys.
    :returns: Human-readable inbox line.
    """
    handle_id = payload.get("handle_id", "unknown")
    tool = payload.get("tool_name", "unknown")
    status = payload.get("status", "unknown")
    output = _truncate_inbox_output(payload.get("output", ""))
    # An empty completion (e.g. a native child that idled with no assistant
    # text — the runner delivers "" rather than fabricating from stale
    # history) must read as "produced no output", not a dangling
    # "…returned: " that the parent LLM mistakes for a truncated handoff.
    has_output = bool(output and output.strip())
    if payload.get("type") == "sub_agent":
        agent = payload.get("agent") or payload.get("tool_name", "sub_agent")
        title = payload.get("title", "")
        target = f"{agent}:{title}" if title else str(agent)
        if status == "completed":
            if not has_output:
                return (
                    f"[System: sub-agent task {handle_id} completed — {target} produced no output]"
                )
            return f"[System: sub-agent task {handle_id} completed — {target} returned: {output}]"
        if status == "failed":
            return f"[System: sub-agent task {handle_id} failed — {target} error: {output}]"
        if status == "cancelled":
            return f"[System: sub-agent task {handle_id} cancelled — {target}]"
        return f"[System: sub-agent task {handle_id} {status} — {target}: {output}]"
    if status == "completed":
        if not has_output:
            return f"[System: task {handle_id} completed — {tool} produced no output]"
        return f"[System: task {handle_id} completed — {tool} returned: {output}]"
    if status == "failed":
        return f"[System: task {handle_id} failed — {tool} error: {output}]"
    if status == "cancelled":
        return f"[System: task {handle_id} cancelled]"
    return f"[System: task {handle_id} {status} — {tool}: {output}]"


def _subagent_child_id(payload: _JsonObject) -> str | None:
    """
    Extract the child session id from a sub-agent inbox payload.

    :param payload: Inbox payload, e.g. a ``type="sub_agent"`` item.
    :returns: Child session id, or ``None`` when absent.
    """
    for key in ("conversation_id", "task_id", "handle_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _subagent_policy_failure_payload(payload: _JsonObject) -> _JsonObject:
    """
    Return a fail-closed copy of a sub-agent inbox payload.

    :param payload: Original inbox payload.
    :returns: Payload with output replaced by a policy-failure
        sentinel.
    """
    return {**payload, "output": _SUBAGENT_POLICY_FAILURE_OUTPUT}


def _subagent_tool_result_policy_request(
    payload: _JsonObject,
    output: str,
) -> _JsonObject:
    """
    Build the Omnigent policy-evaluation request for delayed child output.

    :param payload: Completed sub-agent inbox payload.
    :param output: Raw child output text.
    :returns: JSON body for ``POST /policies/evaluate``.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_RESULT",
            "data": {"result": output},
            "request_data": {
                "name": "sys_session_send",
                "tool": "sys_session_send",
                "args": {
                    "agent": payload.get("agent") or payload.get("tool_name"),
                    "title": payload.get("title"),
                    "conversation_id": _subagent_child_id(payload),
                },
            },
        }
    }


async def _post_subagent_policy_verdict(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    payload: _JsonObject,
    output: str,
) -> _JsonObject | None:
    """
    POST delayed sub-agent output to Omnigent policy evaluation.

    :param server_client: HTTP client pointed at Omnigent server.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param payload: Completed sub-agent inbox payload.
    :param output: Raw child output text.
    :returns: Parsed policy verdict, or ``None`` on failure.
    """
    try:
        resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json=_subagent_tool_result_policy_request(payload, output),
            timeout=30.0,
        )
    except httpx.HTTPError:
        _logger.exception(
            "Sub-agent inbox TOOL_RESULT policy evaluation failed for parent=%s child=%s",
            conversation_id,
            _subagent_child_id(payload),
            extra={"session_id": conversation_id},
        )
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Sub-agent inbox TOOL_RESULT policy evaluation rejected for "
            "parent=%s status=%s body=%s",
            conversation_id,
            resp.status_code,
            resp.text,
            extra={"session_id": conversation_id},
        )
        return None
    try:
        return _string_object_dict(resp.json())
    except (json.JSONDecodeError, ValueError):
        _logger.warning(
            "Sub-agent inbox TOOL_RESULT policy evaluation returned non-JSON for parent=%s",
            conversation_id,
            extra={"session_id": conversation_id},
        )
        return None


def _apply_subagent_policy_verdict(
    payload: _JsonObject,
    verdict: _JsonObject,
) -> _SubagentInboxEvaluation:
    """
    Apply an Omnigent policy verdict to a sub-agent inbox payload.

    :param payload: Original completed sub-agent payload.
    :param verdict: Parsed Omnigent policy response, e.g.
        ``{"result": "POLICY_ACTION_ALLOW"}``.
    :returns: Evaluation result for ``sys_read_inbox`` formatting.
    """
    result = verdict.get("result")
    if result in {"POLICY_ACTION_DENY", "POLICY_ACTION_ASK"}:
        reason = verdict.get("reason") or "no reason given"
        return _SubagentInboxEvaluation(
            {**payload, "output": f"[Result suppressed by policy: {reason}]"}
        )
    if result in {"POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"}:
        transformed = verdict.get("data")
        if transformed is None:
            return _SubagentInboxEvaluation(payload)
        if not isinstance(transformed, str):
            _logger.warning(
                "Sub-agent inbox TOOL_RESULT policy data must be str; got %s",
                type(transformed).__name__,
                extra={"session_id": runner_primary_session_id()},
            )
        return _SubagentInboxEvaluation(
            {
                **payload,
                "output": transformed if isinstance(transformed, str) else str(transformed),
            }
        )
    _logger.warning(
        "Sub-agent inbox TOOL_RESULT policy evaluation returned unknown result=%r",
        result,
        extra={"session_id": runner_primary_session_id()},
    )
    return _SubagentInboxEvaluation(
        _subagent_policy_failure_payload(payload),
        retry_original=True,
    )


async def _evaluate_subagent_inbox_output(
    payload: _JsonObject,
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
) -> _SubagentInboxEvaluation:
    """
    Apply parent TOOL_RESULT policy to a delayed sub-agent payload.

    :param payload: Inbox payload for a completed sub-agent task.
    :param server_client: HTTP client pointed at Omnigent server.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Evaluation result carrying the safe payload plus retry
        metadata for transient evaluation failures.
    """
    if (
        payload.get("type") != "sub_agent"
        or payload.get("status") not in _SUBAGENT_POLICY_STATUSES
    ):
        return _SubagentInboxEvaluation(payload)
    output = payload.get("output")
    if not isinstance(output, str) or server_client is None or conversation_id is None:
        return _SubagentInboxEvaluation(
            _subagent_policy_failure_payload(payload),
            retry_original=True,
        )
    verdict = await _post_subagent_policy_verdict(
        server_client=server_client,
        conversation_id=conversation_id,
        payload=payload,
        output=output,
    )
    if verdict is None:
        return _SubagentInboxEvaluation(
            _subagent_policy_failure_payload(payload),
            retry_original=True,
        )
    return _apply_subagent_policy_verdict(payload, verdict)


def _cleanup_drained_subagent_work(payload: _JsonObject) -> None:
    """
    Remove terminal sub-agent work after its inbox item is drained.

    :param payload: Drained inbox payload.
    :returns: None.
    """
    if payload.get("type") != "sub_agent":
        return
    if payload.get("status") not in _SUBAGENT_INBOX_TERMINAL_STATUSES:
        return
    child_id = _subagent_child_id(payload)
    if child_id is None:
        return
    work_id = payload.get("work_id")
    if not isinstance(work_id, str) or not work_id:
        return
    from omnigent.runner import app as _runner_app

    _runner_app.unregister_subagent_work(
        child_id,
        work_id=work_id,
        remember_drained_delivery=True,
    )


async def _drain_inbox(
    inbox: asyncio.Queue[_JsonObject] | None,
    *,
    server_client: httpx.AsyncClient | None = None,
    conversation_id: str | None = None,
) -> str:
    """
    Non-blocking drain of the per-session inbox queue.

    Returns formatted completion payloads or "Inbox is empty."

    :param inbox: The session's asyncio.Queue, or ``None`` if
        no queue has been created yet.
    :param server_client: HTTP client pointed at Omnigent server.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Formatted string of completed tasks.
    """
    if inbox is None or inbox.empty():
        return "Inbox is empty — no completed tasks."
    items: list[str] = []
    retry_payloads: list[_JsonObject] = []
    while not inbox.empty():
        try:
            payload = inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if payload.get("type") == "terminal_idle":
            try:
                items.append(_format_terminal_idle_item(payload))
            except ValueError as exc:
                _logger.warning(
                    "malformed terminal-idle inbox item ignored: %s",
                    exc,
                    exc_info=True,
                    extra={"session_id": conversation_id},
                )
                items.append(f"[System: malformed terminal_idle inbox item ignored — {exc}]")
            continue
        evaluation = await _evaluate_subagent_inbox_output(
            payload,
            server_client=server_client,
            conversation_id=conversation_id,
        )
        items.append(_format_async_task_item(evaluation.payload))
        if evaluation.retry_original:
            retry_payloads.append(payload)
        else:
            _cleanup_drained_subagent_work(evaluation.payload)
    for payload in retry_payloads:
        inbox.put_nowait(payload)
    return "\n\n".join(items) if items else "Inbox is empty — no completed tasks."


async def _evaluate_async_tool_call_policy(
    tool_name: str,
    tool_args: str,
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
) -> bool:
    """
    Evaluate PHASE_TOOL_CALL policy for an out-of-turn background dispatch.

    Calls the AP server's policy-evaluate endpoint directly (no SSE
    round-trip, since the originating turn has already ended).

    ``arguments`` is sent as a dict (not a JSON string) so the server's policy
    context builder and argument-aware built-in policies (e.g. safety rules
    that inspect ``arguments.command``) see the same structure every in-turn
    evaluation path delivers.

    An ASK verdict parks the gate server-side (up to the policy's
    ``ask_timeout``) and blocks the background task until resolved or timed
    out — ``sys_cancel_async`` cannot interrupt a parked evaluation.

    :returns: ``True`` when the tool may proceed; ``False`` to DENY.
    """
    evaluation_id = f"poleval_async_{uuid.uuid4().hex[:12]}"
    phase = "PHASE_TOOL_CALL"
    try:
        try:
            arguments_dict: _JsonObject = json.loads(tool_args)
            if not isinstance(arguments_dict, dict):
                arguments_dict = {}
        except (json.JSONDecodeError, ValueError):
            arguments_dict = {}
        resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json={
                "event": {"type": phase, "data": {"name": tool_name, "arguments": arguments_dict}}
            },
            timeout=_ASK_GATE_DELIVERY_TIMEOUT,
        )
        if resp.status_code == 200:
            result = _string_object_dict(resp.json())
            if result is None:
                return False
            action = result.get("result", "POLICY_ACTION_DENY")
            return bool(action == "POLICY_ACTION_ALLOW" or action == "POLICY_ACTION_UNSPECIFIED")
        _logger.warning(
            "async PHASE_TOOL_CALL policy evaluate returned %d for %s; denying",
            resp.status_code,
            evaluation_id,
            extra={"session_id": conversation_id},
        )
    except Exception:  # noqa: BLE001
        _logger.warning(
            "async PHASE_TOOL_CALL policy evaluate failed for %s; denying",
            evaluation_id,
            exc_info=True,
            extra={"session_id": conversation_id},
        )
    return False


def _spawn_async_tool(
    args: _JsonObject,
    *,
    session_inbox: asyncio.Queue[_JsonObject] | None,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    server_client: httpx.AsyncClient | None,
    terminal_registry: TerminalRegistry | None,
    resource_registry: SessionResourceRegistry | None,
    agent_spec: AgentSpec | None,
    conversation_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    agent_name: str | None,
    runner_workspace: Path | None,
    mcp_manager: RunnerMcpManager | None,
    filesystem_registry: FilesystemRegistry | None = None,
) -> str:
    """
    Spawn a tool as a background asyncio.Task.

    Returns a handle immediately. On completion, the result is
    pushed to the session's inbox queue for ``sys_read_inbox``
    to drain.

    :param args: Must contain ``"tool"`` (target tool name) and
        ``"args"`` (JSON string of target tool arguments).
    :param filesystem_registry: Optional registry forwarded to
        ``execute_tool`` so that OS-env tools invoked via
        ``sys_call_async`` record file changes for the
        ``GET …/changes`` endpoint.
    :param resource_registry: Optional session-resource registry used by
        async terminal-tool launches.
    :returns: JSON handle string with canonical ``handle_id``,
        plus compatibility ``task_id`` (identical value; remove in 0.8.0),
        ``tool_name``, ``status``, and ``message``. Prefer
        ``handle_id``; ``task_id`` exists only so older clients
        that still parse the pre-handle_id field keep working.
    """
    target_tool = args.get("tool")
    target_args = args.get("args", "{}")
    if not isinstance(target_tool, str) or not target_tool:
        return 'Error: sys_call_async requires "tool" argument'
    if not isinstance(target_args, str):
        return 'Error: sys_call_async requires string "args" argument'
    if target_tool == SysCallAsyncTool.name():
        return "Error: sys_call_async cannot dispatch itself"
    if session_inbox is None or session_async_tasks is None:
        return "Error: async inbox not initialized for this session"

    handle_id = f"handle_{uuid.uuid4().hex[:12]}"
    cancel_event = asyncio.Event()

    async def _bg() -> str:
        """
        Background task: dispatch the tool and push result to inbox.

        Uses a cancel_event to bail out immediately when
        sys_cancel_async is called — asyncio.Task.cancel() alone
        can't interrupt asyncio.to_thread (the thread keeps running
        until the subprocess finishes).

        :returns: The tool output string.
        """
        try:
            # Evaluate PHASE_TOOL_CALL policy before executing. The originating
            # turn has already ended, so we call the AP server directly instead
            # of going through the SSE round-trip. ASK is treated as DENY —
            # there is no active turn to surface an approval prompt.
            if server_client is not None and conversation_id is not None:
                allowed = await _evaluate_async_tool_call_policy(
                    target_tool,
                    target_args,
                    server_client=server_client,
                    conversation_id=conversation_id,
                )
                if not allowed:
                    result = "[Result suppressed by policy: PHASE_TOOL_CALL denied]"
                    session_inbox.put_nowait(
                        {
                            "handle_id": handle_id,
                            "tool_name": target_tool,
                            "status": "failed",
                            "output": result,
                        }
                    )
                    return result

            # Race the tool execution against the cancel event.
            exec_coro = execute_tool(
                tool_name=target_tool,
                arguments=target_args,
                server_client=server_client,
                terminal_registry=terminal_registry,
                resource_registry=resource_registry,
                agent_spec=agent_spec,
                conversation_id=conversation_id,
                task_id=task_id,
                agent_id=agent_id,
                agent_name=agent_name,
                runner_workspace=runner_workspace,
                mcp_manager=mcp_manager,
                session_inbox=session_inbox if target_tool in _TERMINAL_TOOLS else None,
                filesystem_registry=filesystem_registry,
            )
            execution_task = asyncio.create_task(exec_coro)
            cancellation_task = asyncio.create_task(cancel_event.wait())
            _done, pending = await asyncio.wait(
                [execution_task, cancellation_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task.done():
                # Drop the losing future (the tool coro). This cancels the
                # task/coroutine but cannot interrupt an underlying
                # asyncio.to_thread, so that thread may run to completion.
                for fut in pending:
                    fut.cancel()
                session_inbox.put_nowait(
                    {
                        "handle_id": handle_id,
                        "tool_name": target_tool,
                        "status": "cancelled",
                        "output": "",
                    }
                )
                return ""
            # Drop the losing future (cancel_event.wait()) so it doesn't
            # linger as a pending task for the life of the session.
            for fut in pending:
                fut.cancel()
            result = execution_task.result()
            session_inbox.put_nowait(
                {
                    "handle_id": handle_id,
                    "tool_name": target_tool,
                    "status": "completed",
                    "output": result,
                }
            )
            return result
        except asyncio.CancelledError:
            session_inbox.put_nowait(
                {
                    "handle_id": handle_id,
                    "tool_name": target_tool,
                    "status": "cancelled",
                    "output": "",
                }
            )
            raise
        except Exception as exc:
            _logger.exception("async tool %s failed", target_tool)
            session_inbox.put_nowait(
                {
                    "handle_id": handle_id,
                    "tool_name": target_tool,
                    "status": "failed",
                    "output": str(exc),
                }
            )
            return f"Error: {exc}"
        finally:
            session_async_tasks.pop(handle_id, None)

    bg_task = asyncio.create_task(_bg(), name=f"async-{handle_id}")
    session_async_tasks[handle_id] = (bg_task, cancel_event)

    return json.dumps(
        {
            "handle_id": handle_id,
            # Compatibility alias for older clients; remove in 0.8.0.
            "task_id": handle_id,
            "tool_name": target_tool,
            "status": "in_progress",
            "message": (
                f"[System: {target_tool} dispatched as background "
                f"task {handle_id}. Result will appear in your "
                f"inbox — call sys_read_inbox to check. To abort, "
                f"call sys_cancel_async with handle_id={handle_id!r}.]"
            ),
        }
    )


def _cancel_async_tool_result(
    args: _JsonObject,
    *,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
) -> _CancelAsyncToolResult:
    """
    Cancel an in-flight local async tool by handle id.

    Signals the cancel_event so the background task's
    ``asyncio.wait`` returns immediately — the underlying
    thread may keep running but the task won't block on it.

    :param args: Must contain ``"handle_id"`` (``"task_id"`` is
        accepted as a legacy alias).
    :returns: Structured local-cancel result. ``try_subagent_cancel``
        is true only when no local async task matched.
    """
    handle_id = args.get("handle_id") or args.get("task_id")
    if not isinstance(handle_id, str) or not handle_id:
        return _CancelAsyncToolResult('Error: sys_cancel_async requires "handle_id"')
    if session_async_tasks is None:
        return _CancelAsyncToolResult("Error: async inbox not initialized for this session")
    entry = session_async_tasks.get(handle_id)
    if entry is None:
        return _CancelAsyncToolResult(
            f"Error: no in-flight task with handle_id {handle_id}",
            try_subagent_cancel=True,
        )
    _task, cancel_event = entry
    # Signal the event — _bg's asyncio.wait returns immediately.
    # Don't call task.cancel(): the CancelledError races with
    # the event check and can prevent the inbox push.
    cancel_event.set()
    return _CancelAsyncToolResult(json.dumps({"cancelled": True, "handle_id": handle_id}))


def _cancel_async_tool(
    args: _JsonObject,
    *,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
) -> str:
    """
    Cancel an in-flight async tool by handle_id.

    :param args: Must contain ``"handle_id"`` (``"task_id"`` is
        accepted as a legacy alias).
    :param session_async_tasks: Per-session async task map, or
        ``None`` when async inbox state is unavailable.
    :returns: Confirmation or error string.
    """
    return _cancel_async_tool_result(
        args,
        session_async_tasks=session_async_tasks,
    ).output


async def _execute_task_lifecycle_tool(
    args: _JsonObject,
    *,
    session_async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] | None,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Runner-local handler for ``sys_cancel_task``.

    The generic cancel path first tries the in-memory async dispatches
    tracked in ``session_async_tasks``. If no async tool handle matches,
    it falls through to the sub-agent work registry so handles returned
    by ``sys_session_send`` can be cancelled by task id.

    :param args: Parsed JSON arguments from the LLM.
    :param session_async_tasks: Per-session async task map
        from ``create_runner_app``.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON-encoded result string.
    """
    async_result = _cancel_async_tool_result(
        args,
        session_async_tasks=session_async_tasks,
    )
    if not async_result.try_subagent_cancel:
        return async_result.output
    return await _cancel_subagent_task(
        args,
        conversation_id=conversation_id,
        server_client=server_client,
    )


async def _post_session_stop(server_client: httpx.AsyncClient, task_id: str) -> str | None:
    """Hard-stop one claude-native child through the server."""
    try:
        resp = await server_client.post(
            f"/v1/sessions/{task_id}/events",
            json={"type": "stop_session", "data": {}},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return f"Error: sys_cancel_task stop_session failed: {type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return (
            f"Error: sys_cancel_task stop_session returned {resp.status_code}: {resp.text[:200]}"
        )
    return None


async def _cancel_evicted_claude_native_subagent(
    task_id: str,
    *,
    conversation_id: str,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Stop an owned claude-native child after its work entry was evicted."""
    if server_client is None:
        return "Error: sys_cancel_task requires server access for sub-agent tasks"
    try:
        resp = await server_client.get(f"/v1/sessions/{task_id}", timeout=10.0)
    except httpx.HTTPError as exc:
        return f"Error: sys_cancel_task lookup failed: {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return f"Error: no in-flight task with task_id {task_id}"
    snapshot = resp.json()
    labels = snapshot.get("labels") if isinstance(snapshot, dict) else None
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("parent_session_id") != conversation_id
        or not isinstance(labels, dict)
        or labels.get(_SESSION_WRAPPER_LABEL_KEY) != CLAUDE_NATIVE_WRAPPER_VALUE
    ):
        return f"Error: no in-flight task with task_id {task_id}"
    stop_error = await _post_session_stop(server_client, task_id)
    if stop_error is not None:
        return stop_error
    return json.dumps({"cancelled": True, "task_id": task_id, "status": "cancelled"})


async def _cancel_subagent_task(
    args: _JsonObject,
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """
    Cancel a running sub-agent worker, routing by the child's harness.

    Only ``claude-native`` has a runner-side hard-stop, so the cancel
    event is chosen per harness — the child runner's ``stop_session``
    handler 204 no-ops for every other harness, so posting it there
    would silently do nothing:

    * ``claude-native`` — POST ``stop_session``. The child runner
      hard-kills the worker's tmux pane via ``_handle_claude_native_stop``
      and marks the work entry cancelled, delivering a terminal payload to
      the parent inbox and auto-waking it. A bare interrupt (Escape) only
      cancelled the current turn and left the worker process alive; a stop
      frees it.
    * everything else (in-process harnesses, ``codex-native``) — POST
      ``interrupt``, the path those harnesses actually honor. For an
      in-process child the runner marks the turn cancelled (via
      ``_interrupted_sessions`` → ``_on_proxy_stream_end``) and wakes the
      parent. ``codex-native`` has no runner-side stop yet, so its cancel
      stays best-effort (see message).

    :param args: Tool arguments containing ``task_id`` or
        ``handle_id``, e.g. ``{"task_id": "conv_child456"}``.
    :param conversation_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: JSON cancellation result.
    """
    from omnigent.runner import app as _runner_app

    task_id = args.get("task_id") or args.get("handle_id")
    if not task_id:
        return 'Error: sys_cancel_task requires "task_id"'
    if conversation_id is None:
        return "Error: sys_cancel_task requires conversation_id"
    entry = _runner_app.get_subagent_work(str(task_id))
    if entry is None:
        return await _cancel_evicted_claude_native_subagent(
            str(task_id),
            conversation_id=conversation_id,
            server_client=server_client,
        )
    if entry.parent_session_id != conversation_id:
        return f"Error: no in-flight task with task_id {task_id}"
    # A dispatched child sits in ``launching`` until its runtime emits a real
    # busy edge (see ``mark_subagent_work_started``). Cancellation must still
    # route to the child during that window — otherwise cancelling a slow-to-
    # start sub-agent would silently no-op and leave it running. A failed
    # claude-native entry still falls through because its pane may be alive.
    is_claude_native = entry.wrapper_label == CLAUDE_NATIVE_WRAPPER_VALUE
    can_stop_failed_claude = is_claude_native and entry.status == "failed"
    if entry.status not in ("launching", "running", "waiting") and not can_stop_failed_claude:
        return json.dumps(
            {
                "cancelled": entry.status == "cancelled",
                "task_id": task_id,
                "status": entry.status,
            }
        )
    if server_client is None:
        return "Error: sys_cancel_task requires server access for sub-agent tasks"

    # claude-native is the only harness with a runner-side hard-stop; every
    # other harness 204 no-ops on stop_session, so route them to interrupt.
    event_type = "stop_session" if is_claude_native else "interrupt"

    try:
        resp = await server_client.post(
            f"/v1/sessions/{task_id}/events",
            # Bare control events 422 on servers that require body.data.
            json={"type": event_type, "data": {}},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return f"Error: sys_cancel_task {event_type} failed: {type(exc).__name__}: {exc}"
    if resp.status_code >= 400:
        return (
            f"Error: sys_cancel_task {event_type} returned {resp.status_code}: {resp.text[:200]}"
        )

    updated = _runner_app.get_subagent_work(str(task_id)) or entry
    if updated.status == "cancelled":
        return json.dumps({"cancelled": True, "task_id": task_id, "status": "cancelled"})
    if updated.wrapper_label == CODEX_NATIVE_WRAPPER_VALUE:
        return json.dumps(
            {
                "cancel_requested": True,
                "cancel_confirmed": False,
                "best_effort": True,
                "task_id": task_id,
                "status": updated.status,
                "message": (
                    "Interrupt forwarded, but a runner-side hard-stop is not wired "
                    "for codex-native workers yet; the child may keep running and no "
                    "terminal inbox status is guaranteed."
                ),
            }
        )
    return json.dumps(
        {
            "cancel_requested": True,
            "cancel_confirmed": False,
            "task_id": task_id,
            "status": updated.status,
            "message": (
                "Cancel requested; cancellation has not been confirmed yet. "
                "Use sys_read_inbox to observe terminal status."
            ),
        }
    )


def _inject_orchestrator_skills(
    skills: list[SkillSpec],
    agent_spec: AgentSpec | None,
) -> list[SkillSpec]:
    """
    Auto-inject built-in platform skills for every omnigent agent.

    The ``build-omnigent`` skill teaches the LLM how to author valid
    agent configs. Every agent on the platform should have access to it
    — whether it declares ``tools.agents`` or not — so that any
    ``omnigent claude`` user can author and launch new agents. The
    skill is injected from the canonical source at
    ``omnigent/onboarding/agent/skills/build-omnigent/`` when not
    already present in the bundled set.

    :param skills: The agent's current skill list (bundled +
        potentially others); mutated in-place and returned.
    :param agent_spec: The session's AgentSpec (unused after the gate
        removal; retained for call-site compatibility).
    :returns: The (possibly augmented) skill list.
    """
    del agent_spec  # no longer gated; inject unconditionally
    existing_names = {getattr(s, "name", None) for s in skills}
    if "build-omnigent" in existing_names:
        return skills
    from omnigent.spec.parser import _discover_skills

    onboarding_skills_dir = (
        Path(__file__).resolve().parent.parent / "onboarding" / "agent" / "skills"
    )
    if not onboarding_skills_dir.is_dir():
        return skills
    for spec in _discover_skills(onboarding_skills_dir, skipped=[]):
        if spec.name == "build-omnigent":
            skills.append(spec)
            break
    return skills


def _execute_skill_tool(
    tool_name: str,
    args: _JsonObject,
    *,
    agent_spec: AgentSpec | None,
    runner_workspace: Path | None,
) -> str:
    """
    Runner-local handler for ``load_skill`` and ``read_skill_file``.

    Both tools are built from one registry — the agent spec's bundled
    skills merged with host-scope discovery from the runner workspace —
    so anything ``load_skill`` can load, ``read_skill_file`` can read.

    :param tool_name: ``"load_skill"`` or ``"read_skill_file"``.
    :param args: Parsed JSON arguments from the LLM.
    :param agent_spec: The session's AgentSpec.
    :param runner_workspace: The runner's workspace path for
        host-scope skill discovery.
    :returns: Tool output string.
    """
    from omnigent.tools.builtins.load_skill import LoadSkillTool
    from omnigent.tools.builtins.read_skill_file import ReadSkillFileTool

    bundled_skills = list(getattr(agent_spec, "skills", None) or [])
    skills_filter = getattr(agent_spec, "skills_filter", "all")
    # Auto-inject the build-omnigent skill for agents that opt into the
    # orchestration surface (tools.agents). This teaches the LLM how to
    # author valid agent configs via sys_os_write without requiring the
    # agent's own bundle to ship a skills/ directory.
    bundled_skills = _inject_orchestrator_skills(bundled_skills, agent_spec)

    # Both tools must resolve the same registry: a skill load_skill can load
    # from host scope must have its files readable too.
    load_tool = LoadSkillTool(
        bundled_skills,
        agent_root=runner_workspace,
        skills_filter=skills_filter,
    )
    tool: Tool = load_tool if tool_name == "load_skill" else ReadSkillFileTool(load_tool.skills)

    arguments_json = json.dumps(args)
    from omnigent.tools.base import ToolContext

    ctx = ToolContext(task_id="", conversation_id="", agent_id="")
    return tool.invoke(arguments_json, ctx)
