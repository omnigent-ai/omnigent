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
import enum
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol, TypeAlias, cast, overload

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.claude_native import ClaudeNativeUcodeConfig
    from omnigent.claude_native_bridge import ClaudeNativeToolRelay
    from omnigent.codex_native_bridge import CodexNativeBridgeState
    from omnigent.llms.client import Client as LLMClient
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.runner.policy import PolicyVerdict
    from omnigent.terminals.registry import TerminalListEntry, TerminalRegistry

import click
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import (
    canonicalize_harness,
    is_native_harness,
    native_terminal_name,
)
from omnigent.harness_capabilities import InstructionDelivery
from omnigent.harness_plugins import (
    harness_capabilities,
    load_object,
    model_env_keys,
    spawn_env_builders,
)
from omnigent.inner.native_attachments import has_unresolved_file_id, resolve_file_id_block
from omnigent.json_types import JsonObject as _JsonObject
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.native_coding_agents import (
    native_coding_agent_for_harness,
    native_coding_agent_for_terminal_name,
)
from omnigent.policies.types import FAIL_CLOSED_PHASES
from omnigent.runner import native as _native
from omnigent.runner import pending_approvals
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
from omnigent.runner.native import (
    _AUTO_OPENCODE_SERVERS,
    _COST_POPUP_REPOP_TASKS,
    _REPL_TERMINAL_NAME,
    _REPL_TERMINAL_SESSION_KEY,
    NativeLaunchContext,
    PreLaunchResult,
    ResolvedSpec,
    _antigravity_native_terminal_arrives_via_transfer,
    _auto_create_opencode_terminal,
    _auto_create_qwen_terminal,
    _auto_create_repl_terminal,
    _cancel_auto_forwarder_task,
    _claude_native_bridge_id_for_session,
    _claude_native_bridge_id_with_optional_labels,
    _claude_native_session_wants_rebuild,
    _claude_native_terminal_arrives_via_transfer,
    _codex_ensure_response_with_policy_notice,
    _codex_native_model_from_spec,
    _codex_session_needs_runner_terminal,
    _CodexNativeModelOptionsNotReady,
    _delete_native_bridge_dirs,
    _ensure_native_terminal,
    _ensure_orchestrator_skills_in_bundle,
    _forward_harness_response,
    _is_runner_owned_antigravity_terminal,
    _is_runner_owned_codex_terminal,
    _is_spec_local_native_python_tool,
    _launch_native_terminal,
    _log_terminal_lookup_miss,
    _publish_terminal_pending,
    _publish_tmux_target_for_bridge,
    _required_runner_env,
    _resolve_native_spawn_env,
    _resolve_opencode_compact_model,
    _resolved_spec_workdir,
    _resolved_workdir_for_spec,
    _session_labels_for_runner_spawn,
    _session_payload_for_host_spawn_check,
    _unwrap_resolved_spec,
)
from omnigent.runner.native import orchestration as _native_runtime
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    CLAUDE_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    QWEN_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    parse_runner_session_init_envelope,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError
from omnigent.runtime.prompt import (
    SHARED_SESSION_AUTHORSHIP_INSTRUCTION,
    build_instructions,
    build_instructions_nullable,
    input_items_have_multiple_authors,
    prepare_input_items_for_model,
    raw_author_instructions,
    shared_message_attribution_enabled,
)
from omnigent.server.schemas import (
    BackgroundSessionTitleRequest,
    BackgroundSessionTitleResponse,
)
from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)

_logger = logging.getLogger(__name__)


class _SubAgentProvenance(enum.Enum):
    """How a cached session spec relates to the session's ``sub_agent_name``.

    Three states, because the fact has three: the entry IS the requested
    child (or the session names no sub-agent), the entry is the PARENT kept
    after a miss, or nobody has decided yet. The third is carried by
    :data:`UNDETERMINED`; the second by the plain ``str`` name that failed to
    resolve, so the reader that reports it has the name to report.

    A two-state encoding folds "not decided" into "resolved", and a reader
    then treats an entry published mid-resolution — or one published after a
    session-snapshot fetch failed, where the name was never recoverable — as
    an authoritative child. Both used to happen.
    """

    RESOLVED = "resolved"
    UNDETERMINED = "undetermined"


# What a cache entry records about its sub-agent: the name that failed to
# resolve, or one of the two states above.
_SubAgentProvenanceValue: TypeAlias = "str | _SubAgentProvenance"


class _SubAgentRecovery(NamedTuple):
    """The outcome of recovering a session's ``sub_agent_name``.

    ``known`` is what separates "this session names no sub-agent" from "the
    lookup failed and the answer is still unknown" — two facts a bare
    ``str | None`` return cannot tell apart, and conflating them publishes a
    cache entry claiming a resolution nobody performed.
    """

    name: str | None
    known: bool


def _warn_unresolved_sub_agent(session_id: str | None, sub_agent_name: str) -> None:
    """
    Log that a sub-agent name did not resolve to a declared child spec.

    A renamed/removed sub-agent or stale session metadata can still reach
    the spec-swap sites even though the create route rejects an undeclared
    name up front. Every such site skips its swap and continues with the
    PARENT spec, so the request succeeds and carries no marker of the
    miss — this warning is the only record that the session is running
    something other than the sub-agent it is bound to. Individual sites
    used to raise, answer 404, or drop the spec entirely instead.

    :param session_id: The session whose turn is resolving the spec.
    :param sub_agent_name: The name that failed to resolve in the parent
        spec tree.
    """
    _logger.warning(
        "Sub-agent %r for session %s did not resolve in the parent spec; "
        "continuing with the parent spec. Likely a renamed/removed sub-agent "
        "or stale session metadata.",
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
# Cached server version from the /api/version probe; ``None`` until a probe
# succeeds. A failed probe stays ``None`` and is retried on the next
# session-create — the GET is cheap and self-heals a transient failure.
_server_version: str | None = None


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
        _logger.info("resolved server version: %s", _server_version)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully; never 500 an old server
        _logger.warning("could not probe server /api/version (%s); treating as unknown", exc)
    return _server_version


def _client_safe_error_detail(exc: BaseException, *, context: str) -> str:
    """
    Log *exc* in full and return a generic detail string safe for clients.

    Raw exception text (``str(exc)``) can embed absolute paths, internal
    hostnames, PIDs, and other server-side state. The runner is reached via
    the AP server proxy and its error bodies are relayed to the caller, so
    the cause is logged here for operators while the HTTP response carries
    only this fixed string. The structured ``error`` code that accompanies
    the detail already names the failure category for the caller.

    :param exc: The caught exception, e.g. a ``RuntimeError`` from a harness
        spawn or an ``InvalidPath`` from path validation.
    :param context: Short operator-facing label for the failing operation,
        e.g. ``"harness spawn"``. Appears only in the server log.
    :returns: A fixed, non-sensitive string safe to return to clients.
    """
    _logger.warning("%s failed: %s", context, exc, exc_info=True)
    return "Request failed on the runner; see runner logs for details."


_SpecEntry: TypeAlias = AgentSpec | ResolvedSpec
SpecResolver: TypeAlias = Callable[[str, str | None], Awaitable[_SpecEntry | None]]
_ResourceType: TypeAlias = Literal["environment", "terminal", "file"]


@overload
def _unwrap_spec_entry(entry: None) -> None: ...


@overload
def _unwrap_spec_entry(entry: _SpecEntry) -> AgentSpec: ...


def _unwrap_spec_entry(entry: _SpecEntry | None) -> AgentSpec | None:
    """Return the agent spec from a runner app cache entry."""
    return entry.spec if isinstance(entry, ResolvedSpec) else entry


_NO_BODY_STATUS_CODES = {204, 304}
_SUBAGENT_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SUBAGENT_DELIVERY_DELIVERED = "delivered"
_SUBAGENT_DELIVERY_ALREADY_DELIVERED = "already_delivered"
_SUBAGENT_DELIVERY_UNTRACKED = "untracked"
_SUBAGENT_DELIVERY_MISSING_WORK_ENTRY = "missing_work_entry"
_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX = "missing_parent_inbox"
# Read budget for runner→server POSTs that can PARK behind a human-approval
# ASK gate: policy evaluation (``_evaluate_policy_via_omnigent``) and sub-agent
# wake-notice delivery (``_deliver_subagent_wake_post``). Both are gated at the
# recipient's REQUEST/LLM/TOOL phase, which can hold for the deciding policy's
# ``ask_timeout`` (default one day). Held at one day (86400s) — matching that
# default — so the POST WAITS for the real verdict instead of severing the
# parked gate at a short read timeout. A 30s cut previously fail-closed to DENY
# (and the wake POST retried into duplicate approval cards). Fast connect (30s)
# so an unreachable server still fails out promptly into the caller's
# fail-open/retry path. Guarded by tests/test_ask_timeout_infinite.py.
_ASK_GATE_DELIVERY_READ_TIMEOUT_S: float = 86400.0
_ASK_GATE_DELIVERY_TIMEOUT = httpx.Timeout(_ASK_GATE_DELIVERY_READ_TIMEOUT_S, connect=30.0)
# Bounded retry budget for the sub-agent wake POST. The wake is the sole
# delivery signal for the last child of a fan-out, and Omnigent routinely
# returns a transient 503 RUNNER_UNAVAILABLE while the parent's runner tunnel
# is reconnecting, so a single attempt can strand the parent silently.
_WAKE_POST_MAX_ATTEMPTS = 3
_WAKE_POST_RETRY_BASE_DELAY_S = 0.5
_WAKE_POST_RETRY_MAX_DELAY_S = 4.0
# 4xx statuses that are transient and worth retrying (mirrors the forwarder's
# classification): everything else in 4xx is a permanent client-side rejection.
_WAKE_POST_TRANSIENT_4XX = frozenset({408, 409, 425, 429})

# Cadence for ``session.heartbeat`` keepalive events on the runner's
# ``GET /v1/sessions/{id}/stream`` endpoint. Between turns the event
# queue is idle — without periodic bytes, an intermediate proxy (e.g.
# the Databricks Apps ingress) can drop the long-lived HTTP connection.
# Matches the AP-side ``_SESSION_STREAM_HEARTBEAT_INTERVAL_S``.
_SESSION_STREAM_HEARTBEAT_S = 15.0

# Lazy singleton LLM client for the runner process. Created on first use so
# the runner does not import llms at startup (imports are expensive and the
# /v1/summarize endpoint is optional). The concrete type is imported only
# during type checking to keep the runtime import graph lazy.
_runner_llm_client: LLMClient | None = None


def _get_runner_llm_client() -> LLMClient:
    """Return the runner-process LLM client, creating it on first use.

    The client is constructed from the runner process's environment
    variables, which include the Databricks credentials set up by the
    runner entry point. This is intentionally separate from the AP
    server's ``_get_llm_client()`` — the runner may have different
    (or more) credentials than the Omnigent server.

    :returns: A ``llms.Client`` instance bound to this runner process.
    """
    global _runner_llm_client
    if _runner_llm_client is None:
        from omnigent.llms import Client as LLMClient

        _runner_llm_client = LLMClient()
    return _runner_llm_client


# Marker the runner stamps on action_required SSE events it intends
# to dispatch locally. See designs/RUNNER_MCP.md §Explicit dispatch
# marker.
_RUNNER_DISPATCHED_FIELD = "omnigent_runner_dispatched"


def _encode_sse_event(event: Mapping[str, object]) -> bytes:
    """Re-encode an SSE event as a single ``data:`` frame."""
    import json as _json

    return f"data: {_json.dumps(event)}\n\n".encode()


async def _evaluate_policy_via_omnigent(
    *,
    server_client: httpx.AsyncClient,
    harness_client: httpx.AsyncClient,
    conversation_id: str,
    evaluation_id: str,
    phase: str,
    data: _JsonObject,
) -> None:
    """
    Proxy a policy evaluation request from the harness to the Omnigent server.

    Called by the runner's ``proxy_stream`` when it intercepts a
    ``policy_evaluation.requested`` SSE event from the harness. Posts
    the evaluation request to the Omnigent server's
    ``POST /sessions/{id}/policies/evaluate`` endpoint, then delivers
    the verdict back to the harness as a ``policy_verdict`` inbound
    event.

    On failure (AP unreachable, non-200, malformed response) the default
    verdict is phase-aware:

    - ``PHASE_LLM_REQUEST`` / ``PHASE_LLM_RESPONSE`` fail OPEN
      (``POLICY_ACTION_ALLOW``) so a transient Omnigent outage does not
      hang the turn — these gates are advisory.
    - ``PHASE_TOOL_CALL`` fails CLOSED (``POLICY_ACTION_DENY``). For
      connector-native MCP tools the harness ``can_use_tool`` callback
      (which consumes this verdict) is the *only* enforcement point — the
      call is never re-checked server-side — so a policy that cannot be
      evaluated must not let the tool through.
    - ``PHASE_TOOL_RESULT`` fails OPEN: by the result phase the tool has
      already executed, so denying would only block an already-incurred
      side effect.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param harness_client: HTTP client pointed at the harness subprocess.
    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param evaluation_id: Unique correlation id from the harness,
        e.g. ``"poleval_abc123"``.
    :param phase: Proto-style phase string, e.g.
        ``"PHASE_LLM_REQUEST"``.
    :param data: Event data dict for the policy engine.
    """
    # Default verdict on error / non-200 / timeout. Phase-aware: TOOL_CALL
    # fails CLOSED (this round-trip is the authoritative gate for
    # connector-native tools), while advisory LLM phases and TOOL_RESULT
    # (the tool already ran) fail OPEN so a transient outage never hangs
    # the turn.
    _fail_closed = phase in FAIL_CLOSED_PHASES
    _default_action = "POLICY_ACTION_DENY" if _fail_closed else "POLICY_ACTION_ALLOW"
    verdict_action = _default_action
    verdict_reason: str | None = (
        f"Omnigent policy evaluation unavailable; failing closed for {phase}."
        if _fail_closed
        else None
    )
    verdict_data: _JsonObject | None = None

    try:
        ap_resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json={
                "event": {
                    "type": phase,
                    "data": data,
                },
            },
            # A TOOL_CALL/LLM_REQUEST/REQUEST ASK parks server-side in
            # ``_hold_native_ask_gate`` until a human resolves it (up to the
            # deciding policy's ``ask_timeout``, default one day). A 30s read
            # budget here severed that long-poll after 30s — the server saw an
            # UPSTREAM DISCONNECT and failed the gate closed (DENY), so the
            # main (claude-sdk) agent's approval card auto-resolved while
            # native sub-agents (whose hooks already wait the full day) parked
            # correctly. Hold the read budget at one day to match the native
            # hooks' ``_EVALUATE_POLICY_TIMEOUT_S``; the server's ``ask_timeout``
            # remains the single real cap. Fast connect so an unreachable
            # server still fails out promptly into the fail-open path below.
            timeout=_ASK_GATE_DELIVERY_TIMEOUT,
        )
        if ap_resp.status_code == 200:
            result = ap_resp.json()
            # A well-formed 200 carries "result"; a malformed body that
            # omits it falls back to _default_action — i.e. DENY on a
            # tool-call phase. That's deliberate: a 200 we can't read is
            # an unevaluable verdict, which fails closed like any other.
            verdict_action = result.get("result", _default_action)
            verdict_reason = result.get("reason")
            verdict_data = result.get("data")
        else:
            _logger.warning(
                "AP policy evaluate returned %d for %s; defaulting to %s",
                ap_resp.status_code,
                evaluation_id,
                _default_action,
            )
    except Exception:  # noqa: BLE001 — fail-open (LLM phases) / fail-closed (tool phases)
        _logger.warning(
            "AP policy evaluate failed for %s; defaulting to %s",
            evaluation_id,
            _default_action,
            exc_info=True,
        )

    # Post the verdict back to the harness as a policy_verdict event.
    try:
        verdict_body: _JsonObject = {
            "type": "policy_verdict",
            "evaluation_id": evaluation_id,
            "action": verdict_action,
        }
        if verdict_reason is not None:
            verdict_body["reason"] = verdict_reason
        if verdict_data is not None:
            verdict_body["data"] = verdict_data
        await harness_client.post(
            f"/v1/sessions/{conversation_id}/events",
            json=verdict_body,
            timeout=30.0,
        )
    except Exception:  # noqa: BLE001 — best-effort delivery
        _logger.warning(
            "Failed to deliver policy verdict %s to harness",
            evaluation_id,
            exc_info=True,
        )


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


def _cache_get_for_agent(
    cache: dict[str, tuple[str | None, Any]], conv_id: str, agent_id: str | None
) -> Any | None:
    """Read an agent-tagged per-session cache entry.

    Every entry is a ``(tagged_agent_id, value)`` pair written by
    :func:`_cache_set_for_agent` — provenance travels with the value
    itself instead of a parallel marker dict that a write could touch
    without the paired cache write (or vice versa). A read only returns
    *value* when the stored tag is ``None`` (agent-independent — e.g. no
    spec resolver is configured, so the entry is valid regardless of who
    this turn is for) or matches *agent_id* exactly.

    A concretely-tagged entry left over from a DIFFERENT agent is always
    a miss — including when *agent_id* is ``None``, i.e. this turn's
    agent is not positively known. "Unknown" must never be treated as
    "no conflict" with a previously-cached agent's data; that conflation
    is exactly the leak this accessor closes.
    """
    entry = cache.get(conv_id)
    if entry is None:
        return None
    tagged_agent_id, value = entry
    if tagged_agent_id is not None and tagged_agent_id != agent_id:
        return None
    return value


def _cache_set_for_agent(
    cache: dict[str, tuple[str | None, Any]],
    conv_id: str,
    agent_id: str | None,
    value: Any,
) -> None:
    """Write *value* into *cache*, tagged with *agent_id* for
    :func:`_cache_get_for_agent` to verify on later reads.
    """
    cache[conv_id] = (agent_id, value)


@dataclasses.dataclass(frozen=True)
class _SessionInitContext:
    """Metadata source selected before shared session initialization runs."""

    envelope: RunnerSessionInitEnvelope | None

    @property
    def labels(self) -> Mapping[str, str] | None:
        """Return server-supplied labels, or ``None`` on the legacy path."""
        return self.envelope.snapshot.labels if self.envelope is not None else None


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
    :param per_request_instructions: The turn's RAW per-request
        instruction text, exactly as the caller sent it, kept separate
        from the already-composed ``instructions`` above. The two are
        not interchangeable: ``instructions`` has the agent's authored
        text (and any framework additions) folded in already, so feeding
        it back into composition would duplicate them, while treating
        the raw text as composed would discard the author's. Carried
        runner-locally rather than on the wire: no wire field distinguishes
        composed from raw text, so the distinction cannot survive a
        round trip and is kept in process instead.
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
    per_request_instructions: str | None = None
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
    """

    def __init__(self, max_tokens: int, actual_tokens: int) -> None:
        self.max_tokens = max_tokens
        self.actual_tokens = actual_tokens
        super().__init__(f"context window exceeded: {actual_tokens} > {max_tokens}")


_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "prompt is too long",
)


def _is_context_overflow_error(event: _JsonObject) -> tuple[int, int] | None:
    """
    Check if a ``response.failed`` SSE event indicates a context-window overflow.

    :param event: The parsed SSE event dict.
    :returns: ``(max_tokens, actual_tokens)`` if overflow detected, else ``None``.
    """
    if event.get("type") != "response.failed":
        return None
    error = cast(_JsonObject, event.get("error", {}))
    msg = str(error.get("message", "")).lower()
    if not any(pat in msg for pat in _CONTEXT_OVERFLOW_PATTERNS):
        return None
    actual_gt_max = re.search(r"(\d{4,})\D*>\D*(\d{4,})", msg)
    if actual_gt_max is not None:
        return int(actual_gt_max.group(2)), int(actual_gt_max.group(1))

    numbers = re.findall(r"(\d{4,})", msg)
    if len(numbers) >= 2:
        return int(numbers[-2]), int(numbers[-1])
    if len(numbers) == 1:
        return int(numbers[0]), int(numbers[0]) + 1
    return 128000, 128001


def _response_failed_event(error: Mapping[str, object]) -> bytes:
    """
    Encode one ``response.failed`` SSE frame.

    Keep a top-level ``error`` mirror for older tests/debuggers that
    inspected the legacy runner proxy shape directly.

    :param error: Error payload to place under ``response.error``,
        e.g. ``{"code": "connection_error", "message": "dropped"}``.
    :returns: UTF-8 encoded SSE frame bytes.
    """
    response = {"status": "failed", "error": error}
    payload = json.dumps({"type": "response.failed", "response": response, "error": error})
    return f"event: response.failed\ndata: {payload}\n\n".encode()


async def _resolve_forwarded_message_content(
    content: list[_JsonObject],
    *,
    session_id: str,
    server_client: httpx.AsyncClient,
) -> list[_JsonObject]:
    """Resolve server-uploaded ``file_id`` blocks inside the runner.

    Remote Omnigent servers can forward session messages with raw file IDs
    because their file store is not available to the out-of-process
    runner. The runner can still fetch bytes through the session-scoped
    file resource endpoint and inline them before handing content to a
    harness. Blocks already resolved by the server pass through.
    """
    if not any(isinstance(block, dict) and has_unresolved_file_id(block) for block in content):
        return content

    resolved: list[_JsonObject] = []
    changed = False
    for block in content:
        new_block = None
        if isinstance(block, dict) and has_unresolved_file_id(block):
            new_block = await resolve_file_id_block(
                block, session_id=session_id, client=server_client
            )
        if new_block is None:
            resolved.append(block)
        else:
            resolved.append(new_block)
            changed = True

    return resolved if changed else content


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


@dataclasses.dataclass
class _SubagentWorkEntry:
    """
    Runner-local state for one asynchronous ``sys_session_send`` dispatch.

    :param parent_session_id: Parent session id that invoked
        ``sys_session_send``, e.g. ``"conv_parent123"``.
    :param child_session_id: Child session id used as the work handle,
        e.g. ``"conv_child456"``.
    :param work_id: Unique id for this dispatch to the child session,
        e.g. ``"subagent_a1b2c3"``.
    :param agent: Sub-agent name from the parent spec, e.g.
        ``"researcher"``.
    :param title: Caller-provided child instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional terminal wrapper label from the
        child session, e.g. ``"codex-native-ui"`` for codex-native
        native sub-agents.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param status: Current work status, e.g. ``"launching"`` or
        ``"running"``.
    :param output: Terminal child output or error text. ``None``
        while the work is still running.
    :param created_at: Unix timestamp when the dispatch was registered.
    :param completed_at: Unix timestamp when the dispatch reached a
        terminal status, or ``None`` while running.
    :param delivered: Whether the terminal payload has been pushed to
        the parent's inbox.
    """

    parent_session_id: str
    child_session_id: str
    work_id: str
    agent: str
    title: str
    wrapper_label: str | None = None
    created_by: str | None = None
    status: str = "launching"
    output: str | None = None
    created_at: float = dataclasses.field(default_factory=time.time)
    completed_at: float | None = None
    delivered: bool = False


@dataclasses.dataclass(frozen=True)
class _SubagentDeliveryAck:
    """
    Result of attempting to deliver a terminal sub-agent payload.

    :param entry: Work entry whose delivery was attempted, or ``None``
        when the child session is not tracked in the work registry.
    :param delivered: Whether the payload is confirmed delivered to the
        parent inbox. True for both first delivery and already-delivered
        duplicate terminal reports.
    :param delivered_now: Whether this attempt pushed a new payload into
        the parent inbox.
    :param reason: Machine-readable outcome, e.g. ``"delivered"`` or
        ``"missing_parent_inbox"``.
    """

    entry: _SubagentWorkEntry | None
    delivered: bool
    delivered_now: bool
    reason: str


_subagent_work_by_child: dict[str, _SubagentWorkEntry] = {}
_subagent_work_by_parent: dict[str, set[str]] = {}
_drained_delivered_subagent_children: set[str] = set()


def register_subagent_work(
    *,
    parent_session_id: str,
    child_session_id: str,
    agent: str,
    title: str,
    wrapper_label: str | None = None,
    created_by: str | None = None,
) -> _SubagentWorkEntry:
    """
    Register one running sub-agent dispatch.

    Re-registering the same child replaces the prior entry so a
    repeated send to an existing child represents the latest turn.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param child_session_id: Child session id, e.g.
        ``"conv_child456"``.
    :param agent: Sub-agent name, e.g. ``"researcher"``.
    :param title: Sub-agent instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional child ``omnigent.wrapper``
        label, e.g. ``"claude-code-native-ui"``.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :returns: The registered work entry.
    """
    prior = _subagent_work_by_child.get(child_session_id)
    if prior is not None:
        children = _subagent_work_by_parent.get(prior.parent_session_id)
        if children is not None:
            children.discard(child_session_id)
            if not children:
                _subagent_work_by_parent.pop(prior.parent_session_id, None)

    entry = _SubagentWorkEntry(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        work_id=f"subagent_{uuid.uuid4().hex[:12]}",
        agent=agent,
        title=title,
        wrapper_label=wrapper_label,
        created_by=created_by,
    )
    _drained_delivered_subagent_children.discard(child_session_id)
    _subagent_work_by_child[child_session_id] = entry
    _subagent_work_by_parent.setdefault(parent_session_id, set()).add(child_session_id)
    return entry


def get_subagent_work(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Return registered sub-agent work by child session id.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The work entry, or ``None`` if the child is not tracked.
    """
    return _subagent_work_by_child.get(child_session_id)


def mark_subagent_work_started(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Promote a sub-agent dispatch from launch bookkeeping to real execution.

    ``sys_session_send`` creates the child session and registers work before
    the child harness has proven it started. The first child
    ``session.status:running`` / ``waiting`` edge is that proof.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The updated work entry, or ``None`` if the child is untracked.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return None
    if entry.status == "launching":
        entry.status = "running"
    return entry


def unregister_subagent_work(
    child_session_id: str,
    *,
    work_id: str | None = None,
    remember_drained_delivery: bool = False,
) -> None:
    """
    Remove sub-agent work tracking for a child session.

    Used when the child-message POST fails before a handle has been
    returned to the LLM.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param work_id: Optional dispatch id guard. When provided, the
        current registry entry is removed only if it still belongs to
        that dispatch.
    :param remember_drained_delivery: Whether to remember a delivered
        entry as drained so duplicate terminal status reports for the
        same child are acknowledged as already delivered.
    :returns: None.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return
    if work_id is not None and entry.work_id != work_id:
        return
    if remember_drained_delivery and entry.delivered:
        _drained_delivered_subagent_children.add(child_session_id)
    _subagent_work_by_child.pop(child_session_id, None)
    children = _subagent_work_by_parent.get(entry.parent_session_id)
    if children is None:
        return
    children.discard(child_session_id)
    if not children:
        _subagent_work_by_parent.pop(entry.parent_session_id, None)


def unregister_subagent_work_for_session(session_id: str) -> None:
    """
    Remove sub-agent work associated with a deleted session.

    A deleted session can be either the child work handle itself or
    the parent that owns several child handles. Both indexes are
    cleaned so runner-local state cannot outlive the session tree.

    :param session_id: Session id being deleted, e.g.
        ``"conv_parent123"`` or ``"conv_child456"``.
    :returns: None.
    """
    unregister_subagent_work(session_id)
    _drained_delivered_subagent_children.discard(session_id)
    for child_id in list(_subagent_work_by_parent.get(session_id, set())):
        _subagent_work_by_child.pop(child_id, None)
        _drained_delivered_subagent_children.discard(child_id)
    _subagent_work_by_parent.pop(session_id, None)


def list_subagent_work(parent_session_id: str) -> list[_SubagentWorkEntry]:
    """
    List sub-agent work registered by a parent session.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Work entries ordered by creation time.
    """
    child_ids = _subagent_work_by_parent.get(parent_session_id, set())
    entries = [
        entry
        for child_id in child_ids
        if (entry := _subagent_work_by_child.get(child_id)) is not None
    ]
    return sorted(entries, key=lambda entry: entry.created_at)


def mark_subagent_work_terminal(
    child_session_id: str,
    *,
    status: str,
    output: str | None,
) -> _SubagentDeliveryAck:
    """
    Mark a sub-agent dispatch terminal and notify the parent inbox.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param status: Terminal status: ``"completed"``, ``"failed"``, or
        ``"cancelled"``.
    :param output: Child output or error text. ``None`` means the
        completion had no assistant text to deliver.
        If an earlier terminal report could not be delivered, a later
        report for the same child replaces the undelivered status and
        output before retrying parent inbox delivery.
    :returns: Delivery acknowledgement for this terminal report.
    :raises ValueError: If ``status`` is not terminal.
    """
    if status not in _SUBAGENT_TERMINAL_STATUSES:
        raise ValueError(
            f"sub-agent terminal status must be one of "
            f"{sorted(_SUBAGENT_TERMINAL_STATUSES)}; got {status!r}"
        )
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        if child_session_id in _drained_delivered_subagent_children:
            return _SubagentDeliveryAck(
                entry=None,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        return _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_UNTRACKED,
        )
    if entry.status in _SUBAGENT_TERMINAL_STATUSES:
        if entry.delivered:
            return _SubagentDeliveryAck(
                entry=entry,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        entry.status = status
        entry.output = output
        entry.completed_at = time.time()
        return _deliver_subagent_completion(entry)
    entry.status = status
    entry.output = output
    entry.completed_at = time.time()
    return _deliver_subagent_completion(entry)


def _deliver_subagent_completion(entry: _SubagentWorkEntry) -> _SubagentDeliveryAck:
    """
    Push a terminal sub-agent payload into the parent session inbox.

    :param entry: Terminal sub-agent work entry to deliver.
    :returns: Delivery acknowledgement describing whether the payload is
        confirmed in the parent inbox.
    """
    if entry.delivered:
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
        )
    inbox = _session_inboxes_ref.get(entry.parent_session_id)
    if inbox is None:
        _logger.warning(
            "Sub-agent work completed but parent inbox is missing; parent=%s child=%s",
            entry.parent_session_id,
            entry.child_session_id,
        )
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX,
        )
    output = entry.output
    if output is None:
        output = "[System: sub-agent completed with no output]"
    inbox.put_nowait(
        {
            "type": "sub_agent",
            "work_id": entry.work_id,
            "task_id": entry.child_session_id,
            "handle_id": entry.child_session_id,
            "conversation_id": entry.child_session_id,
            "tool_name": entry.agent,
            "agent": entry.agent,
            "title": entry.title,
            "status": entry.status,
            "output": output,
        }
    )
    entry.delivered = True
    return _SubagentDeliveryAck(
        entry=entry,
        delivered=True,
        delivered_now=True,
        reason=_SUBAGENT_DELIVERY_DELIVERED,
    )


async def _wake_retry_sleep(seconds: float) -> None:
    """
    Sleep between sub-agent wake-POST retries.

    Indirection point so tests can stub the backoff without clobbering the
    process-wide ``asyncio.sleep`` (the ``no-global-asyncio-patch`` lint
    hook bans patching the module singleton).

    :param seconds: Seconds to wait before the next retry, e.g. ``0.5``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


def _wake_post_is_retryable(exc: httpx.HTTPError) -> bool:
    """
    Return whether a failed wake POST should be retried.

    Transport-level failures (connect/read errors, timeouts) are always
    retryable. A non-2xx response surfaces as :class:`httpx.HTTPStatusError`:
    5xx statuses are transient (notably the 503 ``RUNNER_UNAVAILABLE`` that
    Omnigent returns while the parent's runner tunnel is reconnecting), as
    are a few 4xx codes; every other 4xx is a permanent client-side rejection
    that retrying cannot fix.

    :param exc: HTTP error raised by the wake POST or ``raise_for_status``,
        e.g. an ``httpx.HTTPStatusError`` wrapping a 503 response.
    :returns: ``True`` if a bounded retry is worthwhile, else ``False``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        # Transport failure — the POST may never have reached Omnigent.
        return True
    status_code = exc.response.status_code
    if status_code >= 500:
        return True
    return status_code in _WAKE_POST_TRANSIENT_4XX


async def _deliver_subagent_wake_post(
    server_client: httpx.AsyncClient,
    parent_id: str,
    notice: str,
    *,
    created_by: str | None = None,
) -> bool:
    """
    POST a sub-agent wake notice with a bounded retry on transient failure.

    httpx does not raise on a non-2xx response, so a real 503
    ``RUNNER_UNAVAILABLE`` JSON response (routine while the parent's runner
    tunnel reconnects) would otherwise be treated as a successful delivery.
    This calls ``raise_for_status`` to turn any non-2xx into a failure and
    retries transient failures up to :data:`_WAKE_POST_MAX_ATTEMPTS` with
    exponential backoff, because the wake is the sole delivery signal for
    the last child of a fan-out. Permanent 4xx rejections stop immediately.

    :param server_client: Omnigent HTTP client for the runner subprocess.
    :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
    :param notice: The ``[System: ...]`` notice text to inject.
    :param created_by: Human actor that dispatched the completed child
        turn, if known.
    :returns: ``True`` if a 2xx was confirmed, ``False`` if every attempt
        failed (transport error, timeout, or non-2xx response).
    """
    attribution_created_by = created_by
    for attempt in range(1, _WAKE_POST_MAX_ATTEMPTS + 1):
        try:
            resp = await server_client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": notice}],
                    },
                    **(
                        {"created_by": attribution_created_by}
                        if attribution_created_by is not None
                        else {}
                    ),
                },
                # The server gates this injected wake at the parent's REQUEST
                # phase, which can PARK on a human ASK (e.g. session_cost_budget)
                # for up to the deciding policy's ``ask_timeout`` (default one
                # day). A 30s read budget severed that park after 30s → the
                # TimeoutError below retried → each retry re-posted the notice
                # and parked ANOTHER gate → duplicate approval cards, and the
                # gate never cleanly blocked. Hold the read budget at one day so
                # this POST waits for the real verdict (one held connection, one
                # card); fast connect so an unreachable parent runner still
                # fails out into the bounded retry below.
                timeout=_ASK_GATE_DELIVERY_TIMEOUT,
            )
            # Treat a non-2xx RESPONSE (e.g. a genuine 503 JSONResponse) as a
            # failure — httpx does not raise on status by itself.
            resp.raise_for_status()
            return True
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if (
                attribution_created_by is not None
                and isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 403
            ):
                _logger.debug(
                    "Sub-agent wake POST attribution rejected for parent=%s; "
                    "retrying without actor",
                    parent_id,
                )
                attribution_created_by = None
                continue
            last_attempt = attempt >= _WAKE_POST_MAX_ATTEMPTS
            retryable = isinstance(exc, asyncio.TimeoutError) or _wake_post_is_retryable(exc)
            _logger.debug(
                "Sub-agent wake POST attempt %d/%d for parent=%s failed (retryable=%s): %r",
                attempt,
                _WAKE_POST_MAX_ATTEMPTS,
                parent_id,
                retryable,
                exc,
            )
            if last_attempt or not retryable:
                return False
            delay_s = min(
                _WAKE_POST_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)),
                _WAKE_POST_RETRY_MAX_DELAY_S,
            )
            await _wake_retry_sleep(delay_s)
    return False


def _subagent_delivery_not_confirmed_response(
    ack: _SubagentDeliveryAck,
    *,
    is_runner_known_subagent: bool,
) -> JSONResponse | None:
    """
    Build a 503 response when a known sub-agent result was not delivered.

    Top-level sessions also post terminal status but have no parent inbox, so
    an untracked status remains a no-op unless the runner knows this session
    was created as a sub-agent. For known sub-agents, Omnigent must not receive a
    2xx acknowledgement unless the terminal payload is confirmed in the
    parent's inbox.

    :param ack: Delivery acknowledgement returned by
        ``mark_subagent_work_terminal``.
    :param is_runner_known_subagent: Whether runner session state identifies
        the status sender as a sub-agent child.
    :returns: A 503 JSON response when delivery is not confirmed, or ``None``
        when the status can be acknowledged.
    """
    if ack.delivered:
        return None
    if ack.entry is None and not is_runner_known_subagent:
        return None
    reason = _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY if ack.entry is None else ack.reason
    detail_by_reason = {
        _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY: (
            "Sub-agent terminal status arrived, but the runner has no "
            "tracked work entry to deliver to the parent inbox."
        ),
        _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX: (
            "Sub-agent terminal status arrived, but the parent inbox is missing on this runner."
        ),
    }
    detail = detail_by_reason[reason]
    return JSONResponse(
        status_code=503,
        content={
            "error": "subagent_delivery_not_confirmed",
            "reason": reason,
            "detail": detail,
        },
    )


def _format_subagent_wake_notice(*, agent: str, title: str, status: str, pending: int) -> str:
    """
    Build the framework notice that wakes a parent after a child finishes.

    :param agent: Sub-agent name from the parent spec, e.g. ``"researcher"``.
    :param title: Child instance title supplied at dispatch, e.g. ``"auth"``.
    :param status: Terminal child status, e.g. ``"completed"``, ``"failed"``,
        or ``"cancelled"``.
    :param pending: Number of undrained items in the parent inbox, e.g. ``3``.
    :returns: A ``[System: ...]`` notice string, e.g. ``"[System: sub-agent
        researcher/auth finished (completed) — 1 result waiting in inbox. Call
        sys_read_inbox to collect.]"``.
    """
    noun = "result" if pending == 1 else "results"
    return (
        f"[System: sub-agent {agent}/{title} finished ({status}) — "
        f"{pending} {noun} waiting in inbox. Call sys_read_inbox to collect.]"
    )


# Max length of a child message preview mirrored to the parent stream.
# Matches the server-side ``_latest_message_preview`` truncation so the
# live runner-pushed preview and the snapshot preview look the same.
_CHILD_PREVIEW_MAX_CHARS = 150


@dataclasses.dataclass
class _ChildParentMeta:
    """Fan-out metadata for one child sub-agent session.

    Lets the runner mirror a child's status/preview deltas onto the
    PARENT's SSE stream — the child's own relay isn't running when only
    the parent is viewed, and the runner runs the child turn (affinity).

    :param parent_id: Parent session id whose stream receives the deltas.
    :param title: Child title ``"{tool}:{session_name}"`` — carried in
        status deltas so even a cold update has a display name.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    :param last_busy: Last busy value fanned out, used to coalesce
        duplicate status deltas. ``None`` until first publish.
    :param last_task_status: Last child-rail task status fanned out, e.g.
        ``"completed"``. Tracked separately so ``idle`` → ``failed`` emits
        even though both states are non-busy.
    :param last_error: Last child failure detail fanned out, used to emit a
        new parent update when only the error changes, and to clear stale
        errors on a later running/waiting edge.
    """

    parent_id: str
    title: str
    tool: str
    session_name: str
    last_busy: bool | None = None
    last_task_status: str | None = None
    last_error: tuple[str, str] | None = None


# child_session_id -> :class:`_ChildParentMeta`. Populated at spawn (see
# tool_dispatch._execute_subagent_tool), dropped when the child ends.
_child_session_parents: dict[str, _ChildParentMeta] = {}


def register_child_session(
    child_session_id: str,
    *,
    parent_session_id: str,
    title: str,
    tool: str,
    session_name: str,
) -> None:
    """
    Record a child→parent mapping for SSE status/preview fan-out.

    :param child_session_id: Child session id, e.g. ``"conv_child123"``.
    :param parent_session_id: Parent session id whose stream should
        receive the child's deltas, e.g. ``"conv_parent987"``.
    :param title: Child title, ``"{tool}:{session_name}"``.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    """
    _child_session_parents[child_session_id] = _ChildParentMeta(
        parent_id=parent_session_id,
        title=title,
        tool=tool,
        session_name=session_name,
    )


def unregister_child_session(child_session_id: str) -> None:
    """
    Drop a child→parent mapping when the child session ends.

    :param child_session_id: Child session id to forget.
    """
    _child_session_parents.pop(child_session_id, None)


def _session_status_to_task_status(status: object) -> str | None:
    """
    Map a ``session.status`` value to a child summary ``current_task_status``.

    The two vocabularies differ (session status vs. task status); this
    keeps the child rail's status text roughly in sync as ``busy`` flips.

    :param status: A ``session.status`` value, e.g. ``"running"``.
    :returns: ``"launching"`` / ``"in_progress"`` / ``"completed"`` /
        ``"failed"``, or ``None`` for an unrecognized status (caller
        omits the field).
    """
    if status == "launching":
        return "launching"
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status == "failed":
        return "failed"
    return None


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

    :param error: Raw error dict from a ``_on_proxy_stream_end`` call,
        e.g. ``{"message": "turn setup failed: ..."}`` or
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
    raw_code = error.get("type")
    code = raw_code if isinstance(raw_code, str) and raw_code else "runner_error"
    return {"code": code, "message": message}


def _truncate_child_preview(text: str) -> str:
    """
    Truncate a child message preview to the cap with an ellipsis.

    Matches the server-side ``_latest_message_preview`` truncation so the
    live runner-pushed preview and the snapshot preview look the same.

    :param text: The child's latest assistant reply text.
    :returns: ``text`` truncated to :data:`_CHILD_PREVIEW_MAX_CHARS` with
        a trailing ellipsis when longer, else ``text`` unchanged.
    """
    if len(text) > _CHILD_PREVIEW_MAX_CHARS:
        return text[:_CHILD_PREVIEW_MAX_CHARS].rstrip() + "…"
    return text


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

# Module-level ref to _session_inboxes. Populated inside create_runner_app;
# used by the sub-agent work registry to deliver completions to the parent.
_session_inboxes_ref: dict[str, asyncio.Queue[_JsonObject]] = {}


def get_session_agent_id(session_id: str) -> str | None:
    """
    Return the durable agent_id for a session.

    :param session_id: Session/conversation ID, e.g.
        ``"conv_abc123"``.
    :returns: The agent_id, or ``None`` if not found.
    """
    return _session_agent_ids_ref.get(session_id)


# How long a session's discovered skills stay cached before the runner
# re-walks the filesystem. Short enough that a skill or plugin installed
# mid-session surfaces in the composer menu without a session restart, long
# enough to collapse the bursty menu-open + per-invocation resolve calls onto
# a single walk. Module-level so it can be tuned/patched in one place.
_SESSION_SKILLS_CACHE_TTL_SECONDS = 60.0
_SESSION_INIT_ENVELOPE_TTL_SECONDS = 60.0


class _BodyRequest:
    """Minimal stand-in for a Starlette ``Request`` exposing only ``json()``.

    Lets internal callers reuse a route handler that consumes the request
    solely for its JSON body (e.g. ``create_session_terminal``) without
    constructing a real ASGI ``Request``. Not a general Request substitute.
    """

    def __init__(self, body: _JsonObject) -> None:
        self._body = body

    async def json(self) -> _JsonObject:
        return self._body


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
    # conversation_id → EVERY (effective_harness, InstructionDelivery) pair the
    # delivery-gap warning has already fired for in that conversation.
    #
    # At most once per (conversation, effective harness, instruction-delivery
    # value), for the LIFETIME of the conversation. That is a membership
    # question, not a comparison against the most recent pair:
    # holding only the last pair would let A -> B -> A warn for A twice, since
    # B displaces A's record and A then looks unseen. Presence of the
    # conversation alone is equally wrong in the other direction — it cannot
    # distinguish "already warned about THIS agent's harness" from "already
    # warned about SOME agent's", which silently suppresses a genuine new gap.
    # Popped only at delete_session; deliberately NOT cleared by
    # _clear_session_agent_caches, which would collapse this to per-switch
    # behaviour.
    _instruction_delivery_warned: dict[str, set[tuple[str | None, InstructionDelivery]]] = {}
    # agent_id → cached AgentSpec for terminal tools. GLOBAL and keyed by agent
    # id, not by session — so unlike the per-session caches below, entries are
    # shared across conversations, and its write guard
    # (_agent_spec_cache_put) can only see the INITIATING session's
    # invalidations. A stale entry reinstated by one conversation is therefore
    # visible to the others; see that function for the exact sequence and why
    # closing it is deferred.
    _spec_cache: dict[str, _SpecEntry] = {}
    _resp_to_conv: dict[str, str] = {}  # harness response_id → conversation_id
    _live_response_id: dict[str, str] = {}
    _session_start_cache: dict[str, float] = {}  # session_id → registered start time
    # Agent-tagged per-session caches: each value is a
    # ``(tagged_agent_id, value)`` pair. Writes go through
    # _cache_set_for_agent (itself reached only via _session_cache_put); reads
    # normally go through _cache_get_for_agent, which will not hand back a
    # value alongside provenance it was not written with (see those functions'
    # docstrings).
    #
    # Two reads deliberately bypass the accessor, because they want the TAG
    # rather than a provenance-checked value — the accessor only returns the
    # value half and would discard exactly what they came for:
    #   - ``_run_turn_bg_setup_and_stream``'s on-demand resolution branch,
    #     taken when the turn carries no explicit agent id, asks WHICH agent
    #     the entry it just resolved was tagged for, so the rest of that turn
    #     tags its own writes identically instead of guessing. It also reads
    #     the value half for the workdir, having just resolved it itself.
    #   - ``_resolve_session_skills`` reads the same tag to key the skills TTL
    #     cache to the agent the spec cache actually resolved.
    # Both read the tag via ``[0]``; neither treats the value half as
    # provenance-checked.
    #
    # ``_session_snapshot_cache`` is the one cache with no separate tag — its
    # value (_SessionSnapshot) already carries its own ``agent_id`` field, so
    # the entry is self-describing.
    # session_id → AgentSpec
    _session_spec_cache: dict[str, tuple[str | None, _SpecEntry | None]] = {}
    # Provenance for the entry beside it in ``_session_spec_cache`` — see
    # :class:`_SubAgentProvenance` for the three states and why two are not
    # enough. Without it a cached PARENT kept after a miss is
    # indistinguishable from a resolved child on every later turn, and the
    # readers below decide "already the child?" by name equality, which a root
    # whose own name matches the request satisfies. Written only by
    # :func:`_session_spec_cache_put`, which publishes both halves together.
    _session_sub_agent_fallbacks: dict[str, tuple[str | None, _SubAgentProvenanceValue]] = {}
    _session_snapshot_cache: dict[str, _SessionSnapshot] = {}  # session_id → snapshot
    _session_snapshot_locks: dict[str, asyncio.Lock] = {}  # session_id → snapshot fetch lock
    _session_spec_locks: dict[str, asyncio.Lock] = {}  # session_id → spec resolution lock
    _session_init_tasks: dict[tuple[str, str, str | None], asyncio.Task[JSONResponse]] = {}
    _session_init_envelopes: dict[str, tuple[float, RunnerSessionInitEnvelope]] = {}
    _session_skills_cache: dict[str, tuple[str | None, tuple[float, list[SkillSpec]]]] = {}
    _session_workspace_cache: dict[str, str | None] = {}  # session_id → workspace path
    _session_cursor_model_names: dict[str, dict[str, str]] = {}
    _session_claude_launch_configs: dict[str, ClaudeNativeUcodeConfig | None] = {}
    _session_claude_launch_config_tasks: dict[
        str, asyncio.Task[ClaudeNativeUcodeConfig | None]
    ] = {}

    # Monotonic per-session fill guard. Evicting a cache cannot stop a fill
    # that is already in flight: a resolve started before an agent switch can
    # complete after it and write the OLD agent's value straight back over the
    # eviction, so the next reader sees the previous agent again with no fetch
    # of its own. Every fill that spans an await captures this counter before
    # its first await and discards its result on write if
    # _clear_session_agent_caches has bumped it since.
    _session_cache_generations: dict[str, int] = {}

    def _session_cache_generation(session_id: str) -> int:
        """Return the fill generation a cache fill for *session_id* starts under.

        Materializes the counter so that a later teardown (which pops it) is
        distinguishable from "never switched" — see
        :func:`_session_cache_generation_is_current`.
        """
        return _session_cache_generations.setdefault(session_id, 0)

    def _session_cache_generation_is_current(session_id: str, generation: int) -> bool:
        """Return whether a fill that started at *generation* may still write.

        An ABSENT counter means the session was torn down while this fill was
        in flight, and is never current: a fill that captured generation 0
        would otherwise match the ``0`` default and land its write after
        ``delete_session`` had already dropped every cache for that id —
        re-creating entries for a dead session, and for the comment relay
        re-creating a live bridge that nothing is left to close.

        This does NOT amount to a lifecycle guarantee. Two conditions defeat
        it, both pre-existing and both deliberately out of scope here:

        - Session ids are caller-supplied and imports reuse them
          deterministically, so a torn-down id can come back. A fill parked
          across teardown, followed by re-creation of the SAME id, sees the
          counter re-materialize at 0 and matches its captured 0 — a genuine
          ABA, publishing an entry created for the old session into the new
          one wearing that id. Only the timing makes it rare, not the design.
        - Normal server-side session deletion calls the runner's ``/resources``
          cleanup, NOT full runner session teardown. So these caches and the
          comment relay can outlive a deletion outright, with no race involved
          at all — an entry simply survives the session it was created for.

        Both mean an entry can outlive its session, so read a "current"
        verdict as "this session did not invalidate me", never as "this entry
        certainly belongs to a live session".
        """
        current = _session_cache_generations.get(session_id)
        return current is not None and current == generation

    _UNTAGGED_CACHE_WRITE = object()

    def _session_cache_put(
        cache: dict[str, Any],
        session_id: str,
        value: Any,
        *,
        generation: int,
        agent_tag: Any = _UNTAGGED_CACHE_WRITE,
    ) -> bool:
        """THE sanctioned write path for every per-session agent-derived cache.

        Guarding fill *sites* individually cannot hold the invariant: a
        writer that does not pass through an enumerated site is invisible to
        the enumeration. Routing every write through here makes the invariant
        a property of the write path instead of a convention: no raw
        ``cache[session_id] = value`` assignment to a protected container may
        exist outside this function.

        :param cache: The protected per-session container.
        :param session_id: Session the value was resolved for.
        :param value: Value to publish.
        :param generation: Generation captured BEFORE the fill's first await.
        :param agent_tag: Provenance tag for agent-tagged caches; omit for
            plain untagged containers.
        Scope: CACHED VALUES only. Resources built alongside those values —
        terminals, forwarders, bridge state, harness clients — are not fenced
        by this and are not rolled back when a write is dropped.

        Every write to a protected container goes through one of two guarded
        setters: this one, and :func:`_agent_spec_cache_put` for the
        agent-keyed ``_spec_cache``. The protected containers are exactly
        those :func:`_clear_session_agent_caches` evicts. What that enforces
        is the write PATH — a raw ``cache[session_id] = value`` assignment
        bypasses the guard entirely, so the invariant holds exactly as long
        as no such assignment exists.

        Both setters answer the SAME-SESSION question — "did this session's
        agent change while my fill was in flight" — and answer it correctly.
        Neither answers the cross-conversation one, which only matters for the
        global agent-keyed ``_spec_cache``; see :func:`_agent_spec_cache_put`
        for that gap. The write-path guarantee above says only that the guard
        cannot be bypassed, not that that cache is safe against another
        conversation's concurrent invalidation.

        :returns: ``True`` if the value was published, ``False`` if the fill
            was invalidated while in flight and the write was dropped. A
            ``False`` return says nothing about resources built alongside the
            value; none of them are rolled back.
        """
        if not _session_cache_generation_is_current(session_id, generation):
            return False
        if agent_tag is _UNTAGGED_CACHE_WRITE:
            cache[session_id] = value
        else:
            _cache_set_for_agent(cache, session_id, agent_tag, value)
        return True

    def _session_spec_cache_put(
        session_id: str,
        value: _SpecEntry | None,
        *,
        generation: int,
        agent_tag: str | None,
        provenance: _SubAgentProvenanceValue,
    ) -> bool:
        """THE write path for ``_session_spec_cache``, provenance included.

        A spec entry and the answer to "is this the resolved child, the PARENT
        kept after a miss, or not yet decided?" are one fact, so they are
        published by one call. *provenance* has no default: a caller cannot
        publish a spec without stating which of the three it is.

        Both halves ride the same *generation*, so an invalidation that drops
        one drops the other; neither can survive alone.

        :param session_id: Session the spec was resolved for.
        :param value: The spec entry to publish.
        :param generation: Generation captured BEFORE the fill's first await.
        :param agent_tag: Provenance tag, as for :func:`_session_cache_put`.
        :param provenance: The sub-agent name *value* failed to resolve, or
            :data:`_SubAgentProvenance.RESOLVED` / ``UNDETERMINED``.
        :returns: ``True`` if published, ``False`` if dropped as stale.
        """
        if not _session_cache_put(
            _session_spec_cache,
            session_id,
            value,
            generation=generation,
            agent_tag=agent_tag,
        ):
            return False
        _session_cache_put(
            _session_sub_agent_fallbacks,
            session_id,
            provenance,
            generation=generation,
            agent_tag=agent_tag,
        )
        return True

    def _session_spec_provenance(
        session_id: str, agent_id: str | None
    ) -> _SubAgentProvenanceValue:
        """Return what the cached entry records about its sub-agent.

        An absent record reads as :data:`_SubAgentProvenance.UNDETERMINED`
        rather than as a resolution: nothing has stated otherwise, and both
        consumers below are allowlists that admit only states someone
        positively recorded.

        They ask DIFFERENT questions, so neither trusts the same set:
        :func:`_session_spec_is_resolved_child` admits ``RESOLVED`` alone,
        because a fallback parent is not the child; and
        :func:`_session_spec_provenance_is_settled` also admits a recorded
        fallback name, because a reader may serve an entry whose sub-agent
        question was answered either way.
        """
        recorded = _cache_get_for_agent(_session_sub_agent_fallbacks, session_id, agent_id)
        if recorded is None:
            return _SubAgentProvenance.UNDETERMINED
        return cast(_SubAgentProvenanceValue, recorded)

    def _session_spec_is_resolved_child(session_id: str, agent_id: str | None) -> bool:
        """Return whether the cached entry is a POSITIVELY resolved spec.

        The single allowlist behind every "may I treat the cached spec as the
        already-resolved child?" decision. Only
        :data:`_SubAgentProvenance.RESOLVED` qualifies — a recorded fallback
        name, ``UNDETERMINED``, and an absent record all answer ``False``.
        Written as an allowlist rather than "not a fallback" so a provenance
        state added later is untrusted until someone says otherwise, instead
        of joining the trusted set by default.
        """
        return _session_spec_provenance(session_id, agent_id) is _SubAgentProvenance.RESOLVED

    def _session_spec_provenance_is_settled(recorded: _SubAgentProvenanceValue) -> bool:
        """Return whether *recorded* is an ANSWER a cache hit may be served on.

        The second allowlist, and deliberately a different question from
        :func:`_session_spec_is_resolved_child`. A reader handing back a
        cached entry may serve it when the sub-agent question was settled
        EITHER way — positively resolved, or resolved to a miss whose name is
        recorded (which that reader reports as it hands the parent over). A
        decider asking "is this cached spec the child?" must accept only the
        first, so the two questions cannot share one helper.

        Both are allowlists for the same reason: written as "not
        ``UNDETERMINED``" they would admit any state added later by default,
        which is the fail-open shape this machinery exists to remove.
        """
        return isinstance(recorded, str) or recorded is _SubAgentProvenance.RESOLVED

    def _session_spec_fallback_name(session_id: str, agent_id: str | None) -> str | None:
        """Return the sub-agent name the cached spec FAILED to resolve, if any.

        ``None`` covers both a positively resolved child and an undetermined
        entry — neither has a name to report. A non-``None`` name means the
        cached spec is the parent, and the session is running something other
        than the sub-agent it is bound to.
        """
        recorded = _session_spec_provenance(session_id, agent_id)
        return recorded if isinstance(recorded, str) else None

    def _agent_spec_cache_put(
        agent_id: str, value: Any, *, session_id: str, generation: int
    ) -> bool:
        """Sanctioned write path for the agent-keyed ``_spec_cache``.

        Keying by agent id does NOT make this safe on its own: a same-agent
        spec/MCP edit resets the session and pops the entry, after which an
        older in-flight fill would reinstate the stale spec under the
        UNCHANGED agent id. The generation of the SESSION the fill was started
        for decides whether it may publish, regardless of what the value is
        keyed by.

        WHAT THIS VALIDATES: the generation of ``session_id`` — the session
        that initiated this fill. It answers "did THIS session's agent change
        while I was in flight", and for that question it is effective. That is
        the same-session staleness this guard addresses.

        WHAT IT DOES NOT VALIDATE: anything driven by a DIFFERENT conversation.
        ``_spec_cache`` is global and keyed by agent id, while the generation
        counter is per-session, so it cannot answer "did THIS AGENT change
        while I was in flight" — which is the question a globally-shared
        agent-keyed cache actually needs to ask. Concretely: conversation S1
        starts resolving agent A; conversation S2 resets agent A and pops
        ``_spec_cache[A]``; S1's own generation never moved, because S2's reset
        is not S1's reset, so S1's write is judged current and REINSTATES the
        stale entry, which later reads then consume across conversations.

        That cross-conversation stale reinstatement is a known gap. This
        guard closes the same-session half and leaves the cross-conversation
        half no worse than before. Closing it properly needs an agent-scoped
        epoch rather than a session-scoped one.

        :param agent_id: Cache key.
        :param value: Resolved spec entry.
        :param session_id: Session whose generation fences this fill. Only
            this session's invalidations are seen; see above.
        :param generation: Generation captured before the resolver await.
        :returns: ``True`` if published, ``False`` if dropped as stale by the
            initiating session's own generation. ``True`` does NOT mean no
            other conversation invalidated this agent meanwhile.
        """
        if not _session_cache_generation_is_current(session_id, generation):
            return False
        _spec_cache[agent_id] = value
        return True

    def _guarded_launch_config_recorder(
        session_id: str,
    ) -> Callable[[str, ClaudeNativeUcodeConfig | None], None]:
        """Return a generation-checked ``record_launch_config`` callback.

        The raw ``dict.__setitem__`` used to be handed across the module
        boundary into ``_auto_create_claude_terminal``, which calls it after
        awaiting resolution — so a reset during that await repopulated the old
        config after eviction. The guard has to travel with the callback, not
        stay on this side of the boundary.

        :param session_id: Session the terminal is being created for.
        :returns: Callback that publishes only if still current.
        """
        _generation = _session_cache_generation(session_id)

        def _record(sid: str, config: ClaudeNativeUcodeConfig | None) -> None:
            _session_cache_put(_session_claude_launch_configs, sid, config, generation=_generation)

        return _record

    async def _resolve_session_claude_launch_config(
        session_id: str,
    ) -> ClaudeNativeUcodeConfig | None:
        if session_id in _session_claude_launch_configs:
            return _session_claude_launch_configs[session_id]
        task = _session_claude_launch_config_tasks.get(session_id)
        if task is None:
            from omnigent.claude_native import resolve_native_claude_config

            _load_generation = _session_cache_generation(session_id)

            async def _load() -> ClaudeNativeUcodeConfig | None:
                spec = await _resolve_session_agent_spec(
                    session_id, agent_id_hint=_session_agent_ids.get(session_id)
                )
                config = await asyncio.to_thread(resolve_native_claude_config, spec=spec)
                _session_cache_put(
                    _session_claude_launch_configs,
                    session_id,
                    config,
                    generation=_load_generation,
                )
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
        task = _session_claude_launch_config_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    _session_agent_ids = _session_agent_ids_ref  # shared with module-level get_session_agent_id
    _session_sub_agent_names: dict[str, str] = {}
    _session_tool_schemas: dict[str, tuple[str | None, list[_JsonObject]]] = {}
    _session_mcp_spec_hash: dict[str, tuple[str | None, str]] = {}  # session_id → last MCP hash
    _session_comment_relays: dict[str, ClaudeNativeToolRelay] = {}
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
    app.state.antigravity_terminal_ensure_locks = _antigravity_terminal_ensure_locks
    _repl_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _active_turns: dict[str, asyncio.Task[None] | None] = {}
    _native_pane_status: dict[str, str] = {}
    _session_message_buffers: dict[str, list[_JsonObject]] = {}
    _author_attribution_sessions: set[str] = set()
    _ingest_next_seq: dict[str, int] = {}
    _ingest_now_serving: dict[str, int] = {}
    _ingest_cond: dict[str, asyncio.Condition] = {}
    _interrupted_sessions: set[str] = set()
    app.state.interrupted_sessions = _interrupted_sessions
    _background_tasks: set[asyncio.Task[object]] = set()
    _subagent_wake_pending: set[str] = set()

    _session_histories = _session_histories_ref
    _last_server_item_id: dict[str, str] = {}
    _session_event_queues = _session_event_queues_ref
    _session_inboxes = _session_inboxes_ref
    _session_async_tasks: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}

    def _has_active_work() -> bool:
        if _active_turns:
            return True
        if _has_live_async_tasks(_session_async_tasks):
            return True
        for timers in _session_timers.values():
            for timer_task in timers.values():
                if not timer_task.done():
                    return True
        if pending_approvals.has_any_pending():
            return True
        if process_manager is not None:
            session_ids = set(_session_start_cache) | set(_session_agent_ids)
            if any(process_manager.has_active_turn(session_id) for session_id in session_ids):
                return True
        return False

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
        _publish_event(
            session_id,
            {
                "type": "session.terminal.activity",
                "session_id": session_id,
                "terminal_id": terminal_id,
            },
        )

    resource_registry.set_terminal_activity_publisher(_publish_terminal_activity)

    def _publish_session_status(session_id: str, status: str) -> None:
        _publish_event(
            session_id,
            {"type": "session.status", "status": status},
        )

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

    def _format_required_terminal_exit_output(event: TerminalExitEvent) -> str:
        command = _format_terminal_command_for_failure(event)
        cwd = event.cwd or "unknown"
        parts = [
            "Required terminal exited unexpectedly; the session runtime is no longer available.",
            "",
            "Terminal diagnostics:",
            f"terminal: {event.terminal_name}:{event.session_key}",
            f"command: {command}",
            f"cwd: {cwd}",
        ]
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

    def _release_required_terminal_session(session_id: str) -> None:
        if process_manager is None:
            return

        async def _release() -> None:
            try:
                await process_manager.release(session_id)
            except Exception:
                _logger.exception(
                    "Failed to release harness subprocess after required terminal exit: "
                    "session=%s",
                    session_id,
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
        # A codex TUI pane that exits on its own (crash / OOM / host recycle)
        # never runs the DELETE-session cleanup, so its per-session app-server
        # + forwarder would linger with no TUI. Tear them down here; no-op for
        # any session without a registered codex app-server.
        _teardown_task = asyncio.create_task(
            _native_runtime.teardown_codex_native_app_server(event.session_id)
        )
        _teardown_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_teardown_task)
        if event.lifecycle != TerminalLifecycle.REQUIRED:
            return

        if event.terminal_name in ("qwen", "antigravity") and event.session_key == "main":
            _publish_event(event.session_id, {"type": "session.status", "status": "idle"})
            _release_required_terminal_session(event.session_id)
            return

        if event.session_was_idle:
            _release_required_terminal_session(event.session_id)
            return

        output = _format_required_terminal_exit_output(event)
        _publish_event(
            event.session_id,
            {
                "type": "session.status",
                "status": "failed",
                "error": {
                    "code": "required_terminal_exited",
                    "message": output,
                },
            },
        )
        _mark_subagent_terminal_and_wake(
            event.session_id,
            status="failed",
            output=output,
        )
        _release_required_terminal_session(event.session_id)

    resource_registry.set_terminal_exit_publisher(_publish_terminal_exit)

    from omnigent.runtime.filesystem_registry import (
        FilesystemRegistry,
        create_filesystem_registry,
    )

    if runner_workspace is not None:
        filesystem_registry = create_filesystem_registry(watch_path=runner_workspace)
        filesystem_registry.start()
    else:
        filesystem_registry = None
    app.state.filesystem_registry = filesystem_registry

    _session_fs_registries: dict[str, FilesystemRegistry] = {}

    async def _session_snapshot(session_id: str) -> _SessionSnapshot:
        cached = _session_snapshot_cache.get(session_id)
        if cached is not None:
            return cached
        _fill_generation = _session_cache_generation(session_id)
        lock = _session_snapshot_locks.setdefault(session_id, asyncio.Lock())
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
                resp = await server_client.get(f"/v1/sessions/{session_id}")
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
                _session_cache_put(
                    _session_snapshot_cache,
                    session_id,
                    snapshot,
                    generation=_fill_generation,
                )
            return snapshot

    async def _session_workspace_value(session_id: str) -> str | None:
        if session_id not in _session_workspace_cache:
            snapshot = await _session_snapshot(session_id)
            if snapshot.ok:
                _session_workspace_cache[session_id] = snapshot.workspace
            else:
                return snapshot.workspace
        return _session_workspace_cache.get(session_id)

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
        generation: int,
    ) -> _SessionInitContext:
        """Publish the envelope's derived state under the CALLER's generation.

        *generation* must be the one captured before the envelope was
        obtained, not one read here: a fill's guard only rejects a stale
        write if its generation predates the resolution of the data being
        written, and this function only formats data resolved upstream.
        """
        if envelope.session_id != session_id or envelope.agent_id != agent_id:
            raise ValueError("session initialization envelope identity mismatch")

        global _server_version
        _server_version = envelope.server_version
        snapshot = envelope.snapshot
        _envelope_snapshot = _SessionSnapshot(
            ok=True,
            status_code=200,
            created_at=float(snapshot.created_at),
            workspace=snapshot.workspace,
            agent_id=agent_id,
            sub_agent_name=envelope.sub_agent_name,
            parent_session_id=snapshot.parent_session_id,
        )
        _session_cache_put(
            _session_snapshot_cache,
            session_id,
            _envelope_snapshot,
            generation=generation,
        )
        _session_start_cache[session_id] = float(snapshot.created_at)
        _session_workspace_cache[session_id] = snapshot.workspace
        if envelope.sub_agent_name:
            _session_sub_agent_names[session_id] = envelope.sub_agent_name
        _session_cache_put(
            _session_init_envelopes,
            session_id,
            (time.monotonic(), envelope),
            generation=generation,
        )
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
        generation: int,
    ) -> _SessionInitContext:
        envelope = parse_runner_session_init_envelope(body)
        if envelope is None:
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
            generation=generation,
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
        FilesystemEntry,
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
            UnsupportedMediaType,
        )

        status = 500
        if isinstance(exc, FilesystemPathNotFound):
            status = 404
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
            content={
                "error": {"code": exc.code, "message": exc.message},
            },
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

        # Only the name is wanted here: this is a title hint, it publishes no
        # cache entry and states no resolution, so "unknown" and "none" lead
        # to the same generated title.
        sub_agent_name = body.sub_agent_name or (await _recover_sub_agent(conversation_id)).name
        resolver_agent_id = body.agent_id or _session_agent_ids.get(conversation_id)
        resolver_cwd = await _session_runtime_cwd(conversation_id)
        try:
            effective_harness, spawn_env = await _resolve_harness_config(
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
            session_spec=_unwrap_spec_entry(
                _cache_get_for_agent(_session_spec_cache, conversation_id, resolver_agent_id)
            ),
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
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner POST /v1/sessions needs a HarnessProcessManager."),
                },
            )
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        _init_generation = _session_cache_generation(cast(str, session_id)) if session_id else 0
        if not session_id or not agent_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": ("'session_id' and 'agent_id' required."),
                },
            )
        session_id = cast(str, session_id)
        agent_id = cast(str, agent_id)

        try:
            init_context = await _load_session_init_context(
                body,
                session_id=session_id,
                agent_id=agent_id,
                generation=_init_generation,
            )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Invalid session initialization envelope.",
                },
            )

        spec: AgentSpec | None = None
        spec_entry: _SpecEntry | None = None
        # Travels with the spec into the session cache below: the requested
        # sub-agent's name when it does not resolve and the parent is kept,
        # otherwise RESOLVED. This route decides before it publishes, so it
        # never has to say UNDETERMINED.
        _sa_provenance: _SubAgentProvenanceValue = _SubAgentProvenance.RESOLVED
        if spec_resolver is not None:
            try:
                spec_entry = await spec_resolver(agent_id, session_id)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
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
            if _sa_name_assign:
                from omnigent.runtime.workflow import _find_spec_by_name

                _sub_spec = _find_spec_by_name(spec, _sa_name_assign)
                if _sub_spec is None:
                    # A requested sub-agent that no longer resolves leaves the
                    # PARENT spec driving the session: the swap below is
                    # skipped, so harness, instructions and start-gate all come
                    # from the parent exactly as for a request that named no
                    # sub-agent. This used to answer 404 instead. The logged
                    # warning is the only signal a caller gets — the response
                    # is an ordinary success and carries no marker.
                    _warn_unresolved_sub_agent(session_id, _sa_name_assign)
                    _sa_provenance = _sa_name_assign
                else:
                    spec = _sub_spec
                    spec_entry = (
                        ResolvedSpec(spec=spec, workdir=_resolved_spec_workdir(spec_entry))
                        if _resolved_spec_workdir(spec_entry) is not None
                        else spec
                    )
            harness_name = spec.executor.config.get("harness") or spec.executor.type
            harness_name = canonicalize_harness(harness_name) or harness_name

            _start_verdict = await _evaluate_agent_start_gate(spec, harness_name)
            if _start_verdict is not None:
                if _start_verdict.action in ("deny", "ask"):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "agent_start_denied",
                            "detail": _start_verdict.deny_text or "Agent start denied by policy",
                        },
                    )
                if _start_verdict.data is not None:
                    _apply_sandbox_override_from_verdict(spec, _start_verdict.data)

            spawn_env = _build_spawn_env_from_spec(
                spec,
                harness_name,
                workdir=_resolved_spec_workdir(spec_entry),
                cwd=await _session_runtime_cwd(session_id),
            )
            if spawn_env is None:
                spawn_env = await _resolve_native_spawn_env(
                    harness_name,
                    session_id,
                    server_client=server_client,
                    optional_labels=init_context.labels,
                )
            _session_spec_cache_put(
                session_id,
                spec_entry,
                generation=_init_generation,
                agent_tag=agent_id,
                provenance=_sa_provenance,
            )
        else:
            harness_name = "runner-test-default"
            spawn_env = None

        try:
            await process_manager.get_client(
                session_id,
                harness_name,
                env=spawn_env,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _session_start_cache.setdefault(session_id, time.time())
        _session_cache_put(_session_agent_ids, session_id, agent_id, generation=_init_generation)
        if session_id not in _session_event_queues:
            _session_event_queues[session_id] = asyncio.Queue()
        if session_id not in _session_inboxes:
            _session_inboxes[session_id] = asyncio.Queue()
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
            _launch_locks = {
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
            }[_native_agent.key]
            _launch_ctx = NativeLaunchContext(
                session_id=session_id,
                resource_registry=resource_registry,
                publish_event=_publish_event,
                server_client=server_client,
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
                        )
                    return PreLaunchResult(force_recreate=wants_rebuild, skip=inbound)

                async def _claude_build_context(ctx: NativeLaunchContext) -> NativeLaunchContext:
                    bundle_dir: Path | None = None
                    agent_name: str | None = None
                    skills_filter: str | list[str] = "all"
                    try:
                        spec = await _resolve_session_agent_spec(
                            session_id, agent_id_hint=agent_id
                        )
                    except OmnigentError:
                        spec = None
                        _logger.info(
                            "Claude terminal spec resolution failed; continuing without "
                            "bundle skills: session=%s",
                            session_id,
                        )
                    if spec is not None:
                        entry = _cache_get_for_agent(_session_spec_cache, session_id, agent_id)
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
                        record_launch_config=_guarded_launch_config_recorder(session_id),
                    )

                _launch_pre = _claude_pre_launch
                _launch_build = _claude_build_context

            elif harness_name == "codex-native":

                async def _codex_pre_launch(has_terminal: bool) -> PreLaunchResult:
                    needs = await _codex_session_needs_runner_terminal(server_client, session_id)
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
                        spec = await _resolve_session_agent_spec(
                            session_id, agent_id_hint=agent_id
                        )
                    except OmnigentError:
                        spec = None
                    if spec is not None:
                        entry = _cache_get_for_agent(_session_spec_cache, session_id, agent_id)
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
                _launch_resolve_spec = lambda: _resolve_session_agent_spec(  # noqa: E731
                    session_id, agent_id_hint=agent_id
                )
            elif harness_name in ("cursor-native", "opencode-native", "kimi-native"):
                _launch_resolve_spec = lambda: _resolve_session_agent_spec_or_none(  # noqa: E731
                    session_id, agent_id_hint=agent_id
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
                        repl_agent_spec = await _resolve_session_agent_spec(
                            session_id, agent_id_hint=agent_id
                        )
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
        history: list[_JsonObject]
        if is_native_harness(harness_name):
            await _seed_last_server_item_id(session_id)
            history = []
        else:
            history = await _load_history_as_input(session_id)
        if history:
            _session_histories[session_id] = history
            last = history[-1]
            last_type = last.get("type")
            last_role = last.get("role")
            needs_turn = (
                (last_type == "message" and last_role == "user")
                or last_type == "function_call"
                or last_type == "function_call_output"
            )
            if needs_turn and session_id not in _active_turns and not _suppress_recovery:
                _active_turns[session_id] = None
                _publish_turn_status(session_id, "running")
                msg_body = {
                    "agent_id": agent_id,
                    "model": body.get("model", agent_id),
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

        status = "running" if session_id in _active_turns else "idle"
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
        key = (
            session_id,
            agent_id,
            sub_agent_name if isinstance(sub_agent_name, str) else None,
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
        if not process_manager.has_session(session_id):
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "detail": (f"No session '{session_id}' on this runner."),
                },
            )
        has_turn = session_id in _active_turns or process_manager.has_active_turn(session_id)
        status = "running" if has_turn else "idle"
        agent_id = _session_agent_ids.get(session_id)
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
        turn_task = _active_turns.pop(session_id, None)
        if turn_task is not None and isinstance(turn_task, asyncio.Task):
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn_task
        _session_message_buffers.pop(session_id, None)
        _live_response_id.pop(session_id, None)
        _native_pane_status.pop(session_id, None)
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

        if process_manager is not None:
            await process_manager.forward_cancel(session_id)

        queue = _session_event_queues.get(session_id)
        if queue is not None:
            queue.put_nowait(None)

        await resource_registry.cleanup_session(session_id)

        if process_manager is not None:
            await process_manager.release(session_id)

        await _delete_native_bridge_dirs(
            server_client=server_client,
            session_id=session_id,
        )

        _session_spec_cache.pop(session_id, None)
        # Popped with the spec it describes: provenance outliving its entry
        # would warn about a name the next cached spec never failed on.
        _session_sub_agent_fallbacks.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_cursor_model_names.pop(session_id, None)
        _drop_session_claude_launch_config(session_id)
        _session_start_cache.pop(session_id, None)
        _session_workspace_cache.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        _session_snapshot_locks.pop(session_id, None)
        _session_init_envelopes.pop(session_id, None)
        _session_cache_generations.pop(session_id, None)
        _session_spec_locks.pop(session_id, None)
        _session_fs_registries.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        _session_mcp_spec_hash.pop(session_id, None)
        if _relay := _session_comment_relays.pop(session_id, None):
            _relay.close()
        _session_histories.pop(session_id, None)
        _author_attribution_sessions.discard(session_id)
        _last_server_item_id.pop(session_id, None)
        _session_event_queues.pop(session_id, None)
        _session_inboxes.pop(session_id, None)
        _subagent_wake_pending.discard(session_id)
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
        _instruction_delivery_warned.pop(session_id, None)
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

    async def _seed_last_server_item_id(session_id: str) -> None:
        """
        Record the newest server item ID without loading history.

        Native-harness sessions never call ``_load_history_as_input``
        (their transcripts are mirrored from the underlying runtime), but
        harness compaction persistence still needs the latest server item
        ID as its anchor — fetch just that ID.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        try:
            resp = await server_client.get(
                f"/v1/sessions/{session_id}/items",
                params={"limit": "1", "order": "desc"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                _logger.warning(
                    "Last-item seed returned %d for session=%s",
                    resp.status_code,
                    session_id,
                )
                return
            page_items = resp.json().get("data", [])
        except (httpx.HTTPError, ValueError):
            _logger.warning(
                "Last-item seed failed for session=%s",
                session_id,
                exc_info=True,
            )
            return
        last_id = page_items[0].get("id") if page_items else None
        if last_id:
            _last_server_item_id[session_id] = last_id

    async def _load_history_as_input(
        session_id: str,
        drop_item_id: str | None = None,
    ) -> list[_JsonObject]:
        all_items: list[_JsonObject] = []
        after_cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "limit": "100",
                "order": "asc",
            }
            if after_cursor is not None:
                params["after"] = after_cursor
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{session_id}/items",
                    params=params,
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    _logger.warning(
                        "History load returned %d for session=%s",
                        resp.status_code,
                        session_id,
                    )
                    break
            except httpx.HTTPError:
                _logger.warning(
                    "History load failed for session=%s",
                    session_id,
                    exc_info=True,
                )
                break
            page = resp.json()
            page_items = page.get("data", [])
            if not page_items:
                break
            all_items.extend(page_items)
            last_id = page_items[-1].get("id")
            if last_id:
                _last_server_item_id[session_id] = last_id
            if not page.get("has_more", False):
                break
            after_cursor = last_id

        if drop_item_id is not None:
            all_items = [it for it in all_items if it.get("id") != drop_item_id]

        converted = _convert_raw_items_to_input(all_items)
        # Items are persisted pre-resolution, so reloaded history can still
        # carry raw file_id blocks (the runner has no file/artifact stores).
        # Resolve them the same way current-turn intake does.
        for item in converted:
            content = item.get("content")
            if item.get("type") == "message" and isinstance(content, list):
                item["content"] = await _resolve_forwarded_message_content(
                    content,
                    session_id=session_id,
                    server_client=server_client,
                )
        return converted

    def _convert_raw_items_to_input(
        items: list[_JsonObject],
    ) -> list[_JsonObject]:
        compaction_idx: int | None = None
        for i, item in enumerate(items):
            if item.get("type") == "compaction":
                compaction_idx = i

        result: list[_JsonObject] = []
        if compaction_idx is not None:
            c = items[compaction_idx]
            _compacted = cast(list[_JsonObject] | None, c.get("compacted_messages"))
            if _compacted:
                result.extend(_compacted)
            else:
                result.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "[Automatically generated summary of prior "
                                    "conversation context.]\n\n"
                                    "Please provide a summary of our conversation so far."
                                ),
                            }
                        ],
                    }
                )
                result.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": c.get("summary", ""),
                            }
                        ],
                    }
                )
            remaining = items[compaction_idx + 1 :]
        else:
            remaining = items

        _skipped_types: list[str] = []
        for item in remaining:
            item_type = item.get("type")
            if item_type not in (
                "message",
                "function_call",
                "function_call_output",
                "error",
            ):
                _skipped_types.append(str(item_type))
            if item_type == "message":
                message = {
                    "type": "message",
                    "role": item.get("role", "user"),
                    "content": item.get("content", []),
                }
                if item.get("created_by") is not None:
                    message["created_by"] = item["created_by"]
                result.append(message)
            elif item_type == "function_call":
                result.append(
                    {
                        "type": "function_call",
                        "call_id": item.get("call_id"),
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )
            elif item_type == "function_call_output":
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.get("call_id"),
                        "output": item.get("output"),
                    }
                )
            elif item_type == "error":
                error_message = item.get("message")
                code = item.get("code")
                source = item.get("source")
                result.append(
                    {
                        "type": "error",
                        "source": source if isinstance(source, str) and source else "execution",
                        "code": code if isinstance(code, str) and code else "error",
                        "message": (
                            error_message
                            if isinstance(error_message, str) and error_message
                            else "unknown error"
                        ),
                    }
                )
        if _skipped_types:
            _logger.warning(
                "_convert_raw_items_to_input: skipped %d items with types: %s",
                len(_skipped_types),
                _skipped_types,
            )
        _logger.info(
            "_convert_raw_items_to_input: %d raw items → %d converted (compaction_idx=%s)",
            len(items),
            len(result),
            compaction_idx,
        )
        return result

    def _extract_last_assistant_text(session_id: str) -> str:
        history = _session_histories.get(session_id, [])
        for item in reversed(history):
            if item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text") or block.get("input_text")
                            if text:
                                parts.append(str(text))
                        elif isinstance(block, str):
                            parts.append(block)
                    return "\n".join(parts) if parts else ""
        return ""

    async def _handle_harness_compaction(
        conv: str,
        event: _JsonObject,
    ) -> None:
        summary = cast(str, event.get("summary", ""))
        token_count = cast(int, event.get("total_tokens") or 0)
        model = cast(str | None, event.get("summary_model"))
        last_item_id = _last_server_item_id.get(conv)

        if not last_item_id:
            _logger.warning(
                "Skipping harness compaction persist for %s: no "
                "server-side last_item_id available",
                conv,
            )
            return

        compacted_messages = cast(list[_JsonObject] | None, event.get("compacted_messages"))
        compaction_event: _JsonObject = {
            "type": "compaction",
            "summary": summary,
            "last_item_id": last_item_id,
            "model": model,
            "token_count": token_count,
        }
        if compacted_messages:
            compaction_event["compacted_messages"] = compacted_messages
        try:
            await server_client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "compaction",
                    "data": compaction_event,
                },
                timeout=10.0,
            )
        except (httpx.HTTPError, RuntimeError):
            _logger.warning(
                "Failed to persist harness compaction item for %s",
                conv,
                exc_info=True,
            )

        if compacted_messages:
            _session_histories[conv] = compacted_messages
        else:
            _session_histories[conv] = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "[Automatically generated summary of prior "
                                "conversation context.]\n\n"
                                "Please provide a summary of our conversation so far."
                            ),
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": summary,
                        }
                    ],
                },
            ]

    _CANCELLATION_TOOL_OUTPUT = "[Cancelled — tool execution was interrupted.]"
    _CANCELLATION_MARKER_TEXT = (
        "[System: interrupted]\n"
        "The user interrupted and abandoned their previous request (the user "
        "message immediately before this one). Do not resume or act on that "
        "interrupted request unless the user asks for it again; treat the next "
        "user message as the current instruction. The preceding assistant "
        "message may be incomplete."
    )

    def _append_cancellation_items(conv_id: str) -> None:
        history = _session_histories.get(conv_id, [])

        call_ids_with_output: set[str] = set()
        dangling_calls: list[_JsonObject] = []
        for item in history:
            itype = item.get("type")
            if itype == "function_call":
                cid = item.get("call_id")
                if cid:
                    dangling_calls.append(item)
            elif itype == "function_call_output":
                cid = item.get("call_id")
                if cid:
                    call_ids_with_output.add(cast(str, cid))

        items_to_persist: list[_JsonObject] = []
        synthetic_items: list[_JsonObject] = []
        cached_spec_entry = _cache_get_for_agent(
            _session_spec_cache, conv_id, _session_agent_ids.get(conv_id)
        )
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        agent_name = cached_spec.name if cached_spec else "unknown"
        for fc in dangling_calls:
            call_id = fc["call_id"]
            if call_id not in call_ids_with_output:
                fc_for_db = dict(fc)
                fc_for_db.setdefault("agent", agent_name)
                items_to_persist.append(fc_for_db)
                synthetic_output: _JsonObject = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _CANCELLATION_TOOL_OUTPUT,
                }
                synthetic_items.append(synthetic_output)
                items_to_persist.append(synthetic_output)

        marker: _JsonObject = {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": _CANCELLATION_MARKER_TEXT,
                }
            ],
        }
        synthetic_items.append(marker)
        items_to_persist.append(marker)

        _session_histories.setdefault(conv_id, []).extend(synthetic_items)

        loop = asyncio.get_running_loop()
        _task = loop.create_task(
            _persist_cancellation_items(conv_id, items_to_persist),
            name=f"persist-cancel-{conv_id}",
        )
        _task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_task)

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
                )

    async def _recover_sub_agent(conv_id: str) -> _SubAgentRecovery:
        """Recover the session's ``sub_agent_name``, saying whether it is known.

        A failed snapshot fetch reports ``known=False`` rather than "no
        sub-agent": a caller that publishes a cache entry off this answer
        would otherwise record a resolution nobody performed, and the session
        answers from the parent in silence from then on.
        """
        cached = _session_sub_agent_names.get(conv_id)
        if cached:
            return _SubAgentRecovery(cached, True)
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return _SubAgentRecovery(None, False)
        # ``_session_snapshot`` answers a failed fetch with a not-ok snapshot
        # whose every field is None rather than by raising, so reading
        # ``sub_agent_name`` off one reports "this session has no sub-agent"
        # for what is really "the fetch failed". Gate on ``ok`` — the fields
        # only mean anything when it is true.
        if snapshot is None or not snapshot.ok:
            return _SubAgentRecovery(None, False)
        name = snapshot.sub_agent_name
        if name:
            _session_sub_agent_names[conv_id] = name
        return _SubAgentRecovery(name, True)

    async def _ensure_subagent_work_entry(conv_id: str) -> _SubagentWorkEntry | None:
        existing = get_subagent_work(conv_id)
        if existing is not None:
            return existing
        if conv_id in _drained_delivered_subagent_children:
            return None
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        parent_id = snapshot.parent_session_id
        if not parent_id or parent_id == conv_id:
            return None
        agent = snapshot.sub_agent_name or snapshot.agent_name or "sub-agent"
        return register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=conv_id,
            agent=agent,
            title=snapshot.sub_agent_name or "",
        )

    def _session_harness_name(conv_id: str) -> str | None:
        spec = _cache_get_for_agent(_session_spec_cache, conv_id, _session_agent_ids.get(conv_id))
        if spec is None:
            return None
        h = spec.executor.config.get("harness") or spec.executor.type
        return canonicalize_harness(h) or h

    def _publish_turn_status(
        conv_id: str,
        status: str,
        error: Mapping[str, object] | None = None,
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
        _publish_event(conv_id, event)

    def _is_native_harness(conv_id: str) -> bool:
        return is_native_harness(_session_harness_name(conv_id))

    async def _codex_native_bridge_state_for_session(
        conv_id: str,
        *,
        action: str,
        missing_state_log_level: int = logging.WARNING,
    ) -> CodexNativeBridgeState | None:
        from omnigent.codex_native_bridge import (
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

    codex_goal_runner = CodexGoalRunner(
        bridge_state_for_session=_codex_native_bridge_state_for_session,
        client_safe_error_detail=_client_safe_error_detail,
        logger=_logger,
    )

    async def _handle_codex_native_settings_update(
        conv_id: str,
        settings: _JsonObject,
    ) -> Response:
        from omnigent.codex_native_app_server import client_for_transport

        if not settings:
            return Response(status_code=204)
        state = await _codex_native_bridge_state_for_session(conv_id, action="settings update")
        if state is None:
            return Response(status_code=204)

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            await codex_client.request(
                "thread/settings/update",
                {
                    "threadId": state.thread_id,
                    **settings,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface app-server settings failures.
            _logger.warning(
                "Codex-native thread/settings/update failed for session=%s thread=%s settings=%s",
                conv_id,
                state.thread_id,
                sorted(settings),
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="codex-native settings update"
                    ),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        return Response(status_code=204)

    async def _codex_native_model_and_effort_for_settings_update(
        conv_id: str,
    ) -> tuple[str | None, str | None]:
        model: str | None = None
        effort: str | None = None
        if server_client is not None:
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{urllib.parse.quote(conv_id, safe='')}",
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    snapshot = resp.json()
                    if isinstance(snapshot, dict):
                        raw_model = snapshot.get("model_override") or snapshot.get("llm_model")
                        if isinstance(raw_model, str) and raw_model.strip():
                            model = raw_model.strip()
                        raw_effort = snapshot.get("reasoning_effort")
                        if isinstance(raw_effort, str) and raw_effort.strip():
                            effort = raw_effort.strip()
            except (httpx.HTTPError, RuntimeError, ValueError):
                _logger.warning(
                    "Codex-native plan-mode update could not fetch session snapshot for %s",
                    conv_id,
                    exc_info=True,
                )

        if model is None:
            model = _codex_native_model_from_spec(
                _cache_get_for_agent(_session_spec_cache, conv_id, _session_agent_ids.get(conv_id))
            )
        return model, effort

    async def _handle_codex_native_plan_mode_change(
        conv_id: str,
        *,
        enabled: bool,
    ) -> Response:
        state = await _codex_native_bridge_state_for_session(conv_id, action="plan-mode update")
        if state is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": "Codex-native plan-mode update requires a loaded Codex bridge.",
                },
            )
        model, effort = await _codex_native_model_and_effort_for_settings_update(conv_id)
        if model is None:
            _logger.warning(
                "Codex-native plan-mode update skipped for %s: current model is unknown",
                conv_id,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": "Codex-native plan-mode update requires a current model.",
                },
            )
        from omnigent.codex_native_bridge import (
            DeveloperInstructionsReadState,
            read_codex_config_developer_instructions_state_from_home,
        )

        # Read the current developer_instructions from the bridge's private
        # config so this settings update doesn't silently overwrite what
        # build_codex_native_server persisted. Unlike model/effort above,
        # this has no persistent runner-side state to fall back to, so a
        # transient read failure (UNREADABLE) must not be collapsed into
        # "genuinely absent" — that would send developer_instructions: null
        # and wipe live state on nothing more than a momentary read glitch.
        # Only a genuine ABSENT reads as None; UNREADABLE fails the request
        # instead, matching this handler's existing precondition-failure
        # style (missing bridge / unknown model both already 503 here).
        _di_read = read_codex_config_developer_instructions_state_from_home(Path(state.codex_home))
        if _di_read.state is DeveloperInstructionsReadState.UNREADABLE:
            _logger.warning(
                "Codex-native plan-mode update skipped for %s: developer_instructions "
                "config unreadable — refusing to guess and risk wiping live state.",
                conv_id,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_settings_update_failed",
                    "detail": (
                        "Codex-native plan-mode update requires reading the current "
                        "developer_instructions config; it could not be read."
                    ),
                },
            )
        developer_instructions = _di_read.value
        return await _handle_codex_native_settings_update(
            conv_id,
            {
                "collaborationMode": {
                    "mode": "plan" if enabled else "default",
                    "settings": {
                        "model": model,
                        "reasoning_effort": effort,
                        "developer_instructions": developer_instructions,
                    },
                },
            },
        )

    async def _codex_native_model_options(conv_id: str) -> list[_JsonObject]:
        from omnigent.codex_native_app_server import (
            client_for_transport,
            list_codex_model_options,
        )

        state = await _codex_native_bridge_state_for_session(
            conv_id,
            action="model options",
            missing_state_log_level=logging.DEBUG,
        )
        if state is None:
            raise _CodexNativeModelOptionsNotReady("Codex-native model options are not ready yet.")

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            return await list_codex_model_options(codex_client)
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()

    async def _handle_pi_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.pi_native_bridge import bridge_dir_for_session_id, enqueue_model_change

        if model is None or not model.strip():
            return Response(status_code=204)
        try:
            await asyncio.to_thread(
                enqueue_model_change,
                bridge_dir_for_session_id(conv_id),
                model.strip(),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native model change failed for session=%s",
                conv_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native model change"),
                },
            )
        return Response(status_code=204)

    async def _teardown_session_terminals(conv_id: str) -> None:
        from omnigent.entities.session_resources import terminal_resource_id
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = resource_registry.terminal_registry
        if terminal_registry is None:
            return
        terminals = [
            (entry.terminal_name, entry.session_key)
            for entry in terminal_registry.list_for_conversation(conv_id)
        ]
        for terminal_name, session_key in terminals:
            terminal_id = terminal_resource_id(terminal_name, session_key)
            try:
                await resource_registry.close_terminal(conv_id, terminal_id)
            except (RuntimeError, OSError):
                _logger.warning(
                    "Failed to close terminal %s for session %s during stop",
                    terminal_id,
                    conv_id,
                    exc_info=True,
                )
            _publish_terminal_deleted_event(
                conversation_id=conv_id,
                terminal_name=terminal_name,
                session_key=session_key,
                publish_event=_publish_event,
            )

    async def _handle_claude_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )
        from omnigent.reasoning_effort import CLAUDE_EFFORTS

        if effort is None or effort not in CLAUDE_EFFORTS:
            return Response(status_code=204)
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/effort {effort}"
        try:
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_effort_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="claude-native effort change"
                    ),
                },
            )
        return Response(status_code=204)

    async def _handle_claude_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.claude_native import (
            resolve_claude_native_model_selection,
        )
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        selected_model = model.strip()
        resolved_model = resolve_claude_native_model_selection(
            selected_model,
            await _resolve_session_claude_launch_config(conv_id),
        )
        command = f"/model {resolved_model}"
        try:
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command=command,
                timeout_s=1.0,
                auto_confirm=True,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_cursor_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.cursor_native_bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        selected_model = model.strip()
        expected_display_name = _session_cursor_model_names.get(conv_id, {}).get(selected_model)
        try:
            await asyncio.to_thread(
                inject_model_command,
                bridge_dir,
                model=selected_model,
                expected_display_name=expected_display_name,
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_kiro_native_model_change(
        conv_id: str,
        model: str | None,
    ) -> Response:
        from omnigent.kiro_native_bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )

        if model is None or not model.strip():
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(
                inject_model_command,
                bridge_dir,
                model=model.strip(),
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "kiro_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="kiro-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_claude_native_compact(conv_id: str) -> Response:
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            await asyncio.to_thread(
                inject_slash_command,
                bridge_dir,
                command="/compact",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_codex_native_compact(conv_id: str) -> Response:
        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)

        socket_path = str(instance.socket_path)
        target = instance.tmux_target

        try:
            await asyncio.to_thread(_inject_codex_compact, socket_path, target)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_opencode_native_compact(conv_id: str) -> Response:
        from omnigent.opencode_native_bridge import bridge_dir_for_bridge_id, read_bridge_state
        from omnigent.opencode_native_client import OpenCodeClientError

        server = _AUTO_OPENCODE_SERVERS.get(conv_id)
        state = read_bridge_state(bridge_dir_for_bridge_id(conv_id))
        if server is None or state is None or not state.opencode_session_id:
            return Response(status_code=204)
        client = server.client()
        try:
            session = await client.get_session(state.opencode_session_id)
            messages = await client.list_messages(state.opencode_session_id)
            provider_id, model_id = _resolve_opencode_compact_model(
                session, messages, state.model_override
            )
            if not provider_id or not model_id:
                return Response(status_code=204)
            await client.summarize(
                state.opencode_session_id, provider_id=provider_id, model_id=model_id
            )
        except (httpx.HTTPError, OpenCodeClientError, RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native compact"),
                },
            )
        finally:
            await client.aclose()
        return Response(status_code=200)

    async def _opencode_native_model_options(conv_id: str) -> list[_JsonObject]:
        from omnigent.opencode_native_app_server import (
            filtered_server_env,
            list_opencode_cli_model_options,
        )
        from omnigent.opencode_native_bridge import bridge_dir_for_bridge_id, read_bridge_state
        from omnigent.opencode_native_client import OpenCodeClient

        bridge_dir = bridge_dir_for_bridge_id(conv_id)
        state = read_bridge_state(bridge_dir)
        if state is None or not state.server_base_url:
            raise _CodexNativeModelOptionsNotReady("OpenCode-native app-server is not ready yet.")

        cli_env = filtered_server_env(
            bridge_dir=bridge_dir,
            auth_secret=state.auth_secret or "",
        )
        try:
            return await asyncio.to_thread(list_opencode_cli_model_options, env=cli_env)
        except Exception as exc:  # noqa: BLE001 - fall back to the server catalog.
            _logger.debug("OpenCode CLI model list failed for %s: %r", conv_id, exc)

        client = OpenCodeClient(
            base_url=state.server_base_url,
            headers=state.auth_headers(),
        )
        try:
            return await client.list_models()
        finally:
            await client.aclose()

    async def _handle_opencode_native_model_change(conv_id: str, model: str | None) -> Response:
        from omnigent.opencode_native_bridge import (
            bridge_dir_for_bridge_id,
            update_model_override,
        )

        updated = await asyncio.to_thread(
            update_model_override, bridge_dir_for_bridge_id(conv_id), model
        )
        return Response(status_code=200 if updated else 204)

    async def _handle_opencode_native_clear(conv_id: str) -> Response:
        if _session_harness_name(conv_id) != "opencode-native":
            return Response(status_code=204)
        if server_client is not None:
            with contextlib.suppress(httpx.HTTPError):
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(conv_id, safe='')}",
                    json={"external_session_id": None},
                    timeout=10.0,
                )
        try:
            spec = await _resolve_session_agent_spec(
                conv_id, agent_id_hint=_session_agent_ids.get(conv_id)
            )
        except OmnigentError:
            spec = None
        try:
            await _auto_create_opencode_terminal(
                conv_id,
                resource_registry,
                _publish_event,
                agent_spec=spec,
                server_client=server_client,
                ensure_comment_relay=_ensure_comment_relay_started,
            )
        except Exception as exc:  # noqa: BLE001 - report relaunch failure to caller.
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_clear_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native clear"),
                },
            )
        return Response(status_code=200)

    async def _handle_cursor_native_compact(conv_id: str) -> Response:
        from omnigent.cursor_native_bridge import bridge_dir_for_session_id, inject_user_message

        bridge_dir = bridge_dir_for_session_id(conv_id)
        _publish_event(conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id})
        try:
            await asyncio.to_thread(
                inject_user_message,
                bridge_dir,
                content="/summarize",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_pi_native_compact(conv_id: str) -> Response:
        from omnigent.pi_native_bridge import bridge_dir_for_session_id, enqueue_compact

        try:
            await asyncio.to_thread(
                enqueue_compact,
                bridge_dir_for_session_id(conv_id),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native compact failed for session=%s",
                conv_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native compact"),
                },
            )
        return Response(status_code=200)

    def _inject_codex_compact(socket_path: str, target: str) -> None:
        from omnigent.claude_native_bridge import _run_tmux

        _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/compact")
        _run_tmux(socket_path, "send-keys", "-t", target, "Enter")

    async def _handle_hermes_native_compact(conv_id: str) -> Response:
        from omnigent.hermes_native_bridge import (
            bridge_dir_for_session_id,
            inject_compress_command,
        )

        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            await asyncio.to_thread(inject_compress_command, bridge_dir, timeout_s=1.0)
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "hermes_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="hermes-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_qwen_native_compact(conv_id: str) -> Response:
        from omnigent.qwen_native_bridge import bridge_dir_for_session_id, submit_user_message

        bridge_dir = bridge_dir_for_session_id(conv_id)
        _publish_event(conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id})
        try:
            await asyncio.to_thread(submit_user_message, bridge_dir, content="/compress")
        except (RuntimeError, OSError) as exc:
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
            return JSONResponse(
                status_code=503,
                content={
                    "error": "qwen_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="qwen-native compact"),
                },
            )
        return Response(status_code=200)

    async def _handle_claude_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            display_cost_approval_popup,
        )

        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        config_file = await _native_cost_popup_config_file(conv_id, "claude-native")
        try:
            await asyncio.to_thread(
                display_cost_approval_popup,
                bridge_dir,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
                timeout_s=1.0,
                config_file=config_file,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_codex_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)
        config_file = await _native_cost_popup_config_file(conv_id, "codex-native")
        try:
            await asyncio.to_thread(
                launch_cost_popup,
                str(instance.socket_path),
                instance.tmux_target,
                config_file,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_opencode_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "opencode", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)
        config_file = await _native_cost_popup_config_file(conv_id, "opencode-native")
        try:
            await asyncio.to_thread(
                launch_cost_popup,
                str(instance.socket_path),
                instance.tmux_target,
                config_file,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_cost_popup_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native cost popup"),
                },
            )
        return Response(status_code=204)

    async def _handle_opencode_native_blocked_notice(
        conv_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        from omnigent.native_cost_popup import launch_blocked_notice

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "opencode", "main") if registry is not None else None
        if instance is None or not instance.running:
            return Response(status_code=204)
        try:
            await asyncio.to_thread(
                launch_blocked_notice,
                str(instance.socket_path),
                instance.tmux_target,
                message=message,
                policy_name=policy_name,
            )
        except (RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_blocked_notice_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="opencode-native blocked notice"
                    ),
                },
            )
        return Response(status_code=204)

    async def _native_cost_popup_config_file(conv_id: str, harness: str) -> Path:
        from omnigent.cli_auth import databricks_request_headers
        from omnigent.opencode_native_bridge import write_cost_popup_config
        from omnigent.runner._entry import _make_auth_token_factory

        if harness == "claude-native":
            from omnigent import claude_native_bridge as _cnb

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client, session_id=conv_id
            )
            bridge_dir = _cnb.bridge_dir_for_bridge_id(bridge_id)
        elif harness == "opencode-native":
            from omnigent.opencode_native_bridge import (
                bridge_dir_for_bridge_id as _oc_bridge_dir,
            )

            bridge_dir = _oc_bridge_dir(conv_id)
        else:  # codex-native
            from omnigent import codex_native_bridge as _cxb

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
        from omnigent.native_cost_popup import launch_cost_popup, wait_for_tmux_client

        attached = await asyncio.to_thread(
            wait_for_tmux_client, socket_path, tmux_target, timeout_s=5.0
        )
        if not attached:
            return
        try:
            resp = await server_client.get(f"/v1/sessions/{conv_id}", timeout=10.0)
        except httpx.HTTPError:
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

    def _on_proxy_stream_end(
        conv_id: str,
        *,
        error: Mapping[str, object] | None = None,
    ) -> None:

        _active_turns.pop(conv_id, None)
        _live_response_id.pop(conv_id, None)
        if process_manager is not None:
            process_manager.clear_in_flight(conv_id)
        has_buffered = bool(_session_message_buffers.get(conv_id))
        was_interrupted = conv_id in _interrupted_sessions
        if was_interrupted:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            if not has_buffered:
                _publish_turn_status(conv_id, "idle")
        elif error is not None:
            _publish_turn_status(conv_id, "failed", error=_normalize_turn_error(error))
        else:
            if not has_buffered:
                children = _subagent_work_by_parent.get(conv_id, set())
                has_running_children = any(
                    (e := _subagent_work_by_child.get(c)) is not None
                    and e.status in ("launching", "running", "waiting")
                    for c in children
                )
                _publish_turn_status(conv_id, "waiting" if has_running_children else "idle")
        if was_interrupted:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="cancelled",
                output="[System: sub-agent interrupted]",
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
        if not isinstance(turn_task, asyncio.Task) or turn_task.done():
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
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="cancelled",
                output="[System: sub-agent interrupted]",
            )
        return True

    async def _cancel_inprocess_turn(conv_id: str) -> None:
        target = _active_turns.get(conv_id)
        if process_manager is None or not isinstance(target, asyncio.Task) or target.done():
            return
        _interrupted_sessions.add(conv_id)
        try:
            harness_client = await process_manager.get_client(conv_id, "any")
            await harness_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
                timeout=3.0,
            )
        except NoLiveHarnessError:
            _logger.debug("Interrupt forward skipped for %s: no live harness", conv_id)
        except Exception:  # noqa: BLE001 — best-effort: harness may have exited
            _logger.warning(
                "Interrupt forward to harness failed for %s",
                conv_id,
                exc_info=True,
            )
        await _cancel_active_turn(conv_id, expected_task=target)

    def _history_message_from_body(body: _JsonObject) -> _JsonObject:
        message = {
            "type": "message",
            "role": body.get("role", "user"),
            "content": body.get("content", []),
        }
        if body.get("created_by") is not None:
            message["created_by"] = body["created_by"]
        return message

    def _note_message_author(session_id: str, body: _JsonObject) -> None:
        if session_id in _author_attribution_sessions:
            return
        if body.get("author_attribution_required") is True:
            _author_attribution_sessions.add(session_id)
            return
        authors = {
            item.get("created_by")
            for item in _session_histories.get(session_id, [])
            if isinstance(item.get("created_by"), str) and item.get("created_by")
        }
        created_by = body.get("created_by")
        if isinstance(created_by, str) and created_by:
            authors.add(created_by)
        if len(authors) >= 2:
            _author_attribution_sessions.add(session_id)

    def _message_body_for_harness(
        body: _JsonObject,
        *,
        force_author_attribution: bool,
    ) -> _JsonObject:
        event = {
            key: value
            for key, value in body.items()
            if key not in {"created_by", "author_attribution_required"}
        }
        prepared = prepare_input_items_for_model(
            [_history_message_from_body(body)],
            force_author_attribution=force_author_attribution,
        )
        event["content"] = prepared[0]["content"]
        return event

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

            if _is_native_harness(session_id):
                next_body = buf.pop(0)
                if not buf:
                    _session_message_buffers.pop(session_id, None)
                _session_histories.setdefault(session_id, []).append(
                    _history_message_from_body(next_body)
                )
            else:
                all_bodies = list(buf)
                buf.clear()
                _session_message_buffers.pop(session_id, None)

                for body in all_bodies:
                    _session_histories.setdefault(session_id, []).append(
                        _history_message_from_body(body)
                    )
                next_body = all_bodies[-1]

            _active_turns[session_id] = None
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

    async def _post_subagent_wake_notice(
        parent_id: str, notice: str, child_id: str, created_by: str | None
    ) -> None:
        delivered = await _deliver_subagent_wake_post(
            server_client, parent_id, notice, created_by=created_by
        )
        if not delivered:
            _subagent_wake_pending.discard(parent_id)
            _logger.warning(
                "Sub-agent wake POST failed for parent=%s child=%s after %d attempt(s); "
                "result remains in the parent inbox until the next wake",
                parent_id,
                child_id,
                _WAKE_POST_MAX_ATTEMPTS,
            )

    def _schedule_subagent_wake(entry: _SubagentWorkEntry) -> None:
        if entry.parent_session_id == entry.child_session_id:
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
        _subagent_wake_pending.add(entry.parent_session_id)
        notice = _format_subagent_wake_notice(
            agent=entry.agent,
            title=entry.title,
            status=entry.status,
            pending=inbox.qsize(),
        )
        _wake_task = loop.create_task(
            _post_subagent_wake_notice(
                entry.parent_session_id,
                notice,
                entry.child_session_id,
                entry.created_by,
            )
        )
        _wake_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_wake_task)

    def _rewake_parent_if_inbox_stranded(parent_session_id: str) -> None:
        if parent_session_id not in _subagent_wake_pending:
            return
        _subagent_wake_pending.discard(parent_session_id)
        inbox = _session_inboxes.get(parent_session_id)
        if inbox is None or inbox.empty():
            return
        entries = list_subagent_work(parent_session_id)
        if not entries:
            return
        latest = max(
            entries,
            key=lambda entry: entry.completed_at if entry.completed_at is not None else 0.0,
        )
        _schedule_subagent_wake(latest)

    def _mark_subagent_terminal_and_wake(
        child_session_id: str, *, status: str, output: str | None
    ) -> _SubagentDeliveryAck:
        ack = mark_subagent_work_terminal(child_session_id, status=status, output=output)
        if ack.entry is not None and ack.delivered_now:
            _schedule_subagent_wake(ack.entry)
        return ack

    _native_interrupt_runner = NativeInterruptRunner(
        server_client=server_client,
        resource_registry=resource_registry,
        publish_event=_publish_event,
        mark_subagent_terminal_and_wake=_mark_subagent_terminal_and_wake,
        session_sub_agent_names=_session_sub_agent_names,
        codex_bridge_state_for_session=_codex_native_bridge_state_for_session,
        client_safe_error_detail=_client_safe_error_detail,
        logger=_logger,
    )

    async def _ensure_comment_relay_started(
        session_id: str,
        *,
        bridge_id: str | None = None,
        explicit_bridge_dir: Path | None = None,
        await_notify: bool = False,
        session_labels: Mapping[str, str] | None = None,
    ) -> None:
        if session_id in _session_comment_relays:
            return

        _fill_generation = _session_cache_generation(session_id)

        import json as _json

        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            post_tools_changed,
            start_tool_relay,
        )

        if explicit_bridge_dir is not None:
            bridge_dir = explicit_bridge_dir
        else:
            if bridge_id is None:
                bridge_id = await _claude_native_bridge_id_with_optional_labels(
                    server_client=server_client,
                    session_id=session_id,
                    session_labels=session_labels,
                )

            if session_id in _session_comment_relays:
                return

            bridge_dir = bridge_dir_for_bridge_id(bridge_id or session_id)

        try:
            relay_spec = await _resolve_session_agent_spec(
                session_id, agent_id_hint=_session_agent_ids.get(session_id)
            )
        except OmnigentError:
            relay_spec = None
        if session_id in _session_comment_relays:
            return
        from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas

        relay_schemas: list[_JsonObject] = build_native_relay_tool_schemas(relay_spec)

        _captured_session_id = session_id

        async def _relay_tool_executor(
            name: str,
            arguments: _JsonObject,
        ) -> _JsonObject:
            result_str = await ProxyMcpManager(
                _captured_session_id, server_client, publish_event=_publish_event
            ).call_tool(None, name, arguments)
            try:
                return cast(_JsonObject, _json.loads(result_str))
            except _json.JSONDecodeError:
                return {"result": result_str}

        try:
            relay: ClaudeNativeToolRelay = start_tool_relay(
                bridge_dir=bridge_dir,
                tools=relay_schemas,
                tool_executor=_relay_tool_executor,
                loop=asyncio.get_running_loop(),
                policy_client=server_client,
                session_id=session_id,
            )
        except (OSError, RuntimeError):
            _logger.warning(
                "Failed to start comment relay for session=%s",
                session_id,
                exc_info=True,
            )
            return
        if not _session_cache_put(
            _session_comment_relays, session_id, relay, generation=_fill_generation
        ):
            # An agent switch landed while this relay was being built off the
            # PREVIOUS agent's spec, so it is not RETAINED IN THE SESSION
            # CACHE and is closed best-effort here. That is narrower than it
            # may look: start_tool_relay has already written tool_relay.json,
            # bound its HTTP server and started its thread by the time it
            # returns, so the stale relay may already have been externally
            # visible. Dropping the cache entry stops the session serving it
            # onward; it does not un-publish what was published.
            with contextlib.suppress(OSError, RuntimeError):
                relay.close()
            return

        async def _notify_tools_changed() -> None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, post_tools_changed, bridge_dir
                )
            except RuntimeError:
                _logger.debug(
                    "tools-changed notification skipped for session=%s (bridge server not ready)",
                    session_id,
                )

        if await_notify:
            await _notify_tools_changed()
        else:
            _notify_task = asyncio.create_task(_notify_tools_changed())
            _background_tasks.add(_notify_task)
            _notify_task.add_done_callback(_background_tasks.discard)

    async def _run_turn_bg(
        msg_body: _JsonObject,
        conv: str,
    ) -> None:
        _subagent_wake_pending.discard(conv)
        try:
            await _run_turn_bg_setup_and_stream(msg_body, conv)
        except asyncio.CancelledError as exc:
            _logger.error(
                "turn cancelled for %s: %s",
                conv,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})
            raise
        except Exception as exc:
            _logger.error(
                "turn setup failed for %s: %s",
                conv,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})

    async def _run_turn_bg_setup_and_stream(
        msg_body: _JsonObject,
        conv: str,
    ) -> None:
        # This path resolves the turn's spec inline rather than through
        # _resolve_effective_turn. The 202 has already gone out by the time
        # resolution runs, so a resolver EXCEPTION can only surface
        # asynchronously, as a terminal ``failed`` session status — that part
        # still differs from the other two paths by transport.
        # A missing requested child does not: like every other path, it warns
        # and runs the turn on the parent spec. It used to end the turn here
        # with that same terminal ``failed`` status.
        _dispatched_agent_id = cast(str | None, msg_body.get("agent_id"))
        _prior_agent_id = _session_agent_ids.get(conv)
        # A known, EXPLICIT agent switch (prior dispatch recorded a
        # different agent than this one) releases the harness subprocess
        # and drops the agent-keyed ``_spec_cache`` entry — that part
        # still needs _session_agent_ids as the "what did we last
        # dispatch as" fact. Whether any *cache* below is trustworthy for
        # THIS turn is decided separately, per read, by the agent tag
        # stored on the cache entry itself (see _cache_get_for_agent) —
        # not by this dict, so a write to one can never drift from the
        # other.
        if _dispatched_agent_id and _prior_agent_id != _dispatched_agent_id:
            _logger.info(
                "agent provenance mismatch for %s: prior=%s dispatched=%s; "
                "invalidating session agent state",
                conv,
                _prior_agent_id,
                _dispatched_agent_id,
            )
            await _invalidate_session_agent_state(conv, _dispatched_agent_id)
        # Captured after this path's OWN invalidation above (so that
        # invalidation cannot cancel this path's own writes) and before every
        # await below that can publish into a protected cache, so an
        # invalidation landing mid-turn drops those writes.
        _bg_fill_generation = _session_cache_generation(conv)
        if _dispatched_agent_id:
            _session_cache_put(
                _session_agent_ids, conv, _dispatched_agent_id, generation=_bg_fill_generation
            )

        # Tracks the agent id the currently-held ``cached_spec`` is tagged
        # for, so every subsequent write of ``_session_spec_cache[conv]``
        # in this function tags the entry consistently instead of leaving
        # an untagged/mistagged value for the next reader.
        _current_cache_agent_tag: str | None = _dispatched_agent_id
        cached_spec_entry = _cache_get_for_agent(_session_spec_cache, conv, _dispatched_agent_id)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        cached_spec_workdir = _resolved_spec_workdir(cached_spec_entry)
        # Tracks whether ``cached_spec`` is a FRESH, un-swapped PARENT/root
        # (True only immediately after the raw ``spec_resolver(_aid, conv)``
        # call below, which returns the parent regardless of sub_agent_name)
        # versus a spec that came from the cache. A cache hit is NOT
        # necessarily the swapped child: a session whose sub-agent name does
        # not resolve caches the PARENT, and one whose name could not be
        # recovered caches a spec with nothing decided about it at all. Which
        # of the three it is comes from the entry's recorded provenance, not
        # from this flag — see :func:`_session_spec_is_resolved_child`.
        #
        # This flag narrows a case provenance cannot: a fresh, unswapped
        # parent may coincidentally share its name with the requested
        # sub-agent without BEING it (a top-level coordinator named e.g.
        # "worker" asked to resolve sub_agent_name "worker" with no such
        # child) — that must still search as a parent tree and correctly
        # miss, not self-match.
        _cached_spec_is_fresh_parent = False
        if cached_spec is None and spec_resolver is not None:
            _aid = _dispatched_agent_id
            if _aid:
                try:
                    resolved = await spec_resolver(_aid, conv)
                    if isinstance(resolved, ResolvedSpec):
                        cached_spec = _unwrap_resolved_spec(resolved)
                        cached_spec_workdir = _resolved_spec_workdir(resolved)
                        # A freshly resolved PARENT, published before the swap
                        # below has decided anything — and the swap's own
                        # input is only recovered after another await, so the
                        # answer is genuinely not known yet. Published as
                        # UNDETERMINED: a concurrent reader that would have
                        # trusted this entry resolves for itself instead of
                        # being handed a parent labelled as a child.
                        _session_spec_cache_put(
                            conv,
                            resolved,
                            generation=_bg_fill_generation,
                            agent_tag=_aid,
                            provenance=_SubAgentProvenance.UNDETERMINED,
                        )
                    elif resolved is not None:
                        cached_spec = resolved
                        _session_spec_cache_put(
                            conv,
                            resolved,
                            generation=_bg_fill_generation,
                            agent_tag=_aid,
                            provenance=_SubAgentProvenance.UNDETERMINED,
                        )
                    _current_cache_agent_tag = _aid
                    _cached_spec_is_fresh_parent = cached_spec is not None
                except (httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "Spec resolution failed for %s",
                        conv,
                        exc_info=True,
                    )
            else:
                # No explicit agent id on THIS turn's wire body (e.g. a
                # buffered/continuation turn) — but _prior_agent_id (this
                # session's last-known agent, already computed above) is a
                # real identity fact, not a guess, so pass it as a hint:
                # _resolve_session_spec_entry then trusts a same-session
                # cache hit tagged for that agent, and falls back to a
                # snapshot fetch only when even that's unknown.
                # _resolve_session_agent_spec tags the cache entry it
                # writes with the agent id it actually resolved, so read
                # that tag back rather than assuming it matches the hint.
                try:
                    cached_spec = await _resolve_session_agent_spec(
                        conv, agent_id_hint=_prior_agent_id
                    )
                    _resolved_entry = _session_spec_cache.get(conv)
                    _current_cache_agent_tag = (
                        _resolved_entry[0] if _resolved_entry is not None else None
                    )
                    cached_spec_workdir = _resolved_spec_workdir(
                        _resolved_entry[1] if _resolved_entry is not None else None
                    )
                except (OmnigentError, httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "On-demand agent resolution failed for %s",
                        conv,
                        exc_info=True,
                    )

        _recovered = await _recover_sub_agent(conv)
        _sa_name = _recovered.name
        # Starts UNDETERMINED, not RESOLVED: if the lookup above could not say
        # whether this session names a sub-agent, no swap decision happens
        # below and nothing has been resolved. Publishing RESOLVED there used
        # to make one failed snapshot fetch silence the session permanently —
        # the next turn's cache hit answered "already the child" by name.
        _sa_provenance: _SubAgentProvenanceValue = (
            _SubAgentProvenance.RESOLVED if _recovered.known else _SubAgentProvenance.UNDETERMINED
        )
        if _sa_name and cached_spec is not None:
            from omnigent.runtime.workflow import _find_spec_by_name

            # A cached PARENT kept after an earlier miss is NOT the resolved
            # child, however its name reads. Recorded provenance settles that
            # before name equality is consulted: a coordinator named "worker"
            # asked for sub-agent "worker" satisfies the equality while
            # _find_spec_by_name — the authority on the same question —
            # correctly reports a miss, so the cache used to answer the
            # opposite way on every turn after the first, silently. Only a
            # positively RESOLVED entry may take the shortcut.
            sub_spec = (
                cached_spec
                if not _cached_spec_is_fresh_parent
                and _session_spec_is_resolved_child(conv, _current_cache_agent_tag)
                and cached_spec.name == _sa_name
                else _find_spec_by_name(cached_spec, _sa_name)
            )
            if sub_spec is None:
                # A recorded sub_agent_name that no longer resolves (removed
                # from the spec tree, or a stale record) leaves the PARENT
                # spec driving the turn: its harness and instructions are what
                # run. This used to raise NOT_FOUND and publish a "failed"
                # status for the turn. The warning below is the only signal;
                # the turn itself looks ordinary.
                # This branch writes nothing. The fresh-resolution branch
                # above may already have published an UNDETERMINED entry; the
                # unconditional put below republishes over it with the final
                # answer — the failed name — so the next turn reaches this
                # branch too.
                _warn_unresolved_sub_agent(conv, _sa_name)
                _sa_provenance = _sa_name
            else:
                cached_spec = sub_spec
                _sa_provenance = _SubAgentProvenance.RESOLVED
                _session_spec_cache_put(
                    conv,
                    ResolvedSpec(spec=cached_spec, workdir=cached_spec_workdir)
                    if cached_spec_workdir is not None
                    else cached_spec,
                    generation=_bg_fill_generation,
                    agent_tag=_current_cache_agent_tag,
                    provenance=_SubAgentProvenance.RESOLVED,
                )

        cached_spec = _spec_with_workdir_paths(cached_spec, cached_spec_workdir)
        if cached_spec is not None:
            _session_spec_cache_put(
                conv,
                ResolvedSpec(spec=cached_spec, workdir=cached_spec_workdir)
                if cached_spec_workdir is not None
                else cached_spec,
                generation=_bg_fill_generation,
                agent_tag=_current_cache_agent_tag,
                provenance=_sa_provenance,
            )

        harness_name: str | None = None
        spawn_env: dict[str, str] | None = None
        instructions: str | None = None
        # RAW per-request text off the caller's body, captured before any
        # composition so it stays distinguishable from the composed string
        # built below. See TurnDispatch.per_request_instructions.
        _raw_per_request_instructions = cast(str | None, msg_body.get("instructions"))
        if cached_spec is not None:
            h = (
                cast(str | None, msg_body.get("harness_override"))
                or cached_spec.executor.config.get("harness")
                or cached_spec.executor.type
            )
            harness_name = canonicalize_harness(h) or h

        if conv not in _session_histories:
            _session_histories[conv] = (
                [] if is_native_harness(harness_name) else await _load_history_as_input(conv)
            )
        if conv not in _author_attribution_sessions and input_items_have_multiple_authors(
            _session_histories[conv]
        ):
            _author_attribution_sessions.add(conv)
        if cached_spec is not None:
            spawn_env = _build_spawn_env_from_spec(
                cached_spec,
                cast(str, harness_name),
                workdir=cached_spec_workdir,
                cwd=await _session_runtime_cwd(conv),
                model_override=cast(str | None, msg_body.get("model_override")),
            )
            from omnigent.runtime.prompt import build_instructions

            framework_instructions = (
                (SHARED_SESSION_AUTHORSHIP_INSTRUCTION,)
                if shared_message_attribution_enabled() and conv in _author_attribution_sessions
                else ()
            )
            # The turn's own per-request text composes with the agent's
            # authored instructions rather than being dropped: this body is
            # the caller's, so its ``instructions`` is raw user text, never an
            # internally-composed string (this function builds a fresh
            # harness_body below and never copies one back in).
            instructions = build_instructions(
                cached_spec,
                _raw_per_request_instructions,
                [],
                framework_instructions=framework_instructions,
            )

        ctx = TurnDispatch(
            agent_id=_dispatched_agent_id,
            harness=harness_name,
            per_request_instructions=_raw_per_request_instructions,
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
        if _session_histories[conv]:
            history = _session_histories[conv]
            if any("created_by" in item for item in history):
                harness_body["content"] = prepare_input_items_for_model(
                    history,
                    force_author_attribution=conv in _author_attribution_sessions,
                )
            else:
                harness_body["content"] = history
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
        )

        if instructions:
            harness_body["instructions"] = instructions

        _cached_tool_schemas = _cache_get_for_agent(
            _session_tool_schemas, conv, _current_cache_agent_tag
        )
        # A cache entry is only ever written once the builtin half genuinely
        # resolved (below), so an existing entry already implies it.
        _tools_resolved = _cached_tool_schemas is not None
        if _cached_tool_schemas is None:
            all_tools: list[_JsonObject] = []
            if cached_spec is not None:
                try:
                    from omnigent.tools.manager import (
                        ToolManager,
                    )

                    _tmgr = ToolManager(
                        cached_spec,
                        workdir=cached_spec_workdir or runner_workspace,
                    )
                    all_tools.extend(_tmgr.get_tool_schemas())
                    _tools_resolved = True
                except (
                    ImportError,
                    ValueError,
                    RuntimeError,
                ):
                    _logger.warning(
                        "ToolManager schema build failed for %s",
                        conv,
                        exc_info=True,
                    )
            if _tools_resolved:
                _session_cache_put(
                    _session_tool_schemas,
                    conv,
                    all_tools,
                    generation=_bg_fill_generation,
                    agent_tag=_current_cache_agent_tag,
                )

        # Gated on the builtin half having resolved: merging MCP schemas onto
        # an absent builtin set reads it as `[]` and would cache (and hash) a
        # partial, MCP-only tool list as final, blocking any later retry of
        # the builtin half for the rest of the conversation.
        if cached_spec and cached_spec.mcp_servers and _tools_resolved:
            from omnigent.runner.mcp_manager import compute_spec_hash

            _mcp_hash = compute_spec_hash(list(cached_spec.mcp_servers))
            _cached_mcp_hash = _cache_get_for_agent(
                _session_mcp_spec_hash, conv, _current_cache_agent_tag
            )
            if _mcp_hash != _cached_mcp_hash:
                _session_mcp_proxy = ProxyMcpManager(conv, server_client)
                try:
                    mcp_result = await _session_mcp_proxy.schemas_for(
                        cached_spec,
                    )
                    _builtin_tools = [
                        t
                        for t in (
                            _cache_get_for_agent(
                                _session_tool_schemas, conv, _current_cache_agent_tag
                            )
                            or []
                        )
                        if not (
                            isinstance(t, dict)
                            and isinstance(t.get("name"), str)
                            and "__" in cast(str, t.get("name"))
                        )
                    ]
                    _session_cache_put(
                        _session_tool_schemas,
                        conv,
                        _builtin_tools + list(mcp_result.schemas),
                        generation=_bg_fill_generation,
                        agent_tag=_current_cache_agent_tag,
                    )
                    _session_cache_put(
                        _session_mcp_spec_hash,
                        conv,
                        _mcp_hash,
                        generation=_bg_fill_generation,
                        agent_tag=_current_cache_agent_tag,
                    )
                except (
                    httpx.HTTPError,
                    RuntimeError,
                    ValueError,
                ):
                    _logger.warning(
                        "MCP schema resolution failed for %s",
                        conv,
                        exc_info=True,
                    )

        _spec_tools = (
            _cache_get_for_agent(_session_tool_schemas, conv, _current_cache_agent_tag) or []
        )
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
            from omnigent.codex_native_bridge import (
                CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                write_mcp_bridge_config,
            )
            from omnigent.codex_native_bridge import (
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
            from omnigent.antigravity_native_bridge import (
                ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
                write_mcp_bridge_config,
            )
            from omnigent.antigravity_native_bridge import (
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
            from omnigent.hermes_native_bridge import (
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
            err_detail = "harness returned error response"
            if hasattr(response, "body"):
                with contextlib.suppress(
                    UnicodeDecodeError,
                    AttributeError,
                ):
                    err_detail = bytes(response.body).decode(
                        "utf-8",
                    )[:200]
            _logger.error(
                "turn bg error for %s: %s",
                conv,
                err_detail,
            )
            _on_proxy_stream_end(
                conv,
                error={"message": err_detail},
            )

    async def _drain_streaming_response(
        response: StreamingResponse,
        session_id: str,
    ) -> None:
        try:
            async for _chunk in response.body_iterator:
                pass
        except asyncio.CancelledError:
            _active_turns.pop(session_id, None)
            _live_response_id.pop(session_id, None)
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
        _harness_override_applied = False
        # The turn's RAW per-request instruction text, kept apart from the
        # composed string so composition below includes it rather than
        # discarding it. A dispatch carries it out-of-band because that path's
        # ``body["instructions"]`` already holds composed text; a direct
        # caller-supplied body holds the raw text itself.
        _raw_per_request_instructions = (
            dispatch.per_request_instructions
            if dispatch
            else cast(str | None, body.get("instructions"))
        )
        # A known agent switch evicts every agent-derived cache and
        # releases the harness subprocess BEFORE anything in this function
        # reads them (same shared routine the background dispatch path
        # uses — see _invalidate_session_agent_state's docstring).
        # _session_agent_ids[conv_id] is written only after cold-boot
        # completes (further down): cold-boot receives this turn's agent id
        # directly as a hint, so marking it authoritative before that
        # resolution runs would be premature.
        _early_turn_agent_id = (
            dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
        )
        if _early_turn_agent_id and _session_agent_ids.get(conv_id) != _early_turn_agent_id:
            await _invalidate_session_agent_state(conv_id, _early_turn_agent_id)
        # Captured after this path's OWN invalidation above and before the
        # resolution awaits below, so a reset landing while this turn resolves
        # drops the marker write rather than republishing a superseded agent.
        _early_turn_generation = _session_cache_generation(conv_id)
        if dispatch:
            # TurnDispatch is built by _run_turn_bg_setup_and_stream, which
            # already applied harness_override and canonicalize_harness when
            # constructing ctx.harness — trust it (and its already-matching
            # spawn_env) verbatim.
            harness_name = dispatch.harness
        else:
            # Caller-supplied body: only a caller-supplied `harness` (not a
            # bare `harness_override` with no base harness — that case still
            # needs full spec-driven resolution below). When
            # `harness` IS present, `harness_override` must not silently lose
            # to it, and either spelling may be an alias (e.g. "opencode",
            # "acp:foo") that must be canonicalized before capability lookup
            # / gated delivery — matching the resolver path below and the
            # alias-inheritance contract in docs/AGENT_YAML_SPEC.md.
            _raw_harness = cast(str | None, body.get("harness"))
            if _raw_harness:
                _override = cast(str | None, body.get("harness_override"))
                # Compare CANONICAL identities, not raw spellings: an
                # override that's merely an alias of the same harness
                # (e.g. harness="opencode-native", harness_override="opencode",
                # or harness="claude-sdk", harness_override="claude") is not
                # an actual swap and must not trigger a spawn_env rebuild —
                # a raw-string compare would falsely register one and
                # discard a valid caller-supplied spawn_env for no reason.
                if _override and (canonicalize_harness(_override) or _override) != (
                    canonicalize_harness(_raw_harness) or _raw_harness
                ):
                    _harness_override_applied = True
                    _raw_harness = _override
                harness_name = canonicalize_harness(_raw_harness) or _raw_harness
            else:
                harness_name = None
        # When an override actually swapped the harness, any caller-supplied
        # ``spawn_env`` was built for the ORIGINAL harness and must not be
        # trusted for the overridden one — harness and spawn_env are resolved
        # together as one effective-turn result, not independently. Cleared
        # here; rebuilt below (once the spec is available) or via the
        # native-provider fallback further down.
        spawn_env = (
            dispatch.spawn_env
            if dispatch
            else (
                None
                if _harness_override_applied
                else cast("dict[str, str] | None", body.get("spawn_env"))
            )
        )
        startup_envelope = _fresh_session_init_envelope(conv_id)
        startup_labels = startup_envelope.snapshot.labels if startup_envelope is not None else None
        _agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
        # Only the name is used: this path publishes no cache entry, so an
        # unknown answer degrades exactly as "no sub-agent" already does —
        # the swap below is skipped and the parent composes, which is what
        # this path does for an unresolvable name anyway.
        _sub_agent_name = (await _recover_sub_agent(conv_id)).name
        # ``_turn_spec_for_instructions`` is the single source of truth for
        # "do we have a positively resolved spec to compose instructions
        # from" — every consumer below (the gated wire-swap, the warn
        # check) gates directly on ``is not None`` rather than a
        # separately-tracked boolean. A parallel flag that has to be kept
        # in sync by hand across every branch that can leave the spec
        # unresolved (stale cache, missing provenance, no agent id, no
        # resolver, resolver exception, resolver None) is exactly how this
        # class of bug (indeterminate composition state silently treated as
        # positive absence) arises — collapsing to one gate removes the
        # possibility of the two drifting apart.
        _turn_spec_for_instructions: Any = None
        if not harness_name:
            try:
                (
                    harness_name,
                    spawn_env,
                    _turn_spec_for_instructions,
                    _,
                ) = await _resolve_effective_turn(
                    agent_id=_agent_id,
                    spec_resolver=spec_resolver,
                    session_id=conv_id,
                    model_override=cast(str | None, body.get("model_override")),
                    harness_override=cast(str | None, body.get("harness_override")),
                    sub_agent_name=_sub_agent_name,
                    cwd=await _session_runtime_cwd(conv_id),
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        else:
            # Harness is already known independent of the resolver (dispatch
            # from the background path, or caller-supplied in the body) —
            # resolving the spec for InstructionComposition is best-effort
            # only. That graceful degradation is deliberate and is the third
            # leg of a three-way split: the no-harness branch above answers a
            # resolver failure with a synchronous 503 and the background path
            # with an async terminal failure, while this one logs, leaves the
            # spec unknown, keeps the caller's own instructions and continues.
            # A failure here
            # must not escalate to a 503 or drop any caller-supplied
            # ``instructions`` already in the body; it just means composition
            # and the warn check degrade to "unknown".
            # The background-turn twin (_run_turn_bg_setup_and_stream)
            # invalidates _session_spec_cache on an in-conversation agent
            # switch (dispatched agent_id differs from the previously
            # recorded one) before reading it. This direct-stream path must
            # apply the same check — otherwise a cache entry left over from
            # the PREVIOUS agent (still keyed by conv_id) would silently
            # drive this turn's composition/warn decision. Provenance must
            # match EXACTLY: the cache entry's own agent tag (see
            # _cache_get_for_agent) must equal ``_agent_id``, or be the
            # agent-independent ``None`` tag. An unset/unknown ``_agent_id``
            # is not evidence the cache belongs to the CURRENT turn — it is
            # treated as a miss against any concretely-tagged entry, same as
            # an explicit mismatch, never as automatic "no conflict".
            _cached_entry = _cache_get_for_agent(_session_spec_cache, conv_id, _agent_id)
            _turn_spec_for_instructions = _unwrap_resolved_spec(_cached_entry)
            _turn_workdir_for_instructions = _resolved_spec_workdir(_cached_entry)
            # Same fresh-parent distinction as
            # ``_run_turn_bg_setup_and_stream``'s cache read: only a spec
            # resolved fresh right here (raw ``spec_resolver`` call, never
            # sub-agent-aware) may coincidentally share its name with
            # ``_sub_agent_name`` without actually being that sub-agent.
            # A cache hit is not automatically the swapped child either — a
            # session whose sub-agent no longer resolves caches the PARENT —
            # so the shortcut below also requires the entry's provenance to
            # say a child was positively resolved.
            _turn_spec_is_fresh_parent = False
            if _turn_spec_for_instructions is None and _agent_id and spec_resolver is not None:
                try:
                    _resolved_for_instructions = await spec_resolver(_agent_id, conv_id)
                except (httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "instruction composition spec resolution failed for %s; "
                        "composition/warn check degrade gracefully",
                        conv_id,
                        exc_info=True,
                    )
                    _turn_spec_for_instructions = None
                else:
                    _turn_spec_for_instructions = _unwrap_resolved_spec(_resolved_for_instructions)
                    _turn_workdir_for_instructions = _resolved_spec_workdir(
                        _resolved_for_instructions
                    )
                    _turn_spec_is_fresh_parent = _turn_spec_for_instructions is not None
            # Every path above that leaves ``_turn_spec_for_instructions``
            # ``None`` — stale/no-provenance cache, no agent id, no
            # configured resolver, a raised resolver exception, or a
            # resolver that legitimately returned ``None`` — is equally
            # "we do not positively know the composed value" and must be
            # treated identically by every downstream consumer: composition
            # degrades to "unknown", and the gated wire-swap below (gated on
            # this same ``is not None`` check) leaves caller-supplied
            # ``instructions`` untouched rather than guessing.
            if _turn_spec_for_instructions is not None and _sub_agent_name:
                from omnigent.runtime.workflow import _find_spec_by_name

                # Recorded provenance decides before name equality does: a
                # cached PARENT kept after an earlier miss reads as the child
                # whenever the root's own name matches the request, which is
                # the opposite of what _find_spec_by_name answers for the same
                # spec and name. Only a positively RESOLVED entry may take the
                # shortcut; an undetermined one re-searches instead.
                _sub_spec_for_instructions = (
                    _turn_spec_for_instructions
                    if not _turn_spec_is_fresh_parent
                    and _session_spec_is_resolved_child(conv_id, _agent_id)
                    and _turn_spec_for_instructions.name == _sub_agent_name
                    else _find_spec_by_name(_turn_spec_for_instructions, _sub_agent_name)
                )
                # A miss here (a recorded sub_agent_name no longer resolving)
                # leaves the PARENT spec in place, so the parent's authored
                # instructions are what compose for this turn. This used to
                # null the spec out and report the turn as indeterminate,
                # which left the three consumers gated on
                # ``_turn_spec_for_instructions is not None`` — the spawn_env
                # rebuild, the composition, and the delivery warning — all
                # switched off. They now run against the parent.
                # Caller-supplied ``instructions`` are unaffected either way:
                # they compose additively with the authored text rather than
                # being replaced by it.
                if _sub_spec_for_instructions is None:
                    _warn_unresolved_sub_agent(conv_id, _sub_agent_name)
                else:
                    _turn_spec_for_instructions = _sub_spec_for_instructions
            if (
                _harness_override_applied
                and spawn_env is None
                and _turn_spec_for_instructions is not None
            ):
                # harness + spawn_env are resolved together: rebuild the
                # spawn_env for the FINAL (overridden) harness_name now that
                # the spec is available, mirroring _resolve_effective_turn's
                # single-pass resolution on the other dispatch path.
                spawn_env = _build_spawn_env_from_spec(
                    _turn_spec_for_instructions,
                    harness_name,
                    cwd=await _session_runtime_cwd(conv_id),
                    workdir=_turn_workdir_for_instructions,
                    model_override=cast(str | None, body.get("model_override")),
                )

        instruction_composition = InstructionComposition(authored_present=False, composed=None)
        _ordinary_composed: str | None = None
        if _turn_spec_for_instructions is not None:
            _authored = raw_author_instructions(_turn_spec_for_instructions) is not None
            _framework_instructions = (
                (SHARED_SESSION_AUTHORSHIP_INSTRUCTION,)
                if shared_message_attribution_enabled() and conv_id in _author_attribution_sessions
                else ()
            )
            # Ordinary harnesses take the standard composed-per-turn string,
            # fallback seed and all, exactly as the background dispatch path
            # builds it — the two paths must agree on what an ordinary harness
            # receives. Only the two gated harnesses take the nullable variant
            # below, because their executors read the wire field directly and
            # must never be handed the fabricated fallback literal.
            _ordinary_composed = build_instructions(
                _turn_spec_for_instructions,
                _raw_per_request_instructions,
                [],
                framework_instructions=_framework_instructions,
            )
            instruction_composition = InstructionComposition(
                authored_present=_authored,
                composed=build_instructions_nullable(
                    _turn_spec_for_instructions,
                    _raw_per_request_instructions,
                    [],
                    framework_instructions=_framework_instructions,
                ),
            )

        if instruction_composition.authored_present:
            _delivery_caps = harness_capabilities().get(harness_name)
            _delivery_value = (
                _delivery_caps.instruction_delivery
                if _delivery_caps is not None
                else InstructionDelivery.UNKNOWN
            )
            # At most once per (conversation, harness, delivery value) over
            # the conversation's whole lifetime. The harness is part of the
            # key, so two harnesses sharing one delivery value each warn once.
            # Membership in the set of pairs already warned for — NOT a
            # comparison against the last one, which would re-warn for a pair
            # that recurs after an intervening different one.
            _warned_for = (harness_name, _delivery_value)
            _already_warned = _warned_for in _instruction_delivery_warned.get(conv_id, ())
        else:
            _delivery_value = InstructionDelivery.UNKNOWN
            _warned_for = (harness_name, _delivery_value)
            _already_warned = True

        if not _already_warned:
            if _delivery_value is InstructionDelivery.NOT_DELIVERED:
                _instruction_delivery_warned.setdefault(conv_id, set()).add(_warned_for)
                _logger.warning(
                    "conversation %s: agent-authored instructions are not delivered to "
                    "harness %r (instruction_delivery=not-delivered) — the vendor agent "
                    "will not see AgentSpec.instructions for this conversation",
                    conv_id,
                    harness_name,
                )
            elif _delivery_value is InstructionDelivery.UNKNOWN:
                _instruction_delivery_warned.setdefault(conv_id, set()).add(_warned_for)
                _logger.warning(
                    "conversation %s: agent-authored instructions are present, but harness "
                    "%r declares no instruction_delivery capability (instruction_delivery="
                    "unknown) — whether the vendor agent sees AgentSpec.instructions for "
                    "this conversation is undetermined",
                    conv_id,
                    harness_name,
                )

        if (
            harness_name in _GATED_COMPOSED_INSTRUCTION_HARNESSES
            and _turn_spec_for_instructions is not None
        ):
            # These harnesses read the wire ``instructions`` field
            # themselves (opencode-native's NativePrompt.system_prompt via
            # its executor's run_turn `system_prompt` param; hermes the
            # same). The wire value set upstream (if any) is still the old
            # fallback-including composed-per-turn string — swap in the
            # gated nullable value computed above so neither harness ever
            # sees the fabricated "You are a helpful assistant." literal,
            # framework-only or otherwise. No new wire field: same
            # ``instructions`` key, harness-conditional value only.
            # Overwriting is safe ONLY because the composed value already
            # folded this turn's raw per-request text in (see
            # _raw_per_request_instructions). Compose first, then overwrite —
            # overwriting a body whose text was never composed silently drops
            # genuine caller instructions.
            # Gated on a POSITIVELY resolved spec (not a "did resolution
            # fail" flag) — any indeterminate state leaves caller-supplied
            # ``instructions`` already in the body untouched rather than
            # guessing. See the comment above ``_turn_spec_for_instructions``.
            if instruction_composition.composed:
                body["instructions"] = instruction_composition.composed
            else:
                body.pop("instructions", None)
        elif _ordinary_composed is not None:
            # Every OTHER harness on this path. Without this, a direct
            # ?stream=true turn carries only whatever raw text the caller
            # sent, so an agent's own authored instructions never reach
            # codex, copilot, open-responses, openai-agents, pi and the rest,
            # and a turn with no caller text carries no instructions field at
            # all. Composition belongs on both dispatch paths, not just the
            # background one.
            body["instructions"] = _ordinary_composed

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

            async def _opencode_boot_spec() -> AgentSpec | ResolvedSpec | None:
                """Resolve this turn's spec for the cold-boot, tolerating failure.

                Passes the already-known dispatched agent id for THIS turn
                directly, rather than letting ``_resolve_session_agent_spec``
                fall back to a fresh session-snapshot fetch — see the
                provenance-eviction comment above this function's entry for
                why that fallback races an in-flight agent switch. A resolver
                failure degrades to no spec, matching every other
                resolver-failure-tolerant path here (``InstructionComposition``
                catches the same exception set below): a transient failure must
                not 503 the turn when the harness is known and the terminal can
                boot without a spec.
                """
                try:
                    return await _resolve_session_agent_spec(
                        conv_id, agent_id_hint=_early_turn_agent_id
                    )
                except (OmnigentError, httpx.HTTPError, RuntimeError):
                    return None

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
                    resolve_agent_spec=_opencode_boot_spec,
                    reraise=True,
                )
            except Exception as exc:
                _logger.exception("opencode-native cold-boot ensure failed for %s", conv_id)
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_boot_failed",
                        "detail": _client_safe_error_detail(exc, context="opencode-native boot"),
                    },
                )

        # Attest provenance only now that every resolution step this
        # function performs on this turn's behalf (the opencode-native
        # cold-boot block above, agent-hinted so it cannot race a lagging
        # snapshot) has actually completed — never before the value it
        # gates exists. The generation captured before those steps is what
        # makes that hold across them: a reset landing while they run
        # supersedes this turn, and the marker write is dropped rather than
        # republishing an agent the session has already moved off.
        if _early_turn_agent_id:
            _session_cache_put(
                _session_agent_ids,
                conv_id,
                _early_turn_agent_id,
                generation=_early_turn_generation,
            )

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
        # Every ``_session_spec_cache`` read further down (eager MCP
        # resolution, the lazy resolver, and the local-tool dispatch hint)
        # goes through ``_cache_get_for_agent(..., _turn_agent_id)``, which
        # checks the entry's own agent tag rather than a parallel marker —
        # an entry left over for a different agent is a miss regardless of
        # what ``_session_agent_ids`` currently says.
        _has_mcp_hint = dispatch.has_mcp_servers if dispatch else body.get("has_mcp_servers")
        _turn_spec: object | None = None
        _turn_spec_entry: object | None = None
        _turn_spec_resolved = False
        _mcp_schemas: list[_JsonObject] = []
        _mcp_tool_names: set[str] = set()
        _eager_spec_error: tuple[str, str] | None = None
        if _has_mcp_hint is True and _turn_agent_id:
            # Cross-conversation read of the global agent-keyed cache: a hit
            # here may have been reinstated by a DIFFERENT conversation's
            # in-flight fill after this agent was reset. Known deferred gap,
            # not closed by the generation guard — see _agent_spec_cache_put.
            _turn_spec_entry = _spec_cache.get(_turn_agent_id)
            _turn_spec = _unwrap_resolved_spec(_turn_spec_entry)
            if _turn_spec is None:
                _session_entry = _cache_get_for_agent(_session_spec_cache, conv_id, _turn_agent_id)
                _turn_spec_entry = _session_entry
                _turn_spec = _unwrap_resolved_spec(_session_entry)
            if _turn_spec is None and spec_resolver is not None:
                _eager_spec_generation = _session_cache_generation(conv_id)
                try:
                    _resolved_turn_spec = await spec_resolver(_turn_agent_id, conv_id)
                    _turn_spec = _unwrap_resolved_spec(_resolved_turn_spec)
                except (httpx.HTTPError, RuntimeError) as exc:
                    _logger.warning(
                        "eager turn spec resolution failed for %s: %s",
                        conv_id,
                        exc,
                        exc_info=True,
                    )
                    _eager_spec_error = (
                        type(exc).__name__,
                        "Failed to resolve the agent spec for this turn.",
                    )
                else:
                    if _resolved_turn_spec is not None and _turn_spec is not None:
                        _agent_spec_cache_put(
                            _turn_agent_id,
                            _resolved_turn_spec,
                            session_id=conv_id,
                            generation=_eager_spec_generation,
                        )
                        _turn_spec_entry = _resolved_turn_spec
            _turn_spec_resolved = True
            _turn_mcp = ProxyMcpManager(conv_id, server_client)
            if _eager_spec_error is None and _turn_spec is not None:
                try:
                    _mcp = await _turn_mcp.schemas_for(cast(AgentSpec, _turn_spec))
                    _mcp_schemas = _mcp.schemas
                    _mcp_tool_names = _mcp.tool_names
                    for _srv, _err in _mcp.failures.items():
                        _logger.warning("runner MCP %r unavailable for this turn: %s", _srv, _err)
                except Exception:
                    _logger.exception("runner mcp_manager.schemas_for failed")

        async def _resolve_turn_spec_lazy() -> tuple[object | None, tuple[str, str] | None]:
            nonlocal _turn_spec, _turn_spec_entry, _turn_spec_resolved
            if _turn_spec_resolved:
                return _turn_spec_entry or _turn_spec, None
            _turn_spec_resolved = True
            session_cached = _cache_get_for_agent(_session_spec_cache, conv_id, _turn_agent_id)
            if session_cached is not None:
                _turn_spec_entry = session_cached
                _turn_spec = _unwrap_resolved_spec(session_cached)
                return session_cached, None
            if not _turn_agent_id or spec_resolver is None:
                return None, None
            # Same cross-conversation caveat as the eager read above: this
            # global agent-keyed hit is not fenced against another
            # conversation's stale reinstatement.
            cached = _spec_cache.get(_turn_agent_id)
            if cached is not None:
                _turn_spec_entry = cached
                _turn_spec = _unwrap_resolved_spec(cached)
                return cached, None
            _lazy_spec_generation = _session_cache_generation(conv_id)
            try:
                resolved = await spec_resolver(_turn_agent_id, conv_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                _logger.warning(
                    "lazy turn spec resolution failed for %s: %s",
                    conv_id,
                    exc,
                    exc_info=True,
                )
                return None, (
                    type(exc).__name__,
                    "Failed to resolve the agent spec for this turn.",
                )
            if resolved is not None:
                _agent_spec_cache_put(
                    _turn_agent_id,
                    resolved,
                    session_id=conv_id,
                    generation=_lazy_spec_generation,
                )
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
                _fail = {
                    "type": "response.failed",
                    "error": {
                        "message": _err_msg,
                        "type": _err_type,
                    },
                }
                _publish_event(conv_id, _fail)
                _on_proxy_stream_end(
                    conv_id,
                    error={"message": _err_msg, "type": _err_type},
                )
                yield _response_failed_event({"message": _err_msg, "type": _err_type})
                return

            event_body = _wrap_as_message_event(body)
            _inject_mcp_schemas(event_body, _mcp_schemas)
            try:
                async with client.stream(
                    "POST",
                    f"/v1/sessions/{conv_id}/events",
                    json=event_body,
                    timeout=None,
                ) as harness_resp:
                    if harness_resp.status_code != 200:
                        _fail_status = {
                            "type": "response.failed",
                            "error": {
                                "status": harness_resp.status_code,
                            },
                        }
                        _publish_event(
                            conv_id,
                            _fail_status,
                        )
                        _on_proxy_stream_end(
                            conv_id,
                            error={"status": harness_resp.status_code},
                        )
                        yield _response_failed_event({"status": harness_resp.status_code})
                        return

                    _response_id: str | None = None
                    _omnigent_task_id = cast(str | None, body.get("task_id"))
                    _buffer = ""
                    _dispatch_tasks: list[_asyncio.Task[object]] = []
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
                                    raise _ContextWindowOverflow(*_overflow)

                                _evt_type = event.get("type")
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
                                                _history_message_from_body(_m)
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
                                    await _handle_harness_compaction(conv_id, event)

                                if is_action_required(event):
                                    tool_name = get_tool_name(event)
                                    is_mcp = tool_name in _mcp_tool_names
                                    _spec_for_dispatch_hint = _unwrap_resolved_spec(
                                        _cache_get_for_agent(
                                            _session_spec_cache, conv_id, _turn_agent_id
                                        )
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
                                            _fail = {
                                                "type": "response.failed",
                                                "error": {
                                                    "message": _err_msg,
                                                    "type": _err_type,
                                                },
                                            }
                                            _publish_event(conv_id, _fail)
                                            _on_proxy_stream_end(
                                                conv_id,
                                                error={
                                                    "message": _err_msg,
                                                    "type": _err_type,
                                                },
                                            )
                                            yield _response_failed_event(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            return
                                        _dispatch_workdir = (
                                            _resolved_workdir_for_spec(
                                                _spec_for_dispatch_entry,
                                                runner_workspace,
                                            )
                                            if _is_spec_local
                                            else runner_workspace
                                        )
                                        _spec_for_dispatch = _unwrap_resolved_spec(
                                            _spec_for_dispatch_entry
                                        )
                                        event[_RUNNER_DISPATCHED_FIELD] = True
                                        raw_sse_bytes = _encode_sse_event(event)
                                        _agent_id_for_dispatch = cast(
                                            str | None, body.get("agent_id")
                                        )
                                        _dispatch_mcp = ProxyMcpManager(
                                            conv_id,
                                            server_client,
                                            publish_event=_publish_event,
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
                                                    runner_workspace=_dispatch_workdir,
                                                    mcp_manager=cast(
                                                        "RunnerMcpManager", _dispatch_mcp
                                                    ),
                                                    session_inbox=_session_inboxes.get(conv_id),
                                                    session_async_tasks=_session_async_tasks.get(
                                                        conv_id
                                                    ),
                                                    publish_event=_publish_event,
                                                    filesystem_registry=filesystem_registry,
                                                )
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
                                            )
                                        )
                                    )
                                    continue

                            if event is None:
                                yield raw_sse_bytes
                                continue
                            if not _defer_publish and event.get("type") != "response.created":
                                _publish_event(conv_id, event)
                            if dispatch is not None and event.get(_RUNNER_DISPATCHED_FIELD):
                                pass
                            else:
                                yield raw_sse_bytes

                    if _dispatch_tasks:
                        await _asyncio.gather(*_dispatch_tasks, return_exceptions=True)

                    _on_proxy_stream_end(conv_id, error=_stream_failed_error)

            except _ContextWindowOverflow as overflow:
                _error = {
                    "code": "context_length_exceeded",
                    "message": (
                        f"Context window exceeded: {overflow.actual_tokens} tokens "
                        f"> {overflow.max_tokens} max"
                    ),
                    "type": "_ContextWindowOverflow",
                }
                _overflow_fail = {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": _error},
                    "error": _error,
                }
                _publish_event(conv_id, _overflow_fail)
                _on_proxy_stream_end(conv_id, error=_error)
                yield _response_failed_event(_error)

            except (httpx.HTTPError, RuntimeError) as exc:
                _logger.warning(
                    "proxy stream connection error for %s: %s",
                    conv_id,
                    exc,
                    exc_info=True,
                )
                _error = {
                    "code": "connection_error",
                    "message": "Harness stream connection error.",
                    "type": type(exc).__name__,
                }
                _http_fail = {
                    "type": "response.failed",
                    "response": {"status": "failed", "error": _error},
                    "error": _error,
                }
                _publish_event(conv_id, _http_fail)
                _on_proxy_stream_end(conv_id, error=_error)
                yield _response_failed_event(_error)

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
            "post_session_events: conv=%s type=%s active=%s buffer_len=%d content_types=%s",
            conversation_id,
            body_type,
            conversation_id in _active_turns,
            len(_session_message_buffers.get(conversation_id, [])),
            [b.get("type") for b in body.get("content", []) if isinstance(b, dict)]
            if isinstance(body, dict)
            else "N/A",
        )
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
                _note_message_author(conversation_id, message_body)

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
                    )
                    _session_message_buffers.setdefault(
                        conversation_id,
                        [],
                    ).append(message_body)
                    if _can_forward and process_manager is not None:
                        try:
                            _hc = await process_manager.get_client(conversation_id, "any")
                            injection_body = _message_body_for_harness(
                                message_body,
                                force_author_attribution=(
                                    conversation_id in _author_attribution_sessions
                                ),
                            )
                            _injection_resp = await _hc.post(
                                f"/v1/sessions/{conversation_id}/events",
                                json=injection_body,
                                timeout=5.0,
                            )
                            if _injection_resp.status_code >= 400:
                                _logger.warning(
                                    "post_session_events: mid-turn injection forward rejected "
                                    "conv=%s status=%s body=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                    _response_body_preview(_injection_resp),
                                )
                            else:
                                _logger.debug(
                                    "post_session_events: mid-turn injection forward accepted "
                                    "conv=%s status=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                )
                        except (httpx.HTTPError, RuntimeError, asyncio.TimeoutError):
                            _logger.debug(
                                "mid-turn injection forward failed for %s; "
                                "LLM will see message on next turn",
                                conversation_id,
                                exc_info=True,
                            )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "buffered",
                            "detail": ("Message buffered; active turn will process it."),
                        },
                    )

                new_item = _history_message_from_body(message_body)
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

                _active_turns[conversation_id] = None
                _logger.info(
                    "post_session_events: starting background turn conv=%s",
                    conversation_id,
                )

                _publish_turn_status(conversation_id, "running")

                if stream:
                    response = await _stream_message_to_harness(message_body, conversation_id)
                    if not isinstance(response, StreamingResponse):
                        _on_proxy_stream_end(
                            conversation_id,
                            error={"message": "harness returned error response"},
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
            _harness = _session_harness_name(conversation_id)
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
            if status in ("running", "waiting", "idle", "failed"):
                resource_registry.note_external_session_status(conversation_id, status)
                _fan_out_child_delta_to_parent(
                    conversation_id,
                    {"type": "session.status", "status": status},
                    latest_assistant_text=output,
                    allow_history_preview_fallback=False,
                )
            if status in ("idle", "failed"):
                recovered_entry = await _ensure_subagent_work_entry(conversation_id)
            if status == "idle":
                delivery_ack = _mark_subagent_terminal_and_wake(
                    conversation_id,
                    status="completed",
                    output=output if output is not None else "",
                )
            elif status == "failed":
                delivery_ack = _mark_subagent_terminal_and_wake(
                    conversation_id,
                    status="failed",
                    output=output or "Error: native sub-agent turn failed",
                )
            if delivery_ack is not None:
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
            _harness = _session_harness_name(conversation_id)
            _stop_resp = await _native_interrupt_runner.stop(_harness, conversation_id)
            if _stop_resp is not None:
                return _stop_resp
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "effort_change":
            harness = _session_harness_name(conversation_id)
            if harness in ("claude-native", "codex-native"):
                effort = body.get("effort") if isinstance(body, dict) else None
                if effort is not None and not isinstance(effort, str):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "invalid_input",
                            "detail": "Body 'effort' must be a string or null",
                        },
                    )
                if harness == "codex-native":
                    return await _handle_codex_native_settings_update(
                        conversation_id,
                        {"effort": effort},
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
                    return await _handle_codex_native_settings_update(
                        conversation_id,
                        {"model": model.strip()},
                    )
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
            pending_approvals.resolve(_data.get("elicitation_id", ""), _elicit_action == "accept")
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

    async def _resolve_conversation_id(response_id: str) -> str | None:
        return _resp_to_conv.get(response_id)

    @app.get("/v1/sessions/{session_id}/resources")
    async def list_session_resources(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        type: str | None = Query(default=None),
    ) -> JSONResponse:
        from omnigent.entities.pagination import paginate_in_memory

        spec = await _resolve_session_agent_spec(
            session_id, agent_id_hint=_session_agent_ids.get(session_id)
        )
        full = resource_registry.list_resources(
            session_id,
            resource_type=cast(_ResourceType | None, type),
            agent_spec=spec,
        )
        page = paginate_in_memory(
            full.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    def _build_typed_list_response(
        session_id: str,
        resource_type: _ResourceType,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        from omnigent.entities.pagination import paginate_in_memory

        filtered = resource_registry.list_resources(
            session_id,
            resource_type=resource_type,
        )
        page = paginate_in_memory(
            filtered.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/environments")
    async def list_session_environments(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        return _build_typed_list_response(
            session_id,
            "environment",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}")
    async def get_session_environment(
        session_id: str,
        environment_id: str,
    ) -> JSONResponse:
        agent_spec = await _resolve_session_agent_spec(
            session_id, agent_id_hint=_session_agent_ids.get(session_id)
        )
        resource = resource_registry.get_resource(
            session_id,
            environment_id,
        )
        if resource is None or resource.type != "environment":
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Environment {environment_id!r} not found",
                    }
                },
            )
        content = session_resource_view_to_dict(resource)
        if environment_id == DEFAULT_ENVIRONMENT_ID:
            root = resource_registry.compute_default_env_root(session_id, agent_spec)
            if root is not None:
                raw_metadata = content.get("metadata")
                metadata: dict[str, object] = (
                    dict(cast(Mapping[str, object], raw_metadata))
                    if isinstance(raw_metadata, Mapping)
                    else {}
                )
                metadata["root"] = root
                home = os.path.expanduser("~")
                if os.path.isabs(home):
                    metadata["home"] = home
                content = {**content, "metadata": metadata}
        return JSONResponse(
            status_code=200,
            content=content,
        )

    @app.get("/v1/sessions/{session_id}/resources/terminals")
    async def list_session_terminals(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        return _build_typed_list_response(
            session_id,
            "terminal",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.post("/v1/sessions/{session_id}/resources/terminals")
    async def create_session_terminal(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await request.json()
        terminal_name = body.get("terminal")
        session_key = body.get("session_key")
        if not terminal_name or not session_key:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": ("'terminal' and 'session_key' are required"),
                    }
                },
            )

        _ensure_agent = native_coding_agent_for_terminal_name(terminal_name)
        if (
            body.get("ensure_native_terminal")
            and _ensure_agent is not None
            and session_key == "main"
            # antigravity's ensure arm declined to auto-create when the request
            # carried a spec (the CLI-wrapper launch path owns that case).
            and not (terminal_name == "antigravity" and body.get("spec"))
        ):
            # Each native harness contributes only the ensure hooks that differ
            # from the uniform base; a single _ensure_native_terminal call runs
            # them. The 4 uniform harnesses (goose/kiro/hermes/qwen) need only the
            # base context; pi/opencode/cursor/kimi/claude resolve an agent spec
            # via build_context; codex/antigravity add an ownership check (and
            # codex a one-shot policy-notice response wrap).
            _ensure_locks = {
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
            }[_ensure_agent.key]
            _ensure_ctx = NativeLaunchContext(
                session_id=session_id,
                resource_registry=resource_registry,
                publish_event=_publish_event,
                server_client=server_client,
                ensure_comment_relay=_ensure_comment_relay_started,
            )
            _ensure_build: (
                Callable[[NativeLaunchContext], Awaitable[NativeLaunchContext]] | None
            ) = None
            _ensure_is_owned: (
                Callable[[SessionResourceRegistry, SessionResourceView], bool] | None
            ) = None
            _ensure_finalize: Callable[[SessionResourceView], JSONResponse] | None = None
            _ensure_conflict: str | None = None

            if terminal_name == "claude":

                async def _claude_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    claude_agent_spec = await _resolve_session_agent_spec(
                        session_id, agent_id_hint=_session_agent_ids.get(session_id)
                    )
                    return dataclasses.replace(
                        ctx,
                        agent_spec=claude_agent_spec,
                        auth_token_factory=auth_token_factory,
                        resolve_launch_config=lambda: _resolve_session_claude_launch_config(
                            session_id
                        ),
                        record_launch_config=_guarded_launch_config_recorder(session_id),
                    )

                _ensure_build = _claude_ensure_build

            elif terminal_name == "codex":

                async def _codex_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    codex_agent_spec = await _resolve_session_agent_spec(
                        session_id, agent_id_hint=_session_agent_ids.get(session_id)
                    )
                    return dataclasses.replace(ctx, agent_spec=codex_agent_spec)

                _ensure_build = _codex_ensure_build
                _ensure_is_owned = _is_runner_owned_codex_terminal
                _ensure_finalize = lambda view: _codex_ensure_response_with_policy_notice(  # noqa: E731
                    session_id, view
                )
                _ensure_conflict = (
                    "Existing codex terminal is not a runner-owned Codex TUI "
                    "and could not be closed."
                )

            elif terminal_name == "antigravity":
                _ensure_is_owned = _is_runner_owned_antigravity_terminal
                _ensure_conflict = (
                    "Existing antigravity terminal is not a runner-owned agy TUI "
                    "and could not be closed."
                )

            elif terminal_name in ("pi", "opencode"):
                # pi/opencode resolve the spec unwrapped — a resolution error
                # surfaces as a terminal-start error (the resolver does not
                # swallow it).
                async def _spec_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    return dataclasses.replace(
                        ctx,
                        agent_spec=await _resolve_session_agent_spec(
                            session_id, agent_id_hint=_session_agent_ids.get(session_id)
                        ),
                    )

                _ensure_build = _spec_ensure_build

            elif terminal_name in ("cursor", "kimi"):

                async def _spec_or_none_ensure_build(
                    ctx: NativeLaunchContext,
                ) -> NativeLaunchContext:
                    return dataclasses.replace(
                        ctx,
                        agent_spec=await _resolve_session_agent_spec_or_none(
                            session_id, agent_id_hint=_session_agent_ids.get(session_id)
                        ),
                    )

                _ensure_build = _spec_or_none_ensure_build

            _ensure_result = await _ensure_native_terminal(
                terminal_name,
                _ensure_ctx,
                ensure_locks=_ensure_locks,
                build_context=_ensure_build,
                is_owned=_ensure_is_owned,
                conflict_message=_ensure_conflict,
                finalize=_ensure_finalize,
            )
            if _ensure_result is not None:
                return _ensure_result

        from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

        cwd_override = body.get("cwd")
        sandbox_override = body.get("sandbox")
        spec = body.get("spec") or {}

        agent_spec = await _resolve_session_agent_spec(
            session_id, agent_id_hint=_session_agent_ids.get(session_id)
        )
        agent_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None

        declared_terminal = None
        if agent_spec is not None:
            terminals_map = getattr(agent_spec, "terminals", None) or {}
            declared_terminal = terminals_map.get(terminal_name)

        if declared_terminal is not None:
            from omnigent.tools.builtins.sys_terminal import (
                _materialize_terminal_spec_for_launch,
                _synthesize_parent_os_env,
            )

            default_root = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = _materialize_terminal_spec_for_launch(declared_terminal, default_root)
            agent_os_env = _synthesize_parent_os_env(agent_os_env, default_root)
            cwd_override = cwd_override or spec.get("cwd")
        else:
            spec_cwd = spec.get("cwd")
            if spec_cwd is None or spec_cwd in (".", "./"):
                spec_cwd = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type=spec.get("os_env_type", "caller_process"),
                    cwd=spec_cwd,
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=spec.get("command", "bash"),
                args=spec.get("args", []),
                env=spec.get("env", {}),
                scrollback=spec.get("scrollback", 10000),
                tmux_allow_passthrough=bool(spec.get("tmux_allow_passthrough", False)),
                tmux_start_on_attach=bool(spec.get("tmux_start_on_attach", False)),
            )
        bridge_inject = bool(body.get("bridge_inject_dir"))
        bridge_id = session_id
        relay_existed = False
        if bridge_inject:
            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=session_id,
            )
            relay_existed = session_id in _session_comment_relays
            await _ensure_comment_relay_started(session_id, bridge_id=bridge_id)

        try:
            launch_method = (
                resource_registry.launch_required_terminal
                if bridge_inject
                else resource_registry.launch_auxiliary_terminal
            )
            resource_view = await launch_method(
                session_id=session_id,
                terminal_name=terminal_name,
                session_key=session_key,
                spec=env_spec,
                cwd_override=cwd_override,
                sandbox_override=sandbox_override,
                parent_os_env=agent_os_env,
                resource_role=(CLAUDE_NATIVE_TERMINAL_ROLE if bridge_inject else None),
            )
        except RuntimeError as exc:
            if bridge_inject and not relay_existed:
                relay = _session_comment_relays.pop(session_id, None)
                if relay is not None:
                    relay.close()
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "terminal_launch_failed",
                        "message": _client_safe_error_detail(exc, context="terminal launch"),
                    }
                },
            )

        if bridge_inject:
            _publish_tmux_target_for_bridge(
                resource_registry=resource_registry,
                session_id=session_id,
                bridge_id=bridge_id,
                terminal_name=terminal_name,
                session_key=session_key,
            )

        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource_view),
        )

    async def _ensure_native_terminal_for_turn(conv_id: str, harness_name: str | None) -> None:
        """Re-create a reaped native pane before forwarding a turn (#1349 self-heal).

        The native-pane idle reaper may reclaim an idle pane while a session sits
        between turns. ``NativeServerHarness.run_turn`` forwards into the live
        pane and assumes it exists, so a turn arriving WITHOUT a client handshake
        (a sub-agent or API forward to a long-idle session) would otherwise inject
        into a dead tmux target and lose the message. This re-ensures the pane
        first. Idempotent: a no-op when the harness is not a native CLI harness or
        the pane is already live. Reuses ``create_session_terminal``'s
        ``ensure_native_terminal`` path, so the pane resumes via the vendor CLI's
        own ``--resume`` (no fresh-start, no lost history).

        Detection has two layers: (1) the reaper POPPING the registry entry
        when it reaps (``registry.close()`` -> ``get()`` returns ``None``),
        and (2) an ``is_alive()`` probe when the registry entry exists, catching
        crashed-but-registered panes (tmux killed externally without
        ``close()``). The probe runs only when a turn arrives, not on a
        poll. Every native short-name this can target has a matching
        ``ensure_native_terminal`` branch in ``create_session_terminal``
        (kept in lockstep with ``harness_aliases.NATIVE_HARNESSES``).
        """
        terminal_name = native_terminal_name(harness_name)
        if terminal_name is None:
            return
        terminal_registry = resource_registry.terminal_registry if resource_registry else None
        if terminal_registry is None:
            return
        instance = terminal_registry.get(conv_id, terminal_name, "main")
        if instance is not None:
            if await instance.is_alive():
                return  # pane is registered and alive — nothing to heal
            _logger.info(
                "native pane registered but dead for conv=%s harness=%s; closing stale entry",
                conv_id,
                harness_name,
            )
            # Re-check the registry before closing: a concurrent ensure/recreate
            # path may have already replaced this entry with a live pane between
            # our get() and now.  Only close if the registry still points at the
            # same dead instance we just probed.
            current = terminal_registry.get(conv_id, terminal_name, "main")
            if current is instance:
                # is_alive() set instance.running=False as a side effect;
                # restore it so close() issues tmux kill-server.
                instance.running = True
                try:
                    await terminal_registry.close(conv_id, terminal_name, "main")
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — cleanup is best-effort
                    _logger.warning(
                        "failed to close stale native pane for conv=%s; proceeding to re-create",
                        conv_id,
                        exc_info=True,
                    )
            else:
                _logger.info(
                    "stale entry already replaced for conv=%s; skipping close",
                    conv_id,
                )
        _logger.info(
            "native pane missing for conv=%s harness=%s; re-ensuring before turn (#1349)",
            conv_id,
            harness_name,
        )
        try:
            resp = await create_session_terminal(
                conv_id,
                cast(
                    Request,
                    _BodyRequest(
                        {
                            "terminal": terminal_name,
                            "session_key": "main",
                            "ensure_native_terminal": True,
                        }
                    ),
                ),
            )
        except Exception:
            _logger.exception("native pane self-heal failed for conv=%s", conv_id)
            return
        status = getattr(resp, "status_code", 200)
        if status >= 400:
            _logger.warning(
                "native pane self-heal returned status %s for conv=%s (%s)",
                status,
                conv_id,
                terminal_name,
            )

    @app.get("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def get_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        resource = await resource_registry.get_terminal_resource(
            session_id,
            terminal_id,
        )
        if resource is None:
            _log_terminal_lookup_miss(resource_registry, session_id, terminal_id)
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    @app.post("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer")
    async def transfer_session_terminal(
        session_id: str,
        terminal_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await request.json()
        target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
        if not isinstance(target_session_id, str) or not target_session_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'target_session_id' is required",
                    }
                },
            )
        try:
            resource = await resource_registry.transfer_terminal(
                source_session_id=session_id,
                target_session_id=target_session_id,
                terminal_id=terminal_id,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "resource_conflict",
                        "message": _client_safe_error_detail(exc, context="terminal transfer"),
                    }
                },
            )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Terminal {terminal_id!r} not found",
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    @app.delete("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def delete_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        closed = await resource_registry.close_terminal(
            session_id,
            terminal_id,
        )
        if not closed:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": terminal_id,
                "object": "session.resource.deleted",
                "deleted": True,
            },
        )

    async def _recreate_repl_terminal(
        session_id: str, terminal_id: str
    ) -> TerminalListEntry | None:
        if resource_registry is None or resource_registry.terminal_registry is None:
            return None
        registry = resource_registry.terminal_registry
        lock = _repl_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = registry.get(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
            if existing is None or not existing.running or not await existing.is_alive():
                await registry.close(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
                try:
                    repl_agent_spec = await _resolve_session_agent_spec(
                        session_id, agent_id_hint=_session_agent_ids.get(session_id)
                    )
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
                        "Failed to recreate omnigent REPL terminal for %s",
                        session_id,
                    )
                    return None
        return resolve_terminal_entry_by_resource_id(session_id, terminal_id, registry)

    async def _recreate_qwen_terminal(
        session_id: str, terminal_id: str
    ) -> TerminalListEntry | None:
        if resource_registry is None or resource_registry.terminal_registry is None:
            return None
        registry = resource_registry.terminal_registry
        lock = _qwen_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = registry.get(session_id, "qwen", "main")
            if existing is None or not existing.running or not await existing.is_alive():
                await registry.close(session_id, "qwen", "main")
                try:
                    await _auto_create_qwen_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception:
                    _logger.exception(
                        "Failed to recreate omnigent qwen terminal for %s",
                        session_id,
                    )
                    return None
        return resolve_terminal_entry_by_resource_id(session_id, terminal_id, registry)

    @app.websocket("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def terminal_resource_attach_ws(
        websocket: WebSocket,
        session_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
        transport: str | None = Query(default=None),
    ) -> None:
        await websocket.accept()
        entry = resolve_terminal_entry_by_resource_id(
            session_id,
            terminal_id,
            terminal_registry,
        )
        terminal_role = (
            resource_registry.terminal_resource_role(session_id, terminal_id)
            if resource_registry is not None
            else None
        )
        if entry is None or not entry.instance.running or not await entry.instance.is_alive():
            if terminal_role == OMNIGENT_REPL_TERMINAL_ROLE:
                entry = await _recreate_repl_terminal(session_id, terminal_id)
            elif terminal_role == QWEN_NATIVE_TERMINAL_ROLE:
                entry = await _recreate_qwen_terminal(session_id, terminal_id)
            else:
                entry = None
            if entry is None:
                await websocket.close(
                    code=WS_CLOSE_TERMINAL_NOT_FOUND,
                    reason="terminal resource not found or not running",
                )
                return
        _repop_task = asyncio.create_task(
            _repop_pending_cost_popup_on_attach(
                session_id,
                str(entry.instance.socket_path),
                entry.instance.tmux_target,
            )
        )
        _COST_POPUP_REPOP_TASKS.add(_repop_task)
        _repop_task.add_done_callback(_COST_POPUP_REPOP_TASKS.discard)
        from omnigent.inner.terminal import (
            TERMINAL_TRANSPORT_CONTROL,
            resolve_terminal_transport,
        )

        resolved_transport = resolve_terminal_transport(
            override=transport,
            spec_transport=entry.instance.terminal_transport,
        )
        bridge = (
            bridge_tmux_control_to_websocket
            if resolved_transport == TERMINAL_TRANSPORT_CONTROL
            else bridge_tmux_pty_to_websocket
        )
        await bridge(
            websocket,
            socket_path=str(entry.instance.socket_path),
            tmux_target=entry.instance.tmux_target,
            read_only=read_only,
            on_client_interaction=entry.instance.note_client_interaction,
        )

    async def _require_os_env(session_id: str) -> AgentSpec | None:
        spec = await _resolve_session_agent_spec(
            session_id, agent_id_hint=_session_agent_ids.get(session_id)
        )
        if spec is not None and getattr(spec, "os_env", None) is None:
            raise HTTPException(
                status_code=404,
                detail="Session agent has no os_env configured; filesystem API unavailable.",
            )
        return spec

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem")
    async def list_environment_root(
        session_id: str,
        environment_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        await _require_os_env(session_id)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            "",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/search")
    async def search_environment_files(
        session_id: str,
        environment_id: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
            split_glob_list,
        )

        include_patterns = split_glob_list(include)
        exclude_patterns = split_glob_list(exclude)

        agent_spec = await _require_os_env(session_id)  # also resolves spec
        await _ensure_session_registered(session_id)
        env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
        fs = CallerProcessFilesystem(env)
        entries = await fs.search_files(
            q,
            include=include_patterns,
            exclude=exclude_patterns,
            limit=limit,
        )
        data = [_fs_entry_to_dict(e) for e in entries]
        return JSONResponse(
            status_code=200,
            content={"object": "list", "data": data, "has_more": len(entries) >= limit},
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/changes")
    async def list_filesystem_changes(
        session_id: str,
        environment_id: str,  # noqa: ARG001
    ) -> JSONResponse:
        from omnigent.runtime.filesystem_registry import GitStatusUnavailable

        await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)
        try:
            raw_changes = (
                session_registry.list_changed_files(
                    session_id,
                    limit=10_000,
                )
                if session_registry is not None
                else []
            )
        except GitStatusUnavailable as exc:
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "git_status_failed", "message": exc.reason}},
            )
        data = [
            {
                "object": "session.environment.filesystem.entry",
                "path": rec["path"],
                "name": rec["path"].split("/")[-1],
                "status": rec["status"],
                "bytes": rec.get("bytes"),
                "modified_at": rec.get("modified_at"),
                "lines_added": rec.get("lines_added"),
                "lines_removed": rec.get("lines_removed"),
            }
            for rec in raw_changes
        ]
        return JSONResponse(
            status_code=200,
            content={"object": "list", "data": data, "has_more": False},
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/diff/{relative_path:path}"
    )
    async def read_environment_file_diff(
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> JSONResponse:
        agent_spec = await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)

        from omnigent.entities.environment_filesystem import InvalidPath
        from omnigent.runner.environment_filesystem import _validate_path

        try:
            relative_path = _validate_path(relative_path)
        except InvalidPath as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": str(exc),
                    }
                },
            )
        if not relative_path:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": "Cannot diff the environment root",
                    }
                },
            )

        from omnigent.runtime.filesystem_registry import GitStatusUnavailable

        try:
            record = (
                session_registry.get_changed_file(session_id, relative_path)
                if session_registry is not None
                else None
            )
        except GitStatusUnavailable as exc:
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "git_status_failed", "message": exc.reason}},
            )
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (
                            f"Path {relative_path!r} is not in the "
                            "changed-files registry for this session"
                        ),
                    }
                },
            )
        is_deleted = record.get("status") == "deleted"

        import asyncio as _asyncio

        before: str | None = (
            await _asyncio.to_thread(session_registry.get_baseline, relative_path)
            if session_registry is not None
            else None
        )

        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        after: str | None = None
        if not is_deleted:
            env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
            fs = CallerProcessFilesystem(env)
            content = await fs.read(relative_path, limit=None)
            after = content.data.decode(content.encoding or "utf-8", errors="replace")

        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.file_diff",
                "path": relative_path,
                "before": before,
                "after": after,
            },
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def read_or_list_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        await _require_os_env(session_id)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            relative_path,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.put(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def write_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        body = await request.json()
        content_str = body.get("content", "")
        encoding = body.get("encoding", "utf-8")
        create_parents = body.get("create_parents", True)
        content_bytes = content_str.encode(encoding)
        try:
            existing = await fs.read(relative_path, limit=None)
            if existing.encoding and filesystem_registry is not None:
                filesystem_registry.seed_snapshot(
                    relative_path,
                    existing.data.decode(existing.encoding, errors="replace"),
                    session_id=session_id,
                )
        except Exception:  # noqa: BLE001
            pass
        result = await fs.write(
            relative_path,
            content_bytes,
            create_parents=create_parents,
        )
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.write_result",
                "operation": result.operation,
                "path": result.path,
                "created": result.created,
                "bytes_written": result.bytes_written,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

    @app.patch(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def edit_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.entities.environment_filesystem import (
            TextEditRequest,
        )
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        try:
            existing = await fs.read(relative_path, limit=None)
            if existing.encoding and filesystem_registry is not None:
                filesystem_registry.seed_snapshot(
                    relative_path,
                    existing.data.decode(existing.encoding, errors="replace"),
                    session_id=session_id,
                )
        except Exception:  # noqa: BLE001
            pass
        body = await request.json()
        edit_req = TextEditRequest(
            old_text=body.get("old_text"),
            new_text=body.get("new_text"),
            replace_all=body.get("replace_all", False),
        )
        result = await fs.edit_text(relative_path, edit_req)
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.edit_result",
                "operation": result.operation,
                "path": result.path,
                "replacements": result.replacements,
                "bytes_before": result.bytes_before,
                "bytes_after": result.bytes_after,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

    @app.delete(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def delete_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        recursive: bool = Query(default=False),
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        result = await fs.delete(relative_path, recursive=recursive)
        if filesystem_registry is not None and result.type == "file":
            filesystem_registry.record_change(relative_path, "deleted", session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.delete_result",
                "operation": result.operation,
                "path": result.path,
                "deleted": result.deleted,
                "type": result.type,
                "bytes_deleted": result.bytes_deleted,
                "entries_deleted": result.entries_deleted,
            },
        )

    async def _ensure_session_registered(session_id: str) -> None:
        if session_id in _session_start_cache:
            return
        snapshot = await _session_snapshot(session_id)
        _session_start_cache[session_id] = snapshot.created_at
        if snapshot.ok:
            _session_workspace_cache[session_id] = snapshot.workspace

    async def _resolve_session_spec_entry(
        session_id: str, agent_id_hint: str | None = None
    ) -> _SpecEntry | None:
        # A cache read needs to know WHICH agent's entry it's looking for
        # before it can trust a hit (see _cache_get_for_agent) — with a
        # hint that's known up front, so check without taking the lock.
        # Without one, the agent is only known after the snapshot fetch
        # below, so the cache is consulted again once that's resolved.
        if agent_id_hint:
            _fast_hit = _cache_get_for_agent(_session_spec_cache, session_id, agent_id_hint)
            if _fast_hit is not None:
                # The sub-agent block below never runs on this return, so the
                # provenance check belongs here. A cached PARENT kept after a
                # miss is handed to every consumer of this resolver — terminal
                # auto-create, resource listing, os_env checks — and used to
                # go out with no signal at all once the first turn had filled
                # the cache. The warning is that signal, and it is repeated
                # for as long as the session keeps answering with the parent.
                _fast_prov = _session_spec_provenance(session_id, agent_id_hint)
                if isinstance(_fast_prov, str):
                    _warn_unresolved_sub_agent(session_id, _fast_prov)
                # Only a SETTLED entry is an answer. An unsettled one cannot
                # be reported here either — this return has fetched no
                # snapshot and does not know the name — so it declines the hit
                # and falls through to the full path below, which learns the
                # name, decides, and republishes. That costs one resolution,
                # inside a window the lock already serialises.
                if _session_spec_provenance_is_settled(_fast_prov):
                    return _fast_hit
        _fill_generation = _session_cache_generation(session_id)
        if spec_resolver is None:
            return None
        lock = _session_spec_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # ``agent_id_hint`` lets a caller that ALREADY knows the correct
            # current agent for this turn (e.g. a just-dispatched agent_id
            # from the request body) skip the session-snapshot lookup below
            # entirely. That snapshot is a separate, independently-lagging
            # source of truth: on the server side, an in-flight agent
            # switch may not have landed yet, so a snapshot fetched WHILE
            # the switch is in flight can report the PREVIOUS agent_id.
            # Without the hint, this function would resolve and cache a
            # spec for that stale agent — tagged, in the caller's mind, as
            # "the spec for the session's CURRENT agent" — a race a caller
            # who already has ground truth for this turn should never be
            # exposed to.
            if agent_id_hint:
                agent_id = agent_id_hint
                # The hint settles the AGENT, not the sub-agent. An absent
                # ``_session_sub_agent_names`` record means "not recorded
                # yet", not "this session has no sub-agent" — during another
                # task's in-flight recovery it is absent for a session that
                # very much has one, and reading it as "none" publishes an
                # entry claiming a resolution nobody performed. Ask the
                # snapshot for the fact instead; it is single-flight, so a
                # concurrent recovery is joined rather than duplicated.
                sub_agent_name = _session_sub_agent_names.get(session_id)
                if sub_agent_name is None:
                    _hint_snapshot = await _session_snapshot(session_id)
                    if _hint_snapshot.ok:
                        sub_agent_name = _hint_snapshot.sub_agent_name
                    else:
                        # Still unknown. Resolve for this caller, but publish
                        # nothing that claims the sub-agent question is
                        # settled.
                        _unknown_entry = await spec_resolver(agent_id, session_id)
                        _session_spec_cache_put(
                            session_id,
                            _unknown_entry,
                            generation=_fill_generation,
                            agent_tag=agent_id,
                            provenance=_SubAgentProvenance.UNDETERMINED,
                        )
                        return _unknown_entry
            else:
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
                sub_agent_name = snapshot.sub_agent_name
            _cached = _cache_get_for_agent(_session_spec_cache, session_id, agent_id)
            if _cached is not None:
                # Same reasoning as the lock-free hit above: this return also
                # skips the sub-agent block, so the provenance has to be read
                # here or not at all — and an unsettled entry is declined so
                # the resolution below can settle it. Unlike that hit, this one
                # already knows ``sub_agent_name``, but it is still the block
                # below that decides, so it does not warn on its own.
                _cached_prov = _session_spec_provenance(session_id, agent_id)
                if isinstance(_cached_prov, str):
                    _warn_unresolved_sub_agent(session_id, _cached_prov)
                if _session_spec_provenance_is_settled(_cached_prov):
                    return _cached
            spec_entry = await spec_resolver(agent_id, session_id)
            if spec_entry is None:
                raise OmnigentError(
                    f"session spec resolver: agent {agent_id!r} for "
                    f"session {session_id!r} was not found",
                    code=ErrorCode.NOT_FOUND,
                )
            # UNDETERMINED until the block below decides. The unwrap can
            # yield None, in which case no decision is made at all and the
            # entry must not claim one.
            _entry_provenance: _SubAgentProvenanceValue = _SubAgentProvenance.RESOLVED
            if sub_agent_name:
                _session_sub_agent_names[session_id] = sub_agent_name
                from omnigent.runtime.workflow import _find_spec_by_name

                parent_spec = _unwrap_resolved_spec(spec_entry)
                if parent_spec is None:
                    # Nothing to search, so nothing is decided. Saying
                    # RESOLVED here would hand later readers a spec labelled
                    # as this session's child on no evidence at all.
                    _entry_provenance = _SubAgentProvenance.UNDETERMINED
                else:
                    sub_spec = _find_spec_by_name(parent_spec, sub_agent_name)
                    if sub_spec is None:
                        # The PARENT entry is cached and returned unchanged, so
                        # every downstream caller of this resolver (terminal
                        # auto-create, resource listing, os_env checks, ...)
                        # sees the parent's identity for a session bound to a
                        # sub-agent that no longer resolves. This used to raise
                        # NOT_FOUND, which the module-level OmnigentError
                        # handler turned into a 404. Unlike the failure modes
                        # above (bad snapshot, no agent_id, agent not found),
                        # this one is recoverable and does not stop the
                        # session — the warning is the sole record of it.
                        # The entry is published carrying the name it failed
                        # to resolve, so the two cache-hit returns above warn
                        # for it as well instead of handing the parent out in
                        # silence for the rest of the session.
                        _warn_unresolved_sub_agent(session_id, sub_agent_name)
                        _entry_provenance = sub_agent_name
                    else:
                        workdir = _resolved_spec_workdir(spec_entry)
                        spec_entry = (
                            ResolvedSpec(spec=sub_spec, workdir=workdir)
                            if workdir is not None
                            else sub_spec
                        )
            _session_spec_cache_put(
                session_id,
                spec_entry,
                generation=_fill_generation,
                agent_tag=agent_id,
                provenance=_entry_provenance,
            )
            return spec_entry

    async def _resolve_session_agent_spec(
        session_id: str, agent_id_hint: str | None = None
    ) -> AgentSpec | None:
        entry = await _resolve_session_spec_entry(session_id, agent_id_hint)
        return _unwrap_spec_entry(entry)

    async def _resolve_session_agent_spec_or_none(
        session_id: str, agent_id_hint: str | None = None
    ) -> AgentSpec | None:
        """Resolve the session agent spec, tolerating resolution failure.

        The cursor/opencode/kimi launch arms swallow ``OmnigentError`` and
        continue without a spec; this is their spec resolver for
        ``_launch_native_terminal``.
        """
        try:
            return await _resolve_session_agent_spec(session_id, agent_id_hint=agent_id_hint)
        except OmnigentError:
            return None

    async def _resolve_session_skills(session_id: str) -> list[SkillSpec]:
        _fill_generation = _session_cache_generation(session_id)
        # Skills are derived from the resolved spec, so the TTL cache below
        # must be tagged with the same agent id _resolve_session_spec_entry
        # resolved — read that back off the spec cache entry it just wrote
        # (or hit) rather than guessing, so a stale skills list can never
        # survive an agent switch that the spec cache itself already caught.
        entry = await _resolve_session_spec_entry(session_id, _session_agent_ids.get(session_id))
        _spec_cache_entry = _session_spec_cache.get(session_id)
        _skills_agent_tag = _spec_cache_entry[0] if _spec_cache_entry is not None else None
        cached = _cache_get_for_agent(_session_skills_cache, session_id, _skills_agent_tag)
        if cached is not None:
            expires_at, cached_skills = cached
            if time.monotonic() < expires_at:
                return cached_skills
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

        def _discover() -> list[SkillSpec]:
            merged: list[SkillSpec] = [s for s in spec.skills if s.user_invocable]
            seen = {s.name for s in spec.skills}
            seen_dirs = {s.skill_dir.resolve() for s in spec.skills if s.skill_dir is not None}
            ctx = SkillSourceContext(
                roots=tuple(roots),
                home=Path.home(),
                skills_filter=spec.skills_filter,
                bundle_dir=_resolved_spec_workdir(entry),
            )
            harness = canonicalize_harness(spec.executor.harness_kind)
            for hs in resolve_harness_skills(ctx, harness):
                if hs.name in seen:
                    continue
                if hs.skill_dir is not None and hs.skill_dir.resolve() in seen_dirs:
                    continue
                seen.add(hs.name)
                if hs.skill_dir is not None:
                    seen_dirs.add(hs.skill_dir.resolve())
                merged.append(hs)
            return merged

        skills = await asyncio.to_thread(_discover)
        _session_cache_put(
            _session_skills_cache,
            session_id,
            (time.monotonic() + _SESSION_SKILLS_CACHE_TTL_SECONDS, skills),
            generation=_fill_generation,
            agent_tag=_skills_agent_tag,
        )
        return skills

    @app.get("/v1/sessions/{session_id}/skills")
    async def get_session_skills(session_id: str) -> JSONResponse:
        skills = await _resolve_session_skills(session_id)
        return JSONResponse(
            status_code=200,
            content={"skills": [{"name": s.name, "description": s.description} for s in skills]},
        )

    @app.get("/v1/sessions/{session_id}/models")
    async def get_session_models(session_id: str) -> JSONResponse:
        spec = await _resolve_session_agent_spec(
            session_id, agent_id_hint=_session_agent_ids.get(session_id)
        )
        if spec is None:
            return JSONResponse(status_code=200, content={"workers": {}})
        from omnigent.model_catalog import catalog_for_spec

        try:
            catalog = await asyncio.to_thread(catalog_for_spec, spec)
        except Exception:
            _logger.exception(
                "get_session_models: catalog_for_spec failed for session=%s", session_id
            )
            return JSONResponse(status_code=200, content={"workers": {}})
        return JSONResponse(status_code=200, content={"workers": catalog})

    @app.get("/v1/sessions/{session_id}/codex-model-options")
    async def get_session_codex_model_options(session_id: str) -> JSONResponse:
        harness = _session_harness_name(session_id)
        if harness not in ("codex-native", "opencode-native"):
            return JSONResponse(status_code=200, content={"models": []})
        if harness == "opencode-native":
            try:
                models = await _opencode_native_model_options(session_id)
                return JSONResponse(status_code=200, content={"models": models})
            except _CodexNativeModelOptionsNotReady:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_model_options_failed",
                        "detail": "OpenCode-native app-server is not ready yet.",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
                _logger.warning("OpenCode-native model list failed for %s: %s", session_id, exc)
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_model_options_failed",
                        "detail": _client_safe_error_detail(
                            exc, context="opencode-native model options"
                        ),
                    },
                )
        try:
            return JSONResponse(
                status_code=200,
                content={"models": await _codex_native_model_options(session_id)},
            )
        except _CodexNativeModelOptionsNotReady:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_model_options_failed",
                    "detail": "Codex-native model options are not ready yet.",
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface Codex app-server failures to AP.
            _logger.warning(
                "Codex-native model/list failed for session=%s",
                session_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_model_options_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native model options"),
                },
            )

    @app.get("/v1/sessions/{session_id}/kiro-model-options")
    async def get_session_kiro_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "kiro-native":
            return JSONResponse(status_code=200, content={"models": []})
        from omnigent.kiro_native import list_kiro_cli_model_options

        try:
            models = await asyncio.to_thread(list_kiro_cli_model_options)
        except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
            _logger.warning(
                "Kiro-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "kiro_native_model_options_failed",
                    "detail": _client_safe_error_detail(exc, context="kiro-native model options"),
                },
            )
        return JSONResponse(status_code=200, content={"models": models})

    @app.get("/v1/sessions/{session_id}/cursor-model-options")
    async def get_session_cursor_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "cursor-native":
            return JSONResponse(status_code=200, content={"models": []})
        from omnigent.cursor_native import list_cursor_cli_model_options

        # Captured before the discovery await: a reset or agent switch landing
        # while it runs must drop this mapping rather than republish it over
        # the cleared state, since model confirmation reads it later.
        _model_names_generation = _session_cache_generation(session_id)
        try:
            models = await asyncio.to_thread(list_cursor_cli_model_options)
        except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
            _logger.warning(
                "Cursor-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_model_options_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="cursor-native model options"
                    ),
                },
            )
        _session_cache_put(
            _session_cursor_model_names,
            session_id,
            {
                str(option["id"]): str(option["displayName"])
                for option in models
                if option.get("id") and option.get("displayName")
            },
            generation=_model_names_generation,
        )
        return JSONResponse(status_code=200, content={"models": models})

    @app.get("/v1/sessions/{session_id}/claude-model-options")
    async def get_session_claude_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "claude-native":
            return JSONResponse(status_code=200, content={"models": []})
        try:
            claude_config = await _resolve_session_claude_launch_config(session_id)
        except click.ClickException as exc:
            _logger.warning(
                "Claude-native model options unavailable for session=%s: %s",
                session_id,
                exc.message,
            )
            return JSONResponse(
                status_code=424,
                content={
                    "error": "claude_native_model_options_config",
                    "detail": exc.message,
                },
            )
        except Exception as exc:  # noqa: BLE001 — retryable model-options failure
            _logger.warning(
                "Claude-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_options_failed",
                    "detail": _client_safe_error_detail(
                        exc,
                        context="claude-native model options",
                    ),
                },
            )
        from omnigent.claude_native import claude_native_model_options

        return JSONResponse(
            status_code=200,
            content={"models": claude_native_model_options(claude_config)},
        )

    @app.post("/v1/sessions/{session_id}/skills/resolve")
    async def resolve_session_skill(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "Request body must be JSON."},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Request body must be a JSON object.",
                },
            )
        name = body.get("name")
        arguments = body.get("arguments", "")
        if not isinstance(name, str) or not name:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'name' is required."},
            )
        if not isinstance(arguments, str):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'arguments' must be a string."},
            )
        skills = await _resolve_session_skills(session_id)
        skill = find_skill_by_name(skills, name)
        if skill is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "skill_not_found",
                    "detail": (f"Skill {name!r} not found for session {session_id!r}."),
                    "available": sorted(s.name for s in skills),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"meta_text": format_skill_meta_text(skill, arguments)},
        )

    async def _fs_list_or_read(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(
            session_id, agent_id_hint=_session_agent_ids.get(session_id)
        )
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )

        fs = CallerProcessFilesystem(env)
        resolved = fs._resolve(path)

        if resolved.is_dir():
            page = await fs.list_dir(
                path,
                limit=limit,
                after=after,
                before=before,
                order=order,
            )
            data = [_fs_entry_to_dict(e) for e in page.data]
            return JSONResponse(
                status_code=200,
                content={
                    "object": "list",
                    "data": data,
                    "first_id": page.first_id,
                    "last_id": page.last_id,
                    "has_more": page.has_more,
                },
            )

        content = await fs.read(path)
        content_type_guess, _ = mimetypes.guess_type(path)
        payload: dict[str, object] = {
            "object": "session.environment.filesystem.file_content",
            "path": content.path,
            "content_type": content_type_guess,
            "bytes": content.bytes,
            "truncated": content.truncated,
        }
        if content.encoding:
            payload["encoding"] = content.encoding
            payload["content"] = content.data.decode(content.encoding)
        else:
            import base64

            payload["encoding"] = "base64"
            payload["content"] = base64.b64encode(content.data).decode()
        return JSONResponse(status_code=200, content=payload)

    def _fs_entry_to_dict(entry: FilesystemEntry) -> dict[str, object]:
        return {
            "id": entry.id,
            "object": "session.environment.filesystem.entry",
            "name": entry.name,
            "path": entry.path,
            "type": entry.type,
            "bytes": entry.bytes,
            "modified_at": entry.modified_at,
        }

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
        # Bump FIRST: any fill already in flight captured the previous
        # generation and must lose its write, including one that completes
        # between here and the last pop below.
        _session_cache_generations[session_id] = _session_cache_generations.get(session_id, 0) + 1
        _session_spec_cache.pop(session_id, None)
        # Popped with the spec it describes: provenance outliving its entry
        # would warn about a name the next cached spec never failed on.
        _session_sub_agent_fallbacks.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_cursor_model_names.pop(session_id, None)
        _drop_session_claude_launch_config(session_id)
        _session_tool_schemas.pop(session_id, None)
        _session_mcp_spec_hash.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        # The comment relay is agent-derived too: its advertised tool set is
        # built from the resolved spec (build_native_relay_tool_schemas), and
        # native harnesses ignore the wire `tools` list, so the relay IS their
        # whole tool surface. Left running across a switch it would keep
        # serving the previous agent's gated tools; the relay is untagged and
        # presence-gated, so it must be closed and evicted here for the next
        # _ensure_comment_relay_started to rebuild it from the new spec.
        if (_relay := _session_comment_relays.pop(session_id, None)) is not None:
            with contextlib.suppress(OSError, RuntimeError):
                _relay.close()
        # The session-init envelope is agent-derived: it carries the agent id
        # it was created for and the session labels chosen for it (bridge id
        # among them), and _fresh_session_init_envelope hands those straight
        # to native spawn/bridge lookups as optional_labels for its whole TTL.
        # Surviving a switch, it points the NEW agent at the PREVIOUS agent's
        # bridge.
        _session_init_envelopes.pop(session_id, None)
        # The tagged caches above are self-contained (their own eviction
        # drops the provenance tag with them), but _session_agent_ids is a
        # separate "what did we last dispatch as" fact some callers still
        # read to pick which agent's tag to look up next — leaving it
        # pointing at the agent whose caches were JUST cleared would let a
        # later, unrelated cache write for that same agent look validly
        # "current" again. Clear it too so the next read starts unknown.
        _session_agent_ids.pop(session_id, None)
        if agent_id:
            # Global, agent-keyed: this pop is visible to every conversation,
            # but the generation bump above is not — it belongs to THIS session
            # only. So another conversation with a fill for this agent already
            # in flight can reinstate the entry right after this pop, and its
            # write will be judged current because its own session never reset.
            # Known deferred gap; see _agent_spec_cache_put.
            _spec_cache.pop(agent_id, None)

    async def _invalidate_session_agent_state(session_id: str, new_agent_id: str | None) -> None:
        """Invalidate every agent-derived CACHE and release the cached
        harness subprocess for *session_id*, on an in-conversation agent
        switch.

        Scope: the per-session value caches listed in
        :func:`_clear_session_agent_caches` plus the harness subprocess. It
        does NOT fence or tear down agent-derived RESOURCES — terminals,
        forwarders, and bridge state outlive this call, and a creator already
        in flight can still register one afterwards; that is not
        transactional.

        The single shared routine both dispatch paths (background
        ``_run_turn_bg_setup_and_stream`` and direct-stream
        ``_stream_message_to_harness``) call for this, so their eviction
        scope cannot silently diverge.

        Releasing the subprocess matters as much as clearing the specs:
        ``process_manager.get_client()`` reuses an existing cached
        subprocess entry regardless of a new env/config on that call — it
        only spawns fresh when no entry exists — so a same-harness agent
        switch would otherwise keep serving the PREVIOUS agent's
        process-start-seeded config (claude-sdk's SDK options, Hermes's
        instruction-prefix-once-per-vendor-session) even after every
        spec/skills/schema cache is correctly invalidated.

        :param session_id: Conversation id whose agent-derived state is
            no longer trustworthy.
        :param new_agent_id: The agent id this session is now believed to
            be dispatching as, if known — passed through to
            :func:`_clear_session_agent_caches` so the agent-keyed
            ``_spec_cache`` entry for it is dropped too, not just the
            session-keyed caches. Dropping it is not the same as fencing it:
            that cache is global, so another conversation's in-flight fill for
            the same agent can reinstate the entry right afterwards. See
            :func:`_agent_spec_cache_put`.
        :returns: None.
        """
        _clear_session_agent_caches(session_id, new_agent_id)
        if process_manager is not None:
            await process_manager.release(session_id)

    @app.delete("/v1/sessions/{session_id}/resources")
    async def cleanup_session_resources(
        session_id: str,
    ) -> JSONResponse:
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

    @app.post("/v1/sessions/{session_id}/mcp/execute")
    async def mcp_execute(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": {"code": -32700, "message": "Parse error: invalid JSON"}},
            )
        method: str = body.get("method") or ""
        params: _JsonObject = body.get("params") or {}

        if method == "tools/list":
            if mcp_manager is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": -32000,
                            "message": "Runner MCP manager not configured",
                        }
                    },
                )
            agent_id = _session_agent_ids.get(session_id)
            spec_entry = _cache_get_for_agent(_session_spec_cache, session_id, agent_id)
            spec = _unwrap_resolved_spec(spec_entry)
            if spec is None and spec_resolver is not None:
                if agent_id:
                    try:
                        resolved = await spec_resolver(agent_id, session_id)
                        spec = _unwrap_resolved_spec(resolved)
                    except Exception:  # noqa: BLE001
                        pass
            if spec is None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": f"No spec available for session {session_id!r}",
                        }
                    },
                )
            try:
                result = await mcp_manager.schemas_for(spec)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": _client_safe_error_detail(exc, context="MCP tool dispatch"),
                        }
                    },
                )
            return JSONResponse(
                content={
                    "result": {
                        "schemas": result.schemas,
                        "tool_names": list(result.tool_names),
                        "failures": result.failures,
                    }
                }
            )

        if method == "tools/call":
            import json as _json

            from omnigent.runner.tool_dispatch import execute_tool

            tool_name = cast(str, params.get("name") or "")
            arguments = cast(_JsonObject, params.get("arguments") or {})
            input_responses = cast(_JsonObject | None, params.get("inputResponses"))
            request_state = cast(str | None, params.get("requestState"))
            if not tool_name:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": "Missing tool name"}},
                )

            if "__" in tool_name:
                if mcp_manager is None:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "code": -32000,
                                "message": "Runner MCP manager not configured",
                            }
                        },
                    )
                _agent_id = _session_agent_ids.get(session_id)
                spec_entry = _cache_get_for_agent(_session_spec_cache, session_id, _agent_id)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    if _agent_id:
                        try:
                            resolved = await spec_resolver(_agent_id, session_id)
                            spec = _unwrap_resolved_spec(resolved)
                        except Exception:  # noqa: BLE001
                            pass
                if spec is None:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": f"No spec available for session {session_id!r}",
                            }
                        },
                    )
                from omnigent.tools.mcp import McpElicitationRequired

                try:
                    if input_responses is not None:
                        route = mcp_manager._resolve_tool_route(spec, tool_name)
                        if route is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {tool_name!r}"
                            )
                        owning, bare_tool = route
                        if owning.connection is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {tool_name!r}"
                            )
                        output = await owning.connection.call_tool_with_elicitation(
                            bare_tool,
                            arguments,
                            input_responses=input_responses,
                            request_state=request_state,
                        )
                    else:
                        output = await mcp_manager.call_tool(
                            spec,
                            tool_name,
                            arguments,
                            session_id=session_id,
                        )
                except McpElicitationRequired as elicit:
                    return JSONResponse(
                        content={
                            "result": {
                                "input_required": {
                                    "inputRequests": elicit.input_requests,
                                    "requestState": elicit.request_state,
                                },
                            },
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            else:
                _agent_id_local = _session_agent_ids.get(session_id)
                spec_entry = _cache_get_for_agent(_session_spec_cache, session_id, _agent_id_local)
                spec_workdir = _resolved_spec_workdir(spec_entry)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    _agent_id = _agent_id_local
                    if _agent_id:
                        try:
                            resolved = await spec_resolver(_agent_id, session_id)
                            spec_workdir = _resolved_spec_workdir(resolved)
                            spec = _unwrap_resolved_spec(resolved)
                        except Exception:  # noqa: BLE001
                            pass
                dispatch_workspace = (
                    spec_workdir
                    if spec_workdir is not None
                    and _is_spec_local_native_python_tool(spec, tool_name)
                    else runner_workspace
                )
                try:
                    output = await execute_tool(
                        tool_name=tool_name,
                        arguments=_json.dumps(arguments),
                        server_client=server_client,
                        terminal_registry=terminal_registry,
                        resource_registry=resource_registry,
                        agent_spec=spec,
                        conversation_id=session_id,
                        task_id=session_id,
                        agent_id=_agent_id_local,
                        agent_name=getattr(spec, "name", None),
                        runner_workspace=dispatch_workspace,
                        mcp_manager=None,
                        session_inbox=_session_inboxes.get(session_id),
                        session_async_tasks=_session_async_tasks.get(session_id),
                        harness_client=None,
                        publish_event=_publish_event,
                        filesystem_registry=filesystem_registry,
                    )
                except Exception as exc:  # noqa: BLE001
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            return JSONResponse(content={"result": {"output": output}})

        return JSONResponse(
            status_code=200,
            content={"error": {"code": -32601, "message": f"Method not found: {method!r}"}},
        )

    def _resolve_summarize_connection(
        session_id: str,
        model: str,
    ) -> dict[str, str] | None:
        from omnigent.spec.types import ApiKeyAuth, DatabricksAuth, ProviderAuth

        spec_entry = _cache_get_for_agent(
            _session_spec_cache, session_id, _session_agent_ids.get(session_id)
        )
        if spec_entry is None:
            return None
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        if spec is None:
            return None

        auth = getattr(spec.executor, "auth", None)

        if isinstance(auth, ProviderAuth):
            return _resolve_provider_connection(auth.name, model)

        if isinstance(auth, DatabricksAuth):
            return _resolve_databricks_connection(auth.profile, session_id)

        if isinstance(auth, ApiKeyAuth):
            conn: dict[str, str] = {"api_key": auth.api_key}
            if auth.base_url:
                conn["base_url"] = auth.base_url
            return conn

        _spec_has_legacy_profile = bool(
            spec.executor.profile or (spec.executor.config or {}).get("profile")
        )
        if auth is None and not _spec_has_legacy_profile:
            from omnigent.runtime.workflow import _load_global_auth

            global_auth = _load_global_auth()
            if isinstance(global_auth, DatabricksAuth):
                return _resolve_databricks_connection(global_auth.profile, session_id)
            if isinstance(global_auth, ApiKeyAuth):
                conn = {"api_key": global_auth.api_key}
                if global_auth.base_url:
                    conn["base_url"] = global_auth.base_url
                return conn

        if model.startswith(("databricks/", "databricks-")):
            _db_profile = (
                spec.executor.profile or (spec.executor.config or {}).get("profile") or "DEFAULT"
            )
            return _resolve_databricks_connection(_db_profile, session_id)

        return None

    def _resolve_provider_connection(
        provider_name: str,
        model: str = "",
    ) -> dict[str, str] | None:
        try:
            from omnigent.onboarding.detected import effective_config_with_detected
            from omnigent.onboarding.provider_config import (
                load_config,
                load_providers,
            )

            config = load_config()
            providers = load_providers(effective_config_with_detected(config))
            entry = providers.get(provider_name)
            if entry is None:
                return None
            if entry.kind == "databricks" and entry.profile:
                return _resolve_databricks_connection(entry.profile, provider_name)
            _is_anthropic = model.startswith(("anthropic/", "claude"))
            _preferred = "anthropic" if _is_anthropic else "openai"
            _fallback = "openai" if _is_anthropic else "anthropic"
            family = entry.family(_preferred) or entry.family(_fallback)
            if family is None:
                return None
            conn: dict[str, str] = {}
            if family.api_key:
                conn["api_key"] = family.api_key
            if family.base_url:
                conn["base_url"] = family.base_url
            return conn or None
        except Exception:  # noqa: BLE001
            _logger.warning(
                "/v1/summarize: failed to resolve provider %r",
                provider_name,
                exc_info=True,
            )
            return None

    def _resolve_databricks_connection(
        profile: str,
        context: str,
    ) -> dict[str, str] | None:
        from omnigent.runtime.credentials.databricks import (
            resolve_databricks_workspace,
        )

        try:
            creds = resolve_databricks_workspace(profile)
        except OSError:
            _logger.warning(
                "/v1/summarize: failed to resolve Databricks profile %r (context=%s)",
                profile,
                context,
                exc_info=True,
            )
            return None
        return {
            "base_url": creds.host.rstrip("/") + "/serving-endpoints",
            "api_key": creds.token,
        }

    @app.post("/v1/summarize")
    async def summarize(request: Request) -> JSONResponse:
        body = await request.json()
        messages = body.get("messages")
        model = body.get("model")
        if not isinstance(messages, list) or not model:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'messages' (list) and 'model' (str) are required",
                    }
                },
            )
        connection: dict[str, str] | None = body.get("connection") or None
        if connection is None:
            session_id: str | None = body.get("session_id")
            if session_id is not None:
                connection = _resolve_summarize_connection(
                    session_id,
                    model,
                )
        llm_client = _get_runner_llm_client()
        resp = await llm_client.responses.create(
            model=model,
            input=build_summarization_input(messages),
            instructions=build_summarization_prompt(messages),
            tools=[],
            connection_params=connection,
        )
        summary_text = extract_summary_text(resp)
        import tiktoken

        bare = model.split("/", 1)[-1] if "/" in model else model
        try:
            enc = tiktoken.encoding_for_model(bare)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(summary_text))
        return JSONResponse(content={"text": summary_text, "token_count": token_count})

    @app.post("/v1/elicitations/{elicitation_id}")
    async def elicitation(elicitation_id: str, request: Request) -> Response:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={"error": "not_implemented", "detail": "Runner not configured"},
            )
        body = await request.json()
        response_id = body.get("response_id")
        if not response_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "response_id required in elicitation body",
                },
            )
        conv_id = await _resolve_conversation_id(response_id)
        if conv_id is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": f"Cannot resolve response {response_id}"},
            )
        try:
            client = await process_manager.get_client(conv_id, "any")
        except NoLiveHarnessError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no_live_harness",
                    "detail": "no harness subprocess is running for this conversation",
                },
            )
        try:
            event_body = {
                "type": "approval",
                "elicitation_id": elicitation_id,
                "action": body.get("action"),
            }
            if body.get("content") is not None:
                event_body["content"] = body["content"]
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json=event_body,
                timeout=30.0,
            )
            return _forward_harness_response(resp)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={
                    "error": "elicitation_failed",
                    "detail": _client_safe_error_detail(exc, context="elicitation forward"),
                },
            )

    async def _catch_up_scan() -> None:
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
                    _active_turns[session_id] = None
                    _publish_turn_status(session_id, "running")
                    agent_id = _session_agent_ids.get(session_id)
                    msg_body: _JsonObject = {
                        "agent_id": agent_id,
                        "model": agent_id or "",
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
        from omnigent.native_cost_popup import _list_tmux_clients
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event
        from omnigent.terminals.pane_reaper import NativePaneReaper, PaneRef

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
            clients = await asyncio.to_thread(_list_tmux_clients, str(pane.socket_path), "main")
            return bool(clients)

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
    server_client = httpx.AsyncClient(
        base_url=server_url,
        timeout=httpx.Timeout(5.0, read=None),
    )
    return create_runner_app(server_client=server_client)


async def _resolve_effective_turn(
    *,
    agent_id: str | None,
    spec_resolver: SpecResolver | None,
    session_id: str | None = None,
    model_override: str | None = None,
    harness_override: str | None = None,
    sub_agent_name: str | None = None,
    cwd: Path | None = None,
) -> tuple[str, dict[str, str] | None, Any, Path | None]:
    """Resolve harness, spawn-env, effective ``AgentSpec``, and workdir together.

    Despite the name, this is NOT the single resolution path every turn goes
    through — three genuinely different code paths compute the effective
    spec/harness for a turn, and only ONE of them calls this function:

    - The direct ``?stream=true`` bypass, when no harness is already known
      (``_stream_message_to_harness``'s ``if not harness_name:`` branch) —
      calls this function directly.
    - The direct ``?stream=true`` bypass, when a harness IS already known
      (dispatch from the background path, or caller-supplied in the body) —
      does its OWN inline cache read + resolver call + sub-agent swap,
      entirely separate from this function (see the ``else:`` branch
      immediately below the branch above).
    - The background-turn path (``_run_turn_bg_setup_and_stream``) — also
      does its OWN inline cache read + resolver call + sub-agent swap,
      structurally similar to but independent from both of the above.

    All three converge only on the OUTPUT contract (a harness name, a
    spawn_env, and — separately, in the caller-known-harness branch and the
    background path — an effective spec used for
    ``InstructionComposition``), not on a single resolution call. They are not
    unified because they differ in what's already known at entry: no harness
    at all here, a known harness/dispatch elsewhere, and a persistent
    ``_session_spec_cache`` with its own agent-switch/provenance
    invalidation rules in the other two.

    The differing FAILURE behaviour is deliberate, not drift. This path
    answers synchronously, so a resolver EXCEPTION becomes an HTTP 503 here,
    where the other two cannot report that way.

    A missing requested child is not one of those differences: every path
    warns and continues on the parent spec, so this one derives harness and
    spawn_env from the parent and returns them as the turn's effective
    result. It used to raise, which the route answered with that same 503.
    Unlike the other two, this path holds no session spec cache and resolves
    afresh every turn, so its warning repeats without needing a record of
    the earlier miss.

    :func:`_resolve_harness_config` wraps this for callers that only need the
    2-tuple.

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
        the parent spec is swapped to the matching sub-spec via
        :func:`_find_spec_by_name` before harness derivation. ``None`` for
        top-level sessions.
    :param cwd: Runtime working directory for harnesses that need it.
    :returns: ``(harness, spawn_env, spec, workdir)``; ``spec``/``workdir``
        are ``None`` for unresolved specs (matches the harness/spawn_env
        default-for-unresolved-specs fallback).
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
            if sub_agent_name:
                from omnigent.runtime.workflow import _find_spec_by_name

                sub_spec = _find_spec_by_name(spec, sub_agent_name)
                if sub_spec is None:
                    # The swap is skipped, so harness and spawn_env below are
                    # derived from the PARENT spec and returned as this turn's
                    # effective result. This used to raise RuntimeError, which
                    # the direct-stream no-harness path answered with a 503
                    # spec_resolver_failed. The warning is the only trace.
                    _warn_unresolved_sub_agent(session_id, sub_agent_name)
                else:
                    spec = sub_spec
            harness = harness_override or spec.executor.config.get("harness") or spec.executor.type
            harness = canonicalize_harness(harness) or harness
            spawn_env = _build_spawn_env_from_spec(
                spec, harness, cwd=cwd, workdir=workdir, model_override=model_override
            )
            return harness, spawn_env, spec, workdir

    # Fallback for tests that register a custom harness in _HARNESS_MODULES.
    return "runner-test-default", None, None, None


async def _resolve_harness_config(
    *,
    agent_id: str | None,
    spec_resolver: SpecResolver | None,
    session_id: str | None = None,
    model_override: str | None = None,
    harness_override: str | None = None,
    sub_agent_name: str | None = None,
    cwd: Path | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Resolve harness type + spawn-env from the agent spec.

    Thin 2-tuple wrapper over :func:`_resolve_effective_turn` for callers
    that don't need the resolved spec/workdir.

    :param agent_id: Agent id to resolve the spec for.
    :param spec_resolver: Resolver that returns the spec for *agent_id*.
    :param session_id: Session/conversation id, threaded to the resolver.
    :param model_override: Per-session ``/model`` override, applied to the
        spawn-env model so it takes effect on the SDK harnesses.
    :param harness_override: Per-session brain-harness override (validated
        at session create, forwarded by the server in the message body),
        e.g. ``"pi"``. Replaces the spec's ``executor.config.harness``.
    :param sub_agent_name: For a sub-agent session, the dispatched
        sub-agent's name. See :func:`_resolve_effective_turn`.
    :param cwd: Runtime working directory for harnesses that need it.
    :returns: ``(harness, spawn_env)``; a default for unresolved specs.
    """
    harness, spawn_env, _spec, _workdir = await _resolve_effective_turn(
        agent_id=agent_id,
        spec_resolver=spec_resolver,
        session_id=session_id,
        model_override=model_override,
        harness_override=harness_override,
        sub_agent_name=sub_agent_name,
        cwd=cwd,
    )
    return harness, spawn_env


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


def _build_spawn_env_from_spec(
    spec: AgentSpec,
    harness: str,
    *,
    cwd: Path | None = None,
    workdir: Path | None = None,
    model_override: str | None = None,
) -> dict[str, str] | None:
    """Build spawn-env from spec — mirrors workflow.py's helpers.

    :param spec: The resolved agent spec.
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :param cwd: Runtime working directory for harnesses that need it.
    :param workdir: Bundle workdir, threaded to the builders.
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
    # the concrete agent's slug is read from the spec by ``_build_acp_spawn_env``.
    harness = canonicalize_harness(harness) or harness
    effective_spec = spec
    if model_override is not None:
        executor = getattr(spec, "executor", None)
        if hasattr(spec, "model_copy") and hasattr(executor, "model_copy"):
            copied_executor = cast(_ModelCopyValue, executor).model_copy(
                update={"model": model_override}
            )
            effective_spec = cast(
                AgentSpec,
                cast(_ModelCopyValue, spec).model_copy(update={"executor": copied_executor}),
            )
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
            _build_kimi_spawn_env,
            _build_openai_agents_sdk_spawn_env,
            _build_pi_spawn_env,
            _build_qwen_spawn_env,
        )

        if harness == "claude-sdk":
            env = _build_claude_sdk_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "codex":
            env = _build_codex_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
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
        elif harness == "qwen":
            env = _build_qwen_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "goose":
            env = _build_goose_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "acp":
            env = _build_acp_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "copilot":
            env = _build_copilot_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness in ACP_CLI_HARNESSES:
            # Builtin ACP CLI harnesses (one catalog row each) share a single
            # builder; the row supplies the command, label, and install info.
            env = _build_acp_cli_spawn_env(
                effective_spec, harness=harness, cwd=cwd, workdir=workdir
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
            env.get(f"{prefix}_GATEWAY_BASE_URL"),
            env.get(f"{prefix}_DATABRICKS_PROFILE"),
            env.get(_HARNESS_MODEL_ENV_KEY.get(harness, f"{prefix}_MODEL")),
        )
    return env


# ── Agent-start policy gate ────────────────────────────────────────────


async def _evaluate_agent_start_gate(
    spec: AgentSpec,
    harness: str,
) -> PolicyVerdict | None:
    """Evaluate ``__agent_start`` through the spec's policy gate.

    Constructs a :class:`RunnerToolPolicyGate` from the spec and
    evaluates a synthetic ``__agent_start`` tool call.  This reuses
    the same gate that guards MCP tool calls — no round-trip to the
    Omnigent server required.

    :param spec: The resolved agent spec (``AgentSpec``).
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :returns: A :class:`PolicyVerdict` if the spec has guardrails
        policies, ``None`` if no policies apply.
    """
    from omnigent.runner.policy import RunnerToolPolicyGate

    gate = RunnerToolPolicyGate.from_spec(spec)
    if gate.is_empty:
        return None

    sandbox_dict: _JsonObject | None = None
    if spec.os_env is not None and spec.os_env.sandbox is not None:
        sandbox_dict = cast(_JsonObject, dataclasses.asdict(spec.os_env.sandbox))

    return await gate.evaluate_tool_call(
        "sys_agent_start",
        {
            "agent_name": getattr(spec, "name", None) or "",
            "harness": harness,
            "sandbox": sandbox_dict,
        },
    )


def _apply_sandbox_override_from_verdict(
    spec: AgentSpec,
    verdict_data: object,
) -> None:
    """Apply sandbox override from a policy verdict's ``data`` field.

    The ``enforce_sandbox`` policy returns replacement ``data`` shaped
    as ``{"name": "sys_agent_start", "arguments": {"sandbox": {...}}}``.
    This extracts the ``sandbox`` dict and mutates ``spec.os_env``
    in-place.

    :param spec: The agent spec (``AgentSpec``) — mutated in-place.
    :param verdict_data: The ``PolicyVerdict.data`` payload, expected
        to be a dict with ``arguments.sandbox``.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    if not isinstance(verdict_data, Mapping):
        return
    args = verdict_data.get("arguments")
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
