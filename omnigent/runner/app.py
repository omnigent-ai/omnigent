"""Runner FastAPI app — spawns harness subprocesses and dispatches to them.

Per ``designs/RUNNER.md`` §1, the runner owns harness subprocesses.
It resolves the harness type + spawn-env from the agent spec (either
via a spec_resolver callback for in-process use, or via
GET /v1/agents/{id}/contents for out-of-process use).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import itertools
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.harnesses.claude_native.bridge import ClaudeNativeToolRelay
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig
    from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.terminals.registry import TerminalRegistry

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.debug_logging import (
    debug_event,
    phase_scope,
    runner_primary_session_id,
    set_current_session_id,
)
from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import (
    SESSION_AGENT_MISSING_MESSAGE,
    ErrorCategory,
    ErrorCode,
    ErrorImpact,
    ErrorPhase,
    OmnigentError,
)
from omnigent.harness_aliases import (
    canonicalize_harness,
    is_native_harness,
)
from omnigent.harness_availability import CODEX_CANONICAL_HARNESSES
from omnigent.harness_capabilities import InstructionDelivery
from omnigent.harness_plugins import (
    harness_capabilities,
    load_object,
    model_env_keys,
    spawn_env_builders,
)
from omnigent.llms.errors import detect_request_size_overflow
from omnigent.native.native_coding_agents import (
    native_coding_agent_for_harness,
)
from omnigent.runner import native as _native
from omnigent.runner import pending_approvals
from omnigent.runner import subagent_work as _subagent_work
from omnigent.runner.acp_subagent_sessions import (
    _chain_acp_subagent_post,
    _complete_acp_subagent_child,
    _mint_acp_subagent_child,
    _post_acp_subagent_tool_call,
)
from omnigent.runner.app_support import (
    SpecResolver,
    _client_safe_error_detail,
    _CommentRelayBinding,
    _require_full_native_lock_coverage,
    _resolve_forwarded_message_content,
    _SpecEntry,
    _unwrap_spec_entry,
)
from omnigent.runner.background_titles import (
    BackgroundTitleContext,
    BackgroundTitleHarnessError,
    generator_spec_for_harness,
)
from omnigent.runner.background_titles import (
    generate_background_title as run_background_title,
)
from omnigent.runner.background_titles.service import BACKGROUND_TITLE_MAX_PROMPT_CHARS
from omnigent.runner.codex.goal import CodexGoalRunner
from omnigent.runner.launch_failure import FailureDiagnosis, classify_terminal_failure
from omnigent.runner.mcp_execution_registry import (
    McpExecutionRegistry,
)
from omnigent.runner.mcp_routes import register_mcp_routes
from omnigent.runner.model_option_routes import register_model_option_routes
from omnigent.runner.native import (
    _REPL_TERMINAL_NAME,
    _REPL_TERMINAL_SESSION_KEY,
    _SESSION_METADATA_PARAMS,
    NativeLaunchContext,
    PreLaunchResult,
    ResolvedSpec,
    _antigravity_native_terminal_arrives_via_transfer,
    _auto_create_repl_terminal,
    _cancel_auto_forwarder_task,
    _claude_native_bridge_id_for_session,
    _claude_native_bridge_id_with_optional_labels,
    _claude_native_session_wants_rebuild,
    _claude_native_terminal_arrives_via_transfer,
    _codex_native_terminal_arrives_via_transfer,
    _codex_session_needs_runner_terminal,
    _delete_native_bridge_dirs,
    _ensure_orchestrator_skills_in_bundle,
    _forward_harness_response,
    _is_spec_local_native_python_tool,
    _launch_native_terminal,
    _publish_terminal_pending,
    _required_runner_env,
    _resolve_native_spawn_env,
    _resolved_spec_workdir,
    _resolved_workdir_for_spec,
    _rewrap_like,
    _session_labels_for_runner_spawn,
    _session_payload_for_host_spawn_check,
    _unwrap_resolved_spec,
)
from omnigent.runner.native import orchestration as _native_runtime
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.runner.native_controls import build_native_controls
from omnigent.runner.policy_proxy import _evaluate_policy_via_omnigent
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
    trim_terminal_output,
)
from omnigent.runner.resource_routes import register_resource_routes
from omnigent.runner.session_history import build_session_history
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    parse_runner_session_init_envelope,
)
from omnigent.runner.sign_in_watch import build_sign_in_watch
from omnigent.runner.subagent_recovery import build_subagent_recovery
from omnigent.runner.subagent_routing import (
    PLAIN_SESSION,
    SessionRoutingClass,
    forget_session_routing_class,
    remember_session_routing_class,
    routing_class_from_snapshot,
    session_routing_class,
)
from omnigent.runner.subagent_work import (
    _CODEX_NATIVE_HARNESS,
    _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX,
    _SUBAGENT_TERMINAL_STATUSES,
    _WAKE_POST_MAX_ATTEMPTS,
    _child_session_parents,
    _ChildParentMeta,
    _deliver_subagent_completion,
    _deliver_subagent_wake_post,
    _format_subagent_wake_notice,
    _session_inboxes_ref,
    _session_status_to_task_status,
    _subagent_delivery_not_confirmed_response,
    _subagent_recovery_done,
    _subagent_recovery_locks,
    _subagent_work_by_child,
    _subagent_work_by_parent,
    _SubagentDeliveryAck,
    _SubagentWorkEntry,
    _truncate_child_preview,
    get_subagent_work,
    is_codex_native_subagent_wrapper,
    list_subagent_work,
    mark_subagent_work_started,
    mark_subagent_work_terminal,
    unregister_child_session,
    unregister_subagent_work_for_session,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError
from omnigent.runtime.prompt import (
    build_instructions,
    build_instructions_nullable,
    raw_author_instructions,
)
from omnigent.server.schemas import (
    BackgroundSessionTitleRequest,
    BackgroundSessionTitleResponse,
)
from omnigent.spec.skill_sources import resolve_session_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

# Allow process termination and forwarder cleanup to finish before DELETE proceeds.
_SESSION_INIT_CANCEL_TIMEOUT_S = 20.0


# Pending questions outlive prompt delivery; poll outside the turn watchdog.
_CLAUDE_PENDING_PROMPT_POLL_S = 0.5


def _warn_unresolved_sub_agent(session_id: str | None, sub_agent_name: str) -> None:
    """
    Log that a sub-agent name did not resolve to a declared child spec.

    Every spec-swap site is guarded by ``if sub_spec is not None`` with no
    ``else`` and falls back to the already-resolved PARENT spec — so a
    renamed/removed sub-agent or stale session metadata silently boots the
    child as a parent clone (parent prompt, tools, harness, workdir). The
    create route now rejects an undeclared name up front, but stale rows
    and post-create bundle edits can still reach these sites; a loud log
    makes the fallback diagnosable instead of invisible.

    :param session_id: The session whose turn is resolving the spec.
    :param sub_agent_name: The name that failed to resolve in the parent
        spec tree.
    """
    _logger.warning(
        "Sub-agent %r for session %s did not resolve in the parent spec; "
        "falling back to the parent spec (child runs with the parent's "
        "prompt, tools and harness). Likely a renamed/removed sub-agent or "
        "stale session metadata.",
        sub_agent_name,
        session_id,
    )


def __getattr__(name: str) -> object:
    """Preserve private native-helper imports during the package move."""
    return cast(object, getattr(_native, name))


class _NativeBuilderCall(Protocol):
    async def __call__(self, *args: object, **kwargs: object) -> object: ...


def _native_builder(name: str) -> _NativeBuilderCall:
    async def _call(*args: object, **kwargs: object) -> object:
        overrides: list[tuple[str, object]] = []
        for dependency in _native.__all__:
            if not dependency.startswith("_auto_create_") and dependency in globals():
                app_value = globals()[dependency]
                runtime_value = getattr(_native_runtime, dependency)
                if app_value is not runtime_value:
                    overrides.append((dependency, runtime_value))
                    setattr(_native_runtime, dependency, app_value)
        try:
            builder = cast(_NativeBuilderCall, getattr(_native_runtime, name))
            return await builder(*args, **kwargs)
        finally:
            for dependency, runtime_value in reversed(overrides):
                setattr(_native_runtime, dependency, runtime_value)

    return _call


for _builder_name in (
    "_auto_create_antigravity_terminal",
    "_auto_create_claude_terminal",
    "_auto_create_codex_terminal",
    "_auto_create_cursor_terminal",
    "_auto_create_devin_terminal",
    "_auto_create_goose_terminal",
    "_auto_create_hermes_terminal",
    "_auto_create_kimi_terminal",
    "_auto_create_kiro_terminal",
    "_auto_create_opencode_terminal",
    "_auto_create_pi_terminal",
    "_auto_create_qwen_terminal",
    "_auto_create_repl_terminal",
):
    globals()[_builder_name] = _native_builder(_builder_name)


# Servers before 0.3.0 cannot serialize the runner's "waiting" status.
# Unknown versions also downgrade to "running" so old servers never return 500.
_WAITING_STATUS_MIN_SERVER_VERSION = "0.3.0"
# Published statuses that mean a session's terminal is still working a turn.
# ``waiting`` is parked on user input, so it keeps the runner alive too.
_IN_FLIGHT_SESSION_STATUSES = ("running", "waiting")
# Cached server version from the /api/version probe; ``None`` until a probe
# succeeds. A failed probe stays ``None`` and is retried on the next
# session-create — the GET is cheap and self-heals a transient failure.
_server_version: str | None = None


def _acknowledge_settings_rollback(response: Response) -> JSONResponse:
    """Mark a refused Codex settings response as not kept by this runner."""
    try:
        content = json.loads(bytes(response.body))
    except ValueError:
        content = None
    if not isinstance(content, dict):
        content = {}
    return JSONResponse(
        status_code=response.status_code, content={**content, "rollback_on_refusal": True}
    )


def _invalid_effort_response(effort: object) -> JSONResponse | None:
    """Return the 400 for a non-string, non-null session-event effort, else ``None``."""
    if effort is None or isinstance(effort, str):
        return None
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_input", "detail": "Body 'effort' must be a string or null"},
    )


def _version_supports_waiting_status(server_version: str) -> bool:
    """
    Whether *server_version* can serialize ``session.status: "waiting"``.

    :param server_version: The server's reported version, e.g. ``"0.2.0"`` or
        ``"0.3.0.dev0"``.
    :returns: ``True`` iff the server's PEP 440 release tuple is ``>= 0.3.0``
        (the release that added "waiting" to the session-status model).
    """
    from packaging.version import InvalidVersion, Version

    try:
        return (
            Version(server_version).release >= Version(_WAITING_STATUS_MIN_SERVER_VERSION).release
        )
    except InvalidVersion:
        _logger.warning(
            "server version %r is not PEP 440; treating waiting status support as unknown",
            server_version,
            extra={"session_id": runner_primary_session_id()},
        )
        return False


async def _get_server_version(server_client: httpx.AsyncClient) -> str | None:
    """
    Resolve the server's version via a one-time ``GET /api/version`` probe.

    Memoized once it succeeds: later calls return the cached version. A failed
    probe returns ``None`` and is retried on the next call, so callers fail safe
    (treat an unknown version as not supporting newer behavior).

    :param server_client: The runner's httpx client pointed at the server.
    :returns: The server's reported version (e.g. ``"0.2.0"``), or ``None`` when
        the probe has not yet succeeded.
    """
    global _server_version
    if _server_version is not None:
        return _server_version
    try:
        resp = await server_client.get("/api/version")
        resp.raise_for_status()
        _server_version = resp.json()["version"]
        _logger.info(
            "resolved server version: %s",
            _server_version,
            extra={"session_id": runner_primary_session_id()},
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully; never 500 an old server
        _logger.warning(
            "could not probe server /api/version (%s); treating as unknown",
            exc,
            extra={"session_id": runner_primary_session_id()},
        )
    return _server_version


_NO_BODY_STATUS_CODES = {204, 304}
# Native agents whose forwarder stamps ``turn_completed`` on the idle edges of
# genuinely finished turns (claude: the ``Stop`` hook, which never fires on an
# interrupt). For these, a bare quiescence ``idle`` is NOT a completion. Add a
# key here only together with its forwarder's ``turn_completed`` plumbing,
# or that harness's sub-agent completions stop reaching parent inboxes.
_TURN_OUTCOME_CONFIRMING_NATIVE_AGENTS = frozenset({"claude"})


# Not in the retryable-harness-error allowlist — desync is terminal, not transient.
_RUNNER_TURN_CONTEXT_DESYNC_CODE = "runner_turn_context_desync"
# Delays before each stranded-wake re-attempt round after a tunnel reconnect.
# The reconnect callback fires BEFORE the new tunnel connection is established,
# and the server 503s an injected parent event while the parent's runner is
# still offline — so pace the rounds to outlast a slow handshake instead of
# spending the bounded per-POST retries against a not-yet-open tunnel. The
# final long round covers a server that is reachable but slow to become ready;
# rounds cost nothing once no parent is stranded (the loop exits early).
_STRANDED_WAKE_RETRY_DELAYS_S = (2.0, 5.0, 10.0, 30.0)

# Cadence for ``session.heartbeat`` keepalive events on the runner's
# ``GET /v1/sessions/{id}/stream`` endpoint. Between turns the event
# queue is idle — without periodic bytes, an intermediate proxy (e.g.
# the Databricks Apps ingress) can drop the long-lived HTTP connection.
# Matches the AP-side ``_SESSION_STREAM_HEARTBEAT_INTERVAL_S``.
_SESSION_STREAM_HEARTBEAT_S = 15.0

# How long a required-terminal exit waits for the session's in-flight turn
# stream to converge before releasing the harness subprocess. The harness
# usually reports the failure that killed its pane (e.g. a prompt-readiness
# timeout) on that very stream; releasing at once would close the client the
# runner is reading and turn the report into a bare transport error. A pane
# that died on its own leaves the harness parked on a readiness wait, so the
# wait is bounded and the stream failure is then attributed to the exit.
_TERMINAL_EXIT_RELEASE_GRACE_S = 2.0

# Banner printed by Claude Code on a voluntary /exit or /quit (exit 0).
# The pane activity from printing it can flip the idle memo back to "running"
# before the pane dies, making session_was_idle False on a user-initiated quit.
_CLAUDE_VOLUNTARY_EXIT_MARKER = "Resume this session with:"


# Marker the runner stamps on action_required SSE events it intends
# to dispatch locally. See designs/RUNNER_MCP.md §Explicit dispatch
# marker.
_RUNNER_DISPATCHED_FIELD = "omnigent_runner_dispatched"


def _encode_sse_event(event: Mapping[str, object]) -> bytes:
    """Re-encode an SSE event as a single ``data:`` frame."""
    import json as _json

    return f"data: {_json.dumps(event)}\n\n".encode()


def _response_body_preview(resp: object, *, limit: int = 500) -> str:
    """
    Return a short response-body preview for diagnostics.

    Some runner tests use lightweight response fakes that expose
    ``content`` and ``status_code`` but not HTTPX's convenience
    ``text`` property. Logging should not make those fakes diverge from
    production behavior.

    :param resp: Response-like object, e.g. ``httpx.Response``.
    :param limit: Maximum number of characters to include.
    :returns: Decoded response text preview.
    """
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text[:limit]
    content = getattr(resp, "content", b"")
    if isinstance(content, bytes):
        return content[:limit].decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content[:limit]
    return ""


@dataclasses.dataclass
@dataclasses.dataclass(frozen=True)
class _SessionSnapshot:
    """One ``GET /v1/sessions/{id}`` projected for all runner readers.

    The single source registration, workspace resolution, and spec
    resolution share instead of each fetching. See
    :func:`_session_snapshot` for the single-flight loader.

    :param ok: ``True`` only when the fetch returned HTTP 200.
    :param status_code: The fetch's HTTP status, or ``None`` on a
        transport error before any response, e.g. ``200`` / ``404``.
    :param created_at: Server creation time (UNIX seconds), or the
        runner's wall clock when the fetch failed / omitted it.
    :param workspace: Server-stored workspace path, or ``None``.
    :param agent_id: Bound agent id, or ``None`` when not yet bound /
        the fetch failed, e.g. ``"ag_abc123"``.
    :param sub_agent_name: For sub-agent sessions, the dispatched
        sub-agent's name, e.g. ``"claude_code"`` — used to swap the
        parent spec to the child's sub-spec so the child's harness
        (e.g. ``claude-native``) is resolved instead of the parent's.
        ``None`` for top-level sessions. Projected from the server
        snapshot so the identity survives a runner reconnect / spec-cache
        eviction (the in-memory ``_session_sub_agent_names`` map does not).
    :param parent_session_id: For sub-agent sessions, the parent
        conversation's id, e.g. ``"conv_parent987"``. ``None`` for
        top-level sessions. Lets ``_ensure_subagent_work_entry`` rebuild a lost
        work entry when the in-memory map was wiped (reconnect / restart) or
        never populated (a ``sys_session_create`` child).
    :param agent_name: Human-readable bound agent name, e.g.
        ``"cursor-native-ui"``. Used as the sub-agent label when rebuilding a
        work entry for a child the server did not record a ``sub_agent_name``
        for. ``None`` when unbound / the fetch failed.
    """

    ok: bool
    status_code: int | None
    created_at: float
    workspace: str | None
    agent_id: str | None
    sub_agent_name: str | None = None
    parent_session_id: str | None = None
    agent_name: str | None = None


@dataclasses.dataclass(frozen=True)
class _SessionInitContext:
    """Metadata source selected before shared session initialization runs."""

    envelope: RunnerSessionInitEnvelope | None

    @property
    def labels(self) -> Mapping[str, str] | None:
        """Return server-supplied labels, or ``None`` on the legacy path."""
        return self.envelope.snapshot.labels if self.envelope is not None else None

    @property
    def routing_class(self) -> SessionRoutingClass:
        """Return the session's Smart Routing class.

        The legacy path carries no snapshot, so it reads as plain — a
        session whose routing state cannot be established must not pay any
        routing-path cost.
        """
        if self.envelope is None:
            return PLAIN_SESSION
        snapshot = self.envelope.snapshot
        return routing_class_from_snapshot(
            cost_control_mode=snapshot.cost_control_mode_override,
            harness_override=snapshot.harness_override,
            labels=snapshot.labels,
        )


# Language constant the omnigent YAML translator stamps on callable-backed
# tools (omnigent/spec/omnigent.py:OMNIGENT_TOOL_LANGUAGE). Duplicated rather
# than imported to avoid pulling the heavy translator module in for one
# string — same rationale as omnigent/tools/local_callable.py.
_OMNIGENT_CALLABLE_LANGUAGE = "omnigent-python-callable"


def _looks_like_file_path(path: str) -> bool:
    """
    Return whether *path* is a filesystem path rather than a dotted import.

    File-based local tools are discovered as ``tools/python/foo.py`` /
    ``tools/typescript/foo.ts`` — always carrying a path separator and a
    source extension (see :func:`omnigent.spec.parser._discover_local_tools`).
    Callable-backed tools store a dotted import path (``pkg.mod.func``) in the
    same field — no separator, no source extension. This structural test is
    the primary guard so a rename of the callable-tool *language* string can
    never reintroduce the workdir-mangling bug.

    :param path: A :class:`LocalToolInfo` ``path`` value.
    :returns: ``True`` when *path* is a file path safe to resolve onto the
        workdir; ``False`` for dotted import paths.
    """
    return "/" in path or os.sep in path or path.endswith((".py", ".ts"))


def _spec_with_workdir_paths(
    spec: AgentSpec | None,
    workdir: Path | None,
) -> AgentSpec | None:
    if workdir is None or spec is None:
        return spec
    local_tools = getattr(spec, "local_tools", None)
    if not local_tools:
        return spec
    resolved_tools: list[LocalToolInfo] = []
    changed = False
    for info in local_tools:
        path = getattr(info, "path", None)
        # Only resolve genuine file paths onto the workdir. Callable-backed
        # tools store a dotted import path (``pkg.mod.func``) in the same
        # field; joining that to the workdir corrupts it, the import fails,
        # the tool never registers, and any tool_call policy narrowed to it
        # can never fire. The structural file-vs-dotted check is the primary
        # guard; the language check is belt-and-suspenders.
        if (
            path
            and getattr(info, "language", None) != _OMNIGENT_CALLABLE_LANGUAGE
            and _looks_like_file_path(path)
            and not Path(path).is_absolute()
        ):
            resolved_tools.append(dataclasses.replace(info, path=str((workdir / path).resolve())))
            changed = True
        else:
            resolved_tools.append(info)
    if not changed:
        return spec
    return dataclasses.replace(spec, local_tools=resolved_tools)


@dataclasses.dataclass
class TurnDispatch:
    """
    Runner-side dispatch context for a single turn.

    Carries metadata the runner needs for harness resolution,
    MCP schema injection, and system prompt — separated from
    the harness message body so no field-stripping is needed.

    :param agent_id: Agent identifier for spec resolution,
        e.g. ``"ag_abc123"``.
    :param harness: Harness type, e.g. ``"openai-agents"``.
    :param has_mcp_servers: Whether to inject MCP tool schemas.
    :param instructions: System prompt for the LLM.
    :param agent_version: Spec version for invalidation.
    :param spawn_env: Harness subprocess environment overrides.
    :param client_side_tool_names: Names of request-supplied
        client-side tools for this turn (e.g. ``{"Read", "Glob"}``).
        These are executed by the caller, not the runner, so the
        proxy_stream relays their ``action_required`` events upstream
        to tunnel rather than dispatching them locally.
    """

    agent_id: str | None = None
    harness: str | None = None
    has_mcp_servers: bool = False
    instructions: str | None = None
    agent_version: int | None = None
    spawn_env: dict[str, str] | None = None
    client_side_tool_names: frozenset[str] = frozenset()


@dataclasses.dataclass
class InstructionComposition:
    """Runner-local, never-serialized view of this turn's instruction state.

    Computed once inside ``_stream_message_to_harness`` (the point where the
    background and direct-stream dispatch paths converge) and consumed
    in-process by the single delivery-gap warn check and by delivery
    channels (opencode-native, hermes) that must not leak the fabricated
    ``"You are a helpful assistant."`` fallback. Never attached to
    ``TurnDispatch``, ``MessageEvent``, ``CreateResponseRequest``, or
    ``ExecutorConfig`` — the wire shape is unchanged from today.

    :param authored_present: Whether ``AgentSpec.instructions`` is
        non-empty/non-whitespace, resolved pre-composition.
    :param composed: The meaningful composed text (author + applicable
        framework instructions), or ``None`` if there is truly nothing.
    """

    authored_present: bool
    composed: str | None


# Harnesses whose executor reads the wire ``instructions`` field itself and
# needs the gated ``InstructionComposition.composed`` value there instead of
# the default fallback-including composed-per-turn string — opencode-native
# via its NativePrompt.system_prompt; hermes via HermesExecutor.run_turn's
# system_prompt param. See the harness-conditional swap in
# _stream_message_to_harness.
_GATED_COMPOSED_INSTRUCTION_HARNESSES = frozenset({"opencode-native", "hermes"})


def _wrap_as_message_event(body: _JsonObject) -> _JsonObject:
    """
    Adapt a ``CreateResponseRequest``-shaped body into a
    :class:`MessageEvent` body for the harness's discriminated
    ``POST /v1/sessions/{id}/events`` endpoint.

    The runtime still synthesizes ``CreateResponseRequest``-shaped
    bodies internally to drive harness turns; this helper renames
    ``input`` → ``content`` and stamps the discriminator
    (``type="message"``) and role (``role="user"``) fields without
    copying every other field by name — the harness's
    :class:`MessageEvent` accepts arbitrary extras and forwards them
    onto its synthesized :class:`CreateResponseRequest`, so
    passthrough is automatic.

    :param body: The runner's incoming JSON body, e.g.
        ``{"model": "agent", "input": [...], "tools": [...]}``.
    :returns: A new dict in :class:`MessageEvent` shape, e.g.
        ``{"type": "message", "role": "user", "model": "agent",
        "content": [...], "tools": [...]}``. Does not mutate the
        input dict.
    """
    event_body = dict(body)
    event_body["type"] = "message"
    event_body["role"] = "user"
    if "input" in event_body:
        event_body["content"] = event_body.pop("input")
    return event_body


class _ContextWindowOverflow(Exception):
    """
    Raised and caught inside ``proxy_stream`` when the harness reports a
    context-window overflow, so both live and background turns end the
    same way.

    :param max_tokens: The model's context window.
    :param actual_tokens: The prompt size that overflowed.
    :param detail_message: Original rejection text to surface verbatim in the
        error detail (e.g. a deployment byte-cap message carrying its byte
        sizes), kept instead of the token-count approximation when present.
    """

    def __init__(
        self,
        max_tokens: int,
        actual_tokens: int,
        *,
        detail_message: str | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        self.actual_tokens = actual_tokens
        self.detail_message = detail_message
        super().__init__(f"context window exceeded: {actual_tokens} > {max_tokens}")


_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "prompt is too long",
)


def _is_context_overflow_error(
    event: _JsonObject,
) -> tuple[int, int, str | None] | None:
    """
    Check if a ``response.failed`` SSE event indicates a context-window overflow.

    :param event: The parsed SSE event dict.
    :returns: ``(max_tokens, actual_tokens, detail_message)`` on overflow, else
        ``None``. ``detail_message`` carries a byte-cap rejection's raw text and
        is ``None`` for token-shaped overflows, which have no extra detail.
    """
    if event.get("type") != "response.failed":
        return None
    error = cast(_JsonObject, event.get("error", {}))
    raw = str(error.get("message", ""))
    msg = raw.lower()
    # Parse byte-cap rejections (request first, limit second) ahead of the
    # generic gate so the numeric fallback can't invert the pair; size-less
    # content-length phrases stay generic. Sizes are expressed as tokens.
    size_overflow = detect_request_size_overflow(msg)
    if size_overflow is not None:
        return (
            size_overflow.approx_limit_tokens,
            size_overflow.approx_request_tokens,
            raw,
        )
    if not any(pat in msg for pat in _CONTEXT_OVERFLOW_PATTERNS):
        return None
    actual_gt_max = re.search(r"(\d{4,})\D*>\D*(\d{4,})", msg)
    if actual_gt_max is not None:
        return int(actual_gt_max.group(2)), int(actual_gt_max.group(1)), None

    numbers = re.findall(r"(\d{4,})", msg)
    if len(numbers) >= 2:
        return int(numbers[-2]), int(numbers[-1]), None
    if len(numbers) == 1:
        return int(numbers[0]), int(numbers[0]) + 1, None
    return 128000, 128001, None


def _response_failed_payload(
    error: Mapping[str, object],
    source: str = "execution",
) -> _JsonObject:
    """Build a failure envelope with required error fields and a legacy mirror."""
    failure_error = {**_normalize_turn_error(error), **error}
    return {
        "type": "response.failed",
        "source": source,
        "response": {"status": "failed", "error": failure_error},
        "error": failure_error,
    }


def _response_failed_event(
    error: Mapping[str, object],
    source: str = "execution",
) -> bytes:
    """
    Encode one ``response.failed`` SSE frame.

    Keep a top-level ``error`` mirror for older tests/debuggers that
    inspected the legacy runner proxy shape directly.

    :param error: Error payload to place under ``response.error``,
        e.g. ``{"code": "connection_error", "message": "dropped"}``.
    :param source: Where the fault originated -- ``"llm"`` for
        inference/context errors, ``"harness"`` for Claude Code/harness
        process failures, ``"execution"`` for runner configuration
        or infrastructure failures.  Forwarded as-is to the AP server
        so it can persist the right ``ErrorData.source``.
    :returns: UTF-8 encoded SSE frame bytes.
    """
    payload = json.dumps(_response_failed_payload(error, source=source))
    return f"event: response.failed\ndata: {payload}\n\n".encode()


def _inject_mcp_schemas(
    event_body: _JsonObject,
    mcp_schemas: list[_JsonObject],
) -> None:
    """Append *mcp_schemas* to ``event_body["tools"]`` in place.

    Preserves any existing tools (builtins / client-side from the AP
    server) and adds MCP schemas after them. No-op when *mcp_schemas*
    is empty. See ``designs/RUNNER_MCP.md`` §Schema injection.

    Skips schemas already present by name: the per-session tool cache
    also folds in MCP schemas, and codex rejects duplicate tool names.
    """
    if not mcp_schemas:
        return
    existing = cast(list[_JsonObject], event_body.get("tools") or [])
    existing_names = {t.get("name") for t in existing if t.get("name")}
    new_schemas = [s for s in mcp_schemas if s.get("name") not in existing_names]
    event_body["tools"] = list(existing) + new_schemas


def _schema_tool_name(schema: _JsonObject) -> str | None:
    """
    Extract a tool's function name from its OpenAI-format schema.

    :param schema: A tool schema dict in nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "Read", ...}}``.
    :returns: The tool name (e.g. ``"Read"``), or ``None`` when the
        schema is malformed / missing the ``function.name`` field.
    """
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return name if isinstance(name, str) else None
    return None


def _merge_request_client_tools(
    spec_tools: list[_JsonObject],
    client_tools: list[_JsonObject],
) -> list[_JsonObject]:
    """
    Append request-supplied client-side tools to the spec tool schemas.

    The runner-native session path assembles the harness tool list from
    the agent spec's builtin + MCP schemas only. Client-side tools the
    caller registers on the event (``request.tools`` — e.g. a REPL's
    ``Read`` / ``Write`` / ``Glob``) must also reach non-native harnesses
    so the model can emit them. The resulting call is not in
    ``_ALL_LOCAL_TOOLS``, so ``dispatch_tool_locally`` relays the
    ``action_required`` event upstream and it tunnels back to the caller.
    Without this merge the schemas never reach the executor and the model
    cannot invoke client tools at all.

    Builtins win on a name clash: a request tool must not shadow a
    policy-enforced server-side builtin of the same name.

    :param spec_tools: Spec-derived builtin + MCP tool schemas, each in
        nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "load_skill", ...}}``.
    :param client_tools: Request-supplied client-side tool schemas in the
        same nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "Read", ...}}``.
    :returns: ``spec_tools`` followed by the named client tools whose names
        don't collide with a spec tool. Non-dict and nameless client
        entries are dropped. A fresh list; inputs are not mutated. Empty
        when both inputs are empty.
    """
    seen: set[str] = {
        name
        for t in spec_tools
        if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
    }
    merged: list[_JsonObject] = list(spec_tools)
    for tool in client_tools:
        if not isinstance(tool, dict):
            continue
        name = _schema_tool_name(tool)
        # Drop nameless/malformed entries: the executor rejects an unnamed
        # FunctionTool, so forwarding one would only risk a hard error.
        if name is None or name in seen:
            continue
        seen.add(name)
        merged.append(tool)
    return merged


def _should_dispatch_tool_locally(
    tool_name: str,
    *,
    dispatch: TurnDispatch | None,
    is_mcp: bool,
    is_runner_builtin: bool,
    is_spec_local: bool,
) -> bool:
    """
    Decide whether the runner dispatches *tool_name* locally vs. relays it.

    Client-side (request-supplied) tools execute on the caller, so their
    ``action_required`` events must relay upstream to tunnel — dispatching
    them locally would error ``"<tool> not in local dispatch table"``. Every
    other tool keeps the prior behavior, including the ``dispatch is not
    None`` catch-all that covers spec-local / UC / spec-callable tools in
    session-native mode.

    :param tool_name: The tool the LLM called, e.g. ``"Read"`` or
        ``"sys_session_send"``.
    :param dispatch: The turn's :class:`TurnDispatch` (carries
        ``client_side_tool_names``), or ``None`` on the legacy path.
    :param is_mcp: ``True`` when *tool_name* is an MCP-server tool for
        this turn.
    :param is_runner_builtin: ``True`` when *tool_name* is a
        runner-dispatched builtin (``should_dispatch_locally(tool_name)``).
    :param is_spec_local: ``True`` when *tool_name* is a spec-declared
        local python/callable tool.
    :returns: ``True`` to dispatch locally on the runner; ``False`` to
        relay the ``action_required`` event upstream (client-side tunnel).
    """
    if dispatch is not None and tool_name in dispatch.client_side_tool_names:
        return False
    return dispatch is not None or is_mcp or is_runner_builtin or is_spec_local


def _side_chat_text_from_content(content: object) -> str:
    """
    Join user text from message content blocks for a Codex ``/side`` follow-up.

    :param content: A message body's ``content`` list, e.g.
        ``[{"type": "input_text", "text": "and why?"}]``.
    :returns: The concatenated text, or ``""`` when there is none.
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in {"text", "input_text"}:
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


def _normalize_turn_error(error: Mapping[str, object]) -> dict[str, str]:
    """
    Coerce a turn-failure ``error`` dict into a ``{code, message}`` shape.

    The ``error`` dicts passed to :func:`_on_proxy_stream_end` vary by
    call site: most carry ``{"message": "..."}`` (and sometimes
    ``"type"``), but a few carry only ``{"status": <http status>}``.
    The wire ``SessionStatusEvent.error`` field (``ErrorDetail``)
    requires both ``code`` and ``message``, so this normalizes every
    shape into one the schema accepts, never raising on a missing key.
    The result is what gets published on the ``failed`` status event
    and ultimately rendered as the REPL's terminal error line.

    A harness ``response.failed`` error already names its failure in ``code``;
    that code is kept so the failed status edge and the persisted error item
    describe one failure the same way (the web UI de-duplicates them by code
    and message). ``type`` is the legacy spelling; ``runner_error`` is the
    fallback for setup failures that carry neither.

    :param error: Raw error dict from a ``_on_proxy_stream_end`` call,
        e.g. ``{"message": "turn setup failed: ..."}``,
        ``{"code": "agent_startup_pending", "message": "..."}`` or
        ``{"status": 502}``.
    :returns: A dict with ``code`` and ``message`` string keys, e.g.
        ``{"code": "runner_error", "message": "turn setup failed: ..."}``.
        Falls back to a generic message when none is present.
    """
    raw_message = error.get("message")
    if isinstance(raw_message, str) and raw_message.strip():
        message = raw_message
    elif "status" in error:
        message = f"turn failed (status {error['status']})"
    else:
        message = "turn failed"
    code = "runner_error"
    for key in ("code", "type"):
        raw_code = error.get(key)
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
            break
    return {"code": code, "message": message}


def _harness_error_response_error(response: object) -> dict[str, str]:
    """
    Convert a non-streaming harness error response into a turn failure.

    For example, ``{"error": "harness_spawn_failed", "detail": "See runner log"}``
    becomes ``{"message": "harness_spawn_failed: See runner log"}``. Missing or
    malformed bodies fall back to a short raw-body preview or a generic message.

    :param response: Response returned instead of a ``StreamingResponse``.
    :returns: An error dict suitable for :func:`_on_proxy_stream_end`.
    """
    text = ""
    # A stub response without a body, a body that is not bytes, or bytes
    # that are not UTF-8 all fall back to the generic message below.
    with contextlib.suppress(UnicodeDecodeError, AttributeError, TypeError):
        text = bytes(cast(Any, response).body).decode("utf-8")
    payload: object = None
    with contextlib.suppress(ValueError):
        payload = json.loads(text)
    if isinstance(payload, dict):
        raw_detail = payload.get("detail")
        raw_code = payload.get("error")
        detail = raw_detail.strip() if isinstance(raw_detail, str) else ""
        code = raw_code.strip() if isinstance(raw_code, str) else ""
        if code == ErrorCode.SESSION_AGENT_MISSING:
            # A lifecycle condition the web UI explains; keep its code and text.
            return {"code": code, "message": detail or SESSION_AGENT_MISSING_MESSAGE}
        if code and detail:
            return {"message": f"{code}: {detail}"}
        if detail:
            return {"message": detail}
        if code:
            return {"message": code}
    return {"message": text.strip()[:200] or "harness returned error response"}


# Per-session timer registry. Keyed by session_id → {timer_id → Task}.
_session_timers: dict[str, dict[str, asyncio.Task[None]]] = {}


def _has_live_async_tasks(
    session_async_tasks: Mapping[
        str,
        Mapping[str, tuple[asyncio.Task[object], asyncio.Event]],
    ],
) -> bool:
    """Return whether an async-tool registry contains unfinished work."""
    return any(
        not task.done()
        for handles in session_async_tasks.values()
        for task, _cancel_event in handles.values()
    )


def register_timer(
    session_id: str,
    timer_id: str,
    task: asyncio.Task[None],
) -> None:
    """
    Register an active timer task for a session.

    :param session_id: Session the timer belongs to.
    :param timer_id: Timer identifier, e.g. ``"timer_a1b2..."``.
    :param task: The asyncio.Task running the timer loop.
    """
    _session_timers.setdefault(session_id, {})[timer_id] = task


def unregister_timer(session_id: str, timer_id: str) -> None:
    """
    Remove a timer from the registry on completion or cancel.

    :param session_id: Session the timer belongs to.
    :param timer_id: Timer to remove.
    """
    timers = _session_timers.get(session_id)
    if timers is not None:
        timers.pop(timer_id, None)


def cancel_timer(session_id: str, timer_id: str) -> bool:
    """
    Cancel a timer by ID.

    :param session_id: Session the timer belongs to.
    :param timer_id: Timer to cancel.
    :returns: True if found and cancelled, False otherwise.
    """
    timers = _session_timers.get(session_id)
    if timers is None:
        return False
    task = timers.pop(timer_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


# Module-level ref to _session_agent_ids. Populated inside
# create_runner_app; read by tool_dispatch._execute_subagent_tool.
_session_agent_ids_ref: dict[str, str] = {}

# Module-level ref to _session_histories. Populated inside
# create_runner_app; used by tests to inspect in-memory history.
_session_histories_ref: dict[str, list[_JsonObject]] = {}

# Module-level ref to _session_event_queues. Populated inside
# create_runner_app; used by tests to inspect the queue an SSE
# subscriber would have read (events published synchronously by
# ``_publish_event`` are visible by the time the producer's await
# call returns, so tests don't need to subscribe to the HTTP
# ``/stream`` endpoint just to assert on emitted events).
_session_event_queues_ref: dict[str, asyncio.Queue[_JsonObject | None]] = {}


def get_session_agent_id(session_id: str) -> str | None:
    """
    Return the durable agent_id for a session.

    :param session_id: Session/conversation ID, e.g.
        ``"conv_abc123"``.
    :returns: The agent_id, or ``None`` if not found.
    """
    return _session_agent_ids_ref.get(session_id)


# Repeated invocations share a filesystem scan; installed skills become
# resolvable after at most one minute without restarting the runner.
_SESSION_SKILLS_CACHE_TTL_SECONDS = 60.0
_SESSION_INIT_ENVELOPE_TTL_SECONDS = 60.0


def create_runner_app(
    *,
    process_manager: HarnessProcessManager | None = None,
    spec_resolver: SpecResolver | None = None,
    server_client: httpx.AsyncClient,
    terminal_registry: TerminalRegistry | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    runner_workspace: Path | None = None,
    per_session_workspace: bool = True,
    mcp_manager: RunnerMcpManager | None = None,
    auth_token: str | None = None,
    auth_token_factory: Callable[[], str | None] | None = None,
) -> FastAPI:
    """Build a fresh runner FastAPI app.

    :param process_manager: Pre-started HarnessProcessManager.
        ``None`` → scaffold mode (501 stubs).
    :param spec_resolver: Async callback ``(agent_id) -> AgentSpec | None``.
        For in-process: wraps the server's agent cache.
        For out-of-process: wraps HTTP fetch to GET /v1/agents/{id}/contents.
        ``None`` → runner falls back to body-supplied hints (test path).
    :param server_client: httpx.AsyncClient pointed at the AP
        server's public API. Used by the runner for
        elicitation/approval forwarding.
        In-process: pointed at the Omnigent ASGI app.
        Out-of-process: pointed at the server's HTTP URL.
    :param terminal_registry: TerminalRegistry instance for
        runner-local terminal tool dispatch (Phase 2).
        ``None`` → terminal tools relay upstream.
    :param runner_workspace: Optional local workspace path passed
        by the CLI when the runner owns filesystem tools for a
        remote app server session.
    :param per_session_workspace: ``True`` (default) isolates each
        session under a subdirectory of *runner_workspace*.
        Single-user CLI runners pass ``False`` so the agent sees the
        project root. No effect when *runner_workspace* is ``None``.
    :param mcp_manager: Optional :class:`RunnerMcpManager` owning
        this runner's MCP pool. ``None`` skips MCP injection
        (test path).
    :param auth_token: Optional bearer token that callers must
        present in the ``Authorization`` header.  When set, every
        request except ``GET /health`` is rejected with 401 if
        the token is missing or wrong.  ``None``
        disables auth (in-process / test path).
    :param auth_token_factory: Refresh-capable server bearer factory owned by
        the runner process. Native terminal helpers reuse it instead of
        resolving host credentials again for every terminal launch.
    """
    import hmac

    app = FastAPI(title="omnigent-runner")

    from omnigent.runner.logging_context import RunnerLogContextMiddleware

    app.add_middleware(RunnerLogContextMiddleware)
    mcp_execution_registry = McpExecutionRegistry()
    app.state.mcp_execution_registry = mcp_execution_registry

    # Set as soon as SIGINT/SIGTERM is handled, so a required terminal that
    # dies with the runner's process group is not reported as a crash.
    _shutting_down = asyncio.Event()
    app.state.shutting_down = _shutting_down

    from omnigent.runtime import telemetry

    telemetry.instrument_fastapi_app(app)

    if auth_token is not None:
        _expected_token = auth_token

        @app.middleware("http")
        async def _runner_auth_middleware(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            if request.url.path == "/health":
                return await call_next(request)
            client = request.scope.get("client")
            if client is not None and client[0] == "tunnel":
                return await call_next(request)
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                provided = auth_header[7:]
            else:
                provided = ""
            if not provided or not hmac.compare_digest(provided, _expected_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing runner auth token"},
                )
            return await call_next(request)

    if terminal_registry is not None:
        from omnigent.runtime import _globals as _rt_globals

        _rt_globals._terminal_registry = terminal_registry

    _version_cache: dict[str, int] = {}  # conversation_id → last seen agent_version
    _spec_cache: dict[str, _SpecEntry] = {}  # agent_id → cached AgentSpec for terminal tools
    _resp_to_conv: dict[str, str] = {}  # harness response_id → conversation_id
    _live_response_id: dict[str, str] = {}
    app.state.live_response_id = _live_response_id
    _session_start_cache: dict[str, float] = {}  # session_id → registered start time
    _session_spec_cache: dict[str, _SpecEntry | None] = {}  # session_id → session AgentSpec
    # session_id → the harness the session actually runs, when it differs from
    # the spec's. Smart Routing pins a routed child's harness on the
    # conversation and forwards it as ``harness_override``; without this record
    # every spec-derived read (native-vs-SDK checks above all) still answers
    # with the harness the spec declared, which a routed session is not on.
    _session_harness_overrides: dict[str, str] = {}
    # session_id → revision of the agent bundle its caches were built from
    _session_agent_revisions: dict[str, str] = {}
    _session_snapshot_cache: dict[str, _SessionSnapshot] = {}  # session_id → snapshot
    _session_snapshot_locks: dict[str, asyncio.Lock] = {}  # session_id → snapshot fetch lock
    _session_spec_locks: dict[str, asyncio.Lock] = {}  # session_id → spec resolution lock
    _session_init_tasks: dict[
        tuple[str, str, str | None, str | None], asyncio.Task[JSONResponse]
    ] = {}
    _recovery_turn_ids: dict[str, set[str]] = {}
    _session_init_envelopes: dict[str, tuple[float, RunnerSessionInitEnvelope]] = {}
    # session_id → canonical reasoning effort, seeded from the session-init
    # snapshot and updated by ``effort_change``. In-process harnesses learn the
    # effort only from the forwarded turn body, which is built field by field.
    _session_reasoning_effort: dict[str, str] = {}
    _session_skills_cache: dict[str, tuple[float, list[SkillSpec]]] = {}
    _session_workspace_cache: dict[str, str | None] = {}  # session_id → workspace path
    _session_cursor_model_names: dict[str, dict[str, str]] = {}
    _session_claude_launch_configs: dict[str, ClaudeNativeUcodeConfig | None] = {}
    _session_claude_launch_config_tasks: dict[
        str, asyncio.Task[ClaudeNativeUcodeConfig | None]
    ] = {}
    # Claude's session listing IS the shared launch catalog: the same
    # fingerprint-keyed store file the launch resolved against and the
    # host's pre-launch picker serves — identical by construction, no
    # separate composition. Rows are re-read from the store once
    # ``_CLAUDE_MODEL_OPTIONS_CACHE_TTL_S`` passes (so its background
    # re-probe reaches the picker) and dropped whenever the launch config is
    # recorded or dropped, so a relaunch under another provider never serves
    # the previous one's rows. Entries pair a ``time.monotonic()`` deadline
    # with the rows. A cold store pays one probe: a short inline wait answers
    # a warm one, past that the endpoint answers 503 (the server's fetch
    # retries those) while the store's single-flight probe completes in the
    # background.
    _claude_model_options_rows: dict[str, tuple[float, list[dict[str, object]]]] = {}

    async def _resolve_session_claude_launch_config(
        session_id: str,
    ) -> ClaudeNativeUcodeConfig | None:
        if session_id in _session_claude_launch_configs:
            return _session_claude_launch_configs[session_id]
        task = _session_claude_launch_config_tasks.get(session_id)
        if task is None:
            from omnigent.harnesses.claude_native.main import resolve_native_claude_config

            async def _load() -> ClaudeNativeUcodeConfig | None:
                spec = await _resolve_session_agent_spec(session_id)
                config = await asyncio.to_thread(resolve_native_claude_config, spec=spec)
                _session_claude_launch_configs[session_id] = config
                return config

            task = asyncio.create_task(_load())
            _session_claude_launch_config_tasks[session_id] = task

            def _forget_completed(
                completed: asyncio.Task[ClaudeNativeUcodeConfig | None],
                sid: str = session_id,
            ) -> None:
                if _session_claude_launch_config_tasks.get(sid) is completed:
                    _session_claude_launch_config_tasks.pop(sid, None)

            task.add_done_callback(_forget_completed)
        return await asyncio.shield(task)

    def _drop_session_claude_launch_config(session_id: str) -> None:
        _session_claude_launch_configs.pop(session_id, None)
        _claude_model_options_rows.pop(session_id, None)
        task = _session_claude_launch_config_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    def _record_session_claude_launch_config(
        session_id: str, config: ClaudeNativeUcodeConfig | None
    ) -> None:
        """
        Memoize the config a launch resolved; rows listed under the old one retire.
        """
        _session_claude_launch_configs[session_id] = config
        _claude_model_options_rows.pop(session_id, None)

    _session_agent_ids = _session_agent_ids_ref  # shared with module-level get_session_agent_id
    _session_sub_agent_names: dict[str, str] = {}
    # Tracks whether the sub-agent was successfully resolved on session create.
    # True = child spec is cached; False = parent kept as fallback after miss.
    # Absent = session has no sub_agent_name or create has not completed.
    _session_sub_agent_resolved: dict[str, bool] = {}
    # Per-conversation set of (harness, InstructionDelivery) pairs already
    # warned about. Keyed by conversation so a session that switches harnesses
    # warns again for the new pair rather than inheriting the old one's silence.
    _instruction_delivery_warned: dict[str, set[tuple[str, InstructionDelivery]]] = {}
    _session_tool_schemas: dict[str, list[_JsonObject]] = {}  # session_id → cached tool schemas
    _session_mcp_spec_hash: dict[str, str] = {}  # session_id → last MCP spec hash
    _session_comment_relays: dict[str, _CommentRelayBinding] = {}

    # Process-lifetime monotonic fill-generation counter per session id.
    # Incremented (never popped) at delete_session so a fill parked across a
    # delete→recreate of the same id sees a changed generation and discards its
    # result instead of publishing stale data into the new session.
    _session_cache_generations: dict[str, int] = {}

    def _session_cache_generation(session_id: str) -> int:
        """Return (and materialize) the generation a cache fill starts under."""
        return _session_cache_generations.setdefault(session_id, 0)

    def _session_cache_generation_is_current(session_id: str, generation: int) -> bool:
        """True only when the captured generation still matches — fill may write."""
        return _session_cache_generations.get(session_id, 0) == generation

    _codex_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _pi_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _opencode_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _cursor_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _kiro_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _goose_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _qwen_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _kimi_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _hermes_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _claude_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _antigravity_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _devin_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    app.state.antigravity_terminal_ensure_locks = _antigravity_terminal_ensure_locks
    _repl_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _active_turns: dict[str, asyncio.Task[None] | None] = {}
    app.state.active_turns = _active_turns
    # Conversations whose claude-sdk `/compact` published an up-front
    # `response.compaction.in_progress`. Used to (a) swallow the executor's own
    # later `in_progress` so the web shows a single spinner, and (b) publish a
    # `failed` if the turn produced no compaction, so the spinner is never
    # stranded. Discarded on `response.compaction.completed` (real compaction),
    # else cleared by `_on_proxy_stream_end` — the single turn-end convergence
    # point reached on every exit path (clean end, setup error, cancel).
    _sdk_compact_inprogress: set[str] = set()
    app.state.sdk_compact_inprogress = _sdk_compact_inprogress
    _native_pane_status: dict[str, str] = {}
    app.state.native_pane_status = _native_pane_status
    # Detached watchers answering a /model confirm dialog that pops after
    # the active turn settles (a mid-turn switch queues in the composer).
    _model_dialog_watchers: set[asyncio.Task[None]] = set()
    _session_message_buffers: dict[str, list[dict[str, Any]]] = {}
    app.state.session_message_buffers = _session_message_buffers
    _claude_prompt_waiters: dict[str, asyncio.Task[None]] = {}
    app.state.claude_prompt_waiters = _claude_prompt_waiters
    _author_attribution_sessions: set[str] = set()
    _ingest_next_seq: dict[str, int] = {}
    _ingest_now_serving: dict[str, int] = {}
    _ingest_cond: dict[str, asyncio.Condition] = {}
    _interrupted_sessions: set[str] = set()
    app.state.interrupted_sessions = _interrupted_sessions
    # Desynced conversations; cleared when a fresh turn binds.
    _desynced_sessions: set[str] = set()
    app.state.desynced_sessions = _desynced_sessions
    # Required-terminal exits whose handler released the session's harness
    # subprocess. The release closes the httpx client an in-flight
    # ``proxy_stream`` is reading, which surfaces there as a transport error;
    # the stream's failure handler consumes the record to report the exit.
    _required_terminal_exit_errors: dict[str, dict[str, str]] = {}
    # Monotonic epoch stamped at each turn bind; lets recovery detect a replacement that ran
    # and finished during a teardown await (slot empty, but epoch advanced).
    _turn_epoch_seq = itertools.count(1)
    _turn_bind_epoch: dict[str, int] = {}
    app.state.turn_bind_epoch = _turn_bind_epoch
    # Epoch at which desync recovery claimed the terminal token; competing sites skip their
    # ``idle`` only if the epoch still matches.
    _desync_terminalized: dict[str, int] = {}
    app.state.desync_terminalized = _desync_terminalized
    _background_tasks: set[asyncio.Task[Any]] = set()
    # One watcher per session for a pending Databricks sign-in (see _start_sign_in_watch).
    _sign_in_watchers: dict[str, asyncio.Task[None]] = {}
    _subagent_recovery_tasks: dict[str, asyncio.Task[None]] = {}
    _subagent_wake_pending: set[str] = set()
    _last_rewake_notice: dict[str, str] = {}
    # Parents whose wake POST exhausted its bounded retries while their inbox
    # still held a sub-agent result (typically: the server was down when the
    # child finished). The catch-up scan re-attempts these on tunnel reconnect.
    _stranded_wake_parents: set[str] = set()
    # Single-flight holder for the paced stranded-wake retry loop, so
    # back-to-back reconnects don't stack concurrent retry loops.
    _stranded_wake_retry_task: list[asyncio.Task[None]] = []

    _session_histories = _session_histories_ref
    _last_server_item_id: dict[str, str] = {}
    _session_event_queues = _session_event_queues_ref
    app.state.session_event_queues = _session_event_queues
    _session_inboxes = _session_inboxes_ref
    _session_async_tasks: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}

    def _has_active_work() -> bool:
        if _active_turns:
            return True
        if _claude_prompt_waiters:
            return True
        if _has_live_async_tasks(_session_async_tasks):
            return True
        for timers in _session_timers.values():
            for timer_task in timers.values():
                if not timer_task.done():
                    return True
        if pending_approvals.has_any_pending():
            return True
        session_ids = set(_session_start_cache) | set(_session_agent_ids)
        if process_manager is not None and any(
            process_manager.has_active_turn(session_id) for session_id in session_ids
        ):
            return True
        return any(_native_turn_in_flight(session_id) for session_id in session_ids)

    def _native_turn_in_flight(session_id: str) -> bool:
        """Whether a native terminal still reports this session's turn as in flight.

        Native delivery returns once the prompt is typed, so the terminal's own
        status edges decide when the turn settles. SDK turns are already covered
        by ``_active_turns`` and need not publish a closing edge.
        """
        if _native_pane_status.get(session_id) not in _IN_FLIGHT_SESSION_STATUSES:
            return False
        return is_native_harness(_session_harness_name(session_id))

    app.state.has_active_work = _has_active_work

    def _drain_session_streams() -> None:
        for queue in list(_session_event_queues.values()):
            queue.put_nowait(None)

    app.state.drain_session_streams = _drain_session_streams

    def _publish_event(session_id: str, event: Mapping[str, object]) -> None:
        event_body = cast(_JsonObject, event)
        queue = _session_event_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            _session_event_queues[session_id] = queue
        queue.put_nowait(event_body)
        if event_body.get("type") == "session.status":
            _status_value = event_body.get("status")
            if isinstance(_status_value, str):
                _native_pane_status[session_id] = _status_value
        _fan_out_child_delta_to_parent(session_id, event_body)

    def _child_preview_from_status(
        session_id: str,
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> str | None:
        if latest_assistant_text is not None:
            reply_source = latest_assistant_text
        elif allow_history_preview_fallback:
            reply_source = _extract_last_assistant_text(session_id)
        else:
            return None
        reply = reply_source.strip()
        if not reply:
            return None
        return _truncate_child_preview(reply)

    def _child_status_body(
        session_id: str,
        meta: _ChildParentMeta,
        status: str | None,
        *,
        error: dict[str, str] | None = None,
        include_error: bool = False,
    ) -> _JsonObject:
        busy = status in ("running", "waiting")
        child: _JsonObject = {
            "id": session_id,
            "title": meta.title,
            "tool": meta.tool,
            "session_name": meta.session_name,
            "busy": busy,
            "current_task_status": _session_status_to_task_status(status),
        }
        if include_error:
            child["last_task_error"] = error
        return child

    def _child_error_from_status_event(
        status: str | None,
        event: _JsonObject,
    ) -> dict[str, str] | None:
        if status != "failed":
            return None
        raw_error = event.get("error")
        if not isinstance(raw_error, dict):
            return None
        raw_code = raw_error.get("code")
        raw_message = raw_error.get("message")
        if not isinstance(raw_code, str) or not isinstance(raw_message, str):
            return None
        if not raw_code or not raw_message:
            return None
        return {"code": raw_code, "message": raw_message}

    def _build_child_status_update(
        session_id: str,
        meta: _ChildParentMeta,
        status: str | None,
        *,
        error: dict[str, str] | None = None,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> _JsonObject | None:
        if status in ("running", "waiting"):
            mark_subagent_work_started(session_id)
        # A trailing pane-idle edge must not turn a failed or aborted task
        # into success. A new running/waiting edge clears the terminal outcome.
        if status == "idle" and meta.last_task_status in ("failed", "cancelled"):
            return None
        busy = status in ("running", "waiting")
        task_status = _session_status_to_task_status(status)
        error_signature = (error["code"], error["message"]) if error is not None else None
        include_error = status in ("running", "waiting") or error is not None
        if (
            meta.last_busy == busy
            and meta.last_task_status == task_status
            and meta.last_error == error_signature
        ):
            return None
        meta.last_busy = busy
        meta.last_task_status = task_status
        meta.last_error = error_signature
        child = _child_status_body(
            session_id,
            meta,
            status,
            error=error,
            include_error=include_error,
        )
        if not busy:
            preview = _child_preview_from_status(
                session_id,
                latest_assistant_text=latest_assistant_text,
                allow_history_preview_fallback=allow_history_preview_fallback,
            )
            if preview is not None:
                child["last_message_preview"] = preview
        return {
            "type": "session.child_session.updated",
            "conversation_id": meta.parent_id,
            "child_session_id": session_id,
            "child": child,
        }

    def _fan_out_child_delta_to_parent(
        session_id: str,
        event: _JsonObject,
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> None:
        meta = _child_session_parents.get(session_id)
        if meta is None:
            return
        evt_type = event.get("type")
        if evt_type == "session.status":
            raw_status = event.get("status")
            status = raw_status if isinstance(raw_status, str) else None
            child_update = _build_child_status_update(
                session_id,
                meta,
                status,
                error=_child_error_from_status_event(status, event),
                latest_assistant_text=latest_assistant_text,
                allow_history_preview_fallback=allow_history_preview_fallback,
            )
            if child_update is not None:
                _publish_event(meta.parent_id, child_update)

    if resource_registry is None:
        resource_registry = SessionResourceRegistry(
            terminal_registry=terminal_registry,
            runner_workspace=runner_workspace,
            per_session_workspace=per_session_workspace,
        )
    app.state.session_resource_registry = resource_registry

    def _publish_terminal_activity(session_id: str, terminal_id: str) -> None:
        if process_manager is not None:
            process_manager.note_activity(session_id)
        _publish_event(
            session_id,
            {
                "type": "session.terminal.activity",
                "session_id": session_id,
                "terminal_id": terminal_id,
            },
        )

    resource_registry.set_terminal_activity_publisher(_publish_terminal_activity)

    def _publish_session_status(
        session_id: str,
        status: str,
        blocked_on: str | None = None,
    ) -> None:
        event: dict[str, object] = {"type": "session.status", "status": status}
        if blocked_on is not None:
            event["blocked_on"] = blocked_on
        _publish_event(session_id, event)

    resource_registry.set_session_status_publisher(_publish_session_status)

    def _format_terminal_command_for_failure(event: TerminalExitEvent) -> str:
        if event.command is None:
            return "unknown"
        if event.args_count is None or event.args_count == 0:
            return event.command
        noun = "arg" if event.args_count == 1 else "args"
        return (
            f"{event.command} ({event.args_count} {noun}; "
            "argv omitted because terminal args may contain secrets)"
        )

    def _format_required_terminal_exit_output(
        event: TerminalExitEvent, diagnosis: FailureDiagnosis | None
    ) -> str:
        command = _format_terminal_command_for_failure(event)
        cwd = event.cwd or "unknown"
        parts: list[str] = []
        if diagnosis is not None:
            # Lead with the human interpretation so the failure reads clearly
            # even before the raw diagnostics block.
            parts.extend([diagnosis.title, "", diagnosis.cause])
            if diagnosis.remediation:
                parts.extend(["", f"Try this: {diagnosis.remediation}"])
        else:
            parts.append(
                "Required terminal exited unexpectedly; the session runtime is no longer "
                "available."
            )
        exited_with = (
            f" (exited with status {event.exit_status})" if event.exit_status is not None else ""
        )
        parts.extend(
            [
                "",
                "Terminal diagnostics:",
                f"terminal: {event.terminal_name}:{event.session_key}",
                f"command: {command}{exited_with}",
                f"cwd: {cwd}",
            ]
        )
        if event.last_output:
            parts.extend(["", "Last captured terminal output:", event.last_output])
        else:
            parts.extend(
                [
                    "",
                    "Last captured terminal output: unavailable. The process exited before "
                    "Omnigent captured a pane snapshot.",
                ]
            )
        return "\n".join(parts)

    def _build_required_terminal_error(
        event: TerminalExitEvent, diagnosis: FailureDiagnosis | None
    ) -> dict[str, str]:
        """Build the structured ``session.status`` error for a required-terminal exit.

        Always carries ``code`` + a fully-composed ``message`` (back-compat: the
        REPL and older clients render it verbatim). When the failure is
        recognized, also carries ``title`` / ``cause`` / ``remediation`` so the
        web UI can render a friendly card instead of the raw enum + blob.

        :param event: The required terminal's exit event.
        :param diagnosis: The exit's :func:`classify_terminal_failure` result.
        """
        message = _format_required_terminal_exit_output(event, diagnosis)
        error: dict[str, str] = {"code": "required_terminal_exited", "message": message}
        if diagnosis is not None:
            error["title"] = diagnosis.title
            error["cause"] = diagnosis.cause
            if diagnosis.remediation:
                error["remediation"] = diagnosis.remediation
        return error

    def _live_terminal_pane_snapshot(conv_id: str) -> str | None:
        """Return the first captured pane text among the conversation's terminals.

        Serves failures that strike while terminals are still alive (e.g. a
        harness stream drop): the required-terminal *exit* diagnostics never
        fire, yet what is on the pane right now is often the most actionable
        context available (a CLI parked at a trust prompt, an auth error, ...).
        """
        registry = resource_registry.terminal_registry if resource_registry else None
        if registry is None:
            return None
        for entry in registry.list_for_conversation(conv_id):
            try:
                pane = trim_terminal_output(entry.instance.last_pane_text())
            except Exception:
                _logger.exception(
                    "Failed to read terminal pane diagnostics for %s",
                    conv_id,
                    extra={"session_id": conv_id},
                )
                continue
            if pane:
                return pane
        return None

    def _harness_stream_failure_message(conv_id: str, exc: BaseException) -> str:
        """Compose the user-facing message for a harness stream failure.

        Leads with the real transport cause (which otherwise reaches only the
        runner log) and attaches the live pane snapshot as a ``Last captured
        terminal output:`` block, which the web UI renders as diagnostics.
        """
        # httpx raises several transport errors with no message at all
        # (``ReadError()``), which left the whole diagnostic as the bare
        # sentence. Fall back to the exception type so the message always names
        # which transport failure ended the stream.
        cause = str(exc).strip() or type(exc).__name__
        message = f"Harness stream connection error: {cause}"
        pane = _live_terminal_pane_snapshot(conv_id)
        if pane:
            message = f"{message}\n\nLast captured terminal output:\n{pane}"
        return message

    def _release_required_terminal_session(session_id: str) -> None:
        if process_manager is None:
            return

        async def _release() -> None:
            # Let a live turn stream converge first (bounded): the harness's own
            # failure event may already be on the wire, and releasing now would
            # sever the stream carrying it.
            deadline = time.monotonic() + _TERMINAL_EXIT_RELEASE_GRACE_S
            while session_id in _live_response_id and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            try:
                await process_manager.release(session_id)
            except Exception:
                _logger.exception(
                    "Failed to release harness subprocess after required terminal exit: "
                    "session=%s",
                    session_id,
                    extra={"session_id": session_id},
                )

        task = asyncio.create_task(
            _release(),
            name=f"required-terminal-release:{session_id}",
        )
        task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(task)

    def _publish_terminal_exit(event: TerminalExitEvent) -> None:
        _publish_event(
            event.session_id,
            {
                "type": "session.resource.deleted",
                "resource_id": event.terminal_id,
                "resource_type": "terminal",
                "session_id": event.session_id,
            },
        )
        # Auxiliary terminals do not own the session control plane. In
        # particular, losing Codex's streamable TUI must not cancel an active
        # app-server turn; the native-terminal ensure path can recreate it.
        if event.lifecycle != TerminalLifecycle.REQUIRED:
            return

        # A required terminal exit ends the session. Tear down any registered
        # Codex app-server alongside it; this is a no-op for other harnesses.
        _teardown_task = asyncio.create_task(
            _native_runtime.teardown_codex_native_app_server(event.session_id)
        )
        _teardown_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_teardown_task)

        # Record the exit before releasing the harness: the release severs any
        # in-flight turn stream, whose failure handler then reports this exit
        # instead of the transport error the severed socket raises.
        # Classify once; the error card and the failure log share the diagnosis.
        diagnosis = classify_terminal_failure(
            command=event.command,
            exit_status=event.exit_status,
            output=event.last_output,
        )
        error = _build_required_terminal_error(event, diagnosis)
        _required_terminal_exit_errors[event.session_id] = error
        # A dead required terminal cannot still be working a turn.
        _native_pane_status.pop(event.session_id, None)

        if event.terminal_name in ("qwen", "antigravity") and event.session_key == "main":
            _publish_event(event.session_id, {"type": "session.status", "status": "idle"})
            _release_required_terminal_session(event.session_id)
            return

        # A claude /exit or /quit prints this banner and exits 0. Printing it
        # can flip the idle memo back to "running" before pane death, so treat
        # a banner exit as a clean stop rather than a failure.
        if (
            event.exit_status == 0
            and event.terminal_name == "claude"
            and event.last_output is not None
            and _CLAUDE_VOLUNTARY_EXIT_MARKER in event.last_output
        ):
            _publish_event(event.session_id, {"type": "session.status", "status": "idle"})
            _release_required_terminal_session(event.session_id)
            return

        if event.session_was_idle:
            _release_required_terminal_session(event.session_id)
            return

        if _shutting_down.is_set():
            # tmux died with this runner's process group on a stop signal, not
            # a crash; the server settles the turn from the dropped tunnel.
            _logger.info(
                "required terminal %s exited for %s while the runner is shutting down; "
                "not failing the turn",
                event.terminal_name,
                event.session_id,
                extra={"session_id": event.session_id},
            )
            _release_required_terminal_session(event.session_id)
            return

        exit_log = debug_event("required_terminal_exited", session_id=event.session_id)
        exit_log["attributes"] = {
            **{
                key: value
                for key, value in event.lifecycle_context.items()
                if key not in {"event_name", "session_id", "turn_id", "user_id"}
            },
            "terminal_id": event.terminal_id,
            "terminal_instance_id": event.terminal_instance_id,
            "terminal_name": event.terminal_name,
            "terminal_exit_status": event.exit_status,
            "runner_shutting_down": _shutting_down.is_set(),
            "error_code": error["code"],
            # An unrecognized exit is the harness CLI dying under the runner.
            "error_category": (diagnosis.category if diagnosis else ErrorCategory.RUNNER).value,
            "error_impact": ErrorImpact.BLOCKING.value,
        }
        _logger.error(
            "required terminal %s exited; failing turn for %s: %s",
            event.terminal_name,
            event.session_id,
            error.get("message"),
            extra=exit_log,
        )
        _publish_event(
            event.session_id,
            {
                "type": "session.status",
                "status": "failed",
                "error": error,
            },
        )
        # The turn is over (its terminal exited), so drop any interrupt still
        # pending for it — otherwise its stale grace timer, or a reused child's
        # next idle consuming that pending record, could mis-settle a later
        # dispatch on this session.
        _native_interrupt_runner.clear_pending_interrupt(event.session_id)
        _mark_subagent_terminal_and_wake(
            event.session_id,
            status="failed",
            output=error["message"],
        )
        _release_required_terminal_session(event.session_id)

    resource_registry.set_terminal_exit_publisher(_publish_terminal_exit)

    from omnigent.runtime.filesystem_registry import (
        FilesystemRegistry,
        create_filesystem_registry,
        detect_git_root,
    )

    if runner_workspace is not None:
        filesystem_registry = create_filesystem_registry(watch_path=runner_workspace)
        filesystem_registry.start()
    else:
        filesystem_registry = None
    app.state.filesystem_registry = filesystem_registry

    _session_fs_registries: dict[str, FilesystemRegistry] = {}
    # Roots whose search registry stays warm; bounded so distinct generated
    # workspaces cannot accumulate for the runner's lifetime.
    _search_fs_registries: OrderedDict[str, FilesystemRegistry] = OrderedDict()
    _search_registry_cache_size = 8

    def _search_registry_for_root(root: Path) -> FilesystemRegistry:
        """Registry rooted at *root*, the tree a search actually walks.

        The session registry watches the session's stored workspace (or the
        runner's), which is not necessarily the environment root the search
        walks — runner-managed sessions get a generated per-session workspace
        no other registry covers. The repository root is re-detected on every
        call, so a repository created or removed mid-session — including one
        nested at the workspace root inside an outer repository — is read on
        the next search. The registry is never started: startup
        runs ``git update-index`` inside the repository, and a repository at a
        generated workspace root may be the agent's own. Search needs only
        the anchored index read.

        :param root: Absolute directory the search walks.
        :returns: A registry whose workspace root is *root*.
        """
        key = str(root)
        registry = _search_fs_registries.get(key)
        if registry is None or registry.git_root != detect_git_root(root):
            registry = create_filesystem_registry(watch_path=root)
            _search_fs_registries[key] = registry
            while len(_search_fs_registries) > _search_registry_cache_size:
                _search_fs_registries.popitem(last=False)
        _search_fs_registries.move_to_end(key)
        return registry

    async def _session_snapshot(session_id: str) -> _SessionSnapshot:
        cached = _session_snapshot_cache.get(session_id)
        if cached is not None:
            return cached
        lock = _session_snapshot_locks.setdefault(session_id, asyncio.Lock())
        generation = _session_cache_generation(session_id)
        async with lock:
            cached = _session_snapshot_cache.get(session_id)
            if cached is not None:
                return cached
            status_code: int | None = None
            created_at: float | None = None
            workspace: str | None = None
            agent_id: str | None = None
            sub_agent_name: str | None = None
            parent_session_id: str | None = None
            agent_name: str | None = None
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{session_id}", params=_SESSION_METADATA_PARAMS
                )
                status_code = resp.status_code
                if resp.status_code == 200:
                    body = resp.json()
                    raw_created = body.get("created_at")
                    if raw_created is not None:
                        created_at = float(raw_created)
                    workspace = body.get("workspace")
                    raw_agent_id = body.get("agent_id")
                    if isinstance(raw_agent_id, str) and raw_agent_id:
                        agent_id = raw_agent_id
                    raw_sub_agent = body.get("sub_agent_name")
                    if isinstance(raw_sub_agent, str) and raw_sub_agent:
                        sub_agent_name = raw_sub_agent
                    raw_parent = body.get("parent_session_id")
                    if isinstance(raw_parent, str) and raw_parent:
                        parent_session_id = raw_parent
                    raw_agent_name = body.get("agent_name")
                    if isinstance(raw_agent_name, str) and raw_agent_name:
                        agent_name = raw_agent_name
            except Exception:  # noqa: BLE001 — best-effort; created_at falls back to wall time
                pass
            snapshot = _SessionSnapshot(
                ok=status_code == 200,
                status_code=status_code,
                created_at=created_at if created_at is not None else time.time(),
                workspace=workspace,
                agent_id=agent_id,
                sub_agent_name=sub_agent_name,
                parent_session_id=parent_session_id,
                agent_name=agent_name,
            )
            if snapshot.ok and snapshot.agent_id is not None:
                if _session_cache_generation_is_current(session_id, generation):
                    _session_snapshot_cache[session_id] = snapshot
            return snapshot

    async def _session_workspace_value(session_id: str) -> str | None:
        if session_id not in _session_workspace_cache:
            generation = _session_cache_generation(session_id)
            snapshot = await _session_snapshot(session_id)
            # A failed fetch carries no workspace. Memoizing its ``None``
            # would pin the session to the global workspace for its lifetime.
            if not snapshot.ok:
                return None
            if _session_cache_generation_is_current(session_id, generation):
                _session_workspace_cache[session_id] = snapshot.workspace
        return _session_workspace_cache.get(session_id)

    async def _fetch_session_model_override(session_id: str) -> str | None:
        """One-shot uncached read of the persisted ``/model`` override.

        Legacy (no-envelope) init only — current servers ship the override in
        the init envelope. Deliberately NOT cached in ``_SessionSnapshot``:
        ``model_override`` is mutable (a ``/model`` switch changes it), so a
        value stored in the long-lived identity cache would go stale and reseed
        the old model on a later re-init, forcing a needless respawn. Each init
        re-reads it fresh.
        """
        try:
            resp = await server_client.get(
                f"/v1/sessions/{session_id}", params=_SESSION_METADATA_PARAMS
            )
            if resp.status_code == 200:
                raw = resp.json().get("model_override")
                if isinstance(raw, str) and raw:
                    return raw
            else:
                _logger.warning(
                    "legacy model_override fallback for %s: session GET returned "
                    "HTTP %s; a model-pinned first turn may respawn",
                    session_id,
                    resp.status_code,
                )
        except Exception:  # noqa: BLE001 — best-effort, but surface it
            _logger.warning(
                "legacy model_override fallback for %s failed; a model-pinned "
                "first turn may respawn",
                session_id,
                exc_info=True,
            )
        return None

    async def _session_runtime_cwd(session_id: str) -> Path | None:
        workspace = await _session_workspace_value(session_id)
        if workspace and workspace.strip():
            return Path(workspace.strip()).expanduser().resolve()
        return runner_workspace.resolve() if runner_workspace is not None else None

    async def _load_legacy_session_init_context() -> _SessionInitContext:
        await _get_server_version(server_client)
        return _SessionInitContext(envelope=None)

    def _load_envelope_session_init_context(
        envelope: RunnerSessionInitEnvelope,
        *,
        session_id: str,
        agent_id: str,
    ) -> _SessionInitContext:
        from omnigent.runner.session_init_protocol import validate_runner_inference_config

        if envelope.session_id != session_id or envelope.agent_id != agent_id:
            raise ValueError("session initialization envelope identity mismatch")
        validate_runner_inference_config(envelope.snapshot.inference_config)

        global _server_version
        _server_version = envelope.server_version
        snapshot = envelope.snapshot
        _session_snapshot_cache[session_id] = _SessionSnapshot(
            ok=True,
            status_code=200,
            created_at=float(snapshot.created_at),
            workspace=snapshot.workspace,
            agent_id=agent_id,
            sub_agent_name=envelope.sub_agent_name,
            parent_session_id=snapshot.parent_session_id,
        )
        _session_start_cache[session_id] = float(snapshot.created_at)
        _session_workspace_cache[session_id] = snapshot.workspace
        if envelope.sub_agent_name:
            _session_sub_agent_names[session_id] = envelope.sub_agent_name
        if snapshot.reasoning_effort:
            _session_reasoning_effort[session_id] = snapshot.reasoning_effort
        _session_init_envelopes[session_id] = (time.monotonic(), envelope)
        return _SessionInitContext(envelope=envelope)

    def _fresh_session_init_envelope(session_id: str) -> RunnerSessionInitEnvelope | None:
        cached = _session_init_envelopes.get(session_id)
        if cached is None:
            return None
        cached_at, envelope = cached
        if time.monotonic() - cached_at <= _SESSION_INIT_ENVELOPE_TTL_SECONDS:
            return envelope
        _session_init_envelopes.pop(session_id, None)
        return None

    async def _load_session_init_context(
        body: _JsonObject,
        *,
        session_id: str,
        agent_id: str,
    ) -> _SessionInitContext:
        envelope = parse_runner_session_init_envelope(body)
        if envelope is None:
            if os.environ.get("OMNIGENT_INFERENCE_CONFIG"):
                from omnigent.runner.session_init_protocol import validate_runner_inference_config

                validate_runner_inference_config(None)
            return await _load_legacy_session_init_context()
        body_sub_agent = body.get("sub_agent_name")
        if envelope.sub_agent_name != (
            body_sub_agent if isinstance(body_sub_agent, str) else None
        ):
            raise ValueError("session initialization envelope sub-agent mismatch")
        return _load_envelope_session_init_context(
            envelope,
            session_id=session_id,
            agent_id=agent_id,
        )

    async def _resolve_session_fs_registry(
        session_id: str,
    ) -> FilesystemRegistry | None:
        if session_id in _session_fs_registries:
            return _session_fs_registries[session_id]

        session_workspace = await _session_workspace_value(session_id)
        if session_workspace is None:
            return filesystem_registry

        session_ws_path = Path(session_workspace).resolve()
        runner_ws_resolved = runner_workspace.resolve() if runner_workspace is not None else None
        if runner_ws_resolved is not None and session_ws_path == runner_ws_resolved:
            return filesystem_registry

        registry = create_filesystem_registry(watch_path=session_ws_path)
        registry.start()
        _session_fs_registries[session_id] = registry
        return registry

    from omnigent.entities.environment_filesystem import (
        ResourceError,
    )

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ValueError)
    async def _handle_value_error(
        request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": str(exc),
                },
            },
        )

    @app.exception_handler(ResourceError)
    async def _handle_resource_error(
        request: Request,
        exc: ResourceError,
    ) -> JSONResponse:
        del request
        from omnigent.entities.environment_filesystem import (
            DirectoryNotEmpty,
            FilesystemPathNotFound,
            FileTooLarge,
            InvalidPath,
            PathUnreachable,
            UnsupportedMediaType,
        )

        status = 500
        error: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, FilesystemPathNotFound):
            status = 404
        elif isinstance(exc, PathUnreachable):
            # 403, not 400: the path is well-formed, the caller just may not
            # see it. Carries the reachable roots so a UI can say what IS
            # available without a second round trip.
            status = 403
            error["reachable_roots"] = exc.reachable_roots
        elif isinstance(exc, InvalidPath):
            status = 400
        elif isinstance(exc, DirectoryNotEmpty):
            status = 409
        elif isinstance(exc, FileTooLarge):
            status = 413
        elif isinstance(exc, UnsupportedMediaType):
            status = 415
        return JSONResponse(
            status_code=status,
            content={"error": error},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/sessions/{conversation_id}/background-title",
        response_model=BackgroundSessionTitleResponse,
    )
    async def generate_background_session_title(
        conversation_id: str,
        body: BackgroundSessionTitleRequest,
    ) -> BackgroundSessionTitleResponse | JSONResponse:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": "Background titles require a HarnessProcessManager.",
                },
            )

        sub_agent_name = body.sub_agent_name or await _recover_sub_agent_name(conversation_id)
        resolver_agent_id = body.agent_id or _session_agent_ids.get(conversation_id)
        resolver_cwd = await _session_runtime_cwd(conversation_id)
        try:
            effective_harness, spawn_env = await _resolve_harness_config(
                resource_registry=resource_registry,
                agent_id=resolver_agent_id,
                spec_resolver=spec_resolver,
                session_id=conversation_id,
                model_override=body.model_override,
                harness_override=body.harness_override,
                sub_agent_name=sub_agent_name,
                cwd=resolver_cwd,
            )
            generator_spec = generator_spec_for_harness(effective_harness)
            if generator_spec is None:
                return BackgroundSessionTitleResponse(status="unsupported")
            resolver_harness = generator_spec.resolver_harness or effective_harness
            if resolver_harness != effective_harness:
                resolved_harness, spawn_env = await _resolve_harness_config(
                    resource_registry=resource_registry,
                    agent_id=resolver_agent_id,
                    spec_resolver=spec_resolver,
                    session_id=conversation_id,
                    model_override=body.model_override,
                    harness_override=resolver_harness,
                    sub_agent_name=sub_agent_name,
                    cwd=resolver_cwd,
                )
                if resolved_harness != resolver_harness:
                    return BackgroundSessionTitleResponse(status="unsupported")
        except (httpx.HTTPError, RuntimeError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "spec_resolver_failed",
                    "detail": _client_safe_error_detail(exc, context="spec resolve"),
                },
            )

        context = BackgroundTitleContext(
            prompt=body.prompt[:BACKGROUND_TITLE_MAX_PROMPT_CHARS],
            harness=effective_harness,
            spawn_env=dict(spawn_env or {}),
            process_manager=process_manager,
            cwd=resolver_cwd,
            model_override=body.model_override,
            session_spec=_unwrap_spec_entry(_session_spec_cache.get(conversation_id)),
            additional_instructions=body.additional_instructions,
        )
        try:
            title = await run_background_title(context)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "error": "title_harness_timeout",
                    "detail": "Harness title generation timed out.",
                },
            )
        except BackgroundTitleHarnessError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "title_harness_failed", "detail": str(exc)},
            )
        except (ImportError, OSError, RuntimeError) as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "title_harness_failed",
                    "detail": _client_safe_error_detail(exc, context="title harness"),
                },
            )

        if title is None:
            return JSONResponse(
                status_code=502,
                content={
                    "error": "title_harness_failed",
                    "detail": "Harness title generation returned no text.",
                },
            )
        return BackgroundSessionTitleResponse(
            status="generated",
            title=" ".join(title.split()),
        )

    async def _initialize_session(body: _JsonObject) -> JSONResponse:
        from omnigent.runner.session_init_protocol import RunnerInferenceConfigMismatch

        raw_id = body.get("session_id")
        set_current_session_id(raw_id if isinstance(raw_id, str) else None)
        _logger.info(
            "Runner session initialization started",
            extra=debug_event("runner_session_init_started", stage="session_init"),
        )
        if process_manager is None:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=501,
                    error_code="not_implemented",
                ),
            )
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner POST /v1/sessions needs a HarnessProcessManager."),
                },
            )
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        if not session_id or not agent_id:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=400,
                    error_code="invalid_request",
                ),
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": ("'session_id' and 'agent_id' required."),
                },
            )
        session_id = cast(str, session_id)
        agent_id = cast(str, agent_id)
        initial_turn_epoch = _turn_bind_epoch.get(session_id)
        initial_native_activity = resource_registry.session_activity_epoch(session_id)
        initially_active = session_id in _active_turns or resource_registry.session_turn_is_active(
            session_id
        )

        # Captured before init's first await: the legacy (no-envelope) context
        # load below probes the server's version over the network, so a reset
        # landing anywhere in init — including that probe — must fence the
        # memoizing writes at the end of init.
        spec_cache_generation = _session_cache_generation(session_id)

        try:
            init_context = await _load_session_init_context(
                body,
                session_id=session_id,
                agent_id=agent_id,
            )
        except RunnerInferenceConfigMismatch:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "inference_config_mismatch",
                    "detail": (
                        "This runner has a different saved provider configuration; "
                        "launch a new runner."
                    ),
                },
            )
        except ValueError:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=400,
                    error_code="invalid_request",
                ),
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Invalid session initialization envelope.",
                },
            )

        # Stamp the session's Smart Routing class before anything reads it: the
        # spawn env is rebuilt on every harness respawn, long after this
        # envelope is gone, and on the codex family the class decides whether
        # the session gets the extended model catalog and the spawn-routing
        # endpoint at all.
        _routing_class = init_context.routing_class
        remember_session_routing_class(session_id, _routing_class)
        if init_context.envelope is not None:
            _note_session_harness_override(
                session_id, init_context.envelope.snapshot.harness_override
            )

        spec: AgentSpec | None = None
        spec_entry: _SpecEntry | None = None
        if spec_resolver is not None:
            try:
                spec_entry = await spec_resolver(agent_id, session_id)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                _logger.error(
                    "Runner session initialization failed",
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        status_code=503,
                        error_code="spec_resolver_failed",
                    ),
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        if spec_entry is not None:
            spec = _unwrap_spec_entry(spec_entry)
            raw_sub_agent_name = body.get("sub_agent_name")
            _sa_name_assign = cast(str | None, raw_sub_agent_name)
            # A sub-agent's bundle assets live under its own directory; keeping
            # the parent's workdir would load the parent's skills and local
            # tools into the child.
            if _sa_name_assign:
                _sub_entry = _native_runtime._resolve_sub_agent_spec_entry(
                    spec_entry, _sa_name_assign
                )
                if _sub_entry is None:
                    _warn_unresolved_sub_agent(session_id, _sa_name_assign)
                    _session_sub_agent_resolved[session_id] = False
                else:
                    spec_entry = _sub_entry
                    spec = _unwrap_resolved_spec(_sub_entry)
                    _session_sub_agent_resolved[session_id] = True
            # The session's override outranks the spec: resolving from the spec
            # alone made init spawn a harness the turns never ask for, evicting
            # the override's live subprocess (entries are keyed by conversation).
            raw_harness = (
                _session_harness_overrides.get(session_id)
                or spec.executor.config.get("harness")
                or spec.executor.type
            )
            harness_name = canonicalize_harness(raw_harness) or raw_harness

            from omnigent.runner.policy import AgentStartPolicyError

            try:
                _start_data = await _evaluate_agent_start_gate(spec, harness_name)
            except AgentStartPolicyError as exc:
                # The gate raises without logging; this is the single record of
                # the failure. exc_info keeps any underlying policy traceback.
                _logger.error(
                    "Runner session initialization failed",
                    exc_info=True,
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        status_code=403,
                        error_code="agent_start_policy_unevaluable",
                        policy_name=exc.policy_name,
                        reason=exc.reason,
                    ),
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "agent_start_policy_unevaluable",
                        "detail": str(exc),
                    },
                )
            if _start_data is not None:
                _apply_sandbox_override_from_start_data(spec, _start_data)

            await _ensure_session_subagent_router(
                session_id,
                harness_name,
                server_client=server_client,
                routing_class=_routing_class,
            )
            # Seed the initial spawn with the persisted /model override so a
            # model-pinned session's first turn doesn't force a wasteful
            # model-switch respawn. Current servers ship the override in the
            # init envelope (read fresh each init); legacy (no-envelope) servers
            # fall back to a one-shot uncached GET. Both sources are read fresh
            # so a later /model switch can't reseed a stale model. Native
            # harnesses no-op (_build_spawn_env_from_spec guards on env=None).
            _model_override = (
                init_context.envelope.snapshot.model_override
                if init_context.envelope is not None
                else await _fetch_session_model_override(session_id)
            )
            try:
                spawn_env = _build_spawn_env_from_spec(
                    spec,
                    raw_harness,
                    workdir=_resolved_spec_workdir(spec_entry),
                    cwd=await _session_runtime_cwd(session_id),
                    session_id=session_id,
                    model_override=_model_override,
                    resource_registry=resource_registry,
                )
            except OmnigentError as exc:
                # The relay also needs the failure when init precedes the first turn.
                _publish_turn_status(
                    session_id, "failed", error={"code": exc.code, "message": str(exc)}
                )
                raise
            if spawn_env is None:
                spawn_env = await _resolve_native_spawn_env(
                    harness_name,
                    session_id,
                    server_client=server_client,
                    optional_labels=init_context.labels,
                )
            # An agent-cache reset acknowledged while init awaited retired
            # this entry; skip the memoization so the reset wins. Init still
            # runs on what it resolved — the next spec read re-resolves.
            if _session_cache_generation_is_current(session_id, spec_cache_generation):
                _session_spec_cache[session_id] = spec_entry
        else:
            if spec_resolver is not None:
                # spec_resolver was configured but returned no spec for this
                # agent_id. Return a clear 400 rather than silently proceeding
                # with the test-only harness and leaving the session in a
                # broken/unrunnable state.
                _logger.error(
                    "Runner session initialization failed",
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        status_code=400,
                        error_code="no_agent_spec",
                    ),
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "no_agent_spec",
                        "detail": (
                            f"No agent spec found for agent_id={agent_id!r}. "
                            "Ensure the agent is registered before creating a session."
                        ),
                    },
                )
            harness_name = "runner-test-default"
            spawn_env = None

        try:
            await process_manager.get_client(
                session_id,
                harness_name,
                env=spawn_env,
            )
        except RuntimeError as exc:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=503,
                    error_code="harness_spawn_failed",
                ),
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _session_start_cache.setdefault(session_id, time.time())
        # The same reset that retires the spec entry also retires this
        # binding, and later resets read it to decide which agent's shared
        # ``_spec_cache`` entry to drop; reinstating a superseded binding
        # would misdirect them at the old agent. Same fence as the spec-cache
        # write above — a fenced-out reader falls back to the fresh session
        # snapshot instead.
        if _session_cache_generation_is_current(session_id, spec_cache_generation):
            _session_agent_ids[session_id] = agent_id
        else:
            # The reset that fenced this binding ran before init registered
            # the harness, so it could not release the subprocess spawned
            # above with the superseded spec's environment baked in. With
            # the binding left absent, the next turn's prior-binding teardown
            # would skip release too, and a new agent sharing the harness and
            # model would silently reuse the stale process. Release it now so
            # the next turn respawns from the freshly resolved spec.
            _logger.info(
                "session init raced an agent-cache reset; releasing the "
                "harness spawned from the superseded spec",
                extra={"session_id": session_id},
            )
            # Conditional on idleness (the reaper's guard): a consumer that
            # touched or is mid-turn on the shared entry after this cutoff is
            # never torn down out from under it; the entry init itself just
            # registered predates the cutoff and is released.
            await process_manager.release(session_id, only_if_idle_cutoff=time.monotonic())
        if session_id not in _session_event_queues:
            _session_event_queues[session_id] = asyncio.Queue()
        if session_id not in _session_inboxes:
            _session_inboxes[session_id] = asyncio.Queue()
        # A fresh queue can mean a fresh runner process rather than a fresh
        # session: re-queue results the previous process never drained. Start
        # the durable server scan now, but overlap it with terminal creation;
        # sys_read_inbox uses the same locked helper if a turn races the scan.
        _deliver_retained_subagent_results(session_id)
        _subagent_recovery_task = _start_subagent_recovery(session_id)
        if session_id not in _session_async_tasks:
            _session_async_tasks[session_id] = {}
        raw_sub_agent_name = body.get("sub_agent_name")
        _sa_name = cast(str | None, raw_sub_agent_name)
        if _sa_name:
            _session_sub_agent_names[session_id] = _sa_name

        terminal_ready: bool | None = None

        _native_agent = native_coding_agent_for_harness(harness_name)
        if _native_agent is not None:
            # Each native harness contributes only its launch parameters here;
            # a single _launch_native_terminal call at the end runs them. The
            # 8 uniform harnesses differ only in their lock dict and whether
            # they pass an agent-spec resolver; the 3 special harnesses
            # (claude/codex/antigravity) add a pre_launch check and, for
            # claude/codex, a build_context enrichment. All wire the comment
            # relay (pi/opencode route their policy hook through it).
            _launch_locks = _require_full_native_lock_coverage(
                {
                    "claude": _claude_terminal_ensure_locks,
                    "codex": _codex_terminal_ensure_locks,
                    "pi": _pi_terminal_ensure_locks,
                    "cursor": _cursor_terminal_ensure_locks,
                    "kiro": _kiro_terminal_ensure_locks,
                    "antigravity": _antigravity_terminal_ensure_locks,
                    "opencode": _opencode_terminal_ensure_locks,
                    "goose": _goose_terminal_ensure_locks,
                    "hermes": _hermes_terminal_ensure_locks,
                    "qwen": _qwen_terminal_ensure_locks,
                    "kimi": _kimi_terminal_ensure_locks,
                    "devin": _devin_terminal_ensure_locks,
                }
            )[_native_agent.key]
            _launch_ctx = NativeLaunchContext(
                session_id=session_id,
                resource_registry=resource_registry,
                publish_event=_publish_event,
                server_client=server_client,
                event_dispatcher=getattr(app.state, "runner_event_dispatcher", None),
                ensure_comment_relay=_ensure_comment_relay_started,
            )
            _launch_pre: Callable[[bool], Awaitable[PreLaunchResult]] | None = None
            _launch_build: (
                Callable[[NativeLaunchContext], Awaitable[NativeLaunchContext]] | None
            ) = None
            _launch_resolve_spec: (
                Callable[[], Awaitable[AgentSpec | ResolvedSpec | None]] | None
            ) = None

            if harness_name == "claude-native":

                async def _claude_pre_launch(has_terminal: bool) -> PreLaunchResult:
                    # Mirror the inline arm exactly: a rebuild (agent switch) tears
                    # the stale terminal down, but the transfer-inbound check still
                    # runs on the resulting terminal-absent state and, if a sibling
                    # session's terminal is rotating in, wins over create. So the
                    # combined rebuild+inbound case is teardown + wait-for-transfer,
                    # NOT teardown + fresh create (which would race the rotation).
                    wants_rebuild = has_terminal and await _claude_native_session_wants_rebuild(
                        server_client, session_id, init_context.envelope
                    )
                    if wants_rebuild:
                        _logger.info(
                            "Claude terminal stale after agent switch; tearing it down to "
                            "rebuild from current items: session=%s",
                            session_id,
                            extra={"session_id": session_id},
                        )
                    # The inline arm ran the transfer check whenever the terminal was
                    # (or just became, via rebuild) absent. Return force_recreate and
                    # skip together: the shell tears down first (rebuild), then honors
                    # skip (inbound) — so rebuild+inbound is teardown + wait-for-transfer.
                    inbound = False
                    if not has_terminal or wants_rebuild:
                        inbound = await _claude_native_terminal_arrives_via_transfer(
                            server_client=server_client,
                            session_id=session_id,
                            resource_registry=resource_registry,
                            session_labels=init_context.labels,
                        )
                        _logger.info(
                            "Claude terminal transfer-inbound check: session=%s "
                            "terminal_inbound=%s",
                            session_id,
                            inbound,
                            extra={"session_id": session_id},
                        )
                    return PreLaunchResult(force_recreate=wants_rebuild, skip=inbound)

                async def _claude_build_context(ctx: NativeLaunchContext) -> NativeLaunchContext:
                    bundle_dir: Path | None = None
                    agent_name: str | None = None
                    skills_filter: str | list[str] = "all"
                    try:
                        spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        spec = None
                        _logger.info(
                            "Claude terminal spec resolution failed; continuing without "
                            "bundle skills: session=%s",
                            session_id,
                            extra={"session_id": session_id},
                        )
                    if spec is not None:
                        entry = _session_spec_cache.get(session_id)
                        bundle_dir = _resolved_spec_workdir(entry) if entry is not None else None
                        agent_name = getattr(spec, "name", None)
                        skills_filter = getattr(spec, "skills_filter", "all")
                    if bundle_dir is None:
                        bundle_dir = Path(tempfile.mkdtemp(prefix="omnigent-skill-bundle-"))
                    _logger.info(
                        "Claude terminal auto-create inputs resolved: session=%s "
                        "bundle_dir=%s agent_name=%s skills_filter=%s",
                        session_id,
                        bundle_dir,
                        agent_name,
                        skills_filter,
                        extra={"session_id": session_id},
                    )
                    _ensure_orchestrator_skills_in_bundle(bundle_dir, spec)
                    return dataclasses.replace(
                        ctx,
                        bundle_dir=bundle_dir,
                        agent_name=agent_name,
                        agent_spec=spec,
                        skills_filter=skills_filter,
                        session_init=init_context.envelope,
                        auth_token_factory=auth_token_factory,
                        resolve_launch_config=lambda: _resolve_session_claude_launch_config(
                            session_id
                        ),
                        record_launch_config=_record_session_claude_launch_config,
                    )

                _launch_pre = _claude_pre_launch
                _launch_build = _claude_build_context

            elif harness_name == "codex-native":

                async def _codex_pre_launch(has_terminal: bool) -> PreLaunchResult:
                    needs = (
                        init_context.envelope is not None
                        or await _codex_session_needs_runner_terminal(server_client, session_id)
                    )
                    if not has_terminal:
                        inbound = await _codex_native_terminal_arrives_via_transfer(
                            server_client=server_client,
                            session_id=session_id,
                            resource_registry=resource_registry,
                            session_labels=init_context.labels,
                        )
                        _logger.info(
                            "Codex terminal transfer-inbound check: session=%s "
                            "terminal_inbound=%s",
                            session_id,
                            inbound,
                            extra={"session_id": session_id},
                        )
                        if inbound:
                            return PreLaunchResult(skip=True)
                    if not needs and not has_terminal:
                        _logger.info(
                            "Skipping codex terminal auto-create for %s; session "
                            "snapshot was not available.",
                            session_id,
                        )
                    return PreLaunchResult(needs_terminal=needs)

                async def _codex_build_context(ctx: NativeLaunchContext) -> NativeLaunchContext:
                    bundle_dir: Path | None = None
                    skills_filter: str | list[str] = "all"
                    try:
                        spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        spec = None
                    if spec is not None:
                        entry = _session_spec_cache.get(session_id)
                        bundle_dir = _resolved_spec_workdir(entry) if entry is not None else None
                        skills_filter = getattr(spec, "skills_filter", "all")
                    if bundle_dir is not None and spec is not None:
                        _ensure_orchestrator_skills_in_bundle(bundle_dir, spec)
                    # Preserve the inline arm's use of the outer spec_entry (not the
                    # locally-resolved spec) as agent_spec.
                    return dataclasses.replace(
                        ctx,
                        bundle_dir=bundle_dir,
                        skills_filter=skills_filter,
                        agent_spec=spec_entry,
                        session_init=init_context.envelope,
                    )

                _launch_pre = _codex_pre_launch
                _launch_build = _codex_build_context

            elif harness_name == "antigravity-native":

                async def _antigravity_pre_launch(has_terminal: bool) -> PreLaunchResult:
                    needs = (
                        await _session_payload_for_host_spawn_check(server_client, session_id)
                    ) is not None
                    if not has_terminal:
                        inbound = await _antigravity_native_terminal_arrives_via_transfer(
                            server_client=server_client,
                            session_id=session_id,
                            resource_registry=resource_registry,
                        )
                        _logger.info(
                            "Antigravity terminal transfer-inbound check: session=%s "
                            "terminal_inbound=%s",
                            session_id,
                            inbound,
                            extra={"session_id": session_id},
                        )
                        if inbound:
                            return PreLaunchResult(skip=True)
                    if not needs:
                        _logger.info(
                            "Skipping antigravity terminal auto-create for %s; session "
                            "snapshot was not available.",
                            session_id,
                        )
                    return PreLaunchResult(needs_terminal=needs)

                _launch_pre = _antigravity_pre_launch

            elif harness_name == "pi-native":
                # pi resolves its spec unwrapped — a resolution error surfaces as
                # a terminal-start error (the resolver does not swallow it).
                _launch_resolve_spec = lambda: _resolve_session_agent_spec(session_id)  # noqa: E731
            elif harness_name in (
                "cursor-native",
                "opencode-native",
                "kimi-native",
                "devin-native",
            ):
                _launch_resolve_spec = lambda: _resolve_session_agent_spec_or_none(  # noqa: E731
                    session_id
                )

            _launch_result = await _launch_native_terminal(
                harness_name,
                _launch_ctx,
                ensure_locks=_launch_locks,
                pre_launch=_launch_pre,
                build_context=_launch_build,
                resolve_agent_spec=_launch_resolve_spec,
            )
            # Only claude reported terminal_ready in the create-session response.
            if harness_name == "claude-native":
                terminal_ready = _launch_result

                # Start the loopback relay now instead of at the first
                # web-dispatched turn: a prompt typed directly in the TUI
                # fires policy hooks immediately, and without the relay every
                # hook falls back to a Python spawn + WAN round trip. In the
                # background so session create doesn't wait on it — hooks
                # that beat it use that same fallback.
                async def _start_claude_relay_early() -> None:
                    try:
                        await _ensure_comment_relay_started(
                            session_id, session_labels=init_context.labels
                        )
                    except Exception:
                        _logger.exception(
                            "Failed to pre-start comment relay for %s",
                            session_id,
                            extra={"session_id": session_id},
                        )

                _relay_task = asyncio.create_task(
                    _start_claude_relay_early(),
                    name=f"claude-comment-relay:{session_id}",
                )
                _relay_task.add_done_callback(_background_tasks.discard)
                _background_tasks.add(_relay_task)

        if (
            spec is not None
            and not is_native_harness(harness_name)
            and not _sa_name
            and resource_registry.terminal_registry is not None
        ):
            _repl_lock = _repl_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _repl_lock:
                _tr = resource_registry.terminal_registry
                _has_repl_terminal = (
                    _tr.get(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
                    is not None
                )
                if not _has_repl_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        repl_agent_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        repl_agent_spec = None
                    try:
                        await _auto_create_repl_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            agent_spec=repl_agent_spec,
                        )
                    except Exception:
                        _logger.exception(
                            "Failed to auto-create omnigent REPL terminal for %s",
                            session_id,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        # Preserve the initialization contract: undrained child results are
        # recovered before POST /sessions returns. The scan no longer delays
        # native terminal registration because it ran concurrently above.
        await asyncio.shield(_subagent_recovery_task)

        # Crash recovery (Step 8.5 Scenario A): if the session
        # has existing history, check whether the last item
        # indicates an incomplete turn that needs restarting.
        # Native terminal transcripts are mirrored from the underlying
        # runtime — a trailing user item can be a real failed native turn —
        # so skip the history load (and its attachment downloads) entirely.
        #
        # Skip the recovery-turn check when the server set
        # suppress_recovery_turn=True in the init envelope.  That flag means
        # the server is about to forward the triggering message immediately
        # after this handshake completes.  If the message was already
        # persisted to DB before the init call (invariant I1), the history
        # load would see it and start a redundant recovery turn; the
        # subsequent forward would then find _active_turns occupied, buffer
        # the message, and re-process it once the recovery turn finishes —
        # causing the first message to be silently ignored (sandbox/lakebox
        # wake) or processed twice (managed relaunch).
        _suppress_recovery = (
            init_context.envelope is not None and init_context.envelope.suppress_recovery_turn
        )
        recovery_id = (
            init_context.envelope.recovery_id
            if init_context.envelope is not None
            and init_context.envelope.resume_interrupted_turn
            and not _suppress_recovery
            else None
        )
        history: list[_JsonObject]
        if is_native_harness(harness_name):
            await _seed_last_server_item_id(session_id)
            history = []
        else:
            history = await _load_history_as_input(session_id)
        execution_seen = (
            initially_active
            or _turn_bind_epoch.get(session_id) != initial_turn_epoch
            or resource_registry.session_activity_epoch(session_id) != initial_native_activity
        )
        recovery_turn = "none"
        if history and not execution_seen and session_id not in _active_turns:
            _session_histories[session_id] = history
            last = history[-1]
            last_type = last.get("type")
            last_role = last.get("role")
            needs_turn = (
                (last_type == "message" and last_role == "user")
                or last_type == "function_call"
                or last_type == "function_call_output"
            )
            if (
                needs_turn
                and recovery_id is None
                and not _suppress_recovery
                and session_id not in _active_turns
            ):
                recovery_turn = "history_resume"
                _begin_turn_slot(session_id)
                _publish_turn_status(session_id, "running")
                msg_body = {
                    "agent_id": agent_id,
                    "model": body.get("model", agent_id),
                    # Recovery has no live server dispatch carrying renderer state.
                    "browser_renderer_available": False,
                }
                _turn_task = asyncio.create_task(
                    _run_turn_bg(msg_body, session_id),
                    name=f"turn-recover-{session_id}",
                )
                _active_turns[session_id] = _turn_task
                _turn_task.add_done_callback(
                    _background_tasks.discard,
                )
                _background_tasks.add(_turn_task)

        if recovery_id is not None and recovery_id not in _recovery_turn_ids.get(
            session_id, set()
        ):
            # Active execution, including a newer message, takes precedence over
            # automatic continuation. Initialization alone cannot consume it.
            if (
                not execution_seen
                and session_id not in _active_turns
                and not resource_registry.session_turn_is_active(session_id)
            ):
                recovery_turn = "recovery_prompt"
                if is_native_harness(harness_name):
                    _session_histories[session_id] = []
                _begin_turn_slot(session_id)
                _publish_turn_status(session_id, "running")
                recovery_body: _JsonObject = {
                    "agent_id": agent_id,
                    "model": body.get("model", agent_id),
                    "browser_renderer_available": False,
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Your runner was interrupted while this task was active. "
                                "Continue the existing task from its current state. "
                                "Check any interrupted operation's outcome before repeating it."
                            ),
                        }
                    ],
                }
                if not is_native_harness(harness_name):
                    _session_histories.setdefault(session_id, []).append(
                        {"type": "message", "role": "user", "content": recovery_body["content"]}
                    )
                recovery_task = asyncio.create_task(
                    _run_turn_bg(recovery_body, session_id), name=f"turn-recover-{session_id}"
                )
                _active_turns[session_id] = recovery_task
                recovery_task.add_done_callback(_background_tasks.discard)
                _background_tasks.add(recovery_task)
            _recovery_turn_ids.setdefault(session_id, set()).add(recovery_id)

        status = "running" if session_id in _active_turns else "idle"
        # The recovery decision and its inputs, so a turn that restarted after a
        # reconnect can be attributed to the history heuristic, the server's
        # continuation request, or neither.
        _logger.info(
            "Runner session initialization finished",
            extra=debug_event(
                "runner_session_initialized",
                session_id=session_id,
                stage="session_init",
                status_code=201,
                harness=harness_name,
                status=status,
                recovery_turn=recovery_turn,
                recovery_id=recovery_id,
                resume_interrupted_turn=(
                    init_context.envelope is not None
                    and init_context.envelope.resume_interrupted_turn
                ),
                suppress_recovery_turn=_suppress_recovery,
                execution_seen=execution_seen,
                history_len=len(history),
                last_item_type=history[-1].get("type") if history else None,
            ),
        )
        return JSONResponse(
            status_code=201,
            content={
                "id": session_id,
                "agent_id": agent_id,
                "status": status,
                "created_at": int(_session_start_cache[session_id]),
                "title": None,
                "labels": {},
                "runner_id": None,
                "reasoning_effort": None,
                "items": [],
                "permission_level": None,
                "session_init_protocol_version": (
                    init_context.envelope.protocol_version
                    if init_context.envelope is not None
                    else None
                ),
                "terminal_ready": terminal_ready,
                **(
                    {"inference_config_verified": True}
                    if init_context.envelope is not None
                    and init_context.envelope.snapshot.inference_config is not None
                    else {}
                ),
            },
        )

    @app.post("/v1/sessions")
    async def create_session(request: Request) -> JSONResponse:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Session initialization body must be a JSON object.",
                },
            )
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        if not isinstance(session_id, str) or not isinstance(agent_id, str):
            return await _initialize_session(body)
        sub_agent_name = body.get("sub_agent_name")
        try:
            envelope = parse_runner_session_init_envelope(body)
        except ValueError:
            return await _initialize_session(body)
        key = (
            session_id,
            agent_id,
            sub_agent_name if isinstance(sub_agent_name, str) else None,
            envelope.recovery_id if envelope is not None else None,
        )
        task = _session_init_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                _initialize_session(body),
                name=f"session-init-{session_id}",
            )
            _session_init_tasks[key] = task

            def _drop_completed_init(done: asyncio.Task[JSONResponse]) -> None:
                if _session_init_tasks.get(key) is done:
                    _session_init_tasks.pop(key, None)

            task.add_done_callback(_drop_completed_init)
        response = await asyncio.shield(task)
        return JSONResponse(
            status_code=response.status_code,
            content=json.loads(bytes(response.body)),
        )

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream_session(session_id: str) -> StreamingResponse:
        async def _event_generator() -> AsyncIterator[bytes]:
            queue = _session_event_queues.get(session_id)
            if queue is None:
                queue = asyncio.Queue()
                _session_event_queues[session_id] = queue
            heartbeat_frame = b'data: {"type": "session.heartbeat"}\n\n'
            yield heartbeat_frame
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SESSION_STREAM_HEARTBEAT_S
                    )
                except asyncio.TimeoutError:
                    yield heartbeat_frame
                    continue
                if event is None:
                    break
                frame = "data: " + json.dumps(event) + "\n\n"
                try:
                    yield frame.encode("utf-8")
                except (GeneratorExit, asyncio.CancelledError):
                    queue.put_nowait(event)
                    return
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> JSONResponse:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner GET /v1/sessions/{id} needs a HarnessProcessManager."),
                },
            )
        # A live session's subprocess can be legitimately unregistered — a
        # fence-fired release after init raced a reset, or an agent-switch
        # teardown awaiting its next-turn respawn — so a missing entry alone
        # is not "no such session": the start cache tracks every session this
        # runner initialized until it is deleted.
        if not process_manager.has_session(session_id) and session_id not in _session_start_cache:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "detail": (f"No session '{session_id}' on this runner."),
                },
            )
        has_turn = session_id in _active_turns or process_manager.has_active_turn(session_id)
        status = "running" if has_turn else "idle"
        # A failed setup has no active turn; retain its failure in server status probes.
        if not has_turn and _native_pane_status.get(session_id) == "failed":
            status = "failed"
        agent_id = _session_agent_ids.get(session_id)
        if agent_id is None:
            # An agent-cache reset retires the binding while the session
            # stays live, so a registered session can transiently lack it.
            # The server snapshot is the authoritative binding; serve it
            # rather than failing the read (the next turn re-memoizes).
            snapshot = await _session_snapshot(session_id)
            if snapshot.ok and snapshot.agent_id is not None:
                _logger.info(
                    "session agent binding absent from cache; serving the server snapshot binding",
                    extra={"session_id": session_id},
                )
                agent_id = snapshot.agent_id
        if agent_id is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": (
                        f"Session '{session_id}' registered but agent_id missing from cache."
                    ),
                },
            )
        created_at = _session_start_cache.get(session_id)
        if created_at is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": (
                        f"Session '{session_id}' registered but start_time missing from cache."
                    ),
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": session_id,
                "agent_id": agent_id,
                "status": status,
                "created_at": int(created_at),
                "title": None,
                "labels": {},
                "runner_id": None,
                "reasoning_effort": None,
                "items": [],
                "permission_level": None,
            },
        )

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> JSONResponse:
        resource_registry.note_terminal_control_request(session_id, "delete_session")
        _cancel_claude_prompt_waiter(session_id)
        _session_message_buffers.pop(session_id, None)
        # Stop initialization before it can recreate resources during teardown.
        init_tasks = [
            task
            for key, task in list(_session_init_tasks.items())
            if key[0] == session_id and not task.done()
        ]
        for init_task in init_tasks:
            init_task.cancel()
        if init_tasks:
            # Bound cleanup time; log init failures so resource teardown still runs.
            _finished, pending = await asyncio.wait(
                set(init_tasks), timeout=_SESSION_INIT_CANCEL_TIMEOUT_S
            )
            if pending:
                _logger.warning(
                    "Cancelled session init for %s did not finish within %.0fs",
                    session_id,
                    _SESSION_INIT_CANCEL_TIMEOUT_S,
                )
            for done_task in _finished:
                if not done_task.cancelled() and done_task.exception() is not None:
                    _logger.warning(
                        "Session init for %s failed while being cancelled: %r",
                        session_id,
                        done_task.exception(),
                    )
        await _cancel_subagent_recovery(session_id)
        turn_task = _active_turns.pop(session_id, None)
        if turn_task is not None and isinstance(turn_task, asyncio.Task):
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn_task
        await mcp_execution_registry.cancel_session(session_id)
        _session_message_buffers.pop(session_id, None)
        _live_response_id.pop(session_id, None)
        # Clear all desync/turn state so a recreated same-id session starts clean.
        _turn_bind_epoch.pop(session_id, None)
        _recovery_turn_ids.pop(session_id, None)
        _desync_terminalized.pop(session_id, None)
        _desynced_sessions.discard(session_id)
        _required_terminal_exit_errors.pop(session_id, None)
        _native_pane_status.pop(session_id, None)
        _native_interrupt_runner.clear_pending_interrupt(session_id)
        _ingest_next_seq.pop(session_id, None)
        _ingest_now_serving.pop(session_id, None)
        _ingest_cond.pop(session_id, None)
        _codex_terminal_ensure_locks.pop(session_id, None)
        _claude_terminal_ensure_locks.pop(session_id, None)
        _pi_terminal_ensure_locks.pop(session_id, None)
        _cursor_terminal_ensure_locks.pop(session_id, None)
        _kiro_terminal_ensure_locks.pop(session_id, None)
        _antigravity_terminal_ensure_locks.pop(session_id, None)
        _goose_terminal_ensure_locks.pop(session_id, None)
        _qwen_terminal_ensure_locks.pop(session_id, None)
        _kimi_terminal_ensure_locks.pop(session_id, None)
        _hermes_terminal_ensure_locks.pop(session_id, None)
        _repl_terminal_ensure_locks.pop(session_id, None)
        _interrupted_sessions.discard(session_id)
        await _cancel_auto_forwarder_task(session_id)
        # Close any OpenCode server that no forwarder adopted.
        await _native_runtime.teardown_opencode_native_server(session_id)

        if process_manager is not None:
            await process_manager.forward_cancel(session_id)

        queue = _session_event_queues.get(session_id)
        if queue is not None:
            queue.put_nowait(None)

        if process_manager is not None:
            await process_manager.release(session_id)

        await resource_registry.cleanup_session(session_id)

        await _delete_native_bridge_dirs(
            server_client=server_client,
            session_id=session_id,
        )

        # The SDK harnesses' router is started here (not by a terminal launch
        # path), so this is its only teardown: without it the session leaks an
        # HTTP server, its thread, and a live bearer token on disk.
        from omnigent.runner.subagent_routing import shutdown_session_router

        await asyncio.to_thread(shutdown_session_router, session_id)
        forget_session_routing_class(session_id)

        from omnigent.runner.tool_dispatch import forget_spawn_family

        forget_spawn_family(session_id)

        # Increment before clearing caches so in-flight fills see a changed
        # generation and discard rather than repopulating entries for a dead
        # (or reborn) session.
        _session_cache_generations[session_id] = _session_cache_generations.get(session_id, 0) + 1

        _session_spec_cache.pop(session_id, None)
        _session_harness_overrides.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_cursor_model_names.pop(session_id, None)
        _drop_session_claude_launch_config(session_id)
        _session_start_cache.pop(session_id, None)
        _session_workspace_cache.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        _session_snapshot_locks.pop(session_id, None)
        _session_init_envelopes.pop(session_id, None)
        _session_reasoning_effort.pop(session_id, None)
        _session_spec_locks.pop(session_id, None)
        _session_fs_registries.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        _instruction_delivery_warned.pop(session_id, None)
        _session_sub_agent_resolved.pop(session_id, None)
        if _binding := _session_comment_relays.pop(session_id, None):
            _binding.relay.close()
        _session_histories.pop(session_id, None)
        _last_server_item_id.pop(session_id, None)
        _session_event_queues.pop(session_id, None)
        _session_inboxes.pop(session_id, None)
        _subagent_recovery_done.discard(session_id)
        _subagent_recovery_locks.pop(session_id, None)
        _subagent_wake_pending.discard(session_id)
        _stranded_wake_parents.discard(session_id)
        _last_rewake_notice.pop(session_id, None)
        _session_sub_agent_names.pop(session_id, None)
        unregister_child_session(session_id)
        unregister_subagent_work_for_session(session_id)
        if filesystem_registry is not None:
            filesystem_registry.unregister_conversation(session_id)
        for _task, evt in _session_async_tasks.pop(session_id, {}).values():
            evt.set()
        for _tmr in _session_timers.pop(session_id, {}).values():
            _tmr.cancel()
        _version_cache.pop(session_id, None)
        stale_resp_ids = [rid for rid, cid in _resp_to_conv.items() if cid == session_id]
        for rid in stale_resp_ids:
            _resp_to_conv.pop(rid, None)

        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.deleted",
                "deleted": True,
            },
        )

    async def _persist_cancellation_items(
        conv_id: str,
        items: list[_JsonObject],
    ) -> None:
        import uuid as _uuid

        response_id = f"cancel_{_uuid.uuid4().hex}"
        for item in items:
            item_type = item.get("type", "message")
            item_data = {k: v for k, v in item.items() if k != "type"}
            try:
                await server_client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": item_type,
                            "item_data": item_data,
                            "response_id": response_id,
                        },
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Failed to persist cancellation item for %s: %s",
                    conv_id,
                    item_type,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )

    def _note_session_harness_override(conv_id: str, harness_override: str | None) -> None:
        """Record the harness a session was forwarded, so reads match the run.

        A routed child arrives with ``harness_override`` naming a harness its
        (sub-)agent spec never declared — Smart Routing picks it on the first
        message. The ``"auto"`` sentinel is not a harness, so it is ignored.

        :param conv_id: Session/conversation id, e.g. ``"conv_child456"``.
        :param harness_override: The forwarded override, e.g. ``"codex"``.
        :returns: None.
        """
        if not harness_override or harness_override == "auto":
            return
        _session_harness_overrides[conv_id] = harness_override

    def _session_harness_name(conv_id: str) -> str | None:
        # The override wins: a routed session runs the harness the server
        # pinned, not the one its spec declares. Reading the spec here left a
        # sub-agent whose spec says ``claude-native`` looking native while it
        # actually ran ``claude-sdk``, so its completion was never pushed to
        # the parent inbox (the native path that owes it never ran).
        override = _session_harness_overrides.get(conv_id)
        if override is not None:
            return canonicalize_harness(override) or override
        spec = _session_spec_cache.get(conv_id)
        if spec is None:
            return None
        h = spec.executor.config.get("harness") or spec.executor.type
        return canonicalize_harness(h) or h

    def _native_turn_outcome_is_forwarder_confirmed(conv_id: str) -> bool:
        """Whether this session's forwarder distinguishes finished from aborted turns."""
        agent = native_coding_agent_for_harness(_session_harness_name(conv_id))
        return agent is not None and agent.key in _TURN_OUTCOME_CONFIRMING_NATIVE_AGENTS

    def _publish_turn_status(
        conv_id: str,
        status: str,
        error: Mapping[str, object] | None = None,
        *,
        source_error: Mapping[str, object] | None = None,
        response_id: str | None = None,
    ) -> None:
        if status == "waiting" and not (
            _server_version is not None and _version_supports_waiting_status(_server_version)
        ):
            status = "running"
        harness = _session_harness_name(conv_id)
        if status != "failed" and harness in {
            "claude-native",
            "pi-native",
            "cursor-native",
            "kiro-native",
            "goose-native",
            "qwen-native",
            "kimi-native",
            "hermes-native",
        }:
            return
        if status == "idle" and harness in {"codex-native", "antigravity-native"}:
            return
        event: _JsonObject = {"type": "session.status", "status": status}
        if error is not None:
            event["error"] = error
        if response_id is not None:
            # Name the turn this edge closes. The web UI already rendered the
            # response's own terminal error; the id lets it recognise this edge
            # as the same failure instead of adding a second card.
            event["response_id"] = response_id
        if status == "failed":
            source = source_error if source_error is not None else (error or {})
            dimensions: dict[str, str] = {}
            for key in ("code", "type", "status"):
                value = source.get(key)
                if (isinstance(value, int) and not isinstance(value, bool)) or (
                    isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,128}", value)
                ):
                    dimensions[f"source_{key}"] = str(value)
            # Canonical broken-turn signal: every failed turn shown in the UI
            # funnels through here, so log once at ERROR for the dashboard.
            _logger.error(
                "turn surfaced to UI as failed for %s (harness=%s): %s",
                conv_id,
                harness,
                error,
                extra=debug_event(
                    "runner_turn_failed",
                    session_id=conv_id,
                    harness=harness,
                    **dimensions,
                ),
            )
        _publish_event(conv_id, event)

    def _is_native_harness(conv_id: str) -> bool:
        return is_native_harness(_session_harness_name(conv_id))

    async def _codex_native_bridge_state_for_session(
        conv_id: str,
        *,
        action: str,
        missing_state_log_level: int = logging.WARNING,
    ) -> CodexNativeBridgeState | None:
        from omnigent.harnesses.codex_native.bridge import (
            CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )

        labels = await _session_labels_for_runner_spawn(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or conv_id
        state = read_bridge_state(bridge_dir_for_bridge_id(bridge_id))
        if state is None:
            _logger.log(
                missing_state_log_level,
                "Codex-native %s skipped for %s: no bridge state.",
                action,
                conv_id,
            )
            return None
        if state.session_id != conv_id:
            _logger.warning(
                "Codex-native %s skipped for %s: bridge belongs to %s.",
                action,
                conv_id,
                state.session_id,
            )
            return None
        return state

    async def _codex_native_bridge_dir_for_session(conv_id: str) -> Path:
        """
        Bridge directory for a codex-native session.

        Same resolution as :func:`_codex_native_bridge_state_for_session` — the
        bridge id label when present, else the conversation id — so a request
        written here lands in the directory the forwarder polls.

        :param conv_id: Conversation id, e.g. ``"conv_abc123"``.
        :returns: The session's bridge directory.
        """
        from omnigent.harnesses.codex_native.bridge import (
            CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
            bridge_dir_for_bridge_id,
        )

        labels = await _session_labels_for_runner_spawn(
            server_client=server_client,
            session_id=conv_id,
        )
        return bridge_dir_for_bridge_id(labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or conv_id)

    codex_goal_runner = CodexGoalRunner(
        bridge_state_for_session=_codex_native_bridge_state_for_session,
        client_safe_error_detail=_client_safe_error_detail,
        logger=_logger,
    )

    async def _native_cost_popup_config_file(conv_id: str, harness: str) -> Path:
        from omnigent.cli_auth import databricks_request_headers
        from omnigent.harnesses.opencode_native.bridge import write_cost_popup_config
        from omnigent.runner._entry import _make_auth_token_factory

        if harness == "claude-native":
            from omnigent.harnesses.claude_native import bridge as _cnb

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client, session_id=conv_id
            )
            bridge_dir = _cnb.bridge_dir_for_bridge_id(bridge_id)
        elif harness == "opencode-native":
            from omnigent.harnesses.opencode_native.bridge import (
                bridge_dir_for_bridge_id as _oc_bridge_dir,
            )

            bridge_dir = _oc_bridge_dir(conv_id)
        else:  # codex-native
            from omnigent.harnesses.codex_native import bridge as _cxb

            bridge_dir = _cxb.bridge_dir_for_bridge_id(conv_id)

        _server_url = _required_runner_env("RUNNER_SERVER_URL")
        _factory = _make_auth_token_factory()
        _token = _factory() if _factory is not None else None
        return await asyncio.to_thread(
            write_cost_popup_config,
            bridge_dir,
            ap_server_url=_server_url,
            ap_auth_headers=databricks_request_headers(_server_url, bearer_token=_token),
        )

    async def _repop_pending_cost_popup_on_attach(
        conv_id: str,
        socket_path: str,
        tmux_target: str,
    ) -> None:
        harness = _session_harness_name(conv_id)
        if harness not in ("claude-native", "codex-native", "opencode-native"):
            return
        from omnigent.inner.databricks_executor import DatabricksAuthError
        from omnigent.native.native_cost_popup import launch_cost_popup, wait_for_tmux_client

        attached = await asyncio.to_thread(
            wait_for_tmux_client, socket_path, tmux_target, timeout_s=5.0
        )
        if not attached:
            return
        try:
            resp = await server_client.get(
                f"/v1/sessions/{conv_id}", params=_SESSION_METADATA_PARAMS, timeout=10.0
            )
        except httpx.HTTPError:
            return
        except DatabricksAuthError as exc:
            # Best-effort: the host credential service may be unable to sign the request.
            _logger.warning("Skipping cost popup repopulation for %s: %s", conv_id, exc)
            return
        if resp.status_code != 200:
            return
        pending = resp.json().get("pending_elicitations") or []
        approval = next(
            (
                e
                for e in pending
                if isinstance(e, dict)
                and isinstance(e.get("params"), dict)
                and e["params"].get("phase") in ("request", "tool_call", "llm_request")
            ),
            None,
        )
        if approval is None:
            return
        elicitation_id = approval.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            return
        message = approval["params"].get("message") or "Approval required"
        policy_name = approval["params"].get("policy_name")
        config_file = await _native_cost_popup_config_file(conv_id, harness)
        await asyncio.to_thread(
            launch_cost_popup,
            socket_path,
            tmux_target,
            config_file,
            session_id=conv_id,
            elicitation_id=elicitation_id,
            message=message,
            policy_name=policy_name if isinstance(policy_name, str) and policy_name else None,
        )

    _session_history = build_session_history(
        _background_tasks=_background_tasks,
        _last_server_item_id=_last_server_item_id,
        _persist_cancellation_items=_persist_cancellation_items,
        _session_histories=_session_histories,
        _session_spec_cache=_session_spec_cache,
        server_client=server_client,
    )
    _append_cancellation_items = _session_history.append_cancellation_items
    _convert_raw_items_to_input = _session_history.convert_raw_items_to_input
    _extract_last_assistant_text = _session_history.extract_last_assistant_text
    _handle_harness_compaction = _session_history.handle_harness_compaction
    _load_history_as_input = _session_history.load_history_as_input
    _seed_last_server_item_id = _session_history.seed_last_server_item_id

    _sign_in_watch = build_sign_in_watch(
        _background_tasks=_background_tasks,
        _session_harness_name=_session_harness_name,
        _sign_in_watchers=_sign_in_watchers,
        resource_registry=resource_registry,
        server_client=server_client,
    )
    _native_pane_names = _sign_in_watch.native_pane_names
    _start_sign_in_watch = _sign_in_watch.start_sign_in_watch

    def _begin_turn_slot(conv_id: str) -> None:
        """Bind the ``None`` sentinel and stamp a fresh epoch for the new turn.

        Must be used instead of a bare ``_active_turns[conv] = None`` so recovery can detect
        a replacement turn that ran and finished during a teardown await.
        """
        _active_turns[conv_id] = None
        _turn_bind_epoch[conv_id] = next(_turn_epoch_seq)

    def _release_live_turn_markers(conv_id: str) -> None:
        """Clear ``_live_response_id`` and the process-manager in-flight marker atomically.

        The two stores represent one fact; clearing only one leaves the idle reaper stuck.
        """
        _live_response_id.pop(conv_id, None)
        if process_manager is not None:
            process_manager.clear_in_flight(conv_id)

    def _sweep_dead_turn_slot(conv_id: str, occupant: asyncio.Task[None] | None) -> bool:
        """Remove a completed turn and all its per-turn tokens together (identity-guarded).

        Returns ``True`` if swept, ``False`` if a newer turn already owns the slot.
        """
        if _active_turns.get(conv_id) is not occupant:
            return False
        _active_turns.pop(conv_id, None)
        _release_live_turn_markers(conv_id)
        _interrupted_sessions.discard(conv_id)
        return True

    def _on_proxy_stream_end(
        conv_id: str,
        *,
        error: dict[str, Any] | None = None,
        owner_response_id: str | None = None,
    ) -> None:
        # Stale-finalizer guard: when owner_response_id no longer matches the live response,
        # a newer turn has taken over — skip all conversation-state mutations.
        if owner_response_id is not None and _live_response_id.get(conv_id) != owner_response_id:
            _logger.debug(
                "proxy stream end for %s ignored: response %s superseded by %s",
                conv_id,
                owner_response_id,
                _live_response_id.get(conv_id),
                extra={"session_id": conv_id},
            )
            return

        _active_turns.pop(conv_id, None)
        _release_live_turn_markers(conv_id)
        # A claude-sdk `/compact` turn that ended without emitting
        # `response.compaction.completed` produced no compaction (nothing to
        # compact, or the turn failed before/without streaming). Clear the
        # up-front spinner with `failed` here — the single turn-end convergence
        # point, reached on every exit path (clean end, setup error, cancel) — so
        # no path strands the spinner or leaks the flag into a later turn's
        # compaction signalling. A successful compaction already discarded the
        # flag on `response.compaction.completed`, making this a no-op then.
        if conv_id in _sdk_compact_inprogress:
            _sdk_compact_inprogress.discard(conv_id)
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
        # Transport-loss ending desyncs harness from runner; flag for clean rebind.
        if error is not None and error.get("code") == "connection_error":
            _desynced_sessions.add(conv_id)
        has_buffered = bool(_session_message_buffers.get(conv_id))
        was_interrupted = conv_id in _interrupted_sessions
        # Suppress terminal only if desync recovery claimed the token for THIS generation's epoch.
        _suppress_status = _desync_terminalized.get(conv_id) == _turn_bind_epoch.get(conv_id, 0)
        if _suppress_status:
            _desync_terminalized.pop(conv_id, None)
        if was_interrupted:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            if not has_buffered and not _suppress_status:
                _publish_turn_status(conv_id, "idle")
        elif error is not None:
            normalized_error = _normalize_turn_error(error)
            if not _suppress_status:
                _publish_turn_status(
                    conv_id,
                    "failed",
                    error=normalized_error,
                    source_error=error,
                    response_id=owner_response_id,
                )
            if normalized_error.get("code") == "databricks_sign_in_pending":
                _start_sign_in_watch(conv_id)
        else:
            if not has_buffered and not _suppress_status:
                children = _subagent_work_by_parent.get(conv_id, set())
                has_running_children = any(
                    (e := _subagent_work_by_child.get(c)) is not None
                    and e.status in ("launching", "running", "waiting")
                    for c in children
                )
                _publish_turn_status(conv_id, "waiting" if has_running_children else "idle")
        if was_interrupted:
            if conv_id in _desynced_sessions and not has_buffered:
                # This turn was torn down by desync recovery (which sets the
                # interrupt marker to unwind the harness), NOT by a user
                # interrupt — and it publishes a terminal desync ``failed``. Report
                # the sub-agent FAILED so the parent wake/result matches that
                # ``failed``, rather than a contradictory ``cancelled``.
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="failed",
                    output="Error: sub-agent turn failed: runner turn-context desync.",
                )
            else:
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="cancelled",
                    output=None,
                )
        elif error is not None:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="failed",
                output=f"Error: sub-agent turn failed: {error.get('message', 'unknown')}",
            )
        elif not _is_native_harness(conv_id) and not has_buffered:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="completed",
                output=_extract_last_assistant_text(conv_id),
            )
        elif _is_native_harness(conv_id):
            # A clean native turn end acknowledges prompt submission, so the
            # dispatch leaves ``launching`` even when no running edge is relayed.
            mark_subagent_work_started(conv_id)
        try:
            loop = asyncio.get_running_loop()
            _cont = loop.create_task(
                _check_and_start_next_turn(conv_id),
            )
            _cont.add_done_callback(_background_tasks.discard)
            _background_tasks.add(_cont)
        except RuntimeError:
            pass

    async def _cancel_active_turn(
        conv_id: str, expected_task: asyncio.Task[None] | None = None
    ) -> bool:
        turn_task = _active_turns.get(conv_id)
        if not isinstance(turn_task, asyncio.Task):
            return False
        if turn_task.done():
            # A completed generation left in the slot is a CORPSE (same class as
            # _cancel_inprocess_turn's done-task handling). This IS reachable: a
            # live task cancel-forwarded by _cancel_inprocess_turn can COMPLETE
            # during the intervening _forward_harness_interrupt await, arriving
            # here done — and it carries an _interrupted_sessions token that must
            # be cleared or it taints the next turn. Sweep it (tokens included),
            # honoring expected_task.
            if expected_task is None or turn_task is expected_task:
                _sweep_dead_turn_slot(conv_id, turn_task)
            return False
        if expected_task is not None and turn_task is not expected_task:
            return False
        turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn_task
        if _active_turns.get(conv_id) is turn_task:
            _on_proxy_stream_end(conv_id)
            return True
        if conv_id in _interrupted_sessions:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            # A turn torn down by desync recovery publishes a desync `failed`, so
            # its sub-agent must be reported FAILED, not a contradictory
            # `cancelled`, for the parent wake/result.
            if conv_id in _desynced_sessions:
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="failed",
                    output="Error: sub-agent turn failed: runner turn-context desync.",
                )
            else:
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="cancelled",
                    output=None,
                )
        return True

    async def _forward_harness_interrupt(conv_id: str) -> None:
        """Best-effort POST ``{"type":"interrupt"}`` to a conversation's harness.

        Releases the harness's parked policy/tool future so its ``run_turn``
        unwinds. A dead or wedged harness logs and is swallowed — the
        runner-side floor does not depend on this succeeding.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        if process_manager is None:
            return
        try:
            harness_client = await process_manager.get_client(conv_id, "any")
            await harness_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
                # Bounded under the Omnigent server's 5s stop deadline.
                timeout=3.0,
            )
        except NoLiveHarnessError:
            _logger.debug(
                "Interrupt forward skipped for %s: no live harness",
                conv_id,
                extra={"session_id": conv_id},
            )
        except Exception:  # noqa: BLE001 — best-effort: harness may have exited
            _logger.warning(
                "Interrupt forward to harness failed for %s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )

    async def _cancel_inprocess_turn(conv_id: str) -> None:
        # Distinguish "no live turn" (absent) from a stream-mode turn (present as
        # the None sentinel — driven by the AP request's consumption of
        # proxy_stream, so the runner owns no cancellable Task). Both a live Task
        # and the sentinel have a live harness turn parked on a future, so the
        # interrupt must be forwarded for either.
        if conv_id not in _active_turns:
            return
        target = _active_turns.get(conv_id)
        if isinstance(target, asyncio.Task) and target.done():
            # A done Task is a corpse, not a live turn. Leaving it wedges every
            # ``conv in _active_turns`` liveness check (the buffer gate would strand
            # later messages) — sweep it, tokens included.
            _sweep_dead_turn_slot(conv_id, target)
            return
        _interrupted_sessions.add(conv_id)
        await _forward_harness_interrupt(conv_id)
        # Floor: force-cancel the runner Task when we own one. In stream mode
        # there is no Task here — ``_resync_turn_state`` owns the sentinel pop,
        # and direct interrupt/stop callers rely on the forwarded interrupt
        # ending proxy_stream.
        if isinstance(target, asyncio.Task):
            await _cancel_active_turn(conv_id, expected_task=target)

    async def _resync_turn_state(
        conv_id: str, reason: str, *, owner_response_id: str | None = None
    ) -> None:
        """Single ordered recovery entry for a harness↔runner desync.

        Marks the conversation desynced, clears the stale live-response marker,
        tears the wedged turn down, and either drains a buffered continuation or
        publishes one terminal desync ``failed``. Cancelling the turn unwinds
        ``run_turn``, releasing the harness's parked policy future in
        milliseconds instead of at ``_POLICY_EVAL_TIMEOUT_S``.

        Idempotent: ``_cancel_inprocess_turn`` no-ops with no turn in flight and
        ``_interrupted_sessions`` is the existing idempotency token, so a
        duplicate signal for the same wedged turn collapses to one recovery.

        Generation-ownership gate: a desync signal names the turn that produced
        it (its ``owner_response_id``). A delayed or duplicate signal from an
        OLD response must not cancel whichever newer turn is now active, so when
        ``owner_response_id`` is supplied and no longer matches the live
        response, this is a stale signal — no-op. Signals with no owning response
        (e.g. a conversation-level path) pass ``None`` and always recover.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param reason: Short machine reason for the desync, logged for ops.
        :param owner_response_id: The response id the signal belongs to; when it
            no longer matches ``_live_response_id[conv_id]`` a newer turn has
            taken over and the signal is ignored.
        """
        if owner_response_id is not None and _live_response_id.get(conv_id) != owner_response_id:
            _logger.debug(
                "resync for %s ignored: response %s superseded by %s",
                conv_id,
                owner_response_id,
                _live_response_id.get(conv_id),
                extra={"session_id": conv_id},
            )
            return
        _logger.warning(
            "resyncing turn state for %s: %s",
            conv_id,
            reason,
            extra={"session_id": conv_id},
        )
        _desynced_sessions.add(conv_id)
        # Capture entry epoch; an advance during teardown means a replacement ran.
        _entry_epoch = _turn_bind_epoch.get(conv_id, 0)
        # Release markers before any await so concurrent callers see no live turn.
        _release_live_turn_markers(conv_id)
        # Pre-claim terminal token for this epoch; authoritative decision re-made after teardown.
        if not _session_message_buffers.get(conv_id):
            _desync_terminalized[conv_id] = _entry_epoch
        # Stream-sentinel turns have no Task — pop synchronously so the gate doesn't stay stuck.
        stream_sentinel = conv_id in _active_turns and not isinstance(
            _active_turns.get(conv_id), asyncio.Task
        )
        if stream_sentinel:
            _active_turns.pop(conv_id, None)
            await _forward_harness_interrupt(conv_id)
        else:
            await _cancel_inprocess_turn(conv_id)
        # Epoch advanced → a replacement ran (covers live-slot and empty-slot after teardown).
        _continuation_ran = _turn_bind_epoch.get(conv_id, 0) != _entry_epoch
        _has_buffer = bool(_session_message_buffers.get(conv_id))
        if not _continuation_ran and not _has_buffer:
            _publish_turn_status(
                conv_id,
                "failed",
                error={
                    "code": _RUNNER_TURN_CONTEXT_DESYNC_CODE,
                    "message": (
                        "The agent turn was interrupted by a harness desync and "
                        "could not be recovered. Please send your message again."
                    ),
                },
            )
        else:
            # A continuation owns the terminal status — release our claim (compare-and-pop
            # so a nested recovery's higher-epoch claim isn't accidentally stripped).
            if _desync_terminalized.get(conv_id) == _entry_epoch:
                _desync_terminalized.pop(conv_id, None)
            # Kick a continuation if none ran; a cancelled mid-drain turn doesn't schedule one.
            if _has_buffer and not _continuation_ran:
                try:
                    loop = asyncio.get_running_loop()
                    _cont = loop.create_task(_check_and_start_next_turn(conv_id))
                    _cont.add_done_callback(_background_tasks.discard)
                    _background_tasks.add(_cont)
                except RuntimeError:
                    pass

    def _recover_failed_tool_dispatch(
        dispatch_task: asyncio.Task[object], *, conv_id: str, response_id: str
    ) -> None:
        if dispatch_task.cancelled() or dispatch_task.exception() is None:
            return
        # Recovery can cancel a turn awaiting this dispatch task. Run it
        # independently so teardown cannot await or cancel itself.
        task = asyncio.create_task(
            _resync_turn_state(conv_id, "tool_dispatch_failed", owner_response_id=response_id)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _resync_turn_state_on_delivery_failure(
        conv_id: str, response_id: str | None
    ) -> None:
        """``on_delivery_failure`` adapter binding the desync reason + owner.

        Carries the response id of the turn whose verdict delivery failed so a
        delayed or duplicate failure from an old response cannot cancel a newer
        active turn (the ownership gate in :func:`_resync_turn_state`).

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param response_id: The response id of the turn whose verdict delivery
            failed, or ``None`` if the eval fired before ``response.created``.
        """
        await _resync_turn_state(
            conv_id, "verdict_delivery_channel_dead", owner_response_id=response_id
        )

    async def _resync_turn_state_on_harness_respawn(
        conv_id: str, reason: str, replaced_response_id: str
    ) -> None:
        """``HarnessProcessManager`` respawn-hook adapter for ``_resync_turn_state``.

        A respawn while the replaced response is still bound is the deterministic respawn-desync:
        the inner generation dies with the subprocess and the slot is never cleaned.
        Recovering here collapses the window instead of waiting for the orphan backstop.

        Two gates keep it from cancelling a HEALTHY turn:

        1. ``conv_id in _active_turns`` — a respawn with no bound turn is a no-op.
        2. ``_live_response_id[conv_id] == replaced_response_id`` — the process
           manager only fires when the replaced process was mid-response, but by
           the time this runs the wedged turn may already have ended and a NEW
           turn bound under the same conversation (whose ``get_client`` triggered
           the respawn). Cancelling on the bare conversation would then clobber
           that new turn. Identity-match the replaced response so only the turn
           that actually lost its subprocess is torn down; a mismatch means the
           new turn owns the slot and must be left alone.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param reason: Machine reason from the process manager, e.g.
            ``"harness_respawn_model_switch"``.
        :param replaced_response_id: The in-flight response id of the process
            that was torn down, for identity-matching the bound turn.
        """
        if conv_id not in _active_turns:
            return
        # The identity match against the replaced response is enforced centrally
        # by ``_resync_turn_state``'s ownership gate — a fresh turn that took the
        # slot has a different live response id and is left alone.
        await _resync_turn_state(conv_id, reason, owner_response_id=replaced_response_id)

    # Test seams: the real signals originate in a harness verdict POST failure
    # and inside the process manager's get_client, neither scriptable in-process.
    app.state.resync_turn_state = _resync_turn_state
    app.state.resync_turn_state_on_harness_respawn = _resync_turn_state_on_harness_respawn
    app.state.on_proxy_stream_end = _on_proxy_stream_end
    # Test seam: bind a turn slot with a fresh (non-repeating) bind epoch, so a
    # test can simulate a same-id recreate binding a new lifetime's turn.
    app.state.begin_turn_slot = _begin_turn_slot
    # hasattr guard: alternate/stub process managers need not implement the hook
    # — they simply fall back to the orphan-callback backstop.
    if process_manager is not None and hasattr(process_manager, "set_respawn_hook"):
        process_manager.set_respawn_hook(_resync_turn_state_on_harness_respawn)

    async def _claude_prompt_bridge_dir(session_id: str) -> Path:
        """Resolve the bridge label before inspecting a native prompt."""
        from omnigent.harnesses.claude_native.bridge import bridge_dir_for_bridge_id

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client, session_id=session_id
        )
        return bridge_dir_for_bridge_id(bridge_id)

    async def _pending_claude_prompt_bridge_dir(session_id: str) -> Path | None:
        """Return the bridge whose question or approval currently owns input."""
        if _session_harness_name(session_id) != "claude-native":
            return None
        from omnigent.harnesses.claude_native.bridge import has_pending_user_prompt

        bridge_dir = await _claude_prompt_bridge_dir(session_id)
        if await asyncio.to_thread(has_pending_user_prompt, bridge_dir):
            return bridge_dir
        return None

    def _cancel_claude_prompt_waiter(session_id: str) -> None:
        """Discard waiting input before explicit terminal interruption or teardown."""
        waiter = _claude_prompt_waiters.pop(session_id, None)
        if waiter is not None or _session_harness_name(session_id) == "claude-native":
            # Cancel queued work even if the terminal control fails; restoring it
            # could restart work the user explicitly asked to stop.
            _session_message_buffers.pop(session_id, None)
        if waiter is not None:
            waiter.cancel()

    def _start_claude_prompt_waiter(session_id: str, bridge_dir: Path | None = None) -> None:
        """Resume the native FIFO after its prompt is answered, without a turn timeout."""
        existing = _claude_prompt_waiters.get(session_id)
        if existing is not None and not existing.done():
            return

        async def _wait_for_prompt() -> None:
            from omnigent.harnesses.claude_native.bridge import has_pending_user_prompt

            try:
                resolved_dir = bridge_dir or await _claude_prompt_bridge_dir(session_id)
                while _session_message_buffers.get(session_id):
                    if session_id in _active_turns:
                        return
                    try:
                        pending = await asyncio.to_thread(has_pending_user_prompt, resolved_dir)
                    except (OSError, RuntimeError):
                        _logger.warning(
                            "Failed to inspect pending Claude prompt for %s; retrying",
                            session_id,
                            exc_info=True,
                            extra={"session_id": session_id},
                        )
                        pending = True
                    if not pending:
                        # A cancelled waiter must not abandon an ordered ingest ticket.
                        drain = asyncio.create_task(_check_and_start_next_turn(session_id))
                        _background_tasks.add(drain)
                        drain.add_done_callback(_background_tasks.discard)
                        await asyncio.shield(drain)
                        if session_id in _active_turns:
                            return
                    await asyncio.sleep(_CLAUDE_PENDING_PROMPT_POLL_S)
            finally:
                if _claude_prompt_waiters.get(session_id) is asyncio.current_task():
                    _claude_prompt_waiters.pop(session_id, None)

        waiter = asyncio.create_task(_wait_for_prompt(), name=f"claude-prompt-{session_id}")
        _claude_prompt_waiters[session_id] = waiter
        _background_tasks.add(waiter)
        waiter.add_done_callback(_background_tasks.discard)

    async def _check_and_start_next_turn(
        session_id: str,
    ) -> None:
        _seq = _ingest_next_seq.get(session_id, 0)
        _ingest_next_seq[session_id] = _seq + 1
        _cond = _ingest_cond.get(session_id)
        if _cond is None:
            _cond = asyncio.Condition()
            _ingest_cond[session_id] = _cond
        async with _cond:
            while _ingest_now_serving.get(session_id, 0) != _seq:
                await _cond.wait()
        try:
            if session_id in _active_turns:
                return

            buf = _session_message_buffers.get(session_id)
            if not buf:
                _rewake_parent_if_inbox_stranded(session_id)
                return

            pending_bridge_dir = await _pending_claude_prompt_bridge_dir(session_id)
            # Stop/delete can clear the queue while the pane inspection is in flight.
            buf = _session_message_buffers.get(session_id)
            if not buf:
                return
            if pending_bridge_dir is not None:
                _start_claude_prompt_waiter(session_id, pending_bridge_dir)
                return

            # A buffered claude-sdk /compact must dispatch as its OWN turn: the
            # SDK runs native compaction only when /compact is the turn prompt,
            # but the default non-native drain coalesces the whole buffer and
            # dispatches only the last body — burying a /compact behind a later
            # message and silently no-opping it (the runner already returned 200,
            # so the server won't fall back). Drain one at a time (like native)
            # whenever a /compact is buffered, so each lands as its own turn.
            if _is_native_harness(session_id) or any(_is_sdk_compact_body(b) for b in buf):
                next_body = buf.pop(0)
                if not buf:
                    _session_message_buffers.pop(session_id, None)
                _session_histories.setdefault(session_id, []).append(
                    {
                        "type": "message",
                        "role": next_body.get("role", "user"),
                        "content": next_body.get("content", []),
                    }
                )
            else:
                all_bodies = list(buf)
                buf.clear()
                _session_message_buffers.pop(session_id, None)

                for body in all_bodies:
                    _session_histories.setdefault(session_id, []).append(
                        {
                            "type": "message",
                            "role": body.get("role", "user"),
                            "content": body.get("content", []),
                        }
                    )
                next_body = all_bodies[-1]

            if _is_sdk_compact_body(next_body):
                # This buffered /compact now dispatches as its own turn. Mirror the
                # idle path: publish the spinner up front AND set the flag, so
                # (a) the relay swallows the executor's own duplicate in_progress
                # (single spinner), and (b) _on_proxy_stream_end publishes `failed`
                # if the turn ends without a `response.compaction.completed` (a real
                # executor path — the compaction-complete event can be None) rather
                # than stranding the spinner. The prior turn has ended, so showing
                # "Compacting…" now is accurate.
                _publish_event(
                    session_id,
                    {"type": "response.compaction.in_progress", "task_id": session_id},
                )
                _sdk_compact_inprogress.add(session_id)
            _begin_turn_slot(session_id)
            _publish_turn_status(session_id, "running")
            _turn_task = asyncio.create_task(
                _run_turn_bg(next_body, session_id),
                name=f"turn-cont-{session_id}",
            )
            _active_turns[session_id] = _turn_task
            _turn_task.add_done_callback(
                _background_tasks.discard,
            )
            _background_tasks.add(_turn_task)
        finally:
            async with _cond:
                _ingest_now_serving[session_id] = _seq + 1
                _cond.notify_all()

    app.state.check_and_start_next_turn = _check_and_start_next_turn

    async def _post_subagent_wake_notice(
        parent_id: str,
        notice: str,
        child_id: str,
        created_by: str | None,
        *,
        is_rewake: bool = False,
    ) -> None:
        delivered = await _deliver_subagent_wake_post(
            server_client, parent_id, notice, created_by=created_by
        )
        if delivered:
            _stranded_wake_parents.discard(parent_id)
            if is_rewake:
                _last_rewake_notice[parent_id] = notice
        else:
            _subagent_wake_pending.discard(parent_id)
            _stranded_wake_parents.add(parent_id)
            _logger.warning(
                "Sub-agent wake POST failed for parent=%s child=%s after %d attempt(s); "
                "result remains in the parent inbox; the wake will be re-attempted "
                "after the next tunnel reconnect or parent turn",
                parent_id,
                child_id,
                _WAKE_POST_MAX_ATTEMPTS,
                extra={"session_id": runner_primary_session_id()},
            )

    def _schedule_subagent_wake(entry: _SubagentWorkEntry, *, is_rewake: bool = False) -> None:
        if entry.parent_session_id == entry.child_session_id:
            return
        # A codex-native sub-agent (a /side side chat, or one codex spawned) is a
        # thread in the parent's own app-server, so its completion is not the
        # parent's to collect — waking the parent would inject an inbox notice
        # into a chat the user is reading. The wrapper label is only set by the
        # spawn-tool path, so a forwarder-registered child is caught by the
        # parent's harness instead.
        if is_codex_native_subagent_wrapper(entry.wrapper_label) or (
            _session_harness_name(entry.parent_session_id) == _CODEX_NATIVE_HARNESS
        ):
            return
        inbox = _session_inboxes.get(entry.parent_session_id)
        if inbox is None:
            return
        if entry.parent_session_id in _subagent_wake_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        notice = _format_subagent_wake_notice(
            agent=entry.agent,
            title=entry.title,
            status=entry.status,
            pending=inbox.qsize(),
        )
        if is_rewake and notice == _last_rewake_notice.get(entry.parent_session_id):
            return
        _subagent_wake_pending.add(entry.parent_session_id)
        _wake_task = loop.create_task(
            _post_subagent_wake_notice(
                entry.parent_session_id,
                notice,
                entry.child_session_id,
                entry.created_by,
                is_rewake=is_rewake,
            )
        )
        _wake_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_wake_task)

    def _rewake_parent_if_inbox_stranded(parent_session_id: str) -> None:
        inbox = _session_inboxes.get(parent_session_id)
        drained = inbox is None or inbox.empty()
        if drained:
            # A drained inbox ends the stranding episode, so the recorded
            # re-wake no longer describes outstanding work; forget it or a
            # later episode's matching notice is wrongly deduped.
            _last_rewake_notice.pop(parent_session_id, None)
            _stranded_wake_parents.discard(parent_session_id)
        # A parent whose wake POST exhausted its retries has no pending flag,
        # but its inbox still holds an undelivered result — rescue it too.
        stranded_retry = parent_session_id in _stranded_wake_parents
        if parent_session_id not in _subagent_wake_pending and not stranded_retry:
            return
        _subagent_wake_pending.discard(parent_session_id)
        _stranded_wake_parents.discard(parent_session_id)
        if drained:
            return
        entries = list_subagent_work(parent_session_id)
        if not entries:
            return
        latest = max(
            entries,
            key=lambda entry: entry.completed_at if entry.completed_at is not None else 0.0,
        )
        _schedule_subagent_wake(latest, is_rewake=True)

    async def _retry_stranded_wakes_soon() -> None:
        # Paced rounds: a failed re-attempt lands the parent back in
        # _stranded_wake_parents (see _post_subagent_wake_notice), so a later
        # round picks it up once the handshake has had more time. In-flight
        # attempts are deduped by _subagent_wake_pending. Recovery is bounded:
        # a parent still failing after the last round stays stranded until the
        # NEXT tunnel reconnect or an explicit parent turn re-attempts it.
        for delay_s in _STRANDED_WAKE_RETRY_DELAYS_S:
            await _subagent_work._wake_retry_sleep(delay_s)
            if not _stranded_wake_parents:
                return
            for parent_id in list(_stranded_wake_parents):
                _stranded_wake_parents.discard(parent_id)
                inbox = _session_inboxes.get(parent_id)
                if inbox is None or inbox.empty():
                    continue
                entries = list_subagent_work(parent_id)
                if not entries:
                    continue
                latest = max(
                    entries,
                    key=lambda entry: (
                        entry.completed_at if entry.completed_at is not None else 0.0
                    ),
                )
                _logger.info(
                    "Re-attempting stranded sub-agent wake for parent=%s after reconnect",
                    parent_id,
                    extra={"session_id": runner_primary_session_id()},
                )
                # Deliberately not is_rewake=True: skip the _last_rewake_notice
                # dedup so the notice always re-sends after a reconnect; the
                # drained-inbox check above prevents true duplicates.
                _schedule_subagent_wake(latest)

    def _retry_stranded_wakes() -> None:
        # A wake POST that exhausted its bounded retries (the server was down
        # when the child finished) left the result in the parent's inbox with
        # nothing scheduled to re-deliver it — the wake is the sole delivery
        # signal for an idle parent. The tunnel just reconnected, so the
        # server is reachable again: re-attempt one wake per stranded parent
        # whose inbox still holds results.
        if not _stranded_wake_parents:
            return
        if _stranded_wake_retry_task and not _stranded_wake_retry_task[0].done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _retry_task = loop.create_task(_retry_stranded_wakes_soon())
        _stranded_wake_retry_task[:] = [_retry_task]

        def _clear_retry_refs(task: asyncio.Task[None]) -> None:
            _background_tasks.discard(task)
            if _stranded_wake_retry_task and _stranded_wake_retry_task[0] is task:
                _stranded_wake_retry_task.clear()
            # Surface an unexpected failure instead of a GC-time warning;
            # recovery degrades to the next reconnect or explicit parent turn.
            if not task.cancelled() and task.exception() is not None:
                _logger.warning(
                    "Stranded sub-agent wake retry loop failed: %r",
                    task.exception(),
                    extra={"session_id": runner_primary_session_id()},
                )

        _retry_task.add_done_callback(_clear_retry_refs)
        _background_tasks.add(_retry_task)

    def _mark_subagent_terminal_and_wake(
        child_session_id: str,
        *,
        status: str,
        output: str | None,
        only_if_work_id: str | None = None,
    ) -> _SubagentDeliveryAck:
        ack = mark_subagent_work_terminal(
            child_session_id,
            status=status,
            output=output,
            only_if_work_id=only_if_work_id,
        )
        if ack.entry is not None and ack.delivered_now:
            _schedule_subagent_wake(ack.entry)
        return ack

    # Seam for the entrypoint's launch reaper (and tests): terminal delivery
    # that also schedules the parent wake POST, not just the inbox insert.
    app.state.mark_subagent_terminal_and_wake = _mark_subagent_terminal_and_wake

    _subagent_recovery = build_subagent_recovery(
        app,
        _background_tasks=_background_tasks,
        _schedule_subagent_wake=_schedule_subagent_wake,
        _session_inboxes=_session_inboxes,
        _session_snapshot=_session_snapshot,
        _session_sub_agent_names=_session_sub_agent_names,
        _subagent_recovery_tasks=_subagent_recovery_tasks,
        server_client=server_client,
    )
    _cancel_subagent_recovery = _subagent_recovery.cancel_subagent_recovery
    _deliver_retained_subagent_results = _subagent_recovery.deliver_retained_subagent_results
    _ensure_subagent_work_entry = _subagent_recovery.ensure_subagent_work_entry
    _parent_is_nested_subagent = _subagent_recovery.parent_is_nested_subagent
    _recover_sub_agent_name = _subagent_recovery.recover_sub_agent_name
    _recover_undrained_subagent_results = _subagent_recovery.recover_undrained_subagent_results
    _start_subagent_recovery = _subagent_recovery.start_subagent_recovery

    def _subagent_work_id_for_session(conv_id: str) -> str | None:
        entry = get_subagent_work(conv_id)
        return entry.work_id if entry is not None else None

    _native_interrupt_runner = NativeInterruptRunner(
        server_client=server_client,
        resource_registry=resource_registry,
        publish_event=_publish_event,
        mark_subagent_terminal_and_wake=_mark_subagent_terminal_and_wake,
        session_sub_agent_names=_session_sub_agent_names,
        codex_bridge_state_for_session=_codex_native_bridge_state_for_session,
        client_safe_error_detail=_client_safe_error_detail,
        logger=_logger,
        subagent_work_id_for_session=_subagent_work_id_for_session,
    )

    def _discard_comment_relay(session_id: str, relay: ClaudeNativeToolRelay) -> None:
        """Unbind and close *relay*, unless another path already replaced it.

        Removal is by relay instance rather than by session id: a
        replacement installed while the caller was working owns the entry
        and has already closed *relay*, so popping the key would tear down
        the newer relay instead of the intended one.

        :param session_id: Session/conversation id the relay was bound to.
        :param relay: The relay instance the caller installed.
        :returns: None.
        """
        binding = _session_comment_relays.get(session_id)
        if binding is None or binding.relay is not relay:
            return
        del _session_comment_relays[session_id]
        relay.close()

    async def _ensure_comment_relay_started(
        session_id: str,
        *,
        bridge_id: str | None = None,
        explicit_bridge_dir: Path | None = None,
        await_notify: bool = False,
        session_labels: Mapping[str, str] | None = None,
    ) -> None:
        import json as _json

        from omnigent.harnesses.claude_native.bridge import (
            BRIDGE_ID_LABEL_KEY,
            bridge_dir_for_bridge_id,
            post_tools_changed,
            start_tool_relay,
        )

        try:
            spec_entry = await _resolve_session_spec_entry(session_id)
        except OmnigentError:
            # Resolution failed; this is not the same as a session that
            # resolves to no spec. A relay already serving the session was
            # built from a real spec, so replacing it with the fallback
            # surface would withdraw spec-gated tools the agent does grant.
            # Keep it: once resolution recovers, the resolved spec differs
            # from the stored one and the relay rebuilds then.
            if session_id in _session_comment_relays:
                return
            spec_entry = None

        # The bridge dir, when the caller pinned it down or handed over the
        # labels it comes from. Deriving it any other way costs a server
        # round trip, which a session whose agent has not changed must not
        # pay on every turn.
        known_bridge_dir: Path | None = None
        if explicit_bridge_dir is not None:
            known_bridge_dir = explicit_bridge_dir
        elif bridge_id is not None:
            known_bridge_dir = bridge_dir_for_bridge_id(bridge_id or session_id)
        elif session_labels is not None:
            known_bridge_dir = bridge_dir_for_bridge_id(
                session_labels.get(BRIDGE_ID_LABEL_KEY) or session_id
            )

        # Same agent and no bridge hint to check against: skip the lookup that
        # would cost a server round trip. This rests on a bridge id only ever
        # being reassigned alongside the agent (a native-harness-family
        # switch), which the spec comparison already caught. The callers that
        # can reassign it independently — the terminal-launch and per-harness
        # startup paths — all pass a bridge hint and take the branch below.
        current = _session_comment_relays.get(session_id)
        if current is not None and current.spec_entry is spec_entry and known_bridge_dir is None:
            return

        bridge_dir = known_bridge_dir
        if bridge_dir is None:
            bridge_dir = bridge_dir_for_bridge_id(
                await _claude_native_bridge_id_with_optional_labels(
                    server_client=server_client,
                    session_id=session_id,
                    session_labels=session_labels,
                )
                or session_id
            )

        # Re-read after the awaits above: a concurrent caller may have
        # installed a relay that already matches the current agent.
        current = _session_comment_relays.get(session_id)
        if (
            current is not None
            and current.spec_entry is spec_entry
            and current.bridge_dir == bridge_dir
        ):
            return

        from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas

        relay_schemas: list[_JsonObject] = build_native_relay_tool_schemas(
            _unwrap_spec_entry(spec_entry)
        )

        _captured_session_id = session_id

        async def _relay_tool_executor(
            name: str,
            arguments: _JsonObject,
        ) -> _JsonObject:
            result_str = await ProxyMcpManager(
                _captured_session_id,
                server_client,
                publish_event=_publish_event,
                execution_registry=mcp_execution_registry,
            ).call_tool(None, name, arguments)
            try:
                return cast(_JsonObject, _json.loads(result_str))
            except _json.JSONDecodeError:
                return {"result": result_str}

        async def _observe_native_file_changes(payload: _JsonObject) -> None:
            """Record native file-mutating tool calls in the session's registry.

            Non-git workspaces track changes only through
            ``FilesystemRegistry.record_change``, which the runner's
            ``sys_os_write``/``sys_os_edit`` dispatch calls; a native harness
            writing through its own tools (Claude Code's ``Write``/``Edit``)
            never reaches it, so ``GET .../changes`` stayed empty. The relay's
            ``/hook/observe-tool`` delivers every PostToolUse event here so
            those writes are recorded too. Best-effort: failures are logged
            and never surface to the hook.

            :param payload: Hook JSON object from ``/hook/observe-tool``.
            """
            from omnigent.runner.native_file_observer import native_file_changes
            from omnigent.runner.tool_dispatch import _maybe_signal_changed_files

            try:
                changes = native_file_changes(payload)
                if not changes:
                    return
                registry = await _resolve_session_fs_registry(_captured_session_id)
                if registry is None:
                    return
                for change in changes:
                    if change.baseline is not None:
                        registry.seed_snapshot(
                            change.path,
                            change.baseline,
                            session_id=_captured_session_id,
                        )
                    registry.record_change(change.path, change.operation, _captured_session_id)
                _maybe_signal_changed_files(
                    _captured_session_id,
                    _publish_event,
                    now=asyncio.get_running_loop().time(),
                )
            except Exception:  # noqa: BLE001 — best-effort observer; never fail the hook
                _logger.warning(
                    "native file-change recording failed for session=%s",
                    _captured_session_id,
                    exc_info=True,
                    extra={"session_id": _captured_session_id},
                )

        try:
            relay: ClaudeNativeToolRelay = start_tool_relay(
                bridge_dir=bridge_dir,
                tools=relay_schemas,
                tool_executor=_relay_tool_executor,
                loop=asyncio.get_running_loop(),
                policy_client=server_client,
                session_id=session_id,
                file_change_observer=_observe_native_file_changes,
            )
        except (OSError, RuntimeError):
            _logger.warning(
                "Failed to start comment relay for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return
        superseded = _session_comment_relays.get(session_id)
        _session_comment_relays[session_id] = _CommentRelayBinding(
            relay=relay,
            spec_entry=spec_entry,
            bridge_dir=bridge_dir,
        )
        # Close last: the new advertisement is already written, and
        # ClaudeNativeToolRelay.close only unlinks a tool_relay.json that
        # still points at the relay being closed, so a shared bridge dir
        # keeps the new file.
        if superseded is not None:
            superseded.relay.close()

        async def _notify_tools_changed() -> None:
            from threading import Event

            cancelled = Event()
            try:
                await asyncio.to_thread(post_tools_changed, bridge_dir, cancelled=cancelled)
            except (RuntimeError, OSError):
                # Fire-and-forget below, so anything escaping here resurfaces as
                # an unretrieved task exception at ERROR. Re-advertising the tool
                # list is best-effort; a stale list costs one turn, a dead task
                # loop costs the session.
                _logger.debug(
                    "tools-changed notification skipped for session=%s (bridge server not ready)",
                    session_id,
                    exc_info=True,
                    extra={"session_id": session_id},
                )
            finally:
                # Cancelling an executor future does not stop its worker thread.
                cancelled.set()

        if await_notify:
            await _notify_tools_changed()
        else:
            _notify_task = asyncio.create_task(
                _notify_tools_changed(), name=f"tools-changed:{session_id}"
            )
            _background_tasks.add(_notify_task)
            _notify_task.add_done_callback(_background_tasks.discard)

    async def _run_turn_bg(
        msg_body: _JsonObject,
        conv: str,
    ) -> None:
        _subagent_wake_pending.discard(conv)
        # Capture our own task so the finally floor can identity-compare before
        # clearing the slot (see below).
        _own_task = asyncio.current_task()
        # A fresh turn is binding: whatever desync the previous turn ended on is
        # resolved now. Also clear a stale publish-once token (e.g. left set by a
        # wedged stream that never reached its own _on_proxy_stream_end) so it
        # can't suppress this turn's legitimate terminal publish.
        _desynced_sessions.discard(conv)
        _desync_terminalized.pop(conv, None)
        # Locate any uncoded exception logged below in the turn phase (this task's
        # context carries it for its lifetime). Coded errors keep their own phase.
        with phase_scope(ErrorPhase.TURN):
            try:
                await _run_turn_bg_setup_and_stream(msg_body, conv)
            except _ContextWindowOverflow:
                # Re-raise so the streaming-phase handler (which publishes the
                # error event) is never shadowed by the generic except below.
                raise
            except asyncio.CancelledError as exc:
                _logger.error(
                    "turn cancelled for %s: %s",
                    conv,
                    exc,
                    exc_info=True,
                    extra={"session_id": conv},
                )
                _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})
                raise
            except Exception as exc:
                _logger.error(
                    "turn setup failed for %s: %s",
                    conv,
                    exc,
                    exc_info=True,
                    extra={"session_id": conv},
                )
                _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})
            finally:
                # Permanent-wedge floor: guarantee _active_turns is never left stale,
                # however the body exits — including a BaseException that escapes
                # ``except Exception``. A setup-phase abnormal exit otherwise leaves
                # the slot set and every later message buffers forever.
                #
                # Identity compare-and-clear: only finalize when the slot STILL holds
                # THIS turn's own task. A turn that ended cleanly already popped its
                # slot via _on_proxy_stream_end, which schedules a continuation that
                # can bind a NEW turn's task under the same conv — a bare
                # ``conv in _active_turns`` check would then let this stale finally
                # clobber the newer turn (the same class of bug the ExecutorAdapter
                # identity CAS fixes). When the slot is a None sentinel or a
                # different task, this turn is already accounted for — skip.
                if _active_turns.get(conv) is _own_task and _own_task is not None:
                    _on_proxy_stream_end(conv)

    def _turn_reasoning(conv: str, msg_body: _JsonObject) -> _JsonObject | None:
        """Reasoning block to forward on a turn, or ``None`` when unset.

        An explicit per-event value wins; otherwise the session's remembered
        effort (session-init snapshot or a later ``effort_change``) applies, so
        an in-process harness sees the same effort a native TUI got on its argv.

        :param conv: Session/conversation id.
        :param msg_body: The dispatched message body.
        :returns: ``{"effort": "<level>"}`` or ``None``.
        """
        raw = msg_body.get("reasoning")
        if isinstance(raw, dict):
            effort = raw.get("effort")
            if isinstance(effort, str) and effort:
                return {"effort": effort}
        remembered = _session_reasoning_effort.get(conv)
        return {"effort": remembered} if remembered else None

    async def _run_turn_bg_setup_and_stream(
        msg_body: _JsonObject,
        conv: str,
    ) -> None:
        _dispatched_agent_id = cast(str | None, msg_body.get("agent_id"))
        await _sync_session_agent(
            conv, _dispatched_agent_id, cast(str | None, msg_body.get("agent_revision"))
        )

        cached_spec_entry = _session_spec_cache.get(conv)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        cached_spec_workdir = _resolved_spec_workdir(cached_spec_entry)
        if cached_spec is None and spec_resolver is not None:
            _aid = _dispatched_agent_id
            if _aid:
                try:
                    resolved = await spec_resolver(_aid, conv)
                    if isinstance(resolved, ResolvedSpec):
                        cached_spec = _unwrap_resolved_spec(resolved)
                        cached_spec_workdir = _resolved_spec_workdir(resolved)
                        _session_spec_cache[conv] = resolved
                    elif resolved is not None:
                        cached_spec = resolved
                        _session_spec_cache[conv] = resolved
                except (httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "Spec resolution failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )
            else:
                try:
                    cached_spec = await _resolve_session_agent_spec(conv)
                    cached_spec_workdir = _resolved_spec_workdir(_session_spec_cache.get(conv))
                except (OmnigentError, httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "On-demand agent resolution failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )

        # The resolver branches above write straight into the cache, so the
        # entry read at the top of this block can already be stale.
        cached_spec_entry = _session_spec_cache.get(conv, cached_spec_entry)

        _sa_name = await _recover_sub_agent_name(conv)
        # The child's workdir is rooted before _spec_with_workdir_paths below,
        # which joins relative local-tool paths onto whatever workdir is current.
        if _sa_name and cached_spec is not None:
            sub_entry = _native_runtime._resolve_sub_agent_spec_entry(cached_spec_entry, _sa_name)
            if sub_entry is None:
                # Warn unless the child was confirmed resolved (True = already cached).
                if _session_sub_agent_resolved.get(conv) is not True:
                    _warn_unresolved_sub_agent(conv, _sa_name)
            else:
                cached_spec_entry = sub_entry
                cached_spec = _unwrap_resolved_spec(sub_entry)
                cached_spec_workdir = _resolved_spec_workdir(sub_entry)
                _session_spec_cache[conv] = sub_entry

        cached_spec = _spec_with_workdir_paths(cached_spec, cached_spec_workdir)
        if cached_spec is not None:
            cached_spec_entry = _rewrap_like(cached_spec_entry, cached_spec, cached_spec_workdir)
            _session_spec_cache[conv] = cached_spec_entry

        harness_name: str | None = None
        raw_harness: str | None = None
        spawn_env: dict[str, str] | None = None
        instructions: str | None = None
        _note_session_harness_override(conv, cast(str | None, msg_body.get("harness_override")))
        if cached_spec is not None:
            # The session's recorded override outranks the spec (mirrors
            # _initialize_session): the native terminal forward carries no
            # per-event harness_override, so resolving from the body alone
            # dropped a later turn back onto the spec's harness and evicted
            # the override harness mid-session.
            raw_harness = (
                _session_harness_overrides.get(conv)
                or cast(str | None, msg_body.get("harness_override"))
                or cached_spec.executor.config.get("harness")
                or cached_spec.executor.type
            )
            harness_name = canonicalize_harness(raw_harness) or raw_harness

        if conv not in _session_histories:
            _session_histories[conv] = (
                [] if is_native_harness(harness_name) else await _load_history_as_input(conv)
            )
        _raw_per_request_instructions = cast(str | None, msg_body.get("instructions"))
        if cached_spec is not None:
            spawn_env = _build_spawn_env_from_spec(
                cached_spec,
                cast(str, raw_harness),
                workdir=cached_spec_workdir,
                cwd=await _session_runtime_cwd(conv),
                model_override=cast(str | None, msg_body.get("model_override")),
                session_id=conv,
                resource_registry=resource_registry,
            )
            # Gated harnesses use nullable to avoid the fallback literal.
            _authored_bg = raw_author_instructions(cached_spec) is not None
            if harness_name in _GATED_COMPOSED_INSTRUCTION_HARNESSES:
                instructions = build_instructions_nullable(
                    cached_spec, _raw_per_request_instructions, []
                )
            else:
                instructions = build_instructions(
                    cached_spec,
                    _raw_per_request_instructions,
                    [],
                )
            # Warn once per (conversation, harness, delivery) if the agent has
            # authored instructions but the harness can't deliver them.
            if _authored_bg and harness_name:
                _bg_caps = harness_capabilities().get(harness_name)
                _bg_delivery = (
                    _bg_caps.instruction_delivery
                    if _bg_caps is not None
                    else InstructionDelivery.UNKNOWN
                )
                _bg_warn_key = (harness_name, _bg_delivery)
                if _bg_warn_key not in _instruction_delivery_warned.get(conv, ()):
                    _instruction_delivery_warned.setdefault(conv, set()).add(_bg_warn_key)
                    if _bg_delivery in (
                        InstructionDelivery.NOT_DELIVERED,
                        InstructionDelivery.UNKNOWN,
                    ):
                        _logger.warning(
                            "agent instructions not delivered for session=%s "
                            "harness=%s delivery=%s — authored instructions "
                            "accepted but have no delivery channel on this harness.",
                            conv,
                            harness_name,
                            _bg_delivery.value,
                            extra={"session_id": conv},
                        )

        ctx = TurnDispatch(
            agent_id=_dispatched_agent_id,
            harness=harness_name,
            spawn_env=spawn_env,
            has_mcp_servers=(
                (cached_spec is not None and bool(cached_spec.mcp_servers))
                or msg_body.get("has_mcp_servers") is True
            ),
            instructions=instructions,
        )

        harness_body: _JsonObject = {
            "type": "message",
            "role": "user",
            "model": msg_body.get("model", ""),
        }
        # The routed model rides in-band on the forwarded message. This body is
        # built field by field (not copied), so it must be threaded explicitly:
        # the harness forwards it onto CreateResponseRequest.model_override and
        # the executor adapter into ExecutorConfig.model, which is how a native
        # terminal learns to switch models for this turn.
        _model_override = msg_body.get("model_override")
        if isinstance(_model_override, str) and _model_override:
            harness_body["model_override"] = _model_override
        # The web's stable id for this message: stamped on the turn's failure
        # so the server can settle exactly this queued entry, not the oldest.
        _stable_id = msg_body.get("stable_id")
        if isinstance(_stable_id, str) and _stable_id:
            harness_body["input_stable_id"] = _stable_id
            _logger.info(
                "_run_turn_bg: conv=%s received model_override=%s (forwarding to harness)",
                conv,
                _model_override,
                extra={"session_id": conv},
            )
        # Resolve the effort for this turn — an explicit per-event value, else
        # the session's remembered one — then deliver only what this harness can
        # accept. The persisted effort is validated at create against the union
        # vocabulary, so a value that is legal there ("none" on an
        # Anthropic-family harness) can still be foreign here, and the executors
        # reject an unsupported value by failing the turn. The native launch path
        # filters the same way (see native/orchestration.py's ``--effort``
        # guard); an unknown harness is passed through rather than dropped, since
        # a plugin harness may accept efforts this registry has never heard of.
        _reasoning = _turn_reasoning(conv, msg_body)
        if _reasoning is not None:
            from omnigent.util.reasoning_effort import efforts_for_harness, format_supported

            _effort = _reasoning["effort"]
            _supported = efforts_for_harness(harness_name)
            if _supported is None or _effort in _supported:
                harness_body["reasoning"] = _reasoning
            else:
                _logger.warning(
                    "_run_turn_bg: conv=%s dropping reasoning effort %r — harness %s accepts %s",
                    conv,
                    _effort,
                    harness_name,
                    format_supported(_supported) if _supported else "no effort override",
                )
        if _session_histories[conv]:
            harness_body["content"] = _session_histories[conv]
        else:
            harness_body["content"] = msg_body.get(
                "content",
                [],
            )
        _content = cast(list[object], harness_body.get("content", []))
        _content_summary = []
        for _ci in _content:
            if isinstance(_ci, dict):
                _ct = _ci.get("type", "?")
                if _ct == "message":
                    _blocks = cast(list[object], _ci.get("content", []))
                    _block_types = [b.get("type") for b in _blocks if isinstance(b, dict)]
                    _content_summary.append(f"msg({_ci.get('role', '?')}, blocks={_block_types})")
                else:
                    _content_summary.append(str(_ct))
        _logger.info(
            "_run_turn_bg: conv=%s history_msgs=%d content_summary=%s",
            conv,
            len(_content),
            _content_summary[:20],
            extra={"session_id": conv},
        )

        if instructions:
            harness_body["instructions"] = instructions

        if conv not in _session_tool_schemas:
            all_tools: list[_JsonObject] = []
            if cached_spec is not None:
                try:
                    from omnigent.tools.manager import (
                        ToolManager,
                    )

                    _tmgr = ToolManager(
                        cached_spec,
                        workdir=_resolved_workdir_for_spec(cached_spec_entry, runner_workspace),
                        os_env_schema_only=True,
                    )
                    all_tools.extend(_tmgr.get_tool_schemas())
                except (
                    ImportError,
                    ValueError,
                    RuntimeError,
                ):
                    _logger.warning(
                        "ToolManager schema build failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )
            _session_tool_schemas[conv] = all_tools

        if cached_spec and cached_spec.mcp_servers:
            from omnigent.runner.mcp_manager import compute_spec_hash

            _mcp_hash = compute_spec_hash(list(cached_spec.mcp_servers))
            if _mcp_hash != _session_mcp_spec_hash.get(conv):
                _session_mcp_proxy = ProxyMcpManager(
                    conv,
                    server_client,
                    execution_registry=mcp_execution_registry,
                )
                try:
                    mcp_result = await _session_mcp_proxy.schemas_for(
                        cached_spec,
                    )
                    _builtin_tools = [
                        t
                        for t in _session_tool_schemas.get(conv, [])
                        if not (
                            isinstance(t, dict)
                            and isinstance(t.get("name"), str)
                            and "__" in cast(str, t.get("name"))
                        )
                    ]
                    _session_tool_schemas[conv] = _builtin_tools + list(mcp_result.schemas)
                    _session_mcp_spec_hash[conv] = _mcp_hash
                except (
                    httpx.HTTPError,
                    RuntimeError,
                    ValueError,
                ):
                    _logger.warning(
                        "MCP schema resolution failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )

        _spec_tools = _session_tool_schemas.get(conv) or []
        # Request-driven harnesses should not advertise browser tools when no
        # renderer is subscribed. Native harnesses ignore this per-turn list
        # and keep their session-scoped relay surface; their calls still use
        # the prompt no-renderer failure below. An absent hint from an older
        # server preserves the previous advertised surface. Only the spec
        # surface is filtered; request-supplied tools remain caller-owned.
        if msg_body.get("browser_renderer_available") is False:
            from omnigent.runner.tool_dispatch import strip_browser_tool_schemas

            _spec_tools = strip_browser_tool_schemas(_spec_tools)
        _client_tools = cast(list[_JsonObject], msg_body.get("tools") or [])
        merged_tools = _merge_request_client_tools(_spec_tools, _client_tools)
        if merged_tools:
            harness_body["tools"] = merged_tools
        _spec_names = {
            name
            for t in _spec_tools
            if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
        }
        ctx.client_side_tool_names = frozenset(
            name
            for t in _client_tools
            if isinstance(t, dict)
            and (name := _schema_tool_name(t)) is not None
            and name not in _spec_names
        )

        await _ensure_native_terminal_for_turn(conv, harness_name)

        startup_envelope = _fresh_session_init_envelope(conv)
        startup_labels = startup_envelope.snapshot.labels if startup_envelope is not None else None

        if harness_name == "claude-native":
            await _ensure_comment_relay_started(
                conv,
                await_notify=False,
                session_labels=startup_labels,
            )
        elif harness_name == "codex-native":
            from omnigent.harnesses.codex_native.bridge import (
                CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                write_mcp_bridge_config,
            )
            from omnigent.harnesses.codex_native.bridge import (
                bridge_dir_for_bridge_id as codex_bridge_dir_for_id,
            )

            codex_labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv,
            )
            codex_bid = codex_labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
            codex_bdir = codex_bridge_dir_for_id(codex_bid or conv)
            write_mcp_bridge_config(codex_bdir)
            await _ensure_comment_relay_started(
                conv, explicit_bridge_dir=codex_bdir, await_notify=False
            )
        elif harness_name == "antigravity-native":
            from omnigent.harnesses.antigravity_native.bridge import (
                ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
                write_mcp_bridge_config,
            )
            from omnigent.harnesses.antigravity_native.bridge import (
                bridge_dir_for_bridge_id as antigravity_bridge_dir_for_id,
            )

            antigravity_labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv,
            )
            antigravity_bid = antigravity_labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY)
            antigravity_bdir = antigravity_bridge_dir_for_id(antigravity_bid or conv)
            write_mcp_bridge_config(antigravity_bdir)
            await _ensure_comment_relay_started(
                conv, explicit_bridge_dir=antigravity_bdir, await_notify=False
            )
        elif harness_name == "hermes":
            from omnigent.harnesses.hermes_native.bridge import (
                bridge_dir_for_session_id as hermes_bridge_dir_for_session,
            )

            await _ensure_comment_relay_started(
                conv,
                explicit_bridge_dir=hermes_bridge_dir_for_session(conv),
                await_notify=False,
            )

        try:
            response = await _stream_message_to_harness(
                harness_body,
                conv,
                dispatch=ctx,
            )
        finally:
            _session_init_envelopes.pop(conv, None)
        if isinstance(response, StreamingResponse):
            await _drain_streaming_response(response, conv)
        else:
            error = _harness_error_response_error(response)
            _logger.error(
                "turn bg error for %s: %s",
                conv,
                error["message"],
                extra={"session_id": conv},
            )
            _on_proxy_stream_end(conv, error=error)

    async def _drain_streaming_response(
        response: StreamingResponse,
        session_id: str,
    ) -> None:
        try:
            async for _chunk in response.body_iterator:
                pass
        except asyncio.CancelledError:
            # Identity guard (same generation-ownership class as
            # _on_proxy_stream_end and the _run_turn_bg finally floor): the drain
            # runs INLINE in this turn's own _run_turn_bg task, so the slot should
            # still hold that task. Only clear when it does — if a newer turn has
            # taken the slot, this is a stale finalizer and must not pop the newer
            # turn's state, response id, or publish a spurious idle over it.
            # ``delete_session`` pops the slot before cancelling, so an empty
            # slot still means "no newer turn took over" — publish for it too.
            _slot = _active_turns.get(session_id)
            if _slot is None or _slot is asyncio.current_task():
                _active_turns.pop(session_id, None)
                # Clear the live response AND the in-flight marker together (B1
                # class fix): a bare pop would leak the process-manager marker and
                # the idle reaper would skip the harness forever.
                _release_live_turn_markers(session_id)
                # Publish-once guard, epoch-scoped (same token as
                # _on_proxy_stream_end): suppress this ``idle`` only if recovery
                # claimed the terminal for THIS generation's epoch.
                if _desync_terminalized.get(session_id) == _turn_bind_epoch.get(session_id, 0):
                    _desync_terminalized.pop(session_id, None)
                else:
                    _publish_turn_status(session_id, "idle")
            raise
        except (httpx.HTTPError, RuntimeError, StopAsyncIteration) as exc:
            _logger.error(
                "drain failed for %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(
                session_id,
                error={
                    "message": f"background turn drain failed: {exc}",
                },
            )

    async def _stream_message_to_harness(
        body: _JsonObject,
        conv_id: str,
        dispatch: TurnDispatch | None = None,
    ) -> Response:
        manager = cast(HarnessProcessManager, process_manager)
        harness_name = dispatch.harness if dispatch else cast(str | None, body.get("harness"))
        spawn_env = (
            dispatch.spawn_env if dispatch else cast(dict[str, str] | None, body.get("spawn_env"))
        )
        _note_session_harness_override(conv_id, cast(str | None, body.get("harness_override")))
        # Shared agent-change invalidation for both dispatch paths.
        _ds_agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
        await _sync_session_agent(
            conv_id,
            _ds_agent_id,
            None if dispatch else cast(str | None, body.get("agent_revision")),
        )
        startup_envelope = _fresh_session_init_envelope(conv_id)
        startup_labels = startup_envelope.snapshot.labels if startup_envelope is not None else None
        if not harness_name:
            _agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
            _sub_agent_name = await _recover_sub_agent_name(conv_id)
            try:
                harness_name, spawn_env = await _resolve_harness_config(
                    resource_registry=resource_registry,
                    agent_id=_agent_id,
                    spec_resolver=spec_resolver,
                    session_id=conv_id,
                    model_override=cast(str | None, body.get("model_override")),
                    # Session-recorded override first (the note above already
                    # folded in any body value): a body without one must not
                    # drop the turn back onto the spec's harness.
                    harness_override=(
                        _session_harness_overrides.get(conv_id)
                        or cast(str | None, body.get("harness_override"))
                    ),
                    sub_agent_name=_sub_agent_name,
                    cwd=await _session_runtime_cwd(conv_id),
                )
            except SessionAgentMissingError:
                return JSONResponse(
                    status_code=410,
                    content={
                        "error": ErrorCode.SESSION_AGENT_MISSING,
                        "detail": SESSION_AGENT_MISSING_MESSAGE,
                    },
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        from omnigent.sandbox.copy_on_write import (
            SHARED_ENVIRONMENT_VAR,
            export_shared_environment,
            has_copy_on_write,
            validate_copy_on_write_harness,
        )

        stream_spec = _unwrap_resolved_spec(_session_spec_cache.get(conv_id))
        if stream_spec is None and _ds_agent_id and spec_resolver is not None:
            try:
                stream_spec = _unwrap_resolved_spec(await spec_resolver(_ds_agent_id, conv_id))
            except (OmnigentError, httpx.HTTPError, RuntimeError):
                stream_spec = None
        if has_copy_on_write(
            getattr(stream_spec, "os_env", None)
        ) or resource_registry.uses_copy_on_write(conv_id):
            if stream_spec is None:
                return JSONResponse(
                    status_code=503, content={"error": "copy_on_write session spec unavailable"}
                )
            try:
                validate_copy_on_write_harness(stream_spec.os_env, harness_name)
                environment = resource_registry.resolve_environment(
                    conv_id, DEFAULT_ENVIRONMENT_ID, stream_spec
                )
                policy = getattr(environment, "sandbox", None)
                if policy is None:
                    raise ValueError("copy_on_write requires a local sandbox environment")
                environment.prepare_sandbox(policy)
                spawn_env = dict(spawn_env or {})
                spawn_env[SHARED_ENVIRONMENT_VAR] = export_shared_environment(policy)
            except ValueError as exc:
                _logger.warning("copy_on_write setup failed for %s", conv_id, exc_info=True)
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "copy_on_write_setup_failed",
                        "detail": _client_safe_error_detail(exc, context="copy_on_write setup"),
                        "hint": "Use executor.harness=openai-agents; start a new session after "
                        "changing copy_on_write paths.",
                    },
                )

        if spawn_env is None:
            spawn_env = await _resolve_native_spawn_env(
                harness_name,
                conv_id,
                server_client=server_client,
                optional_labels=startup_labels,
            )

        agent_version = (
            dispatch.agent_version if dispatch else cast(int | None, body.get("agent_version"))
        )
        if agent_version is not None and conv_id in _version_cache:
            if agent_version > _version_cache[conv_id]:
                await manager.release(conv_id)
        if agent_version is not None:
            _version_cache[conv_id] = agent_version

        if harness_name == "opencode-native":
            # Turn-path cold-boot: ensure the terminal exists before the turn.
            # A launch failure here aborts the turn with a 503 (reraise=True),
            # unlike the create-session arms that publish a start-error event.
            try:
                await _launch_native_terminal(
                    harness_name,
                    NativeLaunchContext(
                        session_id=conv_id,
                        resource_registry=resource_registry,
                        publish_event=_publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    ),
                    ensure_locks=_opencode_terminal_ensure_locks,
                    resolve_agent_spec=lambda: _resolve_session_agent_spec_or_none(conv_id),
                    reraise=True,
                )
            except Exception as exc:
                _logger.exception(
                    "opencode-native cold-boot ensure failed for %s",
                    conv_id,
                    extra={"session_id": conv_id},
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_boot_failed",
                        "detail": _client_safe_error_detail(exc, context="opencode-native boot"),
                    },
                )

        # A required-terminal exit recorded before this turn belongs to an
        # earlier stream; only exits observed from here on can end this one.
        _required_terminal_exit_errors.pop(conv_id, None)
        try:
            client = await manager.get_client(conv_id, harness_name, env=spawn_env)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _turn_agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
        _has_mcp_hint = dispatch.has_mcp_servers if dispatch else body.get("has_mcp_servers")
        _turn_spec: object | None = None
        _turn_spec_entry: object | None = None
        _turn_spec_resolved = False
        _mcp_schemas: list[_JsonObject] = []
        _mcp_tool_names: set[str] = set()
        _eager_spec_error: tuple[str, str] | None = None
        if _has_mcp_hint is True and _turn_agent_id:
            _turn_spec_entry = _spec_cache.get(_turn_agent_id)
            _turn_spec = _unwrap_resolved_spec(_turn_spec_entry)
            if _turn_spec is None:
                _session_entry = _session_spec_cache.get(conv_id)
                _turn_spec_entry = _session_entry
                _turn_spec = _unwrap_resolved_spec(_session_entry)
            if _turn_spec is None and spec_resolver is not None:
                try:
                    _resolved_turn_spec = await spec_resolver(_turn_agent_id, conv_id)
                    _turn_spec = _unwrap_resolved_spec(_resolved_turn_spec)
                except (httpx.HTTPError, RuntimeError) as exc:
                    _logger.warning(
                        "eager turn spec resolution failed for %s: %s",
                        conv_id,
                        exc,
                        exc_info=True,
                        extra={
                            "session_id": conv_id,
                            "event_name": "runner_turn_spec_resolution_failed",
                            "attributes": {"phase": "eager", "exception_type": type(exc).__name__},
                        },
                    )
                    _eager_spec_error = (
                        type(exc).__name__,
                        "Failed to resolve the agent spec for this turn.",
                    )
                else:
                    if _resolved_turn_spec is not None and _turn_spec is not None:
                        _spec_cache[_turn_agent_id] = _resolved_turn_spec
                        _turn_spec_entry = _resolved_turn_spec
            _turn_spec_resolved = True
            _turn_mcp = ProxyMcpManager(
                conv_id,
                server_client,
                execution_registry=mcp_execution_registry,
            )
            if _eager_spec_error is None and _turn_spec is not None:
                try:
                    _mcp = await _turn_mcp.schemas_for(cast(AgentSpec, _turn_spec))
                    _mcp_schemas = _mcp.schemas
                    _mcp_tool_names = _mcp.tool_names
                    for _srv, _err in _mcp.failures.items():
                        _logger.warning(
                            "runner MCP %r unavailable for this turn: %s",
                            _srv,
                            _err,
                            extra={"session_id": conv_id},
                        )
                except Exception:
                    _logger.exception(
                        "runner mcp_manager.schemas_for failed", extra={"session_id": conv_id}
                    )

        async def _resolve_turn_spec_lazy() -> tuple[object | None, tuple[str, str] | None]:
            nonlocal _turn_spec, _turn_spec_entry, _turn_spec_resolved
            if _turn_spec_resolved:
                return _turn_spec_entry or _turn_spec, None
            _turn_spec_resolved = True
            session_cached = _session_spec_cache.get(conv_id)
            if session_cached is not None:
                _turn_spec_entry = session_cached
                _turn_spec = _unwrap_resolved_spec(session_cached)
                return session_cached, None
            if not _turn_agent_id or spec_resolver is None:
                return None, None
            cached = _spec_cache.get(_turn_agent_id)
            if cached is not None:
                _turn_spec_entry = cached
                _turn_spec = _unwrap_resolved_spec(cached)
                return cached, None
            try:
                resolved = await spec_resolver(_turn_agent_id, conv_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                _logger.warning(
                    "lazy turn spec resolution failed for %s: %s",
                    conv_id,
                    exc,
                    exc_info=True,
                    extra={
                        "session_id": conv_id,
                        "event_name": "runner_turn_spec_resolution_failed",
                        "attributes": {"phase": "lazy", "exception_type": type(exc).__name__},
                    },
                )
                return None, (
                    type(exc).__name__,
                    "Failed to resolve the agent spec for this turn.",
                )
            if resolved is not None:
                _spec_cache[_turn_agent_id] = resolved
                _turn_spec_entry = resolved
                _turn_spec = _unwrap_resolved_spec(resolved)
                return resolved, None
            return None, None

        async def proxy_stream() -> AsyncIterator[bytes]:
            import asyncio as _asyncio
            import json as _json

            from omnigent.runner.tool_dispatch import (
                dispatch_tool_locally,
                get_arguments,
                get_call_id,
                get_tool_name,
                is_action_required,
                should_dispatch_locally,
            )

            if _eager_spec_error is not None:
                _err_type, _err_msg = _eager_spec_error
                _fail = _response_failed_payload({"message": _err_msg, "type": _err_type})
                _publish_event(conv_id, _fail)
                _on_proxy_stream_end(
                    conv_id,
                    error={"message": _err_msg, "type": _err_type},
                )
                yield _response_failed_event({"message": _err_msg, "type": _err_type})
                return

            # Compose instructions for direct-stream turns (dispatch is None;
            # background path pre-composes).
            _instr_body = body
            if dispatch is None:
                with contextlib.suppress(OmnigentError, httpx.HTTPError, RuntimeError, ValueError):
                    _instr_entry_ds = await _resolve_session_spec_entry(conv_id)
                    _instr_spec_ds = _unwrap_resolved_spec(_instr_entry_ds)
                    if _instr_spec_ds is not None:
                        _per_req_instr = cast(str | None, body.get("instructions"))
                        _authored_ds = raw_author_instructions(_instr_spec_ds) is not None
                        _ic_ds = InstructionComposition(
                            authored_present=_authored_ds,
                            composed=build_instructions_nullable(
                                _instr_spec_ds, _per_req_instr, []
                            ),
                        )
                        # Gated harnesses get nullable — skip the fallback literal.
                        if harness_name in _GATED_COMPOSED_INSTRUCTION_HARNESSES:
                            _instr_val = _ic_ds.composed
                            if _instr_val is not None:
                                _instr_body = {**body, "instructions": _instr_val}
                        elif _ic_ds.composed is not None:
                            _instr_body = {
                                **body,
                                "instructions": build_instructions(
                                    _instr_spec_ds, _per_req_instr, []
                                ),
                            }
                        if _authored_ds and harness_name:
                            _ds_caps = harness_capabilities().get(harness_name)
                            _ds_delivery = (
                                _ds_caps.instruction_delivery
                                if _ds_caps is not None
                                else InstructionDelivery.UNKNOWN
                            )
                            _ds_warn_key = (harness_name, _ds_delivery)
                            _already_warned_ds = _ds_warn_key in _instruction_delivery_warned.get(
                                conv_id, ()
                            )
                            if not _already_warned_ds:
                                _instruction_delivery_warned.setdefault(conv_id, set()).add(
                                    _ds_warn_key
                                )
                                if _ds_delivery in (
                                    InstructionDelivery.NOT_DELIVERED,
                                    InstructionDelivery.UNKNOWN,
                                ):
                                    _logger.warning(
                                        "agent instructions not delivered for session=%s "
                                        "harness=%s delivery=%s — authored instructions "
                                        "accepted but have no delivery channel on this harness.",
                                        conv_id,
                                        harness_name,
                                        _ds_delivery.value,
                                        extra={"session_id": conv_id},
                                    )
            # Re-warn on every turn when session-create established a miss.
            _ds_sa = _session_sub_agent_names.get(conv_id)
            if _ds_sa and _session_sub_agent_resolved.get(conv_id) is False:
                _warn_unresolved_sub_agent(conv_id, _ds_sa)
            event_body = _wrap_as_message_event(_instr_body)
            _inject_mcp_schemas(event_body, _mcp_schemas)
            _response_id: str | None = None
            try:
                async with client.stream(
                    "POST",
                    f"/v1/sessions/{conv_id}/events",
                    json=event_body,
                    timeout=None,
                ) as harness_resp:
                    if harness_resp.status_code != 200:
                        _logger.error(
                            "harness rejected turn delivery for %s with status %d",
                            conv_id,
                            harness_resp.status_code,
                            extra={
                                "session_id": conv_id,
                                "event_name": "harness_turn_rejected",
                                "attributes": {
                                    "harness": harness_name,
                                    "http_status": harness_resp.status_code,
                                },
                            },
                        )
                        _fail_status = _response_failed_payload(
                            {"status": harness_resp.status_code}, source="harness"
                        )
                        _publish_event(
                            conv_id,
                            _fail_status,
                        )
                        _on_proxy_stream_end(
                            conv_id,
                            error={"status": harness_resp.status_code},
                        )
                        yield _response_failed_event(
                            {"status": harness_resp.status_code}, source="harness"
                        )
                        return

                    _omnigent_task_id = cast(str | None, body.get("task_id"))
                    _buffer = ""
                    _dispatch_tasks: list[_asyncio.Task[object]] = []
                    # Sub-agent start edge → minted child id, so the completion
                    # edge can address the child it created. Per-stream.
                    _subagent_child_futures: dict[str, _asyncio.Future[str]] = {}
                    # Sub-agent start edge → its title, so the completion edge can
                    # author the summary message (assistant messages need one).
                    _subagent_titles: dict[str, str] = {}
                    # Sub-agent child_key → its latest transcript-post task, so the
                    # mint / tool-call / completion posts for one child land in
                    # order instead of racing.
                    _subagent_post_chains: dict[str, _asyncio.Task[object]] = {}
                    _text_acc: list[str] = []
                    _stream_failed_error: _JsonObject | None = None
                    async for chunk in harness_resp.aiter_text():
                        _buffer += chunk
                        while "\n\n" in _buffer:
                            frame, _, _buffer = _buffer.partition("\n\n")
                            raw_sse_bytes = (frame + "\n\n").encode("utf-8")

                            data_line = next(
                                (line for line in frame.splitlines() if line.startswith("data:")),
                                None,
                            )
                            if data_line is not None:
                                try:
                                    event = _json.loads(data_line[5:].strip())
                                except _json.JSONDecodeError:
                                    event = None
                            else:
                                event = None

                            _defer_publish = False
                            if event is not None:
                                if event.get("type") == "response.created":
                                    resp_obj = event.get("response") or {}
                                    _response_id = resp_obj.get("id")
                                    if _response_id and conv_id:
                                        _resp_to_conv[_response_id] = conv_id
                                        _live_response_id[conv_id] = _response_id
                                        manager.mark_in_flight(conv_id, _response_id)

                                _overflow = _is_context_overflow_error(event)
                                if _overflow is not None:
                                    _max_tokens, _actual_tokens, _ov_detail = _overflow
                                    raise _ContextWindowOverflow(
                                        _max_tokens,
                                        _actual_tokens,
                                        detail_message=_ov_detail,
                                    )

                                _evt_type = event.get("type")
                                if (
                                    _evt_type == "response.compaction.in_progress"
                                    and conv_id in _sdk_compact_inprogress
                                ):
                                    # _handle_claude_sdk_compact already published an
                                    # up-front spinner; drop the executor's own duplicate
                                    # so the web renders a single compaction spinner.
                                    continue
                                if _evt_type == "injection.consumed":
                                    _inj_id = event.get("injection_id")
                                    _buf = _session_message_buffers.get(conv_id)
                                    if _inj_id is not None and _buf:
                                        _consumed = [
                                            _m for _m in _buf if _m.get("injection_id") == _inj_id
                                        ]
                                        _remaining = [
                                            _m for _m in _buf if _m.get("injection_id") != _inj_id
                                        ]
                                        _session_message_buffers[conv_id] = _remaining
                                        for _m in _consumed:
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "message",
                                                    "role": _m.get("role", "user"),
                                                    "content": _m.get("content", []),
                                                }
                                            )
                                    continue
                                if _evt_type == "response.output_text.delta":
                                    delta = event.get("delta")
                                    if delta is not None:
                                        _text_acc.append(delta)
                                elif _evt_type == "response.completed":
                                    _stream_failed_error = None
                                    if _text_acc:
                                        _session_histories.setdefault(conv_id, []).append(
                                            {
                                                "type": "message",
                                                "role": "assistant",
                                                "content": [
                                                    {
                                                        "type": "output_text",
                                                        "text": "".join(_text_acc),
                                                    }
                                                ],
                                            }
                                        )
                                        _text_acc.clear()
                                elif _evt_type == "response.failed":
                                    _err = event.get("error") or (event.get("response") or {}).get(
                                        "error"
                                    )
                                    _stream_failed_error = (
                                        _err
                                        if isinstance(_err, dict)
                                        else {"message": "harness turn failed"}
                                    )
                                elif _evt_type == "response.output_item.done":
                                    _item = event.get("item")
                                    if isinstance(_item, dict):
                                        _it = _item.get("type")
                                        if _it == "function_call":
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "function_call",
                                                    "call_id": _item["call_id"],
                                                    "name": _item["name"],
                                                    "arguments": _item["arguments"],
                                                }
                                            )
                                        elif _it == "function_call_output":
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "function_call_output",
                                                    "call_id": _item["call_id"],
                                                    "output": _item["output"],
                                                }
                                            )
                                elif _evt_type == "response.compaction.completed" and event.get(
                                    "summary"
                                ):
                                    # A real compaction landed; the completed event clears
                                    # the up-front spinner, so drop the pending flag and
                                    # skip the stream-end `failed` fallback.
                                    _sdk_compact_inprogress.discard(conv_id)
                                    await _handle_harness_compaction(conv_id, event)

                                if is_action_required(event):
                                    tool_name = get_tool_name(event)
                                    is_mcp = tool_name in _mcp_tool_names
                                    _spec_for_dispatch_hint = _unwrap_resolved_spec(
                                        _session_spec_cache.get(conv_id)
                                    )
                                    _is_spec_local = _is_spec_local_native_python_tool(
                                        _spec_for_dispatch_hint,
                                        tool_name,
                                    )
                                    if (
                                        not _is_spec_local
                                        and not is_mcp
                                        and not should_dispatch_locally(tool_name)
                                    ):
                                        (
                                            _spec_for_dispatch_hint_entry,
                                            _lazy_hint_err,
                                        ) = await _resolve_turn_spec_lazy()
                                        if _lazy_hint_err is None:
                                            _spec_for_dispatch_hint = _unwrap_resolved_spec(
                                                _spec_for_dispatch_hint_entry
                                            )
                                            _is_spec_local = _is_spec_local_native_python_tool(
                                                _spec_for_dispatch_hint,
                                                tool_name,
                                            )
                                    _should_dispatch = _should_dispatch_tool_locally(
                                        tool_name,
                                        dispatch=dispatch,
                                        is_mcp=is_mcp,
                                        is_runner_builtin=should_dispatch_locally(tool_name),
                                        is_spec_local=_is_spec_local,
                                    )
                                    if _should_dispatch and _response_id:
                                        _defer_publish = True
                                        (
                                            _spec_for_dispatch_entry,
                                            _lazy_err,
                                        ) = await _resolve_turn_spec_lazy()
                                        if _lazy_err is not None:
                                            _err_type, _err_msg = _lazy_err
                                            _fail = _response_failed_payload(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            _publish_event(conv_id, _fail)
                                            _on_proxy_stream_end(
                                                conv_id,
                                                error={
                                                    "message": _err_msg,
                                                    "type": _err_type,
                                                },
                                                owner_response_id=_response_id,
                                            )
                                            yield _response_failed_event(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            return
                                        _local_tool_workdir = _resolved_workdir_for_spec(
                                            _spec_for_dispatch_entry,
                                            runner_workspace,
                                        )
                                        _spec_for_dispatch = _unwrap_resolved_spec(
                                            _spec_for_dispatch_entry
                                        )
                                        _dispatch_workspace = await _session_runtime_cwd(conv_id)
                                        event[_RUNNER_DISPATCHED_FIELD] = True
                                        raw_sse_bytes = _encode_sse_event(event)
                                        _agent_id_for_dispatch = cast(
                                            str | None, body.get("agent_id")
                                        )
                                        _dispatch_mcp = ProxyMcpManager(
                                            conv_id,
                                            server_client,
                                            publish_event=_publish_event,
                                            execution_registry=mcp_execution_registry,
                                        )
                                        _dispatch_tasks.append(
                                            _asyncio.create_task(
                                                dispatch_tool_locally(
                                                    tool_name=tool_name,
                                                    call_id=get_call_id(event),
                                                    arguments=get_arguments(event),
                                                    response_id=_response_id,
                                                    harness_client=client,
                                                    server_client=server_client,
                                                    terminal_registry=terminal_registry,
                                                    resource_registry=resource_registry,
                                                    agent_spec=_spec_for_dispatch,
                                                    conversation_id=conv_id,
                                                    task_id=_omnigent_task_id or _response_id,
                                                    agent_id=_agent_id_for_dispatch,
                                                    agent_name=cast(str | None, body.get("model")),
                                                    runner_workspace=_dispatch_workspace,
                                                    local_tool_workdir=_local_tool_workdir,
                                                    mcp_manager=cast(
                                                        "RunnerMcpManager", _dispatch_mcp
                                                    ),
                                                    session_inbox=_session_inboxes.get(conv_id),
                                                    session_async_tasks=_session_async_tasks.get(
                                                        conv_id
                                                    ),
                                                    publish_event=_publish_event,
                                                    filesystem_registry=filesystem_registry,
                                                    effective_harness=_session_harness_name(
                                                        conv_id
                                                    ),
                                                )
                                            )
                                        )
                                        _dispatch_tasks[-1].add_done_callback(
                                            functools.partial(
                                                _recover_failed_tool_dispatch,
                                                conv_id=conv_id,
                                                response_id=_response_id,
                                            )
                                        )

                                if _evt_type == "policy_evaluation.requested":
                                    _eval_id = event.get("evaluation_id", "")
                                    _eval_phase = event.get("phase", "")
                                    _eval_data = event.get("data") or {}
                                    _dispatch_tasks.append(
                                        _asyncio.create_task(
                                            _evaluate_policy_via_omnigent(
                                                server_client=server_client,
                                                harness_client=client,
                                                conversation_id=conv_id,
                                                evaluation_id=_eval_id,
                                                phase=_eval_phase,
                                                data=_eval_data,
                                                # A dead verdict-delivery channel
                                                # parks the harness turn forever;
                                                # route it to the recovery entry,
                                                # binding THIS turn's response id
                                                # so a delayed failure can't cancel
                                                # a newer turn (ownership gate).
                                                on_delivery_failure=functools.partial(
                                                    _resync_turn_state_on_delivery_failure,
                                                    response_id=_response_id,
                                                ),
                                            )
                                        )
                                    )
                                    continue

                                if _evt_type == "subagent.started":
                                    # A harness reported spawning a sub-agent; mint
                                    # a child session so it shows in the Subagents
                                    # panel. Swallowed (never relayed to clients).
                                    # The mint task heads the child's post chain, so
                                    # its tool calls and summary land after it.
                                    _sa_key = event.get("child_key", "")
                                    if isinstance(_sa_key, str) and _sa_key:
                                        _sa_start_future: _asyncio.Future[str] = (
                                            _asyncio.get_running_loop().create_future()
                                        )
                                        _subagent_child_futures[_sa_key] = _sa_start_future
                                        _subagent_titles[_sa_key] = event.get("title", "")
                                        _sa_mint_task = _asyncio.create_task(
                                            _mint_acp_subagent_child(
                                                server_client,
                                                parent_id=conv_id,
                                                child_key=_sa_key,
                                                title=event.get("title", ""),
                                                task=event.get("task", ""),
                                                child_id_future=_sa_start_future,
                                            )
                                        )
                                        _subagent_post_chains[_sa_key] = _sa_mint_task
                                        _dispatch_tasks.append(_sa_mint_task)
                                    continue

                                if _evt_type == "subagent.tool_call":
                                    # A tool call the sub-agent ran; append it to the
                                    # child's transcript, chained after the child's
                                    # previous post so it stays ordered.
                                    _sa_tc_key = event.get("child_key", "")
                                    _sa_tc_future = (
                                        _subagent_child_futures.get(_sa_tc_key)
                                        if isinstance(_sa_tc_key, str)
                                        else None
                                    )
                                    if _sa_tc_future is not None:
                                        _sa_tc_task = _chain_acp_subagent_post(
                                            _subagent_post_chains.get(_sa_tc_key),
                                            _post_acp_subagent_tool_call(
                                                server_client,
                                                child_key=_sa_tc_key,
                                                call_id=event.get("call_id", ""),
                                                name=event.get("name", "tool"),
                                                arguments=event.get("arguments", ""),
                                                child_id_future=_sa_tc_future,
                                                title=_subagent_titles.get(_sa_tc_key, ""),
                                            ),
                                        )
                                        _subagent_post_chains[_sa_tc_key] = _sa_tc_task
                                        _dispatch_tasks.append(_sa_tc_task)
                                    continue

                                if _evt_type == "subagent.completed":
                                    _sa_done_key = event.get("child_key", "")
                                    _sa_done_future = (
                                        _subagent_child_futures.get(_sa_done_key)
                                        if isinstance(_sa_done_key, str)
                                        else None
                                    )
                                    if _sa_done_future is not None:
                                        _sa_done_task = _chain_acp_subagent_post(
                                            _subagent_post_chains.get(_sa_done_key),
                                            _complete_acp_subagent_child(
                                                server_client,
                                                child_key=_sa_done_key,
                                                ok=bool(event.get("ok", True)),
                                                summary=event.get("summary", ""),
                                                child_id_future=_sa_done_future,
                                                title=_subagent_titles.get(_sa_done_key, ""),
                                            ),
                                        )
                                        _subagent_post_chains[_sa_done_key] = _sa_done_task
                                        _dispatch_tasks.append(_sa_done_task)
                                    continue

                            if event is None:
                                yield raw_sse_bytes
                                continue
                            if event.get("type") == "response.failed":
                                _input_stable_id = body.get("input_stable_id")
                                if isinstance(_input_stable_id, str):
                                    event["input_stable_id"] = _input_stable_id
                            if not _defer_publish and event.get("type") != "response.created":
                                _publish_event(conv_id, event)
                            if dispatch is not None and event.get(_RUNNER_DISPATCHED_FIELD):
                                pass
                            else:
                                yield raw_sse_bytes

                    if _dispatch_tasks:
                        await _asyncio.gather(*_dispatch_tasks, return_exceptions=True)

                    # _on_proxy_stream_end clears any claude-sdk `/compact`
                    # spinner (publishing `failed` when no compaction landed);
                    # it is the single convergence point for every turn-end path.
                    _on_proxy_stream_end(
                        conv_id, error=_stream_failed_error, owner_response_id=_response_id
                    )

            except _ContextWindowOverflow as overflow:
                _error = {
                    "code": "context_length_exceeded",
                    "message": overflow.detail_message
                    or (
                        f"Context window exceeded: {overflow.actual_tokens} tokens "
                        f"> {overflow.max_tokens} max"
                    ),
                    "type": "_ContextWindowOverflow",
                }
                _overflow_fail = _response_failed_payload(_error, source="llm")
                _publish_event(conv_id, _overflow_fail)
                _on_proxy_stream_end(conv_id, error=_error, owner_response_id=_response_id)
                yield _response_failed_event(_error, source="llm")

            except (httpx.HTTPError, RuntimeError) as exc:
                _exit_error = _required_terminal_exit_errors.pop(conv_id, None)
                if _exit_error is not None:
                    # The runner ended this stream itself: the session's required
                    # terminal exited and its handler released the harness
                    # subprocess, closing this client mid-read. Report the exit
                    # and its pane diagnostics, not the transport symptom.
                    _logger.warning(
                        "harness stream for %s ended by required terminal exit: %s: %s",
                        conv_id,
                        type(exc).__name__,
                        exc,
                        extra={
                            "session_id": conv_id,
                            "event_name": "harness_stream_ended_by_terminal_exit",
                            "attributes": {
                                "harness": harness_name,
                                "response_id": _response_id,
                                "exception_type": type(exc).__name__,
                            },
                        },
                    )
                    # The status event's code is derived from ``type``.
                    _error = {**_exit_error, "type": _exit_error["code"]}
                else:
                    # Name the type as well as the text: the messageless httpx
                    # errors otherwise log a trailing colon and nothing, so one
                    # signature covered every transport cause.
                    _logger.exception(
                        "proxy stream connection error for %s: %s: %s",
                        conv_id,
                        type(exc).__name__,
                        exc,
                        extra={
                            "session_id": conv_id,
                            "event_name": "harness_stream_failed",
                            "attributes": {
                                "harness": harness_name,
                                "response_id": _response_id,
                                "exception_type": type(exc).__name__,
                            },
                        },
                    )
                    _error = {
                        "code": "connection_error",
                        "message": _harness_stream_failure_message(conv_id, exc),
                        "type": type(exc).__name__,
                    }
                _http_fail = _response_failed_payload(_error, source="harness")
                _publish_event(conv_id, _http_fail)
                _on_proxy_stream_end(conv_id, error=_error, owner_response_id=_response_id)
                yield _response_failed_event(_error, source="harness")

        return StreamingResponse(
            proxy_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/sessions/{conversation_id}/events")
    async def post_session_events(
        conversation_id: str,
        request: Request,
        stream: bool = Query(default=False),
    ) -> Response:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": (
                        "Runner /v1/sessions/{conv}/events needs a HarnessProcessManager; "
                        "build with create_runner_app(process_manager=...) "
                        "after calling await mgr.start()."
                    ),
                },
            )

        body = await request.json()
        body_type = body.get("type") if isinstance(body, dict) else None
        _logger.info(
            "post_session_events: conv=%s type=%s active=%s buffer_len=%d content_types=%s "
            "model_override=%s",
            conversation_id,
            body_type,
            conversation_id in _active_turns,
            len(_session_message_buffers.get(conversation_id, [])),
            [b.get("type") for b in body.get("content", []) if isinstance(b, dict)]
            if isinstance(body, dict)
            else "N/A",
            body.get("model_override") if isinstance(body, dict) else None,
            extra={"session_id": conversation_id},
        )
        _side_thread_id = body.get("codex_side_thread_id") if isinstance(body, dict) else None
        if _side_thread_id:
            # Side-chat controls use the parent's bridge but target the child's
            # thread, leaving the parent's turn and message buffer untouched.
            from websockets.exceptions import WebSocketException

            from omnigent.harnesses.codex_native import side_chat
            from omnigent.harnesses.codex_native.app_server import (
                CodexAppServerResponseError,
                client_for_transport,
            )

            _side_turn_id = body.get("codex_side_turn_id")
            _side_text = ""
            if body_type == "interrupt":
                if not isinstance(_side_turn_id, str) or not _side_turn_id:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_request",
                            "detail": "Missing side-chat turn id.",
                        },
                    )
            else:
                _side_text = _side_chat_text_from_content(
                    body.get("content") if isinstance(body, dict) else None
                )
                if not _side_text:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_request",
                            "detail": "side chat message had no text",
                        },
                    )
            _side_state = await _codex_native_bridge_state_for_session(
                conversation_id, action="side chat turn"
            )
            if _side_state is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "codex_side_chat_no_bridge",
                        "detail": "Codex /side follow-up requires a loaded parent Codex bridge.",
                    },
                )
            _side_client = client_for_transport(
                _side_state.socket_path, client_name="omnigent-codex-native-runner"
            )
            try:
                await _side_client.connect()
                if body_type == "interrupt":
                    await side_chat.interrupt_side_turn(
                        _side_client, str(_side_thread_id), _side_turn_id
                    )
                else:
                    await side_chat.submit_side_turn(
                        _side_client, str(_side_thread_id), _side_text
                    )
            except CodexAppServerResponseError as exc:
                # Codex refused the turn (e.g. typing into a multi-agent-v2
                # sub-agent, or a thread that no longer exists).
                _rpc_message = exc.message or str(exc)
                _missing = "thread not found" in _rpc_message.casefold()
                _logger.warning(
                    "Codex side-chat turn rejected: conv=%s thread=%s error=%s",
                    conversation_id,
                    _side_thread_id,
                    exc,
                    extra={"session_id": conversation_id},
                )
                return JSONResponse(
                    status_code=404 if _missing else 409,
                    content={
                        "error": "codex_side_chat_not_found"
                        if _missing
                        else "codex_side_chat_rejected",
                        "detail": _rpc_message,
                    },
                )
            except (ConnectionError, OSError, TimeoutError, WebSocketException) as exc:
                _logger.warning(
                    "Codex side-chat app-server unreachable: conv=%s thread=%s error=%r",
                    conversation_id,
                    _side_thread_id,
                    exc,
                    extra={"session_id": conversation_id},
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "codex_side_chat_unavailable",
                        "detail": "The Codex app-server connection was lost; try again.",
                    },
                )
            finally:
                await _side_client.close()
            return Response(status_code=202)
        if body_type == "message" and isinstance(body, dict):
            # A codex /side command opens a side chat; it must not run a turn
            # here. Starting one publishes a "running" edge for this session,
            # and the fork's settling edges belong to the child — idle is
            # forwarder-owned for codex-native — so nothing would ever clear it
            # and the chat would sit on "Working…" for good.
            from omnigent.harnesses.codex_native import side_chat as _side_chat

            _side_question = _side_chat.side_chat_question_from_text(
                _side_chat_text_from_content(body.get("content"))
            )
            if (
                _side_question is not None
                and _session_harness_name(conversation_id) == _CODEX_NATIVE_HARNESS
            ):
                _side_chat.request_side_chat(
                    await _codex_native_bridge_dir_for_session(conversation_id),
                    _side_question,
                )
                _logger.info(
                    "Codex /side recorded without starting a turn: conv=%s",
                    conversation_id,
                    extra={"session_id": conversation_id},
                )
                return Response(status_code=202)
        if body_type == "message" or body_type is None:
            if not isinstance(body, dict):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "detail": "session message body must be a JSON object",
                    },
                )
            message_body = dict(body)
            message_body["conversation_id"] = conversation_id

            if _is_native_harness(conversation_id):
                resource_registry.note_session_turn_started(conversation_id)

            _seq = _ingest_next_seq.get(conversation_id, 0)
            _ingest_next_seq[conversation_id] = _seq + 1
            _cond = _ingest_cond.get(conversation_id)
            if _cond is None:
                _cond = asyncio.Condition()
                _ingest_cond[conversation_id] = _cond
            async with _cond:
                while _ingest_now_serving.get(conversation_id, 0) != _seq:
                    await _cond.wait()
            try:
                _raw_content = message_body.get("content")
                if isinstance(_raw_content, list):
                    message_body["content"] = await _resolve_forwarded_message_content(
                        _raw_content,
                        session_id=conversation_id,
                        server_client=server_client,
                    )

                if conversation_id in _active_turns:
                    _native = _is_native_harness(conversation_id)
                    _awaiting_approval = pending_approvals.has_pending(conversation_id)
                    _can_forward = (
                        not _native
                        and not _awaiting_approval
                        and conversation_id in _live_response_id
                    )
                    if _can_forward:
                        message_body["injection_id"] = f"inj_{uuid.uuid4().hex[:16]}"
                    _logger.info(
                        "post_session_events: buffering message for active turn conv=%s "
                        "native=%s awaiting_approval=%s",
                        conversation_id,
                        _native,
                        _awaiting_approval,
                        extra={"session_id": conversation_id},
                    )
                    _session_message_buffers.setdefault(
                        conversation_id,
                        [],
                    ).append(message_body)
                    if _can_forward and process_manager is not None:
                        try:
                            _hc = await process_manager.get_client(conversation_id, "any")
                            _injection_resp = await _hc.post(
                                f"/v1/sessions/{conversation_id}/events",
                                json=message_body,
                                timeout=5.0,
                            )
                            if _injection_resp.status_code >= 400:
                                _logger.warning(
                                    "post_session_events: mid-turn injection forward rejected "
                                    "conv=%s status=%s body=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                    _response_body_preview(_injection_resp),
                                    extra={"session_id": conversation_id},
                                )
                            else:
                                _logger.debug(
                                    "post_session_events: mid-turn injection forward accepted "
                                    "conv=%s status=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                    extra={"session_id": conversation_id},
                                )
                        except (httpx.HTTPError, RuntimeError, asyncio.TimeoutError):
                            _logger.debug(
                                "mid-turn injection forward failed for %s; "
                                "LLM will see message on next turn",
                                conversation_id,
                                exc_info=True,
                                extra={"session_id": conversation_id},
                            )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "buffered",
                            "detail": ("Message buffered; active turn will process it."),
                        },
                    )

                if _session_harness_name(conversation_id) == "claude-native":
                    pending_bridge_dir = None
                    has_queued_messages = bool(_session_message_buffers.get(conversation_id))
                    if not has_queued_messages:
                        pending_bridge_dir = await _pending_claude_prompt_bridge_dir(
                            conversation_id
                        )
                    if has_queued_messages or pending_bridge_dir is not None:
                        if conversation_id not in _session_histories:
                            _session_histories[conversation_id] = await _load_history_as_input(
                                conversation_id,
                                drop_item_id=message_body.get("persisted_item_id"),
                            )
                        _session_message_buffers.setdefault(conversation_id, []).append(
                            message_body
                        )
                        _start_claude_prompt_waiter(conversation_id, pending_bridge_dir)
                        _logger.info(
                            "post_session_events: buffering message for pending Claude prompt "
                            "conv=%s",
                            conversation_id,
                            extra={"session_id": conversation_id},
                        )
                        return JSONResponse(
                            status_code=202,
                            content={
                                "status": "buffered",
                                "detail": "Message buffered until the pending prompt is resolved.",
                            },
                        )

                new_item = {
                    "type": "message",
                    "role": message_body.get("role", "user"),
                    "content": message_body.get("content", []),
                }
                if conversation_id in _session_histories:
                    _session_histories[conversation_id].append(new_item)
                else:
                    persisted_item_id = message_body.get("persisted_item_id")
                    loaded = await _load_history_as_input(
                        conversation_id,
                        drop_item_id=persisted_item_id,
                    )
                    loaded.append(new_item)
                    _session_histories[conversation_id] = loaded

                _begin_turn_slot(conversation_id)
                _logger.info(
                    "post_session_events: starting background turn conv=%s",
                    conversation_id,
                    extra={"session_id": conversation_id},
                )

                _publish_turn_status(conversation_id, "running")

                if stream:
                    response = await _stream_message_to_harness(message_body, conversation_id)
                    if not isinstance(response, StreamingResponse):
                        _on_proxy_stream_end(
                            conversation_id,
                            error=_harness_error_response_error(response),
                        )
                    return response

                _turn_task = asyncio.create_task(
                    _run_turn_bg(message_body, conversation_id),
                    name=f"turn-{conversation_id}",
                )
                _active_turns[conversation_id] = _turn_task
                _turn_task.add_done_callback(
                    _background_tasks.discard,
                )
                _background_tasks.add(_turn_task)

                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "accepted",
                        "detail": "Turn started.",
                    },
                )
            finally:
                async with _cond:
                    _ingest_now_serving[conversation_id] = _seq + 1
                    _cond.notify_all()

        if body_type == "interrupt":
            _cancel_claude_prompt_waiter(conversation_id)
            _harness = _session_harness_name(conversation_id)
            resource_registry.note_terminal_control_request(conversation_id, "interrupt")
            _interrupt_resp = await _native_interrupt_runner.interrupt(_harness, conversation_id)
            if _interrupt_resp is not None:
                return _interrupt_resp
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "external_session_status":
            data = body.get("data") if isinstance(body, dict) else None
            status = data.get("status") if isinstance(data, dict) else None
            forwarded_output = data.get("output") if isinstance(data, dict) else None
            output = forwarded_output if isinstance(forwarded_output, str) else None
            delivery_ack: _SubagentDeliveryAck | None = None
            recovered_entry: _SubagentWorkEntry | None = None
            terminal_status = None
            if status in ("idle", "failed"):
                recovered_entry = get_subagent_work(conversation_id)
                turn_outcome = data.get("turn_outcome") if isinstance(data, dict) else None
                if turn_outcome in ("completed", "cancelled", "failed"):
                    recovered_entry = await _ensure_subagent_work_entry(conversation_id)
                    terminal_status = turn_outcome
                    if turn_outcome == "cancelled" and recovered_entry is not None:
                        recovered_entry.cancellation_confirmed = True
                elif (
                    status == "idle"
                    and recovered_entry is not None
                    and recovered_entry.status == "cancelled"
                    and recovered_entry.cancellation_confirmed
                ):
                    # A bare idle retry cannot overwrite a confirmed abort
                    # while its parent inbox is still unavailable.
                    terminal_status = "cancelled"
                    output = recovered_entry.output
            if status in ("running", "waiting", "idle", "failed"):
                # Forwarders report these edges straight to the server, so record
                # them here too; the idle watchdog reads them for native turns.
                _native_pane_status[conversation_id] = status
                resource_registry.note_external_session_status(conversation_id, status)
                child_status = (
                    "idle" if terminal_status == "completed" else terminal_status or status
                )
                _fan_out_child_delta_to_parent(
                    conversation_id,
                    {"type": "session.status", "status": child_status},
                    latest_assistant_text=output,
                    allow_history_preview_fallback=False,
                )
            turn_completed = data.get("turn_completed") if isinstance(data, dict) else None
            interrupt_pending = False
            interrupt_work_id: str | None = None
            if status == "idle" and terminal_status is None and turn_completed is not True:
                # An unconfirmed idle following an interrupt settles the
                # dispatch: the turn stopped early, so report ``cancelled``
                # with whatever output the edge carried instead of guessing
                # ``completed`` — or discarding a genuine result. Resolve the
                # pending interrupt only when it belongs to the dispatch now
                # registered: a stale record (its dispatch exited, or a new send
                # reused this child) must not capture this idle — for a legacy
                # harness that idle is the NEW dispatch's completion.
                _current_entry = get_subagent_work(conversation_id)
                interrupt_pending, interrupt_work_id = (
                    _native_interrupt_runner.resolve_pending_interrupt(
                        conversation_id,
                        _current_entry.work_id if _current_entry is not None else None,
                    )
                )
            ambiguous_idle = (
                status == "idle"
                and terminal_status is None
                and turn_completed is not True
                and not interrupt_pending
                and _native_turn_outcome_is_forwarder_confirmed(conversation_id)
            )
            if terminal_status is not None:
                _native_interrupt_runner.clear_pending_interrupt(conversation_id)
                if terminal_status == "cancelled":
                    output = output or "[System: sub-agent interrupted]"
                elif terminal_status == "failed":
                    output = output or "Error: native sub-agent turn failed"
                delivery_ack = _mark_subagent_terminal_and_wake(
                    conversation_id,
                    status=terminal_status,
                    output=output if output is not None else "",
                )
            elif ambiguous_idle:
                # This harness's forwarder marks genuine turn completions
                # (``turn_completed``), so a bare quiescence idle proves
                # nothing about the turn's outcome: record the pane status
                # above but settle no outcome. Mapping it to ``completed``
                # reported aborted turns as successes.
                entry = get_subagent_work(conversation_id)
                if entry is None or entry.status not in _SUBAGENT_TERMINAL_STATUSES:
                    return Response(status_code=204)
                # An already-settled outcome may still await parent delivery
                # (the forwarder's 503-retry contract): retry the recorded
                # result as-is. Re-reporting it as a fresh terminal edge would
                # let a provisional launch-timeout ``failed`` pass for the
                # child's own report and spend its flag.
                delivery_ack = _deliver_subagent_completion(entry)
                if delivery_ack.delivered_now:
                    _schedule_subagent_wake(entry)
            else:
                if status in ("idle", "failed"):
                    recovered_entry = await _ensure_subagent_work_entry(conversation_id)
                if status == "idle" and interrupt_pending:
                    # Interrupt with no confirming edge, resolved to the CURRENT
                    # dispatch (``resolve_pending_interrupt`` only reports pending
                    # when the recorded ``work_id`` matches). Bound to that
                    # dispatch so it cannot cancel a newer send.
                    delivery_ack = _mark_subagent_terminal_and_wake(
                        conversation_id,
                        status="cancelled",
                        output=output,
                        only_if_work_id=interrupt_work_id,
                    )
                elif status == "idle":
                    _native_interrupt_runner.clear_pending_interrupt(conversation_id)
                    delivery_ack = _mark_subagent_terminal_and_wake(
                        conversation_id,
                        status="completed",
                        output=output if output is not None else "",
                    )
                elif status == "failed":
                    _native_interrupt_runner.clear_pending_interrupt(conversation_id)
                    delivery_ack = _mark_subagent_terminal_and_wake(
                        conversation_id,
                        status="failed",
                        output=output or "Error: native sub-agent turn failed",
                    )
            if delivery_ack is not None:
                if (
                    delivery_ack.entry is not None
                    and delivery_ack.reason == _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX
                    and await _parent_is_nested_subagent(delivery_ack.entry)
                ):
                    # Acknowledge instead of asking the forwarder to poll. The
                    # entry stays terminal and undelivered; it is handed over
                    # only if this runner ever creates the parent's inbox.
                    return Response(status_code=204)
                is_known = (
                    conversation_id in _session_sub_agent_names or recovered_entry is not None
                )
                not_confirmed = _subagent_delivery_not_confirmed_response(
                    delivery_ack,
                    is_runner_known_subagent=is_known,
                )
                if not_confirmed is not None:
                    return not_confirmed
            return Response(status_code=204)

        if body_type == "stop_session":
            resource_registry.note_terminal_control_request(conversation_id, "stop_session")
            _cancel_claude_prompt_waiter(conversation_id)
            _harness = _session_harness_name(conversation_id)
            _stop_resp = await _native_interrupt_runner.stop(_harness, conversation_id)
            if _stop_resp is not None:
                return _stop_resp
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "effort_change":
            harness = _session_harness_name(conversation_id)
            effort = body.get("effort") if isinstance(body, dict) else None
            if (invalid := _invalid_effort_response(effort)) is not None:
                return invalid
            if harness == "codex-native":
                # The native handler remembers the applied effort only after
                # Codex confirms it; a refused reset must retain the old value.
                server_rolls_back = body.get("rollback_on_refusal") is True
                response = await _handle_codex_native_settings_update(
                    conversation_id,
                    {"effort": effort},
                    # An older server keeps a refused selection for the next turn.
                    legacy_server=not server_rolls_back,
                )
                if server_rolls_back and not (
                    200 <= response.status_code < 300 or response.status_code == 504
                ):
                    # Confirm the refusal was not kept, so the server may roll it back.
                    return _acknowledge_settings_rollback(response)
                return response
            # In-process harnesses apply the effort on their next turn, from the
            # forwarded turn body (see ``_turn_reasoning``).
            if effort:
                _session_reasoning_effort[conversation_id] = effort
            else:
                _session_reasoning_effort.pop(conversation_id, None)
            if harness in ("claude-native", "pi-native", "devin-native"):
                if harness == "pi-native":
                    return await _handle_pi_native_effort_change(
                        conversation_id,
                        effort,
                    )
                if harness == "devin-native":
                    return await _handle_devin_native_effort_change(
                        conversation_id,
                        effort,
                    )
                return await _handle_claude_native_effort_change(
                    conversation_id,
                    effort,
                )
            return Response(status_code=204)

        if body_type == "model_change":
            harness = _session_harness_name(conversation_id)
            if harness in (
                "claude-native",
                "codex-native",
                "cursor-native",
                "opencode-native",
                "kiro-native",
                "devin-native",
                "pi-native",
            ):
                model = body.get("model") if isinstance(body, dict) else None
                if model is not None and not isinstance(model, str):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'model' must be a string or null",
                        },
                    )
                if harness == "codex-native":
                    if model is None or not model.strip():
                        return Response(status_code=204)
                    settings: _JsonObject = {"model": model.strip()}
                    if "effort" in body:
                        effort = body["effort"]
                        if (invalid := _invalid_effort_response(effort)) is not None:
                            return invalid
                        settings["effort"] = effort
                    response = await _handle_codex_native_settings_update(
                        conversation_id,
                        settings,
                        legacy_server=body.get("rollback_on_refusal") is not True,
                    )
                    if "effort" in settings and 200 <= response.status_code < 300:
                        return JSONResponse({"codex_settings_applied": True})
                    return response
                if harness == "cursor-native":
                    return await _handle_cursor_native_model_change(
                        conversation_id,
                        model,
                    )
                if harness == "opencode-native":
                    return await _handle_opencode_native_model_change(
                        conversation_id,
                        model,
                    )
                if harness == "kiro-native":
                    return await _handle_kiro_native_model_change(
                        conversation_id,
                        model,
                    )
                if harness == "devin-native":
                    return await _handle_devin_native_model_change(
                        conversation_id,
                        model,
                    )
                if harness == "pi-native":
                    return await _handle_pi_native_model_change(
                        conversation_id,
                        model,
                    )
                return await _handle_claude_native_model_change(
                    conversation_id,
                    model,
                )
            return Response(status_code=204)

        if body_type == "plan_mode_change":
            harness = _session_harness_name(conversation_id)
            if harness == "codex-native":
                enabled = body.get("enabled") if isinstance(body, dict) else None
                if not isinstance(enabled, bool):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'enabled' must be a boolean",
                        },
                    )
                return await _handle_codex_native_plan_mode_change(
                    conversation_id,
                    enabled=enabled,
                )
            return Response(status_code=204)

        if body_type == "permission_mode_change":
            harness = _session_harness_name(conversation_id)
            if harness in ("claude-native", "devin-native"):
                mode = body.get("permission_mode") if isinstance(body, dict) else None
                if mode is not None and not isinstance(mode, str):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'permission_mode' must be a string or null",
                        },
                    )
                if harness == "devin-native":
                    return await _handle_devin_native_permission_mode_change(
                        conversation_id,
                        mode,
                    )
                return await _handle_claude_native_permission_mode_change(
                    conversation_id,
                    mode,
                )
            return Response(status_code=204)

        if body_type == "btw_dismiss":
            harness = _session_harness_name(conversation_id)
            if harness == "claude-native":
                return await _handle_claude_native_btw_dismiss(conversation_id)
            return Response(status_code=204)

        if body_type == "codex_approval_mode_change":
            harness = _session_harness_name(conversation_id)
            if harness == "codex-native":
                mode = body.get("approval_mode") if isinstance(body, dict) else None
                if not isinstance(mode, str) or not mode:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'approval_mode' must be a non-empty string",
                        },
                    )
                return await _handle_codex_native_approval_mode_change(
                    conversation_id,
                    mode,
                )
            return Response(status_code=204)

        codex_goal_response = await codex_goal_runner.handle_event(
            conversation_id,
            body_type,
            body,
            session_harness_name=_session_harness_name,
        )
        if codex_goal_response is not None:
            return codex_goal_response

        if body_type == "compact":
            if _session_harness_name(conversation_id) == "claude-native":
                return await _handle_claude_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "codex-native":
                return await _handle_codex_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "opencode-native":
                return await _handle_opencode_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "cursor-native":
                return await _handle_cursor_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "pi-native":
                return await _handle_pi_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "hermes-native":
                return await _handle_hermes_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "qwen-native":
                return await _handle_qwen_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "devin-native":
                return await _handle_devin_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "claude-sdk":
                return await _handle_claude_sdk_compact(conversation_id)
            return Response(status_code=204)

        if body_type == "clear":
            if _session_harness_name(conversation_id) == "opencode-native":
                return await _handle_opencode_native_clear(conversation_id)
            return Response(status_code=204)

        if body_type == "cost_approval_popup":
            elicitation_id = body.get("elicitation_id") if isinstance(body, dict) else None
            message = body.get("message") if isinstance(body, dict) else None
            policy_name = body.get("policy_name") if isinstance(body, dict) else None
            if not isinstance(elicitation_id, str) or not elicitation_id:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_input",
                        "detail": "Body 'elicitation_id' must be a non-empty string",
                    },
                )
            popup_message = (
                message if isinstance(message, str) and message else "Approval required"
            )
            popup_policy_name = (
                policy_name if isinstance(policy_name, str) and policy_name else None
            )
            harness = _session_harness_name(conversation_id)
            if harness == "claude-native":
                return await _handle_claude_native_cost_popup(
                    conversation_id, elicitation_id, popup_message, popup_policy_name
                )
            if harness == "codex-native":
                return await _handle_codex_native_cost_popup(
                    conversation_id, elicitation_id, popup_message, popup_policy_name
                )
            if harness == "opencode-native":
                return await _handle_opencode_native_cost_popup(
                    conversation_id, elicitation_id, popup_message, popup_policy_name
                )
            return Response(status_code=204)

        if body_type == "policy_blocked_notice":
            if _session_harness_name(conversation_id) == "opencode-native":
                message = body.get("message") if isinstance(body, dict) else None
                policy_name = body.get("policy_name") if isinstance(body, dict) else None
                return await _handle_opencode_native_blocked_notice(
                    conversation_id,
                    message if isinstance(message, str) and message else "Blocked by policy.",
                    policy_name if isinstance(policy_name, str) and policy_name else None,
                )
            return Response(status_code=204)

        if body_type == "approval":
            _data = body.get("data") or body
            _elicit_action = _data.get("action", "")
            # ``content`` is the person's answer when the prompt asked for
            # more than consent (an MCP ``requestedSchema``). Dropping it here
            # is what used to make the awaiting caller invent one.
            _elicit_content = _data.get("content")
            pending_approvals.resolve(
                _data.get("elicitation_id", ""),
                _elicit_action == "accept",
                _elicit_content if isinstance(_elicit_content, dict) else None,
            )
            if _elicit_action == "decline":
                try:
                    _int_client = await process_manager.get_client(conversation_id, "any")
                    await _int_client.post(
                        f"/v1/sessions/{conversation_id}/events",
                        json={"type": "interrupt"},
                        timeout=5.0,
                    )
                except Exception:  # noqa: BLE001 — best-effort; deny path continues
                    pass
            body = {**_data, "type": "approval"}

        try:
            harness_client = await process_manager.get_client(conversation_id, "any")
        except NoLiveHarnessError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no_live_harness",
                    "detail": "no harness subprocess is running for this conversation",
                },
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "no_harness",
                    "detail": _client_safe_error_detail(exc, context="harness lookup"),
                },
            )
        try:
            resp = await harness_client.post(
                f"/v1/sessions/{conversation_id}/events",
                json=body,
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={
                    "error": "harness_forward_failed",
                    "detail": _client_safe_error_detail(exc, context="harness event forward"),
                    "event_type": body_type,
                },
            )
        return _forward_harness_response(resp)

    async def _ensure_session_registered(session_id: str) -> None:
        if session_id in _session_start_cache:
            return
        snapshot = await _session_snapshot(session_id)
        _session_start_cache[session_id] = snapshot.created_at
        # Only memoize a workspace the server actually returned; a failed
        # fetch is re-resolved lazily by _session_workspace_value.
        if snapshot.ok:
            _session_workspace_cache[session_id] = snapshot.workspace

    async def _resolve_session_spec_entry(session_id: str) -> _SpecEntry | None:
        if session_id in _session_spec_cache:
            return _session_spec_cache[session_id]
        if spec_resolver is None:
            _session_spec_cache[session_id] = None
            return None
        lock = _session_spec_locks.setdefault(session_id, asyncio.Lock())
        generation = _session_cache_generation(session_id)
        async with lock:
            if session_id in _session_spec_cache:
                return _session_spec_cache[session_id]
            snapshot = await _session_snapshot(session_id)
            if not snapshot.ok:
                raise OmnigentError(
                    f"session spec resolver: GET /v1/sessions/{session_id} "
                    f"failed with HTTP {snapshot.status_code}",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            agent_id = snapshot.agent_id
            if not agent_id:
                raise OmnigentError(
                    f"session spec resolver: session {session_id!r} has no agent_id",
                    code=ErrorCode.NOT_FOUND,
                )
            spec_entry = await spec_resolver(agent_id, session_id)
            if spec_entry is None:
                # The session still references agent_id, but its stored bundle
                # no longer resolves (deleted or rebound out from under the
                # live session). A session-lifecycle condition, not a generic
                # NOT_FOUND: the distinct code lets the terminal-ensure and
                # turn-dispatch paths surface a lifecycle reason instead of a
                # runner startup fault.
                raise OmnigentError(
                    f"session spec resolver: agent {agent_id!r} for "
                    f"session {session_id!r} was not found",
                    code=ErrorCode.SESSION_AGENT_MISSING,
                )
            sub_agent_name = snapshot.sub_agent_name
            # Root the child at its own bundle dir. Always wrapped, so an
            # unresolvable workdir registers nothing rather than falling back
            # to the parent's bundle root.
            if sub_agent_name:
                _session_sub_agent_names[session_id] = sub_agent_name
                if _unwrap_resolved_spec(spec_entry) is not None:
                    sub_entry = _native_runtime._resolve_sub_agent_spec_entry(
                        spec_entry, sub_agent_name
                    )
                    if sub_entry is None:
                        _warn_unresolved_sub_agent(session_id, sub_agent_name)
                        _session_sub_agent_resolved[session_id] = False
                    else:
                        spec_entry = sub_entry
                        _session_sub_agent_resolved[session_id] = True
            if _session_cache_generation_is_current(session_id, generation):
                _session_spec_cache[session_id] = spec_entry
            return spec_entry

    async def _resolve_session_agent_spec(session_id: str) -> AgentSpec | None:
        entry = await _resolve_session_spec_entry(session_id)
        return _unwrap_spec_entry(entry)

    async def _resolve_session_agent_spec_or_none(session_id: str) -> AgentSpec | None:
        """Resolve the session agent spec, tolerating resolution failure.

        The cursor/opencode/kimi launch arms swallow ``OmnigentError`` and
        continue without a spec; this is their spec resolver for
        ``_launch_native_terminal``.
        """
        try:
            return await _resolve_session_agent_spec(session_id)
        except OmnigentError:
            return None

    async def _resolve_session_skills(session_id: str) -> list[SkillSpec]:
        cached = _session_skills_cache.get(session_id)
        if cached is not None:
            expires_at, cached_skills = cached
            if time.monotonic() < expires_at:
                return cached_skills
        entry = await _resolve_session_spec_entry(session_id)
        spec = _unwrap_resolved_spec(entry) if entry is not None else None
        if spec is None:
            return []
        workspace = await _session_workspace_value(session_id)
        candidate_roots = [
            Path(workspace).resolve()
            if workspace is not None
            else (runner_workspace.resolve() if runner_workspace is not None else None),
            _resolved_spec_workdir(entry),
        ]
        roots: list[Path] = []
        for candidate in candidate_roots:
            if candidate is None:
                continue
            resolved = candidate.resolve()
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            roots.append(Path.cwd())

        skills = await asyncio.to_thread(
            resolve_session_skills, spec, tuple(roots), _resolved_spec_workdir(entry)
        )
        _session_skills_cache[session_id] = (
            time.monotonic() + _SESSION_SKILLS_CACHE_TTL_SECONDS,
            skills,
        )
        return skills

    _resource_routes = register_resource_routes(
        app,
        _antigravity_terminal_ensure_locks=_antigravity_terminal_ensure_locks,
        _claude_terminal_ensure_locks=_claude_terminal_ensure_locks,
        _codex_terminal_ensure_locks=_codex_terminal_ensure_locks,
        _cursor_terminal_ensure_locks=_cursor_terminal_ensure_locks,
        _devin_terminal_ensure_locks=_devin_terminal_ensure_locks,
        _discard_comment_relay=_discard_comment_relay,
        _ensure_comment_relay_started=_ensure_comment_relay_started,
        _ensure_session_registered=_ensure_session_registered,
        _goose_terminal_ensure_locks=_goose_terminal_ensure_locks,
        _hermes_terminal_ensure_locks=_hermes_terminal_ensure_locks,
        _kimi_terminal_ensure_locks=_kimi_terminal_ensure_locks,
        _kiro_terminal_ensure_locks=_kiro_terminal_ensure_locks,
        _native_pane_names=_native_pane_names,
        _opencode_terminal_ensure_locks=_opencode_terminal_ensure_locks,
        _pi_terminal_ensure_locks=_pi_terminal_ensure_locks,
        _publish_event=_publish_event,
        _qwen_terminal_ensure_locks=_qwen_terminal_ensure_locks,
        _record_session_claude_launch_config=_record_session_claude_launch_config,
        _repl_terminal_ensure_locks=_repl_terminal_ensure_locks,
        _repop_pending_cost_popup_on_attach=_repop_pending_cost_popup_on_attach,
        _resolve_session_agent_spec=_resolve_session_agent_spec,
        _resolve_session_agent_spec_or_none=_resolve_session_agent_spec_or_none,
        _resolve_session_claude_launch_config=_resolve_session_claude_launch_config,
        _resolve_session_fs_registry=_resolve_session_fs_registry,
        _resolve_session_spec_entry=_resolve_session_spec_entry,
        _resp_to_conv=_resp_to_conv,
        _search_registry_for_root=_search_registry_for_root,
        _session_comment_relays=_session_comment_relays,
        auth_token_factory=auth_token_factory,
        filesystem_registry=filesystem_registry,
        resource_registry=resource_registry,
        server_client=server_client,
        terminal_registry=terminal_registry,
    )
    _ensure_native_terminal_for_turn = _resource_routes.ensure_native_terminal_for_turn
    _require_os_env = _resource_routes.require_os_env
    _resolve_conversation_id = _resource_routes.resolve_conversation_id

    _native_controls = build_native_controls(
        _active_turns=_active_turns,
        _background_tasks=_background_tasks,
        _begin_turn_slot=_begin_turn_slot,
        _claude_model_options_rows=_claude_model_options_rows,
        _codex_native_bridge_state_for_session=_codex_native_bridge_state_for_session,
        _ensure_comment_relay_started=_ensure_comment_relay_started,
        _ensure_native_terminal_for_turn=_ensure_native_terminal_for_turn,
        _fetch_session_model_override=_fetch_session_model_override,
        _ingest_cond=_ingest_cond,
        _ingest_next_seq=_ingest_next_seq,
        _ingest_now_serving=_ingest_now_serving,
        _load_history_as_input=_load_history_as_input,
        _model_dialog_watchers=_model_dialog_watchers,
        _native_cost_popup_config_file=_native_cost_popup_config_file,
        _native_pane_status=_native_pane_status,
        _publish_event=_publish_event,
        _publish_turn_status=_publish_turn_status,
        _resolve_session_agent_spec=_resolve_session_agent_spec,
        _resolve_session_claude_launch_config=_resolve_session_claude_launch_config,
        _run_turn_bg=_run_turn_bg,
        _sdk_compact_inprogress=_sdk_compact_inprogress,
        _session_cursor_model_names=_session_cursor_model_names,
        _session_harness_name=_session_harness_name,
        _session_histories=_session_histories,
        _session_message_buffers=_session_message_buffers,
        _session_reasoning_effort=_session_reasoning_effort,
        _session_spec_cache=_session_spec_cache,
        resource_registry=resource_registry,
        server_client=server_client,
    )
    _codex_native_model_options = _native_controls.codex_native_model_options
    _handle_claude_native_btw_dismiss = _native_controls.handle_claude_native_btw_dismiss
    _handle_claude_native_compact = _native_controls.handle_claude_native_compact
    _handle_claude_native_cost_popup = _native_controls.handle_claude_native_cost_popup
    _handle_claude_native_effort_change = _native_controls.handle_claude_native_effort_change
    _handle_claude_native_model_change = _native_controls.handle_claude_native_model_change
    _handle_claude_native_permission_mode_change = (
        _native_controls.handle_claude_native_permission_mode_change
    )
    _handle_claude_sdk_compact = _native_controls.handle_claude_sdk_compact
    _handle_codex_native_approval_mode_change = (
        _native_controls.handle_codex_native_approval_mode_change
    )
    _handle_codex_native_compact = _native_controls.handle_codex_native_compact
    _handle_codex_native_cost_popup = _native_controls.handle_codex_native_cost_popup
    _handle_codex_native_plan_mode_change = _native_controls.handle_codex_native_plan_mode_change
    _handle_codex_native_settings_update = _native_controls.handle_codex_native_settings_update
    _handle_cursor_native_compact = _native_controls.handle_cursor_native_compact
    _handle_cursor_native_model_change = _native_controls.handle_cursor_native_model_change
    _handle_devin_native_compact = _native_controls.handle_devin_native_compact
    _handle_devin_native_effort_change = _native_controls.handle_devin_native_effort_change
    _handle_devin_native_model_change = _native_controls.handle_devin_native_model_change
    _handle_devin_native_permission_mode_change = (
        _native_controls.handle_devin_native_permission_mode_change
    )
    _handle_hermes_native_compact = _native_controls.handle_hermes_native_compact
    _handle_kiro_native_model_change = _native_controls.handle_kiro_native_model_change
    _handle_opencode_native_blocked_notice = _native_controls.handle_opencode_native_blocked_notice
    _handle_opencode_native_clear = _native_controls.handle_opencode_native_clear
    _handle_opencode_native_compact = _native_controls.handle_opencode_native_compact
    _handle_opencode_native_cost_popup = _native_controls.handle_opencode_native_cost_popup
    _handle_opencode_native_model_change = _native_controls.handle_opencode_native_model_change
    _handle_pi_native_compact = _native_controls.handle_pi_native_compact
    _handle_pi_native_effort_change = _native_controls.handle_pi_native_effort_change
    _handle_pi_native_model_change = _native_controls.handle_pi_native_model_change
    _handle_qwen_native_compact = _native_controls.handle_qwen_native_compact
    _is_sdk_compact_body = _native_controls.is_sdk_compact_body
    _opencode_native_model_options = _native_controls.opencode_native_model_options
    _teardown_session_terminals = _native_controls.teardown_session_terminals

    register_model_option_routes(
        app,
        _claude_model_options_rows=_claude_model_options_rows,
        _codex_native_model_options=_codex_native_model_options,
        _opencode_native_model_options=_opencode_native_model_options,
        _resolve_session_agent_spec=_resolve_session_agent_spec,
        _resolve_session_claude_launch_config=_resolve_session_claude_launch_config,
        _resolve_session_skills=_resolve_session_skills,
        _session_cursor_model_names=_session_cursor_model_names,
        _session_harness_name=_session_harness_name,
        _session_spec_cache=_session_spec_cache,
        server_client=server_client,
    )

    @app.post("/v1/sessions/{session_id}/resources/environments/{environment_id}/shell")
    async def run_environment_shell(
        session_id: str,
        environment_id: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            _run_os_env_async,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        body = await request.json()
        command = body.get("command")
        if not command or not isinstance(command, str):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'command' is required",
                    }
                },
            )
        timeout = body.get("timeout")
        if timeout is not None and not isinstance(timeout, int):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'timeout' must be an integer",
                    }
                },
            )
        result = await _run_os_env_async(
            env.shell,
            command,
            timeout,
        )
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.shell_result",
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "cwd": result.get("cwd"),
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/{resource_id}")
    async def get_session_resource(
        session_id: str,
        resource_id: str,
    ) -> JSONResponse:
        resource = resource_registry.get_resource(
            session_id,
            resource_id,
        )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Resource {resource_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    def _clear_session_agent_caches(session_id: str, agent_id: str | None = None) -> None:
        _session_spec_cache.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_agent_revisions.pop(session_id, None)
        _session_harness_overrides.pop(session_id, None)
        # Bump so any in-flight fill discards its write rather than reinstating it.
        _session_cache_generations[session_id] = _session_cache_generations.get(session_id, 0) + 1
        _session_snapshot_cache.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_cursor_model_names.pop(session_id, None)
        _drop_session_claude_launch_config(session_id)
        _session_tool_schemas.pop(session_id, None)
        _session_mcp_spec_hash.pop(session_id, None)
        _instruction_delivery_warned.pop(session_id, None)
        _session_sub_agent_resolved.pop(session_id, None)
        if agent_id:
            _spec_cache.pop(agent_id, None)

    async def _invalidate_session_agent_state(session_id: str, new_agent_id: str | None) -> None:
        """Clear all agent-derived caches and release the harness subprocess.

        Both dispatch paths (background ``_run_turn_bg_setup_and_stream`` and
        direct-stream ``_stream_message_to_harness``) call this shared routine
        on an in-conversation agent switch, so the eviction scope cannot
        diverge between the two paths.
        """
        _clear_session_agent_caches(session_id, new_agent_id)
        if process_manager is not None:
            await process_manager.release(session_id)

    async def _sync_session_agent(
        session_id: str, agent_id: str | None, revision: str | None
    ) -> None:
        """Reset agent-derived state when a turn's agent or its bundle revision changed.

        The server stamps ``agent_revision`` on each turn, so a reinstall or an
        edit made through any server replica reaches this session's next turn. A
        spec cached by a turn without a revision is rebuilt when one arrives.
        """
        prior_id = _session_agent_ids.get(session_id)
        prior_revision = _session_agent_revisions.get(session_id)
        if agent_id and prior_id is not None and prior_id != agent_id:
            _logger.info(
                "agent switch detected for %s: %s -> %s; resetting session caches",
                session_id,
                prior_id,
                agent_id,
                extra={"session_id": session_id},
            )
            await _invalidate_session_agent_state(session_id, agent_id)
        # ponytail: distrusting a spec cached without a revision can drop a fresh shared
        # per-agent spec (one extra resolve); track per-agent revisions if that shows up.
        elif (
            revision
            and revision != prior_revision
            and (
                prior_revision is not None
                or session_id in _session_spec_cache
                or (agent_id is not None and agent_id in _spec_cache)
            )
        ):
            _logger.info(
                "agent %s changed for %s; resetting session caches",
                agent_id,
                session_id,
                extra={"session_id": session_id},
            )
            # Same agent, new bundle: rebuild the spec like an MCP edit does,
            # keeping the harness process and the harness the server pinned.
            override = _session_harness_overrides.get(session_id)
            _clear_session_agent_caches(session_id, agent_id)
            if override is not None:
                _session_harness_overrides[session_id] = override
        if agent_id:
            _session_agent_ids[session_id] = agent_id
        if revision:
            _session_agent_revisions[session_id] = revision

    @app.delete("/v1/sessions/{session_id}/resources")
    async def cleanup_session_resources(
        session_id: str,
    ) -> JSONResponse:
        _required_terminal_exit_errors.pop(session_id, None)
        _codex_terminal_ensure_locks.pop(session_id, None)
        _claude_terminal_ensure_locks.pop(session_id, None)
        _pi_terminal_ensure_locks.pop(session_id, None)
        _cursor_terminal_ensure_locks.pop(session_id, None)
        _kiro_terminal_ensure_locks.pop(session_id, None)
        _antigravity_terminal_ensure_locks.pop(session_id, None)
        _goose_terminal_ensure_locks.pop(session_id, None)
        _qwen_terminal_ensure_locks.pop(session_id, None)
        _kimi_terminal_ensure_locks.pop(session_id, None)
        _hermes_terminal_ensure_locks.pop(session_id, None)
        _repl_terminal_ensure_locks.pop(session_id, None)
        if process_manager is not None:
            await process_manager.release(session_id)
        await resource_registry.cleanup_session(session_id)
        await _delete_native_bridge_dirs(
            server_client=server_client,
            session_id=session_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.resources.cleaned",
                "cleaned": True,
            },
        )

    @app.post("/v1/sessions/{session_id}/reset-state")
    async def reset_session_state(session_id: str) -> JSONResponse:
        _required_terminal_exit_errors.pop(session_id, None)
        _codex_terminal_ensure_locks.pop(session_id, None)
        _claude_terminal_ensure_locks.pop(session_id, None)
        _pi_terminal_ensure_locks.pop(session_id, None)
        _cursor_terminal_ensure_locks.pop(session_id, None)
        _kiro_terminal_ensure_locks.pop(session_id, None)
        _antigravity_terminal_ensure_locks.pop(session_id, None)
        _goose_terminal_ensure_locks.pop(session_id, None)
        _qwen_terminal_ensure_locks.pop(session_id, None)
        _kimi_terminal_ensure_locks.pop(session_id, None)
        _hermes_terminal_ensure_locks.pop(session_id, None)
        _repl_terminal_ensure_locks.pop(session_id, None)
        await _teardown_session_terminals(session_id)
        if process_manager is not None:
            await process_manager.release(session_id)
        await resource_registry.cleanup_session(session_id)
        _clear_session_agent_caches(session_id, _session_agent_ids.get(session_id))
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.state_reset",
                "reset": True,
            },
        )

    @app.post("/v1/sessions/{session_id}/agent-cache/reset")
    async def reset_session_agent_cache(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        agent_id = body.get("agent_id") if isinstance(body, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            agent_id = _session_agent_ids.get(session_id)
        if not agent_id:
            with contextlib.suppress(OmnigentError, httpx.HTTPError, RuntimeError):
                snapshot = await _session_snapshot(session_id)
                if snapshot.ok and snapshot.agent_id:
                    agent_id = snapshot.agent_id

        _clear_session_agent_caches(session_id, agent_id)
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "agent_id": agent_id,
                "object": "session.agent_cache_reset",
                "reset": True,
            },
        )

    register_mcp_routes(
        app,
        _publish_event=_publish_event,
        _recover_undrained_subagent_results=_recover_undrained_subagent_results,
        _resolve_conversation_id=_resolve_conversation_id,
        _resolve_session_agent_spec_or_none=_resolve_session_agent_spec_or_none,
        _resolve_session_spec_entry=_resolve_session_spec_entry,
        _session_agent_ids=_session_agent_ids,
        _session_async_tasks=_session_async_tasks,
        _session_harness_name=_session_harness_name,
        _session_inboxes=_session_inboxes,
        _session_runtime_cwd=_session_runtime_cwd,
        _session_spec_cache=_session_spec_cache,
        filesystem_registry=filesystem_registry,
        mcp_execution_registry=mcp_execution_registry,
        mcp_manager=mcp_manager,
        process_manager=process_manager,
        resource_registry=resource_registry,
        runner_workspace=runner_workspace,
        server_client=server_client,
        spec_resolver=spec_resolver,
        terminal_registry=terminal_registry,
    )

    async def _catch_up_scan() -> None:
        recreated_prompts = pending_approvals.notify_server_reconnect()
        if recreated_prompts:
            _logger.info(
                "Recreating %d pending approval or elicitation request(s) after reconnect",
                recreated_prompts,
                extra={"session_id": runner_primary_session_id()},
            )
        # The tunnel just reconnected, which usually means the SERVER restarted
        # (deploy, crash, replica failover) and lost its in-memory session-status
        # cache. This runner did not restart, so every status source still
        # believes its last edge was delivered and nothing re-asserts — a
        # native session mid-turn during the restart would sit on a stale
        # ``idle`` for the rest of the turn. Re-arm them before the item scan
        # below (which skips native harnesses entirely).
        if resource_registry is not None:
            try:
                resource_registry.resync_session_statuses()
            except Exception:  # noqa: BLE001 — best-effort; never block catch-up.
                _logger.warning(
                    "Session status resync failed after reconnect",
                    exc_info=True,
                    extra={"session_id": runner_primary_session_id()},
                )
        # A server outage at child-completion time fails the wake POST past its
        # bounded retries; the server is reachable again now, so re-deliver
        # those stranded wakes or the parent never learns its child finished.
        _retry_stranded_wakes()
        for session_id in list(_session_histories):
            if _is_native_harness(session_id):
                continue
            try:
                after_id = _last_server_item_id.get(session_id)
                all_new: list[_JsonObject] = []
                while True:
                    params: dict[str, str] = {
                        "limit": "100",
                        "order": "asc",
                    }
                    if after_id:
                        params["after"] = after_id
                    resp = await server_client.get(
                        f"/v1/sessions/{session_id}/items",
                        params=params,
                        timeout=10.0,
                    )
                    if resp.status_code != 200:
                        break
                    page = resp.json()
                    page_items = page.get("data", [])
                    if not page_items:
                        break
                    all_new.extend(page_items)
                    last_id = page_items[-1].get("id")
                    if last_id:
                        after_id = last_id
                        _last_server_item_id[session_id] = last_id
                    if not page.get("has_more", False):
                        break
                if not all_new:
                    continue
                new_items = _convert_raw_items_to_input(all_new)
                _session_histories.setdefault(session_id, []).extend(
                    new_items,
                )
                if (
                    session_id not in _active_turns
                    and new_items
                    and new_items[-1].get("role") == "user"
                ):
                    _begin_turn_slot(session_id)
                    _publish_turn_status(session_id, "running")
                    agent_id = _session_agent_ids.get(session_id)
                    msg_body: _JsonObject = {
                        "agent_id": agent_id,
                        "model": agent_id or "",
                        # Catch-up has no live server dispatch carrying renderer state.
                        "browser_renderer_available": False,
                    }
                    _turn_task = asyncio.create_task(
                        _run_turn_bg(msg_body, session_id),
                        name=f"turn-catchup-{session_id}",
                    )
                    _active_turns[session_id] = _turn_task
                    _turn_task.add_done_callback(
                        _background_tasks.discard,
                    )
                    _background_tasks.add(_turn_task)
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Catch-up scan failed for %s",
                    session_id,
                    exc_info=True,
                )

    app.state.catch_up_scan = _catch_up_scan

    _pane_reaper_registry = getattr(resource_registry, "terminal_registry", None)
    if (
        resource_registry is not None
        and _pane_reaper_registry is not None
        and hasattr(_pane_reaper_registry, "native_panes")
    ):
        from omnigent.harnesses.claude_native.bridge import approval_wait_is_fresh
        from omnigent.native.native_cost_popup import (
            _tmux_last_client_input_at,
            _tmux_window_activity_at,
        )
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event
        from omnigent.terminals.pane_reaper import (
            PANE_OUTPUT_BUSY_WINDOW_S,
            NativePaneReaper,
            PaneRef,
        )

        def _native_panes_for_reaper() -> list[PaneRef]:
            panes: list[PaneRef] = []
            for conv_id, name, socket_path in _pane_reaper_registry.native_panes():
                terminal_id = terminal_resource_id(name, "main")
                if is_native_harness(
                    resource_registry.terminal_resource_role(conv_id, terminal_id)
                ):
                    panes.append(PaneRef(conv_id, terminal_id, name, socket_path))
            return panes

        async def _native_pane_is_busy(pane: PaneRef) -> bool:
            conv_id = pane.conversation_id
            if conv_id in _active_turns or (
                process_manager is not None and process_manager.has_active_turn(conv_id)
            ):
                return True
            if _native_pane_status.get(conv_id) == "running":
                return True
            # A pane parked on a permission prompt emits nothing and reports no
            # active turn, so every signal above reads idle. Reaping it kills the
            # prompt and strands its approval card unanswerable.
            if approval_wait_is_fresh(conv_id):
                return True
            # An attached viewer alone does not spare the pane (a tab left open
            # overnight kept idle native stacks resident). Count it only when a
            # human drove it recently: a CLI keypress, or any web-bridge event.
            input_at = await asyncio.to_thread(
                _tmux_last_client_input_at, str(pane.socket_path), "main"
            )
            if input_at is not None and time.time() - input_at < PANE_OUTPUT_BUSY_WINDOW_S:
                return True
            instance = _pane_reaper_registry.get(conv_id, pane.terminal_name, "main")
            if instance is not None and instance.client_interaction_within(
                PANE_OUTPUT_BUSY_WINDOW_S
            ):
                return True
            # Primary evidence: tmux stamps window_activity on every byte the
            # pane emits, so a producing terminal stays busy even when the
            # status pipeline above has silently stalled (a stalled forwarder
            # once froze the busy signal and got a live session reaped).
            activity_at = await asyncio.to_thread(
                _tmux_window_activity_at, str(pane.socket_path), "main"
            )
            return (
                activity_at is not None and time.time() - activity_at < PANE_OUTPUT_BUSY_WINDOW_S
            )

        async def _reap_native_pane(pane: PaneRef) -> None:
            try:
                await resource_registry.close_terminal(pane.conversation_id, pane.terminal_id)
            finally:
                # Closing the codex TUI pane leaves its per-session app-server
                # (and forwarder) running — no-op for other harnesses. Tear it
                # down in ``finally`` so an idle-reaped codex session can't orphan
                # a ``codex app-server`` for the runner's lifetime even when the
                # pane close above partially fails (the very leak this guards).
                await _native_runtime.teardown_codex_native_app_server(pane.conversation_id)
                _publish_terminal_deleted_event(
                    conversation_id=pane.conversation_id,
                    terminal_name=pane.terminal_name,
                    session_key="main",
                    publish_event=_publish_event,
                )

        app.state.native_pane_reaper = NativePaneReaper(
            list_native_panes=_native_panes_for_reaper,
            is_busy=_native_pane_is_busy,
            reap=_reap_native_pane,
        )
    else:
        app.state.native_pane_reaper = None

    return app


def create_runner_app_from_env() -> FastAPI:
    """Lightweight uvicorn ``--factory`` entry point for transport subprocesses.

    Reads ``RUNNER_SERVER_URL`` from the environment and constructs a
    minimal :class:`httpx.AsyncClient` for the Omnigent server, then delegates
    to :func:`create_runner_app` with no :class:`HarnessProcessManager`,
    no spec resolver, and no terminal registry.

    Used as the default ``app_factory_path`` for
    :class:`~omnigent.runner.transports.tcp.RunnerTCPSubprocess` and
    :class:`~omnigent.runner.transports.uds.RunnerSubprocess`.  It is
    intentionally lighter than :func:`omnigent.runner._entry.create_app`
    so transport smoke tests start quickly without spawning harness pools
    or sweeping orphan directories.

    :returns: A :class:`FastAPI` runner app backed by an httpx client
        pointed at ``RUNNER_SERVER_URL``.
    :raises RuntimeError: If ``RUNNER_SERVER_URL`` is not set in the
        environment.
    """
    import os

    import httpx

    server_url = os.environ.get("RUNNER_SERVER_URL", "").strip()
    if not server_url:
        raise RuntimeError("RUNNER_SERVER_URL is required for the runner subprocess factory")
    from omnigent_client._http import is_loopback_url

    server_client = httpx.AsyncClient(
        base_url=server_url,
        timeout=httpx.Timeout(5.0, read=None),
        # A proxy cannot reach a loopback server, so local targets bypass it.
        trust_env=not is_loopback_url(server_url),
    )
    return create_runner_app(server_client=server_client)


class SessionAgentMissingError(RuntimeError):
    """The session's agent no longer resolves (for example it was removed).

    A ``RuntimeError`` so every existing spec-resolve handler still catches it;
    the turn route reports it as ``session_agent_missing`` with fork guidance.
    """


async def _resolve_harness_config(
    *,
    agent_id: str | None,
    spec_resolver: SpecResolver | None,
    session_id: str | None = None,
    model_override: str | None = None,
    harness_override: str | None = None,
    sub_agent_name: str | None = None,
    cwd: Path | None = None,
    resource_registry: SessionResourceRegistry | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Resolve harness type + spawn-env from the agent spec.

    :param agent_id: Agent id to resolve the spec for.
    :param spec_resolver: Resolver that returns the spec for *agent_id*.
    :param session_id: Session/conversation id, threaded to the resolver.
    :param model_override: Per-session ``/model`` override, applied to the
        spawn-env model so it takes effect on the SDK harnesses.
    :param harness_override: Per-session brain-harness override (validated
        at session create, forwarded by the server in the message body),
        e.g. ``"pi"``. Replaces the spec's ``executor.config.harness``.
    :param sub_agent_name: For a sub-agent session, the dispatched
        sub-agent's name (e.g. ``"claude_code"``). The bound *agent_id*
        resolves to the PARENT spec, so without this swap a child's turn
        resolves the parent's harness (``claude-sdk``) and the process
        manager respawns — tearing down the child's live ``claude-native``
        terminal ("Bridge closed: terminal resource not found"). When set,
        the parent entry is swapped for the child's — spec AND bundle dir —
        via :func:`_resolve_sub_agent_spec_entry` before harness derivation,
        so the spawn-env advertises the child's bundle rather than the
        parent's. ``None`` for top-level sessions.
    :param cwd: Runtime working directory for harnesses that need it.
    :returns: ``(harness, spawn_env)`` derived from the resolved spec.
    :raises RuntimeError: When a spec_resolver is configured but the spec
        cannot be resolved. Callers catch this to surface a clean error
        rather than spawning an invalid harness subprocess.
    """
    if agent_id and spec_resolver:
        spec_entry = await spec_resolver(agent_id, session_id)
        spec = _unwrap_resolved_spec(spec_entry)
        workdir = _resolved_spec_workdir(spec_entry)
        if spec is not None:
            # Swap to the sub-agent's own spec so its harness (not the
            # parent's) drives the turn. Mirrors the POST /v1/sessions and
            # _run_turn_bg swaps; applied here so the harness-HTTP path is
            # sub-agent-aware too, even after a reconnect drops the
            # in-memory _session_sub_agent_names map.
            # The child's bundle dir comes from the same resolution, so the
            # spawn-env below advertises the child's bundle — not the
            # parent's, whose skills and tools the child has no claim to.
            if sub_agent_name:
                sub_entry = _native_runtime._resolve_sub_agent_spec_entry(
                    spec_entry, sub_agent_name
                )
                if sub_entry is None:
                    _warn_unresolved_sub_agent(session_id, sub_agent_name)
                else:
                    spec = _unwrap_resolved_spec(sub_entry)
                    workdir = _resolved_spec_workdir(sub_entry)
            raw_harness = (
                harness_override or spec.executor.config.get("harness") or spec.executor.type
            )
            harness = canonicalize_harness(raw_harness) or raw_harness
            spawn_env = _build_spawn_env_from_spec(
                spec,
                raw_harness,
                cwd=cwd,
                workdir=workdir,
                model_override=model_override,
                session_id=session_id,
                resource_registry=resource_registry,
            )
            return harness, spawn_env

    if spec_resolver is not None:
        # spec_resolver is configured but either agent_id was not provided or
        # the resolver returned no spec. Fail loud rather than silently falling
        # through to the test-only harness and spawning a broken subprocess.
        if not agent_id:
            raise RuntimeError(
                "Cannot select a harness: agent_id is missing and a spec_resolver "
                "is configured. Ensure agent_id is forwarded in the turn body."
            )
        # With a session, the resolver returns None only when the server no
        # longer has the session's agent (a 404, e.g. after the agent is removed).
        missing = SessionAgentMissingError if session_id else RuntimeError
        raise missing(f"No agent spec found for agent_id={agent_id!r}; cannot select a harness.")

    # Fallback for tests that register a custom harness in _HARNESS_MODULES
    # (spec_resolver is None in the test runner).
    return "runner-test-default", None


# The per-harness env var that carries the model into the spawn-env (SDK /
# in-process) harnesses. Used to apply a per-session ``/model`` override at
# highest precedence — see :func:`_build_spawn_env_from_spec`.
_HARNESS_MODEL_ENV_KEY: dict[str, str] = {
    "claude-sdk": "HARNESS_CLAUDE_SDK_MODEL",
    "codex": "HARNESS_CODEX_MODEL",
    "pi": "HARNESS_PI_MODEL",
    "openai-agents": "HARNESS_OPENAI_AGENTS_MODEL",
    "cursor": "HARNESS_CURSOR_MODEL",
    # cursor-native is intentionally omitted here (and from
    # model_override._SDK_MODEL_OVERRIDE_HARNESSES): like the other native CLIs
    # (claude-native, codex-native) it receives the model as a ``--model`` argv
    # at terminal launch (see ``_auto_create_cursor_terminal``), not via a
    # spawn-env var. ``harness_supports_model_override`` already returns True for
    # it because it is a native harness.
    "antigravity": "HARNESS_ANTIGRAVITY_MODEL",
    # Kimi reads ``HARNESS_KIMI_MODEL`` in
    # :mod:`omnigent.inner.kimi_executor`; without this mapping a per-session
    # ``/model`` override would silently drop on the kimi harness path.
    "kimi": "HARNESS_KIMI_MODEL",
    "qwen": "HARNESS_QWEN_MODEL",
    "goose": "HARNESS_GOOSE_MODEL",
    "copilot": "HARNESS_COPILOT_MODEL",
}
_HARNESS_MODEL_ENV_KEY = model_env_keys()


class _SpawnEnvBuilder(Protocol):
    def __call__(
        self,
        spec: object,
        *,
        cwd: Path | None,
        workdir: Path | None,
    ) -> dict[str, str]:
        raise NotImplementedError


class _ModelCopyValue(Protocol):
    def model_copy(self, *, update: Mapping[str, object]) -> object: ...


async def _ensure_session_subagent_router(
    session_id: str,
    harness: str | None,
    *,
    server_client: httpx.AsyncClient | None,
    routing_class: SessionRoutingClass | None = None,
) -> None:
    """Start this session's subagent-routing endpoint.

    Only for the SDK harness families: the native terminals know their own
    bridge directory and start the router from their launch paths, where
    the harness's hooks are also pointed at it.

    Started for Smart Routing sessions only: a plain session must not carry
    the loopback server, its on-disk bearer token, or an in-process hook on
    every ``Task`` for a verdict the server never routes. On the codex SDK
    arm the advertisement also turns generated hooks and the routed-spawn
    tool pre-approvals on, and those spawns already route through
    session-create, so there it takes auto-harness.

    Never raises: ``ensure_session_router_quietly`` owns the bridge-dir
    resolution too, so a hostile or pre-existing ``$TMPDIR`` root cannot
    fail session creation for harnesses that do not even use routing.

    :param session_id: Session/conversation identifier.
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :param server_client: Runner→server client the relay forwards on.
        ``None`` (in-process tests) skips the start.
    :param routing_class: The session's Smart Routing class. ``None``
        reads whatever was stamped at session init, which for an unknown
        session is the plain class.
    """
    from omnigent.runner.subagent_routing import ensure_session_router_quietly

    if is_native_harness(harness):
        return
    resolved = routing_class if routing_class is not None else session_routing_class(session_id)
    ensure_session_router_quietly(
        session_id,
        server_client=server_client,
        harness=harness,
        routing_class=resolved,
    )


def _build_spawn_env_from_spec(
    spec: AgentSpec,
    harness: str,
    *,
    cwd: Path | None = None,
    workdir: Path | None = None,
    model_override: str | None = None,
    session_id: str | None = None,
    resource_registry: SessionResourceRegistry | None = None,
) -> dict[str, str] | None:
    """Build spawn-env from spec — mirrors workflow.py's helpers.

    :param spec: The resolved agent spec.
    :param harness: Requested harness, including any ``acp:<slug>`` selection.
    :param cwd: Runtime working directory for harnesses that need it.
    :param workdir: Bundle workdir, threaded to the builders.
    :param session_id: Session/conversation id, used to hand the harness
        this session's subagent-routing endpoint. ``None`` omits it.
    :param model_override: The per-session ``/model`` override, e.g.
        ``"claude-sonnet-4-6"``, or ``None``. When set, it overrides the
        ``HARNESS_<H>_MODEL`` the builder baked in (spec model / provider
        default / catalog default) so ``/model`` actually takes effect on
        the SDK / in-process harnesses. (The native CLIs honor the override
        via ``--model`` in :func:`_build_claude_native_base_args`; the
        SDK harnesses have no such arg, so the override must land in the
        env var here.)
    :returns: The spawn-env dict, or ``None`` for native / unknown harnesses.
    """
    # Namespaced generic-ACP ids (``acp:<slug>``) canonicalize to ``acp`` so the
    # dispatch, model-key lookup, and logging below all key off the base harness;
    # the concrete agent's slug must also reach the command and model resolvers.
    requested_harness = harness
    harness = canonicalize_harness(harness) or harness
    if requested_harness.startswith("acp:"):
        spec = dataclasses.replace(
            spec,
            executor=dataclasses.replace(
                spec.executor, config={**spec.executor.config, "harness": requested_harness}
            ),
        )
    from omnigent.sandbox.copy_on_write import validate_copy_on_write_harness

    validate_copy_on_write_harness(getattr(spec, "os_env", None), harness)
    effective_spec = spec
    from omnigent.inference_config import load_runtime_inference_config, parse_inference_config

    has_inference_bindings = bool(parse_inference_config(load_runtime_inference_config()))
    if has_inference_bindings and dataclasses.is_dataclass(spec):
        declared_harness = str(spec.executor.config.get("harness") or "")
        identity = (
            requested_harness
            if requested_harness.startswith("acp:")
            else declared_harness
            if harness == "acp" and declared_harness.startswith("acp:")
            else harness
        )
        effective_spec = dataclasses.replace(
            spec,
            executor=dataclasses.replace(
                spec.executor,
                config={**spec.executor.config, "harness": identity},
                model=model_override if model_override is not None else spec.executor.model,
            ),
        )
    if model_override is not None:
        executor = getattr(spec, "executor", None)
        if (
            harness == "acp"
            and not has_inference_bindings
            and dataclasses.is_dataclass(spec)
            and dataclasses.is_dataclass(executor)
        ):
            effective_spec = dataclasses.replace(
                spec, executor=dataclasses.replace(spec.executor, model=model_override)
            )
        elif hasattr(spec, "model_copy") and hasattr(executor, "model_copy"):
            copied_executor = cast(_ModelCopyValue, executor).model_copy(
                update={"model": model_override}
            )
            effective_spec = cast(
                AgentSpec,
                cast(_ModelCopyValue, spec).model_copy(update={"executor": copied_executor}),
            )
    acp_default_model: str | None = None
    if harness == "acp":
        from omnigent.models.model_catalog import _acp_launch_model, validate_acp_model

        policy_spec = effective_spec if has_inference_bindings else spec
        acp_default_model = _acp_launch_model(policy_spec)
        validate_acp_model(policy_spec, acp_default_model)
        validate_acp_model(policy_spec, model_override)
    try:
        from omnigent.runtime.workflow import (
            _build_acp_cli_spawn_env,
            _build_acp_spawn_env,
            _build_antigravity_spawn_env,
            _build_claude_sdk_spawn_env,
            _build_codex_spawn_env,
            _build_copilot_spawn_env,
            _build_cursor_spawn_env,
            _build_goose_spawn_env,
            _build_hermes_spawn_env,
            _build_kimi_spawn_env,
            _build_openai_agents_sdk_spawn_env,
            _build_pi_spawn_env,
            _build_qwen_spawn_env,
        )

        if harness == "claude-sdk":
            env = _build_claude_sdk_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "codex":
            env = _build_codex_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
            env["HARNESS_CODEX_SKILLS_DIR"] = (
                str(resource_registry.codex_skills_dir(session_id))
                if resource_registry is not None and session_id is not None
                else ""
            )
        elif harness == "pi":
            env = _build_pi_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "openai-agents":
            env = _build_openai_agents_sdk_spawn_env(effective_spec)
        elif harness == "cursor":
            env = _build_cursor_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "antigravity":
            env = _build_antigravity_spawn_env(effective_spec)
        elif harness == "kimi":
            env = _build_kimi_spawn_env(effective_spec, cwd=cwd)
        elif harness == "hermes":
            env = _build_hermes_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "qwen":
            env = _build_qwen_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "goose":
            env = _build_goose_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "acp":
            env = _build_acp_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
            # Reset uses the original default even when the process launched
            # with a session override. Empty defers to the vendor's first model.
            env["HARNESS_ACP_DEFAULT_MODEL"] = acp_default_model or ""
        elif harness == "copilot":
            env = _build_copilot_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness in ACP_CLI_HARNESSES:
            # Builtin ACP CLI harnesses (one catalog row each) share a single
            # builder; the row supplies the command, label, and install info.
            env = _build_acp_cli_spawn_env(
                effective_spec, harness=harness, cwd=cwd, workdir=workdir, session_id=session_id
            )
        else:
            builder_path = spawn_env_builders().get(harness)
            if builder_path is not None:
                builder = load_object(builder_path)
                if not callable(builder):
                    raise TypeError(f"spawn environment builder {builder_path!r} is not callable")
                env = cast(_SpawnEnvBuilder, builder)(
                    effective_spec,
                    cwd=cwd,
                    workdir=workdir,
                )
            else:
                # Native terminal harnesses and unknown harnesses build env elsewhere.
                return None
    except ImportError:
        return None

    if env is not None:
        from omnigent.inner.agent_env import desktop_session_passthrough, strip_desktop_session_env

        env = strip_desktop_session_env(env)
        env.update(desktop_session_passthrough(effective_spec.os_env))

    if (
        env is not None
        and spec.os_env is not None
        and spec.os_env.sandbox is not None
        and any(p.copy_on_write for p in spec.os_env.sandbox.write_path_specs)
    ):
        if resource_registry is None or session_id is None:
            raise ValueError("copy_on_write harnesses require a session resource registry")
        from omnigent.sandbox.copy_on_write import (
            SHARED_ENVIRONMENT_VAR,
            export_shared_environment,
        )

        environment = resource_registry.resolve_environment(
            session_id, DEFAULT_ENVIRONMENT_ID, spec
        )
        policy = getattr(environment, "sandbox", None)
        if policy is None:
            raise ValueError("copy_on_write requires a local sandbox environment")
        environment.prepare_sandbox(policy)
        env[SHARED_ENVIRONMENT_VAR] = export_shared_environment(policy)

    # Point the harness process at this session's subagent-routing endpoint
    # when one is running (started at session init). Scoped to *harness* so a
    # codex executor beneath a claude session never sees the codex router vars
    # carrying the parent's session id. Empty when the session has no router.
    if env is not None and session_id:
        from omnigent.runner.subagent_routing import session_router_env

        env.update(session_router_env(session_id, harness))
        if harness in CODEX_CANONICAL_HARNESSES:
            # A Smart Routing turn or spawn can land on a gateway arm codex's
            # bundled catalog has no entry for, so the session replaces that
            # catalog. Plain sessions get nothing here and never pay the
            # ``codex debug models`` probe.
            from omnigent.inner.codex_executor import codex_extended_catalog_env

            env.update(
                codex_extended_catalog_env(session_routing_class(session_id).routing_enabled)
            )

    # Per-session ``/model`` override wins over everything the builder baked
    # into HARNESS_<H>_MODEL. Without this, `/model` is recorded in the
    # readout but the turn still uses the provider/catalog default.
    if model_override and env is not None:
        model_key = _HARNESS_MODEL_ENV_KEY.get(harness)
        if model_key is not None:
            env[model_key] = model_override

    # Routing visibility: log the resolved gateway target so operators can
    # confirm which provider a turn actually hits (api.anthropic.com /
    # api.openai.com for a key, vs a Databricks profile). Logged here in the
    # runner process (INFO is emitted) rather than the harness subprocess
    # (which suppresses inner.* INFO). ``base_url`` is empty for the legacy
    # ``profile:`` path (resolved downstream by ucode); the profile still
    # identifies the Databricks target.
    if env is not None:
        prefix = f"HARNESS_{harness.upper().replace('-', '_')}"
        _logger.info(
            "%s gateway routing: gateway=%s base_url=%s profile=%s model=%s",
            harness,
            env.get(f"{prefix}_GATEWAY"),
            # A harness that carries per-family URLs (pi) sets only the plural
            # ``_BASE_URLS`` JSON; without the fallback it logs base_url=None.
            env.get(f"{prefix}_GATEWAY_BASE_URL") or env.get(f"{prefix}_GATEWAY_BASE_URLS"),
            env.get(f"{prefix}_DATABRICKS_PROFILE"),
            env.get(_HARNESS_MODEL_ENV_KEY.get(harness, f"{prefix}_MODEL")),
            extra={"session_id": session_id},
        )
    return env


# ── Agent-start policy gate ────────────────────────────────────────────


async def _evaluate_agent_start_gate(
    spec: AgentSpec,
    harness: str,
) -> Mapping[str, object] | None:
    """Collect the policies' launch transforms for the synthetic start probe.

    Returns the composed replacement payload (``enforce_sandbox`` forcing a
    sandbox) or ``None``. DENY/ASK verdicts never gate agent start; see
    :meth:`RunnerToolPolicyGate.evaluate_agent_start`.
    """
    from omnigent.runner.policy import RunnerToolPolicyGate

    gate = RunnerToolPolicyGate.from_spec(spec)
    if gate.is_empty:
        return None

    sandbox_dict: _JsonObject | None = None
    if spec.os_env is not None and spec.os_env.sandbox is not None:
        sandbox_dict = cast(_JsonObject, dataclasses.asdict(spec.os_env.sandbox))

    return await gate.evaluate_agent_start(
        {
            "agent_name": getattr(spec, "name", None) or "",
            "harness": harness,
            "sandbox": sandbox_dict,
        },
    )


def _apply_sandbox_override_from_start_data(
    spec: AgentSpec,
    start_data: object,
) -> None:
    """Apply the start probe's composed sandbox transform to *spec*.

    The ``enforce_sandbox`` policy returns replacement data shaped as
    ``{"name": "sys_agent_start", "arguments": {"sandbox": {...}}}``. This
    extracts the ``sandbox`` dict and mutates ``spec.os_env`` in-place.

    :param spec: The agent spec (``AgentSpec``) — mutated in-place.
    :param start_data: The composed transform payload from
        :meth:`RunnerToolPolicyGate.evaluate_agent_start`, expected to be a
        mapping with ``arguments.sandbox``.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    if not isinstance(start_data, Mapping):
        return
    args = start_data.get("arguments")
    if not isinstance(args, Mapping):
        return
    sandbox_override = args.get("sandbox")
    if not isinstance(sandbox_override, Mapping):
        return

    if spec.os_env is None:
        spec.os_env = OSEnvSpec()
    if spec.os_env.sandbox is None:
        spec.os_env.sandbox = OSEnvSandboxSpec()

    for key, value in sandbox_override.items():
        if hasattr(spec.os_env.sandbox, key):
            setattr(spec.os_env.sandbox, key, value)
