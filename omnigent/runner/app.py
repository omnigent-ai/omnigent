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
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.codex_native_app_server import CodexAppServerClient
    from omnigent.runner.cost_advisor import AdvisorTurnResult
    from omnigent.terminals.registry import TerminalListEntry

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from omnigent._platform import IS_WINDOWS
from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.model_override import validate_model_override
from omnigent.policies.types import FAIL_CLOSED_PHASES
from omnigent.runner import pending_approvals
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    ANTIGRAVITY_NATIVE_TERMINAL_ROLE,
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    CURSOR_NATIVE_TERMINAL_ROLE,
    GOOSE_NATIVE_TERMINAL_ROLE,
    HERMES_NATIVE_TERMINAL_ROLE,
    KIMI_NATIVE_TERMINAL_ROLE,
    KIRO_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    OPENCODE_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    QWEN_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
)
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)

_logger = logging.getLogger(__name__)


# ── session.status "waiting" backwards-compat (new runner ↔ old server) ──
# The runner emits ``session.status: "waiting"`` when a turn ends with sub-agents
# still running (PR #930, for the headless ``-p`` fast-exit). Servers older than
# 0.3.0 don't model "waiting" — their ``SessionResponse.status`` is
# ``Literal["idle","running","failed"]`` — and 500 on ``GET /v1/sessions`` when
# they try to serialize the cached value. So we resolve the server version once
# (``_get_server_version``) and, when publishing status, downgrade
# "waiting"→"running" unless that version supports it
# (``_version_supports_waiting_status``). An unknown version — unprobed or a
# probe failure — downgrades too, so an old server is never 500'd.
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
    from packaging.version import Version

    return Version(server_version).release >= Version(_WAITING_STATUS_MIN_SERVER_VERSION).release


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


SpecResolver = Callable[[str, str | None], Awaitable[Any | None]]
_NO_BODY_STATUS_CODES = {204, 304}
_SUBAGENT_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SUBAGENT_DELIVERY_DELIVERED = "delivered"
_SUBAGENT_DELIVERY_ALREADY_DELIVERED = "already_delivered"
_SUBAGENT_DELIVERY_UNTRACKED = "untracked"
_SUBAGENT_DELIVERY_MISSING_WORK_ENTRY = "missing_work_entry"
_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX = "missing_parent_inbox"
_NATIVE_TERMINAL_START_FAILED_CODE = "native_terminal_start_failed"
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
# Terminal resource hosting the framework's own TUI (the Omnigent REPL,
# ``omnigent attach``) for runner-hosted SDK sessions — the SDK mirror of
# the claude-/codex-native embedded terminals. Resource id derives as
# ``terminal_tui_main`` (see ``terminal_resource_id``).
_REPL_TERMINAL_NAME = "tui"
_REPL_TERMINAL_SESSION_KEY = "main"

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
# /v1/summarize endpoint is optional). Typed as Any to avoid a circular
# import between runner and llms.
_runner_llm_client: Any | None = None  # llms.Client


def _get_runner_llm_client() -> Any:
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


def _publish_tmux_target_for_bridge(
    *,
    resource_registry: SessionResourceRegistry,
    session_id: str,
    bridge_id: str,
    terminal_name: str,
    session_key: str,
) -> None:
    """
    Advertise a launched terminal's tmux target to a bridge directory.

    Called from the terminal-launch POST when the caller opts in via
    truthy ``bridge_inject_dir`` in the body. The destination path is
    derived from a server-side bridge id, so a caller can't redirect
    the write.

    The ``claude-native`` harness reads ``tmux.json`` from the derived
    directory and shells out to ``tmux -S <socket> send-keys``. No-op
    if the registry has no live instance for the triple.

    :param resource_registry: Session resource registry that exposes
        the underlying terminal registry.
    :param session_id: Owning session/conversation id.
    :param bridge_id: Opaque bridge id from the session label, e.g.
        ``"bridge_abc123"``.
    :param terminal_name: Terminal spec name, e.g. ``"claude"``.
    :param session_key: Session key, e.g. ``"main"``.
    :returns: None.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return
    instance = terminal_registry.get(session_id, terminal_name, session_key)
    if instance is None or not instance.running:
        return
    # Imported here to avoid pulling Claude-native specifics into the
    # generic runner module's import-time graph.
    from omnigent.claude_native_bridge import bridge_dir_for_bridge_id, write_tmux_target

    write_tmux_target(
        bridge_dir_for_bridge_id(bridge_id),
        socket_path=instance.socket_path,
        tmux_target=instance.tmux_target,
    )


# Background transcript-forwarder tasks for host-spawned claude-native and
# codex-native runners, keyed by session id: strong references so they aren't
# garbage-collected mid-run, and the handle for cancelling a session's previous
# forwarder on terminal re-create (else both mirror, double-posting items).
_AUTO_FORWARDER_TASKS: dict[str, asyncio.Task[Any]] = {}

# Bound how long terminal (re)creation waits for a cancelled forwarder.
_AUTO_FORWARDER_CANCEL_TIMEOUT_S = 10.0


class _CodexNativeModelOptionsNotReady(RuntimeError):
    """Raised when Codex model options are requested before bridge startup."""


async def _cancel_auto_forwarder_task(session_id: str) -> None:
    """
    Cancel and await the session's registered transcript forwarder, if any.

    Native terminal (re)creation calls this before wiping the bridge's
    forward-cursor state: the claude forwarder is restart-forever and tails
    the transcript file across pane death, so without an explicit cancel
    the surviving task keeps mirroring alongside the newly spawned one and
    every post-recovery record is persisted twice (the server has no dedup
    for external conversation items).

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: None.
    """
    task = _AUTO_FORWARDER_TASKS.pop(session_id, None)
    if task is None or task.done():
        return
    task.cancel()
    # asyncio.wait absorbs the CancelledError and bounds the wait on a hung cancellation.
    _done, pending = await asyncio.wait({task}, timeout=_AUTO_FORWARDER_CANCEL_TIMEOUT_S)
    if pending:
        _logger.warning(
            "Cancelled transcript forwarder for %s did not finish within %.0fs",
            session_id,
            _AUTO_FORWARDER_CANCEL_TIMEOUT_S,
        )


def _register_auto_forwarder_task(session_id: str, task: asyncio.Task[Any]) -> None:
    """
    Register a session's transcript-forwarder task in the keyed registry.

    Keeps a strong reference so the task isn't garbage-collected mid-run.
    If a different live task already occupies the slot (a concurrent
    create that slipped past :func:`_cancel_auto_forwarder_task`), it is
    cancelled so a session never runs two forwarders at once.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param task: Freshly created forwarder task for this session.
    :returns: None.
    """
    incumbent = _AUTO_FORWARDER_TASKS.get(session_id)
    if incumbent is not None and incumbent is not task:
        incumbent.cancel()
    _AUTO_FORWARDER_TASKS[session_id] = task

    def _evict(done_task: asyncio.Task[Any]) -> None:
        """Drop the registry entry unless a successor already replaced it."""
        if _AUTO_FORWARDER_TASKS.get(session_id) is done_task:
            del _AUTO_FORWARDER_TASKS[session_id]

    task.add_done_callback(_evict)


# Background tasks that re-pop a still-pending cost-budget approval on a
# terminal client that attaches after the ASK fired. Kept referenced so
# they aren't garbage-collected before they run.
_COST_POPUP_REPOP_TASKS: set[asyncio.Task[Any]] = set()

# Background Codex app-server instances for host-spawned codex-native
# runners, kept referenced so they aren't garbage-collected mid-run.
_AUTO_CODEX_APP_SERVERS: dict[str, Any] = {}

# Background OpenCode ``opencode serve`` instances for host-spawned
# opencode-native runners, kept referenced so they aren't garbage-collected
# mid-run (mirrors ``_AUTO_CODEX_APP_SERVERS``).
_AUTO_OPENCODE_SERVERS: dict[str, Any] = {}

# Bound repeated terminal GET miss logs from tight client poll loops.
_TERMINAL_LOOKUP_MISS_LOG_INTERVAL_S = 10.0
_terminal_lookup_miss_log_state: dict[tuple[str, str, str], float] = {}


def _terminal_lookup_miss_reason(
    resource_registry: SessionResourceRegistry,
    session_id: str,
    terminal_id: str,
) -> str:
    """
    Explain why a terminal resource lookup returned ``None``.

    Used only for runner diagnostics after
    :meth:`SessionResourceRegistry.get_terminal_resource` has already
    performed the authoritative lookup and tmux liveness probe. The helper
    inspects in-memory registry state without running another tmux command,
    so the log line distinguishes absent resources from terminals that were
    registered but are now marked stopped.

    :param resource_registry: Runner resource registry for the session.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :returns: Short reason string for logs.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return "terminal_registry_missing"
    entries = terminal_registry.list_for_conversation(session_id)
    if not entries:
        return "session_has_no_registered_terminals"
    registered_ids = [
        terminal_resource_id(entry.terminal_name, entry.session_key) for entry in entries
    ]
    for entry in entries:
        if terminal_resource_id(entry.terminal_name, entry.session_key) != terminal_id:
            continue
        if not entry.instance.running:
            return (
                "terminal_registered_but_not_running "
                f"name={entry.terminal_name!r} session_key={entry.session_key!r} "
                f"socket={entry.instance.socket_path}"
            )
        return (
            "terminal_registered_but_liveness_probe_failed "
            f"name={entry.terminal_name!r} session_key={entry.session_key!r} "
            f"socket={entry.instance.socket_path}"
        )
    return f"terminal_id_not_registered registered_ids={registered_ids!r}"


def _log_terminal_lookup_miss(
    resource_registry: SessionResourceRegistry,
    session_id: str,
    terminal_id: str,
) -> None:
    """
    Log a throttled terminal lookup miss diagnostic.

    Claude/Codex wrapper clients poll terminal GET endpoints while a runner
    starts. Without throttling, an INFO log per poll would flood the runner
    log for the full startup timeout. This emits immediately for each new
    reason and then at most once per interval while the reason persists.

    :param resource_registry: Runner resource registry for the session.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param terminal_id: Terminal resource id, e.g.
        ``"terminal_claude_main"``.
    :returns: None.
    """
    reason = _terminal_lookup_miss_reason(resource_registry, session_id, terminal_id)
    now = time.monotonic()
    key = (session_id, terminal_id, reason)
    last = _terminal_lookup_miss_log_state.get(key)
    if last is not None and now - last < _TERMINAL_LOOKUP_MISS_LOG_INTERVAL_S:
        return
    _terminal_lookup_miss_log_state[key] = now
    _logger.info(
        "Terminal resource lookup miss: session=%s terminal_id=%s reason=%s",
        session_id,
        terminal_id,
        reason,
    )


@dataclasses.dataclass(frozen=True)
class _CodexNativeLaunchConfig:
    """
    Persisted launch config needed for runner-owned Codex terminal setup.

    :param workspace: Workspace cwd for the Codex app-server and TUI,
        e.g. ``Path("/Users/me/repo")``.
    :param policy_server_url: Omnigent server URL for the Codex policy hook and
        forwarder, e.g. ``"http://127.0.0.1:8123"``.
    :param terminal_launch_args: User pass-through Codex CLI args, e.g.
        ``["--config", "approval_policy=on-request"]``.
    :param model_override: Persisted model override, e.g.
        ``"gpt-5.4-mini"``.
    :param external_session_id: Existing Codex thread id to resume, e.g.
        ``"thread_abc123"``.
    :param fork_source_id: SOURCE conversation id stamped on a forked
        clone (``omnigent.fork.source_id``), used to locate the
        source's ``CODEX_HOME`` when cloning its rollout, e.g.
        ``"conv_source"``. ``None`` when the session is not a fork.
    :param fork_source_external_id: SOURCE Codex thread id stamped on a
        forked clone (``omnigent.fork.source_external_session_id``),
        e.g. ``"019e96aa-..."``. ``None`` when the source had no captured
        thread id (the clone then resumes fresh).
    :param fork_carry_history: ``True`` on a forked clone bound to a
        native target (``omnigent.fork.carry_history``); when no source
        rollout exists to clone (an SDK or cross-family source) the runner
        builds the clone's rollout from the copied Omnigent items instead (see
        ``_ensure_local_codex_resume_rollout``).
    """

    workspace: Path
    policy_server_url: str
    terminal_launch_args: list[str] | None
    model_override: str | None
    external_session_id: str | None
    fork_source_id: str | None
    fork_source_external_id: str | None
    fork_carry_history: bool


@dataclasses.dataclass(frozen=True)
class _PiNativeLaunchConfig:
    """
    Persisted launch config read from a session snapshot for native terminals.

    A generic session-snapshot reader shared by the pi-native and
    cursor-native launch paths (workspace + terminal_launch_args +
    model_override). Each path consumes the subset it needs: pi-native
    ignores ``model_override``; cursor-native applies it as ``--model``.

    :param workspace: Workspace cwd for the native TUI.
    :param server_url: Omnigent server URL for the extension/forwarder.
    :param terminal_launch_args: User pass-through native CLI args.
    :param external_session_id: Existing external session id, when captured by
        the extension.
    :param model_override: Persisted per-session ``/model`` override, e.g.
        ``"claude-4.6-sonnet-medium"``; ``None`` when unset. Consumed by the
        cursor-native launch (``--model``), ignored by pi-native.
    :param fork_carry_history: ``True`` when the session is a fork bound to a
        carry-history native target (``omnigent.fork.carry_history``). Consumed
        by the cursor-native launch to replay prior turns as a text preamble on
        the first message; ignored by pi-native.
    """

    workspace: Path
    server_url: str
    terminal_launch_args: list[str] | None
    external_session_id: str | None
    model_override: str | None = None
    fork_carry_history: bool = False


@dataclasses.dataclass(frozen=True)
class _KiroNativeLaunchConfig:
    """Persisted launch config needed for runner-owned Kiro terminal setup."""

    workspace: Path
    terminal_launch_args: list[str] | None
    external_session_id: str | None


def _required_runner_env(name: str) -> str:
    """
    Return a required runner environment variable.

    :param name: Environment variable name, e.g. ``"RUNNER_SERVER_URL"``.
    :returns: Non-empty environment variable value.
    :raises RuntimeError: If the variable is missing or empty.
    """
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError(f"{name} must be set for runner-owned Codex terminals.")
    return value


def _codex_session_workspace(session_workspace: str | None) -> Path:
    """
    Resolve the cwd for a runner-owned Codex terminal.

    Mirrors :func:`_auto_create_claude_terminal`'s workspace
    resolution and the per-session filesystem registry
    (``_resolve_session_fs_registry``): the server-stored session
    ``workspace`` wins (it holds the git-worktree path for worktree
    sessions, or the repo root otherwise), falling back to the
    runner's ``OMNIGENT_RUNNER_WORKSPACE``.

    Deliberately does NOT consult ``ResolvedSpec.workdir`` — in the
    out-of-process runner that is the agent-bundle extraction dir
    (``runner-specs-<id>/ag_<id>-v<ver>``), not the repo, so using it
    stranded Codex in a temp dir with no ``.git`` (and ignored the
    worktree entirely).

    Normalizes the chosen value with ``strip().expanduser().resolve()``,
    matching the runner entrypoint's ``_runner_workspace_from_env`` and the
    per-session filesystem registry's ``Path(...).resolve()`` so a padded or
    ``~``-prefixed value can't yield a non-existent cwd or diverge from the
    path the Files panel watches.

    :param session_workspace: The session's ``workspace`` from
        ``GET /v1/sessions/{id}``, e.g.
        ``"/Users/me/repo-worktrees/feature-x"``. ``None`` when the
        snapshot omits it.
    :returns: Workspace path for the terminal cwd.
    :raises RuntimeError: If no workspace is available (neither the
        session snapshot nor ``OMNIGENT_RUNNER_WORKSPACE``).
    """
    raw = session_workspace or _required_runner_env("OMNIGENT_RUNNER_WORKSPACE")
    return Path(raw.strip()).expanduser().resolve()


def _pi_session_workspace(session_workspace: str | None) -> Path:
    """
    Resolve the cwd for a runner-owned Pi terminal.

    :param session_workspace: Session ``workspace`` from the server snapshot.
    :returns: Workspace path for the terminal cwd.
    """
    raw = session_workspace or _required_runner_env("OMNIGENT_RUNNER_WORKSPACE")
    return Path(raw.strip()).expanduser().resolve()


def _kiro_session_workspace(session_workspace: str | None) -> Path:
    """Resolve the cwd for a runner-owned Kiro terminal."""
    raw = session_workspace or _required_runner_env("OMNIGENT_RUNNER_WORKSPACE")
    return Path(raw.strip()).expanduser().resolve()


async def _kiro_native_launch_config(
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
) -> _KiroNativeLaunchConfig:
    """Fetch and validate persisted Kiro launch config for a session."""
    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned Kiro terminals.")
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch Kiro launch config for {session_id!r}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not fetch Kiro launch config for {session_id!r}: "
            f"GET /v1/sessions returned {resp.status_code}."
        )
    try:
        snapshot = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Could not fetch Kiro launch config for {session_id!r}: invalid JSON."
        ) from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"Could not fetch Kiro launch config for {session_id!r}: "
            "snapshot was not a JSON object."
        )
    terminal_launch_args = snapshot.get("terminal_launch_args")
    if terminal_launch_args is not None and not (
        isinstance(terminal_launch_args, list)
        and all(isinstance(arg, str) for arg in terminal_launch_args)
    ):
        raise RuntimeError(f"Invalid terminal_launch_args for Kiro session {session_id!r}.")
    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for Kiro session {session_id!r}.")
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id.strip()
    ):
        raise RuntimeError(f"Invalid external_session_id for Kiro session {session_id!r}.")
    return _KiroNativeLaunchConfig(
        workspace=_kiro_session_workspace(session_workspace),
        terminal_launch_args=terminal_launch_args,
        external_session_id=external_session_id.strip()
        if isinstance(external_session_id, str)
        else None,
    )


async def _pi_native_launch_config(
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
) -> _PiNativeLaunchConfig:
    """
    Fetch and validate a session's persisted native-terminal launch config.

    Shared by the pi-native and cursor-native launch paths.

    :param session_id: Session/conversation id.
    :param server_client: Runner Omnigent server client.
    :returns: Parsed launch config.
    """
    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned Pi terminals.")
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch Pi launch config for {session_id!r}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not fetch Pi launch config for {session_id!r}: "
            f"GET /v1/sessions returned {resp.status_code}."
        )
    try:
        snapshot = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Could not fetch Pi launch config for {session_id!r}: invalid JSON."
        ) from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"Could not fetch Pi launch config for {session_id!r}: snapshot was not a JSON object."
        )
    terminal_launch_args = snapshot.get("terminal_launch_args")
    if terminal_launch_args is not None and not (
        isinstance(terminal_launch_args, list)
        and all(isinstance(arg, str) for arg in terminal_launch_args)
    ):
        raise RuntimeError(f"Invalid terminal_launch_args for Pi session {session_id!r}.")
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id
    ):
        raise RuntimeError(f"Invalid external_session_id for Pi session {session_id!r}.")
    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for Pi session {session_id!r}.")
    model_override = snapshot.get("model_override")
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override:
            raise RuntimeError(f"Invalid model_override for session {session_id!r}.")
        try:
            model_override = validate_model_override(model_override)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid model_override for session {session_id!r}: {exc}"
            ) from exc
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    labels = snapshot.get("labels")
    fork_carry_history = (
        isinstance(labels, dict) and labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    )
    return _PiNativeLaunchConfig(
        workspace=_pi_session_workspace(session_workspace),
        server_url=os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767").rstrip("/"),
        terminal_launch_args=terminal_launch_args,
        external_session_id=external_session_id,
        model_override=model_override,
        fork_carry_history=fork_carry_history,
    )


async def _codex_native_launch_config(
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
) -> _CodexNativeLaunchConfig:
    """
    Fetch and validate persisted Codex launch config for a session.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param server_client: Runner Omnigent server client.
    :returns: Parsed launch config.
    :raises RuntimeError: If the session snapshot or required runner env is
        unavailable.
    """
    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned Codex terminals.")
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch Codex launch config for {session_id!r}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not fetch Codex launch config for {session_id!r}: "
            f"GET /v1/sessions returned {resp.status_code}."
        )
    try:
        snapshot = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Could not fetch Codex launch config for {session_id!r}: invalid JSON."
        ) from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"Could not fetch Codex launch config for {session_id!r}: "
            "snapshot was not a JSON object."
        )
    terminal_launch_args = snapshot.get("terminal_launch_args")
    if terminal_launch_args is not None and not (
        isinstance(terminal_launch_args, list)
        and all(isinstance(arg, str) for arg in terminal_launch_args)
    ):
        raise RuntimeError(f"Invalid terminal_launch_args for Codex session {session_id!r}.")
    model_override = snapshot.get("model_override")
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override:
            raise RuntimeError(f"Invalid model_override for Codex session {session_id!r}.")
        # Defense-in-depth: re-validate the persisted override at the runner
        # boundary so a value that somehow bypassed server-side validation
        # can never reach the Codex ``config.toml`` / ``--model`` argv as
        # shell- or TOML-shaped input.
        try:
            validate_model_override(model_override)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid model_override for Codex session {session_id!r}: {exc}"
            ) from exc
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id
    ):
        raise RuntimeError(f"Invalid external_session_id for Codex session {session_id!r}.")
    # The session's stored workspace is the worktree path for worktree
    # sessions (set by _create_session_worktree), or the repo root
    # otherwise. Use it as the Codex terminal cwd so worktree sessions
    # land in the worktree, matching claude-native and the Files panel.
    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for Codex session {session_id!r}.")
    # Fork directives stamped on a clone at fork time. Only consulted when
    # the clone has no external_session_id of its own yet (see the
    # fork-source branch in _auto_create_codex_terminal); inert otherwise.
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    fork_source_id: str | None = None
    fork_source_external_id: str | None = None
    fork_carry_history = False
    labels = snapshot.get("labels")
    if isinstance(labels, dict):
        _fsi = labels.get(FORK_SOURCE_LABEL_KEY)
        if isinstance(_fsi, str) and _fsi:
            fork_source_id = _fsi
        _fse = labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
        if isinstance(_fse, str) and _fse:
            fork_source_external_id = _fse
        fork_carry_history = labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    return _CodexNativeLaunchConfig(
        workspace=_codex_session_workspace(session_workspace),
        policy_server_url=_required_runner_env("RUNNER_SERVER_URL"),
        terminal_launch_args=terminal_launch_args,
        model_override=model_override,
        external_session_id=external_session_id,
        fork_source_id=fork_source_id,
        fork_source_external_id=fork_source_external_id,
        fork_carry_history=fork_carry_history,
    )


@dataclasses.dataclass(frozen=True)
class _OpenCodeNativeLaunchConfig:
    """
    Persisted launch config for runner-owned OpenCode terminals.

    :param workspace: Workspace cwd for ``opencode serve`` and the TUI.
    :param policy_server_url: Omnigent server URL for the forwarder.
    :param terminal_launch_args: User pass-through OpenCode CLI args.
    :param model_override: Persisted model override, or ``None``.
    :param external_session_id: Existing OpenCode session id to resume.
    """

    workspace: Path
    policy_server_url: str
    terminal_launch_args: list[str] | None
    model_override: str | None
    external_session_id: str | None


async def _opencode_native_launch_config(
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
) -> _OpenCodeNativeLaunchConfig:
    """
    Fetch and validate persisted OpenCode launch config for a session.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param server_client: Runner Omnigent server client.
    :returns: Parsed launch config.
    :raises RuntimeError: If the snapshot or required runner env is missing.
    """
    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned OpenCode terminals.")
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not fetch OpenCode launch config for {session_id!r}.") from exc
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not fetch OpenCode launch config for {session_id!r}: "
            f"GET /v1/sessions returned {resp.status_code}."
        )
    try:
        snapshot = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Could not fetch OpenCode launch config for {session_id!r}: invalid JSON."
        ) from exc
    if not isinstance(snapshot, dict):
        raise RuntimeError(
            f"Could not fetch OpenCode launch config for {session_id!r}: "
            "snapshot was not a JSON object."
        )
    terminal_launch_args = snapshot.get("terminal_launch_args")
    if terminal_launch_args is not None and not (
        isinstance(terminal_launch_args, list)
        and all(isinstance(arg, str) for arg in terminal_launch_args)
    ):
        raise RuntimeError(f"Invalid terminal_launch_args for OpenCode session {session_id!r}.")
    model_override = snapshot.get("model_override")
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override:
            raise RuntimeError(f"Invalid model_override for OpenCode session {session_id!r}.")
        try:
            validate_model_override(model_override)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid model_override for OpenCode session {session_id!r}: {exc}"
            ) from exc
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id
    ):
        raise RuntimeError(f"Invalid external_session_id for OpenCode session {session_id!r}.")
    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for OpenCode session {session_id!r}.")
    return _OpenCodeNativeLaunchConfig(
        workspace=_codex_session_workspace(session_workspace),
        policy_server_url=_required_runner_env("RUNNER_SERVER_URL"),
        terminal_launch_args=terminal_launch_args,
        model_override=model_override,
        external_session_id=external_session_id,
    )


async def _auto_create_opencode_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    agent_spec: Any | None = None,
    server_client: httpx.AsyncClient | None = None,
) -> SessionResourceView:
    """
    Auto-create an OpenCode terminal for an opencode-native session.

    Mirrors :func:`_auto_create_codex_terminal`, substituting ``opencode
    serve`` / ``opencode attach`` for Codex's app-server/remote transport:
    boots a per-session ``opencode serve`` process, resumes-or-creates the
    OpenCode session, persists bridge state + ``external_session_id``,
    starts the SSE forwarder, then registers the ``opencode attach`` TUI as
    a streamable terminal resource attached to that server.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param resource_registry: Registry used to launch the terminal.
    :param publish_event: Per-session SSE emitter for the new terminal.
    :param agent_spec: Optional resolved agent spec (os_env + model).
    :param server_client: Runner Omnigent server HTTP client.
    :returns: The created terminal resource view.
    """
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.opencode_native_app_server import (
        OpenCodeNativeServer,
        build_opencode_attach_args,
        opencode_terminal_env,
    )
    from omnigent.opencode_native_bridge import (
        OpenCodeNativeBridgeState,
        clear_bridge_state,
        prepare_bridge_dir,
        seed_opencode_auth,
        write_bridge_state,
    )
    from omnigent.opencode_native_forwarder import OpenCodeNativeForwarder

    launch_config = await _opencode_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = str(launch_config.workspace)
    bridge_dir = prepare_bridge_dir(session_id)
    # Cancel any surviving forwarder first so its teardown closes the OLD
    # server, then clear stale bridge state so web injection waits for the
    # new launch's URL/session instead of a dead one.
    await _cancel_auto_forwarder_task(session_id)
    leftover = _AUTO_OPENCODE_SERVERS.pop(session_id, None)
    if leftover is not None:
        with contextlib.suppress(Exception):
            await leftover.close()
    clear_bridge_state(bridge_dir)

    model_override = launch_config.model_override or _opencode_native_model_from_spec(agent_spec)
    # Route opencode through the Databricks AI gateway when the spec names a
    # profile. Unlike codex/claude/pi (which consume HARNESS_*_GATEWAY_* env the
    # CLI translates), opencode reads provider/auth from its own config file, so
    # synthesize an opencode.json into the per-session XDG config dir BEFORE the
    # server boots. Best-effort: if the gateway can't be resolved (no profile,
    # databricks-sdk absent, auth failure), opencode falls back to whatever
    # provider config the ambient env/global config already gives it.
    from omnigent.opencode_native_bridge import xdg_config_home_for_bridge_dir
    from omnigent.opencode_native_provider import (
        build_opencode_model_default_config,
        build_opencode_provider_config,
        resolve_databricks_gateway,
        write_opencode_provider_config,
    )

    gateway = resolve_databricks_gateway(
        _opencode_native_profile_from_spec(agent_spec), model_id=model_override
    )
    if gateway is not None:
        # Pin the per-prompt model to the synthesized provider/endpoint id, and
        # write it as opencode's default model too so the TUI launches on it.
        model_override = gateway.qualified_model
        config = build_opencode_provider_config(gateway)
        config["model"] = model_override
        write_opencode_provider_config(xdg_config_home_for_bridge_dir(bridge_dir), config)
    elif model_override:
        # No custom provider, but a model is pinned (``omni opencode --model`` or
        # the ``omni setup`` OpenCode default): write opencode's default model so
        # the native TUI and the first turn use it instead of ``opencode/big-pickle``.
        # OpenCode resolves the provider from the model-id prefix against its own
        # auth.json, so no provider block is needed.
        write_opencode_provider_config(
            xdg_config_home_for_bridge_dir(bridge_dir),
            build_opencode_model_default_config(model_override),
        )

    # The server runs with a per-session XDG_DATA_HOME, so copy the user's
    # `opencode auth login` credentials in — otherwise it can't authenticate
    # their providers and falls back to the no-auth default model. No-op on a
    # remote runner (no local auth.json) / Databricks-gateway path.
    seed_opencode_auth(bridge_dir)

    server = OpenCodeNativeServer(bridge_dir=bridge_dir, workspace=launch_config.workspace)
    await server.start()
    _AUTO_OPENCODE_SERVERS[session_id] = server

    try:
        client = server.client()
        try:
            opencode_session_id: str | None = None
            if launch_config.external_session_id is not None:
                existing = await client.get_session(launch_config.external_session_id)
                if existing is not None:
                    opencode_session_id = existing.id
            if opencode_session_id is None:
                created = await client.create_session({"title": f"omnigent:{session_id}"})
                opencode_session_id = created.id
                # Persist the OpenCode session id so a later relaunch resumes
                # it (best effort, like codex-native).
                if server_client is not None:
                    with contextlib.suppress(httpx.HTTPError):
                        await server_client.patch(
                            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                            json={"external_session_id": opencode_session_id},
                            timeout=10.0,
                        )
        finally:
            await client.aclose()

        write_bridge_state(
            bridge_dir,
            OpenCodeNativeBridgeState(
                session_id=session_id,
                server_base_url=server.base_url,
                opencode_session_id=opencode_session_id,
                auth_secret=server.auth_secret,
                xdg_data_home=str(server.xdg_data_home),
                xdg_config_home=str(server.xdg_config_home),
                model_override=model_override,
                workspace=workspace,
            ),
        )
    except Exception:
        await server.close()
        _AUTO_OPENCODE_SERVERS.pop(session_id, None)
        raise

    # Start the SSE forwarder in the background so session creation never
    # blocks on it. The forwarder owns its OpenCode client for the stream
    # lifetime; ``server_client`` is the runner's Omnigent client. The
    # supervisor closes the ``opencode serve`` subprocess when forwarding
    # ends (cancelled on session teardown), mirroring the codex forwarder's
    # ``finally`` — else one server orphans per session.
    if server_client is not None:
        forwarder = OpenCodeNativeForwarder(
            session_id=session_id,
            opencode_session_id=opencode_session_id,
            opencode_client=server.client(),
            server_client=server_client,
            bridge_dir=bridge_dir,
            workspace=workspace,
            # Route OpenCode permission requests through the SAME server-side
            # policy/approval gate codex-native uses. Without this the
            # forwarder would fall back to its fail-closed ``reject`` default
            # and deny every tool; with it, policy decides and an ``ask``
            # parks a human approval card server-side.
            policy_evaluator=_build_opencode_policy_evaluator(
                server_client=server_client,
                conversation_id=session_id,
            ),
        )
        forwarder_task = asyncio.create_task(
            _supervise_opencode_forwarder(session_id, server, forwarder),
            name=f"opencode-forwarder-{session_id}",
        )
        _register_auto_forwarder_task(session_id, forwarder_task)

    agent_os_env = _agent_os_env_from_spec(agent_spec)
    try:
        terminal_view = await resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="opencode",
            session_key="main",
            resource_role=OPENCODE_NATIVE_TERMINAL_ROLE,
            parent_os_env=agent_os_env,
            spec=TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=workspace,
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=server.opencode_path,
                args=build_opencode_attach_args(
                    server_url=server.base_url,
                    workspace=workspace,
                    session_id=opencode_session_id,
                    opencode_args=tuple(launch_config.terminal_launch_args or ()),
                ),
                env=opencode_terminal_env(server),
                scrollback=100_000,
                tmux_allow_passthrough=True,
                tmux_start_on_attach=False,
            ),
        )
        publish_event(
            session_id,
            {
                "type": "session.resource.created",
                "resource": session_resource_view_to_dict(terminal_view),
            },
        )
    except Exception:
        await _cancel_auto_forwarder_task(session_id)
        await server.close()
        _AUTO_OPENCODE_SERVERS.pop(session_id, None)
        raise

    _logger.info("Auto-created opencode terminal + forwarder for session %s", session_id)
    return terminal_view


async def _supervise_opencode_forwarder(
    session_id: str,
    server: Any,
    forwarder: Any,
) -> None:
    """
    Run the OpenCode SSE forwarder, closing the server when it ends.

    Mirrors the codex forwarder task's ``finally``: when forwarding stops
    (the SSE connection dropped or the task was cancelled on session
    teardown) the per-session ``opencode serve`` subprocess is ours to
    stop, else it orphans one process per session.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param server: The :class:`OpenCodeNativeServer` to close on exit.
    :param forwarder: The :class:`OpenCodeNativeForwarder` to run.
    :returns: None.
    """
    try:
        await forwarder.run()
    finally:
        leftover = _AUTO_OPENCODE_SERVERS.pop(session_id, None)
        if leftover is not None:
            with contextlib.suppress(Exception):
                await leftover.close()
        elif server is not None:
            with contextlib.suppress(Exception):
                await server.close()


# Permission decisions can park a human approval card server-side
# (``POLICY_ACTION_ASK``), so the evaluate POST may block until a human
# resolves it. Match the codex-native policy hook's day-long budget; the
# server caps the real wait via the deciding policy's ``ask_timeout``.
_OPENCODE_POLICY_EVALUATE_TIMEOUT_S = 86400.0
# Map the server's proto verdict onto the forwarder's verdict vocabulary
# (``map_verdict_to_decision`` reads ``decision``). Anything unknown is
# treated as ``ask`` → the forwarder fails it closed to ``reject``.
_OPENCODE_POLICY_ACTION_TO_DECISION = {
    "POLICY_ACTION_ALLOW": "allow",
    "POLICY_ACTION_DENY": "deny",
    "POLICY_ACTION_ASK": "ask",
}


def _build_opencode_policy_evaluator(
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
) -> Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]]:
    """
    Build the policy evaluator the OpenCode permission forwarder consults.

    Mirrors codex-native's policy hook exactly: every OpenCode
    ``permission.v2.asked`` request is POSTed to this session's
    ``/v1/sessions/{id}/policies/evaluate`` endpoint as a
    ``PHASE_TOOL_CALL`` event. The server evaluates configured policies and
    — for an ``ASK`` verdict — parks a human approval card and blocks until
    it is resolved, returning a hard ``ALLOW``/``DENY``. The forwarder turns
    that into an OpenCode ``once``/``always``/``reject`` reply.

    Fails CLOSED: an unreachable server, a non-200, a malformed body, or an
    unresolved ``ASK`` all yield a ``deny``/``ask`` verdict the forwarder
    rejects — never a silent approve. Only an explicit ``ALLOW`` permits the
    operation.

    :param server_client: Runner's Omnigent server HTTP client.
    :param conversation_id: Owning Omnigent session id, e.g. ``"conv_abc"``.
    :returns: An async evaluator returning a verdict mapping, or a deny
        verdict on failure.
    """
    from omnigent.opencode_native_permissions import OPENCODE_NATIVE_HARNESS

    session_component = urllib.parse.quote(conversation_id, safe="")
    url = f"/v1/sessions/{session_component}/policies/evaluate"

    async def _evaluate(normalized: Mapping[str, Any]) -> Mapping[str, Any] | None:
        arguments: dict[str, Any] = {
            key: normalized[key]
            for key in ("command", "path", "url")
            if normalized.get(key) is not None
        }
        metadata = normalized.get("metadata")
        if isinstance(metadata, Mapping) and metadata:
            arguments.setdefault("metadata", dict(metadata))
        body = {
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {
                    "name": normalized.get("action") or "permission",
                    "arguments": arguments,
                },
                "context": {"harness": OPENCODE_NATIVE_HARNESS},
            },
        }
        try:
            resp = await server_client.post(
                url, json=body, timeout=_OPENCODE_POLICY_EVALUATE_TIMEOUT_S
            )
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode policy evaluate POST failed for %s; failing closed",
                conversation_id,
                exc_info=True,
            )
            return {"decision": "deny"}
        if resp.status_code != 200 or not resp.content:
            _logger.warning(
                "OpenCode policy evaluate returned %s for %s; failing closed",
                resp.status_code,
                conversation_id,
            )
            return {"decision": "deny"}
        try:
            result = resp.json()
        except ValueError:
            _logger.warning("OpenCode policy evaluate returned non-JSON; failing closed")
            return {"decision": "deny"}
        action = result.get("result") if isinstance(result, Mapping) else None
        return {"decision": _OPENCODE_POLICY_ACTION_TO_DECISION.get(str(action), "ask")}

    return _evaluate


def _opencode_native_model_from_spec(agent_spec: Any | None) -> str | None:
    """
    Resolve the OpenCode default model from a resolved agent spec.

    :param agent_spec: Optional resolved agent spec.
    :returns: The spec's executor model, or ``None``.
    """
    if agent_spec is None:
        return None
    try:
        from omnigent.runtime.workflow import _resolve_spec_model

        return _resolve_spec_model(getattr(agent_spec, "spec", agent_spec))
    except Exception:  # noqa: BLE001 - model resolution is best effort.
        return None


def _opencode_native_profile_from_spec(agent_spec: Any | None) -> str | None:
    """
    Resolve the Databricks profile from a resolved agent spec, if any.

    :param agent_spec: Optional resolved agent spec.
    :returns: The spec's ``executor.config.profile``, or ``None``.
    """
    if agent_spec is None:
        return None
    try:
        spec = getattr(agent_spec, "spec", agent_spec)
        profile = spec.executor.config.get("profile")
        return str(profile) if profile else None
    except Exception:  # noqa: BLE001 - profile resolution is best effort.
        return None


def _pi_args_have_session_control(args: list[str]) -> bool:
    """
    Return whether user Pi args already specify session behavior.

    :param args: User pass-through Pi CLI args.
    :returns: ``True`` when Omnigent should not add resume/session flags.
    """
    session_flags = {
        "--session-dir",
        "--session",
        "--continue",
        "--resume",
        "--fork",
        "--no-session",
    }
    for arg in args:
        if arg in session_flags:
            return True
        if arg.startswith(("--session-dir=", "--session=")):
            return True
    return False


def _pi_args_have_provider(args: list[str]) -> bool:
    """Return whether user Pi args already pin a provider/model/key.

    When the user passes their own ``--provider`` / ``--model`` / ``--api-key``,
    Omnigent must not inject the ``omnigent setup`` provider on top — the
    explicit choice wins.

    :param args: User pass-through Pi CLI args.
    :returns: ``True`` when Omnigent should not add provider/model args.
    """
    provider_flags = {"--provider", "--model", "--api-key"}
    for arg in args:
        if arg in provider_flags:
            return True
        if arg.startswith(("--provider=", "--model=", "--api-key=")):
            return True
    return False


def _build_pi_native_args(
    *,
    terminal_launch_args: list[str] | None,
    extension_path: Path,
    session_dir: Path,
    external_session_id: str | None,
) -> list[str]:
    """
    Build Pi CLI args for a runner-owned native TUI session.

    :param terminal_launch_args: User pass-through Pi args.
    :param extension_path: Generated Omnigent Pi extension path.
    :param session_dir: Per-Omnigent-session Pi session directory.
    :param external_session_id: Captured Pi session id, if any.
    :returns: Complete Pi arg vector excluding the executable.
    """
    user_args = list(terminal_launch_args or [])
    args = ["--extension", str(extension_path)]
    if not _pi_args_have_session_control(user_args):
        args.extend(["--session-dir", str(session_dir)])
        if external_session_id:
            args.extend(["--session", external_session_id])
    args.extend(user_args)
    return args


async def _auto_create_pi_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
    agent_spec: AgentSpec | ResolvedSpec | None = None,
) -> SessionResourceView:
    """
    Auto-create a Pi terminal for a pi-native session.

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :param agent_spec: The session's resolved agent spec, passed so the Pi
        terminal inherits the agent's ``os_env.sandbox`` rather than falling
        back to the platform default. ``None`` only when the session has no
        spec; callers must not pass ``None`` to paper over a resolution error.
    :returns: Created terminal resource view.
    """
    from omnigent.conversation_browser import conversation_url
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.pi_native import resolve_pi_executable
    from omnigent.pi_native_bridge import (
        PI_NATIVE_CONFIG_ENV_VAR,
        clear_inbox,
        pi_session_dir,
        prepare_bridge_dir,
        write_extension_files,
    )
    from omnigent.pi_native_bridge import extension_path as pi_extension_path
    from omnigent.runner._entry import _make_auth_token_factory

    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = str(launch_config.workspace)
    bridge_dir = prepare_bridge_dir(session_id)
    # Drop stale payloads so a relaunched Pi process can't replay them.
    clear_inbox(bridge_dir)
    pi_extension = pi_extension_path(bridge_dir)
    session_dir = pi_session_dir(bridge_dir)
    auth_factory = _make_auth_token_factory()
    auth_token = auth_factory() if auth_factory is not None else None
    auth_headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    _extension, config = write_extension_files(
        bridge_dir,
        session_id=session_id,
        server_url=launch_config.server_url,
        conversation_url=conversation_url(launch_config.server_url, session_id),
        auth_headers=auth_headers,
    )
    pi_command = resolve_pi_executable()
    pi_args = _build_pi_native_args(
        terminal_launch_args=launch_config.terminal_launch_args,
        extension_path=pi_extension,
        session_dir=session_dir,
        external_session_id=launch_config.external_session_id,
    )
    pi_env = {
        PI_NATIVE_CONFIG_ENV_VAR: str(config),
        "OMNIGENT_PI_NATIVE_BRIDGE_DIR": str(bridge_dir),
    }
    # Route the runner-owned Pi process through the provider configured by
    # ``omnigent setup`` (Databricks gateway / API key), so a separate
    # ``pi /login`` isn't required — the parity codex-native/claude-native
    # already have. Skipped when the user pinned their own provider/model via
    # terminal_launch_args, or when no usable provider is configured (Pi then
    # falls back to its own login). Writes a managed per-session Pi config dir,
    # never touching the user's global ``~/.pi/agent``.
    if not _pi_args_have_provider(launch_config.terminal_launch_args or []):
        from omnigent.pi_native_credentials import (
            pi_native_provider_launch,
            resolve_pi_native_provider,
        )

        provider = resolve_pi_native_provider()
        if provider is not None:
            cred_env, cred_args = pi_native_provider_launch(bridge_dir / "pi-agent", provider)
            pi_env.update(cred_env)
            pi_args.extend(cred_args)
    # Inherit the agent's os_env so its sandbox (e.g. ``type: none``),
    # egress_rules and env_passthrough are honoured. Without ``sandbox`` here
    # and ``parent_os_env`` below, launch_required_terminal falls back to
    # _default_sandbox_for_platform (linux_bwrap), overriding the YAML config.
    agent_os_env = _agent_os_env_from_spec(agent_spec)
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="pi",
        session_key="main",
        resource_role=PI_NATIVE_TERMINAL_ROLE,
        parent_os_env=agent_os_env,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=workspace,
                sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
            ),
            command=pi_command,
            args=pi_args,
            env=pi_env,
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )
    _logger.info(
        "Auto-created pi terminal for session %s with extension %s",
        session_id,
        pi_extension,
    )
    return terminal_view


async def _auto_create_cursor_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
    agent_spec: AgentSpec | ResolvedSpec | None = None,
) -> SessionResourceView:
    """
    Auto-create the Cursor TUI terminal for a cursor-native session.

    Launches ``cursor-agent`` (no args → interactive TUI) in a runner-owned
    tmux pane. Auth is the ambient ``cursor-agent login`` (``$HOME/.cursor``),
    so HOME is inherited and no extension bridge is written (cursor owns its own
    tool surface). On first launch in an untrusted workspace the TUI shows a
    one-time "Trust this workspace" prompt the user accepts.

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :param agent_spec: Optional resolved agent spec for the session. When it
        declares a cursor-agent model (``executor.model``), that model is passed
        to the TUI via ``--model`` unless the user already pinned one through the
        passthrough launch args.
    :returns: Created terminal resource view.
    """
    from omnigent.cursor_native import resolve_cursor_executable
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    # Stamp the launch time before the TUI starts. cursor creates the chat's
    # on-disk store lazily on the first message, so its ``meta.json``
    # ``createdAtMs`` is always >= this — which lets the forwarder discover
    # *this* session's chat by recency under ``~/.cursor/chats/<md5(cwd)>``.
    launch_epoch_ms = int(time.time() * 1000)
    # Tear down any forwarder left from a prior terminal for this session before
    # re-creating, so the old and new tasks can't both mirror (double-posting),
    # and drop the prior terminal's stale forward cursor so the new forwarder
    # can't resume the wrong chat / a stale rowid (mirrors codex's clear_bridge_state).
    await _cancel_auto_forwarder_task(session_id)
    from omnigent.cursor_native import is_valid_cursor_chat_id
    from omnigent.cursor_native_bridge import (
        approve_mcp_server_for_workspace,
        bridge_dir_for_session_id,
        write_fork_preamble,
        write_hooks_config,
        write_mcp_config,
    )
    from omnigent.cursor_native_forwarder import clear_cursor_bridge_state, preseed_resume_state
    from omnigent.cursor_native_usage import clear_cursor_usage_state

    bridge_dir = bridge_dir_for_session_id(session_id)

    # Shared native-terminal snapshot reader (workspace + terminal_launch_args
    # + model_override), also used by the pi-native launch.
    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    # Canonicalize the workspace (resolve symlinks / trailing slashes) so the
    # cursor TUI's cwd and the forwarder hash the SAME path — cursor keys its
    # chat store dir on ``md5(cwd)``, and a mismatch would hide the store.
    workspace = os.path.realpath(str(launch_config.workspace))
    # Validate the persisted chat id ONCE, up front. It feeds two untrusted
    # sinks below — the cursor store path in preseed_resume_state (filesystem)
    # and the ``--resume`` argv in _cursor_native_resume_args — so a malformed
    # value must reach neither (defense-in-depth). cursor mints UUID chat ids;
    # anything else is dropped here and the session starts fresh.
    resume_chat_id = launch_config.external_session_id
    if resume_chat_id and not is_valid_cursor_chat_id(resume_chat_id):
        _logger.warning(
            "cursor-native: persisted chat id %r is not a well-formed cursor "
            "chat id; ignoring it for resume (session=%s).",
            resume_chat_id,
            session_id,
        )
        resume_chat_id = None
    # On cold resume, pre-seed the bridge state with the known store path and
    # current rowid so the forwarder skips launch-recency discovery (the existing
    # chat store predates this launch and would fail _discover_store's floor check).
    # On a fresh start, clear any stale state from a prior terminal so the old
    # and new forwarders can't double-post the same chat.
    #
    # Tie the ``--resume`` decision to preseed success: only resume when we
    # actually pre-seeded the prior store. If preseed fails (the store dir is
    # gone), injecting ``--resume`` anyway would reload that store in the TUI
    # while the cleared forwarder falls back to discovery — whose recency floor
    # excludes the pre-launch store — so the relaunched chat would go unmirrored.
    # Dropping resume here starts a genuinely fresh chat that discovery can find.
    preseeded = bool(resume_chat_id) and preseed_resume_state(
        bridge_dir, workspace, resume_chat_id, launch_epoch_ms
    )
    if not preseeded:
        clear_cursor_bridge_state(bridge_dir)
        # Drop any prior terminal's usage log/state so the new forwarder starts
        # the cumulative count clean. Preserved across a preseeded resume (the
        # accumulator's generation-id dedup makes re-reading the log safe).
        clear_cursor_usage_state(bridge_dir)
        if resume_chat_id is not None:
            _logger.warning(
                "cursor-native: could not pre-seed prior chat store for %r; "
                "starting a fresh chat (session=%s).",
                resume_chat_id,
                session_id,
            )
            resume_chat_id = None
    # A fork bound to cursor carries history as a text preamble: cursor's
    # conversation is server-backed, so there's no local store to seed for
    # ``--resume`` (a fresh fork has no prior chat anyway → ``not preseeded``).
    # Render the copied Omnigent items once and stash them; the executor prepends
    # them to the fork's first injected message. Best-effort — a failure just
    # starts the cursor turn without the prior context.
    if launch_config.fork_carry_history and not preseeded and server_client is not None:
        try:
            from omnigent.claude_native import _fetch_all_session_items_for_claude_resume

            fork_items = await _fetch_all_session_items_for_claude_resume(
                server_client, session_id
            )
            write_fork_preamble(bridge_dir, _cursor_fork_history_preamble(fork_items))
        except Exception:  # noqa: BLE001 — context carry-over is best-effort
            _logger.warning(
                "cursor-native: could not build fork history preamble (session=%s).",
                session_id,
                exc_info=True,
            )
    write_mcp_config(Path(workspace), bridge_dir)
    # Register the cursor ``stop`` hook that captures per-turn token usage into
    # the bridge dir for the usage forwarder below (see cursor_native_usage).
    write_hooks_config(Path(workspace), bridge_dir)
    cursor_command = resolve_cursor_executable()
    cursor_args = list(launch_config.terminal_launch_args or [])
    if "--approve-mcps" not in cursor_args:
        cursor_args.append("--approve-mcps")
    # On cold resume, pass ``--resume <chatId>`` to cursor-agent so the TUI
    # reloads the prior conversation. The id was validated above; ``None`` on a
    # brand-new session, so no ``--resume`` is injected and cursor starts fresh.
    cursor_args.extend(_cursor_native_resume_args(resume_chat_id, cursor_args))
    # Launch cursor-agent with ``--model <model>``. Precedence mirrors the
    # codex-native path above: the persisted ``/model`` override
    # (``model_override``) wins, falling back to the spec's pinned model
    # (``--model`` flag / config.yaml ``model:``). An explicit model in the
    # passthrough launch args (``omnigent cursor -- --model X`` or the joined
    # ``--model=X`` form) wins over both, so only inject when the user did not
    # already pin one — otherwise cursor-agent would see two ``--model`` values.
    if not any(arg in ("--model", "-m") or arg.startswith("--model=") for arg in cursor_args):
        model = launch_config.model_override or _cursor_native_model_from_spec(agent_spec)
        if model is not None:
            cursor_args.extend(["--model", model])
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="cursor",
        session_key="main",
        resource_role=CURSOR_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=cursor_command,
            args=cursor_args,
            env={},
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    # Advertise the tmux socket+target so the cursor-native harness executor can
    # inject web-UI messages into this same pane (tmux paste), wiring the web
    # chat box to the running TUI.
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is not None:
        instance = terminal_registry.get(session_id, "cursor", "main")
        if instance is not None and instance.running:
            from omnigent.cursor_native_bridge import write_tmux_target

            write_tmux_target(
                bridge_dir,
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
            )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )

    # Mirror the Cursor TUI's conversation back into the Omnigent session so the
    # chat view (message bubbles, derived title, working spinner) tracks the
    # embedded terminal. Host-spawned sessions have no CLI client to start this,
    # so the runner owns it — the cursor analog of the claude/codex transcript
    # forwarders. Reuses the runner's own server URL + refresh-capable auth.
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    # Fail loud if the server URL isn't in the env (matches codex's
    # ``_required_runner_env``): silently defaulting to ``localhost:6767`` would
    # make every mirror POST miss on a remote deploy, leaving the web
    # conversation permanently empty.
    server_url = _required_runner_env("RUNNER_SERVER_URL")
    # Authorization rides solely on the refresh-capable auth (no static header
    # snapshot that would expire mid-session), matching the runner's server_client.
    _runner_auth = _RunnerDatabricksAuth(_make_auth_token_factory())

    from omnigent.cursor_native_forwarder import supervise_cursor_forwarder
    from omnigent.cursor_native_permissions import supervise_cursor_transcript_elicitations
    from omnigent.cursor_native_usage import supervise_cursor_usage_forwarder

    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )
    approve_mcp_server_for_workspace(Path(workspace))

    async def _supervise_cursor_native_bridges() -> None:
        """Run the transcript forwarder and the approval mirror together.

        Both are per-session, runner-owned, and restart-on-failure; gathering
        them under one task keeps a single registration/cancellation handle
        (:func:`_register_auto_forwarder_task`) for session teardown. The
        forwarder mirrors cursor-agent's replies onto the conversation; the
        transcript elicitation detector surfaces cursor's native tool-approval
        prompts as web elicitations by tailing the chat store for pending tool
        calls (see :mod:`omnigent.cursor_native_permissions`) — more reliable
        than scraping the rendered pane, which misses prompts whose wording
        falls outside its regex; the usage forwarder tails the ``stop``-hook
        usage log and posts cumulative token usage / cost (see
        :mod:`omnigent.cursor_native_usage`).
        """
        await asyncio.gather(
            supervise_cursor_forwarder(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="cursor-native-ui",
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                auth=_runner_auth,
            ),
            supervise_cursor_transcript_elicitations(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                auth=_runner_auth,
            ),
            supervise_cursor_usage_forwarder(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                auth=_runner_auth,
            ),
        )

    _forwarder_task = asyncio.create_task(
        _supervise_cursor_native_bridges(),
        name=f"cursor-bridges-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created cursor terminal + forwarder/approval-mirror for session %s; task=%s",
        session_id,
        _forwarder_task.get_name(),
    )
    return terminal_view


async def _auto_create_goose_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """
    Auto-create the Goose TUI terminal for a goose-native session.

    Launches ``goose session --name <session_id>`` in a runner-owned tmux pane.
    Auth is Goose's own configuration (``goose configure`` → keyring /
    ``~/.config/goose/config.yaml``), so HOME is inherited and Omnigent writes no
    vendor config (Goose owns its own tool surface / MCP extensions). The
    ``--name`` lets the forwarder discover *this* session's row deterministically.
    Mirrors :func:`_auto_create_cursor_terminal`, minus the MCP machinery.

    :param session_id: Session/conversation identifier (also the goose ``--name``).
    :param resource_registry: Session resource registry for launching the terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :returns: Created terminal resource view.
    """
    from omnigent.goose_native import resolve_goose_executable
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    # Tear down any forwarder left from a prior terminal for this session before
    # re-creating, so old and new tasks can't both mirror (double-posting), and
    # drop the prior terminal's stale forward cursor.
    await _cancel_auto_forwarder_task(session_id)
    from omnigent.goose_native_bridge import bridge_dir_for_session_id, write_tmux_target
    from omnigent.goose_native_forwarder import clear_goose_bridge_state

    bridge_dir = bridge_dir_for_session_id(session_id)
    clear_goose_bridge_state(bridge_dir)

    # ``_pi_native_launch_config`` is a generic session-snapshot reader
    # (workspace + terminal_launch_args); reused here, not Pi-specific.
    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = os.path.realpath(str(launch_config.workspace))
    goose_command = resolve_goose_executable()
    # GOOSE_MODE=smart_approve so Goose prompts in its TUI before sensitive tools
    # (its native approval, which shows in the terminal and the web's embedded
    # terminal). Goose's default mode is Auto (no prompt), so we set this for the
    # approval flow to appear at all. Provider/model come from `goose configure`.
    goose_env: dict[str, str] = {
        "GOOSE_CLI_THEME": "ansi",
        "GOOSE_TELEMETRY_OFF": "1",
        "GOOSE_MODE": "smart_approve",
    }
    # Launch-unique Goose session name. `goose session --name X` (without
    # --resume) creates a NEW sessions row each launch (verified, Goose 1.38),
    # so a per-launch-unique name lets the forwarder bind to EXACTLY this
    # launch's row — never an older same-conversation row left by a prior
    # cold-resume. This closes the "replay the whole transcript on restart"
    # risk: discovery resolves one session, and the wiped bridge cursor
    # (clear_goose_bridge_state above) starts it at the new row's first message.
    goose_session_name = f"{session_id}-{int(time.time() * 1000)}"
    goose_args = [
        "session",
        "--name",
        goose_session_name,
        *(launch_config.terminal_launch_args or []),
    ]
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="goose",
        session_key="main",
        resource_role=GOOSE_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=goose_command,
            args=goose_args,
            # ANSI theme keeps the pane cheap to scrape; GOOSE_TELEMETRY_OFF
            # suppresses Goose's first-run "share usage data?" prompt, which
            # would otherwise block the headless pane on a fresh install;
            # GOOSE_MODE=smart_approve turns on Goose's own in-TUI approval. Goose's
            # provider/model come from the user's own `goose configure` (KTD4).
            env=goose_env,
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    # Advertise the tmux socket+target so the goose-native harness executor can
    # inject web-UI messages into this same pane (tmux paste).
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is not None:
        instance = terminal_registry.get(session_id, "goose", "main")
        if instance is not None and instance.running:
            write_tmux_target(
                bridge_dir,
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
            )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )

    # Mirror the Goose TUI's conversation back into the Omnigent session so the
    # chat view tracks the embedded terminal. Host-spawned sessions have no CLI
    # client to start this, so the runner owns it — reusing the runner's own
    # server URL + refresh-capable auth.
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    _runner_auth = _RunnerDatabricksAuth(_make_auth_token_factory())

    from omnigent.goose_native_forwarder import supervise_goose_forwarder
    from omnigent.goose_native_permissions import supervise_goose_approval_mirror

    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    async def _supervise_goose_native_bridges() -> None:
        """Run the transcript forwarder and the approval mirror together.

        Both are per-session, runner-owned, restart-on-failure; gathering them
        under one task keeps a single registration/cancellation handle for
        teardown. The forwarder mirrors Goose's transcript onto the conversation;
        the approval mirror surfaces Goose's cliclack tool-confirmation prompt as
        a web elicitation (see :mod:`omnigent.goose_native_permissions`).
        """
        await asyncio.gather(
            supervise_goose_forwarder(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="goose-native-ui",
                goose_session_name=goose_session_name,
                auth=_runner_auth,
            ),
            supervise_goose_approval_mirror(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                auth=_runner_auth,
            ),
        )

    _forwarder_task = asyncio.create_task(
        _supervise_goose_native_bridges(),
        name=f"goose-bridges-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created goose terminal + forwarder/approval-mirror for session %s; task=%s",
        session_id,
        _forwarder_task.get_name(),
    )
    return terminal_view


async def _auto_create_hermes_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """
    Auto-create the Hermes TUI terminal for a hermes-native session.

    Launches the bare ``hermes`` TUI in a runner-owned tmux pane. Auth is Hermes'
    own configuration (``hermes setup`` / ``hermes model`` →
    ``~/.hermes/config.yaml``), so HOME is inherited and Omnigent writes no vendor
    config (Hermes owns its own tool surface / skills). Hermes can't be told its
    session id in advance, so the forwarder discovers *this* launch's row by
    ``cwd`` + ``started_at`` floor (see :mod:`omnigent.hermes_native_forwarder`).
    Mirrors :func:`_auto_create_goose_terminal`.

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :returns: Created terminal resource view.
    """
    from omnigent.hermes_native import resolve_hermes_executable
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    # Tear down any forwarder left from a prior terminal for this session before
    # re-creating, so old and new tasks can't both mirror (double-posting), and
    # drop the prior terminal's stale forward cursor.
    await _cancel_auto_forwarder_task(session_id)
    from omnigent.hermes_native_bridge import bridge_dir_for_session_id, write_tmux_target
    from omnigent.hermes_native_forwarder import clear_hermes_bridge_state

    bridge_dir = bridge_dir_for_session_id(session_id)
    clear_hermes_bridge_state(bridge_dir)

    # ``_pi_native_launch_config`` is a generic session-snapshot reader
    # (workspace + terminal_launch_args); reused here, not Pi-specific.
    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = os.path.realpath(str(launch_config.workspace))
    hermes_command = resolve_hermes_executable()
    # Stamp the discovery floor BEFORE launch: the forwarder binds the newest
    # ``sessions`` row whose ``cwd`` matches this workspace and whose
    # ``started_at`` is at/after this instant (minus a small skew). A wiped bridge
    # cursor (clear_hermes_bridge_state above) starts it at that row's first row.
    launch_epoch_s = time.time()
    hermes_args = [*(launch_config.terminal_launch_args or [])]
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="hermes",
        session_key="main",
        resource_role=HERMES_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=hermes_command,
            args=hermes_args,
            # No env overrides: Hermes uses the user's own ~/.hermes (model,
            # provider, tools, and its native tool-approval prompt — which appears
            # in the TUI and the web's embedded terminal). No NO_COLOR (an earlier
            # NO_COLOR=1 rendered the gold TUI white); no HERMES_YOLO_MODE (that
            # suppressed Hermes' own approval). The bridge captures the pane with
            # ``tmux capture-pane -p`` (ANSI stripped), so colour never interferes.
            env={},
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    # Advertise the tmux socket+target so the hermes-native harness executor can
    # inject web-UI messages into this same pane (tmux paste).
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is not None:
        instance = terminal_registry.get(session_id, "hermes", "main")
        if instance is not None and instance.running:
            write_tmux_target(
                bridge_dir,
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
            )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )

    # Mirror the Hermes TUI's conversation back into the Omnigent session so the
    # chat view tracks the embedded terminal. Host-spawned sessions have no CLI
    # client to start this, so the runner owns it — reusing the runner's own
    # server URL + refresh-capable auth.
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    _runner_auth = _RunnerDatabricksAuth(_make_auth_token_factory())

    from omnigent.hermes_native_forwarder import supervise_hermes_forwarder
    from omnigent.hermes_native_permissions import supervise_hermes_approval_mirror

    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    async def _supervise_hermes_native_bridges() -> None:
        """Run the transcript forwarder and the approval mirror together.

        Both are per-session, runner-owned, restart-on-failure; gathering them
        under one task keeps a single registration/cancellation handle for
        teardown. The forwarder mirrors the TUI transcript onto the conversation;
        the approval mirror surfaces Hermes' dangerous-command prompt as a web
        elicitation (see :mod:`omnigent.hermes_native_permissions`).
        """
        await asyncio.gather(
            supervise_hermes_forwarder(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="hermes-native-ui",
                workspace=workspace,
                launch_epoch_s=launch_epoch_s,
                # The native TUI uses the user's ~/.hermes, so the forwarder tails
                # the default store there (default_state_db()).
                auth=_runner_auth,
            ),
            supervise_hermes_approval_mirror(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                auth=_runner_auth,
            ),
        )

    _forwarder_task = asyncio.create_task(
        _supervise_hermes_native_bridges(),
        name=f"hermes-bridges-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created hermes terminal + forwarder/approval-mirror for session %s; task=%s",
        session_id,
        _forwarder_task.get_name(),
    )
    return terminal_view


async def _auto_create_kiro_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
) -> SessionResourceView:
    """Auto-create the Kiro TUI terminal for a kiro-native session."""
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.kiro_native import build_kiro_launch
    from omnigent.kiro_native_bridge import (
        KIRO_NATIVE_ENV_UNSET,
        build_kiro_native_terminal_env,
        prepare_bridge_dir,
    )

    launch_config = await _kiro_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace_path = launch_config.workspace
    if not workspace_path.exists():
        raise RuntimeError(f"Kiro workspace does not exist for session {session_id!r}.")
    workspace = str(workspace_path)
    bridge_dir = prepare_bridge_dir(session_id)
    kiro_launch = build_kiro_launch(
        launch_config.terminal_launch_args or [],
        resume_id=launch_config.external_session_id,
    )
    launch_epoch_ms = int(time.time() * 1000)
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="kiro",
        session_key="main",
        resource_role=KIRO_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=kiro_launch.executable,
            args=kiro_launch.argv[1:],
            env=build_kiro_native_terminal_env(session_id),
            env_unset=list(KIRO_NATIVE_ENV_UNSET),
            inherit_env=False,
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is not None:
        instance = terminal_registry.get(session_id, "kiro", "main")
        if instance is not None and instance.running:
            from omnigent.kiro_native_bridge import write_tmux_target

            write_tmux_target(
                bridge_dir,
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
                requires_forwarder_ready=launch_config.external_session_id is not None,
            )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    _runner_auth = _RunnerDatabricksAuth(_make_auth_token_factory())

    from omnigent.kiro_native_session_forwarder import supervise_kiro_session_forwarder

    _forwarder_task = asyncio.create_task(
        supervise_kiro_session_forwarder(
            base_url=server_url,
            headers={},
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="kiro-native-ui",
            workspace=workspace,
            launch_epoch_ms=launch_epoch_ms,
            expected_session_id=launch_config.external_session_id,
            auth=_runner_auth,
        ),
        name=f"kiro-session-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created kiro terminal + session forwarder for session %s; forwarder_task=%s",
        session_id,
        _forwarder_task.get_name(),
    )
    return terminal_view


async def _persist_qwen_external_session_id(
    server_client: httpx.AsyncClient | None,
    session_id: str,
    qwen_session_id: str,
) -> None:
    """Record the qwen session id on the Omnigent session as ``external_session_id``.

    Mirrors claude-/codex-/pi-native: the persisted id is what a later resume
    reads back from the session snapshot to restore the vendor TUI, and what
    ``fork_conversation`` stamps as ``omnigent.fork.source_external_session_id``
    so a fork can carry history. Best-effort — a transient failure only degrades
    resume/fork carry-over, never the live turn (the deterministic id +
    on-disk-recording check still let the *next* launch resume).

    :param server_client: Runner Omnigent server client (``None`` skips the write).
    :param session_id: Omnigent session/conversation id.
    :param qwen_session_id: The qwen ``--session-id`` to persist.
    """
    if server_client is None:
        return
    try:
        resp = await server_client.patch(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            json={"external_session_id": qwen_session_id},
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "Could not record qwen external_session_id for %s; resume/fork will start fresh",
            session_id,
            exc_info=True,
        )
        return
    if resp.status_code >= 400:
        _logger.warning(
            "AP rejected qwen external_session_id PATCH (%s); session=%s",
            resp.status_code,
            session_id,
        )


async def _auto_create_qwen_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """
    Auto-create the qwen TUI terminal for a qwen-native session.

    Launches the interactive ``qwen`` TUI in a runner-owned tmux pane, pointed at
    the bridge dir's ``--input-file`` (web-UI turns are appended here as JSONL
    ``submit`` commands) and ``--json-file`` (qwen streams structured events here
    for the forwarder to mirror). Auth is qwen's own configuration (OpenAI-compat
    env vars or ``~/.qwen`` from ``/auth``), so HOME is inherited and Omnigent
    writes no vendor config. Mirrors :func:`_auto_create_goose_terminal`, with a
    file-based bridge instead of tmux ``send-keys``.

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client.
    :returns: Created terminal resource view.
    """
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.qwen_native import resolve_qwen_executable

    # Tear down any forwarder left from a prior terminal for this session before
    # re-creating, so old and new tasks can't both mirror (double-posting), and
    # drop the prior terminal's stale forward cursor + queued input.
    await _cancel_auto_forwarder_task(session_id)
    from omnigent.qwen_native_bridge import (
        bridge_dir_for_session_id,
        events_file_path,
        input_file_path,
        prepare_bridge_files,
        qwen_session_id_for_conversation,
        qwen_session_recording_exists,
        write_tmux_target,
    )
    from omnigent.qwen_native_forwarder import clear_qwen_bridge_state

    bridge_dir = bridge_dir_for_session_id(session_id)
    clear_qwen_bridge_state(bridge_dir)
    # Create fresh, empty input + event files before launch: qwen ``watchFile``\\s
    # the ``--input-file`` (it must exist) and a relaunched terminal must not
    # replay a prior process's queued commands or events.
    prepare_bridge_files(bridge_dir)
    in_path = input_file_path(bridge_dir)
    out_path = events_file_path(bridge_dir)

    # ``_pi_native_launch_config`` is a generic session-snapshot reader
    # (workspace + terminal_launch_args); reused here, not Pi-specific.
    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = os.path.realpath(str(launch_config.workspace))
    qwen_command = resolve_qwen_executable()
    # Resume the qwen TUI's own history on re-launch (resume / runner restart) so
    # the embedded pane shows the prior conversation, not a blank prompt. Uses the
    # same ``external_session_id`` convention as claude-/codex-/pi-native: the id
    # is persisted on the Omnigent session and read back from the snapshot
    # (``launch_config.external_session_id``), which also lets a fork carry history
    # (``omnigent.fork.source_external_session_id``). qwen is cleaner than
    # claude/codex here — it lets us *assign* the id via ``--session-id``, so we
    # mint a deterministic per-conversation one up front instead of capturing a
    # vendor-generated id off the event stream (and a failed persist self-heals,
    # since the id is recomputable).
    #
    # ``--resume`` on an id qwen never recorded shows its blocking "No saved
    # session found" screen, so the actual resume guard is the on-disk recording
    # check (also covers the never-messaged edge and pre-convention sessions →
    # clean fresh launch). qwen restores history into the TUI from its own
    # checkpoint and emits only NEW events to ``--json-file`` on resume (verified),
    # so the forwarder never re-mirrors the prior transcript — no duplicate bubbles.
    existing_session_id = launch_config.external_session_id
    qwen_session_id = existing_session_id or qwen_session_id_for_conversation(session_id)
    # Scope the recording check to THIS workspace's qwen project slug: qwen
    # resolves ``--resume`` per-project (cwd), so a recording made under another
    # workspace must not pick ``--resume`` here (→ blocking "No saved session").
    if qwen_session_recording_exists(qwen_session_id, workspace):
        resume_args = ["--resume", qwen_session_id]
    else:
        resume_args = ["--session-id", qwen_session_id]
    if existing_session_id != qwen_session_id:
        # First launch (or a prior persist that didn't land): record the id so the
        # next resume reads it from the snapshot and forks can carry history.
        await _persist_qwen_external_session_id(server_client, session_id, qwen_session_id)
    # The dual-output + input-file flags wire qwen to the bridge; any user
    # ``terminal_launch_args`` (e.g. ``-m <model>``) precede them. Approval stays
    # the default in-terminal prompt (the embedded pane shows it) — Omnigent-side
    # gating via ``confirmation_response`` is a follow-up (see design doc).
    qwen_args = [
        *(launch_config.terminal_launch_args or []),
        *resume_args,
        "--input-file",
        str(in_path),
        "--json-file",
        str(out_path),
    ]
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="qwen",
        session_key="main",
        resource_role=QWEN_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=qwen_command,
            args=qwen_args,
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    # Advertise the tmux socket+target so interrupt (Escape) / stop (kill) can
    # reach this pane — message injection itself is file-based, not tmux.
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is not None:
        instance = terminal_registry.get(session_id, "qwen", "main")
        if instance is not None and instance.running:
            write_tmux_target(
                bridge_dir,
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
            )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )

    # Mirror the qwen TUI's conversation back into the Omnigent session so the
    # chat view tracks the embedded terminal. Host-spawned sessions have no CLI
    # client to start this, so the runner owns it — reusing the runner's own
    # server URL + refresh-capable auth.
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    _runner_auth = _RunnerDatabricksAuth(_make_auth_token_factory())

    from omnigent.qwen_native_forwarder import supervise_qwen_forwarder
    from omnigent.qwen_native_permissions import supervise_qwen_approval_mirror

    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    async def _supervise_qwen_native_bridges() -> None:
        """Run the transcript forwarder and the approval mirror together.

        Both are per-session, runner-owned, and self-healing (they catch and
        log their own failures rather than exiting); gathering them under one
        task keeps a single registration/cancellation handle
        (:func:`_register_auto_forwarder_task`) for session teardown. The
        forwarder mirrors qwen's replies onto the conversation; the approval
        mirror surfaces qwen's native ``can_use_tool`` prompts as web
        elicitations (see :mod:`omnigent.qwen_native_permissions`).
        """
        await asyncio.gather(
            supervise_qwen_forwarder(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="qwen-native-ui",
                auth=_runner_auth,
            ),
            supervise_qwen_approval_mirror(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                auth=_runner_auth,
            ),
        )

    _forwarder_task = asyncio.create_task(
        _supervise_qwen_native_bridges(),
        name=f"qwen-bridges-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created qwen terminal + forwarder/approval-mirror for session %s; task=%s",
        session_id,
        _forwarder_task.get_name(),
    )
    return terminal_view


async def _auto_create_kimi_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient | None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
    agent_spec: AgentSpec | ResolvedSpec | None = None,
) -> SessionResourceView:
    """
    Auto-create the Kimi TUI terminal for a kimi-native session.

    Launches ``kimi`` (no args → interactive TUI) in a runner-owned tmux pane,
    then advertises the pane's tmux socket+target so the kimi-native harness
    executor can inject web-UI turns into the same pane (tmux paste).

    The pane runs with a session-scoped ``KIMI_CODE_HOME`` (built by
    :func:`omnigent.kimi_native_credentials.build_kimi_session_home`) that
    mirrors the user's global ``kimi login`` (symlinked ``oauth`` / providers)
    and adds the Omnigent tool-policy hooks — a ``PreToolUse`` deny-gate and a
    ``PermissionRequest`` read-only surface dispatched to
    :mod:`omnigent.kimi_native_hook`. The hook subprocess reads its routing
    from ``hook_config.json`` in the bridge dir.

    A background forwarder (:func:`omnigent.kimi_native_forwarder.
    supervise_kimi_forwarder`) tails kimi's per-session ``wire.jsonl`` transcript
    and mirrors each user prompt + assistant reply into the Omnigent chat, so the
    response shows in the web UI — not only the embedded terminal. Tool calls and
    reasoning are NOT mirrored (the embedded terminal renders those). NO MCP
    plumbing (upstream kimi has no per-spawn MCP config).

    :param session_id: Session/conversation identifier.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: Runner session event publisher.
    :param server_client: Runner Omnigent server client (used only for the
        workspace snapshot read).
    :param ensure_comment_relay: Unused; kept for call-site parity with the
        other native auto-create helpers.
    :param agent_spec: Unused for now (model pinning via the kimi TUI is a
        follow-up); kept for call-site parity.
    :returns: Created terminal resource view.
    """
    del ensure_comment_relay, agent_spec
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.kimi_native import resolve_kimi_executable
    from omnigent.kimi_native_bridge import (
        bridge_dir_for_session_id,
        write_hook_config,
        write_tmux_target,
    )
    from omnigent.kimi_native_credentials import build_kimi_session_home
    from omnigent.kimi_native_forwarder import clear_kimi_bridge_state, supervise_kimi_forwarder
    from omnigent.runner._entry import _make_auth_token_factory

    bridge_dir = bridge_dir_for_session_id(session_id)
    # Stamp launch time before the TUI starts so the forwarder only adopts a kimi
    # session created for THIS launch. Tear down any prior forwarder + its line
    # offset so a re-created terminal tails the fresh wire log (mirrors cursor).
    launch_epoch_ms = int(time.time() * 1000)
    await _cancel_auto_forwarder_task(session_id)
    clear_kimi_bridge_state(bridge_dir)

    # ``_pi_native_launch_config`` is a generic session-snapshot reader
    # (workspace + terminal_launch_args); reused here, not Pi-specific.
    launch_config = await _pi_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = os.path.realpath(str(launch_config.workspace))
    kimi_command = resolve_kimi_executable()
    # No subcommand: bare ``kimi`` launches the interactive TUI. Pass-through
    # launch args (``omnigent kimi -- <args>``) are persisted on the session
    # snapshot and threaded here.
    kimi_args = list(launch_config.terminal_launch_args or [])

    # Wire the Omnigent tool-policy hooks: kimi reads a single
    # ``$KIMI_CODE_HOME/config.toml``, so point it at a session-scoped home that
    # mirrors the user's global kimi config (symlinked auth) plus a PreToolUse
    # deny-gate and a PermissionRequest read-only surface, both dispatched to
    # ``omnigent.kimi_native_hook``. The hook subprocess reads the server URL +
    # auth + session id from ``hook_config.json`` in the bridge dir, so persist
    # those first. The hook gets a one-shot token snapshot (a quick
    # request/reply, like claude-native's permission hook); ``None`` factory is
    # a safe no-op for local unauthenticated runs.
    server_url = os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767").rstrip("/")
    _auth_factory = _make_auth_token_factory()
    _auth_token = _auth_factory() if _auth_factory is not None else None
    _runner_headers = {"Authorization": f"Bearer {_auth_token}"} if _auth_token else {}
    write_hook_config(
        bridge_dir,
        server_url=server_url,
        headers=_runner_headers,
        session_id=session_id,
    )
    kimi_env = build_kimi_session_home(
        bridge_dir / "kimi-code-home",
        bridge_dir=bridge_dir,
    )
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="kimi",
        session_key="main",
        resource_role=KIMI_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=kimi_command,
            args=kimi_args,
            env=kimi_env,
            scrollback=100_000,
            tmux_allow_passthrough=True,
            tmux_start_on_attach=False,
        ),
    )
    # Advertise the tmux socket+target so the kimi-native harness executor can
    # inject web-UI messages into this same pane (tmux paste), wiring the web
    # chat box to the running TUI.
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is not None:
        instance = terminal_registry.get(session_id, "kimi", "main")
        if instance is not None and instance.running:
            write_tmux_target(
                bridge_dir,
                socket_path=instance.socket_path,
                tmux_target=instance.tmux_target,
            )
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )
    # Mirror the kimi TUI transcript into the Omnigent chat: tail the per-session
    # wire.jsonl and POST each user/assistant turn, so the reply renders in the
    # web UI (not just the embedded pane). Reuses the shared auto-forwarder
    # registry so terminal teardown / stop cancels it.
    _forwarder_task = asyncio.create_task(
        supervise_kimi_forwarder(
            base_url=server_url,
            headers=_runner_headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            kimi_home=bridge_dir / "kimi-code-home",
            workspace=workspace,
            launch_epoch_ms=launch_epoch_ms,
        ),
        name=f"kimi-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info("Auto-created kimi terminal + forwarder for session %s", session_id)
    return terminal_view


async def _auto_create_codex_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    bundle_dir: Path | None = None,
    skills_filter: str | list[str] = "all",
    agent_spec: AgentSpec | ResolvedSpec | None = None,
    server_client: httpx.AsyncClient | None = None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """
    Auto-create a Codex terminal for a codex-native session.

    Called when the runner receives a codex-native session via
    ``POST /v1/sessions`` or an explicit terminal ensure request and no
    terminal exists yet. Mirrors :func:`_auto_create_claude_terminal`: it
    boots a Codex app-server, registers the Codex TUI as a streamable
    terminal resource attached to that app-server, then runs the transcript
    forwarder so the chat and terminal share one thread.

    Fresh sessions launch without a thread id so the TUI owns thread
    creation; resume sessions launch with the persisted Codex thread id.
    The runner does not pre-create a thread, because ``codex resume`` of a
    thread with no rollout yet exits the TUI (leaving a dead pane).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param resource_registry: Session resource registry used to launch
        the Codex terminal resource.
    :param publish_event: The runner's per-session SSE emitter, used to
        surface the new terminal on the live stream (the Omnigent relay
        republishes it to the web UI) so the Terminal toggle enables
        without a refresh.
    :param bundle_dir: Materialized agent-bundle root when the session's
        agent ships a ``skills/`` directory, resolved by the caller
        (which has the runner's spec resolver). Its skills are linked
        into the per-bridge ``$CODEX_HOME/skills/`` before the
        app-server boots so the native Codex discovers them — matching
        the wrapped ``codex`` executor. ``None`` exposes no bundle skills.
    :param skills_filter: The agent spec's ``skills_filter`` (``"all"``
        / ``"none"`` / list of skill names), honoured when populating
        ``$CODEX_HOME/skills/``. Defaults to ``"all"``.
    :param agent_spec: Optional resolved agent spec for the session.
        When provided, its executor model is used as the Codex app-server
        default, e.g. ``"gpt-5.4-mini"``.
    :param server_client: Runner's Omnigent server HTTP client. Used to read
        persisted launch args and the native thread id.
    :returns: The created terminal resource view.
    """
    import socket as _socket
    from pathlib import Path

    from omnigent.codex_native_app_server import (
        CodexAppServerClient,
        build_codex_native_server,
        build_codex_remote_args,
        codex_session_meta_model_provider,
        codex_terminal_env,
        preload_codex_thread_for_resume,
        resolve_native_codex_launch,
    )
    from omnigent.codex_native_bridge import (
        clear_bridge_state,
        codex_home_for_bridge_dir,
        prepare_bridge_dir,
        socket_path_for_bridge_dir,
    )
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    launch_config = await _codex_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    original_external_session_id = launch_config.external_session_id
    workspace = str(launch_config.workspace)
    bridge_dir = prepare_bridge_dir(session_id)
    socket_path = socket_path_for_bridge_dir(bridge_dir)
    codex_home = codex_home_for_bridge_dir(bridge_dir)
    # Route across all offerings: a configured provider (omnigent setup),
    # a Databricks ucode profile from provider config, or Codex's own
    # login — parity with the in-process codex harness and the CLI path.
    # Resolved before the fork/cold-resume branches below so any rollout
    # synthesis can stamp session_meta.model_provider with the provider
    # this launch actually routes through.
    default_model = launch_config.model_override or _codex_native_model_from_spec(agent_spec)
    _codex_launch = resolve_native_codex_launch(model=default_model)
    _session_meta_provider = codex_session_meta_model_provider(_codex_launch)
    from omnigent.inner.codex_executor import _find_codex_cli

    _codex_cli_path = _find_codex_cli()
    # Cancel any surviving forwarder first so its teardown closes the OLD app-server,
    # not the one registered below — and so it can't mirror alongside the new one.
    await _cancel_auto_forwarder_task(session_id)
    clear_bridge_state(bridge_dir)

    # Forked clone with no native thread of its own yet: clone the SOURCE's
    # local Codex rollout into the clone's OWN CODEX_HOME under a thread id
    # we mint (rewriting session_meta.id + the structural cwd fields), then
    # flip launch_config so the normal resume path below launches
    # ``codex resume <our_thread_id>``. The app-server boots from this
    # CODEX_HOME just below, so the rollout must be written first. Only
    # viable when the source rollout exists on THIS host (same-host fork —
    # CUJ 1 same-user); else fall through and launch fresh. This mirrors the
    # claude-native fork-resume branch in _auto_create_claude_terminal. See
    # designs/FORK_SESSION_UX.md.
    if (
        launch_config.external_session_id is None
        and launch_config.fork_source_external_id is not None
        and launch_config.fork_source_id is not None
    ):
        from omnigent.codex_native import _clone_codex_rollout, _mint_codex_thread_id

        target_thread_id = _mint_codex_thread_id()
        clone_workspace = Path(workspace).resolve()
        try:
            cloned_rollout = _clone_codex_rollout(
                source_session_id=launch_config.fork_source_id,
                source_thread_id=launch_config.fork_source_external_id,
                target_thread_id=target_thread_id,
                clone_codex_home=codex_home,
                clone_workspace=clone_workspace,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            cloned_rollout = None
            _logger.warning(
                "Could not clone source rollout for forked codex clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Codex terminal fork-resume decision: session=%s source_id=%s source_ext=%s "
            "our_thread=%s clone_workspace=%s cloned_rollout=%s",
            session_id,
            launch_config.fork_source_id,
            launch_config.fork_source_external_id,
            target_thread_id,
            clone_workspace,
            str(cloned_rollout) if cloned_rollout is not None else None,
        )
        if cloned_rollout is not None:
            # Resume our OWN clone via the existing resume path below.
            launch_config = dataclasses.replace(
                launch_config, external_session_id=target_thread_id
            )
            # Record the assigned thread id now so Omnigent reflects the clone's
            # own Codex thread immediately and a later relaunch resumes it.
            # Best-effort, like the claude-native fork branch.
            if server_client is not None:
                try:
                    await server_client.patch(
                        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                        json={"external_session_id": target_thread_id},
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    # The clone resumes via the known-thread forwarder (no
                    # discovery), so nothing re-captures the id later: it stays
                    # unset on the Omnigent session and a future relaunch of this
                    # clone will start fresh rather than resume the cloned
                    # rollout. The cloned rollout itself is already on disk, so
                    # the current launch still resumes with history.
                    _logger.warning(
                        "Could not pre-set external_session_id for forked codex clone %s; "
                        "it will remain unset and a future relaunch will start fresh",
                        session_id,
                        exc_info=True,
                    )
    elif (
        launch_config.external_session_id is None
        and launch_config.fork_carry_history
        and launch_config.fork_source_external_id is None
        and server_client is not None
    ):
        # Forked clone bound to a codex-native target with NO source
        # rollout to clone (an SDK or cross-family source): build the clone's
        # rollout from its OWN copied Omnigent items under a thread id we mint, then flip
        # launch_config so the resume path below launches ``codex resume
        # <our_thread_id>``. Reuses the same server-items→rollout converter
        # the cross-machine cold resume uses, so the clone opens with the
        # prior conversation (messages + tool history) as Codex context.
        # Best-effort: launch fresh on failure. See designs/FORK_SESSION_UX.md.
        from omnigent.codex_native import (
            _ensure_local_codex_resume_rollout,
            _mint_codex_thread_id,
        )

        target_thread_id = _mint_codex_thread_id()
        clone_workspace = Path(workspace).resolve()
        try:
            built_rollout = await _ensure_local_codex_resume_rollout(
                server_client,
                session_id=session_id,
                external_session_id=target_thread_id,
                codex_home=codex_home,
                workspace=clone_workspace,
                model_provider=_session_meta_provider,
                codex_path=_codex_cli_path,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            built_rollout = None
            _logger.warning(
                "Could not build rollout from items for forked codex clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Codex terminal fork-rebuild decision: session=%s our_thread=%s "
            "clone_workspace=%s built_rollout=%s",
            session_id,
            target_thread_id,
            clone_workspace,
            str(built_rollout) if built_rollout is not None else None,
        )
        if built_rollout is not None:
            launch_config = dataclasses.replace(
                launch_config, external_session_id=target_thread_id
            )
            try:
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": target_thread_id},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                _logger.warning(
                    "Could not pre-set external_session_id for forked codex clone %s; "
                    "it will remain unset and a future relaunch will start fresh",
                    session_id,
                    exc_info=True,
                )

    if launch_config.external_session_id is not None and original_external_session_id is not None:
        from omnigent.codex_native import _ensure_local_codex_resume_rollout

        if server_client is None:
            raise RuntimeError("server_client is required for Codex cold resume.")
        await _ensure_local_codex_resume_rollout(
            server_client,
            session_id=session_id,
            external_session_id=launch_config.external_session_id,
            codex_home=codex_home,
            workspace=Path(workspace).resolve(),
            model_provider=_session_meta_provider,
            codex_path=_codex_cli_path,
        )
    # Link the bundle's skills into the per-bridge CODEX_HOME before the
    # app-server boots — Codex discovers ``$CODEX_HOME/skills/<name>/``
    # at startup. This is the codex-native mirror of the wrapped codex
    # executor's skill population; the native CLI otherwise sees zero
    # bundled skills. Best-effort: a skill-link failure must not break
    # the terminal launch.
    from omnigent.inner.codex_executor import populate_codex_skills_from_bundle

    try:
        populate_codex_skills_from_bundle(codex_home, bundle_dir, skills_filter)
    except OSError:
        _logger.warning(
            "Could not populate codex skills for %s; native Codex will see no bundled skills",
            session_id,
            exc_info=True,
        )

    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        codex_ws_port = s.getsockname()[1]
    codex_ws_url = f"ws://127.0.0.1:{codex_ws_port}"

    # Write the minimal MCP bridge config so serve-mcp can boot, and
    # start the tool relay so tool_relay.json is on disk before codex
    # launches its MCP server. This mirrors the claude-native relay
    # start in ``create_session_terminal``. The relay is started here
    # (not in ``_ensure_comment_relay_started``) because that helper
    # is scoped inside ``create_routes`` and not reachable at module
    # level. The ``_run_turn_bg`` fallback path covers sessions whose
    # terminal was created outside this function.
    from omnigent.codex_native_bridge import (
        codex_mcp_config_overrides,
        write_mcp_bridge_config,
    )

    write_mcp_bridge_config(bridge_dir)
    mcp_overrides = codex_mcp_config_overrides(bridge_dir)

    # Omnigent coordinates for the codex-native policy hook. The hook runs as a
    # separate subprocess that POSTs tool calls to /policies/evaluate, so
    # it reads a one-shot token snapshot from policy_hook.json — same as
    # the claude-native PermissionRequest hook on this host-spawned path.
    from omnigent.runner._entry import _make_auth_token_factory

    _policy_auth_factory = _make_auth_token_factory()
    _policy_auth_token = _policy_auth_factory() if _policy_auth_factory is not None else None
    policy_headers = (
        {"Authorization": f"Bearer {_policy_auth_token}"} if _policy_auth_token else {}
    )

    app_server = build_codex_native_server(
        socket_path=socket_path,
        codex_home=codex_home,
        cwd=Path(workspace),
        model=_codex_launch.model,
        profile=_codex_launch.profile,
        extra_config_overrides=[*_codex_launch.config_overrides, *mcp_overrides],
        bridge_dir=bridge_dir,
        ap_server_url=launch_config.policy_server_url,
        ap_auth_headers=policy_headers,
    )
    app_server.listen_url = codex_ws_url
    await app_server.start()
    _AUTO_CODEX_APP_SERVERS[session_id] = app_server

    event_client = CodexAppServerClient(
        ws_url=codex_ws_url,
        client_name="omnigent-codex-native-auto",
    )
    if launch_config.external_session_id is None:
        try:
            # Connect the listener BEFORE launching the TUI so it observes the
            # ``thread/started`` the TUI emits on startup (the client buffers
            # notifications, so there is no created-before-listening race).
            await event_client.connect()
        except Exception:
            # connect() may have half-opened the ws before the initialize
            # handshake failed, so close the listener too — not just the
            # app-server.
            with contextlib.suppress(Exception):
                await event_client.close()
            await app_server.close()
            _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
            raise
    else:
        from omnigent.codex_native_bridge import CodexNativeBridgeState, write_bridge_state

        await preload_codex_thread_for_resume(codex_ws_url, launch_config.external_session_id)
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=session_id,
                socket_path=codex_ws_url,
                thread_id=launch_config.external_session_id,
                codex_home=str(codex_home),
            ),
        )

    # Register the Codex TUI as a streamable terminal resource attached to
    # the app-server started above (``--remote`` over its loopback ws
    # endpoint). Without this the session can have a working chat path
    # (driven by the forwarder) but no terminal to attach to, unlike
    # claude-native, whose terminal IS the agent process. On failure, close
    # the listener and app-server here: the background forwarder task (which
    # otherwise owns their teardown) has not been created yet.
    # Inherit the agent's os_env so its sandbox (e.g. ``type: none``),
    # egress_rules and env_passthrough are honoured. Without ``sandbox`` here
    # and ``parent_os_env`` below, launch_terminal falls back to
    # _default_sandbox_for_platform (linux_bwrap), overriding the YAML config.
    agent_os_env = _agent_os_env_from_spec(agent_spec)
    try:
        terminal_view = await resource_registry.launch_auxiliary_terminal(
            session_id=session_id,
            terminal_name="codex",
            session_key="main",
            resource_role=CODEX_NATIVE_TERMINAL_ROLE,
            parent_os_env=agent_os_env,
            spec=TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type="caller_process",
                    cwd=workspace,
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=app_server.codex_path,
                # Fresh sessions pass no thread id so the TUI creates the
                # thread and the background task adopts it. Resume sessions
                # pass the persisted external_session_id so the runner-owned
                # TUI reopens the existing app-server thread.
                args=build_codex_remote_args(
                    codex_args=tuple(launch_config.terminal_launch_args or ()),
                    thread_id=launch_config.external_session_id,
                    remote_url=codex_ws_url,
                    # The --remote TUI loads its own config and does not
                    # inherit the app-server's -c flags; pass the same
                    # provider/model overrides so it resolves the
                    # Omnigent provider instead of falling back to the
                    # OpenAI built-in (which would force the first-run
                    # login screen and block thread creation).
                    config_overrides=tuple(app_server.config_overrides),
                ),
                env=codex_terminal_env(app_server),
                # Match the local ``omnigent codex`` terminal scrollback.
                scrollback=100_000,
                # Enable tmux passthrough so the Codex TUI's escape sequences
                # reach the web xterm.
                tmux_allow_passthrough=True,
                # Start the TUI at creation rather than on first attach,
                # mirroring claude-native. Deferring to attach (the local CLI
                # default) means the full-screen TUI cold-starts the instant
                # the web UI attaches over the runner tunnel; that initial
                # render burst starves the tunnel ping/pong and the host
                # recycles the unresponsive runner (the "runner
                # death on terminal attach" class). Starting now lets the TUI settle
                # in the detached tmux pane (no tunnel traffic) and create its
                # thread before anyone attaches.
                tmux_start_on_attach=False,
            ),
        )
        publish_event(
            session_id,
            {
                "type": "session.resource.created",
                "resource": session_resource_view_to_dict(terminal_view),
            },
        )
    except Exception:
        await event_client.close()
        await app_server.close()
        _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        raise

    # Adopt the thread the fresh TUI creates and run the forwarder in the
    # background, so session creation never blocks on TUI startup.
    _forwarder_task = asyncio.create_task(
        (
            _codex_discover_thread_and_forward(
                session_id=session_id,
                bridge_dir=bridge_dir,
                codex_ws_url=codex_ws_url,
                codex_home=codex_home,
                event_client=event_client,
            )
            if launch_config.external_session_id is None
            else _codex_forward_known_thread(
                session_id=session_id,
                bridge_dir=bridge_dir,
                codex_ws_url=codex_ws_url,
                thread_id=launch_config.external_session_id,
            )
        ),
        name=f"codex-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)

    # Start the relay now (into codex's serve-mcp bridge dir) so tool_relay.json
    # is on disk and the relay recorded before codex connects on its first turn:
    # the first-turn `_ensure_comment_relay_started` then fast-paths, avoiding
    # the ~30s stall (see its docstring for the lazy-bridge / await_notify=False
    # rationale).
    if ensure_comment_relay is not None:
        await ensure_comment_relay(session_id, explicit_bridge_dir=bridge_dir, await_notify=False)

    _logger.info(
        "Auto-created codex terminal + forwarder for session %s",
        session_id,
    )
    return terminal_view


async def _codex_discover_thread_and_forward(
    *,
    session_id: str,
    bridge_dir: Path,
    codex_ws_url: str,
    codex_home: Path,
    event_client: CodexAppServerClient,
) -> None:
    """
    Adopt the fresh Codex TUI's thread, then mirror it into the Omnigent session.

    Runs as a background task spawned by :func:`_auto_create_codex_terminal`
    so session creation never blocks on TUI startup. Waits for the fresh TUI
    to create its app-server thread, persists the bridge state (so the Codex
    executor's bridge-state retry can inject web-UI turns into that same
    thread), then runs the transcript forwarder for the session's lifetime.

    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory for this session.
    :param codex_ws_url: App-server loopback ws URL the TUI and forwarder
        attach to, e.g. ``"ws://127.0.0.1:9876"``. Persisted as the bridge
        state's ``socket_path`` (the executor reads it to reach the
        app-server) and re-persisted by the forwarder's thread-rotation
        path so a native ``/clear`` keeps the ws:// transport.
    :param codex_home: Per-session private ``CODEX_HOME`` path.
    :param event_client: Connected app-server listener that will observe the
        TUI's ``thread/started``; reused to subscribe the forwarder.
    """
    from omnigent.codex_native_bridge import (
        CodexNativeBridgeState,
        write_bridge_startup_error,
        write_bridge_state,
    )
    from omnigent.codex_native_forwarder import (
        supervise_forwarder,
        wait_for_thread_started,
    )
    from omnigent.runner._entry import (
        _make_auth_token_factory,
        _RunnerDatabricksAuth,
    )

    try:
        try:
            thread_id = await wait_for_thread_started(event_client)
        except (TimeoutError, RuntimeError) as exc:
            # Expected failure modes of wait_for_thread_started: the TUI exited
            # at startup, or the event stream ended before a thread was
            # created. Stop forwarding (cleanup runs in ``finally``); any other
            # error is a bug and propagates.
            _logger.exception(
                "Codex TUI never started a thread for %s; chat will not forward",
                session_id,
            )
            # Bridge state is never written here; leave the real cause for the executor (#59).
            cause = (
                "startup timed out"
                if isinstance(exc, TimeoutError)
                else "event stream ended before a thread was created"
            )
            write_bridge_startup_error(
                bridge_dir,
                f"Codex app-server never started a thread ({cause}: "
                f"{type(exc).__name__}). See the runner log near 'native-codex "
                "routing' for the resolved provider/model.",
            )
            return

        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=session_id,
                socket_path=codex_ws_url,
                thread_id=thread_id,
                codex_home=str(codex_home),
            ),
        )

        server_url = _required_runner_env("RUNNER_SERVER_URL")
        auth_factory = _make_auth_token_factory()
        auth_token = auth_factory() if auth_factory is not None else None
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

        # Mirror the discovered Codex thread id onto the Omnigent session as its
        # external_session_id, the same way claude-native records its
        # captured session id. This is what makes the session forkable with
        # history: fork_conversation stamps
        # ``omnigent.fork.source_external_session_id`` from
        # external_session_id, and the forked clone's runner clones this
        # thread's rollout from it (see _clone_codex_rollout). Without it a
        # host-spawned codex session has no recorded thread id, so a fork
        # would resume fresh. Best-effort: a transient Omnigent failure here still
        # leaves chat streaming working — only fork-history carry-over
        # degrades.
        try:
            async with httpx.AsyncClient(
                base_url=server_url,
                headers=headers,
                auth=_RunnerDatabricksAuth(auth_factory),
                timeout=httpx.Timeout(10.0),
            ) as _ext_client:
                _ext_resp = await _ext_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": thread_id},
                )
            if _ext_resp.status_code >= 400:
                _logger.warning(
                    "AP rejected codex external_session_id PATCH (%s); session=%s thread=%s — "
                    "a fork of this session will resume fresh",
                    _ext_resp.status_code,
                    session_id,
                    thread_id,
                )
        except httpx.HTTPError:
            _logger.warning(
                "Could not record codex external_session_id for %s; a fork of this "
                "session will resume fresh",
                session_id,
                exc_info=True,
            )

        await supervise_forwarder(
            base_url=server_url,
            headers=headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            app_server_url=codex_ws_url,
            thread_id=thread_id,
            client=event_client,
            auth=_RunnerDatabricksAuth(auth_factory),
        )
    finally:
        # Tear down the listener and the per-session app-server whenever
        # forwarding ends — discovery failed, the app-server connection dropped
        # (``supervise_forwarder`` returned), or the task was cancelled on
        # session teardown. ``supervise_forwarder`` also closes ``event_client``
        # in its own ``finally``; ``close()`` is idempotent. The app-server
        # subprocess is ours to stop, else it orphans one process per session.
        # Pop first so the dict never holds a closed reference.
        leftover_app_server = _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        with contextlib.suppress(Exception):
            await event_client.close()
        if leftover_app_server is not None:
            with contextlib.suppress(Exception):
                await leftover_app_server.close()


async def _codex_forward_known_thread(
    *,
    session_id: str,
    bridge_dir: Path,
    codex_ws_url: str,
    thread_id: str,
) -> None:
    """
    Forward a runner-owned Codex terminal that resumes an existing thread.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param bridge_dir: Native Codex bridge directory for this session.
    :param codex_ws_url: App-server loopback URL, e.g.
        ``"ws://127.0.0.1:9876"``.
    :param thread_id: Existing Codex app-server thread id, e.g.
        ``"thread_abc123"``.
    :returns: None. Runs until cancelled or the app-server connection
        closes.
    """
    from omnigent.codex_native_forwarder import supervise_forwarder
    from omnigent.runner._entry import (
        _make_auth_token_factory,
        _RunnerDatabricksAuth,
    )

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    auth_factory = _make_auth_token_factory()
    auth_token = auth_factory() if auth_factory is not None else None
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
    try:
        await supervise_forwarder(
            base_url=server_url,
            headers=headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            app_server_url=codex_ws_url,
            thread_id=thread_id,
            auth=_RunnerDatabricksAuth(auth_factory),
        )
    finally:
        leftover_app_server = _AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        if leftover_app_server is not None:
            with contextlib.suppress(Exception):
                await leftover_app_server.close()


async def _run_antigravity_reader(
    *,
    base_url: str,
    headers: dict[str, str],
    auth: httpx.Auth | None,
    session_id: str,
    bridge_dir: Path,
) -> None:
    """
    Run the agy RPC streaming reader + interaction bridge for one session.

    This is the host-spawned (web-UI) read path that replaces the transcript
    forwarder: the runner-owned tmux terminal IS the agy agent process, and this
    reader is the single writer mirroring agy's conversation into the session.

    A thin wrapper over the shared
    :func:`omnigent.antigravity_native_reader.run_reader_with_bridge` (used by both
    this runner path and the CLI ``omnigent antigravity`` attach fallback); it
    exists only to name the runner-side entry point and keep its task name stable
    for the single-instance task registry. See the helper for the full wiring
    (client lifecycle, elicitation bridge, ``supervise_reader`` spawn).

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param headers: Auth headers for the Omnigent client (best-effort static
        bearer; ``auth`` carries the refresh-capable flow).
    :param auth: Refresh-capable httpx auth flow, or ``None`` when unauthenticated.
    :param session_id: Omnigent conversation id to mirror into, e.g.
        ``"conv_abc123"``.
    :param bridge_dir: Native Antigravity bridge directory for this session.
    :returns: None. Runs until cancelled.
    """
    from omnigent.antigravity_native_reader import run_reader_with_bridge

    await run_reader_with_bridge(
        base_url=base_url,
        headers=headers,
        auth=auth,
        session_id=session_id,
        bridge_dir=bridge_dir,
    )


async def _auto_create_antigravity_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, object]], None],
    *,
    server_client: httpx.AsyncClient | None = None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """
    Auto-create the native Antigravity (agy) terminal for a session.

    Called when the runner receives an antigravity-native session via
    ``POST /v1/sessions`` or an explicit terminal-ensure request and no
    terminal exists yet — the host-spawned (web-UI) case where no CLI
    client is present to launch the terminal itself.

    Unlike codex-native there is **no app-server**: agy self-hosts its
    control surface, so this boots agy directly in a runner-owned tmux
    terminal and runs the native RPC streaming reader server-side so the
    web chat view mirrors agy's conversation. It is structurally closer to
    :func:`_auto_create_claude_terminal` (the terminal IS the agent
    process and the reader is the single conversation writer) than to the
    codex path. The terminal starts agy immediately
    (``tmux_start_on_attach=False``) — UNLIKE the CLI launch in
    :func:`omnigent.antigravity_native._launch_antigravity_terminal`, which
    keeps ``start_on_attach=True`` for its human-TTY driver: this host-spawned
    path has no TTY, and the executor must be able to drive agy's first turn
    over tmux whether or not a web client has opened the Terminal panel (see
    the ``tmux_start_on_attach`` note on the spec below).

    **Permissions are web-attended, not headless.** The web client attaches
    to the agy pane through the runner tunnel and answers agy's
    ``request-review`` TUI prompt there, so the launch is treated as
    *attended* (``headless=False``). Auto-bypass comes only from the user's
    persisted ``terminal_launch_args`` (which carry
    ``--dangerously-skip-permissions`` when the user asked for bypass) —
    the same pass-through mechanism codex/claude use. A server-spawned
    launch must NOT key headlessness on the runner process's (absent) TTY,
    which would silently disable the per-tool prompt for a watching web
    user.

    Fresh sessions launch with no ``--conversation``: the runner cold-starts
    the conversation over connect-RPC (11a) so the reader binds agy's real id
    directly. Resume sessions launch ``--conversation <external_session_id>``
    (agy's real id, persisted by a prior run).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param resource_registry: Session resource registry used to launch the
        agy terminal resource.
    :param publish_event: The runner's per-session SSE emitter, used to
        surface the new terminal on the live stream so the web UI's Terminal
        toggle enables without a refresh.
    :param server_client: Runner's Omnigent server HTTP client. Used to read
        the persisted workspace, launch args, and the discovered agy
        conversation id (``external_session_id``) for resume.
    :param ensure_comment_relay: The runner's relay starter
        (``_ensure_comment_relay_started``). When provided, the Omnigent MCP
        relay is started against this session's bridge dir before launch so the
        wrapped agy sees the ``sys_*`` tools (#1194). ``None`` skips relay wiring
        (the ``_run_turn_bg`` first-turn fallback re-ensures it).
    :returns: The created terminal resource view.
    :raises RuntimeError: If the session snapshot or required runner env is
        unavailable.
    """
    from omnigent.antigravity_native_bridge import (
        ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
        AntigravityNativeBridgeState,
        clear_bridge_state,
        ensure_agy_onboarding_complete,
        prepare_bridge_dir,
        seed_isolated_agy_home,
        write_bridge_state,
        write_mcp_config,
        write_tmux_target,
    )
    from omnigent.antigravity_native_launch import build_agy_launch
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec

    if server_client is None:
        raise RuntimeError("server_client is required for runner-owned Antigravity terminals.")
    snapshot = await _session_payload_for_host_spawn_check(server_client, session_id)
    if snapshot is None:
        raise RuntimeError(f"Could not fetch Antigravity launch config for {session_id!r}.")

    session_workspace = snapshot.get("workspace")
    if session_workspace is not None and (
        not isinstance(session_workspace, str) or not session_workspace
    ):
        raise RuntimeError(f"Invalid workspace for Antigravity session {session_id!r}.")
    workspace = _codex_session_workspace(session_workspace)

    # The user's pass-through agy args (e.g. ``--dangerously-skip-permissions``)
    # persisted by the CLI/web launch. Appended verbatim — bypass only happens
    # when the user put the flag here (see the docstring on web-attended perms).
    raw_launch_args = snapshot.get("terminal_launch_args")
    terminal_launch_args: tuple[str, ...] = ()
    if raw_launch_args is not None:
        if not (
            isinstance(raw_launch_args, list) and all(isinstance(a, str) for a in raw_launch_args)
        ):
            raise RuntimeError(
                f"Invalid terminal_launch_args for Antigravity session {session_id!r}."
            )
        terminal_launch_args = tuple(raw_launch_args)

    # agy's real (discovered) conversation id, persisted by a prior run's
    # forwarder. Present → resume; absent → fresh launch (the forwarder
    # discovers and persists the id).
    external_session_id = snapshot.get("external_session_id")
    if external_session_id is not None and (
        not isinstance(external_session_id, str) or not external_session_id
    ):
        raise RuntimeError(f"Invalid external_session_id for Antigravity session {session_id!r}.")
    resume = bool(external_session_id)

    # agy model label from the session's model_override (None lets agy default).
    _model_override = snapshot.get("model_override")
    model = _model_override if isinstance(_model_override, str) and _model_override else None

    # Bridge id mirrors the CLI/harness derivation: the session's bridge-id
    # label when present (so the spawn env built by
    # ``build_antigravity_native_spawn_env`` and the reader share one dir),
    # else the session id.
    labels = snapshot.get("labels")
    bridge_id = session_id
    if isinstance(labels, dict):
        _bid = labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY)
        if isinstance(_bid, str) and _bid:
            bridge_id = _bid

    # Cancel any surviving reader BEFORE clearing its conversation state, else it
    # keeps mirroring with stale state alongside the one spawned below (mirrors the
    # claude/codex auto-create teardown ordering).
    await _cancel_auto_forwarder_task(session_id)
    bridge_dir = prepare_bridge_dir(bridge_id)
    # Clear stale turn/conversation state so the reader binds this run's real agy
    # conversation id (the cold-start mints it below) instead of a prior run's.
    clear_bridge_state(bridge_dir)

    # Pre-accept agy's first-run onboarding wizard (HOME-global) before launch:
    # a host-spawned agy terminal has no TTY to answer it and would hang with a
    # blank web UI. Mirrors the ``ensure_claude_workspace_trusted`` seed on the
    # Claude auto-create path. Idempotent; offloaded to a thread (file I/O).
    await asyncio.to_thread(ensure_agy_onboarding_complete)

    argv, env_overrides = build_agy_launch(
        conversation_id=external_session_id if resume else None,
        model=model,
        resume=resume,
        # Web-attended: a web client drives agy's request-review prompt over the
        # tunnel, so this is NOT headless. Bypass comes only via the pass-through
        # args below (see docstring). permission_mode is left unset for the same
        # reason — the runner has no separate per-tool mode to map here.
        permission_mode=None,
        headless=False,
        extra_args=terminal_launch_args,
    )

    # Wire the Omnigent MCP relay so the wrapped agy gets the sys_* tools
    # (spawn sub-agent sessions, drive Omnigent terminals, list agents/models,
    # sys_os_*) — the only native harness that otherwise lacks them (#1194).
    # agy has no --mcp-config flag and ignores ANTIGRAVITY_* env knobs; it loads
    # MCP servers ONLY from the HOME-global ~/.gemini/config/mcp_config.json. To
    # avoid clobbering the user's interactive agy config (and the concurrency
    # footgun of a single shared file), launch agy under a per-session ISOLATED
    # HOME seeded with a copy of the user's OAuth token + onboarding state and a
    # bridge-scoped mcp_config.json. The relay subprocess is the same shared
    # ``serve-mcp`` claude/codex/cursor use. Offloaded to a thread (file I/O) and
    # done BEFORE the terminal launch so agy sees the config on its first MCP scan.
    await asyncio.to_thread(write_mcp_config, bridge_dir)
    env_overrides = {
        **env_overrides,
        **await asyncio.to_thread(seed_isolated_agy_home, bridge_dir),
    }
    # Start the shared comment/sys_* relay against THIS session's bridge dir before
    # launch so its tool_relay.json is on disk when agy first scans the MCP server.
    # ``await_notify=False``: agy starts its MCP client lazily, so awaiting the
    # tools/list_changed notification would stall the launch (mirrors codex). The
    # _run_turn_bg first-turn fallback re-ensures this for any session whose
    # terminal was launched outside this path.
    if ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            bridge_id=bridge_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    _logger.info(
        "Antigravity terminal auto-create starting: session=%s workspace=%s resume=%s "
        "bridge_dir=%s args_count=%d",
        session_id,
        workspace,
        resume,
        bridge_dir,
        len(argv) - 1,
    )

    # Resolve every fallible input BEFORE registering the terminal resource, so a
    # failure here (missing RUNNER_SERVER_URL, an unwritable bridge dir) leaves no
    # reader-less terminal behind. A registered-but-reader-less terminal never
    # self-heals: a later ensure sees the existing runner-owned terminal and
    # returns without starting a reader, so the web UI stays blank. Only the
    # non-raising terminal-bound work (tmux pane lookup, task spawn) runs after
    # ``launch_terminal``.
    #
    # Reconstruct the server URL + refresh-capable auth from the runner's own
    # environment, exactly like ``_auto_create_claude_terminal``.
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    server_url = _required_runner_env("RUNNER_SERVER_URL")
    auth_factory = _make_auth_token_factory()
    auth_token = auth_factory() if auth_factory is not None else None
    runner_headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

    # Seed bridge state with the id known so far (the real id on resume; on a
    # fresh launch a placeholder the cold-start below replaces with agy's real
    # cascade id once agy is live, so the RPC reader binds the real conversation).
    # No durable read cursor is seeded: the reader keeps an in-memory seen-set
    # (the transcript forwarder's cursor was retired in the Task 12 cutover).
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id=session_id,
            conversation_id=external_session_id or _mint_runner_agy_conversation_id(),
        ),
    )

    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="antigravity",
        session_key="main",
        resource_role=ANTIGRAVITY_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            # caller_process + sandbox:none mirrors the antigravity-native agent
            # spec (_materialize_antigravity_agent_spec). The terminal IS the
            # agent, so this is a REQUIRED terminal (its death ends the session),
            # like claude/codex/pi native. An explicit sandbox is mandatory:
            # without it launch_required_terminal falls back to
            # _default_sandbox_for_platform (linux_bwrap), which fails in the
            # unprivileged uid-1000 host pods (bwrap needs userns) — agy needs
            # no OS sandbox here (its own --sandbox flag governs tool access).
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(workspace),
                sandbox=OSEnvSandboxSpec(type="none"),
            ),
            command=argv[0],
            args=list(argv[1:]),
            env=env_overrides,
            # Match the local ``omnigent antigravity`` terminal scrollback.
            scrollback=100_000,
            # Let agy's full-screen TUI escape sequences reach the web xterm.
            tmux_allow_passthrough=True,
            # Start agy immediately (NOT on first client attach), matching the
            # claude/codex auto-create paths. This host-spawned web flow has no
            # human TTY, and agy must be live before any client attaches: the
            # cold-start below mints agy's cascade over connect-RPC, the RPC reader
            # mirrors its conversation, and the executor delivers web turns over
            # ``SendUserCascadeMessage`` — all of which need agy running whether or
            # not the user has opened the Terminal panel. agy runs headlessly in
            # the tmux pane (the pty is enough; verified against agy 1.0.10), and a
            # later web attach simply views the already-running pane. (The CLI
            # ``omnigent antigravity`` path keeps start-on-attach: there a human
            # TTY is the driver.)
            tmux_start_on_attach=False,
        ),
    )

    # Resolve THIS session's own agy tmux pane (socket + target). Used to scope
    # the cold-start's ``StartCascade`` port to the agy running under this
    # session's pane (so a multi-agy host cannot cross-bind to a foreign agy) AND,
    # below, for the first-turn TUI bootstrap. The RPC reader discovers its own
    # connect-RPC port from bridge state (cascade id → port), so it needs no pane;
    # the pane is still required so the executor can type the FIRST web turn into
    # agy's TUI before any conversation exists. ``_terminal_tmux_pane`` is fully
    # defensive (never raises for a valid or absent terminal), so NOTHING fallible
    # runs between the terminal registration above and the reader below — a
    # partial failure can never leave a registered terminal without a reader
    # (which a later ensure would see and return 200 for, never self-healing).
    tmux_socket, tmux_target = _terminal_tmux_pane(
        resource_registry, session_id, "antigravity", "main"
    )

    # Cold-start the conversation over connect-RPC on a FRESH launch so the
    # executor's turn-1 has a real cascade id (no send-keys, no waiting for the
    # TUI to lazily mint one): the runner mints the cascade via ``StartCascade``,
    # writes that real id into bridge state (replacing the ``agy_conv_*``
    # placeholder seeded above), and PATCHes it onto the session as
    # ``external_session_id`` so a later ``--resume`` continues it. The pane
    # (resolved above) scopes the ``StartCascade`` port to THIS session's agy.
    # Resume launches already hold agy's real id (``external_session_id``), so
    # cold-starting would create a second empty conversation — skip it.
    # Best-effort and NON-RAISING (see ``_cold_start_agy_conversation``): a failure
    # leaves the placeholder and the reader simply keeps polling discovery until a
    # real id appears, so this stays inside the "nothing fallible between terminal
    # registration and reader start" window. Done BEFORE the reader spawns so the
    # reader binds the real id.
    if not resume:
        await _cold_start_agy_conversation(
            bridge_dir,
            session_id,
            server_client=server_client,
            tmux_socket=tmux_socket,
            tmux_target=tmux_target,
            timeout_s=_AGY_COLD_START_PORT_TIMEOUT_S,
        )

    # Start the RPC streaming reader + interaction bridge server-side (the read
    # path that replaced the retired transcript forwarder). It mirrors agy's
    # conversation over connect-RPC and surfaces WAITING interactions as web
    # elicitations via the Task 9 hook. The reader owns its own Omnigent client
    # (built by the shared ``run_reader_with_bridge`` helper) from the server URL +
    # refresh-capable auth resolved above. Reuses the same per-session
    # background-task registry, so a session never runs two readers at once and a
    # terminal re-create cancels the prior reader.
    _reader_task = asyncio.create_task(
        _run_antigravity_reader(
            base_url=server_url,
            headers=runner_headers,
            auth=_RunnerDatabricksAuth(auth_factory),
            session_id=session_id,
            bridge_dir=bridge_dir,
        ),
        name=f"antigravity-reader-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _reader_task)

    # Advertise the tmux pane so the executor can deliver the FIRST web turn into
    # the agy TUI (agy mints its conversation only after it processes input; the
    # connect-RPC fast path cannot address a conversation that does not exist
    # yet). Done AFTER the reader is registered and made best-effort/off-loop:
    # this is a fallible filesystem write, and the "a registered runner-owned
    # terminal implies a running reader" invariant requires nothing fallible
    # to abort the launch between terminal registration and reader start. A
    # write failure (or a truly remote runner with no local pane) leaves the
    # reader running; the executor's first-turn bootstrap then surfaces a clear
    # "tmux target was not advertised" error and a later ensure can re-advertise.
    if tmux_socket is not None and tmux_target is not None:
        try:
            await asyncio.to_thread(
                write_tmux_target,
                bridge_dir,
                socket_path=tmux_socket,
                tmux_target=tmux_target,
            )
        except OSError:
            _logger.warning(
                "Could not advertise antigravity tmux target for session %s; the first "
                "web turn's TUI bootstrap will report it until a later ensure re-advertises.",
                session_id,
                exc_info=True,
            )

    # Announce the terminal to clients ONLY after the reader is started and
    # registered. ``session_resource_view_to_dict`` serialization + the publish
    # are the LAST steps, so any failure happens before clients are told the
    # terminal exists — preserving the "a registered runner-owned terminal
    # implies a running reader" invariant the ensure path relies on.
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": session_resource_view_to_dict(terminal_view),
        },
    )
    _logger.info(
        "Auto-created antigravity terminal + RPC reader for session %s",
        session_id,
    )
    return terminal_view


def _mint_runner_agy_conversation_id() -> str:
    """
    Mint a placeholder agy conversation id for a fresh runner launch.

    agy mints its own UUID and ignores any id we assign, so this seeds bridge
    state only until the cold-start replaces it with agy's real cascade id (or,
    if cold-start fails, until the reader's discovery binds the real id once a
    turn creates the conversation). Mirrors
    :func:`omnigent.antigravity_native._mint_agy_conversation_id`.

    :returns: An ``"agy_conv_<hex>"`` placeholder id.
    """
    return f"agy_conv_{uuid.uuid4().hex}"


# Cold-start port-discovery budget. agy's connect-RPC server binds its loopback
# port a moment AFTER the process starts (per-process, BEFORE any conversation
# exists), so the bootstrap polls rather than probing once. The total wait is
# bounded so a never-binding agy cannot hang the launch; the reader still spawns
# afterward and keeps polling discovery as a functional fallback.
_AGY_COLD_START_PORT_TIMEOUT_S = 20.0
_AGY_COLD_START_PORT_POLL_INTERVAL_S = 0.25


async def _agy_cold_start_poll_sleep(seconds: float) -> None:
    """
    Sleep between agy cold-start port-discovery polls.

    Indirection point so tests can stub the poll backoff without patching the
    process-wide ``asyncio.sleep`` (the ``no-global-asyncio-patch`` lint hook
    bans patching the module singleton). Mirrors :func:`_wake_retry_sleep`.

    :param seconds: Seconds to wait before the next port probe, e.g. ``0.25``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


async def _cold_start_agy_conversation(
    bridge_dir: Path,
    session_id: str,
    *,
    server_client: httpx.AsyncClient | None = None,
    tmux_socket: Path | None = None,
    tmux_target: str | None = None,
    timeout_s: float = _AGY_COLD_START_PORT_TIMEOUT_S,
) -> str | None:
    """
    Cold-start agy's conversation over connect-RPC and own its id (best-effort).

    The fresh-launch bootstrap: the runner mints the conversation over
    ``StartCascade`` so the executor's turn-1 has a real cascade id, instead of
    waiting for the agy TUI to lazily create one on its first typed turn. The
    connect-RPC port is resolved by
    :func:`omnigent.antigravity_native_rpc.resolve_cold_start_agy_rpc_port`:
    scoped to THIS session's own agy via its tmux pane (``tmux_socket`` /
    ``tmux_target``) so a host running several agy instances (sub-agent fan-out /
    shared runner) cannot ``StartCascade`` onto a FOREIGN agy and permanently
    cross-bind the session — the conversation-ownership check that normally
    disambiguates is not usable yet (no conversation exists). It falls back to the
    lowest ``Heartbeat``-answering candidate (current behavior) only when no local
    pane is reachable (remote runner), or once our agy is up in the pane but its
    port is not lsof-attributable; while our agy is NOT yet up in the pane it keeps
    polling rather than risk a foreign-agy candidate. This polls that resolver
    until a port binds, then ``StartCascade``s a runner-generated
    ``uuid4`` and writes THAT real id into bridge state (replacing the
    ``agy_conv_*`` placeholder) so :func:`read_bridge_state` returns the real id
    and the reader/executor address the cold-started conversation directly.

    The cold-started id is also PATCHed onto the Omnigent session as
    ``external_session_id`` (best-effort, mirroring codex/pi) so a later
    ``--resume`` reads it back and passes ``--conversation <id>`` to continue
    agy's actual conversation — the read-path replacement for the forwarder's
    ``_patch_external_session_id``. Only the fresh-launch caller invokes this
    (``if not resume:``); a resume already holds agy's real id, so it neither
    cold-starts nor re-PATCHes. As defense-in-depth (mirroring the CLI cold-start),
    this ALSO early-returns the existing id when bridge state already holds a
    non-placeholder conversation id, so it can never cold-start over a real id even
    if a future caller forgets the resume gate.

    **Best-effort, never raises.** A bootstrap failure (no port within
    *timeout_s*, or ``StartCascade`` erroring) must NOT abort the auto-create:
    that would leave a registered terminal with no reader (which a later
    ensure sees and returns 200 for, never self-healing). On failure this logs
    and returns ``None`` (the placeholder stays; the reader's discovery then binds
    agy's real id once a turn creates the conversation). The sync
    RPC/poll work runs in :func:`asyncio.to_thread` so the event loop is never
    blocked.

    :param bridge_dir: Native Antigravity bridge directory whose ``state.json``
        the real cold-started id is written into.
    :param session_id: Owning session/conversation id (for log correlation and
        the ``external_session_id`` PATCH target).
    :param server_client: Runner Omnigent server client used for the
        ``external_session_id`` PATCH. ``None`` skips the PATCH (the cascade id is
        still written to bridge state).
    :param tmux_socket: This session's tmux socket path, used to scope the
        ``StartCascade`` port to the agy running under this session's pane.
        ``None`` (remote runner / no local pane) falls back to the candidate scan.
    :param tmux_target: This session's tmux target (e.g. ``"main"``), paired with
        ``tmux_socket`` for the pane-scoped port resolution.
    :param timeout_s: Total seconds to wait for agy's connect-RPC port to bind.
    :returns: The real (cold-started) cascade/conversation id on success, or
        ``None`` when no port answered in time or ``StartCascade`` failed.
    """
    from omnigent.antigravity_native_bridge import (
        is_placeholder_conversation_id,
        read_bridge_state,
        update_conversation_id,
    )
    from omnigent.antigravity_native_rpc import (
        AntigravityRpcError,
        resolve_cold_start_agy_rpc_port,
        start_cascade,
    )

    # Defense-in-depth (mirrors the CLI cold-start in ``antigravity_native.py``):
    # the caller only invokes this on a fresh launch (``if not resume:``), but a
    # non-placeholder id in bridge state means agy's real conversation already
    # exists — cold-starting would create a second empty conversation and clobber
    # the real id. Refuse so this can never cold-start over a real id even if a
    # future caller forgets the resume gate.
    state = await asyncio.to_thread(read_bridge_state, bridge_dir)
    if state is not None and not is_placeholder_conversation_id(state.conversation_id):
        return state.conversation_id

    deadline = time.monotonic() + timeout_s
    port: int | None = None
    while True:
        # Scope to THIS session's pane agy (avoids binding a foreign agy on a
        # multi-agy host); falls back to the lowest validated candidate when no
        # local pane is reachable or the pane is not resolvable yet.
        port = await asyncio.to_thread(resolve_cold_start_agy_rpc_port, tmux_socket, tmux_target)
        if port is not None:
            break
        if time.monotonic() >= deadline:
            _logger.warning(
                "Antigravity cold-start: no agy connect-RPC port bound within %.0fs for "
                "session %s; leaving the placeholder conversation id for the reader to "
                "bind once a turn creates the conversation.",
                timeout_s,
                session_id,
            )
            return None
        await _agy_cold_start_poll_sleep(_AGY_COLD_START_PORT_POLL_INTERVAL_S)

    cascade_id = str(uuid.uuid4())
    try:
        await asyncio.to_thread(start_cascade, port, cascade_id)
    except AntigravityRpcError:
        _logger.warning(
            "Antigravity cold-start: StartCascade failed on port %s for session %s; leaving "
            "the placeholder conversation id for the reader to bind.",
            port,
            session_id,
            exc_info=True,
        )
        return None
    # Persist the real id (replacing the ``agy_conv_*`` placeholder) so
    # ``read_bridge_state`` returns it and the reader/executor address the
    # cold-started conversation. Offloaded (file I/O).
    if not await asyncio.to_thread(update_conversation_id, bridge_dir, cascade_id):
        _logger.warning(
            "Antigravity cold-start: could not persist cold-started conversation id %s for "
            "session %s (no bridge state to update); the reader will stay on the placeholder id.",
            cascade_id,
            session_id,
        )
    # Do NOT record this cold-start cascade as the session's external_session_id:
    # it is the headless ``StartCascade`` bootstrap that the agy TUI never
    # displays. The TUI mints its OWN cascade on the first typed turn, which the
    # read driver ADOPTS in place and records as external_session_id (see
    # ``antigravity_native_reader._record_external_session_id``). Recording the
    # phantom here used to lose the whole conversation on resume: a later
    # ``--resume`` launched ``--conversation <phantom>`` and loaded an EMPTY
    # conversation. external_session_id is set-once, so it MUST be left unset here
    # for the reader's adoption PATCH to set the real id.
    del server_client  # retained for signature parity; no longer PATCHes here
    _logger.info(
        "Antigravity cold-start: created conversation %s on port %s for session %s",
        cascade_id,
        port,
        session_id,
    )
    return cascade_id


def _terminal_tmux_pane(
    resource_registry: SessionResourceRegistry,
    session_id: str,
    terminal_name: str,
    session_key: str,
) -> tuple[Path | None, str | None]:
    """
    Return a launched terminal's tmux socket + target when locally reachable.

    Used to bind the antigravity forwarder's conversation discovery to this
    session's own agy pane. Returns ``(None, None)`` when the registry has no
    live instance for the triple (the forwarder then uses its bounded-ambiguity
    fallback).

    :param resource_registry: Session resource registry exposing the terminal
        registry.
    :param session_id: Owning session/conversation id.
    :param terminal_name: Terminal spec name, e.g. ``"antigravity"``.
    :param session_key: Session key, e.g. ``"main"``.
    :returns: ``(tmux_socket, tmux_target)`` or ``(None, None)``.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return None, None
    instance = terminal_registry.get(session_id, terminal_name, session_key)
    if instance is None or not instance.running:
        return None, None
    # ``socket_path`` is a Path and ``tmux_target`` a str on the live terminal
    # instance (see omnigent.inner.terminal). Guard defensively so a registry
    # variant without them falls back to the forwarder's ambiguity path.
    socket_path = getattr(instance, "socket_path", None)
    target = getattr(instance, "tmux_target", None)
    tmux_socket = Path(socket_path) if isinstance(socket_path, (str, Path)) else None
    tmux_target = target if isinstance(target, str) and target else None
    return tmux_socket, tmux_target


async def _session_payload_for_host_spawn_check(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> dict[str, Any] | None:
    """
    Fetch a session snapshot for Codex host-spawn detection.

    :param server_client: The runner's Omnigent server HTTP client, or
        ``None`` in embedded/test setups.
    :param session_id: Session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Parsed session JSON object, or ``None`` when the
        snapshot cannot be retrieved.
    """
    if server_client is None:
        return None
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "Could not resolve host_id for %s; skipping codex terminal auto-create",
            session_id,
        )
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


async def _fetch_cost_control_mode_override(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> str | None:
    """
    Read the session's per-session Cost Optimized toggle, defensively.

    Fetches the session snapshot and returns its
    ``cost_control_mode_override``. Treats every failure mode
    — no client, transport error, non-200, absent field — as ``None``
    (no override) so the advisor still works against an older server
    that lacks the column. The advisor never blocks on this read.

    :param server_client: The runner's Omnigent server HTTP client, or
        ``None`` in embedded / test setups.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``"on"`` / ``"off"`` when the session set the toggle, or
        ``None`` (unset, or unreadable for any reason).
    """
    payload = await _session_payload_for_host_spawn_check(server_client, session_id)
    if payload is None:
        return None
    override = payload.get("cost_control_mode_override")
    return override if isinstance(override, str) else None


async def _codex_session_needs_runner_terminal(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> bool:
    """
    Whether the runner must auto-create the Codex terminal for a session.

    The runner owns the terminal for every codex-native session, including
    top-level CLI sessions. Older top-level CLI sessions used to run their
    own app-server/TUI/forwarder; that split ownership caused competing
    setup and teardown. Now all codex-native sessions need runner
    auto-create:

    - **Host-spawned (web-UI) top-level sessions** carry a ``host_id``.
    - **Sub-agent children** (dispatched server-side via
      ``sys_session_send``) carry a ``parent_session_id`` but no
      ``host_id`` of their own. No CLI ever manages a sub-agent terminal,
      so the runner must create it regardless of whether the *parent* was
      host- or CLI-spawned. (Gating on the parent's ``host_id`` was a
      regression: codex-native sub-agents under a CLI-driven parent —
      e.g. polly run via ``omnigent run --server`` — silently never got
      a terminal and the dispatch no-op'd.)

    - **CLI top-level sessions** have neither ``host_id`` nor
      ``parent_session_id`` but still need the runner to own the app-server
      and terminal.

    Returns ``False`` only when the lookup fails; without a session
    snapshot, the runner cannot confirm this is a codex-native session.

    :param server_client: The runner's Omnigent server HTTP client, or ``None`` in
        embedded/test setups.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``True`` when the session snapshot exists; ``False`` on
        lookup failure.
    """
    payload = await _session_payload_for_host_spawn_check(server_client, session_id)
    if payload is None:
        return False
    return True


def _codex_native_model_from_spec(agent_spec: AgentSpec | ResolvedSpec | None) -> str | None:
    """
    Read the Codex model default from a resolved agent spec.

    :param agent_spec: Agent spec object, or a resolved wrapper carrying a
        ``spec`` attribute. ``None`` means no spec was available.
    :returns: Model id, e.g. ``"gpt-5.4-mini"``, or ``None``.
    """
    spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    if spec is None:
        return None
    model = spec.executor.config.get("model")
    return model if isinstance(model, str) and model else None


def _cursor_native_model_from_spec(agent_spec: AgentSpec | ResolvedSpec | None) -> str | None:
    """
    Read the cursor-agent model id to launch the native TUI with, from a spec.

    Reads the canonical ``spec.executor.model`` field (the same field the
    in-process cursor SDK harness consumes via ``_resolve_spec_model``). A
    gateway-routed id (``databricks-*``) is not a valid ``cursor-agent`` model
    id, so it is dropped (with a warning) — the caller then omits ``--model`` and
    ``cursor-agent`` keeps its configured default rather than erroring on launch.

    :param agent_spec: Agent spec object, or a resolved wrapper carrying a
        ``spec`` attribute. ``None`` means no spec was available.
    :returns: A cursor-agent model id, e.g. ``"sonnet-4-thinking"``, or ``None``
        when the spec declares no usable cursor model.
    """
    spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    if spec is None:
        return None
    model = spec.executor.model
    if not isinstance(model, str) or not model:
        return None
    if model.startswith(("databricks-", "databricks/")):
        _logger.warning(
            "cursor-native: pinned model %r is not a cursor-agent model id; "
            "launching cursor-agent on its configured default instead.",
            model,
        )
        return None
    return model


def _cursor_native_resume_args(chat_id: str | None, existing_args: list[str]) -> list[str]:
    """Return ``["--resume", chat_id]`` for a cursor-native cold resume, or ``[]``.

    The forwarder persists the cursor chat id as ``external_session_id`` after
    it first discovers the chat store. On a cold resume (terminal has exited)
    this id is injected here so cursor-agent reloads the prior conversation.
    cursor-agent reuses the same chat id/store across ``--resume`` (verified
    empirically), so the persisted id stays valid for the life of the session.

    Re-validates the chat id (callers should already have, but this stays
    self-defensive so a malformed id can never reach the argv directly).

    :param chat_id: The cursor chat id stored as ``external_session_id``, or
        ``None`` for a brand-new session where the forwarder hasn't run yet.
    :param existing_args: Already-built cursor-agent args; ``--resume`` is
        skipped when the user already passed one (``--resume X`` or the joined
        ``--resume=X`` form) via passthrough launch args.
    :returns: ``["--resume", chat_id]`` or ``[]``.
    """
    from omnigent.cursor_native import is_valid_cursor_chat_id

    if not is_valid_cursor_chat_id(chat_id):
        return []
    if any(arg == "--resume" or arg.startswith("--resume=") for arg in existing_args):
        return []
    return ["--resume", chat_id]


def _cursor_message_item_text(content: Any) -> str:
    """Join the text of a session message item's content blocks.

    :param content: A message item's ``content`` — a plain string or a list of
        ``{"type": "input_text"|"output_text"|"text", "text": ...}`` blocks.
    :returns: The concatenated block text (stripped), or ``""``.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "".join(parts).strip()


#: Transcript role labels for the fork preamble. cursor's TUI can't reconstruct
#: native user/assistant bubbles (its conversation is server-backed), so the
#: replayed history reads as close to that as a single text block allows:
#: capitalized speaker labels, blank-line-separated turns.
_CURSOR_FORK_ROLE_LABELS = {"user": "You", "assistant": "Assistant"}


def _cursor_fork_history_preamble(items: list[dict[str, Any]]) -> str:
    """Render copied fork items as a readable conversation transcript.

    cursor's conversation is server-backed, so a fork can't seed a local store
    for ``--resume`` to load; instead the prior turns are replayed as a text
    prefix on the fork's first message (text-prefix replay). Only user/assistant
    message text is replayed — cursor's TUI has no surface to import tool-call
    history or reconstruct native bubbles, so this formats the turns as a clean
    speaker-labelled transcript (the closest single-block analog), mirroring the
    antigravity executor's documented text-prefix fallback. The human framing +
    strip sentinel are added by
    :func:`omnigent.cursor_native_bridge.wrap_fork_preamble`.

    :param items: Committed Omnigent items (``GET /v1/sessions/{id}/items``),
        chronological.
    :returns: A blank-line-separated transcript like ``"You: …\\n\\nAssistant:
        …"``, or ``""`` when no replayable user/assistant text exists.
    """
    turns: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        role = item.get("role")
        if role not in _CURSOR_FORK_ROLE_LABELS:
            continue
        text = _cursor_message_item_text(item.get("content"))
        if text:
            turns.append(f"{_CURSOR_FORK_ROLE_LABELS[role]}: {text}")
    return "\n\n".join(turns)


def _agent_os_env_from_spec(agent_spec: AgentSpec | ResolvedSpec | None) -> Any | None:
    """
    Read the agent's ``os_env`` from a resolved agent spec.

    The auto-created native terminals (codex/claude) must inherit the
    agent's ``os_env`` so its ``sandbox`` (e.g. ``type: none``),
    ``egress_rules`` and ``env_passthrough`` are honoured. Without this
    the terminal is built with a fresh ``OSEnvSpec`` carrying no sandbox,
    and ``launch_terminal`` falls back to ``_default_sandbox_for_platform``
    (``linux_bwrap`` / ``darwin_seatbelt``) — overriding the YAML config.
    Mirrors :func:`create_session_terminal`, which resolves the spec once
    and threads its ``os_env`` through as the inheritance parent.

    :param agent_spec: Agent spec object, or a resolved wrapper carrying a
        ``spec`` attribute. ``None`` means no spec was available.
    :returns: The agent's ``os_env`` spec, or ``None``.
    """
    spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    if spec is None:
        return None
    return getattr(spec, "os_env", None)


def _is_runner_owned_codex_terminal(
    resource_registry: SessionResourceRegistry,
    resource: SessionResourceView,
) -> bool:
    """
    Return whether an existing ``codex/main`` terminal is the native TUI.

    A generic terminal launched with ``terminal=codex`` has the same public
    resource id but is not the runner-owned Codex TUI. The resource registry
    carries the private role marker that identifies terminals created by
    ``_auto_create_codex_terminal`` without leaking launch argv in public
    metadata.

    :param resource_registry: Runner resource registry that owns private
        terminal role markers.
    :param resource: Existing terminal resource view.
    :returns: ``True`` when the resource is marked as Codex native.
    """
    return (
        resource_registry.terminal_resource_role(resource.session_id, resource.id)
        == CODEX_NATIVE_TERMINAL_ROLE
    )


def _is_runner_owned_antigravity_terminal(
    resource_registry: SessionResourceRegistry,
    resource: SessionResourceView,
) -> bool:
    """
    Return whether an existing ``antigravity/main`` terminal is the agy TUI.

    A generic terminal launched with ``terminal=antigravity`` (e.g. the CLI
    wrapper's own launch) has the same public resource id but is not the
    runner-owned agy TUI created by :func:`_auto_create_antigravity_terminal`.
    The resource registry carries the private role marker that distinguishes
    them. Mirrors :func:`_is_runner_owned_codex_terminal`.

    :param resource_registry: Runner resource registry that owns private
        terminal role markers.
    :param resource: Existing terminal resource view.
    :returns: ``True`` when the resource is marked as Antigravity native.
    """
    return (
        resource_registry.terminal_resource_role(resource.session_id, resource.id)
        == ANTIGRAVITY_NATIVE_TERMINAL_ROLE
    )


def _build_claude_native_base_args(
    *,
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    resume_external_session_id: str | None = None,
) -> tuple[str, ...]:
    """
    Assemble the base ``claude`` CLI args for a native-terminal launch.

    These are the args before :func:`augment_claude_args` layers on the
    bridge / MCP / hook / Omnigent wiring. The order is: ``--resume`` for a
    cold resume, then persisted reasoning effort, then the user's
    pass-through ``terminal_launch_args``, then a ``--model`` derived
    from ``model_override`` — appended only when the user did not
    already pass an explicit ``--model``. That precedence (explicit
    ``--model`` in pass-through args wins over ``model_override``)
    mirrors the CLI's ``_merge_default_model_arg``, moved runner-side.
    The ``--resume``-first ordering mirrors the CLI's
    ``(*cold_resume_args, *claude_args)``. See
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md.

    :param reasoning_effort: Persisted per-session effort, e.g.
        ``"high"``. Added as ``--effort <value>`` only when it is one
        of Claude's supported efforts; otherwise ignored. ``None``
        adds nothing (Claude uses its own ``~/.claude/settings.json``
        default).
    :param model_override: Per-session model override, e.g.
        ``"claude-opus-4-7"``. Appended as ``--model <value>`` unless
        the pass-through args already contain a ``--model`` flag.
        ``None`` adds nothing.
    :param terminal_launch_args: The user's pass-through CLI args,
        e.g. ``["--dangerously-skip-permissions"]``. ``None`` or an
        empty list contributes nothing.
    :param resume_external_session_id: Claude-native session id to
        resume, e.g. ``"02857840-6362-408f-b41f-309e396ed7c6"``.
        Prepended as ``--resume <value>`` so Claude reopens the prior
        transcript. A forked clone passes the uuid it assigned to its
        OWN cloned transcript here (see
        :func:`omnigent.claude_native._clone_claude_transcript`), so
        the same plain ``--resume`` path serves both cold resume and
        fork resume. ``None`` (a fresh launch, or no local transcript
        could be synthesized) adds nothing.
    :returns: The assembled base args, e.g.
        ``("--resume", "<sid>", "--effort", "high")``.
    """
    from omnigent.reasoning_effort import CLAUDE_EFFORTS

    args: list[str] = []
    if resume_external_session_id:
        args.extend(("--resume", resume_external_session_id))
    if reasoning_effort is not None and reasoning_effort in CLAUDE_EFFORTS:
        args.extend(("--effort", reasoning_effort))
    if terminal_launch_args:
        args.extend(terminal_launch_args)
    # model_override is a default: it applies only when the user did
    # not pass their own ``--model`` (in either the long ``--model X``
    # or the joined ``--model=X`` form).
    if model_override and not any(arg == "--model" or arg.startswith("--model=") for arg in args):
        args.extend(("--model", model_override))
    return tuple(args)


def _publish_terminal_pending(
    publish_event: Callable[[str, dict[str, Any]], None],
    session_id: str,
    pending: bool,
) -> None:
    """
    Publish a terminal spin-up status event onto the session stream.

    Emitted by the auto-create path so the web UI can show a spinner on
    the Terminal pill while the runner boots a terminal-first session's
    terminal, and clear it once the terminal lands or auto-create
    fails. The Omnigent relay caches the latest value and republishes it, and
    seeds the ``terminal_pending`` snapshot field, so a client that
    connects mid-spin-up still sees the spinner. ``pending=False`` is
    what distinguishes "still starting up" from "no terminal" (killed /
    never created): once cleared, the client relies purely on whether a
    terminal resource exists.

    :param publish_event: The runner's per-session SSE emitter,
        ``(session_id, event_dict) -> None``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` when a terminal is being created (show the
        spinner); ``False`` to clear it (terminal landed, or
        auto-create raised).
    """
    publish_event(
        session_id,
        {"type": "session.terminal_pending", "pending": pending},
    )


def _native_terminal_start_error_payload(exc: BaseException, runtime_name: str) -> dict[str, str]:
    """
    Build the structured error payload for a native terminal start failure.

    :param exc: Exception raised by the native terminal creation path,
        e.g. ``ImportError("Native Codex requires the 'codex' CLI on PATH.")``.
    :param runtime_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: ``{"code": ..., "message": ...}`` payload for SSE and
        JSON error responses. The message is a fixed, client-safe string;
        the raw cause is logged for operators, not surfaced to the caller.
    """
    _logger.warning("Native %s terminal start failed: %s", runtime_name, exc, exc_info=True)
    if IS_WINDOWS:
        # Native terminals are tmux/PTY-based and disabled on Windows by design.
        # Give the client an actionable message instead of "see runner logs".
        message = (
            f"Native {runtime_name} terminal (tmux/PTY) is not supported on "
            "Windows. Use an SDK-based harness (e.g. claude-sdk, cursor, "
            "copilot, or codex) for this agent, or run it on Linux/macOS."
        )
    else:
        message = f"Native {runtime_name} terminal failed to start; see runner logs for details."
    return {"code": _NATIVE_TERMINAL_START_FAILED_CODE, "message": message}


def _publish_native_terminal_start_error(
    publish_event: Callable[[str, dict[str, Any]], None],
    session_id: str,
    runtime_name: str,
    exc: BaseException,
) -> dict[str, str]:
    """
    Publish live failure events for a native terminal start failure.

    The runner stays alive: the affected session receives
    ``session.status: failed`` with the structured cause, while resource
    panels and the relay keep working. The runner does not publish a
    bare ``response.error`` here because terminal auto-create happens
    outside a transcript turn; Omnigent writes and publishes the turn-scoped
    ``response.error`` only when it consumes a user message that cannot
    run because the terminal is failed.

    :param publish_event: The runner's per-session SSE emitter,
        ``(session_id, event_dict) -> None``.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runtime_name: Human-readable runtime name, e.g. ``"Claude"``.
    :param exc: The startup exception whose text should be surfaced.
    :returns: The structured error payload that was published on the
        status event.
    """
    error = _native_terminal_start_error_payload(exc, runtime_name)
    publish_event(
        session_id,
        {
            "type": "session.status",
            "status": "failed",
            "error": error,
        },
    )
    return error


def _native_terminal_start_error_response(exc: BaseException, runtime_name: str) -> JSONResponse:
    """
    Return a structured JSON error for native terminal ensure failures.

    :param exc: Exception raised by terminal auto-create.
    :param runtime_name: Human-readable runtime name, e.g. ``"Codex"``.
    :returns: HTTP 500 response with an ``error`` object carrying the
        real failure message.
    """
    return JSONResponse(
        status_code=500,
        content={"error": _native_terminal_start_error_payload(exc, runtime_name)},
    )


def _codex_ensure_response_with_policy_notice(
    session_id: str, terminal_view: SessionResourceView
) -> JSONResponse:
    """
    Build the codex terminal-ensure 200 response with a one-shot notice.

    When the codex app-server degraded to "no policy enforcement"
    (fail-open — codex too old or trust failed), attach the reason as
    ``policy_hook_disabled_reason`` exactly once so Omnigent can post a single
    durable web-UI banner. The app-server's one-shot flag is cleared
    after the first surface, so repeated ensures (each user message
    re-probes) do not re-post the notice.

    Must be called while holding the per-session codex ensure lock
    (``_codex_terminal_ensure_locks[session_id]``): the read-and-clear of
    ``policy_notice_pending`` is only one-shot because that lock
    serializes concurrent ensures for the same session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param terminal_view: The runner-owned codex terminal resource view
        to return.
    :returns: A 200 JSON response, optionally carrying
        ``policy_hook_disabled_reason``.
    """
    body = session_resource_view_to_dict(terminal_view)
    app_server = _AUTO_CODEX_APP_SERVERS.get(session_id)
    if (
        app_server is not None
        and app_server.policy_notice_pending
        and app_server.policy_hook_disabled_reason
    ):
        body["policy_hook_disabled_reason"] = app_server.policy_hook_disabled_reason
        app_server.policy_notice_pending = False
    return JSONResponse(status_code=200, content=body)


def _ensure_orchestrator_skills_in_bundle(
    bundle_dir: Path,
    agent_spec: Any,
) -> None:
    """
    Link the ``build-omnigent`` skill into a bundle's ``skills/`` dir.

    Called before native bridge launches so ``--plugin-dir`` (claude) or
    ``CODEX_HOME/skills/`` (codex) picks up the skill. Injects
    unconditionally for every agent — every ``omnigent claude`` /
    ``omnigent codex`` user should be able to author new agents. The
    skill isn't already present guard is idempotent. Best-effort: a
    failure to link is logged but does not abort the terminal launch.

    :param bundle_dir: Materialized agent-bundle root, e.g.
        ``/tmp/omnigent-ap-chat-xyz/bundle``.
    :param agent_spec: The session's AgentSpec (unused after gate
        removal; retained for call-site compat).
    """
    del agent_spec  # no longer gated; inject unconditionally
    skill_name = "build-omnigent"
    target_dir = bundle_dir / "skills" / skill_name
    if target_dir.exists():
        return
    source = (
        Path(__file__).resolve().parent.parent / "onboarding" / "agent" / "skills" / skill_name
    )
    if not source.is_dir():
        return
    try:
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        target_dir.symlink_to(source)
    except OSError:
        _logger.debug(
            "Could not link %s skill into bundle %s",
            skill_name,
            bundle_dir,
            exc_info=True,
        )


async def _auto_create_claude_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient,
    bundle_dir: Path | None = None,
    agent_name: str | None = None,
    agent_spec: AgentSpec | ResolvedSpec | None = None,
    skills_filter: str | list[str] = "all",
) -> SessionResourceView:
    """
    Auto-create a Claude Code terminal for a claude-native session.

    Called when the runner receives a claude-native session via
    ``POST /v1/sessions`` and no terminal exists yet. This handles
    the host-spawned runner case where no CLI client is present to
    create the terminal.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_registry: Session resource registry for
        launching the terminal.
    :param publish_event: The runner's per-session SSE emitter, used to
        surface the new terminal on the live stream (the Omnigent relay
        republishes it to the web UI) so the Terminal toggle enables
        without a refresh.
    :param server_client: Omnigent server client used to fetch the session
        snapshot so the terminal inherits the persisted
        ``reasoning_effort``.
    :param bundle_dir: Materialized agent-bundle root when the session's
        agent ships a ``skills/`` directory, resolved by the caller
        (which has the runner's spec resolver). Threaded to
        :func:`augment_claude_args` so Claude Code discovers bundled
        skills via ``--plugin-dir``. ``None`` adds no plugin args.
    :param agent_name: Agent display name for the bundle's plugin
        manifest, e.g. ``"researcher"``. ``None`` falls back to the
        bundle directory's basename.
    :param agent_spec: Optional resolved agent spec for the session. Its
        ``os_env`` (sandbox / egress_rules / env_passthrough) is threaded
        through as the terminal's inheritance parent so the YAML sandbox
        config (e.g. ``type: none``) is honoured instead of being
        overridden by ``_default_sandbox_for_platform``.
    :param skills_filter: The agent spec's ``skills_filter`` (``"all"``
        / ``"none"`` / list of skill names), threaded to
        :func:`augment_claude_args`. Defaults to ``"all"``.
    :returns: The launched terminal's :class:`SessionResourceView`, so
        callers that create it on demand (the resume "ensure" path in
        :func:`create_session_terminal`) can return the resource.
    """
    from pathlib import Path

    from omnigent.claude_native_bridge import (
        BRIDGE_ID_LABEL_KEY,
        ensure_claude_workspace_trusted,
        prepare_bridge_dir,
    )
    from omnigent.claude_native_forwarder import reset_transcript_forward_state
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    workspace = os.environ.get("OMNIGENT_RUNNER_WORKSPACE", str(Path.cwd()))
    started_at = time.monotonic()
    _logger.info(
        "Claude terminal auto-create starting: session=%s workspace=%s bundle_dir=%s "
        "agent_name=%s skills_filter=%s",
        session_id,
        workspace,
        bundle_dir,
        agent_name,
        skills_filter,
    )
    # prepare_bridge_dir uses session_id as the bridge_id (no explicit
    # bridge_id passed), so the bridge dir is keyed by session_id.  If the
    # Omnigent session carries a stale bridge_id label from a prior rotation that
    # timed out before the terminal transfer completed, _ensure_comment_relay_started
    # would read the label and write tool_relay.json to the wrong directory —
    # the bridge subprocess would never see it and the relay tools would be absent.
    # Correcting the label here ensures all subsequent label lookups return
    # session_id, which matches the actual bridge dir.
    try:
        await server_client.patch(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            json={"labels": {BRIDGE_ID_LABEL_KEY: session_id}},
        )
    except httpx.HTTPError:
        _logger.debug(
            "Could not reset bridge_id label for %s; relay may target wrong dir",
            session_id,
        )
    bridge_dir = prepare_bridge_dir(session_id, workspace=Path(workspace))
    # Cancel any surviving forwarder BEFORE wiping its cursor/seen state, else it
    # re-posts with fresh dedup state alongside the forwarder spawned below.
    await _cancel_auto_forwarder_task(session_id)
    reset_transcript_forward_state(bridge_dir)
    _logger.info(
        "Claude terminal bridge prepared: session=%s bridge_dir=%s",
        session_id,
        bridge_dir,
    )
    # Pre-accept Claude's first-run trust + onboarding TUI prompts for this
    # workspace. They have no PermissionRequest hook, so on a host-spawned
    # (web-UI-driven) session they would hang Claude in its terminal with
    # nothing shown in the UI. Acute with per-session worktrees,
    # which launch Claude in a brand-new, untrusted directory.
    ensure_claude_workspace_trusted(Path(workspace))

    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    # The Omnigent server URL + auth are needed in two places below: the
    # PermissionRequest hook (so Claude's approval prompts route to the
    # web UI instead of its TUI) and the transcript forwarder. The CLI
    # client supplies these on the wrapper path; on this host-spawned
    # path the runner reconstructs them from its own environment/auth.
    server_url = os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767")
    # Authenticate the runner's outbound POSTs the same way its other
    # HTTP calls are authenticated.
    _auth_factory = _make_auth_token_factory()
    # The PermissionRequest hook runs in a separate subprocess that reads
    # static headers from permission_hook.json, so it gets a one-shot
    # token snapshot. The long-running transcript forwarder instead gets
    # a refresh-capable ``httpx.Auth`` (below) so it survives the ~1h
    # Databricks OAuth token expiry; a one-shot header would silently
    # stop forwarding after the token lapses. ``_RunnerDatabricksAuth``
    # with a ``None`` factory is a safe no-op (local unauthenticated).
    _auth_token = _auth_factory() if _auth_factory is not None else None
    _runner_headers = {"Authorization": f"Bearer {_auth_token}"} if _auth_token else {}
    _runner_auth = _RunnerDatabricksAuth(_auth_factory)

    from omnigent.claude_native import (
        ClaudeNativeUcodeConfig,
        augment_claude_args,
        build_native_claude_terminal_env,
        resolve_native_claude_config,
    )

    # Fetch the session's persisted launch config (reasoning_effort,
    # model_override, terminal_launch_args) so a web-UI / daemon-spawned
    # launch honours the same flags the CLI would have passed. Best-effort
    # — a failed lookup means Claude starts at its settings.json defaults
    # with no extra args. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    )

    session_effort: str | None = None
    session_model_override: str | None = None
    session_launch_args: list[str] | None = None
    session_external_id: str | None = None
    # Source native session id stamped on a forked clone (one-shot): when
    # the clone has no native session of its own yet, resume + branch the
    # source's local transcript so it opens with prior history.
    fork_source_external_id: str | None = None
    # Set on a forked clone bound to a native target: when no source
    # native transcript exists to clone (an SDK or cross-family source),
    # build the clone's native transcript from the copied Omnigent items
    # instead (see FORK_CARRY_HISTORY_LABEL_KEY / native_replay design notes).
    fork_carry_history: bool = False
    if server_client is not None:
        try:
            _resp = await server_client.get(
                f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                timeout=10.0,
            )
            if _resp.status_code == 200:
                _snap = _resp.json()
                _re = _snap.get("reasoning_effort")
                if isinstance(_re, str) and _re:
                    session_effort = _re
                _mo = _snap.get("model_override")
                if isinstance(_mo, str) and _mo:
                    session_model_override = _mo
                _tla = _snap.get("terminal_launch_args")
                if isinstance(_tla, list) and all(isinstance(a, str) for a in _tla):
                    session_launch_args = _tla
                _ext = _snap.get("external_session_id")
                if isinstance(_ext, str) and _ext:
                    session_external_id = _ext
                _labels = _snap.get("labels")
                if isinstance(_labels, dict):
                    _fse = _labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
                    if isinstance(_fse, str) and _fse:
                        fork_source_external_id = _fse
                    fork_carry_history = _labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
            _logger.info(
                "Claude terminal launch config fetched: session=%s status=%s "
                "effort_set=%s model_override_set=%s launch_args_count=%d "
                "external_session_id_set=%s",
                session_id,
                _resp.status_code,
                session_effort is not None,
                session_model_override is not None,
                len(session_launch_args or []),
                session_external_id is not None,
            )
        except httpx.HTTPError:
            _logger.debug(
                "Could not fetch session launch config for %s; terminal will "
                "use Claude's defaults",
                session_id,
            )

    # Cold resume: when this session wraps a prior Claude session,
    # synthesize the local ``~/.claude/projects/<workspace>/<sid>.jsonl``
    # transcript that Claude's ``--resume`` reads, then pass ``--resume``.
    # The CLI does this client-side via ``_resolve_cold_resume_args``;
    # doing it here lets a daemon / web-UI launch resume too. Best-effort:
    # on any failure we launch fresh rather than point ``--resume`` at a
    # transcript that doesn't exist. See
    # designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    resume_external_session_id: str | None = None
    if server_client is not None and session_external_id is not None:
        from omnigent.claude_native import _ensure_local_claude_resume_transcript

        try:
            _transcript = await _ensure_local_claude_resume_transcript(
                server_client,
                session_id=session_id,
                external_session_id=session_external_id,
                workspace=Path(workspace).resolve(),
            )
            if _transcript is not None:
                resume_external_session_id = session_external_id
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            _logger.warning(
                "Could not synthesize Claude resume transcript for %s; launching without --resume",
                session_id,
                exc_info=True,
            )
    elif session_external_id is None and fork_source_external_id is not None:
        # Forked clone with no native session yet: clone the SOURCE's
        # local Claude transcript into the clone's OWN project dir under a
        # uuid we assign — rewriting per-record sessionId/cwd — then launch
        # plain ``--resume <our_uuid>``. Writing the file ourselves before
        # launch means the forwarder's ``start_at_end`` seeks past the
        # copied prefix (no double-render), and placing it in the clone's
        # own project dir means cwd-scoped ``--resume`` finds it in any
        # dir/worktree. Only viable when the source transcript exists on
        # THIS host (same-host fork — CUJ 1 same-user); else launch fresh.
        # See designs/FORK_SESSION_UX.md.
        from omnigent.claude_native import _clone_claude_transcript

        our_uuid = str(uuid.uuid4())
        _clone_workspace = Path(workspace).resolve()
        try:
            _cloned = _clone_claude_transcript(
                source_external_session_id=fork_source_external_id,
                target_external_session_id=our_uuid,
                clone_workspace=_clone_workspace,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            _cloned = None
            _logger.warning(
                "Could not clone source transcript for forked clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Claude terminal fork-resume decision: session=%s source_ext=%s "
            "our_uuid=%s clone_workspace=%s cloned_transcript=%s",
            session_id,
            fork_source_external_id,
            our_uuid,
            _clone_workspace,
            str(_cloned) if _cloned is not None else None,
        )
        if _cloned is not None:
            # Resume our OWN clone (plain --resume, no --fork-session).
            resume_external_session_id = our_uuid
            # Record the assigned id now so Omnigent reflects the clone's own
            # Claude session immediately, and a later relaunch resumes it
            # via the normal cold-resume path (this branch is gated on
            # external_session_id being unset). Best-effort.
            if server_client is not None:
                try:
                    await server_client.patch(
                        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                        json={"external_session_id": our_uuid},
                        timeout=10.0,
                    )
                except httpx.HTTPError:
                    _logger.warning(
                        "Could not pre-set external_session_id for forked clone %s; "
                        "relying on hook capture",
                        session_id,
                        exc_info=True,
                    )
    elif (
        server_client is not None
        and fork_carry_history
        and session_external_id is None
        and fork_source_external_id is None
    ):
        # Forked clone bound to a native target with NO source native
        # transcript to clone (an SDK or cross-family source): build the clone's
        # native transcript from its OWN copied Omnigent items under a uuid we
        # assign, then launch plain ``--resume <our_uuid>``. This reuses the
        # same server-items→transcript converter the cross-machine cold
        # resume path uses (``_ensure_local_claude_resume_transcript``), so
        # the clone opens with the prior conversation (messages + tool
        # history) as real Claude context. Best-effort: launch fresh on
        # failure. See designs/FORK_SESSION_UX.md.
        from omnigent.claude_native import _ensure_local_claude_resume_transcript

        our_uuid = str(uuid.uuid4())
        _clone_workspace = Path(workspace).resolve()
        try:
            _built = await _ensure_local_claude_resume_transcript(
                server_client,
                session_id=session_id,
                external_session_id=our_uuid,
                workspace=_clone_workspace,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            _built = None
            _logger.warning(
                "Could not build native transcript from items for forked clone %s; "
                "launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Claude terminal fork-rebuild decision: session=%s our_uuid=%s "
            "clone_workspace=%s built_transcript=%s",
            session_id,
            our_uuid,
            _clone_workspace,
            str(_built) if _built is not None else None,
        )
        if _built is not None:
            resume_external_session_id = our_uuid
            # Record the assigned id so Omnigent reflects the clone's own Claude
            # session and a later relaunch resumes it via the cold-resume
            # path above. Best-effort, mirroring the clone branch.
            try:
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": our_uuid},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                _logger.warning(
                    "Could not pre-set external_session_id for forked clone %s; "
                    "relying on hook capture",
                    session_id,
                    exc_info=True,
                )
    _logger.info(
        "Claude terminal cold-resume decision: session=%s external_session_id_set=%s "
        "fork_source_set=%s resume_enabled=%s",
        session_id,
        session_external_id is not None,
        fork_source_external_id is not None,
        resume_external_session_id is not None,
    )

    # Derive the ucode (Databricks gateway) launch config from the
    # runner's own profile so a daemon / web-UI-launched Claude
    # authenticates to the gateway exactly like a CLI-launched one —
    # the CLI injects this in ``_claude_terminal_request``; on this path
    # the runner must, since it (not the CLI) launches the terminal.
    # Best-effort: no profile / no ucode state / malformed state falls
    # back to Claude's own native config (empty env). The runner env is
    # an allowlist that excludes ``ANTHROPIC_API_KEY`` /
    # ``CLAUDE_CODE_*``, so — unlike the CLI — there are no stray
    # provider/session vars to unset before the gateway env applies.
    # See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    # Resolve the launch config across all offerings — a configured provider
    # (omnigent setup), a Databricks ucode profile from provider config, or
    # Claude's own login — so a host-spawned native-claude session honors the
    # provider selection just like the in-process claude-sdk harness and the
    # CLI path.
    claude_config: ClaudeNativeUcodeConfig | None = None
    try:
        claude_config = resolve_native_claude_config(spec=None)
    except Exception:  # noqa: BLE001 — best-effort; fall back to native auth
        _logger.warning(
            "native-claude: could not derive a provider/ucode launch config "
            "— FALLING BACK to Claude Code's own login; "
            "your configured provider will NOT be used. Check "
            "`omnigent setup --no-internal-beta` "
            "and that the secret resolves in this process.",
            exc_info=True,
        )
    _logger.info(
        "Claude terminal provider config resolved: session=%s configured=%s "
        "env_keys=%s api_key_helper_set=%s model_set=%s",
        session_id,
        claude_config is not None,
        sorted(claude_config.env) if claude_config is not None else [],
        bool(claude_config.api_key_helper) if claude_config is not None else False,
        bool(claude_config.model) if claude_config is not None else False,
    )

    base_claude_args = _build_claude_native_base_args(
        reasoning_effort=session_effort,
        # Session override wins; the ucode gateway model is the default
        # when no per-session override is set. Both yield to an explicit
        # ``--model`` in the user's pass-through args (handled in the
        # helper).
        model_override=session_model_override
        or (claude_config.model if claude_config is not None else None),
        terminal_launch_args=session_launch_args,
        resume_external_session_id=resume_external_session_id,
    )

    # Pass ``ap_server_url`` so ``build_hook_settings`` registers the
    # claude-native ``PermissionRequest`` command hook and writes
    # permission_hook.json. Without it, the hook is silently omitted and
    # approval prompts never reach the web UI on this host-spawned path.
    # ``bundle_dir`` / ``skills_filter`` (resolved by the caller, which
    # has the spec resolver) expose a bundle's ``skills/`` to Claude Code
    # via ``--plugin-dir`` — the CLI mirror of the SDK plugin wiring.
    # ``api_key_helper`` (ucode) registers Claude's gateway token command.
    claude_args = augment_claude_args(
        base_claude_args,
        bridge_dir=bridge_dir,
        ap_server_url=server_url,
        ap_auth_headers=_runner_headers,
        bundle_dir=bundle_dir,
        agent_name=agent_name,
        skills_filter=skills_filter,
        api_key_helper=claude_config.api_key_helper if claude_config is not None else None,
    )

    # Inherit the agent's os_env so its sandbox (e.g. ``type: none``),
    # egress_rules and env_passthrough are honoured. Without ``sandbox`` here
    # and ``parent_os_env`` below, launch_terminal falls back to
    # _default_sandbox_for_platform (linux_bwrap), overriding the YAML config.
    agent_os_env = _agent_os_env_from_spec(agent_spec)
    env_spec = TerminalEnvSpec(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=workspace,
            sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
        ),
        command="claude",
        args=list(claude_args),
        # Tool Search env plus ucode gateway env (ANTHROPIC_BASE_URL
        # etc.) when derived. Empty provider config still forces
        # ENABLE_TOOL_SEARCH=true so MCP schemas are loaded on demand.
        env=build_native_claude_terminal_env(claude_config),
        # Strip the ambient Databricks-SDK profile selection from
        # the Claude tmux env. Claude's MCP servers inherit this env,
        # and several construct ``WorkspaceClient`` without pinning
        # ``auth_type``; when ``DATABRICKS_CONFIG_PROFILE`` is set,
        # the SDK's auth resolver picks up that profile's cached
        # OAuth token and ignores the explicit token the MCP was
        # configured with — sending a bearer minted for the wrong
        # workspace and getting back a 400 ``Invalid Token`` from
        # the right one. Claude itself doesn't read this env var
        # (provider routing is via ``ANTHROPIC_BASE_URL`` /
        # ``apiKeyHelper``), so dropping it from the terminal env
        # affects only the leak path. MCPs that genuinely need a
        # specific profile must declare it in their own per-MCP env
        # configuration rather than inheriting it from the runner.
        env_unset=["DATABRICKS_CONFIG_PROFILE"],
        scrollback=50000,
        # Keep the private tmux server alive if the `claude` CLI exits (e.g. a
        # sub-agent worker whose CLI exits right after rendering its prompt on
        # some hosts — #540). Without this, that exit reaps the server and every
        # later control command (send-keys / model / effort / interrupt / stop)
        # fails with "no server running", and the delegated message is silently
        # lost. With it, the dead pane persists (capturable for diagnostics) and
        # the watcher reports the exit deterministically via `#{pane_dead}`.
        keep_alive_after_exit=True,
    )
    _logger.info(
        "Claude terminal tmux launch requested: session=%s command=%s args_count=%d "
        "env_keys=%s cwd=%s scrollback=%d",
        session_id,
        env_spec.command,
        len(env_spec.args),
        sorted(env_spec.env),
        workspace,
        env_spec.scrollback,
    )
    try:
        terminal_view = await resource_registry.launch_required_terminal(
            session_id=session_id,
            terminal_name="claude",
            session_key="main",
            spec=env_spec,
            parent_os_env=agent_os_env,
            # Mark this as the claude-native agent terminal so its pane
            # activity drives the session's PTY-derived working status.
            resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
        )
    except Exception:
        _logger.exception(
            "Claude terminal tmux launch failed: session=%s elapsed_ms=%.0f",
            session_id,
            (time.monotonic() - started_at) * 1000,
        )
        raise
    # Surface the terminal on the live SSE stream so an already-connected
    # web UI enables the Terminal toggle immediately. The required-terminal
    # launch helper registers the resource and starts the activity watcher but
    # does not publish; the tool / REST launch paths emit this same event via
    # _emit_terminal_resource_event. Without it, this auto-created terminal
    # is only discovered on reconnect (snapshot-on-connect), so the toggle
    # stays gray until the user refreshes.
    from omnigent.entities.session_resources import session_resource_view_to_dict

    terminal_payload = session_resource_view_to_dict(terminal_view)
    terminal_metadata = terminal_payload.get("metadata")
    if not isinstance(terminal_metadata, dict):
        terminal_metadata = {}
    _logger.info(
        "Claude terminal tmux launch returned: session=%s terminal_id=%s running=%s "
        "tmux_socket=%s tmux_target=%s elapsed_ms=%.0f",
        session_id,
        terminal_payload.get("id"),
        terminal_metadata.get("running"),
        terminal_metadata.get("tmux_socket"),
        terminal_metadata.get("tmux_target"),
        (time.monotonic() - started_at) * 1000,
    )

    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": terminal_payload,
        },
    )
    _publish_tmux_target_for_bridge(
        resource_registry=resource_registry,
        session_id=session_id,
        # The bridge dir was created via ``prepare_bridge_dir(session_id)``
        # above (no explicit bridge_id), so it is keyed by session_id.
        # Pass the same id so the tmux target lands in that dir and the
        # claude-native harness can find it.
        bridge_id=session_id,
        terminal_name="claude",
        session_key="main",
    )
    _logger.info(
        "Claude terminal tmux target published: session=%s bridge_id=%s",
        session_id,
        session_id,
    )

    # Start the transcript forwarder so Claude's responses flow
    # back to the Omnigent server. Normally the CLI client runs this,
    # but for host-spawned sessions there is no CLI. Reuses the
    # ``server_url`` + auth computed above; ``auth`` refreshes the
    # bearer token per request so forwarding outlives token expiry.
    #
    # ``start_at_end`` must be ``True`` on resume: when
    # ``resume_external_session_id`` is set we launched Claude with
    # ``--resume`` over a transcript synthesized from AP's committed
    # history (see ``_ensure_local_claude_resume_transcript`` above), so
    # offset 0 already holds every item Omnigent has. Starting the forwarder at
    # offset 0 would re-post the whole transcript as new external
    # conversation items — there is no server-side dedup — duplicating the
    # visible history on every resume. A genuinely fresh
    # session (no ``--resume``) starts with an empty transcript, so
    # ``False`` correctly forwards everything from the beginning. This
    # mirrors the CLI client's ``prepared.cold_resumed`` handling in
    # ``claude_native.py``.
    from omnigent.claude_native_forwarder import supervise_forwarder

    _forwarder_task = asyncio.create_task(
        supervise_forwarder(
            base_url=server_url,
            headers=_runner_headers,
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=resume_external_session_id is not None,
            auth=_runner_auth,
        ),
        name=f"claude-forwarder-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created claude terminal + forwarder for session %s; "
        "forwarder_task=%s elapsed_ms=%.0f",
        session_id,
        _forwarder_task.get_name(),
        (time.monotonic() - started_at) * 1000,
    )
    return terminal_view


async def _auto_create_repl_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    server_client: httpx.AsyncClient,
    agent_spec: AgentSpec | ResolvedSpec | None = None,
) -> SessionResourceView:
    """
    Auto-create an Omnigent REPL terminal for a runner-hosted SDK session.

    Called when the runner receives a non-native (SDK-harness) top-level
    session via ``POST /v1/sessions`` and no REPL terminal exists yet. The
    terminal hosts the framework's own TUI (``omnigent attach
    <session_id> --server <url>``) in a tmux pane, exposed through the
    standard terminal-attach WebSocket so the web UI embeds it exactly
    like the claude-/codex-native terminals — with the Omnigent REPL as
    the TUI.

    The REPL is a pure co-drive client: it joins the live session over
    HTTP+SSE and dispatches turns to this runner, so the web chat view and
    the embedded terminal stay in sync. The tmux command is deferred until
    the first client attaches (``tmux_start_on_attach``): a session whose
    terminal is never opened pays only for an idle tmux pane, and by first
    attach the session is fully live (``omnigent attach`` fails loud on a
    non-live session) with the REPL sized to the real attached terminal.

    Auth parity with the native terminals: the spawned ``omnigent
    attach`` resolves credentials for ``--server`` the same way a
    user-launched CLI does (``OMNIGENT_REMOTE_AUTH_TOKEN`` env → stored
    OIDC token from ``omnigent login`` → ``~/.databrickscfg``), which
    holds because the runner lives on the user's machine.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_registry: Session resource registry for launching the
        terminal.
    :param publish_event: The runner's per-session SSE emitter,
        ``(session_id, event_dict) -> None``, used to surface the new
        terminal on the live stream so the web UI's Terminal pill enables
        without a refresh.
    :param server_client: Omnigent server client used to stamp the
        ``omnigent.ui: terminal`` presentation label that makes the web
        UI show the Chat/Terminal toggle.
    :returns: The launched terminal's :class:`SessionResourceView`.
    """
    from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, UI_MODE_TERMINAL_VALUE
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

    started_at = time.monotonic()
    workspace = os.environ.get("OMNIGENT_RUNNER_WORKSPACE", str(Path.cwd()))
    server_url = os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767")
    # Inherit the agent's os_env so its sandbox (e.g. ``type: none``) is honoured;
    # without sandbox= here and parent_os_env below, launch_terminal falls back to
    # _default_sandbox_for_platform (linux_bwrap), which fails in a hardened
    # container. Mirrors the #175 fix on the codex/claude auto-create paths.
    agent_os_env = _agent_os_env_from_spec(agent_spec)
    env_spec = TerminalEnvSpec(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=workspace,
            sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
        ),
        # The runner's interpreter is the venv with omnigent installed;
        # ``python -m omnigent`` avoids depending on the console script
        # being on the tmux pane's PATH.
        command=sys.executable,
        args=["-m", "omnigent", "attach", session_id, "--server", server_url],
        scrollback=50000,
        # Defer the REPL process until the first web client attaches (see
        # docstring): no cost for never-opened terminals, and the REPL
        # starts against the real attached terminal size.
        tmux_start_on_attach=True,
    )
    terminal_view = await resource_registry.launch_auxiliary_terminal(
        session_id=session_id,
        terminal_name=_REPL_TERMINAL_NAME,
        session_key=_REPL_TERMINAL_SESSION_KEY,
        spec=env_spec,
        parent_os_env=agent_os_env,
        # Runner-private marker the attach WebSocket uses to recreate
        # this terminal when its tmux session has died (the REPL exited
        # or crashed) instead of rejecting the attach.
        resource_role=OMNIGENT_REPL_TERMINAL_ROLE,
    )
    # Stamp the presentation label that gates the web UI's Chat/Terminal
    # pill (ap-web TerminalFirstContext). Stamped here — not at session
    # creation — so only sessions whose runner actually hosts a REPL
    # terminal get the toggle; in-process (runner-less) sessions never
    # show a dead pill. The ``omnigent.wrapper`` label is deliberately
    # NOT set: these sessions stay chat-first, the terminal is a
    # secondary view.
    try:
        await server_client.patch(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            json={"labels": {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE}},
        )
    except httpx.HTTPError:
        _logger.warning(
            "Could not stamp %s label for %s; the web Terminal toggle may not appear",
            UI_MODE_LABEL_KEY,
            session_id,
        )
    # Surface the terminal on the live SSE stream so an already-connected
    # web UI enables the Terminal toggle immediately (the auxiliary-terminal
    # launch helper registers the resource but does not publish — mirrors the
    # claude-native auto-create path).
    from omnigent.entities.session_resources import session_resource_view_to_dict

    terminal_payload = session_resource_view_to_dict(terminal_view)
    publish_event(
        session_id,
        {
            "type": "session.resource.created",
            "resource": terminal_payload,
        },
    )
    _logger.info(
        "Auto-created omnigent REPL terminal for session %s: terminal_id=%s "
        "server_url=%s elapsed_ms=%.0f",
        session_id,
        terminal_payload.get("id"),
        server_url,
        (time.monotonic() - started_at) * 1000,
    )
    return terminal_view


async def _claude_native_bridge_id_for_session(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
) -> str:
    """Resolve the bridge id label for a Claude-native session.

    :param server_client: Omnigent server client used to fetch the session
        snapshot.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Opaque bridge id from
        ``omnigent.claude_native.bridge_id`` when present, otherwise
        *session_id* for legacy single-session bridges.
    """
    from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY

    labels = await _session_labels_for_runner_spawn(
        server_client=server_client,
        session_id=session_id,
    )
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY)
    if isinstance(bridge_id, str) and bridge_id:
        return bridge_id
    return session_id


async def _claude_native_session_wants_rebuild(
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> bool:
    """
    Return whether a claude-native session is pending a post-switch rebuild.

    An in-place agent switch into claude-native clears the session's
    ``external_session_id`` and stamps the carry-history label, so the next
    launch must re-synthesize the Claude transcript from the CURRENT AP items.
    But when the session was ALREADY claude-native before the switch, its
    original terminal can still be registered (an open terminal tab keeps it
    alive). The auto-create that performs the re-synthesis is skipped while a
    terminal exists, so the switched-back agent keeps its original on-disk
    transcript — missing the turns added on the other agent. Detecting this
    lets the caller tear the stale terminal down first. A normal resume
    (``external_session_id`` already set) returns ``False`` so its terminal is
    left untouched.

    :param server_client: AP client; ``None`` can't confirm, returns ``False``.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: ``True`` when ``external_session_id`` is unset AND the
        carry-history label is set (a pending rebuild), else ``False``.
    """
    if server_client is None:
        return False
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    snap = resp.json()
    # A captured native session means this is a normal resume, not a switch.
    if snap.get("external_session_id"):
        return False
    labels = snap.get("labels")
    return isinstance(labels, dict) and labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"


async def _claude_native_terminal_arrives_via_transfer(
    *,
    server_client: httpx.AsyncClient | None,
    session_id: str,
    resource_registry: SessionResourceRegistry,
) -> bool:
    """
    Return whether a live Claude terminal will be transferred into a session.

    A ``/clear`` / ``/fork`` rotation binds the runner to a fresh session
    before transferring the existing terminal onto it; auto-creating a
    second Claude here would 409 the transfer and loop the rotation
    (rotation loop). The shared-bridge ``active_session_id`` still names the
    live terminal-owning session at bind time, detected here so the
    caller skips auto-create and lets the transfer deliver the terminal.

    :param server_client: Omnigent client to resolve the bridge id label;
        ``None`` can't confirm a rotation, so returns ``False``.
    :param session_id: Newly-bound session id, e.g. ``"conv_new"``.
    :param resource_registry: Registry probed for the original session's
        live ``claude:main`` terminal.
    :returns: ``True`` when a different session on the same bridge owns a
        live ``claude:main`` terminal (transfer inbound), else ``False``.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return False
    # Lazy import keeps claude-native out of the generic runner import graph.
    from omnigent.claude_native_bridge import (
        bridge_dir_for_bridge_id,
        read_active_session_id,
    )

    bridge_id = await _claude_native_bridge_id_for_session(
        server_client=server_client,
        session_id=session_id,
    )
    active_session_id = read_active_session_id(bridge_dir_for_bridge_id(bridge_id))
    # Fresh bridge, or the new session is already active — nothing transfers in.
    if active_session_id is None or active_session_id == session_id:
        return False
    return terminal_registry.get(active_session_id, "claude", "main") is not None


async def _antigravity_native_terminal_arrives_via_transfer(
    *,
    server_client: httpx.AsyncClient | None,
    session_id: str,
    resource_registry: SessionResourceRegistry,
) -> bool:
    """
    Return whether a live agy terminal will be transferred into a session.

    The antigravity mirror of :func:`_claude_native_terminal_arrives_via_transfer`.
    A TUI ``/clear`` rotation (see
    :func:`omnigent.antigravity_native_reader._rotate_session_for_cascade`) binds the
    runner to a fresh session, then transfers the existing agy terminal onto it —
    agy is one long-lived process hosting many cascades, so the rotation re-homes the
    SAME process rather than spawning a second one. Auto-creating a redundant agy
    here would cold-start a brand-new agy whose own ``external_session_id`` then 400s
    the rotation's PATCH and loops it (the bug this guard fixes). The shared bridge
    state still names the live terminal-owning session at bind time (the rotation
    rewrites it only AFTER the transfer), detected here so the caller skips
    auto-create and lets the transfer deliver the terminal.

    :param server_client: Omnigent client to resolve the bridge id label;
        ``None`` can't confirm a rotation, so returns ``False``.
    :param session_id: Newly-bound session id, e.g. ``"conv_new"``.
    :param resource_registry: Registry probed for the original session's live
        ``antigravity:main`` terminal.
    :returns: ``True`` when a different session on the same bridge owns a live
        ``antigravity:main`` terminal (transfer inbound), else ``False``.
    """
    terminal_registry = resource_registry.terminal_registry
    if terminal_registry is None:
        return False
    # Lazy import keeps antigravity-native out of the generic runner import graph.
    from omnigent.antigravity_native_bridge import (
        ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
        read_bridge_state,
    )

    if server_client is None:
        return False
    labels = await _session_labels_for_runner_spawn(
        server_client=server_client,
        session_id=session_id,
    )
    bridge_id = labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id
    state = read_bridge_state(bridge_dir_for_bridge_id(bridge_id))
    # Fresh bridge, or the new session is already active — nothing transfers in.
    if state is None or state.session_id == session_id:
        return False
    return terminal_registry.get(state.session_id, "antigravity", "main") is not None


_SESSION_LABEL_LOOKUP_TIMEOUT_SECONDS = 1.0


async def _session_labels_for_runner_spawn(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, str]:
    """
    Fetch session labels for harness spawn-env construction.

    :param server_client: Omnigent server client used to fetch the session
        labels endpoint.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: String label mapping. Empty on lookup failure.
    """
    path = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/labels"
    try:
        resp = await server_client.get(
            path,
            timeout=_SESSION_LABEL_LOOKUP_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        _logger.debug(
            "Timed out resolving session labels; session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        return {}
    except httpx.HTTPError as exc:
        _logger.warning(
            "Failed to resolve session labels; session=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        return {}
    if resp.status_code != 200:
        _logger.warning(
            "Failed to resolve session labels; session=%s status=%s",
            session_id,
            resp.status_code,
        )
        return {}
    try:
        labels = resp.json().get("labels")
    except ValueError:
        # A 200 with a non-JSON body (e.g. an empty response from the
        # Databricks Apps proxy when the server event loop is starved,
        # or an HTML login page on an auth edge) must not abort the
        # turn. Labels are a best-effort spawn hint; recover by using
        # the session id, exactly as the timeout / non-200 paths do.
        _logger.warning(
            "Session labels response was not valid JSON; session=%s status=%s",
            session_id,
            resp.status_code,
        )
        return {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


# Marker the runner stamps on action_required SSE events it intends
# to dispatch locally. See designs/RUNNER_MCP.md §Explicit dispatch
# marker.
_RUNNER_DISPATCHED_FIELD = "omnigent_runner_dispatched"


def _encode_sse_event(event: dict[str, Any]) -> bytes:
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
    data: dict[str, Any],
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
    verdict_data: dict[str, Any] | None = None

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
        verdict_body: dict[str, Any] = {
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


def _forward_harness_response(resp: httpx.Response) -> Response:
    """Safely relay a non-streaming harness response through FastAPI.

    Starlette's ``JSONResponse(status_code=204, content=None)`` serializes
    ``None`` as ``b\"null\"``. Uvicorn correctly treats 204/304 as no-body
    responses and raises ``RuntimeError(\"Response content longer than
    Content-Length\")`` when any bytes are sent. Return a plain empty
    ``Response`` for no-body status codes (204/304).  For other statuses with
    an empty body, forward an explicit empty body while preserving
    ``content-type`` so callers can distinguish e.g. a 200 with no payload
    from a 204.
    """
    if resp.status_code in _NO_BODY_STATUS_CODES:
        return Response(status_code=resp.status_code)

    content_type = resp.headers.get("content-type", "")

    if not resp.content:
        return Response(
            content=b"",
            status_code=resp.status_code,
            media_type=content_type or None,
        )

    if "application/json" in content_type.lower():
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except ValueError:
            # Fall through to raw bytes if an upstream mislabels non-JSON content.
            pass

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type or None,
    )


def _response_body_preview(resp: Any, *, limit: int = 500) -> str:
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
class ResolvedSpec:
    spec: Any
    workdir: Path

    def __getattr__(self, name: str) -> Any:
        return getattr(self.spec, name)


def _unwrap_resolved_spec(entry: Any) -> Any:
    return entry.spec if isinstance(entry, ResolvedSpec) else entry


def _resolved_spec_workdir(entry: Any) -> Path | None:
    return entry.workdir if isinstance(entry, ResolvedSpec) else None


def _resolved_workdir_for_spec(spec: Any, fallback: Path | None) -> Path | None:
    """Return the bundle workdir for a possibly wrapped spec entry."""
    return _resolved_spec_workdir(spec) or fallback


def _is_spec_local_native_python_tool(spec: Any, tool_name: str) -> bool:
    """Return whether *tool_name* is a spec-declared native python tool."""
    unwrapped = _unwrap_resolved_spec(spec)
    return any(
        getattr(info, "name", None) == tool_name
        and getattr(info, "language", None) in ("python", "omnigent-python-callable")
        for info in getattr(unwrapped, "local_tools", [])
    )


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
    """

    ok: bool
    status_code: int | None
    created_at: float
    workspace: str | None
    agent_id: str | None
    sub_agent_name: str | None = None


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


def _spec_with_workdir_paths(spec: Any, workdir: Path | None) -> Any:
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


def _merge_advisor_note(
    content: list[dict[str, Any]] | str | None,
    note_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Merge the advisor note into the turn's user message, copy-on-write.

    The note must NOT be appended as its own trailing user message: the
    claude-sdk executor sends only the LATEST user message on resumed
    sessions (``_build_prompt``), so a trailing note-only message would
    shadow the user's actual question — the brain answers the note
    ("Got it, the model is now set to …") and the question is silently
    dropped. Riding the note's text inside the real user message keeps
    the question primary and the note visible.

    Handles both body shapes that reach the advisor: history-shaped
    message items (the background-turn path) get the note blocks
    appended to the latest ``role == "user"`` message; raw content
    blocks (the ``?stream=true`` path) and string shorthand get the
    note appended as additional ``input_text`` blocks of the same
    message.

    :param content: The harness body's ``content`` — message items,
        e.g. ``[{"type": "message", "role": "user", "content":
        [{"type": "input_text", "text": "refactor x"}]}]``, OR content
        blocks, e.g. ``[{"type": "input_text", "text": "refactor x"}]``,
        OR a plain-string shorthand, OR ``None``.
    :param note_item: The advisor's note message item (see
        :func:`omnigent.runner.cost_advisor._advisor_note_item`), e.g.
        ``{"type": "message", "role": "user", "content": [{"type":
        "input_text", "text": "[Cost advisor: …]"}]}``.
    :returns: A new content list with the note merged in; the input list
        and the merged message are copied so the cached session history
        is never mutated.
    """
    note_blocks = list(note_item.get("content") or [])
    if isinstance(content, str):
        # String shorthand: normalize to blocks so the note can ride along.
        return [{"type": "input_text", "text": content}, *note_blocks]
    items: list[dict[str, Any]] = list(content or [])
    for i in range(len(items) - 1, -1, -1):
        item = items[i]
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        merged = dict(item)
        existing = merged.get("content")
        if isinstance(existing, str):
            existing = [{"type": "input_text", "text": existing}]
        merged["content"] = [*(existing or []), *note_blocks]
        items[i] = merged
        return items
    if any(isinstance(it, dict) and it.get("type") == "message" for it in items):
        # Message-shaped history with no user message (degenerate): keep the
        # old trailing-item behavior rather than dropping the note.
        return [*items, note_item]
    # Raw content blocks: the whole list IS the user message's content.
    return [*items, *note_blocks]


def _apply_advisor_to_body(
    body: dict[str, Any],
    result: AdvisorTurnResult,
) -> None:
    """
    Apply a cost-advisor turn result to the harness request body in place.

    Optimize mode (claude-sdk, no user pin): sets ``model_override`` so the
    inner executor runs THIS turn on the verdict model via its per-turn
    ``set_model`` (claude_sdk_executor: switches only when the model
    changes between turns), and merges the one-line system note into the
    turn's user message (see :func:`_merge_advisor_note`). Advise
    mode (or a user pin / non-applicable harness): ``apply_model`` and
    ``note_item`` are both ``None``, so the body is unchanged — the verdict
    is shadow-recorded in the label only.

    :param body: The harness request body, mutated in place. The caller
        must own this dict (copy-on-write at the streaming call site) so
        the cached session history is not mutated.
    :param result: The advisor turn result.
    """
    if result.apply_model is not None:
        # Per-turn brain-model override; flows to ExecutorConfig.model in
        # the harness adapter, then cfg.model in the claude-sdk executor.
        body["model_override"] = result.apply_model
    if result.note_item is not None:
        body["content"] = _merge_advisor_note(body.get("content"), result.note_item)


def _wrap_as_message_event(body: dict[str, Any]) -> dict[str, Any]:
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
    Raised by the proxy_stream when the harness reports a context-window overflow.

    Caught by ``_run_turn_bg_setup_and_stream`` to end the turn with
    a descriptive error.

    :param max_tokens: The model's context window, e.g. ``128000``.
    :param actual_tokens: The prompt size that overflowed, e.g. ``131072``.
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


def _is_context_overflow_error(event: dict[str, Any]) -> tuple[int, int] | None:
    """
    Check if a ``response.failed`` SSE event indicates a context-window overflow.

    :param event: The parsed SSE event dict.
    :returns: ``(max_tokens, actual_tokens)`` if overflow detected, else ``None``.
    """
    if event.get("type") != "response.failed":
        return None
    error = event.get("error", {})
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


def _response_failed_event(error: dict[str, Any]) -> bytes:
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
    content: list[dict[str, Any]],
    *,
    session_id: str,
    server_client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Resolve server-uploaded ``file_id`` blocks inside the runner.

    Remote Omnigent servers can forward session messages with raw file IDs
    because their file store is not available to the out-of-process
    runner. The runner can still fetch bytes through the session-scoped
    file resource endpoint and inline them before handing content to a
    harness. Blocks already resolved by the server pass through.
    """
    if not any(isinstance(block, dict) and "file_id" in block for block in content):
        return content

    import base64 as _base64

    resolved: list[dict[str, Any]] = []
    changed = False
    for block in content:
        if not isinstance(block, dict) or "file_id" not in block:
            resolved.append(block)
            continue
        file_id = block.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            resolved.append(block)
            continue
        try:
            meta_resp = await server_client.get(
                f"/v1/sessions/{session_id}/resources/files/{file_id}",
                timeout=10.0,
            )
            content_resp = await server_client.get(
                f"/v1/sessions/{session_id}/resources/files/{file_id}/content",
                timeout=30.0,
            )
            meta_resp.raise_for_status()
            content_resp.raise_for_status()
        except httpx.HTTPError:
            _logger.warning(
                "runner failed to resolve file_id=%s for session=%s",
                file_id,
                session_id,
                exc_info=True,
            )
            resolved.append(block)
            continue

        meta = meta_resp.json()
        content_type = (
            meta.get("content_type")
            or content_resp.headers.get("content-type")
            or "application/octet-stream"
        )
        # Strip any charset suffix: data URIs need the media type hint.
        if isinstance(content_type, str):
            content_type = content_type.split(";", 1)[0]
        else:
            content_type = "application/octet-stream"
        encoded = _base64.b64encode(content_resp.content).decode("ascii")
        new_block = {k: v for k, v in block.items() if k != "file_id"}
        if block.get("type") == "input_image":
            new_block["image_url"] = f"data:{content_type};base64,{encoded}"
        else:
            new_block["file_data"] = f"data:{content_type};base64,{encoded}"
        resolved.append(new_block)
        changed = True

    return resolved if changed else content


def _inject_mcp_schemas(
    event_body: dict[str, Any],
    mcp_schemas: list[dict[str, Any]],
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
    existing = event_body.get("tools") or []
    existing_names = {t.get("name") for t in existing if t.get("name")}
    new_schemas = [s for s in mcp_schemas if s.get("name") not in existing_names]
    event_body["tools"] = list(existing) + new_schemas


def _schema_tool_name(schema: dict[str, Any]) -> str | None:
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
    spec_tools: list[dict[str, Any]],
    client_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
    merged: list[dict[str, Any]] = list(spec_tools)
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
    :returns: ``True`` if a 2xx was confirmed, ``False`` if every attempt
        failed (transport error, timeout, or non-2xx response).
    """
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


def _normalize_turn_error(error: dict[str, Any]) -> dict[str, str]:
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
_session_histories_ref: dict[str, list[dict[str, Any]]] = {}

# Module-level ref to _session_event_queues. Populated inside
# create_runner_app; used by tests to inspect the queue an SSE
# subscriber would have read (events published synchronously by
# ``_publish_event`` are visible by the time the producer's await
# call returns, so tests don't need to subscribe to the HTTP
# ``/stream`` endpoint just to assert on emitted events).
_session_event_queues_ref: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}

# Module-level ref to _session_inboxes. Populated inside create_runner_app;
# used by the sub-agent work registry to deliver completions to the parent.
_session_inboxes_ref: dict[str, asyncio.Queue[dict[str, Any]]] = {}


def get_session_agent_id(session_id: str) -> str | None:
    """
    Return the durable agent_id for a session.

    :param session_id: Session/conversation ID, e.g.
        ``"conv_abc123"``.
    :returns: The agent_id, or ``None`` if not found.
    """
    return _session_agent_ids_ref.get(session_id)


def create_runner_app(
    *,
    process_manager: HarnessProcessManager | None = None,
    spec_resolver: SpecResolver | None = None,
    server_client: httpx.AsyncClient,
    terminal_registry: Any | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    runner_workspace: Path | None = None,
    per_session_workspace: bool = True,
    mcp_manager: Any | None = None,
    auth_token: str | None = None,
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
    """
    import hmac

    app = FastAPI(title="omnigent-runner")

    # Runner-side auth middleware.
    if auth_token is not None:
        _expected_token = auth_token

        @app.middleware("http")
        async def _runner_auth_middleware(request: Request, call_next: Any) -> Response:
            """Reject requests without a valid bearer token.

            Requests arriving through the WebSocket tunnel have
            ASGI client ``("tunnel", 0)`` and are already
            authenticated by the tunnel handshake — exempt them.

            :param request: Incoming HTTP request.
            :param call_next: Next middleware / route handler.
            :returns: The response, or 401 on auth failure.
            """
            if request.url.path == "/health":
                return await call_next(request)
            # Tunnel-dispatched requests are already authenticated
            # by the WebSocket tunnel registration handshake.
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

    # Set the terminal registry as the runtime global so ToolManager
    # can find it when constructing tool schemas. The runner already
    # owns and dispatches terminal tools — this just lets ToolManager
    # register them for schema extraction.
    if terminal_registry is not None:
        from omnigent.runtime import _globals as _rt_globals

        _rt_globals._terminal_registry = terminal_registry

    _version_cache: dict[str, int] = {}  # conversation_id → last seen agent_version
    _spec_cache: dict[str, Any] = {}  # agent_id → cached AgentSpec for terminal tools
    _resp_to_conv: dict[str, str] = {}  # harness response_id → conversation_id
    # conv_id → live turn's response_id; gates the mid-turn injection forward so
    # a buffered message isn't sent to a harness with no live turn (→ 204).
    _live_response_id: dict[str, str] = {}
    _session_start_cache: dict[str, float] = {}  # session_id → registered start time
    _session_spec_cache: dict[str, Any | None] = {}  # session_id → session AgentSpec
    # Single source for the session's server snapshot. created_at,
    # workspace, and agent_id are all projected out of one
    # GET /v1/sessions/{id}; the projection caches above/below are
    # populated from here. Guarded by per-session locks so a startup
    # burst of concurrent readers shares one fetch instead of stampeding.
    _session_snapshot_cache: dict[str, _SessionSnapshot] = {}  # session_id → snapshot
    _session_snapshot_locks: dict[str, asyncio.Lock] = {}  # session_id → snapshot fetch lock
    _session_spec_locks: dict[str, asyncio.Lock] = {}  # session_id → spec resolution lock
    # session_id → merged (bundled + host) skills, discovered against
    # this runner's filesystem. Skills are runner-owned: the walk runs
    # once per session lifetime and is dropped in ``delete_session``.
    _session_skills_cache: dict[str, list[SkillSpec]] = {}
    _session_workspace_cache: dict[str, str | None] = {}  # session_id → workspace path
    _session_agent_ids = _session_agent_ids_ref  # shared with module-level get_session_agent_id
    # Sub-agent name per session. Set from POST /v1/sessions body
    # for child sessions. _run_turn_bg uses this to resolve the
    # sub-spec from the parent's spec tree.
    _session_sub_agent_names: dict[str, str] = {}
    _session_tool_schemas: dict[str, list[dict[str, Any]]] = {}  # session_id → cached tool schemas
    _session_mcp_spec_hash: dict[str, str] = {}  # session_id → last MCP spec hash
    # session_id → the brain model the cost advisor last APPLIED (optimize
    # mode). Carried forward on conversational turns so the brain doesn't
    # flap back to the spec/gateway default between advised turns; the
    # claude-sdk executor only re-runs set_model when the model changes.
    _session_advisor_applied_model: dict[str, str] = {}
    # Per-session comment-tool relay for claude-native sessions. Value is a
    # ClaudeNativeToolRelay handle; ``Any`` avoids importing the class at
    # module load time. Started when the Claude terminal launches (with a
    # first-turn fallback) and closed when the session is deleted.
    _session_comment_relays: dict[str, Any] = {}
    _codex_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _pi_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _opencode_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _cursor_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _kiro_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _goose_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _qwen_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _kimi_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    _hermes_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    # Per-session lock guarding the claude-native terminal auto-create in
    # ``create_session``. Two ``POST /v1/sessions`` calls can land
    # concurrently on a host-launched runner — ``_on_runner_connect``
    # (server/app.py) fires one on every tunnel connect, and the message
    # path's relaunch handshake fires another — so the check-and-create
    # must serialize or both pass the "no terminal yet" test and double
    # launch (409 / rotation loop).
    _claude_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    # Same guard for the runner-owned native Antigravity (agy) terminal
    # auto-created in ``create_session`` / the terminals-endpoint ensure path.
    # Exposed on app.state below so teardown tests can assert it is pruned (a
    # leaked Lock per session otherwise accumulates for the app's lifetime).
    _antigravity_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    app.state.antigravity_terminal_ensure_locks = _antigravity_terminal_ensure_locks
    # Same guard for the Omnigent REPL (``omnigent attach``) terminal
    # auto-created for non-native SDK sessions.
    _repl_terminal_ensure_locks: dict[str, asyncio.Lock] = {}
    # Turn sequencing (SESSION_REARCHITECTURE Step 5 / SESSION_STEERING_MIGRATION Step 1)
    _active_turns: dict[str, asyncio.Task[None] | None] = {}
    _session_message_buffers: dict[str, list[dict[str, Any]]] = {}
    # Per-conversation message-ingest ordering (RUNNER_MESSAGE_INGEST.md
    # Part A). Each inbound ``message`` event takes a monotonic arrival
    # sequence from ``_ingest_next_seq`` (read-incremented synchronously,
    # so it reflects arrival order), then waits at a FIFO gate until every
    # earlier-arriving message for that conversation has finished its
    # turn-vs-buffer decision (``_ingest_now_serving`` is the sequence
    # currently allowed to proceed; ``_ingest_cond`` wakes waiters). This
    # makes turn ordering follow arrival order, not content-resolution
    # latency — a slow-resolving message can no longer be overtaken.
    _ingest_next_seq: dict[str, int] = {}
    _ingest_now_serving: dict[str, int] = {}
    _ingest_cond: dict[str, asyncio.Condition] = {}
    # Closure-local (one per app instance — a module global would leak stale
    # interrupt flags between distinct create_runner_app() instances in the
    # same process). Exposed on app.state below for test inspection.
    _interrupted_sessions: set[str] = set()
    app.state.interrupted_sessions = _interrupted_sessions
    _background_tasks: set[asyncio.Task[Any]] = set()
    # Parent sessions with an outstanding sub-agent wake POST. Debounces a
    # fan-out's completions: while a parent's wake is outstanding, further
    # child completions skip posting another /events notice (they still land
    # in the inbox, which one wake turn drains). Cleared when the parent
    # starts processing a turn, so a child completion that lands during that
    # turn can schedule the next wake instead of being stranded in the inbox.
    _subagent_wake_pending: set[str] = set()
    # Pending policy-ASK Futures are now owned by
    # ``omnigent.runner.pending_approvals`` so the runner-side
    # policy gate (``omnigent.runner.tool_dispatch``) can register
    # and wait without threading a closure-local dict through every
    # dispatch entry point. The session-event handler below still
    # resolves Futures by elicitation_id; it just routes through the
    # shared module instead of a closure local.

    # Per-session in-memory conversation history. Loaded from the
    # server on the first turn, then appended locally as events
    # flow through proxy_stream. Each entry is a harness input
    # item: {type: "message", role: "user"|"assistant", content: [...]}.
    _session_histories = _session_histories_ref
    # Last server-persisted item ID per session — cursor for
    # incremental catch-up scans (Step 8.5 Scenario B).
    _last_server_item_id: dict[str, str] = {}
    # Per-session SSE event queue. proxy_stream and turn lifecycle
    # helpers put events here; GET /stream reads and removes them.
    # Events accumulate while no subscriber is reading, so tunnel
    # drops don't lose events — the relay drains on reconnect.
    _session_event_queues = _session_event_queues_ref
    # Per-session async inbox queues for sys_call_async /
    # sys_read_inbox (SESSION_REARCHITECTURE Step 7 partial).
    _session_inboxes = _session_inboxes_ref
    # Per-session background async tasks keyed by handle_id.
    # Each entry is (task, cancel_event) so cancellation is instant.
    _session_async_tasks: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}

    def _has_active_work() -> bool:
        """
        Return whether this runner is currently executing agent work.

        Used by the out-of-process runner's inactivity watchdog. The
        closure-local ``_active_turns`` catches turns owned directly by
        ``runner/app.py``; ``process_manager.has_active_turn`` catches
        in-flight responses tracked by the harness subprocess manager.

        :returns: ``True`` while any session has an active agent turn.
        """
        if _active_turns:
            return True
        if process_manager is None:
            return False
        session_ids = set(_session_start_cache) | set(_session_agent_ids)
        return any(process_manager.has_active_turn(session_id) for session_id in session_ids)

    app.state.has_active_work = _has_active_work

    def _publish_event(session_id: str, event: dict[str, Any]) -> None:
        """Put an event on the session's queue for GET /stream.

        Creates the queue lazily if it doesn't exist — handles
        the case where a turn runs before POST /v1/sessions
        initializes session state (e.g. on resume when the
        tunnel connect callback fires before the runner client
        is ready).

        :param session_id: Session/conversation identifier.
        :param event: The SSE event dict to enqueue.
        """
        queue = _session_event_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            _session_event_queues[session_id] = queue
        queue.put_nowait(event)
        # Mirror a child sub-agent's status / preview deltas onto the
        # PARENT's stream. No-op for non-child sessions. Single chokepoint
        # so every session.status publish is covered.
        _fan_out_child_delta_to_parent(session_id, event)

    def _child_preview_from_status(
        session_id: str,
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> str | None:
        """
        Return a child-session preview for an idle status edge.

        Native terminal status must pass AP-forwarded text and disable the
        history fallback because Omnigent owns native transcript persistence. The
        fallback remains for in-process harnesses whose assistant text is
        accumulated only in runner-local history.

        :param session_id: Child session id, e.g. ``"conv_child123"``.
        :param latest_assistant_text: Authoritative assistant text forwarded
            with an external status event, e.g. ``"done"``.
        :param allow_history_preview_fallback: Whether to read runner-local
            history when no explicit assistant text was provided.
        :returns: Truncated preview text, or ``None`` when there is no
            non-empty preview source.
        """
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
    ) -> dict[str, Any]:
        """
        Build the ``child`` object for a parent-stream status update.

        :param session_id: Child session id, e.g. ``"conv_child123"``.
        :param meta: Registered child-to-parent fan-out metadata.
        :param status: Child session status, e.g. ``"running"``.
        :param error: Failure detail from the ``session.status`` event.
        :param include_error: Whether to include ``last_task_error`` in the
            partial payload. ``True`` for failed edges and for activity edges
            that clear a stale failure.
        :returns: Child summary payload for ``session.child_session.updated``.
        """
        busy = status in ("running", "waiting")
        child = {
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
        event: dict[str, Any],
    ) -> dict[str, str] | None:
        """
        Extract typed failure details from a generic ``session.status`` event.

        :param status: Status value from the event, e.g. ``"failed"``.
        :param event: Published status event.
        :returns: ``{"code": "...", "message": "..."}`` for failed events
            with a valid error payload, otherwise ``None``.
        """
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
    ) -> dict[str, Any] | None:
        """
        Build a parent-stream child update for one status edge.

        :param session_id: Child session id, e.g. ``"conv_child123"``.
        :param meta: Registered child-to-parent fan-out metadata.
        :param status: Child session status, e.g. ``"running"``.
        :param error: Failure detail from a failed ``session.status`` edge.
        :param latest_assistant_text: Explicit preview text, e.g. ``"done"``.
        :param allow_history_preview_fallback: Whether to read runner history.
        :returns: Update event, or ``None`` when busy/task status did not change.
        """
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
        event: dict[str, Any],
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> None:
        """Republish a child's status/preview delta onto its parent's stream.

        Used for both runner-published ``session.status`` events and synthetic
        native status projections. It coalesces busy-state edges and emits
        ``session.child_session.updated`` on the parent stream.

        :param session_id: Session the event was published for.
        :param event: Published or synthetic status event, e.g.
            ``{"type": "session.status", "status": "running"}``.
        :param latest_assistant_text: Authoritative assistant text from an
            external terminal status, e.g. ``"done"``.
        :param allow_history_preview_fallback: Whether an idle child update
            may read runner-local history when explicit text is missing.
        """
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
        """Publish a transient terminal-activity pulse onto the session stream.

        Invoked on the event loop by the resource registry's per-terminal
        pane watcher when the pane produces output. The web turns this
        into the "active" badge for any terminal — no client PTY attach.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id, e.g.
            ``"terminal_zsh_s1"``.
        """
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
        """Publish a PTY-activity-derived ``session.status`` edge.

        Invoked on the event loop by the resource registry's claude-native
        agent-terminal watcher when the pane crosses an activity/idle edge.
        Emitting the same ``session.status`` shape the runner uses for its
        own turns lets the Omnigent server relay it through the normal status
        path (cache + SSE). The watcher already dedupes to edges, so this
        only fires on a real running⇄idle transition.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param status: New working status, ``"running"`` or ``"idle"``.
        """
        _publish_event(
            session_id,
            {"type": "session.status", "status": status},
        )

    resource_registry.set_session_status_publisher(_publish_session_status)

    def _format_terminal_command_for_failure(event: TerminalExitEvent) -> str:
        """Format a launch command without exposing possibly secret argv."""
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
        """Build the sub-agent failure text for a required terminal exit."""
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
        """Release the harness subprocess after its required terminal exited.

        Pure subprocess cleanup — publishes no ``failed`` lifecycle events, so
        it is safe on both the crash and the clean-shutdown paths.
        """
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
        """Publish terminal-exit lifecycle effects from the resource registry."""
        _publish_event(
            event.session_id,
            {
                "type": "session.resource.deleted",
                "resource_id": event.terminal_id,
                "resource_type": "terminal",
                "session_id": event.session_id,
            },
        )
        if event.lifecycle != TerminalLifecycle.REQUIRED:
            return

        # qwen-native / antigravity-native: the user drives the TUI directly, so
        # quitting it (Ctrl+C / /quit) is a normal end-of-session, not a crash.
        # The ``session_was_idle`` guard below is meant to catch a clean exit,
        # but the exit-classification memo is never flipped to ``idle`` for these
        # harnesses: qwen's "powering down" redraw on quit trips the PTY-activity
        # watcher and flips the status to ``running`` in the instant before the
        # process exits, and antigravity-native is deliberately excluded from the
        # PTY ``emit_status`` role set (the RPC reader owns working-status, not
        # PTY activity), so its memo stays ``running``. Either way the quit is
        # misclassified as a crash and the scary ``required_terminal_exited`` card
        # renders. Treat these terminals' exit as a clean shutdown: genuine *boot*
        # failures never reach here (they surface via the respective
        # ``_auto_create_*_terminal`` error handler →
        # ``_publish_native_terminal_start_error``), so a qwen/antigravity
        # required-terminal exit is always post-boot, i.e. user-initiated.
        if event.terminal_name in ("qwen", "antigravity") and event.session_key == "main":
            # Publish a final ``idle`` to clear the web "Working…" spinner: the
            # powering-down redraw may have left the PTY watcher's last edge on
            # ``running``, and the watcher is gone once the pane dies, so without
            # this the session spins forever. Then release the harness (no
            # ``failed`` card — the user quit).
            _publish_event(event.session_id, {"type": "session.status", "status": "idle"})
            _release_required_terminal_session(event.session_id)
            return

        # Exit while idle = the turn already finished and the pane shut down
        # cleanly, so don't flip the chat to ``failed`` (the spurious-"failed"
        # bug). Still release the harness; liveness surfaces the offline runner.
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

    # The runner owns a filesystem registry when it has a local workspace
    # (the CLI workspace path). In practice runner_workspace is always set
    # for the real runner — the None branch exists only to keep the
    # signature flexible for tests and embedded use, but production code
    # never passes None here.
    # The registry is exposed on app.state so tests can seed it.
    from omnigent.runtime.filesystem_registry import (
        FilesystemRegistry,
        create_filesystem_registry,
    )

    if runner_workspace is not None:
        filesystem_registry = create_filesystem_registry(watch_path=runner_workspace)
    else:
        filesystem_registry = None
    app.state.filesystem_registry = filesystem_registry

    # Per-session filesystem registries for sessions whose workspace
    # differs from the runner's global workspace (e.g. git worktree
    # sessions). Keyed by session_id. The global filesystem_registry
    # is used when the session workspace matches runner_workspace.

    _session_fs_registries: dict[str, FilesystemRegistry] = {}

    async def _session_snapshot(session_id: str) -> _SessionSnapshot:
        """
        Fetch the session's server snapshot once, shared by all readers.

        Issues a single ``GET /v1/sessions/{id}`` and projects its body
        into a :class:`_SessionSnapshot` (``created_at`` / ``workspace`` /
        ``agent_id``). A per-session lock makes this single-flight: when a
        startup burst of consumers (registration, workspace resolution,
        spec resolution) calls concurrently, the first does the fetch and
        the rest read the cached result instead of issuing their own
        request.

        Only a *complete* snapshot — HTTP 200 with ``agent_id`` already
        bound — is memoized. A transient non-200, or a 200 whose
        ``agent_id`` is still null (the session exists but the agent has
        not bound yet), returns a fallback/partial snapshot without
        caching. This preserves retry-until-bound: spec resolution keeps
        refetching until the binding appears, instead of latching onto a
        stale ``agent_id=None`` and raising forever. Registration and
        workspace are unaffected — they memoize ``created_at`` /
        ``workspace`` in their own projection caches on first read.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The session snapshot. Always returns a value; failure
            is signaled via ``ok=False`` rather than raising, so
            best-effort callers can use the fallback fields directly.
        """
        cached = _session_snapshot_cache.get(session_id)
        if cached is not None:
            return cached
        lock = _session_snapshot_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Re-check under the lock: a concurrent caller may have
            # populated the cache while we waited to acquire it.
            cached = _session_snapshot_cache.get(session_id)
            if cached is not None:
                return cached
            status_code: int | None = None
            created_at: float | None = None
            workspace: str | None = None
            agent_id: str | None = None
            sub_agent_name: str | None = None
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
                    # Sub-agent identity (SessionResponse.sub_agent_name).
                    # Projected here so harness resolution can swap to the
                    # child's sub-spec even after the in-memory
                    # _session_sub_agent_names map is lost (reconnect /
                    # cache eviction) — the bug that respawned a sub-agent's
                    # claude-native harness as the parent's claude-sdk and
                    # tore down its terminal ("Bridge closed").
                    raw_sub_agent = body.get("sub_agent_name")
                    if isinstance(raw_sub_agent, str) and raw_sub_agent:
                        sub_agent_name = raw_sub_agent
            except Exception:  # noqa: BLE001 — best-effort; created_at falls back to wall time
                pass
            snapshot = _SessionSnapshot(
                ok=status_code == 200,
                status_code=status_code,
                created_at=created_at if created_at is not None else time.time(),
                workspace=workspace,
                agent_id=agent_id,
                sub_agent_name=sub_agent_name,
            )
            # Cache only a complete snapshot. A 200 with agent_id still
            # null means the agent has not bound yet; caching it would
            # freeze spec resolution into raising NOT_FOUND forever, since
            # this cache never refreshes on server-side binding.
            # Cache only a complete snapshot. A 200 with agent_id still
            # null means the agent has not bound yet; caching it would
            # freeze spec resolution into raising NOT_FOUND forever, since
            # this cache never refreshes on server-side binding.
            if snapshot.ok and snapshot.agent_id is not None:
                _session_snapshot_cache[session_id] = snapshot
            return snapshot

    async def _session_workspace_value(session_id: str) -> str | None:
        """
        Lazily resolve + cache the session's server-stored workspace path.

        The agent executes in this directory on this runner (the
        claude-native TUI's cwd, the in-process harness workspace, a git
        worktree, ...). The ``POST /v1/sessions`` body omits ``workspace``,
        so the runner asks the server. Reads from the shared
        :func:`_session_snapshot` so it does not issue its own
        ``GET /v1/sessions/{id}``.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The raw workspace string (an absolute path on this
            runner), or ``None`` when the session has no explicit
            workspace or the lookup fails.
        """
        if session_id not in _session_workspace_cache:
            snapshot = await _session_snapshot(session_id)
            _session_workspace_cache[session_id] = snapshot.workspace
        return _session_workspace_cache.get(session_id)

    async def _session_runtime_cwd(session_id: str) -> Path | None:
        """Return the cwd the harness should use for *session_id*.

        The server-stored session workspace wins because it carries
        worktree-specific paths. Fall back to the runner's global workspace
        only when the snapshot has no workspace.
        """
        workspace = await _session_workspace_value(session_id)
        if workspace and workspace.strip():
            return Path(workspace.strip()).expanduser().resolve()
        return runner_workspace.resolve() if runner_workspace is not None else None

    async def _resolve_session_fs_registry(
        session_id: str,
    ) -> FilesystemRegistry | None:
        """Return the filesystem registry for *session_id*.

        For sessions whose server-stored workspace matches the runner's
        global ``runner_workspace`` (the common case), returns the
        shared ``filesystem_registry``.  For sessions with a different
        workspace (e.g. git worktree sessions), creates and caches a
        per-session registry rooted at the session's workspace.

        Lazily fetches the session workspace from the server on first
        call (the ``POST /v1/sessions`` body does not include
        ``workspace``, so the runner must ask the server).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The appropriate :class:`FilesystemRegistry`, or
            ``None`` when no registry can be created.
        """
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
        """
        Translate application errors to structured JSON responses.

        :param request: The incoming request.
        :param exc: The application error.
        :returns: JSON error response with the mapped HTTP status.
        """
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
        """Translate ValueErrors (e.g. from resolve_environment).

        :param request: The incoming request.
        :param exc: The value error.
        :returns: 400 JSON error response.
        """
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
        """Translate ResourceError subclasses to HTTP responses.

        :param request: The incoming request.
        :param exc: The resource error.
        :returns: JSON error response with appropriate status code.
        """
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
        """
        Liveness probe.

        :returns: ``{"status": "ok"}``.
        """
        return {"status": "ok"}

    @app.post("/v1/sessions")
    async def create_session(request: Request) -> JSONResponse:
        """
        Assign a session to this runner.

        The server calls this after creating the conversation in
        the conversation store. The runner eagerly spawns a harness
        subprocess and caches the agent spec so the session is
        ready to accept events immediately.

        Per ``designs/SESSION_REARCHITECTURE.md`` §4 step 3.

        :param request: JSON body with ``session_id`` and
            ``agent_id``.
        :returns: :class:`SessionResponse`-shaped JSON (201) on
            success; 400 for missing fields; 501 in scaffold mode.
        """
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner POST /v1/sessions needs a HarnessProcessManager."),
                },
            )
        body = await request.json()
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        if not session_id or not agent_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": ("'session_id' and 'agent_id' required."),
                },
            )

        # Resolve the server version once so _publish_turn_status can downgrade
        # session.status "waiting"->"running" for servers too old to accept it
        # (< 0.3.0) — they'd otherwise 500 on GET /v1/sessions. Memoized; only
        # the first session-create on this runner pays the cheap GET.
        await _get_server_version(server_client)

        # Resolve the spec once — derive harness config from it and
        # cache it for resource endpoints (filesystem, terminals)
        # that may fire before the first turn dispatches.
        spec = None
        if spec_resolver is not None:
            try:
                spec = await spec_resolver(agent_id, session_id)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        if spec is not None:
            spec_entry = spec
            if isinstance(spec_entry, ResolvedSpec):
                spec = _unwrap_resolved_spec(spec_entry)
            # Swap to sub-agent's own spec so its harness drives the terminal auto-create.
            _sa_name_assign = body.get("sub_agent_name")
            if _sa_name_assign:
                from omnigent.runtime.workflow import _find_spec_by_name

                _sub_spec = _find_spec_by_name(spec, _sa_name_assign)
                if _sub_spec is not None:
                    spec = _sub_spec
                    spec_entry = (
                        ResolvedSpec(spec=spec, workdir=_resolved_spec_workdir(spec_entry))
                        if _resolved_spec_workdir(spec_entry) is not None
                        else spec
                    )
            harness_name = spec.executor.config.get("harness") or spec.executor.type
            harness_name = canonicalize_harness(harness_name) or harness_name

            # ── sys_agent_start policy gate ───────────────────────
            # Evaluate a synthetic ``sys_agent_start`` tool call so
            # policies like ``enforce_sandbox`` can inspect / override
            # sandbox config before the harness subprocess is created.
            #
            # Fires for BOTH top-level and sub-agent starts: the
            # sub-agent spec swap (line ~2665) happens before this
            # gate, so ``spec`` is already the child's spec when a
            # ``sub_agent_name`` is present.
            #
            # Why a synthetic tool instead of AP-server-side
            # enforcement?  ``sys_session_send`` (sub-agent spawn)
            # goes through AP-server policy, but its arguments carry
            # only ``(agent, title)`` — not the sandbox config.
            # Top-level starts have no tool call at all.  This gate
            # fills both gaps by carrying the sandbox dict and
            # evaluating via ``RunnerToolPolicyGate`` (same gate
            # that guards MCP tool calls) — no round-trip needed.
            _start_verdict = await _evaluate_agent_start_gate(spec, harness_name)
            if _start_verdict is not None:
                # ASK is collapsed to DENY: agent start is a
                # pre-spawn gate with no user interaction channel,
                # so we can't park and wait for approval.
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
            if harness_name == "claude-native" and spawn_env is None:
                from omnigent.claude_native_bridge import (
                    build_claude_native_spawn_env,
                )

                bridge_id = await _claude_native_bridge_id_for_session(
                    server_client=server_client,
                    session_id=session_id,
                )
                spawn_env = build_claude_native_spawn_env(session_id, bridge_id=bridge_id)
            if harness_name == "codex-native" and spawn_env is None:
                from omnigent.codex_native_bridge import (
                    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                    build_codex_native_spawn_env,
                )

                labels = await _session_labels_for_runner_spawn(
                    server_client=server_client,
                    session_id=session_id,
                )
                bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
                spawn_env = build_codex_native_spawn_env(session_id, bridge_id=bridge_id)
            if harness_name == "pi-native" and spawn_env is None:
                from omnigent.pi_native_bridge import build_pi_native_spawn_env

                spawn_env = build_pi_native_spawn_env(session_id)
            if harness_name == "opencode-native" and spawn_env is None:
                from omnigent.opencode_native_bridge import (
                    OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
                    build_opencode_native_spawn_env,
                )

                labels = await _session_labels_for_runner_spawn(
                    server_client=server_client,
                    session_id=session_id,
                )
                bridge_id = labels.get(OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY)
                spawn_env = build_opencode_native_spawn_env(session_id, bridge_id=bridge_id)
            if harness_name == "cursor-native" and spawn_env is None:
                from omnigent.cursor_native_bridge import build_cursor_native_spawn_env

                spawn_env = build_cursor_native_spawn_env(session_id)
            if harness_name == "kiro-native" and spawn_env is None:
                from omnigent.kiro_native_bridge import build_kiro_native_spawn_env

                spawn_env = build_kiro_native_spawn_env(session_id)
            if harness_name == "antigravity-native" and spawn_env is None:
                from omnigent.antigravity_native_bridge import (
                    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
                    build_antigravity_native_spawn_env,
                )

                labels = await _session_labels_for_runner_spawn(
                    server_client=server_client,
                    session_id=session_id,
                )
                antigravity_bridge_id = labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY)
                spawn_env = build_antigravity_native_spawn_env(
                    session_id, bridge_id=antigravity_bridge_id
                )
            if harness_name == "goose-native" and spawn_env is None:
                from omnigent.goose_native_bridge import build_goose_native_spawn_env

                spawn_env = build_goose_native_spawn_env(session_id)
            if harness_name == "hermes-native" and spawn_env is None:
                from omnigent.hermes_native_bridge import build_hermes_native_spawn_env

                spawn_env = build_hermes_native_spawn_env(session_id)
            if harness_name == "qwen-native" and spawn_env is None:
                from omnigent.qwen_native_bridge import build_qwen_native_spawn_env

                spawn_env = build_qwen_native_spawn_env(session_id)
            if harness_name == "kimi-native" and spawn_env is None:
                from omnigent.kimi_native_bridge import build_kimi_native_spawn_env

                spawn_env = build_kimi_native_spawn_env(session_id)
            _session_spec_cache[session_id] = spec_entry
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

        _session_start_cache[session_id] = time.time()
        _session_agent_ids[session_id] = agent_id
        # Don't replace a queue ``stream_session`` may have already lazily
        # created: the Omnigent relay's ``GET /stream`` can race ahead of this
        # init, and replacing it orphans the relay on the dead queue so
        # later events never reach the server (see ``stream_session``).
        if session_id not in _session_event_queues:
            _session_event_queues[session_id] = asyncio.Queue()
        # Same guard: a reconnect re-POST must not wipe an already-delivered
        # sub-agent payload (its work entry is latched delivered → never re-sent).
        if session_id not in _session_inboxes:
            _session_inboxes[session_id] = asyncio.Queue()
        if session_id not in _session_async_tasks:
            _session_async_tasks[session_id] = {}
        _sa_name = body.get("sub_agent_name")
        if _sa_name:
            _session_sub_agent_names[session_id] = _sa_name

        # Auto-bootstrap: if this is a claude-native session and no
        # terminal exists yet, create one. This handles the case
        # where a host-spawned runner receives a session assignment
        # without the CLI having created the terminal.
        if harness_name == "claude-native":
            # Serialize the check-and-create: a concurrent POST /v1/sessions
            # (from _on_runner_connect and the message path's relaunch
            # handshake both firing on the same connection) must not both
            # pass the "no terminal yet" test and double-launch. The second
            # caller in then sees the terminal the first created and no-ops.
            _ensure_lock = _claude_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_terminal = (
                    _tr is not None and _tr.get(session_id, "claude", "main") is not None
                )
                # An in-place agent switch BACK into claude-native (ran
                # claude-native, switched to another agent where turns were
                # added, then switched back) leaves the ORIGINAL claude
                # terminal registered — an open terminal tab keeps it alive.
                # Auto-create is skipped while a terminal exists, so the
                # re-synthesis from current AP items never runs and the agent
                # keeps its original on-disk transcript, missing the turns
                # added on the other agent. Confirmed in production: a switched-
                # back session showed external_session_id=None (rebuild never
                # ran) + the carry-history label set, resuming a transcript
                # without the away-agent's turns. When a post-switch rebuild is
                # pending (external_session_id cleared + carry-history stamped),
                # tear the stale terminal down so auto-create re-synthesizes.
                if _has_terminal and await _claude_native_session_wants_rebuild(
                    server_client, session_id
                ):
                    _logger.info(
                        "Claude terminal stale after agent switch; tearing it down to "
                        "rebuild from current items: session=%s",
                        session_id,
                    )
                    # Terminal-only teardown: drop the tmux pane + bridge but
                    # leave the session's primary OSEnv intact (cleanup_session
                    # would close the env mid-session and break the turn).
                    if _tr is not None:
                        await _tr.cleanup_conversation(session_id)
                    _has_terminal = False
                _logger.info(
                    "Claude terminal auto-create decision: session=%s terminal_registry=%s "
                    "has_existing_terminal=%s",
                    session_id,
                    _tr is not None,
                    _has_terminal,
                )
                # A /clear or /fork rotation binds the runner to the new
                # session before transferring the existing terminal onto it.
                # Auto-creating here would make that transfer 409 and loop
                # the rotation, so skip when the bridge's
                # active session still owns the terminal being transferred in.
                _terminal_inbound = False
                if not _has_terminal:
                    _terminal_inbound = await _claude_native_terminal_arrives_via_transfer(
                        server_client=server_client,
                        session_id=session_id,
                        resource_registry=resource_registry,
                    )
                    _logger.info(
                        "Claude terminal transfer-inbound check: session=%s terminal_inbound=%s",
                        session_id,
                        _terminal_inbound,
                    )
                if not _has_terminal and not _terminal_inbound:
                    # Resolve the session's agent spec so a bundle that ships a
                    # ``skills/`` directory is exposed to Claude Code via
                    # ``--plugin-dir`` (the CLI mirror of the SDK plugin
                    # wiring). Best-effort: a resolver error (HTTP failure,
                    # not-yet-bound agent) just means no bundled skills are
                    # wired — Claude still launches with its host config.
                    _native_bundle_dir: Path | None = None
                    _native_agent_name: str | None = None
                    _native_skills_filter: str | list[str] = "all"
                    try:
                        _native_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        _native_spec = None
                        _logger.info(
                            "Claude terminal spec resolution failed; continuing without "
                            "bundle skills: session=%s",
                            session_id,
                        )
                    if _native_spec is not None:
                        _native_entry = _session_spec_cache.get(session_id)
                        _native_bundle_dir = (
                            _resolved_spec_workdir(_native_entry)
                            if _native_entry is not None
                            else None
                        )
                        _native_agent_name = getattr(_native_spec, "name", None)
                        _native_skills_filter = getattr(_native_spec, "skills_filter", "all")
                    # Auto-inject orchestrator skills (build-omnigent)
                    # into the bundle so Claude discovers them via
                    # --plugin-dir — mirrors _inject_orchestrator_skills
                    # in the load_skill dispatch path.
                    # When no bundle dir exists (single-YAML agents like
                    # claude-native-ui), create a synthetic bundle root in
                    # the session's bridge dir so the skill link +
                    # --plugin-dir still fires. Every omnigent agent
                    # should discover the platform skills without needing a
                    # bundled skills/ directory.
                    if _native_bundle_dir is None:
                        _native_bundle_dir = Path(
                            tempfile.mkdtemp(prefix="omnigent-skill-bundle-")
                        )
                    _logger.info(
                        "Claude terminal auto-create inputs resolved: session=%s "
                        "bundle_dir=%s agent_name=%s skills_filter=%s",
                        session_id,
                        _native_bundle_dir,
                        _native_agent_name,
                        _native_skills_filter,
                    )
                    _ensure_orchestrator_skills_in_bundle(_native_bundle_dir, _native_spec)
                    # Surface "terminal starting up" to the web UI before the
                    # (potentially slow) launch, and clear it in finally so a
                    # failure also drops the spinner rather than stranding it.
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_claude_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            bundle_dir=_native_bundle_dir,
                            agent_name=_native_agent_name,
                            agent_spec=_native_spec,
                            skills_filter=_native_skills_filter,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create claude terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Claude",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)
                elif _terminal_inbound:
                    _logger.info(
                        "Skipping claude terminal auto-create for %s; a sibling "
                        "session's terminal will transfer in (rotation target).",
                        session_id,
                    )

        if harness_name == "codex-native":
            # Same concurrency guard as the claude branch: two POST
            # /v1/sessions (connect callback + relaunch handshake) — or a
            # concurrent terminals-endpoint "ensure" — must not both pass
            # the check and double-launch. Reuses the lock the terminals
            # endpoint already keys on so both paths serialize per session.
            _codex_ensure_lock = _codex_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _codex_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_codex_terminal = (
                    _tr is not None and _tr.get(session_id, "codex", "main") is not None
                )
                # Codex-native sessions use runner-owned app-server/TUI/forwarder
                # setup. The CLI now attaches to the resulting tmux terminal only.
                _needs_terminal = await _codex_session_needs_runner_terminal(
                    server_client, session_id
                )
                if not _has_codex_terminal and _needs_terminal:
                    # Resolve the session's bundle so its ``skills/`` are linked
                    # into the native Codex's CODEX_HOME (mirrors claude-native).
                    # Best-effort: a resolver error means no bundled skills.
                    _codex_bundle_dir: Path | None = None
                    _codex_skills_filter: str | list[str] = "all"
                    try:
                        _codex_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        _codex_spec = None
                    if _codex_spec is not None:
                        _codex_entry = _session_spec_cache.get(session_id)
                        _codex_bundle_dir = (
                            _resolved_spec_workdir(_codex_entry)
                            if _codex_entry is not None
                            else None
                        )
                        _codex_skills_filter = getattr(_codex_spec, "skills_filter", "all")
                    # Auto-inject orchestrator skills into the codex
                    # bundle so CODEX_HOME/skills/ picks them up.
                    if _codex_bundle_dir is not None and _codex_spec is not None:
                        _ensure_orchestrator_skills_in_bundle(_codex_bundle_dir, _codex_spec)
                    # Surface "terminal starting up" to the web UI before the
                    # (potentially slow) launch, and clear it in finally so a
                    # failure also drops the spinner rather than stranding it.
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_codex_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            bundle_dir=_codex_bundle_dir,
                            skills_filter=_codex_skills_filter,
                            agent_spec=spec_entry,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create codex terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Codex",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)
                elif not _needs_terminal:
                    _logger.info(
                        "Skipping codex terminal auto-create for %s; session "
                        "snapshot was not available.",
                        session_id,
                    )

        if harness_name == "pi-native":
            _pi_ensure_lock = _pi_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _pi_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_pi_terminal = (
                    _tr is not None and _tr.get(session_id, "pi", "main") is not None
                )
                if not _has_pi_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        # Inherit the session's os_env.sandbox via its agent
                        # spec. A genuine resolution error must propagate to the
                        # outer handler (-> start error), not be swallowed to
                        # agent_spec=None, which silently drops the sandbox
                        # policy and falls back to the platform default (the
                        # failure mode #569 fixed). _resolve_session_agent_spec
                        # returns None legitimately when there is no spec; only
                        # genuine errors raise.
                        _pi_spec = await _resolve_session_agent_spec(session_id)
                        await _auto_create_pi_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            agent_spec=_pi_spec,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create pi terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Pi",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "cursor-native":
            _cursor_ensure_lock = _cursor_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _cursor_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_cursor_terminal = (
                    _tr is not None and _tr.get(session_id, "cursor", "main") is not None
                )
                if not _has_cursor_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        try:
                            _cursor_spec = await _resolve_session_agent_spec(session_id)
                        except OmnigentError:
                            _cursor_spec = None
                        await _auto_create_cursor_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                            agent_spec=_cursor_spec,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create cursor terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Cursor",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "kiro-native":
            _kiro_ensure_lock = _kiro_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _kiro_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_kiro_terminal = (
                    _tr is not None and _tr.get(session_id, "kiro", "main") is not None
                )
                if not _has_kiro_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_kiro_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create kiro terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Kiro",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "antigravity-native":
            # Same concurrency guard as the claude/codex branches: two POST
            # /v1/sessions (connect callback + relaunch handshake) — or a
            # concurrent terminals-endpoint "ensure" — must not both pass the
            # "no terminal yet" test and double-launch. agy is self-hosted, so
            # there is no app-server; the runner just boots agy in a tmux
            # terminal and runs the transcript forwarder server-side.
            _antigravity_ensure_lock = _antigravity_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _antigravity_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_antigravity_terminal = (
                    _tr is not None and _tr.get(session_id, "antigravity", "main") is not None
                )
                # The runner owns the terminal for every antigravity-native
                # session (host-spawned, sub-agent, or CLI top-level). A missing
                # snapshot means the runner cannot confirm the session, so skip.
                _needs_terminal = (
                    await _session_payload_for_host_spawn_check(server_client, session_id)
                ) is not None
                # A /clear rotation binds the runner to the new session before
                # transferring the existing agy terminal onto it. Auto-creating
                # here would cold-start a redundant agy whose own
                # external_session_id then 400s the rotation's PATCH and loops it,
                # so skip when the bridge's active session still owns the terminal
                # being transferred in (mirrors the claude-native guard above).
                _antigravity_inbound = False
                if not _has_antigravity_terminal:
                    _antigravity_inbound = await _antigravity_native_terminal_arrives_via_transfer(
                        server_client=server_client,
                        session_id=session_id,
                        resource_registry=resource_registry,
                    )
                    _logger.info(
                        "Antigravity terminal transfer-inbound check: session=%s "
                        "terminal_inbound=%s",
                        session_id,
                        _antigravity_inbound,
                    )
                if not _has_antigravity_terminal and _needs_terminal and not _antigravity_inbound:
                    # Surface "terminal starting up" to the web UI before the
                    # (potentially slow) launch, and clear it in finally so a
                    # failure also drops the spinner rather than stranding it.
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_antigravity_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create antigravity terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Antigravity",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)
                elif _antigravity_inbound:
                    _logger.info(
                        "Skipping antigravity terminal auto-create for %s; a sibling "
                        "session's terminal will transfer in (rotation target).",
                        session_id,
                    )
                elif not _needs_terminal:
                    _logger.info(
                        "Skipping antigravity terminal auto-create for %s; session "
                        "snapshot was not available.",
                        session_id,
                    )

        if harness_name == "opencode-native":
            # Host/web-UI session-creation path: boot the runner-owned
            # ``opencode serve`` + SSE forwarder + ``opencode attach`` terminal
            # so the web UI has a terminal+chat view to embed — the native-server
            # sibling of the codex-native branch above. (The on-demand
            # ``ensure_native_terminal`` message path also creates it; the
            # per-session lock makes the two idempotent.)
            _opencode_ensure_lock = _opencode_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _opencode_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_opencode_terminal = (
                    _tr is not None and _tr.get(session_id, "opencode", "main") is not None
                )
                if not _has_opencode_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        try:
                            _opencode_spec = await _resolve_session_agent_spec(session_id)
                        except OmnigentError:
                            _opencode_spec = None
                        await _auto_create_opencode_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            agent_spec=_opencode_spec,
                            server_client=server_client,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create opencode terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "OpenCode",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "goose-native":
            _goose_ensure_lock = _goose_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _goose_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_goose_terminal = (
                    _tr is not None and _tr.get(session_id, "goose", "main") is not None
                )
                if not _has_goose_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_goose_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create goose terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Goose",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "hermes-native":
            _hermes_ensure_lock = _hermes_terminal_ensure_locks.setdefault(
                session_id, asyncio.Lock()
            )
            async with _hermes_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_hermes_terminal = (
                    _tr is not None and _tr.get(session_id, "hermes", "main") is not None
                )
                if not _has_hermes_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_hermes_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create hermes terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Hermes",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "qwen-native":
            _qwen_ensure_lock = _qwen_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _qwen_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_qwen_terminal = (
                    _tr is not None and _tr.get(session_id, "qwen", "main") is not None
                )
                if not _has_qwen_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        await _auto_create_qwen_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create qwen terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "qwen",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        if harness_name == "kimi-native":
            _kimi_ensure_lock = _kimi_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _kimi_ensure_lock:
                _tr = resource_registry.terminal_registry
                _has_kimi_terminal = (
                    _tr is not None and _tr.get(session_id, "kimi", "main") is not None
                )
                if not _has_kimi_terminal:
                    _publish_terminal_pending(_publish_event, session_id, True)
                    try:
                        try:
                            _kimi_spec = await _resolve_session_agent_spec(session_id)
                        except OmnigentError:
                            _kimi_spec = None
                        await _auto_create_kimi_terminal(
                            session_id,
                            resource_registry,
                            _publish_event,
                            server_client=server_client,
                            ensure_comment_relay=_ensure_comment_relay_started,
                            agent_spec=_kimi_spec,
                        )
                    except Exception as exc:
                        _logger.exception(
                            "Failed to auto-create kimi terminal for %s",
                            session_id,
                        )
                        _publish_native_terminal_start_error(
                            _publish_event,
                            session_id,
                            "Kimi",
                            exc,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        # Auto-bootstrap the Omnigent REPL terminal for non-native
        # (SDK-harness) top-level sessions: host the framework's own TUI
        # (``omnigent attach``) in a tmux pane so the web UI can embed it
        # — the SDK mirror of the claude-/codex-native terminals above.
        # Sub-agent sessions are skipped (their I/O surfaces through the
        # parent's transcript), as are the spec-less test scaffold and
        # runners wired without a terminal registry (nothing to host on).
        if (
            spec is not None
            and not is_native_harness(harness_name)
            and not _sa_name
            and resource_registry.terminal_registry is not None
        ):
            # Same double-launch hazard as the native branches: serialize
            # the check-and-create per session.
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
                        # Unlike the native branches, the REPL terminal is a
                        # secondary view — chat works without it — so a
                        # launch failure must not fail the session (no
                        # ``session.status: failed`` publication).
                        _logger.exception(
                            "Failed to auto-create omnigent REPL terminal for %s",
                            session_id,
                        )
                    finally:
                        _publish_terminal_pending(_publish_event, session_id, False)

        # Crash recovery (Step 8.5 Scenario A): if the session
        # has existing history, check whether the last item
        # indicates an incomplete turn that needs restarting.
        history = await _load_history_as_input(session_id)
        # Native terminal transcripts are mirrored from the underlying
        # runtime. A trailing user item can be a real failed/errored native
        # turn with no assistant item, not an unanswered Omnigent task to replay.
        if history and not is_native_harness(harness_name):
            _session_histories[session_id] = history
            last = history[-1]
            last_type = last.get("type")
            last_role = last.get("role")
            needs_turn = (
                (last_type == "message" and last_role == "user")
                or last_type == "function_call"
                or last_type == "function_call_output"
            )
            if needs_turn and session_id not in _active_turns:
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
            },
        )

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream_session(session_id: str) -> StreamingResponse:
        """
        Subscribe to live SSE events for a session.

        Reads from the per-session event queue. Events
        accumulate in the queue while no subscriber is
        connected, so tunnel drops don't lose events — the
        relay drains on reconnect. Events are removed from
        the queue after reading.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Long-lived ``text/event-stream`` response.
        """

        async def _event_generator() -> AsyncIterator[bytes]:
            """
            Yield SSE frames from the per-session event queue.

            Blocks on ``queue.get()`` with a heartbeat timeout so
            between-turn idle periods emit keepalive bytes. Without
            these, an intermediate proxy can drop the long-lived
            HTTP connection, leaving the Omnigent relay on a half-open
            socket that blocks forever. Lazily creates the queue if
            the relay connects before session creation (the REPL's
            SSE subscription races the session POST).

            :returns: Async iterator of UTF-8 encoded SSE frames.
            """
            queue = _session_event_queues.get(session_id)
            if queue is None:
                queue = asyncio.Queue()
                _session_event_queues[session_id] = queue
            heartbeat_frame = b'data: {"type": "session.heartbeat"}\n\n'
            # Immediate ready ack: Omnigent waits for this frame before
            # forwarding no-replay user input, proving its relay has
            # reached the runner stream and created/attached to the
            # per-session queue. Later heartbeats are idle keepalives.
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
        """
        Return the runner-local status of a session.

        The server calls this to derive session status. Fields
        not owned by the runner (``title``, ``labels``, etc.)
        return their defaults; the server overlays its own values.

        Per ``designs/SESSION_REARCHITECTURE.md`` §4 step 3.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: :class:`SessionResponse`-shaped JSON; 404 if
            no harness subprocess is registered.
        """
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
        """
        End a session on this runner.

        Cancels any active turn, closes SSE subscriptions, releases
        the harness subprocess, and cleans up runner-local caches
        and resources (environments, terminals).

        Per ``designs/SESSION_REARCHITECTURE.md`` §4 step 3.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Deletion confirmation JSON.
        """
        # Cancel active turn before releasing harness.
        turn_task = _active_turns.pop(session_id, None)
        if turn_task is not None and isinstance(turn_task, asyncio.Task):
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn_task
        _session_message_buffers.pop(session_id, None)
        _live_response_id.pop(session_id, None)
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
        # Stop any TUI→web transcript forwarder (cursor-/goose-native) for this
        # session: on teardown the embedded terminal is gone, so a still-running
        # supervisor would poll a dead store and POST to a deleted session
        # forever. Idempotent when no forwarder was registered.
        await _cancel_auto_forwarder_task(session_id)

        if process_manager is not None:
            await process_manager.forward_cancel(session_id)

        # Signal end-of-stream to GET /stream subscriber.
        queue = _session_event_queues.get(session_id)
        if queue is not None:
            queue.put_nowait(None)

        await resource_registry.cleanup_session(session_id)

        if process_manager is not None:
            await process_manager.release(session_id)

        _session_spec_cache.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_start_cache.pop(session_id, None)
        _session_workspace_cache.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        _session_snapshot_locks.pop(session_id, None)
        _session_spec_locks.pop(session_id, None)
        _session_fs_registries.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        if _relay := _session_comment_relays.pop(session_id, None):
            _relay.close()
        _session_histories.pop(session_id, None)
        _last_server_item_id.pop(session_id, None)
        _session_event_queues.pop(session_id, None)
        _session_inboxes.pop(session_id, None)
        _subagent_wake_pending.discard(session_id)
        # Without this, a deleted child's name lingers, so a late terminal
        # status for it reads is_runner_known_subagent=True with no work
        # entry → a spurious 503 subagent_delivery_not_confirmed (AP retries)
        # plus an unbounded leak across deleted sessions.
        _session_sub_agent_names.pop(session_id, None)
        # Drop the child→parent fan-out mapping if this session was a
        # spawned sub-agent child (no-op otherwise).
        unregister_child_session(session_id)
        unregister_subagent_work_for_session(session_id)
        if filesystem_registry is not None:
            filesystem_registry.unregister_conversation(session_id)
        for _task, evt in _session_async_tasks.pop(session_id, {}).values():
            evt.set()
        for _tmr in _session_timers.pop(session_id, {}).values():
            _tmr.cancel()
        _version_cache.pop(session_id, None)
        # Clean up any response_id → conversation_id mappings
        # for this session.
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

    async def _load_history_as_input(
        session_id: str,
        drop_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Load conversation history from the server and convert to
        the harness input format.

        Fetches items via ``GET /v1/sessions/{id}/items`` and maps
        each to the Responses-API input shape that the harness
        adapter's ``_translate_input_to_messages`` understands.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param drop_item_id: When set, the raw store item with this
            id is excluded before conversion, e.g.
            ``"item_abc123"``. Used by the cold-cache rehydration
            path to drop this turn's just-persisted (pre-resolution)
            input so the caller can append its own resolved copy
            without duplication. ``None`` keeps every item.
        :returns: List of input items in chronological order, or
            empty list if the fetch fails. Each item is a dict
            like ``{"type": "message", "role": "user",
            "content": [...]}``.
        """
        # Paginate through all items using cursor-based `after`.
        all_items: list[dict[str, Any]] = []
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
            # Track last item ID for incremental catch-up.
            last_id = page_items[-1].get("id")
            if last_id:
                _last_server_item_id[session_id] = last_id
            if not page.get("has_more", False):
                break
            after_cursor = last_id

        if drop_item_id is not None:
            all_items = [it for it in all_items if it.get("id") != drop_item_id]

        return _convert_raw_items_to_input(all_items)

    def _convert_raw_items_to_input(
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Convert raw server items to harness input format.

        Scans for the latest ``compaction`` item and discards
        everything before it — those items are already summarized.
        The compaction item is expanded into a synthetic
        user+assistant pair carrying the summary text.

        :param items: Raw items from GET /v1/sessions/{id}/items.
        :returns: List of harness-input-shaped dicts.
        """
        compaction_idx: int | None = None
        for i, item in enumerate(items):
            if item.get("type") == "compaction":
                compaction_idx = i

        result: list[dict[str, Any]] = []
        if compaction_idx is not None:
            c = items[compaction_idx]
            # Prefer compacted_messages when available — they carry the
            # full compacted state (e.g. OpenAI's opaque compaction
            # tokens) that the harness can replay directly. Fall back
            # to a synthetic summary pair for older compaction items or
            # harnesses that don't provide compacted messages.
            _compacted = c.get("compacted_messages")
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
            if item_type not in ("message", "function_call", "function_call_output"):
                _skipped_types.append(str(item_type))
            if item_type == "message":
                result.append(
                    {
                        "type": "message",
                        "role": item.get("role", "user"),
                        "content": item.get("content", []),
                    }
                )
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
        """
        Extract the text of the last assistant message from
        in-memory history.

        Used by sub-agent dispatch to collect the child turn's
        output when the Future is resolved.

        :param session_id: Session/conversation ID whose history
            to search, e.g. ``"conv_child123"``.
        :returns: The assistant message text, or an empty string
            if no assistant message is found.
        """
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
        event: dict[str, Any],
    ) -> None:
        """Persist a harness-originated compaction to the server.

        Called when the proxy stream observes a
        ``response.compaction.completed`` event carrying a ``summary``
        field — indicating the harness compacted its own context.
        The SSE events (in_progress / completed) are already emitted
        by the executor adapter and flow to clients directly; this
        function only persists the compaction item and updates the
        runner's in-memory history mirror.

        :param conv: Session/conversation identifier.
        :param event: The ``response.compaction.completed`` SSE
            payload with ``summary``, ``total_tokens``, and optional
            ``summary_model`` keys.
        """
        summary: str = event.get("summary", "")
        token_count: int = event.get("total_tokens") or 0
        model: str | None = event.get("summary_model")
        last_item_id = _last_server_item_id.get(conv)

        if not last_item_id:
            _logger.warning(
                "Skipping harness compaction persist for %s: no "
                "server-side last_item_id available",
                conv,
            )
            return

        compacted_messages = event.get("compacted_messages")
        compaction_event: dict[str, Any] = {
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

        # Replace the in-memory history. When the harness provided
        # its compacted messages, use those directly — they carry the
        # full compacted state (including opaque compaction tokens for
        # OpenAI). Otherwise fall back to a synthetic summary pair.
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
    # Tells the model the prior request was abandoned, not just that the
    # assistant's reply was cut off — otherwise the canceled instruction
    # survives in history and the next turn acts on it (issue: cancel-leak).
    _CANCELLATION_MARKER_TEXT = (
        "[System: interrupted]\n"
        "The user interrupted and abandoned their previous request (the user "
        "message immediately before this one). Do not resume or act on that "
        "interrupted request unless the user asks for it again; treat the next "
        "user message as the current instruction. The preceding assistant "
        "message may be incomplete."
    )

    def _append_cancellation_items(conv_id: str) -> None:
        """Insert synthetic items for an interrupted turn.

        1. Synthetic ``function_call_output`` for every dangling
           ``function_call`` (call emitted but no matching output).
        2. A cancellation marker ``message`` so the LLM knows
           the prior output was incomplete.

        Items are appended to the runner's in-memory
        ``_session_histories`` and POSTed to the server for
        database persistence.

        .. todo::
            Phase 2 — flush *partial* content on interrupt:
            • Join accumulated ``_text_acc`` deltas and persist
              as an assistant message with
              ``status="incomplete"`` on ``ConversationItem``.
            • Persist in-flight function_call items with
              ``status="incomplete"``.
            • Persist partial tool outputs with
              ``status="incomplete"``.
        """
        history = _session_histories.get(conv_id, [])

        call_ids_with_output: set[str] = set()
        dangling_calls: list[dict[str, Any]] = []
        for item in history:
            itype = item.get("type")
            if itype == "function_call":
                cid = item.get("call_id")
                if cid:
                    dangling_calls.append(item)
            elif itype == "function_call_output":
                cid = item.get("call_id")
                if cid:
                    call_ids_with_output.add(cid)

        items_to_persist: list[dict[str, Any]] = []
        synthetic_items: list[dict[str, Any]] = []
        cached_spec_entry = _session_spec_cache.get(conv_id)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        agent_name = cached_spec.name if cached_spec else "unknown"
        for fc in dangling_calls:
            call_id = fc["call_id"]
            if call_id not in call_ids_with_output:
                fc_for_db = dict(fc)
                fc_for_db.setdefault("agent", agent_name)
                items_to_persist.append(fc_for_db)
                synthetic_output = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _CANCELLATION_TOOL_OUTPUT,
                }
                synthetic_items.append(synthetic_output)
                items_to_persist.append(synthetic_output)

        marker = {
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

        # Only the synthetic items go into in-memory history — the
        # dangling function_calls are already there from proxy_stream.
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
        items: list[dict[str, Any]],
    ) -> None:
        """POST synthetic cancellation items to the server.

        Uses the ``external_conversation_item`` event type so the
        server persists without forwarding back to the runner.
        """
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

    async def _recover_sub_agent_name(conv_id: str) -> str | None:
        """Resolve a session's sub-agent name, recovering it if lost.

        The in-memory ``_session_sub_agent_names`` map is populated only on
        ``POST /v1/sessions`` and wiped on a runner restart / cleared on
        session delete. A continuation turn that reaches a harness-resolution
        path after a tunnel reconnect therefore finds it empty and resolves
        the PARENT harness for a child session — respawning the harness and
        tearing down the child's native terminal ("Bridge closed").

        This recovers the identity from the authoritative server snapshot
        (``GET /v1/sessions/{id}`` -> ``sub_agent_name``) and backfills the
        in-memory map so subsequent reads are cheap. Best-effort: a failed
        lookup returns ``None`` (a top-level session, or the snapshot is
        unavailable), preserving the prior behavior.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :returns: The sub-agent name, or ``None`` for a top-level session
            (or when it cannot be resolved).
        """
        cached = _session_sub_agent_names.get(conv_id)
        if cached:
            return cached
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        name = snapshot.sub_agent_name if snapshot is not None else None
        if name:
            _session_sub_agent_names[conv_id] = name
        return name

    def _session_harness_name(conv_id: str) -> str | None:
        """
        Resolve the canonical harness name for a session, if known.

        Reads ``_session_spec_cache`` (populated at session start by
        ``POST /v1/sessions/{conv}/start`` and the spawn dispatch path)
        and re-derives the harness name via the same precedence used
        at spawn time: ``executor.config.harness`` first, then
        ``executor.type``, then canonicalized via
        :func:`canonicalize_harness`.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The canonical harness name (e.g. ``"claude-native"``)
            or ``None`` if no spec is cached for this session.
        """
        spec = _session_spec_cache.get(conv_id)
        if spec is None:
            return None
        h = spec.executor.config.get("harness") or spec.executor.type
        return canonicalize_harness(h) or h

    def _publish_turn_status(
        conv_id: str,
        status: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        Publish a turn-lifecycle ``session.status`` edge unless a native
        terminal observer already owns that edge.

        Terminal-backed sessions do not all have the same safe edge source.
        For claude-native, pi-native, and cursor-native, the PTY-activity
        watcher owns ``running`` and ``idle`` because a runner turn only types
        into the agent's own pane and ``run_turn`` returns the instant the
        message is injected — the model turn then runs entirely in the TUI.
        Publishing the turn-lifecycle ``idle`` here would race ahead of (and
        clobber) the watcher's ``running``, dropping the web "Working…" spinner
        the moment the message is sent. For codex-native AND antigravity-native,
        the runner may publish ``running`` when it accepts
        a web turn for dispatch, but the native observer owns
        ``idle`` because the runner's injection task returns as soon as the agent
        accepts the message, while the user-visible model turn may still be
        active — for codex-native the Codex app-server forwarder owns ``idle``;
        for antigravity-native the RPC read driver owns it (the executor's
        ``SendUserCascadeMessage`` returns as soon as agy accepts the turn, so the
        runner's ``idle`` would fire ~2s before agy's reasoning/output streams,
        prematurely completing the response — the double-idle the live e2e found).

        ``failed`` always publishes: a turn-setup error is not observable
        from terminal activity and must surface regardless of harness.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param status: The status edge, ``"running"`` / ``"idle"`` /
            ``"failed"``.
        :param error: Failure detail dict for a ``"failed"`` edge, carried
            through so a SETUP-phase failure surfaces a real message;
            ``None`` for ``running`` / ``idle``.
        :returns: None.
        """
        # Backwards-compat: servers older than 0.3.0 can't serialize "waiting"
        # and 500 on GET /v1/sessions. Downgrade it to "running" unless the
        # resolved server version supports it; an unknown version (unprobed or
        # probe failure) downgrades too (safe default). See _get_server_version.
        if status == "waiting" and not (
            _server_version is not None and _version_supports_waiting_status(_server_version)
        ):
            status = "running"
        # An unresolved spec (``_session_harness_name`` → ``None``) means the
        # session hasn't resolved a terminal-backed harness yet, so no native
        # observer is known and the turn lifecycle is still the only status
        # source — fall through and publish. Suppress only once we positively
        # know the harness/edge is terminal-owned.
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
        event: dict[str, Any] = {"type": "session.status", "status": status}
        if error is not None:
            event["error"] = error
        _publish_event(conv_id, event)

    def _is_native_harness(conv_id: str) -> bool:
        """
        Whether this session types messages directly into a terminal.

        Native harnesses (``claude-native`` / ``codex-native`` /
        ``pi-native``) have
        *instant* turns — ``run_turn`` returns as soon as the message is
        typed into the pane — and type only the latest user message per
        turn. The runner's mid-turn forward + collapse-batch continuation,
        designed for LLM harnesses whose turns have real duration, drop
        and duplicate messages for them (the forward's injection races the
        instant turn's teardown; the collapse types only the last buffered
        message). Native sessions therefore take the no-forward,
        one-message-at-a-time delivery path. See
        ``designs/RUNNER_MESSAGE_INGEST.md`` Part C.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` for native terminal sessions.
        """
        return is_native_harness(_session_harness_name(conv_id))

    def _wake_parent_after_native_interrupt(conv_id: str) -> None:
        """Mark an interrupted native sub-agent cancelled and wake its parent.

        Shared by the claude/codex native interrupt handlers; a no-op when
        *conv_id* is a top-level session (no one's tracked sub-agent).

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent interrupted]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Native interrupt: sub-agent delivery not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )

    async def _handle_claude_native_interrupt(conv_id: str) -> Response:
        """
        Stop a claude-native session by injecting Escape into tmux.

        Claude-native sessions have no in-flight harness turn for the
        scaffold's ``InterruptEvent`` path to cancel — the harness's
        ``run_turn`` returns as soon as the user prompt is pasted
        into the tmux pane, and the actual long-running work (Claude
        generating a response) happens inside the ``claude`` binary
        in the pane. The only way to stop it is sending a key to the
        terminal.

        Sending the Escape is the whole job — no synthetic
        ``[System: interrupted]`` transcript marker is persisted. That
        marker exists for in-process LLM harnesses, where the runner's
        ``_session_histories`` *is* the model's next-turn context, so a
        cut-off turn must be repaired (dangling ``function_call`` items
        get synthetic outputs) and annotated. None of that applies to
        Claude-native: Claude owns its own session, the runner only types
        the latest user message into the pane, and Claude records the
        interrupt in its own transcript (mirrored by the forwarder). The
        web UI's interrupt decoration comes from the harness-agnostic
        ``session.interrupted`` event, not this marker. Persisting it here
        only forged a ``role:"user"`` bubble the user never sent into the
        AP-side mirror, diverging it from Claude's real transcript.

        Status is intentionally NOT synthesized here. The terminal's PTY
        activity watcher is the single source of truth: it emits
        ``session.status: idle`` once the pane quiesces after the Escape,
        and keeps the session ``running`` if the interrupt didn't actually
        stop Claude. Emitting ``idle`` here too (as this used to, back when
        the hook-based status couldn't observe idle-on-Escape) would
        bypass — and desync — the watcher's running/idle dedupe, and could
        strand the UI on ``idle`` while Claude kept working.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: 204 on success. 503 if the tmux target is not yet
            advertised (caller treats this as a best-effort failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_interrupt,
        )

        # Resolve the bridge id from the session's labels so
        # ``--resume`` sessions (where bridge_id != conversation_id)
        # land in the right tmux pane. Falls back to ``conv_id`` for
        # legacy single-session bridges; see
        # :func:`_claude_native_bridge_id_for_session`.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: UI stop must feel snappy; a missing
            # tmux.json means there's nothing to interrupt anyway.
            await asyncio.to_thread(inject_interrupt, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native interrupt"),
                },
            )
        # No ``_append_cancellation_items``: the synthetic marker is for
        # in-process LLM harnesses only (see docstring). The /events dispatch
        # already keeps native out of ``_interrupted_sessions``.
        # NB: no synthesized ``session.status: idle`` here — the PTY watcher
        # emits idle when the pane quiesces after the Escape (and re-asserts
        # running if the interrupt didn't take). See the docstring.
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _codex_native_bridge_state_for_session(
        conv_id: str,
        *,
        action: str,
        missing_state_log_level: int = logging.WARNING,
    ) -> Any | None:
        """
        Read the recorded Codex app-server bridge state for a session.

        Codex-native controls (interrupt, model, effort) target the
        app-server socket recorded by the forwarder. ``--resume`` sessions
        can have a bridge id distinct from the Omnigent session id, so the
        lookup first resolves ``omnigent.codex_native.bridge_id`` from
        session labels and falls back to ``conv_id`` for legacy states.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param action: Human-readable control action for logs, e.g.
            ``"interrupt"``.
        :param missing_state_log_level: Log level used when bridge state has
            not been written yet, e.g. ``logging.DEBUG`` for readiness probes.
        :returns: Bridge state for this session, or ``None`` when no matching
            state is currently recorded.
        """
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

    async def _handle_codex_native_interrupt(conv_id: str) -> Response:
        """
        Stop a codex-native turn via Codex app-server ``turn/interrupt``.

        Codex's own TUI maps its interrupt key to an app-server request
        carrying the active ``threadId`` and ``turnId``. The web/runner path
        should use that protocol directly instead of guessing at terminal
        keybindings: the Codex app-server validates that the requested turn is
        active and replies after the turn aborts.

        No interrupted marker is synthesized here. Codex records the interrupt
        only as a turn-status edge in its own transcript — not as a message — so
        injecting a ``[System: interrupted]`` bubble into the Omnigent mirror would
        diverge the web UI from Codex's actual session (and never survive a
        ``--resume``). Interruption surfaces via the harness-agnostic
        ``session.interrupted`` event; a durable, faithful indicator is a
        follow-up (persist turn status, render from that — no fabricated
        message). claude-native is unaffected: its badge mirrors Claude Code's
        *own* ``[Request interrupted by user]`` record, which is real.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 204 when no active turn is recorded or the interrupt lands;
            503 when Codex rejects the active-turn interrupt.
        """
        from omnigent.codex_native_app_server import client_for_transport

        state = await _codex_native_bridge_state_for_session(conv_id, action="interrupt")
        if state is None:
            return Response(status_code=204)
        if state.active_turn_id is None:
            _logger.info("Codex-native interrupt skipped for %s: no active turn.", conv_id)
            return Response(status_code=204)

        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            await codex_client.request(
                "turn/interrupt",
                {
                    "threadId": state.thread_id,
                    "turnId": state.active_turn_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface active-turn interrupt failures to caller.
            _logger.warning(
                "Codex-native turn/interrupt failed for session=%s thread=%s turn=%s",
                conv_id,
                state.thread_id,
                state.active_turn_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native interrupt"),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_codex_native_settings_update(
        conv_id: str,
        settings: dict[str, Any],
    ) -> Response:
        """
        Queue Codex app-server next-turn settings for a loaded thread.

        Codex app-server exposes ``thread/settings/update`` for partial
        updates to a loaded thread's future-turn settings. This is the
        codex-native counterpart to the Claude-native slash-command
        injection path: model/effort changes persisted by Omnigent are
        forwarded into Codex's own control plane instead of being typed into
        the terminal.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param settings: Flat Codex app-server settings fields to update,
            excluding ``threadId``; e.g. ``{"model": "gpt-5.4"}``.
        :returns: 204 when no bridge is loaded or the update lands; 503 when
            Codex rejects the settings update.
        """
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
        """
        Resolve the current Codex model and effort for a settings update.

        ``CollaborationMode.settings.model`` is required by Codex app-server, so
        a Plan/Default-mode update cannot send only the mode kind. Prefer the
        server snapshot, which includes TUI-observed ``model_override`` /
        ``reasoning_effort`` mirrors, and fall back to the cached agent spec.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: ``(model, effort)`` where effort may be ``None``.
        """
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
            model = _codex_native_model_from_spec(_session_spec_cache.get(conv_id))
        return model, effort

    async def _handle_codex_native_plan_mode_change(
        conv_id: str,
        *,
        enabled: bool,
    ) -> Response:
        """
        Queue Codex app-server collaboration-mode settings for a loaded thread.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param enabled: ``True`` enters Plan mode; ``False`` returns to
            Default mode.
        :returns: 204 when the update lands; 503 when no bridge is loaded,
            the current model cannot be resolved, or Codex rejects the update.
        """
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
        return await _handle_codex_native_settings_update(
            conv_id,
            {
                "collaborationMode": {
                    "mode": "plan" if enabled else "default",
                    "settings": {
                        "model": model,
                        "reasoning_effort": effort,
                        "developer_instructions": None,
                    },
                },
            },
        )

    async def _codex_native_model_options(conv_id: str) -> list[dict[str, Any]]:
        """
        Query Codex app-server ``model/list`` for a loaded codex-native session.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: Raw Codex ``model/list`` model objects.
        :raises _CodexNativeModelOptionsNotReady: If Codex has not written
            bridge state for this session yet.
        :raises RuntimeError: If the app-server call fails.
        :raises ValueError: If Codex returns a malformed payload.
        """
        from omnigent.codex_native_app_server import client_for_transport

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
        options: list[dict[str, Any]] = []
        try:
            await codex_client.connect()
            cursor: str | None = None
            while True:
                params: dict[str, Any] = {"includeHidden": False}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await codex_client.request("model/list", params)
                result = response.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Codex model/list result must be an object")
                data = result.get("data")
                if not isinstance(data, list):
                    raise ValueError("Codex model/list data must be a list")
                for raw_model in data:
                    if not isinstance(raw_model, dict):
                        raise ValueError("Codex model/list item must be an object")
                    options.append(raw_model)
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if not isinstance(next_cursor, str) or not next_cursor:
                    raise ValueError("Codex model/list nextCursor must be a string or null")
                cursor = next_cursor
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()
        return options

    async def _handle_pi_native_interrupt(conv_id: str) -> Response:
        """
        Stop a pi-native turn by asking the resident Pi extension to abort.

        Pi-native turns live inside the terminal's Pi process. The runner's
        harness task only queues the user's message into the extension inbox
        and returns, so the generic in-process cancel floor has nothing useful
        to cancel. Queue an explicit interrupt payload instead; the extension
        consumes it in the TUI process and calls the active
        ``ExtensionContext.abort()``.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 204 when the interrupt payload was queued; 503 if the
            bridge inbox could not be written.
        """
        from omnigent.pi_native_bridge import bridge_dir_for_session_id, enqueue_interrupt

        try:
            await asyncio.to_thread(
                enqueue_interrupt,
                bridge_dir_for_session_id(conv_id),
            )
        except OSError as exc:
            _logger.warning(
                "Pi-native interrupt failed for session=%s",
                conv_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "pi_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="pi-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _teardown_session_terminals(conv_id: str) -> None:
        """Close a session's terminal resources and announce their removal.

        Removes each terminal from the registry and publishes
        ``session.resource.deleted`` so clients drop it immediately (the
        server relay persists it, matching ``sys_terminal_close``).
        Without the events the web UI keeps showing a dead terminal whose
        attach fails with "terminal resource not found". Two callers:

        - claude-native stop: runner-side analog of the CLI launcher's
          ``_close_claude_terminal``, for the host-spawned (web-UI-created)
          path which has no CLI wrapper to observe the killed pane.
        - agent-switch ``reset-state``: the switch closes the old agent's
          terminals while the session stays open, so clients must be told.

        Best-effort — a close failure (e.g. the pane is already dead) must
        not fail the caller.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: None.
        """
        from omnigent.entities.session_resources import terminal_resource_id
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = resource_registry.terminal_registry
        if terminal_registry is None:
            return
        # Snapshot (name, key) before closing — close_terminal mutates the
        # registry, so iterating it lazily while closing would skip entries.
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

    async def _handle_claude_native_stop(conv_id: str) -> Response:
        """
        Terminate a claude-native session by killing its tmux session.

        This is the runner-side handler for the Omnigent web UI's "Stop
        session" affordance. Unlike
        :func:`_handle_claude_native_interrupt` (a single ``Escape``
        that cancels the current response but leaves the session
        alive), this kills the tmux session outright, ending the
        ``claude`` process and the pane.

        We do *not* synthesize transcript items the way the interrupt
        handler does: killing the pane causes the wrapper's reconnect
        loop to observe the terminal resource disappear and tear the
        session down through its normal end-of-session path. We do
        publish a ``session.status: idle`` event so the web UI's
        "Working…" spinner clears immediately rather than lingering
        until the wrapper notices the pane is gone — Claude's ``Stop``
        hook never fires on a hard kill.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: 204 on success. 503 if the tmux target is not yet
            advertised (caller treats this as a best-effort failure —
            a missing target means there is no live session to kill).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            kill_session,
        )

        # Resolve the bridge id from the session's labels so
        # ``--resume`` sessions (where bridge_id != conversation_id)
        # land on the right tmux socket. Falls back to ``conv_id`` for
        # legacy single-session bridges; see
        # :func:`_claude_native_bridge_id_for_session`.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: the UI stop must feel snappy; a missing
            # tmux.json means there's nothing left to kill anyway.
            await asyncio.to_thread(kill_session, bridge_dir, timeout_s=1.0)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="claude-native stop"),
                },
            )
        # The pane is dead; on the host-spawned path no CLI wrapper will
        # observe that and tear the terminal resource down, so do it here
        # — otherwise the web UI keeps showing a live terminal for the
        # stopped session.
        await _teardown_session_terminals(conv_id)
        _publish_event(
            conv_id,
            {"type": "session.status", "status": "idle"},
        )
        # Reclaim the work entry deterministically. If this killed session is a
        # sub-agent worker, mark it cancelled now (and auto-wake its parent)
        # rather than waiting on the wrapper's reconnect loop to notice the dead
        # pane — that lag left the parent thinking the worker was still running.
        # A no-op for a top-level session (it is no one's tracked sub-agent).
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Claude-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_cursor_native_interrupt(conv_id: str) -> Response:
        """Cancel the in-flight cursor turn by sending ``Escape`` to its TUI pane.

        cursor-native turns run inside the cursor-agent TUI; the runner harness
        task returns right after the tmux paste, so the in-process cancel floor
        has nothing to cancel. ``Escape`` stops a running cursor turn (verified).

        :param conv_id: Session/conversation identifier.
        :returns: 204 when Escape was sent; 503 if the tmux target is unavailable.
        """
        from omnigent.cursor_native_bridge import bridge_dir_for_session_id, inject_interrupt

        try:
            await asyncio.to_thread(
                inject_interrupt, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_cursor_native_stop(conv_id: str) -> Response:
        """Hard-stop a cursor-native session by killing its tmux session.

        Mirrors :func:`_handle_claude_native_stop`: kill the pane (ends
        cursor-agent), tear the terminal resource down so the web UI stops
        showing a live terminal, publish ``idle`` so the spinner clears, and
        reclaim any sub-agent work entry.

        :param conv_id: Session/conversation identifier.
        :returns: 204 on success; 503 if the tmux target is unavailable.
        """
        from omnigent.cursor_native_bridge import bridge_dir_for_session_id, kill_session

        try:
            await asyncio.to_thread(
                kill_session, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native stop"),
                },
            )
        await _teardown_session_terminals(conv_id)
        # Stop mirroring: the chat store is now frozen, so the forwarder has
        # nothing left to post — cancel it so it isn't left polling a dead pane.
        await _cancel_auto_forwarder_task(conv_id)
        _publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Cursor-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_goose_native_interrupt(conv_id: str) -> Response:
        """Cancel the in-flight goose turn by sending ``Escape`` to its TUI pane.

        goose-native turns run inside the ``goose session`` TUI; the runner
        harness task returns right after the tmux paste, so the in-process cancel
        floor has nothing to cancel. Mirrors the cursor-native interrupt.

        :param conv_id: Session/conversation identifier.
        :returns: 204 when Escape was sent; 503 if the tmux target is unavailable.
        """
        from omnigent.goose_native_bridge import bridge_dir_for_session_id, inject_interrupt

        try:
            await asyncio.to_thread(
                inject_interrupt, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "goose_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="goose-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_kimi_native_interrupt(conv_id: str) -> Response:
        """Cancel the in-flight kimi turn by sending ``Escape`` to its TUI pane.

        kimi-native turns run inside the kimi TUI; the runner harness task
        returns right after the tmux paste, so the in-process cancel floor has
        nothing to cancel. ``Escape`` stops a running kimi turn.

        :param conv_id: Session/conversation identifier.
        :returns: 204 when Escape was sent; 503 if the tmux target is unavailable.
        """
        from omnigent.kimi_native_bridge import bridge_dir_for_session_id, inject_interrupt

        try:
            await asyncio.to_thread(
                inject_interrupt, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "kimi_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="kimi-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_goose_native_stop(conv_id: str) -> Response:
        """Hard-stop a goose-native session by killing its tmux session.

        Mirrors :func:`_handle_cursor_native_stop`: kill the pane (ends
        ``goose``), tear the terminal resource down, cancel the forwarder,
        publish ``idle``, and reclaim any sub-agent work entry.

        :param conv_id: Session/conversation identifier.
        :returns: 204 on success; 503 if the tmux target is unavailable.
        """
        from omnigent.goose_native_bridge import bridge_dir_for_session_id, kill_session

        try:
            await asyncio.to_thread(
                kill_session, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "goose_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="goose-native stop"),
                },
            )
        await _teardown_session_terminals(conv_id)
        await _cancel_auto_forwarder_task(conv_id)
        _publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Goose-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_kimi_native_stop(conv_id: str) -> Response:
        """Hard-stop a kimi-native session by killing its tmux session.

        Mirrors :func:`_handle_cursor_native_stop`: kill the pane (ends kimi),
        cancel the transcript forwarder (the chat store is now frozen — nothing
        left to mirror), tear the terminal resource down so the web UI stops
        showing a live terminal, publish ``idle`` so the spinner clears, and
        reclaim any sub-agent work entry.

        :param conv_id: Session/conversation identifier.
        :returns: 204 on success; 503 if the tmux target is unavailable.
        """
        from omnigent.kimi_native_bridge import bridge_dir_for_session_id, kill_session

        try:
            await asyncio.to_thread(
                kill_session, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "kimi_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="kimi-native stop"),
                },
            )
        await _teardown_session_terminals(conv_id)
        # Stop mirroring: the wire log is now frozen, so cancel the forwarder so
        # it isn't left polling a dead session.
        await _cancel_auto_forwarder_task(conv_id)
        _publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Kimi-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_hermes_native_interrupt(conv_id: str) -> Response:
        """Cancel the in-flight hermes turn by sending ``Escape`` to its TUI pane.

        hermes-native turns run inside the ``hermes`` TUI; the runner harness task
        returns right after the tmux paste, so the in-process cancel floor has
        nothing to cancel. Mirrors the goose-native interrupt.

        :param conv_id: Session/conversation identifier.
        :returns: 204 when Escape was sent; 503 if the tmux target is unavailable.
        """
        from omnigent.hermes_native_bridge import bridge_dir_for_session_id, inject_interrupt

        try:
            await asyncio.to_thread(
                inject_interrupt, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "hermes_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="hermes-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_hermes_native_stop(conv_id: str) -> Response:
        """Hard-stop a hermes-native session by killing its tmux session.

        Mirrors :func:`_handle_goose_native_stop`: kill the pane (ends
        ``hermes``), tear the terminal resource down, cancel the forwarder,
        publish ``idle``, and reclaim any sub-agent work entry.

        :param conv_id: Session/conversation identifier.
        :returns: 204 on success; 503 if the tmux target is unavailable.
        """
        from omnigent.hermes_native_bridge import bridge_dir_for_session_id, kill_session

        try:
            await asyncio.to_thread(
                kill_session, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "hermes_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="hermes-native stop"),
                },
            )
        await _teardown_session_terminals(conv_id)
        await _cancel_auto_forwarder_task(conv_id)
        _publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "Hermes-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_qwen_native_interrupt(conv_id: str) -> Response:
        """Cancel the in-flight qwen turn by sending ``Escape`` to its TUI pane.

        qwen-native turns run inside the ``qwen`` TUI; the runner harness task
        returns right after appending the input-file submit, so the in-process
        cancel floor has nothing to cancel. qwen's input-file protocol has no
        interrupt command, so — like goose-native — Stop drives Escape through
        the display pane.

        :param conv_id: Session/conversation identifier.
        :returns: 204 when Escape was sent; 503 if the tmux target is unavailable.
        """
        from omnigent.qwen_native_bridge import bridge_dir_for_session_id, inject_interrupt

        try:
            await asyncio.to_thread(
                inject_interrupt, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "qwen_native_interrupt_failed",
                    "detail": _client_safe_error_detail(exc, context="qwen-native interrupt"),
                },
            )
        _wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)

    async def _handle_qwen_native_stop(conv_id: str) -> Response:
        """Hard-stop a qwen-native session by killing its tmux session.

        Mirrors :func:`_handle_goose_native_stop`: kill the pane (ends ``qwen``),
        tear the terminal resource down, cancel the forwarder, publish ``idle``,
        and reclaim any sub-agent work entry.

        :param conv_id: Session/conversation identifier.
        :returns: 204 on success; 503 if the tmux target is unavailable.
        """
        from omnigent.qwen_native_bridge import bridge_dir_for_session_id, kill_session

        try:
            await asyncio.to_thread(
                kill_session, bridge_dir_for_session_id(conv_id), timeout_s=1.0
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "qwen_native_stop_failed",
                    "detail": _client_safe_error_detail(exc, context="qwen-native stop"),
                },
            )
        await _teardown_session_terminals(conv_id)
        await _cancel_auto_forwarder_task(conv_id)
        _publish_event(conv_id, {"type": "session.status", "status": "idle"})
        delivery_ack = _mark_subagent_terminal_and_wake(
            conv_id,
            status="cancelled",
            output="[System: sub-agent stopped]",
        )
        if not delivery_ack.delivered and (
            delivery_ack.entry is not None or conv_id in _session_sub_agent_names
        ):
            _logger.warning(
                "qwen-native stop succeeded but sub-agent delivery was "
                "not confirmed; session=%s reason=%s",
                conv_id,
                delivery_ack.reason,
            )
        return Response(status_code=204)

    async def _handle_claude_native_effort_change(
        conv_id: str,
        effort: str | None,
    ) -> Response:
        """
        Type ``/effort <level>`` into Claude's tmux pane.

        Claude-native sessions can't read the persisted
        ``reasoning_effort`` field at turn boundaries — the
        ``--effort`` flag on the ``claude`` binary is baked in at
        spawn. To propagate a live change without restarting the
        pane, this helper types Claude Code's built-in slash
        command into the terminal.

        Skipped silently when:

        * *effort* is ``None`` — Claude Code has no slash form for
          "use the spawn default", so a clear only takes effect on
          the next spawn.
        * *effort* is in ``EFFORT_VALUES`` but not in
          ``CLAUDE_EFFORTS`` (i.e. ``none`` / ``minimal``) —
          injecting ``/effort none`` would type a literal Claude's
          TUI rejects.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param effort: New persisted effort level, e.g. ``"high"``;
            ``None`` when the user cleared the override.
        :returns: 204 on success or skip (caller treats both the
            same — persisted value is the authoritative fallback).
            503 if the tmux target isn't yet advertised (best-
            effort failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )
        from omnigent.reasoning_effort import CLAUDE_EFFORTS

        if effort is None or effort not in CLAUDE_EFFORTS:
            # Persistence already happened on the Omnigent server; the
            # next spawn will pick up the new value via ``--effort``.
            return Response(status_code=204)
        # Resolve the bridge id from the session's labels so
        # ``/fork`` sessions (where bridge_id != conv_id) land in
        # the right tmux pane. Falls back to ``conv_id`` for legacy
        # single-session bridges — same pattern
        # ``_handle_claude_native_interrupt`` uses.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/effort {effort}"
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached; persisted effort still applies on next spawn.
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
        """
        Type ``/model <name>`` into Claude's tmux pane.

        Claude-native sessions can't read the persisted ``model_override``
        field at turn boundaries — the ``--model`` flag on the
        ``claude`` binary is baked in at spawn. To propagate a live
        change without restarting the pane, this helper types Claude
        Code's built-in slash command into the terminal.

        Skipped silently when *model* is ``None`` or empty / whitespace
        only — Claude Code has no slash form for "use the spawn
        default", so a clear only takes effect on the next spawn.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param model: New persisted model identifier, e.g.
            ``"claude-opus-4-7"``; ``None`` when the user cleared the
            override.
        :returns: 204 on success or skip (caller treats both the
            same — persisted value is the authoritative fallback).
            503 if the tmux target isn't yet advertised (best-effort
            failure).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        if model is None or not model.strip():
            # Persistence already happened on the Omnigent server; the
            # next spawn will pick up the new value via ``--model``.
            return Response(status_code=204)
        # Resolve the bridge id from the session's labels so
        # ``/fork`` sessions (where bridge_id != conv_id) land in
        # the right tmux pane. Falls back to ``conv_id`` for legacy
        # single-session bridges — same pattern
        # ``_handle_claude_native_interrupt`` uses.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        command = f"/model {model.strip()}"
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached; persisted model still applies on next spawn.
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
        """
        Switch a running cursor-native session's model via its TUI picker.

        cursor-agent's ``--model`` flag is baked in at spawn (see
        ``_auto_create_cursor_terminal``), so a live web-UI / REPL ``/model``
        switch can't be applied by re-reading the persisted ``model_override``
        — ``inject_model_command`` types ``/model <id>`` into the tmux pane and
        selects the filtered match. Mirrors ``_handle_claude_native_model_change``.

        Skipped silently when *model* is ``None`` or blank — cursor-agent has no
        slash form for "use the spawn default", so a clear only takes effect on
        the next spawn.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param model: New persisted cursor-agent model id, e.g. ``"gpt-5.2"``;
            ``None`` when the user cleared the override.
        :returns: 204 on success or skip; 503 if the tmux pane isn't advertised
            yet (best-effort — the persisted value applies on the next spawn).
        """
        from omnigent.cursor_native_bridge import (
            bridge_dir_for_session_id,
            inject_model_command,
        )

        if model is None or not model.strip():
            # Persistence already happened on the Omnigent server; the
            # next spawn picks up the new value via ``--model``.
            return Response(status_code=204)
        bridge_dir = bridge_dir_for_session_id(conv_id)
        try:
            # Short timeout: a missing tmux.json means the pane isn't attached;
            # the persisted model still applies on the next spawn.
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
                    "error": "cursor_native_model_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native model change"),
                },
            )
        return Response(status_code=204)

    async def _handle_claude_native_compact(conv_id: str) -> Response:
        """
        Type ``/compact`` into Claude's tmux pane.

        Explicit compaction on a claude-native session must run inside
        Claude Code, which owns its own context window in the terminal.
        The Omnigent server's own compaction path (``compact_conversation_now``)
        would only summarise the AP-side transcript mirror — it cannot
        shrink Claude's real context and would desync the two. So the
        web-UI ``/compact`` is injected as Claude Code's built-in slash
        command, the same way ``/effort`` and ``/model`` are.

        Returns 200 (not 204) on successful injection so the Omnigent server
        can tell the control was handled in the terminal and skip its
        own AP-side compaction. Other harnesses 204 no-op in the
        ``post_session_events`` dispatch and the Omnigent server runs its
        in-process compaction instead.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 200 once ``/compact`` has been typed into the pane.
            503 if the tmux target isn't yet advertised (the pane is
            not attached, so there is nothing to compact).
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            inject_slash_command,
        )

        # Resolve the bridge id from the session's labels so ``/fork``
        # sessions (where bridge_id != conv_id) land in the right tmux
        # pane. Falls back to ``conv_id`` for legacy single-session
        # bridges — same pattern the effort/model handlers use.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached, so there is no live Claude to compact.
            # ``auto_confirm`` is left False — ``/compact`` does not pop
            # a confirmation dialog the way ``/effort`` / ``/model`` do.
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
        """
        Type ``/compact`` into Codex's tmux pane.

        Mirrors :func:`_handle_claude_native_compact` for codex-native
        sessions.  Codex owns its own context window in the terminal,
        so explicit compaction must be injected as the ``/compact``
        slash command — the same rationale as the claude-native path.

        The tmux pane coordinates come from the **resource registry**
        (not a ``tmux.json`` sidecar) because codex-native terminals
        are launched through the registry.  This is the same resolution
        path :func:`_handle_codex_native_cost_popup` uses.

        Returns 200 on successful injection so the Omnigent server
        knows the control was handled in the terminal and skips its
        own AP-side compaction.  204 when no live terminal is
        registered (the server falls back to in-process compaction).

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :returns: 200 once ``/compact`` has been typed into the pane.
            204 if no live codex terminal is registered for the session.
            503 if the tmux send-keys invocation fails.
        """
        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            # No live codex terminal — let the server run AP-side compaction.
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

    async def _handle_cursor_native_compact(conv_id: str) -> Response:
        """
        Inject ``/summarize`` into the cursor-agent TUI pane.

        cursor-native sessions manage their own context window inside the
        cursor-agent TUI.  Explicit compaction must be handled there (via
        cursor-agent's built-in ``/summarize`` slash command) rather than as
        AP-side compaction, which would only summarise the transcript mirror
        and desync the two context windows — the same rationale as
        :func:`_handle_claude_native_compact`.

        cursor-agent has no compaction hook (the way Claude Code's
        ``PreCompact`` / ``SessionStart`` hooks drive claude-native's
        ``external_compaction_status`` forwarding), so completion can't be
        observed here — the summarization runs asynchronously in the pane after
        we submit ``/summarize``.  This handler only *starts* it: publish
        ``response.compaction.in_progress`` so the web UI raises its "Compacting
        conversation…" spinner, then submit the command.  The matching
        ``completed`` edge is emitted later by
        :mod:`omnigent.cursor_native_forwarder` when it observes cursor write the
        ``[Previous conversation summary]:`` rollup blob to its store — so the
        permanent "Conversation compacted" marker tracks cursor's real progress
        instead of firing the instant the command was submitted.  If the
        injection fails we publish ``response.compaction.failed`` so the spinner
        is dismissed rather than stranded (no summary blob will ever arrive to
        complete it).  Returns 200 so the Omnigent server knows the control was
        handled in the terminal and skips its own AP-side compaction.

        :param conv_id: Session/conversation identifier.
        :returns: 200 once ``/summarize`` has been submitted into the pane.
            503 if the tmux target isn't yet advertised (the pane is not
            attached, so there is nothing to compact).
        """
        from omnigent.cursor_native_bridge import bridge_dir_for_session_id, inject_user_message

        bridge_dir = bridge_dir_for_session_id(conv_id)
        _publish_event(conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id})
        try:
            # inject_user_message uses bracketed paste, which bypasses cursor-agent's
            # slash-command autocomplete dropdown. send-keys typing the literal
            # ``/summarize`` opens that dropdown, and the single submit Enter then
            # confirms the highlighted completion instead of submitting the command,
            # so the command was never sent (the original bug).
            await asyncio.to_thread(
                inject_user_message,
                bridge_dir,
                content="/summarize",
                timeout_s=1.0,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            # Dismiss the spinner the in_progress event raised — the history
            # was not compacted, so no permanent marker should be left, and no
            # summary blob will arrive for the forwarder to complete it.
            #
            # OSError is in scope (unlike the claude-native analog): cursor's
            # ``inject_user_message`` writes the paste payload to a tempfile in
            # ``bridge_dir`` first, so a filesystem fault (disk full, perms, dir
            # gone) raises OSError. Without it here that escapes after in_progress
            # already fired, stranding the spinner with no failed/completed edge.
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="cursor-native compact"),
                },
            )
        return Response(status_code=200)

    def _inject_codex_compact(socket_path: str, target: str) -> None:
        """
        Blocking helper: type ``/compact`` into a codex tmux pane.

        Uses the same ``C-u`` → literal ``/compact`` → ``Enter``
        sequence that :func:`~omnigent.claude_native_bridge.inject_slash_command`
        uses for claude-native.  Factored into its own function so
        :func:`_handle_codex_native_compact` can run it via
        ``asyncio.to_thread`` without importing at call time.

        :param socket_path: Absolute path to the tmux socket, e.g.
            ``"/tmp/.../codex-main.sock"``.
        :param target: Tmux target pane, e.g. ``"main"``.
        :raises RuntimeError: If any ``tmux send-keys`` invocation fails.
        """
        from omnigent.claude_native_bridge import _run_tmux

        # Clear any draft the user is mid-typing.
        _run_tmux(socket_path, "send-keys", "-t", target, "C-u")
        # Paste ``/compact`` literally.
        _run_tmux(socket_path, "send-keys", "-l", "-t", target, "/compact")
        # Submit.
        _run_tmux(socket_path, "send-keys", "-t", target, "Enter")

    async def _handle_claude_native_cost_popup(
        conv_id: str,
        elicitation_id: str,
        message: str,
        policy_name: str | None = None,
    ) -> Response:
        """
        Overlay a cost-budget approval modal on Claude's tmux pane.

        A server-side tool-policy ASK (the ``TOOL_CALL`` gate, e.g. a
        cost-budget warning checkpoint) parks and is published to the
        web UI as an ``ApprovalCard``. For a user driving the session in the native
        terminal — who never sees the web card — the Omnigent server forwards a
        ``cost_approval_popup`` control event here, and this handler pops
        a ``tmux display-popup`` modal in the pane. The popup resolves the
        **same** elicitation via the same endpoint the web card uses, so
        whichever surface answers first wins and the other clears. The
        server-side approval Future (and its decline-on-timeout → stop
        behaviour) is unchanged — this only adds a second answer surface.

        Best-effort: the modal is fired detached (it does not block this
        handler), and a pane that isn't attached / a tmux too old for
        ``display-popup`` simply leaves the web card as the only surface.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Outstanding elicitation correlation id,
            e.g. ``"elicit_deadbeef"``.
        :param message: Approval reason to display, e.g.
            ``"Session cost $0.12 crossed the $0.10 checkpoint. Continue?"``.
        :param policy_name: Name of the deciding policy, rendered as the
            modal header. ``None`` falls back to a generic header.
        :returns: 204 once the popup has been dispatched (or skipped when
            the pane isn't advertised). 503 only if resolving the bridge
            target raised — a best-effort failure the web card covers.
        """
        from omnigent.claude_native_bridge import (
            bridge_dir_for_bridge_id,
            display_cost_approval_popup,
        )

        # Resolve the bridge id from the session's labels so ``/fork``
        # sessions (where bridge_id != conv_id) land in the right tmux
        # pane. Falls back to ``conv_id`` for legacy single-session
        # bridges — same pattern the effort/model/compact handlers use.
        bridge_id = await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=conv_id,
        )
        bridge_dir = bridge_dir_for_bridge_id(bridge_id)
        try:
            # Short timeout: missing tmux.json means the pane isn't
            # attached, so there is no client to render the modal — the
            # web ApprovalCard is the only surface and that is fine.
            await asyncio.to_thread(
                display_cost_approval_popup,
                bridge_dir,
                session_id=conv_id,
                elicitation_id=elicitation_id,
                message=message,
                policy_name=policy_name,
                timeout_s=1.0,
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
        """
        Overlay a cost-budget approval modal on Codex's tmux pane.

        The codex-native counterpart of
        :func:`_handle_claude_native_cost_popup`. Codex does not advertise
        a ``tmux.json`` (its terminal is launched through the resource
        registry), so the pane's socket/target come from the registry
        instance — the same source the web-terminal attach uses — and AP
        routing comes from this bridge's ``policy_hook.json``. Resolution
        differs; the actual popup launch is the shared, harness-agnostic
        :func:`omnigent.native_cost_popup.launch_cost_popup`.

        Best-effort: skips (204) when no live codex terminal is registered
        for the session, so the web ApprovalCard remains the surface.

        :param conv_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param elicitation_id: Outstanding elicitation correlation id,
            e.g. ``"elicit_deadbeef"``.
        :param message: Approval reason to display.
        :param policy_name: Name of the deciding policy, rendered as the
            modal header. ``None`` falls back to a generic header.
        :returns: 204 once the popup is dispatched (or skipped when no
            terminal is registered). 503 if launching raised.
        """
        from omnigent.codex_native_bridge import _POLICY_HOOK_FILE, bridge_dir_for_bridge_id
        from omnigent.native_cost_popup import launch_cost_popup

        registry = resource_registry.terminal_registry
        instance = registry.get(conv_id, "codex", "main") if registry is not None else None
        if instance is None or not instance.running:
            # No live codex terminal to render on; web card is the surface.
            return Response(status_code=204)
        config_file = bridge_dir_for_bridge_id(conv_id) / _POLICY_HOOK_FILE
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

    async def _native_cost_popup_config_file(conv_id: str, harness: str) -> Path:
        """
        Resolve the AP-routing config file the cost popup reads, per harness.

        The popup script reads ``ap_server_url`` + ``ap_auth_headers`` from
        this file: ``permission_hook.json`` in the claude-native bridge dir,
        ``policy_hook.json`` in the codex-native bridge dir.

        :param conv_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param harness: ``"claude-native"`` or ``"codex-native"``.
        :returns: Path to the harness's AP-routing config file.
        """
        if harness == "claude-native":
            from omnigent import claude_native_bridge as _cnb

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client, session_id=conv_id
            )
            return _cnb.bridge_dir_for_bridge_id(bridge_id) / _cnb._PERMISSION_HOOK_FILE
        from omnigent import codex_native_bridge as _cxb

        return _cxb.bridge_dir_for_bridge_id(conv_id) / _cxb._POLICY_HOOK_FILE

    async def _repop_pending_cost_popup_on_attach(
        conv_id: str,
        socket_path: str,
        tmux_target: str,
    ) -> None:
        """
        Re-pop a still-pending native approval on a newly attached client.

        Covers the case where the ASK fired while no terminal client was
        attached (the user was in the web Chat), then the user opens the
        Terminal: on attach this re-checks the session snapshot and, if a
        native approval is still outstanding — the server-side policy gate
        (``TOOL_CALL`` / ``LLM_REQUEST``, e.g. a cost-budget checkpoint, or
        the ``REQUEST`` gate a native session enforces via the
        ``UserPromptSubmit`` hook) — pops it on the now-attached client.
        Self-correcting — it only pops while the elicitation is still
        pending, so an already-answered approval is not re-shown. Complements
        the ASK-time forward (which covers clients attached *before* the
        ASK). Best-effort: any miss leaves the web card.

        :param conv_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param socket_path: tmux socket of the attaching pane.
        :param tmux_target: tmux target of the attaching pane, e.g. ``"main"``.
        :returns: None.
        """
        harness = _session_harness_name(conv_id)
        if harness not in ("claude-native", "codex-native"):
            return
        from omnigent.native_cost_popup import launch_cost_popup, wait_for_tmux_client

        # The attach is in flight when this task starts; wait for the client
        # to register so there is something to render the modal on.
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
        # The native popup surfaces the server-side policy gate, which parks
        # and resolves via the same endpoint. Re-pop whichever is pending:
        # the tool-policy gate (tool_call / llm_request — including
        # cost-budget checkpoints) and the request-phase gate (request),
        # which native sessions enforce via the UserPromptSubmit hook. A
        # request-phase ASK typically fires while the user is in the web
        # Chat (no client attached), so the on-attach re-pop is its main
        # path onto the terminal.
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
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        Turn-end bookkeeping called from proxy_stream completion points.

        Removes the session from ``_active_turns``, publishes the
        appropriate ``session.status`` event (``idle`` on success
        or cancellation, ``failed`` on error), and schedules a
        post-turn buffer check.

        For a scaffold (in-process) sub-agent, a *successful* turn end is
        reported to the parent as the terminal completion only when no
        continuation is buffered — otherwise the intermediate turn's text
        would be delivered and the real final synthesis dropped (the
        already-terminal entry short-circuits later delivery). Deferring to
        the continuation's own empty-buffer stream end can't strand the
        result: every ``_run_turn_bg`` exit routes back through here, and
        ``_check_and_start_next_turn`` always starts a turn while the buffer
        is non-empty. The error/interrupt/cancel branches stay unconditional
        — those are genuine terminal outcomes, not intermediate narration.

        :param conv_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param error: If the turn ended due to an error, a dict
            with at least a ``"message"`` key. ``None`` for
            successful completion.
        """

        _active_turns.pop(conv_id, None)
        # Turn ended: clear the live marker so a concurrent forward is skipped.
        _live_response_id.pop(conv_id, None)
        # Skip the idle transient when a buffered message will start a
        # continuation turn immediately — `_check_and_start_next_turn`
        # publishes "running" microseconds later, and the in-between idle
        # otherwise hides the Working indicator on the client.
        # `failed` is always published so a real error is never swallowed.
        has_buffered = bool(_session_message_buffers.get(conv_id))
        was_interrupted = conv_id in _interrupted_sessions
        if was_interrupted:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            if not has_buffered:
                _publish_turn_status(conv_id, "idle")
        elif error is not None:
            # Carry the failure detail so a SETUP-phase failure (no
            # response.failed event) still surfaces a real error message to
            # clients instead of ending silently. ``failed`` is published
            # for every harness (including claude-native) — see
            # _publish_turn_status.
            _publish_turn_status(conv_id, "failed", error=_normalize_turn_error(error))
        else:
            if not has_buffered:
                # Emit ``waiting`` instead of ``idle`` when the turn ended
                # cleanly but sub-agents are still running. This lets the
                # headless ``-p`` multi-turn loop (``_drain_extra_turns`` in
                # ``chat.py``) distinguish an async orchestrator that parked
                # on the inbox drain from a truly finished single-turn agent —
                # both would otherwise emit ``idle`` here, making them
                # indistinguishable without a "waiting" signal.
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
            # Defer the success delivery while a continuation is buffered —
            # see the docstring. The continuation turn's own empty-buffer
            # stream end delivers exactly once with the final assistant text.
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="completed",
                output=_extract_last_assistant_text(conv_id),
            )
        # Belt-and-suspenders: POST the terminal status directly to the
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
        """Force-cancel a session's in-flight turn task — the cancel floor.

        The scaffold's interrupt only takes effect when the executor adapter
        polls between emitted events, so a turn blocked mid-op — or one whose
        executor has no native interrupt — can hang until natural completion.
        Cancelling the runner turn task (the proven primitive from
        :func:`delete_session`) unwinds the runner side regardless of harness.

        On a cancel during the streaming phase, ``_drain_streaming_response``'s
        ``CancelledError`` handler pops ``_active_turns`` and publishes ``idle``
        — but it does NOT append the cancellation items (synthetic outputs for
        dangling tool calls + the interrupted marker). So when the session was
        interrupted, append them here. The ``_interrupted_sessions`` discard is
        the idempotency token: a natural completion that races the cancel runs
        ``_on_proxy_stream_end``, which discards the flag first, so this block
        then no-ops.

        A cancel during the *setup* phase (before ``_drain_streaming_response``
        is entered) raises ``CancelledError`` — a ``BaseException`` — past
        ``_run_turn_bg``'s ``except Exception``, so neither handler runs and
        ``_active_turns`` is left stale (every later message then buffers and
        the session hangs). Detected by the entry still pointing at this task
        after the await; we run the full terminal bookkeeping via
        ``_on_proxy_stream_end`` to recover.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param expected_task: If given, only cancel when this exact task is
            still the live turn. Guards against cancelling a continuation turn
            that replaced the original (the original completed naturally while
            the caller was forwarding the interrupt) — killing that would orphan
            its dangling tool calls.
        :returns: ``True`` if a running turn was cancelled, ``False`` if there
            was no live turn task (or it was replaced by a continuation).
        """
        turn_task = _active_turns.get(conv_id)
        if not isinstance(turn_task, asyncio.Task) or turn_task.done():
            return False
        if expected_task is not None and turn_task is not expected_task:
            return False
        turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn_task
        if _active_turns.get(conv_id) is turn_task:
            # Setup-phase cancel: no handler cleaned up. _on_proxy_stream_end
            # pops _active_turns, publishes idle (or starts a buffered
            # continuation), and runs the interrupted path (flag-discard +
            # cancellation items) itself, so skip the block below.
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
        """Stop an in-process (non-native) harness's in-flight turn.

        Shared by the ``interrupt`` and ``stop_session`` dispatch. No-ops when no
        turn is in flight (a stale interrupted flag would taint the next turn).
        Forward the interrupt to the harness FIRST — while its turn is still
        in-flight — so the harness's interrupt handler engages (cancels the turn
        and drops the claude-sdk session); THEN force-cancel the runner turn task
        as the floor. Order matters: cancelling first closes the runner's harness
        stream, which ends the harness turn, so the later interrupt 404s and the
        session is never dropped — the next message then resumes the abandoned
        turn and the agent runs one message behind.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        target = _active_turns.get(conv_id)
        if not isinstance(target, asyncio.Task) or target.done():
            return
        _interrupted_sessions.add(conv_id)
        try:
            harness_client = await process_manager.get_client(conv_id, "any")
            await harness_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
                # Bounded under the Omnigent server's 5s stop deadline.
                timeout=3.0,
            )
        except Exception:  # noqa: BLE001 — best-effort: harness may have exited
            _logger.warning(
                "Interrupt forward to harness failed for %s",
                conv_id,
                exc_info=True,
            )
        await _cancel_active_turn(conv_id, expected_task=target)

    async def _check_and_start_next_turn(
        session_id: str,
    ) -> None:
        """
        Drain the message buffer and start a continuation turn.

        Called after a turn ends. If messages were buffered while
        the turn was active, pops the first one and starts a new
        background turn. The background turn's completion will
        recursively call this function.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """

        # Serialize the drain + turn-start against a concurrent
        # post_session_events via the same ingest gate so the two paths can't
        # both start a turn (invariant I2; a second turn-driver POST → 204).
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
                # Concurrent path already started a turn — key membership (None
                # sentinel or Task) per the runner-wide convention, so a
                # streaming start (slot stays None) is also detected. That turn
                # re-enters here on completion to drain the buffer.
                return

            buf = _session_message_buffers.get(session_id)
            if not buf:
                _rewake_parent_if_inbox_stranded(session_id)
                return

            if _is_native_harness(session_id):
                # Native harnesses type only the latest message per turn; drain
                # one at a time, in order (RUNNER_MESSAGE_INGEST.md Part C).
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
                # LLM harnesses: drain ALL buffered messages into history so
                # rapid-fire input becomes a single continuation turn.
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

            # Reserve before the await so a concurrent POST sees an active turn.
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

    async def _post_subagent_wake_notice(parent_id: str, notice: str, child_id: str) -> None:
        """
        POST a framework wake notice to a parent session's event stream.

        Mirrors the timer-firing POST in ``tool_dispatch._timer_loop``: the
        synthetic ``user`` message rides the normal ingest path, which starts
        a continuation turn when the parent is idle or buffers (coalescing
        with any other pending messages into a single later turn) when a turn
        is already active. The completion payload itself already sits in the
        parent inbox; this only delivers the wake signal.

        Delivery is delegated to :func:`_deliver_subagent_wake_post`, which
        checks the response status and retries transient failures (e.g. a
        503 ``RUNNER_UNAVAILABLE`` while the parent's runner tunnel
        reconnects). On terminal failure the debounce flag is released so a
        later completion can retry — no parent turn will run to clear it
        otherwise — and a warning is logged.

        :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
        :param notice: The ``[System: ...]`` notice text to inject.
        :param child_id: Completing child session id, included only for log
            context, e.g. ``"conv_child456"``.
        :returns: None.
        """
        delivered = await _deliver_subagent_wake_post(server_client, parent_id, notice)
        if not delivered:
            # A failed wake must not crash turn-end; the inbox keeps the result.
            # Release the debounce flag so a later completion can retry the
            # wake — no parent turn will run to clear it otherwise.
            _subagent_wake_pending.discard(parent_id)
            _logger.warning(
                "Sub-agent wake POST failed for parent=%s child=%s after %d attempt(s); "
                "result remains in the parent inbox until the next wake",
                parent_id,
                child_id,
                _WAKE_POST_MAX_ATTEMPTS,
            )

    def _schedule_subagent_wake(entry: _SubagentWorkEntry) -> None:
        """
        Schedule a wake POST after a child completion lands in the parent inbox.

        Called by ``_mark_subagent_terminal_and_wake`` once per delivery (it
        gates on the not-delivered → delivered transition), and a parent is
        never its own child, so a parent's own turn-end never re-wakes it.

        Debounced per parent: while a wake is outstanding (posted, not yet
        consumed by the parent's next turn start), further completions skip
        posting — a fan-out's results all queue in the one inbox, which a
        single wake turn drains via ``sys_read_inbox``. This prevents the
        wake storm (one /events message per completion) that churns turns and
        trips the executor's per-turn tool-context guard.

        :param entry: The just-delivered terminal sub-agent work entry.
        :returns: None.
        """
        # A session is never its own sub-agent; never wake on self.
        if entry.parent_session_id == entry.child_session_id:
            return
        inbox = _session_inboxes.get(entry.parent_session_id)
        if inbox is None:
            return
        # Debounce: one outstanding wake per parent (cleared at turn start).
        if entry.parent_session_id in _subagent_wake_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Off the event loop (defensive); completion drains on the next turn.
            return
        _subagent_wake_pending.add(entry.parent_session_id)
        # qsize counts the item just delivered by put_nowait (>= 1).
        notice = _format_subagent_wake_notice(
            agent=entry.agent,
            title=entry.title,
            status=entry.status,
            pending=inbox.qsize(),
        )
        _wake_task = loop.create_task(
            _post_subagent_wake_notice(entry.parent_session_id, notice, entry.child_session_id)
        )
        _wake_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_wake_task)

    def _rewake_parent_if_inbox_stranded(parent_session_id: str) -> None:
        """
        Clear a stuck wake flag on parent idle, re-arming if results remain.

        The wake debounce (``_subagent_wake_pending``) is cleared only at turn
        start. A wake consumed as a mid-turn injection never enters
        ``_run_turn_bg``, so the flag stays stuck with no future turn to clear
        it — and the next completion is then debounced and stranded. This runs
        when the parent idles (turn ended, no buffered continuation), so the
        flag is always released here regardless of inbox state; otherwise a
        wake the parent already drained in that same turn would leave the flag
        set and strand the *next* completion. The recovery wake is only posted
        when the inbox still holds undrained results. (The fan-out coalesce
        path is unaffected: it has no turn here, so this is never reached and
        its single outstanding wake still starts the draining turn.)

        :param parent_session_id: Parent whose turn just ended, e.g.
            ``"conv_parent123"``.
        :returns: None.
        """
        if parent_session_id not in _subagent_wake_pending:
            return
        # Always drop the stale flag: the turn just ended with no continuation,
        # so nothing else will clear it. Leaving it set (even on an emptied
        # inbox) would debounce and strand the next completion.
        _subagent_wake_pending.discard(parent_session_id)
        inbox = _session_inboxes.get(parent_session_id)
        if inbox is None or inbox.empty():
            # Flag cleared; nothing stranded to re-wake on.
            return
        entries = list_subagent_work(parent_session_id)
        if not entries:
            return
        # Use the latest completed child so the notice names a real (agent,
        # title); _schedule_subagent_wake recomputes the count from the inbox.
        latest = max(
            entries,
            key=lambda entry: entry.completed_at if entry.completed_at is not None else 0.0,
        )
        _schedule_subagent_wake(latest)

    def _mark_subagent_terminal_and_wake(
        child_session_id: str, *, status: str, output: str | None
    ) -> _SubagentDeliveryAck:
        """
        Mark a child terminal and wake its parent if a payload was delivered.

        Thin wrapper over ``mark_subagent_work_terminal`` for the turn-end
        call sites: it wakes the parent only on a genuine not-delivered →
        delivered transition, so a re-marked (already-terminal) child or an
        untracked session (e.g. the orchestrator's own turn ending) never
        fires a spurious or looping wake.

        :param child_session_id: Child session id, e.g. ``"conv_child456"``.
        :param status: Terminal status: ``"completed"``, ``"failed"``, or
            ``"cancelled"``.
        :param output: Child output or error text. ``None`` means the
            completion had no assistant text to deliver.
        :returns: Delivery acknowledgement for the terminal report.
        """
        ack = mark_subagent_work_terminal(child_session_id, status=status, output=output)
        if ack.entry is not None and ack.delivered_now:
            _schedule_subagent_wake(ack.entry)
        return ack

    async def _ensure_comment_relay_started(
        session_id: str,
        *,
        bridge_id: str | None = None,
        explicit_bridge_dir: Path | None = None,
        await_notify: bool = False,
    ) -> None:
        """
        Ensure the comment-tool relay is running for a ``claude-native`` session.

        Writes ``tool_relay.json`` into the session's bridge directory so the
        MCP bridge subprocess (running inside Claude Code) discovers and
        dispatches ``list_comments`` / ``update_comment``, then fires a
        ``notifications/tools/list_changed`` so a Claude Code instance that has
        already fetched its tool list re-fetches it.

        Idempotent and session-scoped: the relay is started once and lives
        until the session is deleted (see the cleanup in ``delete_session``).
        It is started from two places, whichever runs first:

        - ``create_session_terminal`` (the ``bridge_inject_dir`` branch), which
          fires as the Claude terminal launches — after the client has reset
          the bridge dir and before Claude Code's MCP client performs its
          initial ``tools/list``. This is the normal ``omnigent claude``
          path: the comment tools land on that first list with no notification
          race, so the notification is sent in the background (the bridge
          server is not up yet, and awaiting it would block the launch).
        - ``_run_turn_bg`` on the first turn, as a fallback for sessions whose
          terminal was launched outside the runner terminal route — including
          UI-launched terminals, which are never pre-warmed. Here Claude Code
          has already listed its tools, so the relayed tools land a beat late;
          the caller passes ``await_notify=False`` anyway, because a fresh
          UI-launched terminal's bridge has not published ``server.json`` yet
          and awaiting delivery would stall the turn ~15s on the readiness
          poll. The notification fires in the background instead.

        Relay-start failures are logged and swallowed: the relay is additive,
        and a failed socket bind or file write must never break the terminal
        launch or the turn that triggered it.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param bridge_id: Opaque bridge id resolved by the caller, e.g.
            ``"bridge_abc123"``. ``None`` resolves it from the session labels
            via :func:`_claude_native_bridge_id_for_session`.
        :param await_notify: When ``True``, await the
            ``notifications/tools/list_changed`` delivery before returning
            (warm-bridge fallback path); when ``False``, fire it in the
            background (cold-bridge terminal-launch path). Pass ``False``
            for codex-native: codex starts its MCP bridge server lazily (only
            once it runs the turn), so awaiting delivery on a fresh session
            blocks for ``post_tools_changed``'s full readiness timeout (~30s)
            before the turn is dispatched. ``tool_relay.json`` is already on
            disk by then, so codex's initial ``tools/list`` sees the relay
            tools without the notification.
        :returns: None.
        """
        # Fast path: a relay is already running for this session.
        if session_id in _session_comment_relays:
            return

        import json as _json

        from omnigent.claude_native_bridge import (
            ClaudeNativeToolRelay,
            bridge_dir_for_bridge_id,
            post_tools_changed,
            start_tool_relay,
        )
        from omnigent.runner.tool_dispatch import _NATIVE_RELAY_BUILTIN_TOOLS
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

        # Resolve the bridge dir. When an explicit bridge_dir is
        # provided (codex-native path), skip the claude-native bridge
        # id lookup entirely — the caller already resolved it.
        if explicit_bridge_dir is not None:
            bridge_dir = explicit_bridge_dir
        else:
            # Resolve the bridge id (the only await) BEFORE recording
            # anything, so the start→store section below runs
            # atomically: a concurrent delete or a second starter
            # can't interleave mid-setup and strand a relay.
            if bridge_id is None:
                bridge_id = await _claude_native_bridge_id_for_session(
                    server_client=server_client,
                    session_id=session_id,
                )

            # Re-check: another starter may have published the relay
            # during the await.
            if session_id in _session_comment_relays:
                return

            bridge_dir = bridge_dir_for_bridge_id(bridge_id or session_id)

        # Build flat tool schemas (name + description + parameters) for the
        # native relay. start_tool_relay normalises these via
        # _normalize_relay_tool_specs before writing tool_relay.json.
        #
        # claude-native / codex-native ignore the harness ``tools`` list, so
        # this relay is the ONLY tool surface reaching the real CLI — tools
        # added here override the bridge's static tools of the same name,
        # giving centralized policy evaluation on the Omnigent server. Two groups
        # are assembled:
        #
        # 1. The runner-/server-proxied builtin surface
        #    (``_NATIVE_RELAY_BUILTIN_TOOLS`` — comment, session read/write,
        #    agent-discovery, and terminal families), derived from the
        #    session's own ToolManager so the relayed set and the
        #    spec-dependent schemas (e.g. sys_session_send's named-mode
        #    ``agent`` enum, present only when the spec declares
        #    sub-agents; sys_terminal_*, present only when the spec
        #    declares ``terminals:``) exactly match what non-native
        #    harnesses receive via ``request.tools``.
        # 2. OS tools (``sys_os_*``), relayed unconditionally below to
        #    override the bridge's static (non-policy-enforced) versions —
        #    independent of the spec's ``os_env`` gate.
        relay_schemas: list[dict[str, Any]] = []

        def _append_flat_schema(function_dict: dict[str, Any]) -> None:
            """
            Append a tool's OpenAI ``function`` schema in flat relay shape.

            :param function_dict: The ``"function"`` sub-dict of a tool
                schema, e.g. ``{"name": "sys_session_list", "parameters":
                {...}}``.
            :returns: None.
            """
            relay_schemas.append(
                {
                    "name": function_dict["name"],
                    "description": function_dict.get("description", ""),
                    "parameters": function_dict.get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
            )

        # Resolve the session's agent spec so the relayed builtin surface
        # mirrors the spec's gating exactly. This is an await, so re-check
        # for a concurrently-started relay afterward. The relay is additive
        # and must never break the launch/turn, so a resolver error (HTTP
        # failure, not-yet-bound agent on a cold terminal launch) falls back
        # to the always-on read/discovery surface rather than propagating.
        try:
            relay_spec = await _resolve_session_agent_spec(session_id)
        except OmnigentError:
            relay_spec = None
        if session_id in _session_comment_relays:
            return
        if relay_spec is not None:
            from omnigent.tools.manager import ToolManager

            for _schema in ToolManager(relay_spec).get_tool_schemas():
                _fn = _schema["function"]
                if _fn["name"] in _NATIVE_RELAY_BUILTIN_TOOLS:
                    _append_flat_schema(_fn)
        else:
            # No resolvable spec: fall back to the always-on read/discovery
            # surface — never the opt-in spawn writes (send/close/create),
            # whose gate (``tools.agents`` or ``spawn: true``) can't be
            # evaluated without the spec.
            from omnigent.tools.builtins.policy import SysAddPolicyTool, SysPolicyRegistryTool

            for _cls in (
                ListCommentsTool,
                UpdateCommentTool,
                SysSessionListTool,
                SysSessionGetHistoryTool,
                SysSessionGetInfoTool,
                SysAgentGetTool,
                SysAgentListTool,
                SysAgentDownloadTool,
                SysAddPolicyTool,
                SysPolicyRegistryTool,
            ):
                _append_flat_schema(_cls().get_schema()["function"])

        # Add OS tool schemas. Create a minimal OSEnvironment for schema extraction.
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
            for _tool in (
                SysOsReadTool(_os_env),
                SysOsWriteTool(_os_env),
                SysOsEditTool(_os_env),
                SysOsShellTool(_os_env),
            ):
                _append_flat_schema(_tool.get_schema()["function"])
            _os_env.close()
        except Exception:  # noqa: BLE001
            # OS environment setup failed; relay will run without OS tools.
            # This should not happen in practice, but we log and continue
            # since the relay is additive.
            _logger.debug(
                "Could not create OSEnvironment for relay OS tool schemas; "
                "OS tools will not be available in relay for session=%s",
                session_id,
            )

        # Capture session_id in the closure so concurrent sessions are
        # routed correctly.
        _captured_session_id = session_id

        async def _relay_tool_executor(
            name: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            """
            Relay one MCP tool call through the Omnigent server's /mcp endpoint.

            Routes the call through
            :class:`~omnigent.runner.proxy_mcp_manager.ProxyMcpManager`
            so the Omnigent server evaluates TOOL_CALL and TOOL_RESULT policies
            before executing the tool — consistent with all other harnesses
            (claude-sdk, openai-agents). Works for all relay tool types:
            comment tools, session query tools, and OS tools.

            :param name: Tool name, e.g. ``"list_comments"``,
                ``"sys_session_get_history"``, or ``"sys_os_read"``.
            :param arguments: Decoded tool arguments from Claude Code, e.g.
                ``{"conversation_id": "conv_abc"}`` or ``{"path": "file.txt"}``.
            :returns: Parsed JSON result dict for
                :func:`_mcp_response_from_tool_result`, e.g.
                ``{"items": [...]}`` or ``{"error": "..."}``.
            """
            result_str = await ProxyMcpManager(
                _captured_session_id, server_client, publish_event=_publish_event
            ).call_tool(None, name, arguments)
            try:
                return _json.loads(result_str)
            except _json.JSONDecodeError:
                # ProxyMcpManager returns raw text (not JSON) for
                # plain-text tool results (the MCP text-block content
                # joined as a string). Wrap it so
                # _mcp_response_from_tool_result receives a dict; the
                # "result" key is the same wrapper it would apply for
                # a non-dict value.
                return {"result": result_str}

        # start_tool_relay is synchronous, so start→store has no await: atomic.
        try:
            relay: ClaudeNativeToolRelay = start_tool_relay(
                bridge_dir=bridge_dir,
                tools=relay_schemas,
                tool_executor=_relay_tool_executor,
                loop=asyncio.get_running_loop(),
            )
        except (OSError, RuntimeError):
            # Relay is additive: a failed bind/write/thread-start must not break
            # the launch or turn. Nothing was recorded, so a later turn retries.
            _logger.warning(
                "Failed to start comment relay for session=%s",
                session_id,
                exc_info=True,
            )
            return
        _session_comment_relays[session_id] = relay

        async def _notify_tools_changed() -> None:
            """
            Notify Claude Code that its MCP tool list changed.

            ``post_tools_changed`` is synchronous and blocks until the bridge
            server publishes ``server.json``; run it in the default executor so
            the event loop is not blocked, and ignore the not-yet-ready bridge
            (the relay file is already on disk for the initial ``tools/list``).

            :returns: None.
            """
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
            # Warm-bridge fallback: the bridge is already up, so this returns
            # quickly and guarantees delivery before the caller injects the
            # user message — without a fixed sleep.
            await _notify_tools_changed()
        else:
            # Cold-bridge terminal-launch path: awaiting post_tools_changed
            # would block on its readiness wait. The relay file is already on
            # disk for Claude's initial tools/list, so notify in the background
            # purely to cover a warm re-attach.
            _notify_task = asyncio.create_task(_notify_tools_changed())
            _background_tasks.add(_notify_task)
            _notify_task.add_done_callback(_background_tasks.discard)

    async def _run_turn_advisor(
        msg_body: dict[str, Any],
        conv: str,
        spec: Any,  # type: ignore[explicit-any]  # resolved AgentSpec or None
    ) -> AdvisorTurnResult | None:
        """
        Run the cost advisor for one turn (no-op unless the spec opts in
        via ``executor.config.cost_optimize``).

        Every turn path that reaches the harness must run this so the
        per-turn brain-model verdict is judged, recorded, and (optimize
        mode, claude-sdk) applied to this turn's harness request.

        :param msg_body: The forwarded message body; the turn's query is
            read from ``msg_body["content"]`` and the user model pin from
            ``msg_body["model_override"]``.
        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param spec: The resolved agent spec for the session, or ``None``
            (advisor skipped).
        :returns: The verdict + apply_model + note, or ``None`` when the
            turn runs unadvised.
        """
        from datetime import datetime, timezone

        from omnigent.runner.cost_advisor import maybe_run_advisor

        # Resolve the brain harness so the advisor can scope application
        # (claude-sdk only). Mirrors _resolve_harness_config's derivation.
        harness: str | None = None
        if spec is not None:
            _h = spec.executor.config.get("harness") or spec.executor.type
            harness = canonicalize_harness(_h) or _h

        # Per-session Cost Optimized toggle, read defensively
        # off the snapshot so this still works against servers without
        # the column. Precedence (override > spec mode) is resolved inside.
        cost_control_mode_override = await _fetch_cost_control_mode_override(server_client, conv)
        return await maybe_run_advisor(
            spec=spec,
            conversation_id=conv,
            turn_content=msg_body.get("content") or [],
            server_client=server_client,
            turn_anchor=datetime.now(timezone.utc).isoformat(),
            harness=harness,
            # The server-forwarded session model pin (/model or web picker).
            # When set it BEATS the advisor (verdict recorded, not applied).
            user_model_override=msg_body.get("model_override"),
            cost_control_mode_override=cost_control_mode_override,
        )

    def _emit_routing_decision(conv: str, result: AdvisorTurnResult | None) -> None:
        """
        Stream the router's verdict as a turn-start transcript chip.

        Emitted on EVERY advised turn that produced a verdict — applied
        (optimize) or shadow (advise / user pin won) alike — so the model
        the router chose shows in the conversation flow the instant the
        turn begins. Independent of the ``cost_control.plan`` label PATCH:
        a 500 on that persist (telemetry) does NOT suppress this chip, and
        independent of :func:`_apply_advisor_for_turn`'s sticky/apply logic
        so a user-pin turn still surfaces the "would have picked" verdict.

        The AP server's stream relay turns this into a durable, display-only
        ``routing_decision`` item (in arrival order, before the assistant
        output) and forwards it live. No-op when no verdict was produced
        (advisor off, conversational turn, or judge/persist failure).

        :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param result: The advisor turn result, or ``None`` (no verdict —
            nothing to announce).
        """
        if result is None:
            return
        from omnigent.runner.cost_advisor import routing_decision_event

        _publish_event(conv, routing_decision_event(result.verdict))

    def _apply_advisor_for_turn(
        body: dict[str, Any],
        conv: str,
        result: AdvisorTurnResult | None,
        user_model_override: str | None = None,
    ) -> None:
        """
        Apply an advisor result to the turn body and keep the brain sticky.

        Optimize mode applied a model this turn: stamp it on the body and
        remember it. A turn that applied NOTHING (advise mode, a
        conversational/failed judge, or advisor off) carries forward the
        last applied model — so the claude-sdk brain stays on the advisor's
        last selection across conversational turns instead of flapping back
        to the gateway/spec default (whose ``set_model(None)`` would reset
        it).

        An explicit USER pin disables the carry-forward entirely. The pin
        reaches the harness via the spawn env (``HARNESS_<H>_MODEL``), which
        the body's ``model_override`` (→ ``cfg.model``) would BEAT in the
        executor — so stamping the sticky model here would silently override
        the user's choice (the live ``/model``-vs-advisor precedence bug).
        The stored selection is also dropped: user intent supersedes the
        advisor's last applied model, and resurrecting it after an unpin
        would flap the brain to a stale choice.

        :param body: The harness request body, mutated in place (caller owns
            it — copy-on-write at the streaming site).
        :param conv: Session id, key into the sticky-model state.
        :param result: The advisor turn result, or ``None`` (no verdict).
        :param user_model_override: The session's user model pin from the
            inbound message body, e.g. ``"databricks-claude-sonnet-4-6"``,
            or ``None``. When set, no advisor model is stamped this turn.
        """
        if user_model_override:
            _session_advisor_applied_model.pop(conv, None)
            return
        if result is not None and result.apply_model is not None:
            _apply_advisor_to_body(body, result)
            _session_advisor_applied_model[conv] = result.apply_model
            return
        # No application this turn: keep the brain on the last applied model
        # (if any). The body's own model_override (already advisor-free on
        # this path) still wins if a caller set one.
        sticky = _session_advisor_applied_model.get(conv)
        if sticky is not None and not body.get("model_override"):
            body["model_override"] = sticky

    async def _advisor_spec_for_session(conv: str) -> Any:  # type: ignore[explicit-any]  # resolved AgentSpec or None
        """
        Best-effort spec resolution for the ``stream=true`` advisor run.

        Applies the sub-agent override so a child session plans against
        its own spec, not the parent orchestrator's; resolution failures
        return ``None`` (turn runs unadvised) rather than failing a turn
        for a feature that is dark by default.

        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The resolved spec, or ``None``.
        """
        try:
            spec = _unwrap_resolved_spec(await _resolve_session_spec_entry(conv))
        except (OmnigentError, httpx.HTTPError, RuntimeError):
            _logger.warning(
                "cost_advisor: spec resolution failed for %s; turn runs unadvised",
                conv,
                exc_info=True,
            )
            return None
        _sa_name = _session_sub_agent_names.get(conv)
        if _sa_name and spec is not None:
            from omnigent.runtime.workflow import _find_spec_by_name

            sub_spec = _find_spec_by_name(spec, _sa_name)
            if sub_spec is not None:
                spec = sub_spec
        return spec

    async def _run_turn_bg(
        msg_body: dict[str, Any],
        conv: str,
    ) -> None:
        """
        Run one session turn in the background.

        Resolves the agent spec, builds a ``TurnDispatch`` context
        with harness type / instructions / MCP hint, loads
        conversation history, assembles the harness body with tool
        schemas, and streams the turn via
        ``_stream_message_to_harness``.

        Called from both the initial ``post_session_events`` handler
        and from ``_check_and_start_next_turn`` for continuation
        turns (buffered mid-turn messages).

        :param msg_body: The forwarded message body from the server.
            Should include ``agent_id`` for harness resolution; when it
            doesn't (a message racing ahead of session assignment), the
            agent is resolved on demand from the server snapshot.
        :param conv: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        # This turn is consuming any previously-posted sub-agent wake notice.
        # Clear the debounce at turn start rather than turn end so a child
        # completion that lands while the parent is already reacting can post
        # the next wake. Otherwise a fast child can deliver into the inbox
        # during the stale debounce window and strand the result until the
        # human manually nudges the parent.
        _subagent_wake_pending.discard(conv)
        try:
            await _run_turn_bg_setup_and_stream(msg_body, conv)
        except Exception as exc:
            # Any failure before the harness stream starts (e.g. a provider
            # with no resolvable model raising OmnigentError from
            # ``_build_spawn_env_from_spec``) must still end the turn: clear
            # ``_active_turns`` and publish a terminal ``failed`` status via
            # ``_on_proxy_stream_end``. Without this, the session stays pinned
            # to "running" forever and the REPL spins on "working" with no
            # output (the silent-hang failure mode).
            _logger.error(
                "turn setup failed for %s: %s",
                conv,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})

    async def _run_turn_bg_setup_and_stream(
        msg_body: dict[str, Any],
        conv: str,
    ) -> None:
        """
        Resolve the spec, build the dispatch context, and stream one turn.

        Split out of :func:`_run_turn_bg` so the setup phase (spec
        resolution, spawn-env build, instruction/tool assembly) is covered
        by the same terminal-status guard as the streaming phase. Any
        exception raised here propagates to ``_run_turn_bg``'s handler,
        which clears ``_active_turns`` and publishes a ``failed`` status so
        the client never hangs on a stale "running" turn.

        :param msg_body: The forwarded message body from the server.
        :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        # In-place agent switch (POST /v1/sessions/{id}/switch-agent) rebinds
        # the session to a different agent mid-session. The server forwards the
        # NEW agent_id on the next turn; when it differs from the agent this
        # runner last served for the session, drop every spec-derived
        # per-session cache and tear down the old harness subprocess so the new
        # agent's spec, harness, tools, model, and (for a native target) the
        # freshly cleared external_session_id + carry-history label all take
        # effect below instead of stale values. The session-keyed spec cache is
        # otherwise never invalidated within a session's lifetime.
        _dispatched_agent_id = msg_body.get("agent_id")
        _prior_agent_id = _session_agent_ids.get(conv)
        if (
            _dispatched_agent_id
            and _prior_agent_id is not None
            and _prior_agent_id != _dispatched_agent_id
        ):
            _logger.info(
                "agent switch detected for %s: %s -> %s; resetting session caches",
                conv,
                _prior_agent_id,
                _dispatched_agent_id,
            )
            _session_spec_cache.pop(conv, None)
            _session_skills_cache.pop(conv, None)
            _session_tool_schemas.pop(conv, None)
            # The AP snapshot carries external_session_id + labels, which the
            # switch just changed (cleared id, stamped carry-history); re-fetch.
            _session_snapshot_cache.pop(conv, None)
            if process_manager is not None:
                # Force a cold-start of the new harness: the per-conversation
                # subprocess bakes harness/model/auth/MCP env at spawn time.
                await process_manager.release(conv)
        if _dispatched_agent_id:
            _session_agent_ids[conv] = _dispatched_agent_id

        cached_spec_entry = _session_spec_cache.get(conv)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        cached_spec_workdir = _resolved_spec_workdir(cached_spec_entry)
        if cached_spec is None and spec_resolver is not None:
            _aid = msg_body.get("agent_id")
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
                    )
            else:
                # The forwarded message can race ahead of the session
                # assignment (POST /v1/sessions), arriving with no
                # agent_id before the spec cache is populated. Resolve
                # the agent from the authoritative server snapshot
                # (GET /v1/sessions/{conv}) instead of the turn being
                # silently dropped (first-message race).
                try:
                    cached_spec = await _resolve_session_agent_spec(conv)
                    # _resolve_session_agent_spec returns the unwrapped
                    # spec but caches the ResolvedSpec entry — re-read it
                    # to recover the workdir the unwrap drops.
                    cached_spec_workdir = _resolved_spec_workdir(_session_spec_cache.get(conv))
                except (OmnigentError, httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "On-demand agent resolution failed for %s",
                        conv,
                        exc_info=True,
                    )

        # Sub-agent spec resolution: if this session is a child,
        # find the sub-agent's spec in the parent's spec tree
        # instead of using the root spec directly. This ensures
        # the child gets the sub-agent's prompt/tools, not the
        # parent's (which would cause infinite recursion via
        # sys_session_send).
        #
        # Recover the name from the server snapshot when the in-memory map
        # was lost (runner restart / tunnel reconnect): without this, a
        # continuation turn for a claude-native sub-agent resolves the
        # parent's claude-sdk harness, the process manager respawns, and the
        # child's native terminal is torn down ("Bridge closed: terminal
        # resource not found"). The snapshot carries sub_agent_name; this
        # is the primary turn path (the harness baked into TurnDispatch
        # below comes from the swapped spec, so it must be correct here).
        _sa_name = await _recover_sub_agent_name(conv)
        if _sa_name and cached_spec is not None:
            from omnigent.runtime.workflow import _find_spec_by_name

            sub_spec = _find_spec_by_name(cached_spec, _sa_name)
            if sub_spec is not None:
                cached_spec = sub_spec
                _session_spec_cache[conv] = (
                    ResolvedSpec(spec=cached_spec, workdir=cached_spec_workdir)
                    if cached_spec_workdir is not None
                    else cached_spec
                )

        cached_spec = _spec_with_workdir_paths(cached_spec, cached_spec_workdir)
        if cached_spec is not None:
            _session_spec_cache[conv] = (
                ResolvedSpec(spec=cached_spec, workdir=cached_spec_workdir)
                if cached_spec_workdir is not None
                else cached_spec
            )

        harness_name: str | None = None
        spawn_env: dict[str, str] | None = None
        instructions: str | None = None
        if cached_spec is not None:
            # The per-session harness override (validated at session
            # create, forwarded by the Omnigent server in the message
            # body) replaces the spec's declared brain harness.
            h = (
                msg_body.get("harness_override")
                or cached_spec.executor.config.get("harness")
                or cached_spec.executor.type
            )
            harness_name = canonicalize_harness(h) or h
            spawn_env = _build_spawn_env_from_spec(
                cached_spec,
                harness_name,
                workdir=cached_spec_workdir,
                cwd=await _session_runtime_cwd(conv),
                # Apply the per-session /model override so it actually
                # changes the model on the SDK harnesses (not just the
                # readout). Forwarded by the Omnigent server in the message body.
                model_override=msg_body.get("model_override"),
            )
            from omnigent.runtime.prompt import (
                build_instructions,
            )

            instructions = build_instructions(
                cached_spec,
                None,
                [],
            )

        ctx = TurnDispatch(
            agent_id=msg_body.get("agent_id"),
            harness=harness_name,
            spawn_env=spawn_env,
            has_mcp_servers=(
                (cached_spec is not None and bool(cached_spec.mcp_servers))
                or msg_body.get("has_mcp_servers") is True
            ),
            instructions=instructions,
        )

        if conv not in _session_histories:
            _session_histories[conv] = await _load_history_as_input(conv)

        harness_body: dict[str, Any] = {
            "type": "message",
            "role": "user",
            "model": msg_body.get("model", ""),
        }
        if _session_histories[conv]:
            harness_body["content"] = _session_histories[conv]
        else:
            harness_body["content"] = msg_body.get(
                "content",
                [],
            )
        _content = harness_body.get("content", [])
        _content_summary = []
        for _ci in _content:
            if isinstance(_ci, dict):
                _ct = _ci.get("type", "?")
                if _ct == "message":
                    _blocks = _ci.get("content", [])
                    _block_types = [b.get("type") for b in _blocks if isinstance(b, dict)]
                    _content_summary.append(f"msg({_ci.get('role', '?')}, blocks={_block_types})")
                else:
                    _content_summary.append(_ct)
        _logger.info(
            "_run_turn_bg: conv=%s history_msgs=%d content_summary=%s",
            conv,
            len(_content),
            _content_summary[:20],
        )

        # Cost advisor (dark by default): judge this turn's difficulty,
        # persist the cost_control.plan verdict label, and — optimize mode
        # on a claude-sdk brain with no user pin — run the brain on the
        # verdict model this turn and inject the one-line note. No-op
        # unless executor.config.cost_optimize is set.
        _advisor_result = await _run_turn_advisor(msg_body, conv, cached_spec)
        # Announce the router's pick at turn start (display-only chip), before
        # any harness output — independent of the apply/sticky logic below.
        _emit_routing_decision(conv, _advisor_result)
        # harness_body is rebuilt without the inbound model_override, so the
        # user pin must be passed explicitly or the sticky stamp beats it.
        _apply_advisor_for_turn(
            harness_body, conv, _advisor_result, msg_body.get("model_override")
        )

        if instructions:
            harness_body["instructions"] = instructions

        if conv not in _session_tool_schemas:
            all_tools: list[dict[str, Any]] = []
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
            _session_tool_schemas[conv] = all_tools

        # MCP schemas are re-resolved only when the spec's MCP server
        # list changes (tracked via a content hash). This avoids a
        # tools/list round-trip on every turn while still picking up
        # servers added/removed via the Agent Info UI immediately.
        if cached_spec and cached_spec.mcp_servers:
            from omnigent.runner.mcp_manager import compute_spec_hash

            _mcp_hash = compute_spec_hash(list(cached_spec.mcp_servers))
            if _mcp_hash != _session_mcp_spec_hash.get(conv):
                _session_mcp_proxy: Any = ProxyMcpManager(conv, server_client)
                try:
                    mcp_result = await _session_mcp_proxy.schemas_for(
                        cached_spec,
                    )
                    # Replace MCP tools in the cached list: keep builtin
                    # tools (no double-underscore separator) and append
                    # the fresh MCP schemas.
                    _builtin_tools = [
                        t
                        for t in _session_tool_schemas.get(conv, [])
                        if not (isinstance(t, dict) and "__" in (t.get("name") or ""))
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
                    )

        # Spec builtin + MCP schemas are cached per conversation, but the
        # caller's client-side tools arrive per event on ``msg_body["tools"]``
        # — merge them in so non-native harnesses see ``request.tools`` and
        # the model can emit (and tunnel) client-side tool calls.
        _spec_tools = _session_tool_schemas.get(conv) or []
        _client_tools = msg_body.get("tools") or []
        merged_tools = _merge_request_client_tools(_spec_tools, _client_tools)
        if merged_tools:
            harness_body["tools"] = merged_tools
        # Record which tools are client-side (request-supplied and not part
        # of the spec's builtin/MCP/local surface) so the proxy_stream relays
        # their action_required events upstream to tunnel — rather than
        # dispatching them locally, which would error "not in local dispatch
        # table". A request tool that collides with a spec tool name is NOT
        # client-side: the builtin wins (see _merge_request_client_tools).
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

        # Fallback for native sessions whose terminal was launched
        # outside the runner terminal route (e.g. tests, UI-launched
        # terminals): make sure the comment-tool relay is running before the
        # user message is injected. The normal ``omnigent claude`` /
        # ``omnigent codex`` path already started it at terminal launch, in
        # which case this is a no-op. ``await_notify=False``: a UI-launched
        # terminal is never pre-warmed, so on its first turn Claude Code's MCP
        # bridge has not published ``server.json`` yet and awaiting the
        # tools/list_changed delivery would stall the turn ~15s on
        # ``post_tools_changed``'s readiness poll. ``tool_relay.json`` is
        # already on disk synchronously, so fire the notification in the
        # background instead — the relay tools land a beat later, which is
        # harmless on the first turn (nobody reads comments before sending).
        if harness_name == "claude-native":
            await _ensure_comment_relay_started(conv, await_notify=False)
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
            # Fallback for sessions not started via _auto_create_codex_terminal
            # (which already started the relay). await_notify=False: codex's MCP
            # bridge is lazy, so awaiting would stall the turn (see the
            # _ensure_comment_relay_started docstring).
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
            # Fallback for sessions not started via _auto_create_antigravity_terminal
            # (which already started the relay + wrote agy's isolated-HOME mcp_config).
            # await_notify=False: agy starts its MCP client lazily, so awaiting would
            # stall the turn (see the _ensure_comment_relay_started docstring).
            await _ensure_comment_relay_started(
                conv, explicit_bridge_dir=antigravity_bdir, await_notify=False
            )

        try:
            response = await _stream_message_to_harness(
                harness_body,
                conv,
                dispatch=ctx,
            )
            if isinstance(response, StreamingResponse):
                await _drain_streaming_response(response, conv)
            else:
                err_detail = "harness returned error response"
                if hasattr(response, "body"):
                    with contextlib.suppress(
                        UnicodeDecodeError,
                        AttributeError,
                    ):
                        err_detail = response.body.decode(
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
        except _ContextWindowOverflow as overflow:
            _logger.error(
                "Context window exceeded for session=%s: %d > %d",
                conv,
                overflow.actual_tokens,
                overflow.max_tokens,
            )
            _on_proxy_stream_end(
                conv,
                error={
                    "message": (
                        f"Context window exceeded: {overflow.actual_tokens} tokens "
                        f"> {overflow.max_tokens} max"
                    ),
                },
            )

    async def _drain_streaming_response(
        response: StreamingResponse,
        session_id: str,
    ) -> None:
        """
        Consume a background turn's ``StreamingResponse`` to completion.

        The ``proxy_stream`` generator publishes events to
        ``session_stream`` as it runs; the bytes themselves are
        discarded since there is no HTTP client to receive them.
        Turn-end bookkeeping is handled by ``proxy_stream`` calling
        ``_on_proxy_stream_end`` at its completion points.

        :param response: The ``StreamingResponse`` wrapping
            ``proxy_stream()``.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        try:
            async for _chunk in response.body_iterator:
                pass
        except asyncio.CancelledError:
            # Publish terminal status so the client doesn't sit on stale "running".
            # This teardown bypasses _on_proxy_stream_end, so clear the live
            # marker here too or the next turn's forward gate goes stale.
            _active_turns.pop(session_id, None)
            _live_response_id.pop(session_id, None)
            _publish_turn_status(session_id, "idle")
            raise
        except _ContextWindowOverflow:
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
        body: dict[str, Any],
        conv_id: str,
        dispatch: TurnDispatch | None = None,
    ) -> Any:
        """Stream one session message through the runner-owned harness.

        :param body: The harness message body — only fields the
            harness needs (type, role, content, model). No
            runner-only metadata.
        :param conv_id: Conversation/session identifier.
        :param dispatch: Runner dispatch context. When provided,
            used for harness resolution, MCP injection, and
            system prompt. When ``None`` (legacy callers), these
            are read from ``body`` for backward compatibility.
        """
        # Read dispatch context — prefer TurnDispatch, fall back
        # to body fields for legacy callers.
        harness_name = dispatch.harness if dispatch else body.get("harness")
        spawn_env = dispatch.spawn_env if dispatch else body.get("spawn_env")
        if not harness_name:
            _agent_id = dispatch.agent_id if dispatch else body.get("agent_id")
            # Recover the sub-agent name (server snapshot if the in-memory
            # map was lost on reconnect) so a child session resolves its OWN
            # harness, not the parent's. Without this a continuation turn for
            # a claude-native sub-agent resolves the parent claude-sdk harness
            # and respawns, killing the native terminal ("Bridge closed").
            _sub_agent_name = await _recover_sub_agent_name(conv_id)
            try:
                harness_name, spawn_env = await _resolve_harness_config(
                    agent_id=_agent_id,
                    spec_resolver=spec_resolver,
                    session_id=conv_id,
                    model_override=body.get("model_override"),
                    harness_override=body.get("harness_override"),
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
        if harness_name == "claude-native" and spawn_env is None:
            from omnigent.claude_native_bridge import build_claude_native_spawn_env

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=conv_id,
            )
            spawn_env = build_claude_native_spawn_env(conv_id, bridge_id=bridge_id)
        if harness_name == "codex-native" and spawn_env is None:
            from omnigent.codex_native_bridge import (
                CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
                build_codex_native_spawn_env,
            )

            labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv_id,
            )
            bridge_id = labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY)
            spawn_env = build_codex_native_spawn_env(conv_id, bridge_id=bridge_id)
        if harness_name == "pi-native" and spawn_env is None:
            from omnigent.pi_native_bridge import build_pi_native_spawn_env

            spawn_env = build_pi_native_spawn_env(conv_id)
        if harness_name == "opencode-native" and spawn_env is None:
            from omnigent.opencode_native_bridge import (
                OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
                build_opencode_native_spawn_env,
            )

            labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv_id,
            )
            bridge_id = labels.get(OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY)
            spawn_env = build_opencode_native_spawn_env(conv_id, bridge_id=bridge_id)
        if harness_name == "cursor-native" and spawn_env is None:
            from omnigent.cursor_native_bridge import build_cursor_native_spawn_env

            spawn_env = build_cursor_native_spawn_env(conv_id)
        if harness_name == "kiro-native" and spawn_env is None:
            from omnigent.kiro_native_bridge import build_kiro_native_spawn_env

            spawn_env = build_kiro_native_spawn_env(conv_id)
        if harness_name == "antigravity-native" and spawn_env is None:
            from omnigent.antigravity_native_bridge import (
                ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
                build_antigravity_native_spawn_env,
            )

            labels = await _session_labels_for_runner_spawn(
                server_client=server_client,
                session_id=conv_id,
            )
            antigravity_bridge_id = labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY)
            spawn_env = build_antigravity_native_spawn_env(
                conv_id, bridge_id=antigravity_bridge_id
            )
        if harness_name == "goose-native" and spawn_env is None:
            from omnigent.goose_native_bridge import build_goose_native_spawn_env

            spawn_env = build_goose_native_spawn_env(conv_id)
        if harness_name == "hermes-native" and spawn_env is None:
            from omnigent.hermes_native_bridge import build_hermes_native_spawn_env

            spawn_env = build_hermes_native_spawn_env(conv_id)
        if harness_name == "qwen-native" and spawn_env is None:
            from omnigent.qwen_native_bridge import build_qwen_native_spawn_env

            spawn_env = build_qwen_native_spawn_env(conv_id)
        if harness_name == "kimi-native" and spawn_env is None:
            from omnigent.kimi_native_bridge import build_kimi_native_spawn_env

            spawn_env = build_kimi_native_spawn_env(conv_id)

        agent_version = dispatch.agent_version if dispatch else body.get("agent_version")
        if agent_version is not None and conv_id in _version_cache:
            if agent_version > _version_cache[conv_id]:
                await process_manager.release(conv_id)
        if agent_version is not None:
            _version_cache[conv_id] = agent_version

        try:
            client = await process_manager.get_client(conv_id, harness_name, env=spawn_env)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _turn_agent_id = dispatch.agent_id if dispatch else body.get("agent_id")
        _has_mcp_hint = dispatch.has_mcp_servers if dispatch else body.get("has_mcp_servers")
        _turn_spec: Any = None
        _turn_spec_entry: Any = None
        _turn_spec_resolved = False
        _mcp_schemas: list[dict[str, Any]] = []
        _mcp_tool_names: set[str] = set()
        _eager_spec_error: tuple[str, str] | None = None
        if _has_mcp_hint is True and _turn_agent_id:
            # Check both spec caches: agent-keyed (MCP path) and
            # session-keyed (session creation path).
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
                    # Keep the exception class (a safe, generic label) for the
                    # client; log the full cause for operators. The raw message
                    # can embed internal hosts/paths, so it stays out of the
                    # streamed failure event.
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
                    if _turn_spec is not None:
                        _spec_cache[_turn_agent_id] = _resolved_turn_spec
                        _turn_spec_entry = _resolved_turn_spec
            _turn_spec_resolved = True
            _turn_mcp: Any = ProxyMcpManager(conv_id, server_client)
            if _eager_spec_error is None and _turn_spec is not None:
                try:
                    _mcp = await _turn_mcp.schemas_for(_turn_spec)
                    _mcp_schemas = _mcp.schemas
                    _mcp_tool_names = _mcp.tool_names
                    for _srv, _err in _mcp.failures.items():
                        _logger.warning("runner MCP %r unavailable for this turn: %s", _srv, _err)
                except Exception:
                    _logger.exception("runner mcp_manager.schemas_for failed")

        async def _resolve_turn_spec_lazy() -> tuple[Any, tuple[str, str] | None]:
            """Resolve spec on demand for non-eager (non-MCP) turns.

            Returns ``(spec, None)`` on success or ``(None, (type, msg))``
            on resolver failure. Caller decides how to surface the error
            (typically ``_response_failed_event`` from inside the SSE
            generator).
            """
            nonlocal _turn_spec, _turn_spec_entry, _turn_spec_resolved
            if _turn_spec_resolved:
                return _turn_spec_entry or _turn_spec, None
            _turn_spec_resolved = True
            # Session-level cache has the sub-agent's resolved spec
            # (set by _run_turn_bg) for child sessions. Check it
            # first so sub-agent turns dispatch tools against the
            # sub-spec, not the root spec.
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

        async def proxy_stream():
            # If eager spec resolution failed (MCP path), emit the
            # SSE failure now — the harness was never POSTed so no
            # response.created was produced.
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

                    # Relay every SSE frame upstream. For
                    # action_required tool calls that match the
                    # local dispatch table, the runner executes
                    # the tool and PATCHes the harness — the
                    # harness then emits a function_call_output
                    # that flows through here for the executor's
                    # pairing buffer. The action_required event
                    # itself is STILL relayed so the executor
                    # emits ToolCallInProgress for REPL rendering
                    # (the executor skips its own dispatch when
                    # handles_tool_dispatch is set on the process
                    # manager).
                    _response_id: str | None = None
                    _omnigent_task_id: str | None = body.get("task_id")
                    _buffer = ""
                    _dispatch_tasks: list[_asyncio.Task[str]] = []
                    _text_acc: list[str] = []
                    # Last failure seen in the harness stream. Threaded into
                    # _on_proxy_stream_end so a turn that ends after a
                    # response.failed publishes session.status "failed", not
                    # "idle". Critical for codex-native: "idle" is suppressed
                    # there (the app-server forwarder owns it), so without
                    # this the client's working indicator never clears.
                    _stream_failed_error: dict[str, Any] | None = None
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

                            if event is not None:
                                if event.get("type") == "response.created":
                                    resp_obj = event.get("response") or {}
                                    _response_id = resp_obj.get("id")
                                    if _response_id and conv_id:
                                        _resp_to_conv[_response_id] = conv_id
                                        # Mark the turn live for the forward gate.
                                        _live_response_id[conv_id] = _response_id

                                # Defer publish for action_required
                                # events that the runner dispatches
                                # locally — publishing before dispatch
                                # would leak the action_required to the
                                # client before the runner can handle it.
                                _defer_publish = False

                                # Detect context-window overflow from
                                # the harness. Raises so the caller
                                # can end the turn with a descriptive error.
                                _overflow = _is_context_overflow_error(event)
                                if _overflow is not None:
                                    raise _ContextWindowOverflow(*_overflow)

                                # Build in-memory history from
                                # SSE events: text deltas, tool
                                # calls, and tool results.
                                _evt_type = event.get("type")
                                if _evt_type == "injection.consumed":
                                    # Runner-internal exactly-once marker
                                    # (RUNNER_MESSAGE_INGEST.md Part B): the
                                    # harness consumed this mid-turn
                                    # injection into the live turn. Drop the
                                    # buffered copy so it does not also drive
                                    # a continuation turn, and record it in
                                    # history once (the live turn — not a
                                    # continuation — is where it reached the
                                    # LLM). Never published to the client or
                                    # relayed upstream.
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
                                    # A completion supersedes any earlier
                                    # in-stream failure — the turn ended
                                    # successfully, so the stream end must
                                    # publish "idle", not "failed".
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
                                    # Remember the failure so the stream-end
                                    # bookkeeping publishes a terminal
                                    # "failed" status. The frame itself is
                                    # still relayed/published below — this
                                    # only captures the error payload.
                                    _err = event.get("error") or (event.get("response") or {}).get(
                                        "error"
                                    )
                                    _stream_failed_error = (
                                        _err
                                        if isinstance(_err, dict)
                                        # Scaffolds always attach an error
                                        # dict; this fallback only covers a
                                        # malformed frame so the terminal
                                        # edge still carries a message.
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
                                    # A harness compacted its internal
                                    # context and is notifying the runner
                                    # so the compaction can be persisted.
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
                                        # The cheap _session_spec_cache lookup above
                                        # can miss for bundle-deployed agents with NO
                                        # MCP servers (the eager MCP path never resolved
                                        # their spec), leaving a native python tool
                                        # mis-classified as client-side. Resolve once to
                                        # recompute _is_spec_local so it dispatches
                                        # locally against the bundle workdir.
                                        #
                                        # A hint-only resolution failure is NON-fatal:
                                        # we keep the prior _is_spec_local=False and let
                                        # the tool relay to the client exactly as base
                                        # did, rather than aborting the turn with
                                        # response.failed. Only the real dispatch path
                                        # below surfaces resolver errors fatally, since
                                        # it genuinely needs the spec to dispatch.
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
                                        # Lazy spec resolution for non-eager
                                        # (non-MCP) paths. spec_resolver
                                        # failures surface as response.failed
                                        # SSE (see the response.failed contract).
                                        (
                                            _spec_for_dispatch_entry,
                                            _lazy_err,
                                        ) = await _resolve_turn_spec_lazy()
                                        if _lazy_err is not None:
                                            _err_type, _err_msg = _lazy_err
                                            yield _response_failed_event(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            return
                                        # Bundle-deployed agents carry their own
                                        # workdir for spec-local native python
                                        # tools (where tools/python/*.py live).
                                        # Builtins / OS-env / relayed tools must
                                        # keep the caller's runner workspace.
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
                                        # All tool calls go through AP:/mcp
                                        # (ProxyMcpManager in Omnigent mode), which
                                        # enforces TOOL_CALL + TOOL_RESULT
                                        # policies server-side before forwarding
                                        # to the runner's /mcp/execute.
                                        event[_RUNNER_DISPATCHED_FIELD] = True
                                        raw_sse_bytes = _encode_sse_event(event)
                                        _agent_id_for_dispatch = body.get("agent_id")
                                        _dispatch_mcp: Any = ProxyMcpManager(
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
                                                    agent_name=body.get("model"),
                                                    runner_workspace=_dispatch_workdir,
                                                    mcp_manager=_dispatch_mcp,
                                                    session_inbox=_session_inboxes.get(conv_id),
                                                    session_async_tasks=_session_async_tasks.get(
                                                        conv_id
                                                    ),
                                                    publish_event=_publish_event,
                                                    filesystem_registry=filesystem_registry,
                                                )
                                            )
                                        )

                                # ── Policy evaluation round-trip ──
                                # The harness emits this when the inner
                                # executor is about to make (or just made)
                                # an LLM call and needs an LLM_REQUEST /
                                # LLM_RESPONSE policy verdict. The runner
                                # proxies the request to the Omnigent server's
                                # evaluate endpoint and posts the verdict
                                # back to the harness as a policy_verdict
                                # inbound event. The SSE frame is consumed
                                # here — never relayed to clients.
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
                                    # Don't relay or publish — runner-internal.
                                    continue

                            # Publish to session stream if not deferred
                            # by the dispatch path above. Suppress
                            # response.created — the sessions path
                            # does not use response_id.
                            if not _defer_publish and event.get("type") != "response.created":
                                _publish_event(conv_id, event)
                            # In sessions-native mode (dispatch is set),
                            # don't relay runner-dispatched action_required
                            # events — the client would try to handle them
                            # as client-side tools. In legacy mode
                            # (dispatch is None), the server-side executor
                            # needs to see the marker to skip its own
                            # dispatch.
                            if dispatch is not None and event.get(_RUNNER_DISPATCHED_FIELD):
                                pass
                            else:
                                yield raw_sse_bytes

                    if _dispatch_tasks:
                        await _asyncio.gather(*_dispatch_tasks, return_exceptions=True)

                    _on_proxy_stream_end(conv_id, error=_stream_failed_error)

            except (httpx.HTTPError, RuntimeError) as exc:
                # RuntimeError covers httpx.StreamClosed which
                # is NOT an HTTPError subclass — raised when the
                # harness subprocess dies mid-stream. Surface the
                # proxy-stream break as the same retryable code the
                # direct harness client uses for transport drops so
                # the AP-side L2 retry classifier can respawn the
                # harness and retry the turn.
                #
                # The retry classifier keys on ``code``/``type`` (not the
                # human message), so the message is a fixed, client-safe
                # string; the raw cause (which can embed the harness socket
                # path/host) is logged for operators only.
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
    ) -> Any:
        """
        Inbound surface for the Omnigent server's post-migration session
        event wire path, ``POST /v1/sessions/{conv}/events``.

        Bodies arrive in the harness's discriminated-union shape
        (``MessageEvent`` / ``InterruptEvent`` / ``ToolResultEvent``
        / ``ApprovalEvent``) — see
        :class:`omnigent.runtime.harnesses._scaffold.InboundEventRequest`.
        The runner inspects the discriminator and dispatches:

        * ``message`` (default) with ``stream=false``: starts a
          background turn task and returns 202; events flow
          through ``GET /v1/sessions/{conv}/stream``.
        * ``message`` with ``stream=true``: returns a
          :class:`StreamingResponse` whose body IS the SSE event
          stream. Used by the harness HTTP client which consumes
          the SSE body synchronously for the ``response.created``
          → dispatch → pairing buffer flow.
        * ``interrupt`` / ``tool_result`` / ``approval``: control
          events forwarded to the harness verbatim. ``stream``
          is ignored for these types.

        :param conversation_id: AP-allocated conversation id from
            the URL path, e.g. ``"conv_abc123"``.
        :param request: The FastAPI request; we read its JSON body
            for type-discriminated dispatch.
        :param stream: When ``True`` and ``type == "message"``,
            return a streaming SSE response instead of 202.
            Defaults to ``False``.
        :returns: Either 202 JSON (fire-and-forget), a
            :class:`StreamingResponse` (``stream=true``), or the
            forwarded harness response (control events). 501 when
            no :class:`HarnessProcessManager` is wired up.
        """
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
        # ``message`` (and absent discriminator) → streaming path with
        # MCP schema injection + action_required intercept.
        # Other discriminators → forward verbatim as control events.
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

            # A new message means a turn is (about to be) in flight. Mark the
            # native session running now so a pane crash before the PTY
            # watcher's first ``running`` edge isn't misread as a clean
            # shutdown against the prior turn's stale ``idle`` memo.
            if _is_native_harness(conversation_id):
                resource_registry.note_session_turn_started(conversation_id)

            # Take an arrival slot, then wait at the FIFO gate so this
            # conversation's messages reach the turn-vs-buffer decision in
            # arrival order regardless of content-resolution latency
            # (RUNNER_MESSAGE_INGEST.md Part A). The sequence is
            # read-incremented synchronously here, before any await, so it
            # reflects arrival order. Content resolution + the decision then
            # run inside the served slot, serialized per conversation.
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

                # Turn sequencing gate (invariant I2: single active turn).
                if conversation_id in _active_turns:
                    _native = _is_native_harness(conversation_id)
                    # A turn parked on a human approval must not be steered
                    # past its gate by an incoming message. The non-native
                    # mid-turn injection forward below would do exactly that:
                    # a parent agent's ``sys_session_send`` to a child blocked
                    # on an elicitation would reach the parked turn as a steer
                    # and let it advance — the parent jumping a human gate it
                    # has no business resolving. While an approval is
                    # outstanding we therefore buffer the message WITHOUT
                    # forwarding it; it rides the post-turn continuation drain
                    # after the human delivers a verdict (accept/decline/
                    # timeout), so nothing is lost and only a real ``approval``
                    # event advances the gate. Applies to human-sent messages
                    # too — you can't jump the gate, but your message waits
                    # rather than being dropped.
                    _awaiting_approval = pending_approvals.has_pending(conversation_id)
                    # Stamp a correlation id so the buffered copy and the
                    # forwarded injection share an id. When the harness
                    # consumes the injection it echoes this id back in an
                    # ``injection.consumed`` marker, and the proxy_stream
                    # relay drops the matching buffered copy — so a consumed
                    # message is delivered exactly once and never also
                    # drives a continuation turn (RUNNER_MESSAGE_INGEST.md
                    # Part B). Native harnesses skip the forward entirely
                    # (Part C), so they don't need a correlation id; neither
                    # does a buffer-only park (no forward will be made).
                    # Forward as a live injection only when a turn is actually
                    # streaming; otherwise it would start a rogue turn (→ 204).
                    # The buffered copy still drives the post-turn continuation.
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
                    # Mid-turn injection: forward the message to the
                    # harness so the SDK sees it at the next breakpoint
                    # in its tool loop (via the scaffold's injection
                    # queue → executor adapter → enqueue_session_message).
                    # Best-effort — a failed forward means the LLM sees
                    # the message on the next turn instead of mid-chain.
                    #
                    # SKIPPED for native harnesses (Part C): their turns are
                    # instant, so the forward's injection races the turn's
                    # teardown (``_watch_injections`` is cancelled when
                    # ``run_turn`` returns) — the message is then either
                    # never typed or typed by a stray new turn. Native
                    # sessions deliver every message through the
                    # one-at-a-time continuation drain below instead.
                    #
                    # SKIPPED while an approval is parked (``_awaiting_approval``):
                    # forwarding would steer the gated turn past a human
                    # approval (see the buffer-only rationale above).
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

                # Make the new user message visible to the turn. On the
                # first touch of a conversation after a runner restart the
                # in-memory cache is empty; seeding it with ONLY this
                # message (the old ``setdefault(conv, []).append(...)``)
                # dropped all prior context — the harness then ran the
                # turn with no history. The claude-sdk harness makes this
                # acute: on a cold session (no live SDK client) it replays
                # the in-memory history verbatim as the prompt, so a
                # one-message cache erases the whole conversation.
                new_item = {
                    "type": "message",
                    "role": message_body.get("role", "user"),
                    "content": message_body.get("content", []),
                }
                if conversation_id in _session_histories:
                    # Warm cache: append the new message as before.
                    _session_histories[conversation_id].append(new_item)
                else:
                    # Cold cache (e.g. the first message after a runner
                    # restart): rehydrate the full prior history from the
                    # store so the turn keeps prior context instead of
                    # running with only this message.
                    #
                    # The just-posted message may already be persisted in the
                    # store (invariant I1, omnigent/server/routes/sessions.py:
                    # persist-before-forward), but in its PRE-resolution body
                    # (e.g. ``file_id`` blocks the runner has since resolved to
                    # ``image_url`` / ``file_data``) — so that reloaded copy
                    # must not be forwarded to a harness. The server hands us
                    # the id of the item it persisted for this turn; drop that
                    # exact item from the reload and append the runner-resolved
                    # ``new_item``. Dedup is by identity, not a role/content
                    # guess (content can't be matched once media is resolved).
                    # Native-terminal forwards skip persist-before-forward and
                    # omit ``persisted_item_id``, so nothing is dropped and the
                    # message is simply appended — never lost, never doubled,
                    # never left unresolved.
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
                    # Streaming mode: return the SSE body synchronously
                    # so the executor can consume response.created,
                    # dispatch tool calls, and pair results inline.
                    # Advisor parity with _run_turn_bg: without it, opted-in
                    # streaming turns would never judge, record, or apply a
                    # per-turn brain-model verdict.
                    _stream_advisor_result = await _run_turn_advisor(
                        message_body,
                        conversation_id,
                        await _advisor_spec_for_session(conversation_id),
                    )
                    # Announce the router's pick at turn start (display-only
                    # chip), before any harness output — same as _run_turn_bg.
                    _emit_routing_decision(conversation_id, _stream_advisor_result)
                    # Copy-on-write: the per-turn model override + note must
                    # not mutate the caller's body or the cached history.
                    message_body = dict(message_body)
                    _apply_advisor_for_turn(
                        message_body,
                        conversation_id,
                        _stream_advisor_result,
                        message_body.get("model_override"),
                    )
                    response = await _stream_message_to_harness(message_body, conversation_id)
                    if not isinstance(response, StreamingResponse):
                        _on_proxy_stream_end(
                            conversation_id,
                            error={"message": "harness returned error response"},
                        )
                    return response

                # Fire-and-forget mode: start the turn as a background
                # task. Events flow through GET /stream, not the POST
                # response body. Return 202 immediately.
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
                # Advance the gate so the next-arriving message for this
                # conversation proceeds — even if this one raised, so a
                # failed resolve/decision can't stall later messages.
                async with _cond:
                    _ingest_now_serving[conversation_id] = _seq + 1
                    _cond.notify_all()

        if body_type == "interrupt":
            # Native harnesses get a key sent to their TUI pane — a forwarded
            # InterruptEvent 404s at the scaffold (the instant turn already
            # returned). Each native handler returns; in-process LLM harnesses
            # go through the cancel floor below.
            _harness = _session_harness_name(conversation_id)
            if _harness == "claude-native":
                return await _handle_claude_native_interrupt(conversation_id)
            if _harness == "codex-native":
                return await _handle_codex_native_interrupt(conversation_id)
            if _harness == "pi-native":
                # The pi-native turn lives in the Pi TUI process; the runner's
                # harness task already returned, so the cancel floor has nothing
                # to cancel. Queue an abort to the resident extension instead.
                return await _handle_pi_native_interrupt(conversation_id)
            if _harness == "cursor-native":
                # cursor turn lives in the cursor-agent TUI; send Escape to stop it.
                return await _handle_cursor_native_interrupt(conversation_id)
            if _harness == "goose-native":
                # goose turn lives in the goose session TUI; send Escape to stop it.
                return await _handle_goose_native_interrupt(conversation_id)
            if _harness == "hermes-native":
                # hermes turn lives in the hermes TUI; send Escape to stop it.
                return await _handle_hermes_native_interrupt(conversation_id)
            if _harness == "qwen-native":
                # qwen turn lives in the qwen TUI; send Escape to stop it.
                return await _handle_qwen_native_interrupt(conversation_id)
            if _harness == "kimi-native":
                # kimi turn lives in the kimi TUI; send Escape to stop it.
                return await _handle_kimi_native_interrupt(conversation_id)
            # In-process harness: mark interrupted, forward an interrupt to the
            # harness, and force-cancel the runner turn task so the turn ends
            # promptly even if the harness can't honor the interrupt in time.
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "external_session_status":
            data = body.get("data") if isinstance(body, dict) else None
            status = data.get("status") if isinstance(data, dict) else None
            forwarded_output = data.get("output") if isinstance(data, dict) else None
            output = forwarded_output if isinstance(forwarded_output, str) else None
            delivery_ack: _SubagentDeliveryAck | None = None
            # Keep this allowlist in sync with Omnigent server's
            # ``_EXTERNAL_SESSION_STATUS_VALUES``. These events are produced by
            # native terminal forwarders, so AP-forwarded output is the only
            # authoritative transcript source.
            if status in ("running", "waiting", "idle", "failed"):
                resource_registry.note_external_session_status(conversation_id, status)
                _fan_out_child_delta_to_parent(
                    conversation_id,
                    {"type": "session.status", "status": status},
                    latest_assistant_text=output,
                    allow_history_preview_fallback=False,
                )
            if status == "idle":
                # Native transcripts are owned by AP. If Omnigent did not forward
                # output for this idle edge, deliver an explicit empty result
                # rather than inventing content from stale runner history.
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
                not_confirmed = _subagent_delivery_not_confirmed_response(
                    delivery_ack,
                    is_runner_known_subagent=conversation_id in _session_sub_agent_names,
                )
                if not_confirmed is not None:
                    return not_confirmed
            return Response(status_code=204)

        if body_type == "stop_session":
            # Omnigent server forwards a "stop session" request here. Native harnesses
            # have a live external process: claude-native hard-kills its tmux
            # pane; codex-native asks Codex app-server to interrupt the active
            # turn (same as interrupt).
            # Routing codex-native through the in-process floor would synthesize
            # a [System: interrupted] marker Codex never emits, desyncing the web
            # mirror from Codex's own session. In-process harnesses run their
            # turn in the runner, so stop = cancel the in-flight turn via the
            # same floor as interrupt (this used to 204 no-op, so the sidebar
            # Stop did nothing for them).
            _harness = _session_harness_name(conversation_id)
            if _harness == "claude-native":
                return await _handle_claude_native_stop(conversation_id)
            if _harness == "codex-native":
                return await _handle_codex_native_interrupt(conversation_id)
            if _harness == "pi-native":
                # Pi has no separate session-kill; abort the active turn via the
                # extension (mirrors codex-native reusing its interrupt handler).
                return await _handle_pi_native_interrupt(conversation_id)
            if _harness == "cursor-native":
                # Hard-kill the cursor-agent tmux pane (the TUI is the runtime).
                return await _handle_cursor_native_stop(conversation_id)
            if _harness == "goose-native":
                # Hard-kill the goose session tmux pane (the TUI is the runtime).
                return await _handle_goose_native_stop(conversation_id)
            if _harness == "hermes-native":
                # Hard-kill the hermes tmux pane (the TUI is the runtime).
                return await _handle_hermes_native_stop(conversation_id)
            if _harness == "qwen-native":
                # Hard-kill the qwen tmux pane (the TUI is the runtime).
                return await _handle_qwen_native_stop(conversation_id)
            if _harness == "kimi-native":
                # Hard-kill the kimi tmux pane (the TUI is the runtime).
                return await _handle_kimi_native_stop(conversation_id)
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "effort_change":
            # Omnigent server forwards the persisted reasoning_effort here
            # so harnesses that can't re-read it from store at turn
            # boundaries can propagate it live. Claude-native injects a
            # slash command into its terminal; codex-native queues a
            # Codex app-server next-turn settings update. cursor-native is
            # intentionally absent: its effort lives on the /model picker's
            # per-model "Tab to modify" axis, and switching the model resets it
            # to that model's default — so a web effort would silently diverge
            # from the TUI. cursor-native supports model switching only; effort
            # control is dropped pending a model-switch-resets-effort fix. Other
            # harnesses pick up the persisted value on the next turn and 204 here.
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
            # Omnigent server forwards the persisted model_override here so
            # harnesses that can't re-read it from store at turn
            # boundaries can propagate it live. Claude-native and
            # cursor-native type ``/model`` into their tmux pane;
            # codex-native queues a Codex app-server next-turn settings
            # update. Other harnesses pick up the persisted value on the
            # next turn and 204 here.
            harness = _session_harness_name(conversation_id)
            if harness in ("claude-native", "codex-native", "cursor-native"):
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
                return await _handle_claude_native_model_change(
                    conversation_id,
                    model,
                )
            return Response(status_code=204)

        if body_type == "plan_mode_change":
            # Codex-native exposes Plan/Default as a structured app-server
            # collaboration mode, not a terminal slash-command. Other
            # harnesses have no equivalent runtime control and 204 no-op.
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

        if body_type == "compact":
            # Omnigent server forwards explicit /compact here. claude-native
            # and codex-native inject the slash command into the tmux
            # pane so the CLI compacts its own context, and return 200
            # to signal the control was handled in the terminal. Other
            # harnesses 204 no-op — their explicit compaction is an
            # AP-side operation the server runs when the runner does
            # not handle the control (see ``_run_compact_locked``).
            if _session_harness_name(conversation_id) == "claude-native":
                return await _handle_claude_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "codex-native":
                return await _handle_codex_native_compact(conversation_id)
            if _session_harness_name(conversation_id) == "cursor-native":
                return await _handle_cursor_native_compact(conversation_id)
            return Response(status_code=204)

        if body_type == "cost_approval_popup":
            # Omnigent server forwards a cost-budget checkpoint here so it can
            # be answered from the native terminal (a tmux display-popup),
            # not only the web ApprovalCard. The popup resolves the SAME
            # elicitation via the resolve endpoint the web card uses, so
            # whichever surface answers first wins. claude-native and
            # codex-native each pop the modal on their pane (different
            # tmux/AP-config sources, shared launcher); other harnesses
            # 204 no-op (the web card is their only surface).
            elicitation_id = body.get("elicitation_id") if isinstance(body, dict) else None
            message = body.get("message") if isinstance(body, dict) else None
            policy_name = body.get("policy_name") if isinstance(body, dict) else None
            # ``elicitation_id`` is the functional resolve key — reject the
            # event if it's missing. ``message`` is display-only (the modal
            # body) and is always set by the Omnigent server forwarder; fall back
            # to a generic label rather than dropping the (still-answerable)
            # popup if a future caller omits it. ``policy_name`` is the
            # display-only modal header and is optional (a generic header is
            # used when absent).
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
            return Response(status_code=204)

        # Resolve pending policy approval Futures.
        if body_type == "approval":
            _data = body.get("data") or body
            pending_approvals.resolve(
                _data.get("elicitation_id", ""), _data.get("action") == "accept"
            )
            # The server wraps the verdict as ``{"type": "approval", "data": {…}}``,
            # but the harness scaffold's ``ApprovalEvent`` wants the fields at the
            # top level — forwarding the envelope verbatim 422s and hangs the turn.
            # Unwrap ``data`` to the top level (robust to added/renamed fields —
            # the model ignores extras) and keep the discriminator.
            body = {**_data, "type": "approval"}

        # Control event (interrupt / tool_result / approval): get a
        # harness client for this conversation and POST the body
        # verbatim. ``get_client(... "any")`` matches the steering
        # branch in :func:`post_responses` — the runner doesn't need
        # to know the harness name for an already-spawned subprocess;
        # only spawning a fresh one does.
        try:
            harness_client = await process_manager.get_client(conversation_id, "any")
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
            # Best-effort: the harness subprocess may have already
            # exited (race with natural turn completion) or the
            # forward may have failed transport-side. Surface as
            # 502 so the Omnigent route's "best-effort cancel" branch
            # logs and continues with its own asyncio cancel.
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
        """Resolve response_id → conversation_id from the local cache.

        The cache is populated when ``proxy_stream`` sees
        ``response.created``. Elicitations always follow a turn
        that produces ``response.created``, so the cache is
        always warm for legitimate elicitation replies.

        :param response_id: The harness-assigned response id,
            e.g. ``"resp_abc123"``.
        :returns: The conversation id, or ``None`` if the
            response_id is unknown.
        """
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
        """Runner-side session resource inventory.

        :param session_id: Session/conversation identifier.
        :param limit: Max resources to return, default 20.
        :param after: Cursor resource id for forward pagination.
        :param before: Cursor resource id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :param type: Optional resource-type filter.
        :returns: PaginatedList of session resources.
        """
        from omnigent.entities.pagination import paginate_in_memory

        spec = await _resolve_session_agent_spec(session_id)
        full = resource_registry.list_resources(
            session_id,
            resource_type=type,
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

    # ── Phase 1b: typed resource collections ───────────────────
    # Register typed collection routes BEFORE /{resource_id} so
    # names like "terminals" and "environments" are never captured
    # as resource ids.

    def _build_typed_list_response(
        session_id: str,
        resource_type: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        """Build a PaginatedList response filtered by resource type.

        :param session_id: Session/conversation identifier.
        :param resource_type: One of ``"environment"``,
            ``"terminal"``, or ``"file"``.
        :param limit: Max resources to return.
        :param after: Cursor resource id.
        :param before: Cursor resource id.
        :param order: Sort order.
        :returns: JSON response with filtered resource list.
        """
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
        """Return only environment resources for a session.

        :param session_id: Session/conversation identifier.
        :param limit: Max resources to return.
        :param after: Cursor resource id.
        :param before: Cursor resource id.
        :param order: Sort order.
        :returns: Filtered ``PaginatedList`` of environment resources.
        """
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
        """Return a single environment resource by id.

        Includes a ``metadata.root`` field on the default environment
        resource when the session has a filesystem available — the same
        root used by the filesystem API endpoints.

        :param session_id: Session/conversation identifier.
        :param environment_id: Opaque environment resource id,
            e.g. ``"default"``.
        :returns: The environment resource object.
        """
        agent_spec = await _resolve_session_agent_spec(session_id)
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
                metadata = {**content.get("metadata", {}), "root": root}
                # Expose the runner's home dir so the Web UI can expand a
                # leading ``~`` in paths the agent mentions (e.g.
                # ``~/proj/foo.md``) and resolve them against ``root`` —
                # the agent's tools run in this same runner process, so
                # this is exactly the home its ``~`` expands to. Omitted
                # when ``expanduser`` can't resolve ``~`` to an absolute
                # path (it leaves ``~`` literal — e.g. no HOME and no
                # passwd entry to fall back to).
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
        """Return only terminal resources for a session.

        :param session_id: Session/conversation identifier.
        :param limit: Max resources to return.
        :param after: Cursor resource id.
        :param before: Cursor resource id.
        :param order: Sort order.
        :returns: Filtered ``PaginatedList`` of terminal resources.
        """
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
        """Launch or return an existing terminal resource.

        Preserves the idempotency semantics of ``sys_terminal_launch``:
        creating an already-running ``(terminal, session_key)`` returns
        the existing resource rather than spawning a duplicate.

        :param session_id: Session/conversation identifier.
        :param request: JSON body with ``terminal`` and ``session_key``.
        :returns: The terminal resource object.
        """
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

        # Resume "ensure" path (see _ensure_claude_terminal_on_runner): the CLI
        # marks the request with ``ensure_native_terminal`` to ask for the full
        # claude-native setup that only _auto_create_claude_terminal does (incl.
        # cold resume); the generic launch below can't reproduce it. Keyed on
        # the explicit marker — NOT on the absence of spec/bridge_inject_dir,
        # which is ambiguous with a plain generic claude launch. Idempotent:
        # return the live terminal if present, else auto-create.
        if (
            body.get("ensure_native_terminal")
            and terminal_name == "claude"
            and session_key == "main"
        ):
            claude_terminal_id = terminal_resource_id("claude", "main")
            # Serialize the ensure check-and-create with _claude_terminal_ensure_locks
            # so concurrent calls from _on_runner_connect (create_session) and the
            # message path's _ensure_native_terminal_ready (here) cannot both find no
            # terminal and both call _auto_create_claude_terminal — which spawns two
            # forwarders and double-persists every transcript item.
            _ensure_lock = _claude_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with _ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, claude_terminal_id
                )
                if existing is not None:
                    _logger.info(
                        "Claude terminal ensure returning existing resource: session=%s "
                        "terminal_id=%s",
                        session_id,
                        claude_terminal_id,
                    )
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                _logger.info(
                    "Claude terminal ensure auto-creating missing resource: session=%s "
                    "terminal_id=%s",
                    session_id,
                    claude_terminal_id,
                )
                try:
                    claude_agent_spec = await _resolve_session_agent_spec(session_id)
                    terminal_view = await _auto_create_claude_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        agent_spec=claude_agent_spec,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Claude terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Claude")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )
        if (
            body.get("ensure_native_terminal")
            and terminal_name == "codex"
            and session_key == "main"
        ):
            codex_terminal_id = terminal_resource_id("codex", "main")
            ensure_lock = _codex_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, codex_terminal_id
                )
                if existing is not None:
                    if _is_runner_owned_codex_terminal(resource_registry, existing):
                        return _codex_ensure_response_with_policy_notice(session_id, existing)
                    _logger.info(
                        "Replacing non-native codex terminal %s for session %s",
                        codex_terminal_id,
                        session_id,
                    )
                    closed = await resource_registry.close_terminal(session_id, codex_terminal_id)
                    if not closed:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "error": {
                                    "code": "terminal_conflict",
                                    "message": (
                                        "Existing codex terminal is not a runner-owned "
                                        "Codex TUI and could not be closed."
                                    ),
                                }
                            },
                        )
                try:
                    codex_agent_spec = await _resolve_session_agent_spec(session_id)
                    terminal_view = await _auto_create_codex_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        agent_spec=codex_agent_spec,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Codex terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Codex")
                # Surface the one-shot policy notice while still holding the
                # per-session ensure lock so the read-and-clear of
                # ``policy_notice_pending`` is serialized with the
                # existing-terminal path above — two concurrent ensures can
                # never both emit the banner.
                return _codex_ensure_response_with_policy_notice(session_id, terminal_view)

        if body.get("ensure_native_terminal") and terminal_name == "pi" and session_key == "main":
            pi_terminal_id = terminal_resource_id("pi", "main")
            ensure_lock = _pi_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, pi_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    # See _auto_create_pi_terminal: a genuine spec resolution
                    # error must propagate to the outer handler (-> start error
                    # response) rather than be swallowed to agent_spec=None,
                    # which silently drops the agent's sandbox policy.
                    _pi_ensure_spec = await _resolve_session_agent_spec(session_id)
                    terminal_view = await _auto_create_pi_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        agent_spec=_pi_ensure_spec,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Pi terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Pi")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "opencode"
            and session_key == "main"
        ):
            opencode_terminal_id = terminal_resource_id("opencode", "main")
            ensure_lock = _opencode_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, opencode_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    opencode_agent_spec = await _resolve_session_agent_spec(session_id)
                    terminal_view = await _auto_create_opencode_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        agent_spec=opencode_agent_spec,
                        server_client=server_client,
                    )
                except Exception as exc:
                    _logger.exception(
                        "OpenCode terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "OpenCode")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "cursor"
            and session_key == "main"
        ):
            cursor_terminal_id = terminal_resource_id("cursor", "main")
            ensure_lock = _cursor_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, cursor_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    # The spec only feeds optional ``--model`` injection, so a
                    # resolution failure must not block launching the terminal —
                    # fall back to None like the Pi ensure path above.
                    try:
                        cursor_agent_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        cursor_agent_spec = None
                    terminal_view = await _auto_create_cursor_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                        agent_spec=cursor_agent_spec,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Cursor terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Cursor")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "goose"
            and session_key == "main"
        ):
            goose_terminal_id = terminal_resource_id("goose", "main")
            ensure_lock = _goose_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, goose_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    terminal_view = await _auto_create_goose_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Goose terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Goose")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "kiro"
            and session_key == "main"
        ):
            kiro_terminal_id = terminal_resource_id("kiro", "main")
            ensure_lock = _kiro_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, kiro_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    terminal_view = await _auto_create_kiro_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Kiro terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Kiro")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "hermes"
            and session_key == "main"
        ):
            hermes_terminal_id = terminal_resource_id("hermes", "main")
            ensure_lock = _hermes_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, hermes_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    terminal_view = await _auto_create_hermes_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Hermes terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Hermes")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "antigravity"
            and session_key == "main"
            # Only the web-UI / message-path ensure probe (which sends no
            # ``spec``) boots the runner-owned agy terminal here. The
            # ``omnigent antigravity`` CLI wrapper POSTs ``ensure_native_terminal``
            # WITH a full ``spec`` (it owns the agy launch + its own client-side
            # forwarder) and must fall through to the generic launch below —
            # exactly the behavior its launch comment documents. Gating on the
            # absent ``spec`` keeps the CLI path untouched while giving the web UI
            # a runner-owned terminal + server-side forwarder.
            and not body.get("spec")
        ):
            antigravity_terminal_id = terminal_resource_id("antigravity", "main")
            ensure_lock = _antigravity_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, antigravity_terminal_id
                )
                if existing is not None:
                    if _is_runner_owned_antigravity_terminal(resource_registry, existing):
                        return JSONResponse(
                            status_code=200,
                            content=session_resource_view_to_dict(existing),
                        )
                    _logger.info(
                        "Replacing non-native antigravity terminal %s for session %s",
                        antigravity_terminal_id,
                        session_id,
                    )
                    closed = await resource_registry.close_terminal(
                        session_id, antigravity_terminal_id
                    )
                    if not closed:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "error": {
                                    "code": "terminal_conflict",
                                    "message": (
                                        "Existing antigravity terminal is not a "
                                        "runner-owned agy TUI and could not be closed."
                                    ),
                                }
                            },
                        )
                try:
                    terminal_view = await _auto_create_antigravity_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Antigravity terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Antigravity")
                return JSONResponse(
                    status_code=200,
                    content=session_resource_view_to_dict(terminal_view),
                )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "qwen"
            and session_key == "main"
        ):
            qwen_terminal_id = terminal_resource_id("qwen", "main")
            ensure_lock = _qwen_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, qwen_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    terminal_view = await _auto_create_qwen_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                    )
                except Exception as exc:
                    _logger.exception(
                        "qwen terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "qwen")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        if (
            body.get("ensure_native_terminal")
            and terminal_name == "kimi"
            and session_key == "main"
        ):
            kimi_terminal_id = terminal_resource_id("kimi", "main")
            ensure_lock = _kimi_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
            async with ensure_lock:
                existing = await resource_registry.get_terminal_resource(
                    session_id, kimi_terminal_id
                )
                if existing is not None:
                    return JSONResponse(
                        status_code=200,
                        content=session_resource_view_to_dict(existing),
                    )
                try:
                    # The spec only feeds optional model injection (a follow-up),
                    # so a resolution failure must not block launching the
                    # terminal — fall back to None like the cursor/Pi paths.
                    try:
                        kimi_agent_spec = await _resolve_session_agent_spec(session_id)
                    except OmnigentError:
                        kimi_agent_spec = None
                    terminal_view = await _auto_create_kimi_terminal(
                        session_id,
                        resource_registry,
                        _publish_event,
                        server_client=server_client,
                        ensure_comment_relay=_ensure_comment_relay_started,
                        agent_spec=kimi_agent_spec,
                    )
                except Exception as exc:
                    _logger.exception(
                        "Kimi terminal ensure failed for session=%s",
                        session_id,
                    )
                    return _native_terminal_start_error_response(exc, "Kimi")
            return JSONResponse(
                status_code=200,
                content=session_resource_view_to_dict(terminal_view),
            )

        from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

        cwd_override = body.get("cwd")
        sandbox_override = body.get("sandbox")
        spec = body.get("spec") or {}

        # Resolve the agent spec once: we need it for both the
        # declared-terminal lookup and to thread the agent's
        # ``os_env`` (with its sandbox / egress_rules /
        # env_passthrough) through as the inheritance parent. Without
        # the latter, the previous implementation built a fresh
        # TerminalEnvSpec with no sandbox at all — every
        # REST-launched terminal ran completely outside the agent's
        # sandbox, regardless of YAML config.
        agent_spec = await _resolve_session_agent_spec(session_id)
        agent_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None

        # Prefer the operator-declared terminal spec when the agent
        # YAML declares one with this name (e.g. ``sandboxed_zsh``).
        # The body cannot then inject command/args/env/sandbox —
        # only the per-call cwd/sandbox overrides gated by the
        # spec's allow_* flags.
        declared_terminal = None
        if agent_spec is not None:
            terminals_map = getattr(agent_spec, "terminals", None) or {}
            declared_terminal = terminals_map.get(terminal_name)

        if declared_terminal is not None:
            env_spec = declared_terminal
            # Body's ``spec.cwd`` becomes a cwd_override (still
            # subject to the spec's allow_cwd_override gate and
            # the launch-time containment check).
            cwd_override = cwd_override or spec.get("cwd")
        else:
            # No matching terminal in the YAML: synthesise from the
            # body but inherit the agent's sandbox so we don't punch
            # a hole in the policy. The wrapper use case
            # (omnigent claude) lands here; the launched terminal
            # picks up the agent's sandbox/egress instead of running
            # completely unsandboxed.
            spec_cwd = spec.get("cwd")
            if spec_cwd is None or spec_cwd in (".", "./"):
                spec_cwd = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type=spec.get("os_env_type", "caller_process"),
                    cwd=spec_cwd,
                    # Inherit the agent's sandbox by reference;
                    # build_terminal_os_env_spec deep-clones it.
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=spec.get("command", "bash"),
                args=spec.get("args", []),
                env=spec.get("env", {}),
                scrollback=spec.get("scrollback", 10000),
                tmux_allow_passthrough=bool(spec.get("tmux_allow_passthrough", False)),
                tmux_start_on_attach=bool(spec.get("tmux_start_on_attach", False)),
            )
        # Opt-in: callers (e.g. the ``omnigent claude`` wrapper) can ask the
        # runner to publish the launched terminal's tmux socket + target into a
        # bridge directory on this host, and to expose the comment tools to
        # Claude Code. Any truthy value (including a legacy path string from
        # older callers) enables it; the destination is derived server-side
        # from session_id, never from the body.
        bridge_inject = bool(body.get("bridge_inject_dir"))
        bridge_id: str | None = None
        relay_existed = False
        if bridge_inject:
            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=session_id,
            )
            # Start the comment-tool relay BEFORE spawning Claude so
            # tool_relay.json is on disk before Claude Code's first MCP
            # tools/list — eliminating the cold-launch race where the tools
            # would be absent until a best-effort tools-changed notification.
            # The client already reset the bridge dir (prepare_bridge_dir wipes
            # tool_relay.json) before this request, so writing here is safe.
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
                # The bridge-inject path is the ``omnigent claude``
                # wrapper launching the claude-native agent terminal —
                # mark it so its pane activity drives the session's
                # PTY-derived working status.
                resource_role=(CLAUDE_NATIVE_TERMINAL_ROLE if bridge_inject else None),
            )
        except RuntimeError as exc:
            # The relay was started before the spawn; tear down any relay this
            # request started so a failed launch does not leak a bound socket or
            # a stale advertisement. ``relay_existed`` guards against closing a
            # relay a prior launch owns (idempotent re-launch).
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
            # Publish the launched terminal's tmux target now that the pane
            # exists (the publish needs the spawned terminal).
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

    @app.get("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def get_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        """Return a single terminal resource by id.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_bash_s1"``.
        :returns: The terminal resource object.
        """
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
        """Move a terminal resource to another session without closing it.

        This runner-local endpoint does not perform user/session ACL
        checks: the runner has no Omnigent permission store. Public callers
        must use the Omnigent session-resource transfer route, which validates
        edit access on both source and target sessions before proxying
        this request to the bound runner. The runner validates only its
        local invariant: the terminal must still belong to
        ``session_id`` before it can be reparented.

        :param session_id: Current owning session/conversation id.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_claude_main"``.
        :param request: JSON body containing ``target_session_id``.
        :returns: The terminal resource object projected under the
            target session.
        """
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
        """Close a terminal resource.

        Idempotent: returns 404 for unknown terminals. Delegates to
        ``TerminalRegistry.close()``.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: Deletion confirmation object.
        """
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
        """Re-create a dead embedded Omnigent REPL terminal for attach.

        The REPL terminal is runner-owned plumbing behind the web UI's
        Terminal view. Its tmux session dies whenever the REPL process
        exits — the user pressing Ctrl+C inside the REPL, or ``omnigent
        attach`` failing at deferred start — but the registry keeps
        reporting the dead instance as running, so the web Terminal pill
        stays enabled while every attach is rejected, leaving a
        permanently empty pane. Closing the stale entry and re-running
        the auto-create restores a live pane whose REPL boots on the
        very attach that triggered the recreation
        (``tmux_start_on_attach``).

        Serialized per session on ``_repl_terminal_ensure_locks``
        against the session-create bootstrap and concurrent attaches;
        liveness is re-checked under the lock so a racer's fresh
        terminal is reused rather than killed.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param terminal_id: The REPL terminal's resource id
            (``"terminal_tui_main"``), passed through for the stale
            close + final resolve.
        :returns: The live ``TerminalListEntry``, or ``None`` when
            recreation failed (the attach then closes 4404 as before).
        """
        if resource_registry is None or resource_registry.terminal_registry is None:
            return None
        registry = resource_registry.terminal_registry
        lock = _repl_terminal_ensure_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            existing = registry.get(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
            if existing is None or not existing.running or not await existing.is_alive():
                # Low-level registry close, not ``close_terminal``: the
                # resource-level scan skips entries whose ``running`` flag
                # is already False (the liveness probe above flips it),
                # which would leave the dead instance's activity watcher
                # and scratch dir behind. ``TerminalRegistry.close`` pops
                # the entry unconditionally and tears the instance down.
                await registry.close(session_id, _REPL_TERMINAL_NAME, _REPL_TERMINAL_SESSION_KEY)
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
                    # Broad catch, same rationale as the session-create
                    # bootstrap: a failed relaunch (tmux spawn error, label
                    # PATCH failure) must degrade to the pre-existing 4404
                    # close on this attach — never crash the WS route.
                    _logger.exception(
                        "Failed to recreate omnigent REPL terminal for %s",
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
    ) -> None:
        """Attach to a terminal resource by id via WebSocket.

        Resource-addressed counterpart of the legacy
        ``/v1/sessions/{id}/resources/terminals/{id}/attach`` route.
        Resolves the terminal resource id back to the registry entry
        and bridges the tmux PTY.

        The embedded Omnigent REPL terminal (role
        :data:`OMNIGENT_REPL_TERMINAL_ROLE`) gets recreate-on-attach
        semantics: a dead pane is torn down and relaunched instead of
        rejected, so the web Terminal view always opens onto a live
        REPL (see :func:`_recreate_repl_terminal`). Other terminals
        keep the strict 4404 contract — a dead agent-created terminal
        is meaningful state, not plumbing to resurrect.

        :param websocket: Accepted FastAPI WebSocket.
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :param read_only: Pass ``-r`` to tmux and drop inbound
            binary frames when ``True``.
        """
        await websocket.accept()
        entry = resolve_terminal_entry_by_resource_id(
            session_id,
            terminal_id,
            terminal_registry,
        )
        if entry is None or not entry.instance.running or not await entry.instance.is_alive():
            if (
                resource_registry is not None
                and resource_registry.terminal_resource_role(session_id, terminal_id)
                == OMNIGENT_REPL_TERMINAL_ROLE
            ):
                entry = await _recreate_repl_terminal(session_id, terminal_id)
            else:
                entry = None
            if entry is None:
                await websocket.close(
                    code=WS_CLOSE_TERMINAL_NOT_FOUND,
                    reason="terminal resource not found or not running",
                )
                return
        # If a cost-budget approval is still pending when this client attaches
        # (the ASK fired while only the web Chat was open), re-pop it on the
        # now-attaching client. Spawned concurrently — it waits for the tmux
        # client below to register, then pops only if still pending — because
        # the PTY bridge blocks for the connection's lifetime.
        _repop_task = asyncio.create_task(
            _repop_pending_cost_popup_on_attach(
                session_id,
                str(entry.instance.socket_path),
                entry.instance.tmux_target,
            )
        )
        _COST_POPUP_REPOP_TASKS.add(_repop_task)
        _repop_task.add_done_callback(_COST_POPUP_REPOP_TASKS.discard)
        await bridge_tmux_pty_to_websocket(
            websocket,
            socket_path=str(entry.instance.socket_path),
            tmux_target=entry.instance.tmux_target,
            read_only=read_only,
            # Stamp client interactions (attach/detach/keystroke/focus/
            # mouse/resize) on the instance so its idle watcher discounts
            # the client-driven repaints they trigger instead of reading
            # them as agent activity. In-process here (runner owns both the
            # attach bridge and the watcher).
            on_client_interaction=entry.instance.note_client_interaction,
        )

    # ── Phase 3: environment filesystem endpoints ─────────────────

    async def _require_os_env(session_id: str) -> Any | None:
        """Raise HTTP 404 if the session's agent spec has no ``os_env``.

        Guards all Phase-3 filesystem endpoints so that sessions whose
        agent spec does not include an ``os_env`` block receive a clean
        404 rather than falling through to a synthetic default
        environment.  The check is a no-op when no agent spec is
        available (dev/standalone mode where
        ``_resolve_session_agent_spec`` returns ``None``).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :raises HTTPException: HTTP 404 when the resolved spec is not
            ``None`` and its ``os_env`` attribute is ``None``.
        :returns: The resolved agent spec, or ``None`` in dev/standalone
            mode.  Callers can use this to avoid a redundant second
            resolution on the same request.
        """
        spec = await _resolve_session_agent_spec(session_id)
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
        """List the root directory of an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param limit: Max entries to return.
        :param after: Cursor entry id.
        :param before: Cursor entry id.
        :param order: Sort order.
        :returns: PaginatedList of filesystem entries.
        """
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
        """Search for files recursively by name/path substring and glob filters.

        Walks the full directory tree in the session's OS environment and
        returns files matching ``q`` (a case-insensitive name/path substring),
        optionally scoped by glob filters: ``exclude`` globs drop files and
        ``include`` globs restrict which files are kept.  Glob patterns use the
        VSCode/Cursor subset (``*``, ``**``, ``?``, ``{a,b}``).  Only file
        entries are returned (not directories).  Results are capped at
        ``limit``.

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
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
            split_glob_list,
        )

        # Brace-aware split so "*.{js,ts}" stays one pattern (its inner comma
        # is not a list separator). split_glob_list handles None/blank.
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
        """List changed files for the session (flat, registry-backed).

        Returns a flat list of files that the agent has created, modified,
        or deleted, regardless of directory depth.  Behavior is
        mode-dependent:

        - **Non-git workspaces** (``AgentEditFilesystemRegistry``): returns
          only files touched by the agent via ``sys_os_write``,
          ``sys_os_edit``, or the REST write/edit/delete filesystem
          endpoints during this session.  Shell tool (``sys_os_shell``)
          side-effects are not tracked.  No background watcher is involved.
        - **Git workspaces** (``GitFilesystemRegistry``): returns all files
          with uncommitted changes in the working tree (``git status``),
          regardless of which session wrote them.  Session-scoped filtering
          is not available in git mode.

        This endpoint is distinct from the directory listing endpoint
        (``GET /filesystem``) which reflects the current on-disk state.
        Use this endpoint for the flat "changed files" view; use the
        directory listing endpoints for hierarchical browsing.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :returns: JSON list of changed file entries with ``status`` field.
        """
        await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)
        raw_changes = (
            session_registry.list_changed_files(
                session_id,
                limit=10_000,
            )
            if session_registry is not None
            else []
        )
        data = [
            {
                "object": "session.environment.filesystem.entry",
                "path": rec["path"],
                "name": rec["path"].split("/")[-1],
                "status": rec["status"],
                "bytes": rec.get("bytes"),
                "modified_at": rec.get("modified_at"),
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
        """Return before/after diff content for a changed file.

        Looks up the pre-modification snapshot (seeded by the caller before
        each write or edit — REST handlers call ``seed_snapshot`` before
        writing; ``sys_os_write``/``sys_os_edit`` do the same) and the
        current file content, then returns both so the UI can render a
        before/after diff view.

        Returns ``404`` when *relative_path* is not in the changed-files
        registry (i.e. it was never modified or created this session).

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root,
            e.g. ``"src/foo.py"``.
        :returns: JSON with ``before`` and ``after`` content strings (either
            may be ``null``).
        """
        agent_spec = await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)

        from omnigent.entities.environment_filesystem import InvalidPath
        from omnigent.runner.environment_filesystem import _validate_path

        try:
            relative_path = _validate_path(relative_path)
        except InvalidPath as exc:
            # InvalidPath is a 400 input-validation error with a
            # developer-authored, non-sensitive message (e.g. "Path traversal
            # is not allowed"). Surface it verbatim like the global
            # ResourceError handler does, rather than genericizing useful
            # client feedback — str(exc) here carries no server internals.
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

        # Check the file is tracked in the changed-files registry.
        record = (
            session_registry.get_changed_file(session_id, relative_path)
            if session_registry is not None
            else None
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

        # ``before``: pre-modification baseline — seeded snapshot (first-write-wins)
        # for sessions that called seed_snapshot, git HEAD for git workspaces,
        # None for new/untracked files.  Wrapped in asyncio.to_thread because
        # get_baseline may invoke a subprocess (git show).
        import asyncio as _asyncio

        before: str | None = (
            await _asyncio.to_thread(session_registry.get_baseline, relative_path)
            if session_registry is not None
            else None
        )

        # ``after``: current on-disk content via the sandbox, consistent with
        # the rest of the filesystem API.  Pass limit=None to bypass the
        # 2 000-line agent-tool cap — the diff view needs the full file.
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
        """Read a file or list a directory in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param limit: Max entries for directory listing.
        :param after: Cursor entry id.
        :param before: Cursor entry id.
        :param order: Sort order.
        :returns: File content or directory listing.
        """
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
        """Write/replace a file in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``content`` and optional
            ``encoding`` and ``create_parents``.
        :returns: Write result with change tracking.
        """
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
        # Seed the diff snapshot with the current content *before* overwriting
        # so the diff endpoint can return the true pre-modification state.
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
        """Edit a file in an environment via text replacement.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``old_text``, ``new_text``,
            and optional ``replace_all``.
        :returns: Edit result with change tracking.
        """
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
        # Seed the diff snapshot with the current content *before* editing.
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
        """Delete a file or directory in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param recursive: Allow recursive directory deletion.
        :returns: Delete result.
        """
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
        """Cache the session's created_at and workspace to avoid repeated server fetches.

        Reads the shared :func:`_session_snapshot` (one
        ``GET /v1/sessions/{id}`` per session) on first access and
        projects ``created_at`` + ``workspace`` into their caches.
        Subsequent calls for the same session_id short-circuit
        immediately.  ``created_at`` falls back to the current wall time
        when the snapshot fetch fails.

        The ``workspace`` field may differ from the runner's global
        ``runner_workspace`` when the session uses a git worktree.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: None.
        """
        if session_id in _session_start_cache:
            return
        snapshot = await _session_snapshot(session_id)
        _session_start_cache[session_id] = snapshot.created_at
        _session_workspace_cache[session_id] = snapshot.workspace

    async def _resolve_session_spec_entry(session_id: str) -> Any | None:
        """
        Resolve the session-scoped spec *entry*, populating the cache.

        Returns the entry (a :class:`ResolvedSpec` or bare spec) rather
        than the unwrapped spec, so callers that need the materialized
        bundle workdir — e.g. skill discovery — can read it via
        :func:`_resolved_spec_workdir`. Resource access can happen
        before the first turn dispatches, so the harness process
        manager may not have loaded the session's spec yet; this reads
        the shared :func:`_session_snapshot` for the session's
        ``agent_id`` and reuses the normal ``spec_resolver`` path.

        A per-session lock makes resolution single-flight: a startup
        burst of concurrent callers resolves the bundle once and the
        rest read the cached entry, instead of each issuing its own
        ``agent/contents`` fetch. The success cache is keyed on the
        resolved entry only — failures are re-raised without caching so
        the next call retries once the agent binds to the session.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The cached/resolved spec entry, or ``None`` when no
            spec resolver is configured for this runner.
        :raises OmnigentError: If the server returns malformed data
            or the referenced agent cannot be resolved.
        """
        if session_id in _session_spec_cache:
            return _session_spec_cache[session_id]
        if spec_resolver is None:
            _session_spec_cache[session_id] = None
            return None
        lock = _session_spec_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Re-check under the lock: a concurrent caller may have
            # resolved the spec while we waited to acquire it.
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
                raise OmnigentError(
                    f"session spec resolver: agent {agent_id!r} for "
                    f"session {session_id!r} was not found",
                    code=ErrorCode.NOT_FOUND,
                )
            # Sub-agent swap: the bound agent_id resolves to the PARENT
            # spec, so cache the child's sub-spec for a sub-agent session.
            # Otherwise _session_spec_cache (and _session_harness_name /
            # _is_native_harness, which read it) report the parent harness —
            # the misclassification that respawns a claude-native sub-agent
            # as claude-sdk and tears down its terminal ("Bridge closed").
            # The snapshot carries sub_agent_name; backfill the in-memory map
            # so the dispatch-path swap is cheap too.
            sub_agent_name = snapshot.sub_agent_name
            if sub_agent_name:
                _session_sub_agent_names[session_id] = sub_agent_name
                from omnigent.runtime.workflow import _find_spec_by_name

                parent_spec = _unwrap_resolved_spec(spec_entry)
                if parent_spec is not None:
                    sub_spec = _find_spec_by_name(parent_spec, sub_agent_name)
                    if sub_spec is not None:
                        workdir = _resolved_spec_workdir(spec_entry)
                        spec_entry = (
                            ResolvedSpec(spec=sub_spec, workdir=workdir)
                            if workdir is not None
                            else sub_spec
                        )
            _session_spec_cache[session_id] = spec_entry
            return spec_entry

    async def _resolve_session_agent_spec(session_id: str) -> Any | None:
        """
        Resolve the session-scoped agent spec for filesystem resources.

        Thin wrapper over :func:`_resolve_session_spec_entry` that
        returns the unwrapped spec, so primary OS environment creation
        honors the uploaded bundle's ``os_env`` settings.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: The parsed session agent spec, or ``None`` when
            no spec resolver is configured.
        :raises OmnigentError: If the server returns malformed data
            or the referenced agent cannot be resolved.
        """
        entry = await _resolve_session_spec_entry(session_id)
        return _unwrap_resolved_spec(entry) if entry is not None else None

    async def _resolve_session_skills(session_id: str) -> list[SkillSpec]:
        """
        Resolve the merged (bundled + host) skills for a session.

        Skills are runner-owned and combine every source the agent can
        load, discovered against *this runner's* filesystem and honoring
        the spec's ``skills_filter``:

        * the spec's bundled ``skills`` (the bundle's ``skills/`` dir);
        * host skills under the **session's workspace** — the agent's
          working directory on this runner (the claude-native TUI's cwd,
          the in-process harness workspace, a git worktree), where a
          project's ``.claude/skills/`` live;
        * host skills under the **agent bundle workdir**;
        * user-global host skills (``~/.claude/skills`` etc., scanned by
          :func:`discover_host_skills`).

        The workspace is the primary root because that is where the
        harness actually loads project skills; the bundle workdir is
        unioned in for completeness (it is a throwaway temp dir for
        single-YAML agents like ``claude-native-ui``, so usually
        contributes nothing). Falls back to the runner's global workspace,
        then the process cwd, when no workspace is known. Deduplicated by
        name with bundled winning, then earlier roots winning. Cached per
        session so the filesystem walk runs once per session lifetime
        (dropped in ``delete_session``).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Bundled skills followed by host skills, deduplicated
            by name. Empty when no spec resolver is configured or the
            spec exposes no skills.
        :raises OmnigentError: If the session's spec cannot be
            resolved.
        """
        cached = _session_skills_cache.get(session_id)
        if cached is not None:
            return cached
        entry = await _resolve_session_spec_entry(session_id)
        spec = _unwrap_resolved_spec(entry) if entry is not None else None
        if spec is None:
            return []
        workspace = await _session_workspace_value(session_id)
        # Host-discovery roots in priority order: the session workspace
        # (where the harness runs) first, then the agent bundle workdir.
        # Both are unioned; ``discover_host_skills`` also scans ``~`` on
        # each call. Distinct, resolved, non-None paths only.
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
        # No workspace and no bundle workdir: match the cwd fallback the
        # in-process LoadSkillTool uses so behavior stays consistent.
        if not roots:
            roots.append(Path.cwd())

        def _discover() -> list[SkillSpec]:
            """Merge bundled + host skills (every root) off the event loop."""
            merged: list[SkillSpec] = list(spec.skills)
            seen = {s.name for s in merged}
            for root in roots:
                for hs in discover_host_skills(root, spec.skills_filter):
                    if hs.name not in seen:
                        seen.add(hs.name)
                        merged.append(hs)
            return merged

        skills = await asyncio.to_thread(_discover)
        _session_skills_cache[session_id] = skills
        return skills

    @app.get("/v1/sessions/{session_id}/skills")
    async def get_session_skills(session_id: str) -> JSONResponse:
        """
        Return the merged (bundled + host) skills for a session.

        Skills are runner-owned: discovery walks *this* runner's
        filesystem (the materialized bundle and the runner's
        ``~/.claude/skills/``), not the Omnigent server's. The server overlays
        this list onto the session snapshot it serves to clients (the
        web composer's slash-command menu).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: JSON ``{"skills": [{"name", "description"}, ...]}``.
            Empty list when the runner has no spec resolver wired.
        """
        skills = await _resolve_session_skills(session_id)
        return JSONResponse(
            status_code=200,
            content={"skills": [{"name": s.name, "description": s.description} for s in skills]},
        )

    @app.get("/v1/sessions/{session_id}/codex-model-options")
    async def get_session_codex_model_options(session_id: str) -> JSONResponse:
        """
        Return Codex app-server model options for a codex-native session.

        The AP server uses this to populate Web UI model and effort controls
        from Codex's actual ``model/list`` response rather than a copied
        catalog. Non-codex-native sessions return an empty list.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: JSON ``{"models": [...]}``, where each model is a raw
            Codex ``model/list`` object.
        """
        if _session_harness_name(session_id) != "codex-native":
            return JSONResponse(status_code=200, content={"models": []})
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

    # Note: cursor-native has no model-options route. Its catalog is a curated
    # *static* base list served directly by the AP server (see
    # ``_fetch_model_options`` in omnigent/server/routes/sessions.py), so it
    # needs no runner round-trip and stays immune to the runner-backed cache
    # invalidation that would otherwise blank the picker on an effort change.

    @app.post("/v1/sessions/{session_id}/skills/resolve")
    async def resolve_session_skill(session_id: str, request: Request) -> JSONResponse:
        """
        Resolve a skill invocation into its hidden ``<skill>`` meta text.

        The runner owns the skill's on-disk content: it reads the
        ``SKILL.md`` body and lists resource files from the skill's
        directory *on this runner*, so the embedded ``<path>`` and
        resource listing match what the ``read_skill_file`` tool
        resolves at runtime. The Omnigent server calls this, persists the
        returned text as a hidden meta item, and forwards it as the turn
        input (runner-resolves, server-persists).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param request: Request whose JSON body carries ``{"name": str,
            "arguments": str}`` — the skill name and the raw argument
            string typed after the slash command (``arguments`` defaults
            to ``""``).
        :returns: JSON ``{"meta_text": str}`` on success; 404
            ``{"error": "skill_not_found", "detail": str, "available":
            [str, ...]}`` when the skill is not exposed for this session;
            400 when the body is not a JSON object, ``name`` is missing,
            or ``arguments`` is not a string.
        """
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
        """Dispatch GET to list_dir or read depending on path type.

        For file paths the response includes a ``content_type`` field
        derived from ``mimetypes.guess_type`` (per the
        UI_SESSION_RESOURCES_MIGRATION design).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :param path: Relative path (empty string for root).
        :param limit: Max entries for directory listing.
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: JSON response with directory listing or file content.
        """
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(session_id)
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
        # Derive MIME type from the file path for syntax highlighting
        # and binary-vs-text rendering in UI clients.
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
        """Convert a FilesystemEntry to a JSON-serializable dict.

        :param entry: The filesystem entry.
        :returns: Dict matching the API shape.
        """
        return {
            "id": entry.id,
            "object": "session.environment.filesystem.entry",
            "name": entry.name,
            "path": entry.path,
            "type": entry.type,
            "bytes": entry.bytes,
            "modified_at": entry.modified_at,
        }

    # ── Phase 5: environment shell endpoint ────────────────────────

    @app.post("/v1/sessions/{session_id}/resources/environments/{environment_id}/shell")
    async def run_environment_shell(
        session_id: str,
        environment_id: str,
        request: Request,
    ) -> JSONResponse:
        """Execute a shell command in an environment.

        Routes through ``OSEnvironment.shell()`` so the sandbox
        enforces access control.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param request: JSON body with ``command`` and optional
            ``timeout``.
        :returns: Shell result with stdout, stderr, exit_code.
        """
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

    # ── Generic single-resource lookup (registered AFTER typed routes)

    @app.get("/v1/sessions/{session_id}/resources/{resource_id}")
    async def get_session_resource(
        session_id: str,
        resource_id: str,
    ) -> JSONResponse:
        """Return a single resource by id from the unified inventory.

        :param session_id: Session/conversation identifier.
        :param resource_id: Opaque resource id.
        :returns: The resource object regardless of type.
        """
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
        """Drop cached spec/tool data derived from a session's agent bundle."""
        _session_spec_cache.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        _session_mcp_spec_hash.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        if agent_id:
            _spec_cache.pop(agent_id, None)

    # ── Phase 4: session resource cleanup endpoint ────────────────

    @app.delete("/v1/sessions/{session_id}/resources")
    async def cleanup_session_resources(
        session_id: str,
    ) -> JSONResponse:
        """Close all resources owned by a session.

        Runner-internal endpoint invoked by session/conversation
        deletion.  Closes the primary OSEnv, terminals, and removes
        registry entries.  Preserves workspace files for post-mortem.

        :param session_id: Session/conversation identifier.
        :returns: Confirmation with cleanup status.
        """
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
        """Reset runner-side session state after an in-place agent switch.

        Runner-internal endpoint the AP server calls (once, while the
        session is idle) right after rebinding a conversation to a new
        agent.  It switches the session onto the new agent's os_env while
        preserving the workspace files:

        1. Closes the session's terminals via
           :func:`_teardown_session_terminals`, publishing
           ``session.resource.deleted`` for each so connected clients
           drop them (without the events the web UI keeps showing the
           old agent's dead terminal), then closes the primary OSEnv via
           :meth:`SessionResourceRegistry.cleanup_session` (workspace
           files are preserved).  The primary env re-materializes lazily
           on the next access from the new agent's spec, so the new
           ``os_env`` / sandbox / fork policy take effect while ``cwd``
           stays pinned to the same runner workspace.
        2. Drops the spec-derived session caches so the next access
           re-resolves the new agent.  The web filesystem/shell endpoints
           build the primary env from ``_session_spec_cache`` (keyed via
           ``_session_snapshot_cache``'s ``agent_id``); without dropping
           these the env would just rebuild from the STALE old spec and
           the new sandbox would never apply (a cross-agent sandbox
           leak).  Mirrors the turn-path switch reset.

        ``_session_agent_ids`` is deliberately left intact so the next
        turn still detects the switch and cold-starts the new harness
        subprocess.  This is a separate endpoint from
        ``DELETE /resources`` so the session-deletion contract (which
        also closes resources but never needs the switch-specific cache
        reset) is untouched.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :returns: Confirmation that the switch reset was applied.
        """
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
        # Close terminals with ``session.resource.deleted`` events BEFORE
        # cleanup_session — cleanup_conversation would silently pop them
        # from the registry, leaving clients showing a dead terminal
        # whose attach fails with "terminal resource not found".
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
        """Drop cached runner-side agent data after a session agent bundle edit.

        Unlike ``reset-state``, this does not close terminals or filesystem
        resources. MCP server edits only need the next tool/schema lookup to
        re-resolve the updated agent bundle.
        """
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
        """Execute a tool call on the runner after AP-server policy evaluation.

        Called by the Omnigent server's ``POST /v1/sessions/{id}/mcp`` handler
        **after** TOOL_CALL policy evaluation.  The Omnigent server owns policy
        enforcement (TOOL_CALL / TOOL_RESULT); the runner owns execution so
        that all tools run on the correct machine with the correct ``cwd``
        and environment.

        Handles **all** tool categories uniformly:

        - **MCP tools** (namespaced: ``server__tool``) — dispatched via
          :class:`RunnerMcpManager`, which manages live stdio subprocess
          connections to each configured MCP server.
        - **Runner-local tools** (bare names: ``sys_os_read``,
          ``sys_terminal_launch``, etc.) — dispatched via
          :func:`~omnigent.runner.tool_dispatch.execute_tool` using
          the session's terminal registry, inbox queue, and runner
          workspace.

        Supported ``method`` values:

        - ``tools/list`` — return namespaced MCP tool schemas for the
          agent's MCP servers (runner-local tool schemas are already
          injected by the Omnigent server in the turn request body).
        - ``tools/call`` — execute any tool call and return its output.

        Returns ``{"result": {"output": "..."}}`` on success or
        ``{"error": {"code": ..., "message": ...}}`` on failure.

        :param session_id: AP-allocated session id, e.g. ``"conv_abc123"``.
        :param request: FastAPI request; body must be a JSON object with
            ``"method"`` and ``"params"`` keys.
        :returns: :class:`JSONResponse` carrying result or error.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": {"code": -32700, "message": "Parse error: invalid JSON"}},
            )
        method: str = body.get("method") or ""
        params: dict[str, Any] = body.get("params") or {}

        if method == "tools/list":
            # Resolve the agent spec from the session cache, falling
            # back to the spec_resolver so the runner doesn't need a
            # separate spec-fetch round-trip for each tools/list call.
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
            spec_entry = _session_spec_cache.get(session_id)
            spec = _unwrap_resolved_spec(spec_entry)
            if spec is None and spec_resolver is not None:
                agent_id = _session_agent_ids.get(session_id)
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
            # Return schemas + failures so the Omnigent server can surface
            # partial results and per-server error hints.
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
            # params: {"name": "<server>__<tool>" or "sys_os_read", "arguments": {...}}
            # Namespaced names (``__`` present) are MCP tools dispatched via
            # RunnerMcpManager.  Bare names are runner-local tools (sys_*, terminal,
            # etc.) dispatched via execute_tool.
            import json as _json

            from omnigent.runner.tool_dispatch import execute_tool

            tool_name: str = params.get("name") or ""
            arguments: dict[str, Any] = params.get("arguments") or {}
            # MRTR retry: Omnigent server forwards inputResponses + requestState
            # after the user approved a gateway elicitation.
            input_responses: dict[str, Any] | None = params.get("inputResponses")
            request_state: str | None = params.get("requestState")
            if not tool_name:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": "Missing tool name"}},
                )

            if "__" in tool_name:
                # MCP tool: pass the full namespaced name so the runner
                # validates the server prefix before stripping it internally.
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
                spec_entry = _session_spec_cache.get(session_id)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    _agent_id = _session_agent_ids.get(session_id)
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
                try:
                    from omnigent.tools.mcp import McpElicitationRequired

                    if input_responses is not None:
                        # MRTR retry: the Omnigent server already showed the
                        # elicitation and gathered the user's response.
                        # Forward to the MCP server with inputResponses.
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
                    # The external MCP server returned InputRequiredResult
                    # (MRTR). Pass it back to the Omnigent server so it can
                    # surface the elicitation via SSE and retry after
                    # the user responds.
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
                # No double-underscore namespace prefix → runner-local tool
                # (sys_os_*, sys_terminal_*, etc.).  All MCP tools are
                # namespaced as ``{server}__{tool}`` by RunnerMcpManager, so
                # any name without ``__`` is definitively a runner-local tool.
                # Policy enforcement is handled by the AP server.
                spec_entry = _session_spec_cache.get(session_id)
                spec_workdir = _resolved_spec_workdir(spec_entry)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    _agent_id = _session_agent_ids.get(session_id)
                    if _agent_id:
                        try:
                            resolved = await spec_resolver(_agent_id, session_id)
                            spec_workdir = _resolved_spec_workdir(resolved)
                            spec = _unwrap_resolved_spec(resolved)
                        except Exception:  # noqa: BLE001
                            pass
                _agent_id_local = _session_agent_ids.get(session_id)
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
        """
        Resolve LLM connection for ``/v1/summarize`` from the session's spec.

        Mirrors the harness auth resolution order so compaction
        summarization uses the same credentials as normal agent turns:

        1. :class:`ProviderAuth` — resolve named provider from
           ``~/.omnigent/config.yaml``, extract ``api_key`` + ``base_url``
           from the ``openai`` family.
        2. :class:`DatabricksAuth` — resolve the named profile from
           ``~/.databrickscfg`` into ``base_url`` + ``api_key``.
        3. :class:`ApiKeyAuth` — inline ``api_key`` and optional
           ``base_url``.
        4. Global config ``auth:`` block (when spec declares no auth).
        5. Legacy ``executor.config["profile"]`` or auto-Databricks
           DEFAULT for ``databricks-*`` model prefixes.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param model: LLM model string used to decide whether to
            attempt Databricks profile resolution, e.g.
            ``"databricks/databricks-gpt-5-5"``.
        :returns: A connection dict with ``"base_url"`` and ``"api_key"``
            keys, or ``None`` when no credentials could be resolved.
        """
        from omnigent.spec.types import ApiKeyAuth, DatabricksAuth, ProviderAuth

        spec_entry = _session_spec_cache.get(session_id)
        if spec_entry is None:
            return None
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        if spec is None:
            return None

        auth = getattr(spec.executor, "auth", None)

        # 1. ProviderAuth → resolve named provider, extract openai family.
        if isinstance(auth, ProviderAuth):
            return _resolve_provider_connection(auth.name, model)

        # 2. DatabricksAuth → resolve profile from ~/.databrickscfg.
        if isinstance(auth, DatabricksAuth):
            return _resolve_databricks_connection(auth.profile, session_id)

        # 3. ApiKeyAuth → inline key + optional base_url.
        if isinstance(auth, ApiKeyAuth):
            conn: dict[str, str] = {"api_key": auth.api_key}
            if auth.base_url:
                conn["base_url"] = auth.base_url
            return conn

        # 4. Global config auth (when spec declares no auth at all).
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

        # 5. Legacy fallback: executor.config.profile, executor.profile,
        #    or auto-Databricks DEFAULT for databricks-* models.
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
        """
        Resolve connection from a named provider's family.

        Loads providers from ``~/.omnigent/config.yaml`` and extracts
        ``api_key`` + ``base_url`` from the matching family entry.
        Tries the ``anthropic`` family for ``anthropic/`` or
        ``claude`` models, otherwise ``openai``. Returns ``None``
        when the provider or a suitable family is not configured.

        :param provider_name: Provider name from the ``providers:``
            block, e.g. ``"litellm"`` or ``"openrouter"``.
        :param model: LLM model string used to select the family,
            e.g. ``"anthropic/claude-sonnet-4-20250514"``.
        :returns: A connection dict, or ``None``.
        """
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
            # Databricks-kind providers route through profile resolution.
            if entry.kind == "databricks" and entry.profile:
                return _resolve_databricks_connection(entry.profile, provider_name)
            # Pick the family matching the model prefix; fall back to
            # whichever family the provider has.
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
        """
        Resolve Databricks credentials from a ``~/.databrickscfg`` profile.

        :param profile: Databricks profile name, e.g. ``"oss"`` or
            ``"DEFAULT"``.
        :param context: Logging context (session_id or provider name).
        :returns: A connection dict with ``"base_url"`` and ``"api_key"``,
            or ``None`` on failure.
        """
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
        """Summarize a message list using the runner's LLM credentials.

        Accepts a JSON body with ``messages``, ``model``, an optional
        ``connection`` dict, and an optional ``profile`` string.  For
        Databricks models, ``profile`` is used to resolve fresh OAuth
        credentials from the runner's own ``~/.databrickscfg`` — so
        the runner's credentials are used, not the Omnigent server's static
        token.

        :param request: FastAPI request carrying the JSON body.
        :returns: JSON with ``"text"`` (summary string) and
            ``"token_count"`` (tiktoken estimate) keys.
        """
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
        # Resolve LLM connection for the summarization call. Precedence:
        # 1. Explicit connection in the payload (non-Databricks callers).
        # 2. Spec auth from the session's cached spec (DatabricksAuth
        #    profile or ApiKeyAuth).
        # 3. Ambient env-var auth (DATABRICKS_CONFIG_PROFILE / DEFAULT).
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
    async def elicitation(elicitation_id: str, request: Request) -> JSONResponse:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={"error": "not_implemented", "detail": "Runner not configured"},
            )
        body = await request.json()
        # The server includes response_id when relaying elicitations
        # to the runner. Resolve conversation from it.
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
            # Translate the MCP-shape ElicitationResult body
            # ({"action": ..., "content": ...}) onto the harness's
            # discriminated ``approval`` event per
            # ``designs/session_rearchitecture.md`` §3.
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
        """Catch-up scan after tunnel reconnect (Step 8.5 Scenario B).

        For each session with in-memory history, query the server
        for items after the last known item. Append new items to
        history and start a turn if idle and new user messages
        arrived.
        """
        for session_id in list(_session_histories):
            if _is_native_harness(session_id):
                # Same rule as session-start recovery: do not synthesize
                # catch-up turns by replaying mirrored native transcript items.
                continue
            try:
                # Paginate from the last known cursor until all
                # missed items are fetched.
                after_id = _last_server_item_id.get(session_id)
                all_new: list[dict[str, Any]] = []
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
                # Start a turn if idle and new user messages arrived.
                if (
                    session_id not in _active_turns
                    and new_items
                    and new_items[-1].get("role") == "user"
                ):
                    _active_turns[session_id] = None
                    _publish_turn_status(session_id, "running")
                    agent_id = _session_agent_ids.get(session_id)
                    msg_body = {
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

    # Expose catch-up scan so _entry.py can wire it as on_reconnect.
    app.state.catch_up_scan = _catch_up_scan

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
    :returns: ``(harness, spawn_env)``; a default for unresolved specs.
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
                if sub_spec is not None:
                    spec = sub_spec
            harness = harness_override or spec.executor.config.get("harness") or spec.executor.type
            harness = canonicalize_harness(harness) or harness
            spawn_env = _build_spawn_env_from_spec(
                spec, harness, cwd=cwd, workdir=workdir, model_override=model_override
            )
            return harness, spawn_env

    # Fallback for tests that register a custom harness in _HARNESS_MODULES.
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


def _build_spawn_env_from_spec(
    spec: Any,
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
    try:
        from omnigent.runtime.workflow import (
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
            env = _build_claude_sdk_spawn_env(spec, workdir=workdir)
        elif harness == "codex":
            env = _build_codex_spawn_env(spec, workdir=workdir)
        elif harness == "pi":
            env = _build_pi_spawn_env(spec, cwd=cwd, workdir=workdir)
        elif harness == "openai-agents":
            env = _build_openai_agents_sdk_spawn_env(spec)
        elif harness == "cursor":
            env = _build_cursor_spawn_env(spec, workdir=workdir)
        elif harness == "antigravity":
            env = _build_antigravity_spawn_env(spec)
        elif harness == "kimi":
            env = _build_kimi_spawn_env(spec, cwd=cwd)
        elif harness == "qwen":
            env = _build_qwen_spawn_env(spec, workdir=workdir)
        elif harness == "goose":
            env = _build_goose_spawn_env(spec, workdir=workdir)
        elif harness == "copilot":
            env = _build_copilot_spawn_env(spec, workdir=workdir)
        else:
            # Native terminal harnesses and unknown harnesses build env elsewhere.
            return None
    except ImportError:
        return None

    # Per-session ``/model`` override wins over everything the builder baked
    # into HARNESS_<H>_MODEL. Without this, `/model` is recorded in the
    # readout but the turn still uses the provider/catalog default.
    if model_override:
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
    spec: Any,
    harness: str,
) -> Any:
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

    sandbox_dict: dict[str, Any] | None = None
    if spec.os_env is not None and spec.os_env.sandbox is not None:
        sandbox_dict = dataclasses.asdict(spec.os_env.sandbox)

    return await gate.evaluate_tool_call(
        "sys_agent_start",
        {
            "agent_name": getattr(spec, "name", None) or "",
            "harness": harness,
            "sandbox": sandbox_dict,
        },
    )


def _apply_sandbox_override_from_verdict(
    spec: Any,
    verdict_data: Any,
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

    if not isinstance(verdict_data, dict):
        return
    args = verdict_data.get("arguments")
    if not isinstance(args, dict):
        return
    sandbox_override = args.get("sandbox")
    if not isinstance(sandbox_override, dict):
        return

    if spec.os_env is None:
        spec.os_env = OSEnvSpec()
    if spec.os_env.sandbox is None:
        spec.os_env.sandbox = OSEnvSandboxSpec()

    for key, value in sandbox_override.items():
        if hasattr(spec.os_env.sandbox, key):
            setattr(spec.os_env.sandbox, key, value)
