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
import os
import shutil
import sys
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-only import: the runner keeps codex deps out of its runtime import
    # graph (they are imported lazily inside the codex-native helpers).
    from omnigent.claude_native import ClaudeNativeUcodeConfig
    from omnigent.codex_native_app_server import CodexAppServerClient

import click
import httpx
from fastapi.responses import JSONResponse, Response

from omnigent._platform import IS_WINDOWS
from omnigent.entities.session_resources import (
    SessionResourceView,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.model_override import validate_model_override
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
)
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
)
from omnigent.spec.types import AgentSpec

_logger = logging.getLogger("omnigent.runner.app")

_NATIVE_TERMINAL_START_FAILED_CODE = "native_terminal_start_failed"
_REPL_TERMINAL_NAME = "tui"
_REPL_TERMINAL_SESSION_KEY = "main"
_NO_BODY_STATUS_CODES = {204, 304}

_BACKGROUND_TITLE_HARNESS_ADAPTERS = {
    "claude-sdk": "claude-sdk",
    "claude-native": "claude-sdk",
    "codex": "codex",
}
_BACKGROUND_TITLE_MAX_PROMPT_CHARS = 4_000
_BACKGROUND_TITLE_MAX_OUTPUT_TOKENS = 32
_BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS = 60.0


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

# Delegated runner bearers last 30 minutes and refresh five minutes before
# expiry. A one-minute cadence allows several retries without giving the child
# the runner binding token; cached factory calls stay local and cheap.
_PERMISSION_HOOK_AUTH_REFRESH_INTERVAL_S = 60.0


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


async def _refresh_claude_permission_hook_auth(
    *,
    bridge_dir: Path,
    server_url: str,
    auth_token_factory: Callable[[], str | None],
    refresh_interval_s: float = _PERMISSION_HOOK_AUTH_REFRESH_INTERVAL_S,
) -> None:
    """Keep the Claude permission hook's bearer snapshot current.

    :param bridge_dir: Owner-only Claude bridge directory.
    :param server_url: Omnigent server receiving permission requests.
    :param auth_token_factory: Refresh-capable runner bearer factory.
    :param refresh_interval_s: Delay between snapshot refresh attempts.
    """
    from omnigent.claude_native_bridge import update_permission_hook_auth_headers
    from omnigent.cli_auth import databricks_request_headers

    while True:
        await asyncio.sleep(refresh_interval_s)
        try:
            token = await asyncio.to_thread(auth_token_factory)
            if token:
                headers = databricks_request_headers(server_url, bearer_token=token)
                update_permission_hook_auth_headers(bridge_dir, headers)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — retain the last still-valid snapshot
            _logger.warning("Could not refresh Claude permission-hook auth")


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
    :param bypass_sandbox: ``True`` when the session opted into Codex's
        DANGEROUS full-bypass stance (``omnigent.codex_native.bypass_sandbox``
        label == ``"1"``). The runner then launches the ``--remote`` TUI with
        ``--dangerously-bypass-approvals-and-sandbox`` and aligns the
        app-server threads (no approval prompts, no command sandbox). Default
        ``False``. See issue #657.
    """

    workspace: Path
    policy_server_url: str
    terminal_launch_args: list[str] | None
    model_override: str | None
    external_session_id: str | None
    fork_source_id: str | None
    fork_source_external_id: str | None
    fork_carry_history: bool
    bypass_sandbox: bool


@dataclasses.dataclass(frozen=True)
class _PiNativeLaunchConfig:
    """
    Persisted launch config read from a session snapshot for native terminals.

    A generic session-snapshot reader shared by the pi-native and
    cursor-native launch paths (workspace + terminal_launch_args +
    model_override). Each path consumes the subset it needs: pi-native
    uses ``model_override`` as ``--model`` (overrides the spec's pinned
    model); cursor-native does the same.

    :param workspace: Workspace cwd for the native TUI.
    :param server_url: Omnigent server URL for the extension/forwarder.
    :param terminal_launch_args: User pass-through native CLI args.
    :param external_session_id: Existing external session id, when captured by
        the extension.
    :param fork_source_external_id: SOURCE Pi session id stamped on a forked
        clone (``omnigent.fork.source_external_session_id``); consulted only
        when the clone has no native session of its own yet.
    :param fork_carry_history: ``True`` on a forked clone bound to a native
        target (``omnigent.fork.carry_history``); when no source session
        exists to clone, the clone's session is rebuilt from its OWN copied
        Omnigent items (see :func:`_auto_create_pi_terminal`). Also consumed by
        the cursor-native launch to replay prior turns as a text preamble on
        the first message.
    :param model_override: Persisted per-session ``/model`` override, e.g.
        ``"claude-4.6-sonnet-medium"``; ``None`` when unset. Consumed by the
        cursor-native launch (``--model``), ignored by pi-native.
    """

    workspace: Path
    server_url: str
    terminal_launch_args: list[str] | None
    external_session_id: str | None
    fork_source_id: str | None = None
    fork_source_external_id: str | None = None
    fork_carry_history: bool = False
    model_override: str | None = None


@dataclasses.dataclass(frozen=True)
class _KiroNativeLaunchConfig:
    """Persisted launch config needed for runner-owned Kiro terminal setup."""

    workspace: Path
    terminal_launch_args: list[str] | None
    external_session_id: str | None
    model_override: str | None = None


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
    model_override = snapshot.get("model_override")
    if model_override is not None:
        if not isinstance(model_override, str) or not model_override:
            raise RuntimeError(f"Invalid model_override for Kiro session {session_id!r}.")
        try:
            model_override = validate_model_override(model_override)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid model_override for Kiro session {session_id!r}: {exc}"
            ) from exc
    return _KiroNativeLaunchConfig(
        workspace=_kiro_session_workspace(session_workspace),
        terminal_launch_args=terminal_launch_args,
        external_session_id=external_session_id.strip()
        if isinstance(external_session_id, str)
        else None,
        model_override=model_override if isinstance(model_override, str) else None,
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
    # Fork directives stamped on a clone at fork time. Only consulted when the
    # clone has no external_session_id of its own yet (see the fork branches in
    # _auto_create_pi_terminal); inert otherwise. Mirrors the codex-native and
    # claude-native launch-config fork handling.
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
    return _PiNativeLaunchConfig(
        workspace=_pi_session_workspace(session_workspace),
        server_url=os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767").rstrip("/"),
        terminal_launch_args=terminal_launch_args,
        external_session_id=external_session_id,
        fork_source_id=fork_source_id,
        fork_source_external_id=fork_source_external_id,
        fork_carry_history=fork_carry_history,
        model_override=model_override,
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
        CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    fork_source_id: str | None = None
    fork_source_external_id: str | None = None
    fork_carry_history = False
    # DANGEROUS opt-in: full approval/sandbox bypass, stored as a plain
    # conversation label ("1" to enable). Read here so the runner applies
    # it at launch; any other value (incl. absent) leaves the normal stance.
    bypass_sandbox = False
    labels = snapshot.get("labels")
    if isinstance(labels, dict):
        _fsi = labels.get(FORK_SOURCE_LABEL_KEY)
        if isinstance(_fsi, str) and _fsi:
            fork_source_id = _fsi
        _fse = labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
        if isinstance(_fse, str) and _fse:
            fork_source_external_id = _fse
        fork_carry_history = labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
        bypass_sandbox = labels.get(CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY) == "1"
    return _CodexNativeLaunchConfig(
        workspace=_codex_session_workspace(session_workspace),
        policy_server_url=_required_runner_env("RUNNER_SERVER_URL"),
        terminal_launch_args=terminal_launch_args,
        model_override=model_override,
        external_session_id=external_session_id,
        fork_source_id=fork_source_id,
        fork_source_external_id=fork_source_external_id,
        fork_carry_history=fork_carry_history,
        bypass_sandbox=bypass_sandbox,
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
    :param fork_carry_history: ``True`` on a forked clone whose prior
        transcript should be seeded as a text preamble
        (``omnigent.fork.carry_history``); opencode has no native session to
        clone, so the runner rehydrates from the copied Omnigent transcript.
    """

    workspace: Path
    policy_server_url: str
    terminal_launch_args: list[str] | None
    model_override: str | None
    external_session_id: str | None
    fork_carry_history: bool = False


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
    # On a forked clone, the server stamps carry-history (opencode has no native
    # session to clone, so the runner rehydrates the copied transcript as a
    # noReply preamble — same path as a lost-session resume).
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    labels = snapshot.get("labels")
    fork_carry_history = (
        isinstance(labels, dict) and labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
    )
    return _OpenCodeNativeLaunchConfig(
        workspace=_codex_session_workspace(session_workspace),
        policy_server_url=_required_runner_env("RUNNER_SERVER_URL"),
        terminal_launch_args=terminal_launch_args,
        model_override=model_override,
        external_session_id=external_session_id,
        fork_carry_history=fork_carry_history,
    )


async def _auto_create_opencode_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, dict[str, Any]], None],
    *,
    agent_spec: Any | None = None,
    server_client: httpx.AsyncClient | None = None,
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
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
    :param ensure_comment_relay: Callback that starts the Omnigent builtin-tool
        relay for this session's bridge dir (the nested
        ``_ensure_comment_relay_started``). ``None`` skips wiring the Omnigent
        MCP relay (tests / no server).
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
        write_opencode_policy_plugin,
        write_relay_bridge_config,
    )
    from omnigent.opencode_native_forwarder import OpenCodeNativeForwarder

    launch_config = await _opencode_native_launch_config(
        session_id=session_id,
        server_client=server_client,
    )
    workspace = str(launch_config.workspace)
    bridge_dir = prepare_bridge_dir(session_id)
    # Seed the token the shared ``serve-mcp`` reads at boot (idempotent) so the
    # Omnigent builtin-tool relay (wired below) can start. Safe to call before
    # the relay; ``start_tool_relay`` mints its own relay token in
    # ``tool_relay.json``.
    write_relay_bridge_config(bridge_dir)
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
        build_opencode_mcp_block,
        build_opencode_model_default_config,
        build_opencode_omnigent_mcp_server,
        build_opencode_provider_config,
        maybe_merge_user_provider_config,
        resolve_databricks_gateway,
        write_opencode_provider_config,
    )

    # Accumulate the synthesized opencode.json: provider/model (Databricks
    # gateway or a pinned default) + the agent's MCP servers + force-ask.
    config: dict[str, object] = {}
    gateway = resolve_databricks_gateway(
        _opencode_native_profile_from_spec(agent_spec), model_id=model_override
    )
    if gateway is not None:
        # Pin the per-prompt model to the synthesized provider/endpoint id, and
        # write it as opencode's default model too so the TUI launches on it.
        model_override = gateway.qualified_model
        config = dict(build_opencode_provider_config(gateway))
        config["model"] = model_override
    elif model_override:
        # No custom provider, but a model is pinned (``omni opencode --model`` or
        # the ``omni setup`` OpenCode default): write opencode's default model so
        # the native TUI and the first turn use it instead of ``opencode/big-pickle``.
        # OpenCode resolves the provider from the model-id prefix against its own
        # auth.json, so no provider block is needed.
        config = dict(build_opencode_model_default_config(model_override))

    # Build opencode's ``mcp`` block: the Omnigent builtin-tool relay (so the
    # model can call sys_*/load_skill/web_fetch — the real "connects to Omnigent
    # MCP") PLUS the agent's own declared MCP servers (translated into opencode's
    # config). The relay is added only when we'll actually start it below
    # (``ensure_comment_relay`` present), else serve-mcp would launch with no
    # tool_relay.json to read. Force every tool call to prompt so it routes
    # through Omnigent's policy engine via the forwarder's permission gate —
    # opencode's enforcement is reactive (no pre-tool hook), so "ask" is what
    # makes the policy verdicts apply to MCP (and other) tools.
    mcp_block = build_opencode_mcp_block(_opencode_native_mcp_servers_from_spec(agent_spec))
    if server_client is not None and ensure_comment_relay is not None:
        mcp_block.update(build_opencode_omnigent_mcp_server(bridge_dir))
    if mcp_block:
        config.setdefault("$schema", "https://opencode.ai/config.json")
        config["mcp"] = mcp_block
        config["permission"] = "ask"

    # Load the Omnigent policy-bridge plugin so opencode's lifecycle hooks reach
    # the policy engine at phases the reactive permission.asked path can't:
    # REQUEST (gate TUI-typed prompts at submit) and TOOL_RESULT (gate/redact
    # tool output). The plugin POSTs PHASE_REQUEST / PHASE_TOOL_RESULT to
    # ``/policies/evaluate`` (same contract as claude's UserPromptSubmit /
    # PostToolUse hooks); coordinates come from the OMNIGENT_* env stamped on
    # the server below. Only wired when there's a server to evaluate against.
    policy_env: dict[str, str] = {}
    runner_server_url = os.environ.get("RUNNER_SERVER_URL")
    if server_client is not None and runner_server_url:
        plugin_path = write_opencode_policy_plugin(bridge_dir)
        config.setdefault("$schema", "https://opencode.ai/config.json")
        config["plugin"] = [str(plugin_path)]
        policy_env["OMNIGENT_POLICY_URL"] = runner_server_url
        policy_env["OMNIGENT_SESSION_ID"] = session_id
        # One-shot auth-token snapshot (mirrors codex's policy_hook.json /
        # cost-popup). Long-session staleness degrades to fail-open (no
        # enforcement), like codex; a refreshable token file is the follow-up.
        from omnigent.runner._entry import _make_auth_token_factory

        _policy_factory = _make_auth_token_factory()
        _policy_token = _policy_factory() if _policy_factory is not None else None
        if _policy_token:
            from omnigent.cli_auth import databricks_request_headers

            # Bake the FULL routing header map (bearer + workspace / deployment
            # selectors), not a bare bearer: the plugin POSTs /policies/evaluate
            # to the omnigent server out-of-process, so without the selectors it
            # could land on a different server instance than the runner's.
            policy_env["OMNIGENT_POLICY_HEADERS"] = json.dumps(
                databricks_request_headers(runner_server_url, bearer_token=_policy_token)
            )

    # Merge the user's global provider definitions (e.g. OpenAI-compatible
    # endpoints with custom base URLs) into the synthesized config so the
    # spawned server sees both. The per-session XDG_CONFIG_HOME override
    # hides the user's ~/.config/opencode/opencode.jsonc, so without this
    # merge, custom providers with non-default base URLs are invisible.
    config = maybe_merge_user_provider_config(config)

    if config:
        write_opencode_provider_config(xdg_config_home_for_bridge_dir(bridge_dir), config)

    # The server runs with a per-session XDG_DATA_HOME, so copy the user's
    # `opencode auth login` credentials in — otherwise it can't authenticate
    # their providers and falls back to the no-auth default model. No-op on a
    # remote runner (no local auth.json) / Databricks-gateway path.
    seed_opencode_auth(bridge_dir)

    # Start the Omnigent builtin-tool relay BEFORE opencode boots, so
    # ``tool_relay.json`` exists when opencode launches the ``serve-mcp`` MCP
    # server and lists its tools (the sys_*/load_skill/web_fetch surface). The
    # relay POSTs each call back through the Omnigent server (policy enforced).
    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    server = OpenCodeNativeServer(
        bridge_dir=bridge_dir,
        workspace=launch_config.workspace,
        extra_env=policy_env or None,
    )
    await server.start()
    _AUTO_OPENCODE_SERVERS[session_id] = server

    try:
        client = server.client()
        try:
            opencode_session_id: str | None = None
            resume_lost_history = False
            if launch_config.external_session_id is not None:
                existing = await client.get_session(launch_config.external_session_id)
                if existing is not None:
                    opencode_session_id = existing.id
                else:
                    # The persisted opencode session is gone (new host / wiped
                    # XDG store) — we'll rehydrate from the Omnigent transcript
                    # below instead of silently starting empty.
                    resume_lost_history = True
            if opencode_session_id is None:
                created = await client.create_session({"title": f"omnigent:{session_id}"})
                opencode_session_id = created.id
                # Rehydrate prior context (text-prefix replay) when this is a
                # lost-session resume OR a forked clone carrying history — both
                # seed the copied Omnigent transcript as a noReply preamble.
                if resume_lost_history or launch_config.fork_carry_history:
                    await _rehydrate_opencode_session_from_transcript(
                        opencode_client=client,
                        opencode_session_id=opencode_session_id,
                        omnigent_session_id=session_id,
                        server_client=server_client,
                        model_override=model_override,
                    )
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


def _resolve_opencode_compact_model(
    session: Any,
    messages: list[dict[str, Any]],
    model_override: str | None,
) -> tuple[str | None, str | None]:
    """
    Resolve the ``(provider_id, model_id)`` for an opencode ``/summarize``.

    opencode's ``/summarize`` requires an explicit model, but Omnigent
    creates the session WITHOUT one (the model is pinned per prompt), so
    ``session.raw["model"]`` is usually absent. Resolve it from a
    most-authoritative-first fallback chain:

    1. The most-recent assistant message carries the live model on its
       ``info`` as ``providerID`` + ``modelID`` (the MESSAGE keys). Iterate
       in reverse for the last ``info.role == "assistant"`` with both set.
    2. Else the session ``model`` field (covers create-with-model / TUI
       switchModel) — on the SESSION object the keys are ``providerID`` +
       ``id`` (NOT ``modelID``).
    3. Else ``model_override`` from bridge state, a qualified
       ``"provider/model"`` string split on the FIRST ``/``.

    :param session: The :class:`OpenCodeSession` (``.raw`` is the payload),
        or ``None``.
    :param messages: The session's messages, each ``{"info": ..., "parts": ...}``.
    :param model_override: Bridge-state ``model_override`` (qualified
        ``provider/model``), or ``None``.
    :returns: ``(provider_id, model_id)``; both ``None`` when unresolved.
    """
    # 1. The latest assistant message's live model (message keys:
    #    ``providerID`` + ``modelID``).
    for message in reversed(messages):
        info = message.get("info") if isinstance(message, dict) else None
        if not isinstance(info, dict) or info.get("role") != "assistant":
            continue
        provider_id = info.get("providerID")
        model_id = info.get("modelID")
        if isinstance(provider_id, str) and provider_id and isinstance(model_id, str) and model_id:
            return provider_id, model_id

    # 2. The session ``model`` field (session keys: ``providerID`` + ``id``).
    model = session.raw.get("model") if session is not None else None
    if isinstance(model, dict):
        provider_id = model.get("providerID")
        model_id = model.get("id")
        if isinstance(provider_id, str) and provider_id and isinstance(model_id, str) and model_id:
            return provider_id, model_id

    # 3. Bridge-state ``model_override`` (``provider/model``, split on first ``/``).
    if isinstance(model_override, str) and "/" in model_override:
        provider_id, _, model_id = model_override.partition("/")
        if provider_id and model_id:
            return provider_id, model_id

    return None, None


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


def _opencode_native_mcp_servers_from_spec(agent_spec: Any | None) -> list[Any]:
    """
    Return the resolved agent spec's MCP server declarations (or empty).

    :param agent_spec: Optional resolved agent spec.
    :returns: The spec's ``mcp_servers`` list, or ``[]``.
    """
    if agent_spec is None:
        return []
    try:
        spec = getattr(agent_spec, "spec", agent_spec)
        return list(getattr(spec, "mcp_servers", []) or [])
    except Exception:  # noqa: BLE001 - best effort.
        return []


def _render_opencode_transcript_text(items: list[Any]) -> str:
    """
    Render committed Omnigent message items into a plain-text transcript.

    Used for opencode resume's text-prefix replay. Extracts user/assistant
    text from ``GET /v1/sessions/{id}/items`` message items.

    :param items: Raw API items.
    :returns: A ``"User: …\\n\\nAssistant: …"`` transcript, or ``""``.
    """
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, list):
            continue
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]
        ]
        if texts:
            lines.append(f"{role.capitalize()}: " + "\n".join(texts))
    return "\n\n".join(lines)


async def _rehydrate_opencode_session_from_transcript(
    *,
    opencode_client: Any,
    opencode_session_id: str,
    omnigent_session_id: str,
    server_client: Any | None,
    model_override: str | None,
) -> bool:
    """
    Seed a fresh opencode session with prior context (text-prefix replay).

    opencode has no history-import API, so on a cross-host resume (where the
    persisted opencode session is gone) inject the Omnigent transcript as a
    single ``noReply`` context message — the agent resumes with its prior
    context instead of silent amnesia. Best-effort: returns ``False`` when the
    transcript can't be fetched or is empty.

    :returns: ``True`` when prior context was seeded.
    """
    if server_client is None:
        return False
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(omnigent_session_id, safe='')}/items",
            params={"limit": 1000, "order": "asc"},
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        _logger.warning(
            "opencode resume: could not fetch transcript for %s",
            omnigent_session_id,
            exc_info=True,
        )
        return False
    items = payload.get("data", []) if isinstance(payload, dict) else []
    transcript = _render_opencode_transcript_text(items if isinstance(items, list) else [])
    if not transcript:
        return False
    provider_id: str | None = None
    model_id: str | None = None
    if model_override and "/" in model_override:
        provider_id, model_id = model_override.split("/", 1)
    text = (
        "[Resumed session — the prior opencode session was unavailable on this "
        "host, so the earlier conversation is included below for context. Treat "
        "it as history; do not re-run prior actions.]\n\n" + transcript
    )
    try:
        await opencode_client.seed_context(
            opencode_session_id, text, provider_id=provider_id, model_id=model_id
        )
    except Exception:  # noqa: BLE001 - rehydration is best effort.
        _logger.warning(
            "opencode resume: rehydration seed failed for %s", omnigent_session_id, exc_info=True
        )
        return False
    return True


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
    approve: bool = False,
) -> list[str]:
    """
    Build Pi CLI args for a runner-owned native TUI session.

    :param terminal_launch_args: User pass-through Pi args.
    :param extension_path: Generated Omnigent Pi extension path.
    :param session_dir: Per-Omnigent-session Pi session directory.
    :param external_session_id: Captured Pi session id, if any.
    :param approve: When ``True``, pass ``--approve`` to pre-accept Pi's
        project-folder trust dialog (supported from Pi 0.79+).
    :returns: Complete Pi arg vector excluding the executable.
    """
    user_args = list(terminal_launch_args or [])
    args = ["--extension", str(extension_path)]
    if approve:
        # Pre-accept the project-folder trust dialog. Pi 0.79+ shows a
        # blocking TUI prompt on first launch in a directory with .pi/
        # resources. In a web-UI-driven session there is nobody at the
        # terminal to answer it — mirroring ensure_claude_workspace_trusted.
        args.append("--approve")
    if not _pi_args_have_session_control(user_args):
        args.extend(["--session-dir", str(session_dir)])
        if external_session_id:
            args.extend(["--session", external_session_id])
    args.extend(user_args)
    return args


async def _resolve_pi_resume_session(
    *,
    session_id: str,
    launch_config: _PiNativeLaunchConfig,
    session_dir: Path,
    workspace: Path,
    server_client: httpx.AsyncClient | None,
) -> str | None:
    """
    Ensure Pi has a local session JSONL and return the id to launch with.

    Three cases, mirroring claude-native / codex-native fork+resume:

    1. **Cold resume** — the session already carries a captured Pi
       ``external_session_id`` but the local session file may be missing
       (cross-machine, a fresh runner, or a cleared bridge dir). Synthesize the
       file from committed Omnigent items so ``pi --session <id>`` opens with
       prior context. An existing file is reused untouched.
    2. **Fork rebuild** — a forked clone bound to a pi-native target with NO
       captured session of its own and a carry-history marker: mint a new Pi
       session id, build its file from the clone's OWN copied Omnigent items,
       and patch the server so Omnigent reflects the clone's session id and a
       later relaunch resumes it via case 1.
    3. **Fresh / nothing to carry** — return ``None`` so Pi launches a brand
       new session.

    Best-effort: on any failure we return the (possibly ``None``) captured id
    so Pi launches fresh rather than pointing ``--session`` at a file that does
    not exist.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param launch_config: Resolved Pi launch config (carries the captured id
        and fork directives).
    :param session_dir: Directory passed to ``pi --session-dir``.
    :param workspace: Resolved cwd Pi will run in.
    :param server_client: Runner Omnigent server client.
    :returns: Pi session id to launch with via ``--session``, or ``None`` to
        launch fresh.
    """
    if server_client is None:
        return launch_config.external_session_id

    from omnigent.pi_native_resume import ensure_local_pi_resume_session, mint_pi_session_id

    # Resolve the provider's model only for the synthesized assistant records'
    # informational ``model`` field; Pi's resume uses the live provider, so a
    # missing model is harmless.
    model = ""
    try:
        from omnigent.pi_native_credentials import resolve_pi_native_provider

        provider = resolve_pi_native_provider()
        if provider is not None and getattr(provider, "model", None):
            model = provider.model
    except Exception:  # noqa: BLE001 — informational only; never block launch
        model = ""

    # Case 1: cold resume of a session that already has a captured Pi id.
    if launch_config.external_session_id is not None:
        built: Path | None = None
        try:
            built = await ensure_local_pi_resume_session(
                server_client,
                session_id=session_id,
                external_session_id=launch_config.external_session_id,
                session_dir=session_dir,
                workspace=workspace,
                model=model,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            built = None
            _logger.warning(
                "Could not synthesize Pi resume session for %s; launching fresh",
                session_id,
                exc_info=True,
            )
        # Only launch with ``--session <id>`` when a session file actually
        # exists/was written. ``ensure_local_pi_resume_session`` returns
        # ``None`` when nothing resumable was produced (missing/cleared bridge
        # dir, empty history, or a transient fetch/write failure caught above).
        # Returning the captured id regardless would emit ``pi --session <id>``
        # for a file that does not exist — Pi then exits instead of launching,
        # defeating the best-effort fallback this function promises. Fall back
        # to a fresh session (return ``None``) in that case.
        if built is None:
            _logger.info(
                "Pi cold-resume produced no local session file for %s; launching fresh",
                session_id,
            )
            return None
        return launch_config.external_session_id

    # Case 2: forked clone bound to a pi-native target with no captured session
    # yet. Build the clone's session from its OWN copied Omnigent items under a
    # minted id. (A same-provider source's captured id, when present, is stamped
    # as fork_source_external_id; but Pi session files are runner-local and the
    # clone has its OWN copied items, so we rebuild from items either way —
    # there is no cross-session "resume the source's file" like codex's clone.)
    if launch_config.fork_carry_history:
        minted = mint_pi_session_id()
        try:
            built = await ensure_local_pi_resume_session(
                server_client,
                session_id=session_id,
                external_session_id=minted,
                session_dir=session_dir,
                workspace=workspace,
                model=model,
            )
        except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
            built = None
            _logger.warning(
                "Could not build Pi session from items for forked clone %s; launching fresh",
                session_id,
                exc_info=True,
            )
        _logger.info(
            "Pi terminal fork-rebuild decision: session=%s minted=%s built=%s",
            session_id,
            minted,
            str(built) if built is not None else None,
        )
        if built is not None:
            # Record the minted id so Omnigent reflects the clone's own Pi
            # session and a later relaunch resumes it via case 1. Best-effort:
            # the extension also re-captures the id on session_start, so a
            # failed patch is recovered then.
            try:
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"external_session_id": minted},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                _logger.warning(
                    "Could not pre-set external_session_id for forked Pi clone %s; "
                    "relying on extension capture",
                    session_id,
                    exc_info=True,
                )
            return minted

    return None


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
    # Route the extension's out-of-process POSTs (/events, /mcp,
    # /policies/evaluate) through the shared header builder so they carry the
    # workspace / deployment routing selectors, not just a bare bearer. A bare
    # bearer skips those selectors and can land on a different server instance
    # than the one the runner (and the web UI) are on, so live-streamed items
    # never reach the browser's in-process event stream (they only appear on reload).
    from omnigent.cli_auth import databricks_request_headers

    auth_headers = databricks_request_headers(launch_config.server_url, bearer_token=auth_token)
    # Build the Omnigent tool surface (sys_* tools) the Pi extension registers
    # via pi.registerTool. Reuses the same schema set the claude-native /
    # codex-native relay advertises, gated by the session's spec. Each tool's
    # execute() round-trips through POST /v1/sessions/{id}/mcp, so the Pi agent
    # can call Omnigent tools with centralized server-side policy enforcement
    # — parity with the other native harnesses. Best-effort: a schema-build
    # failure must not block the terminal launch, so fall back to no tools.
    pi_tools: list[dict[str, Any]] = []
    try:
        from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas

        spec_for_tools = _unwrap_resolved_spec(agent_spec)
        pi_tools = build_native_relay_tool_schemas(spec_for_tools)
    except Exception:  # noqa: BLE001 — tool registration is additive
        _logger.warning(
            "Failed to build pi-native tool schemas for session %s; "
            "Pi will run with its built-in tools only",
            session_id,
            exc_info=True,
        )
    _extension, config = write_extension_files(
        bridge_dir,
        session_id=session_id,
        server_url=launch_config.server_url,
        conversation_url=conversation_url(launch_config.server_url, session_id),
        auth_headers=auth_headers,
        tools=pi_tools,
    )
    pi_command = resolve_pi_executable()
    # Rebuild the local Pi session JSONL from committed Omnigent items so a
    # cold-resume or fork opens with prior conversation context (parity with
    # claude-native / codex-native). Returns the id to launch with via
    # ``--session`` (the captured id, a minted fork id, or None for fresh).
    resume_session_id = await _resolve_pi_resume_session(
        session_id=session_id,
        launch_config=launch_config,
        session_dir=session_dir,
        workspace=launch_config.workspace,
        server_client=server_client,
    )
    from omnigent.pi_native import pi_supports_approve

    pi_args = _build_pi_native_args(
        terminal_launch_args=launch_config.terminal_launch_args,
        extension_path=pi_extension,
        session_dir=session_dir,
        external_session_id=resume_session_id,
        approve=pi_supports_approve(pi_command),
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

        # Thread the agent spec's pinned model (``executor.model``) into the
        # resolved provider so the generated ``models.json`` — and the
        # appended ``--model`` arg (see ``pi_native_provider_launch``) — select
        # it, reaching parity with claude-native / cursor-native. ``None``
        # (no model declared) keeps the provider's default model.
        # model_override (set by /model or sys_session_create's model arg)
        # takes precedence over the spec's pinned executor.model.
        spec_model = launch_config.model_override or _pi_native_model_from_spec(agent_spec)
        provider = resolve_pi_native_provider(model=spec_model)
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
    from omnigent.cursor_native_status import clear_cursor_status_state
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
        # Likewise drop the turn-end marker + idle poster state so a stale count
        # from a prior terminal can't make the new forwarder skip (or re-fire)
        # the ``external_session_status: idle`` parent-wake edge.
        clear_cursor_status_state(bridge_dir)
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
    from omnigent.hermes_native_bridge import (
        bridge_dir_for_session_id,
        read_hermes_home,
        write_policy_hook_config,
        write_tmux_target,
    )
    from omnigent.hermes_native_forwarder import clear_hermes_bridge_state
    from omnigent.hermes_native_status import clear_hermes_status_state

    bridge_dir = bridge_dir_for_session_id(session_id)
    clear_hermes_bridge_state(bridge_dir)
    # Likewise drop the idle poster state so a stale posted-count from a prior
    # terminal can't make the new forwarder skip (or re-fire) the
    # ``external_session_status: idle`` parent-wake edge.
    clear_hermes_status_state(bridge_dir)

    # Write a per-session HERMES_HOME with the Omnigent policy hook so the
    # native TUI evaluates tool calls against Omnigent policies.
    _hermes_server_url = _required_runner_env("RUNNER_SERVER_URL")
    write_policy_hook_config(bridge_dir, _hermes_server_url, session_id)

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
    # Resolve the per-session HERMES_HOME early: the fork block below needs it
    # to place the cloned state.db, and the env block after needs it for the
    # HERMES_HOME env var.
    _hermes_home_path = read_hermes_home(bridge_dir)
    # Fork with history: clone the source Hermes session's state.db into the
    # new session's HERMES_HOME so the TUI loads the prior conversation context
    # under a fresh session id (true fork, not a shared --resume).
    if launch_config.fork_carry_history and launch_config.fork_source_external_id:
        from omnigent.hermes_native_bridge import (
            clone_hermes_session,
            mint_hermes_session_id,
        )

        # Resolve the source session's state.db from its bridge dir.
        _source_bridge = (
            bridge_dir_for_session_id(launch_config.fork_source_id)
            if launch_config.fork_source_id
            else None
        )
        _source_hermes_home = read_hermes_home(_source_bridge) if _source_bridge else None
        _source_db = _source_hermes_home / "state.db" if _source_hermes_home else None
        if _source_db is not None and _source_db.is_file():
            _target_session_id = mint_hermes_session_id()
            _target_db = _hermes_home_path / "state.db" if _hermes_home_path else None
            if _target_db is not None:
                try:
                    _clone_max_id = await asyncio.to_thread(
                        clone_hermes_session,
                        _source_db,
                        _target_db,
                        launch_config.fork_source_external_id,
                        _target_session_id,
                        workspace=workspace,
                    )
                    hermes_args.extend(["--resume", _target_session_id])
                    # Pre-seed the forwarder cursor past cloned messages so
                    # the forwarder only mirrors NEW messages (Omnigent already
                    # has the cloned ones from the fork item copy).
                    if _clone_max_id > 0:
                        from omnigent.hermes_native_forwarder import (
                            _ForwardState,
                            _write_state,
                        )

                        _write_state(
                            bridge_dir,
                            _ForwardState(
                                hermes_session_id=_target_session_id,
                                last_id=_clone_max_id,
                                launch_epoch_s=launch_epoch_s,
                            ),
                        )
                    _logger.info(
                        "Cloned hermes session %s -> %s for fork; session=%s",
                        launch_config.fork_source_external_id,
                        _target_session_id,
                        session_id,
                    )
                except Exception:  # noqa: BLE001
                    _logger.warning(
                        "Failed to clone hermes session for fork; launching fresh; session=%s",
                        session_id,
                        exc_info=True,
                    )
                    # Remove broken state.db so Hermes starts fresh.
                    if _target_db.exists():
                        _target_db.unlink()
    # If a per-session HERMES_HOME was written (policy hook), pass it via env
    # so the TUI picks up the hook config alongside its own approval prompt.
    _hermes_terminal_env: dict[str, str] = {}
    if _hermes_home_path is not None:
        _hermes_terminal_env["HERMES_HOME"] = str(_hermes_home_path)
    terminal_view = await resource_registry.launch_required_terminal(
        session_id=session_id,
        terminal_name="hermes",
        session_key="main",
        resource_role=HERMES_NATIVE_TERMINAL_ROLE,
        spec=TerminalEnvSpec(
            os_env=OSEnvSpec(type="caller_process", cwd=workspace),
            command=hermes_command,
            args=hermes_args,
            env=_hermes_terminal_env,
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

    from omnigent.hermes_native_bridge import read_hermes_home
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
        # When a per-session HERMES_HOME is configured (policy hooks / MCP),
        # Hermes writes its state.db there, not ~/.hermes.  Point the
        # forwarder at the right database so it can discover the session.
        _hermes_home = read_hermes_home(bridge_dir)
        _state_db = _hermes_home / "state.db" if _hermes_home is not None else None
        await asyncio.gather(
            supervise_hermes_forwarder(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="hermes-native-ui",
                workspace=workspace,
                launch_epoch_s=launch_epoch_s,
                db_path=_state_db,
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
    ensure_comment_relay: Callable[..., Awaitable[None]] | None = None,
) -> SessionResourceView:
    """Auto-create the Kiro TUI terminal for a kiro-native session."""
    from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
    from omnigent.kiro_native import build_kiro_launch
    from omnigent.kiro_native_bridge import (
        KIRO_NATIVE_ENV_UNSET,
        build_kiro_native_terminal_env,
        prepare_bridge_dir,
        write_kiro_workspace_mcp_config,
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
    # Declare the Omnigent MCP server in the workspace-scoped kiro config so
    # kiro-cli can call Omnigent tools. Only when the tool relay will actually
    # start (server_client + ensure_comment_relay present), else serve-mcp would
    # launch with no relay to route calls back to. Mirrors cursor-native.
    if server_client is not None and ensure_comment_relay is not None:
        write_kiro_workspace_mcp_config(workspace_path, bridge_dir)
    kiro_launch = build_kiro_launch(
        launch_config.terminal_launch_args or [],
        resume_id=launch_config.external_session_id,
        model=launch_config.model_override,
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

    # Start the Omnigent builtin-tool relay (writes tool_relay.json into the kiro
    # bridge dir) so the serve-mcp server declared in the workspace mcp.json can
    # route Omnigent tool calls back through the session's policy/elicitation
    # gate. Mirrors cursor-native.
    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    from omnigent.kiro_native_permissions import supervise_kiro_permission_mirror
    from omnigent.kiro_native_session_forwarder import supervise_kiro_session_forwarder

    async def _supervise_kiro_native_bridges() -> None:
        """Run the Kiro transcript forwarder and permission mirror together."""
        await asyncio.gather(
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
            supervise_kiro_permission_mirror(
                base_url=server_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                auth=_runner_auth,
            ),
        )

    _forwarder_task = asyncio.create_task(
        _supervise_kiro_native_bridges(),
        name=f"kiro-bridges-{session_id}",
    )
    _register_auto_forwarder_task(session_id, _forwarder_task)
    _logger.info(
        "Auto-created kiro terminal + forwarder/permission-mirror for session %s; task=%s",
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


async def _build_qwen_fork_recording(
    server_client: httpx.AsyncClient,
    *,
    session_id: str,
    workspace: str,
) -> str | None:
    """Synthesize a qwen chat recording for a forked clone from its Omnigent items.

    A forked clone has its OWN copied Omnigent items but no qwen recording yet
    (``external_session_id`` is NULL on a fork). We rebuild a recording from those
    items under the clone's deterministic session id so the TUI resumes with the
    prior conversation. The rebuild reads harness-neutral items (not the source's
    vendor transcript), so it works cross-harness (claude/pi/codex → qwen).

    If a recording for the clone's id already exists, return the id WITHOUT
    rebuilding — the rebuild is idempotent. Otherwise a relaunch after a failed
    ``external_session_id`` persist (best-effort; qwen has no re-capture path)
    would re-enter here and overwrite qwen's live, full-fidelity recording with
    a text-only rebuild.

    :param server_client: Runner Omnigent server client.
    :param session_id: The forked clone's Omnigent conversation id.
    :param workspace: Realpath'd cwd qwen will resume in.
    :returns: The qwen session id to ``--resume``, or ``None`` when there's
        nothing carryable or the build fails (caller then launches fresh).
    """
    from omnigent.pi_native_resume import fetch_all_session_items_for_pi_resume
    from omnigent.qwen_native_bridge import (
        qwen_session_id_for_conversation,
        qwen_session_recording_exists,
        qwen_session_records_from_session_items,
        write_qwen_session_recording,
    )

    qwen_session_id = qwen_session_id_for_conversation(session_id)
    # Already built (e.g. a relaunch after the external_session_id persist failed):
    # resume the live recording, never clobber it with a fresh text-only rebuild.
    if qwen_session_recording_exists(qwen_session_id, workspace):
        _logger.info(
            "qwen fork-rebuild: recording already present for clone %s; resuming it",
            session_id,
        )
        return qwen_session_id
    try:
        items = await fetch_all_session_items_for_pi_resume(server_client, session_id)
        records = qwen_session_records_from_session_items(
            items,
            qwen_session_id=qwen_session_id,
            cwd=workspace,
        )
        if not records:
            _logger.info(
                "qwen fork-rebuild: no carryable items for clone %s; launching fresh",
                session_id,
            )
            return None
        recording = await asyncio.to_thread(
            write_qwen_session_recording, qwen_session_id, workspace, records
        )
    except Exception:  # noqa: BLE001 — best-effort; launch fresh on failure
        _logger.warning(
            "Could not build qwen recording from items for forked clone %s; launching fresh",
            session_id,
            exc_info=True,
        )
        return None
    _logger.info(
        "qwen fork-rebuild: session=%s qwen_session_id=%s recording=%s records=%d",
        session_id,
        qwen_session_id,
        recording,
        len(records),
    )
    return qwen_session_id


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
        write_mcp_config,
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
    # Forked clone carrying history into qwen: rebuild a recording from the
    # clone's copied Omnigent items and force ``--resume``. Gated on a NULL
    # ``external_session_id`` so it normally runs only on the FIRST launch;
    # ``_build_qwen_fork_recording`` is also idempotent (resumes an existing
    # recording, never clobbers it). Mirrors pi-native's fork rebuild
    # (``_resolve_pi_external_session_id`` case 2).
    forked_qwen_session_id: str | None = None
    if (
        launch_config.fork_carry_history
        and not launch_config.external_session_id
        and server_client is not None
    ):
        forked_qwen_session_id = await _build_qwen_fork_recording(
            server_client,
            session_id=session_id,
            workspace=workspace,
        )

    if forked_qwen_session_id is not None:
        qwen_session_id = forked_qwen_session_id
        resume_args = ["--resume", qwen_session_id]
        # Record the id so the clone reflects its own qwen session and later
        # relaunches resume it via the normal path instead of rebuilding.
        await _persist_qwen_external_session_id(server_client, session_id, qwen_session_id)
    else:
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

    # Expose Omnigent's builtin tools (sys_*, load_skill, web_fetch, …) to qwen
    # via the shared MCP relay, passed through qwen's ``--mcp-config`` flag (the
    # claude-native model). The config lives in the bridge dir — never the
    # workspace — so we drop no file in the user's repo and concurrent
    # same-workspace sessions can't collide; CLI-provided servers are also ungated
    # (no "Untrusted MCP server" prompt), so no pre-approval step is needed.
    # Written before launch so the relay's ``bridge.json`` token exists when qwen
    # spawns ``serve-mcp``; the live tool surface is advertised by the
    # ``tool_relay.json`` that ``ensure_comment_relay`` writes below. Only when the
    # relay will actually start (``ensure_comment_relay`` present), else the
    # registered tools would be dead (serve-mcp with nothing to route calls back
    # to) — mirrors the opencode-native gating.
    mcp_enabled = server_client is not None and ensure_comment_relay is not None
    mcp_args: list[str] = []
    if mcp_enabled:
        try:
            mcp_config = write_mcp_config(bridge_dir)
        except RuntimeError:
            # The bridge dir failed owner-only validation (e.g. a redirected
            # ancestor on a shared host) — don't write the relay token there.
            # Degrade to no MCP rather than crash the session; the relay's own
            # secure-dir check would reject it later too.
            mcp_enabled = False
            _logger.warning(
                "qwen-native: bridge dir failed secure validation; skipping "
                "Omnigent MCP wiring for session %s.",
                session_id,
                exc_info=True,
            )
        else:
            mcp_args = ["--mcp-config", str(mcp_config)]

    # The dual-output + input-file flags wire qwen to the bridge; any user
    # ``terminal_launch_args`` (e.g. ``-m <model>``) precede them. Approval stays
    # the default in-terminal prompt (the embedded pane shows it) — Omnigent-side
    # gating via ``confirmation_response`` is a follow-up (see design doc).
    qwen_args = [
        *(launch_config.terminal_launch_args or []),
        *resume_args,
        *mcp_args,
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

    from omnigent.qwen_native_bridge import qwen_session_recording_path
    from omnigent.qwen_native_forwarder import (
        supervise_qwen_compaction_mirror,
        supervise_qwen_forwarder,
    )
    from omnigent.qwen_native_permissions import supervise_qwen_approval_mirror

    qwen_recording_path = qwen_session_recording_path(qwen_session_id, workspace)

    if server_client is not None and ensure_comment_relay is not None:
        await ensure_comment_relay(
            session_id,
            explicit_bridge_dir=bridge_dir,
            await_notify=False,
        )

    async def _supervise_qwen_native_bridges() -> None:
        """Run the transcript forwarder, approval mirror, and compaction mirror together.

        All three are per-session, runner-owned, and self-healing (they catch and
        log their own failures rather than exiting); gathering them under one
        task keeps a single registration/cancellation handle
        (:func:`_register_auto_forwarder_task`) for session teardown. The
        forwarder mirrors qwen's replies onto the conversation; the approval
        mirror surfaces qwen's native ``can_use_tool`` prompts as web
        elicitations (see :mod:`omnigent.qwen_native_permissions`); the compaction
        mirror tails qwen's chat recording for the ``chat_compression`` marker and
        posts the ``external_compaction_status: completed`` edge (see
        :func:`omnigent.qwen_native_forwarder.supervise_qwen_compaction_mirror`).
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
            supervise_qwen_compaction_mirror(
                base_url=server_url,
                headers={},
                session_id=session_id,
                recording_path=qwen_recording_path,
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
    # The hook subprocess replays these static headers from its config (no
    # refresh-capable httpx.Auth of its own); the helper pairs the bearer with
    # the workspace-routing header so neither is dropped.
    from omnigent.cli_auth import databricks_request_headers

    _runner_headers = databricks_request_headers(server_url, bearer_token=_auth_token)
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
    # CUJ 1 same-user); otherwise the item-history fallback below runs. This
    # mirrors the claude-native fork-resume branch in
    # _auto_create_claude_terminal. See designs/FORK_SESSION_UX.md.
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
        except Exception:  # noqa: BLE001 — best-effort; fall back to stored items
            cloned_rollout = None
            _logger.warning(
                "Could not clone source rollout for forked codex clone %s; "
                "trying item-history fallback",
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
    if (
        launch_config.external_session_id is None
        and launch_config.fork_carry_history
        and server_client is not None
    ):
        # Forked clone bound to a codex-native target with no source rollout
        # available: build the clone's rollout from its own copied Omnigent
        # items under a thread id we mint, then flip launch_config so the
        # resume path below launches ``codex resume <our_thread_id>``. Reuses
        # the same server-items→rollout converter the cross-machine cold resume
        # uses, so the clone opens with the prior conversation as Codex context.
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
    # The codex policy hook subprocess replays these static headers from its
    # config (no refresh-capable auth of its own); the helper pairs the bearer
    # with the workspace-routing header so neither is dropped.
    from omnigent.cli_auth import databricks_request_headers

    policy_headers = databricks_request_headers(
        launch_config.policy_server_url, bearer_token=_policy_auth_token
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
        bypass_sandbox=launch_config.bypass_sandbox,
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
                    bypass_sandbox=launch_config.bypass_sandbox,
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
        agy_gemini_dir,
        agy_home_dir,
        clear_bridge_state,
        ensure_agy_feedback_survey_disabled,
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
    # agy has no --mcp-config flag and ignores ANTIGRAVITY_* env knobs. It does
    # accept the hidden --gemini_dir flag, so keep the process HOME real for auth
    # providers such as macOS Keychain, but point agy's config/state root at a
    # per-session isolated Gemini dir. This avoids clobbering the user's
    # interactive ~/.gemini/config/mcp_config.json and avoids the concurrency
    # footgun of one shared bridge-specific config file. The relay subprocess is
    # the same shared ``serve-mcp`` claude/codex/cursor use. Offloaded to a thread
    # (file I/O) and done BEFORE terminal launch so agy sees the config on its
    # first MCP scan.
    await asyncio.to_thread(write_mcp_config, bridge_dir)
    env_overrides = {
        **env_overrides,
        **await asyncio.to_thread(
            seed_isolated_agy_home,
            bridge_dir,
            trusted_workspace=workspace,
        ),
    }
    # agy's periodic feedback survey shares its "esc to cancel" footer with the
    # running-turn marker, so a web turn injected while it is up is misread as an
    # active turn and lost (#1494). Disable it before launch. agy now runs under
    # the real HOME with an isolated --gemini_dir, so the survey setting must be
    # written into that isolated dir (ensure_agy_feedback_survey_disabled appends
    # /.gemini/antigravity-cli/settings.json to its arg), NOT the user's real
    # HOME — env_overrides no longer carries a HOME key.
    await asyncio.to_thread(ensure_agy_feedback_survey_disabled, agy_home_dir(bridge_dir))
    argv = [argv[0], f"--gemini_dir={agy_gemini_dir(bridge_dir)}", *argv[1:]]
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


def _claude_native_model_from_spec(agent_spec: AgentSpec | ResolvedSpec | None) -> str | None:
    """
    Read the Claude Code model id to launch the native TUI with, from a spec.

    Reads the canonical ``spec.executor.model`` field (the same field the
    in-process claude-sdk harness consumes via ``_resolve_spec_model``). Unlike
    cursor-native, gateway-routed ``databricks-*`` ids are valid Claude Code
    models when the launch is wired through the Databricks AI gateway, so they
    are passed through.

    :param agent_spec: Agent spec object, or a resolved wrapper carrying a
        ``spec`` attribute. ``None`` means no spec was available.
    :returns: A Claude model id, e.g. ``"claude-sonnet-5"``, or ``None`` when
        the spec declares no model pin.
    """
    spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    if spec is None:
        return None
    model = spec.executor.model
    if not isinstance(model, str) or not model:
        return None
    return model


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


def _pi_native_model_from_spec(agent_spec: AgentSpec | ResolvedSpec | None) -> str | None:
    """
    Read the Pi model id to launch the native TUI with, from a spec.

    Reads the canonical ``spec.executor.model`` field (the same field the
    in-process harnesses and cursor-native consume). Unlike cursor-native,
    a gateway-routed id (``databricks-*``) IS usable here: the runner-owned
    Pi process routes through the Databricks AI Gateway, whose ``models.json``
    selects the model by its gateway id (see
    :func:`omnigent.pi_native_credentials.resolve_pi_native_provider`). The
    resolved model is threaded into ``resolve_pi_native_provider(model=...)``
    so the generated ``models.json`` (and the appended ``--model``) selects
    it.

    :param agent_spec: Agent spec object, or a resolved wrapper carrying a
        ``spec`` attribute. ``None`` means no spec was available.
    :returns: A model id, e.g. ``"databricks-claude-opus-4-7"``, or ``None``
        when the spec declares no model (Pi then uses the provider default).
    """
    spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    if spec is None:
        return None
    model = spec.executor.model
    return model if isinstance(model, str) and model else None


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


def _claude_terminal_env_unset(
    claude_config: ClaudeNativeUcodeConfig | None,
) -> list[str]:
    """
    Env vars to strip from a native Claude terminal child.

    Always drops ``DATABRICKS_CONFIG_PROFILE`` so the terminal's MCP
    servers don't inherit the runner's ambient Databricks profile and
    resolve auth against the wrong workspace.

    Always drops ``CLAUDECODE`` because Claude Code rejects any child launch
    carrying that nested-session marker, regardless of its auth mode. When the
    launch config carries an ``apiKeyHelper``, also drops the raw
    ``ANTHROPIC_API_KEY``: seeing both opens Claude Code's "Detected a custom
    API key" menu, whose selected row uses the same ``❯`` glyph the tmux
    delivery path waits for, so the first web message is typed into the menu.

    :param claude_config: The resolved native launch config, or ``None``
        (Claude's own login) — which still strips the nested-session marker.
    :returns: The env var names to unset, e.g.
        ``["DATABRICKS_CONFIG_PROFILE", "CLAUDECODE", "ANTHROPIC_API_KEY"]``.
    """
    env_unset = ["DATABRICKS_CONFIG_PROFILE", "CLAUDECODE"]
    if claude_config is not None and claude_config.api_key_helper:
        env_unset.append("ANTHROPIC_API_KEY")
    return env_unset


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


@dataclasses.dataclass(frozen=True)
class _ClaudeSessionLaunchMetadata:
    """Persisted values consumed by Claude terminal launch."""

    reasoning_effort: str | None = None
    model_override: str | None = None
    terminal_launch_args: list[str] | None = None
    external_session_id: str | None = None
    fork_source_external_id: str | None = None
    fork_carry_history: bool = False


def _claude_launch_metadata_from_envelope(
    session_init: RunnerSessionInitEnvelope,
) -> _ClaudeSessionLaunchMetadata:
    """Project Claude launch metadata without server callbacks."""
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    )

    snapshot = session_init.snapshot
    fork_source = snapshot.labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
    return _ClaudeSessionLaunchMetadata(
        reasoning_effort=snapshot.reasoning_effort,
        model_override=snapshot.model_override,
        terminal_launch_args=snapshot.terminal_launch_args,
        external_session_id=snapshot.external_session_id,
        fork_source_external_id=(
            fork_source if isinstance(fork_source, str) and fork_source else None
        ),
        fork_carry_history=snapshot.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1",
    )


async def _load_legacy_claude_launch_metadata(
    server_client: httpx.AsyncClient,
    session_id: str,
) -> _ClaudeSessionLaunchMetadata:
    """Fetch Claude launch metadata for servers predating the init envelope."""
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    )

    try:
        response = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.debug(
            "Could not fetch session launch config for %s; terminal will use Claude's defaults",
            session_id,
        )
        return _ClaudeSessionLaunchMetadata()
    if response.status_code != 200:
        return _ClaudeSessionLaunchMetadata()

    snapshot = response.json()
    effort = snapshot.get("reasoning_effort")
    model_override = snapshot.get("model_override")
    launch_args = snapshot.get("terminal_launch_args")
    external_session_id = snapshot.get("external_session_id")
    labels = snapshot.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    fork_source = labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
    metadata = _ClaudeSessionLaunchMetadata(
        reasoning_effort=effort if isinstance(effort, str) and effort else None,
        model_override=(
            model_override if isinstance(model_override, str) and model_override else None
        ),
        terminal_launch_args=(
            launch_args
            if isinstance(launch_args, list) and all(isinstance(arg, str) for arg in launch_args)
            else None
        ),
        external_session_id=(
            external_session_id
            if isinstance(external_session_id, str) and external_session_id
            else None
        ),
        fork_source_external_id=(
            fork_source if isinstance(fork_source, str) and fork_source else None
        ),
        fork_carry_history=labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1",
    )
    _logger.info(
        "Claude terminal launch config fetched: session=%s status=%s effort_set=%s "
        "model_override_set=%s launch_args_count=%d external_session_id_set=%s",
        session_id,
        response.status_code,
        metadata.reasoning_effort is not None,
        metadata.model_override is not None,
        len(metadata.terminal_launch_args or []),
        metadata.external_session_id is not None,
    )
    return metadata


async def _load_claude_launch_metadata(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
    session_init: RunnerSessionInitEnvelope | None,
) -> _ClaudeSessionLaunchMetadata:
    """Dispatch between the removable legacy and callback-free loaders."""
    if session_init is None:
        return await _load_legacy_claude_launch_metadata(server_client, session_id)
    metadata = _claude_launch_metadata_from_envelope(session_init)
    _logger.info(
        "Claude terminal launch config loaded from init envelope: session=%s "
        "effort_set=%s model_override_set=%s launch_args_count=%d "
        "external_session_id_set=%s",
        session_id,
        metadata.reasoning_effort is not None,
        metadata.model_override is not None,
        len(metadata.terminal_launch_args or []),
        metadata.external_session_id is not None,
    )
    return metadata


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
    session_init: RunnerSessionInitEnvelope | None = None,
    auth_token_factory: Callable[[], str | None] | None = None,
    resolve_launch_config: Callable[[], Awaitable[ClaudeNativeUcodeConfig | None]] | None = None,
    record_launch_config: Callable[[str, ClaudeNativeUcodeConfig | None], None] | None = None,
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
    :param session_init: Versioned server snapshot. ``None`` selects the
        isolated legacy callback path.
    :param auth_token_factory: Runner-owned refreshable bearer factory.
        ``None`` preserves direct-call behavior by resolving one locally.
    :param resolve_launch_config: Optional per-session resolver shared with
        the model-options endpoint so launch and UI use one catalog query.
    :param record_launch_config: Optional callback that stores the exact
        provider/model snapshot used for this session's launch.
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

    workspace = (
        session_init.snapshot.workspace
        if session_init is not None and session_init.snapshot.workspace
        else os.environ.get("OMNIGENT_RUNNER_WORKSPACE", str(Path.cwd()))
    )
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
    # Pick the bridge id this session's dir is keyed on. Normally session_id,
    # and we (re)assert the label = session_id so a STALE label from a rotation
    # that timed out before its terminal transfer can't make
    # _ensure_comment_relay_started write tool_relay.json to the wrong dir.
    #
    # EXCEPTION: a session superseded by /clear is deliberately re-keyed to
    # "{session_id}-cleared" (see _create_clear_replacement_session). Its natural
    # D(session_id) is the NEW session's live pane; resuming there would share
    # one transcript with two forwarders (duplicate items) and trip the
    # "no longer active after /clear" guard. So when the label is exactly that
    # marker, honour it and resume in the session's own isolated dir. The
    # executor spawn_env already resolves the same label, so the two agree.
    cleared_bridge_id = f"{session_id}-cleared"
    existing_bridge_id = await _claude_native_bridge_id_with_optional_labels(
        server_client=server_client,
        session_id=session_id,
        session_labels=session_init.snapshot.labels if session_init is not None else None,
    )
    bridge_id = cleared_bridge_id if existing_bridge_id == cleared_bridge_id else session_id
    if session_init is not None:
        # The transfer-inbound guard has already consumed the original label.
        # From this point this terminal owns the bridge, so later first-turn
        # helpers must observe the normalized id selected here.
        session_init.snapshot.labels[BRIDGE_ID_LABEL_KEY] = bridge_id
    else:
        try:
            await server_client.patch(
                f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                json={"labels": {BRIDGE_ID_LABEL_KEY: bridge_id}},
            )
        except httpx.HTTPError:
            _logger.debug(
                "Could not set bridge_id label for %s; relay may target wrong dir",
                session_id,
            )
    # Capture the previous claude_session_id from the bridge state file BEFORE
    # prepare_bridge_dir unlinks it. read_claude_session_id reads _STATE_FILE,
    # which prepare_bridge_dir removes as part of its refresh; reading it here
    # lets the cold-resume fallback below use it when the server GET missed the
    # external_session_id binding (e.g. workspace-scope ContextVar not set).
    from omnigent.claude_native_bridge import (
        bridge_dir_for_bridge_id as _bridge_dir_for_bridge_id,
    )
    from omnigent.claude_native_bridge import (
        read_claude_session_id as _read_csid_pre_wipe,
    )

    _pre_wipe_claude_sid = _read_csid_pre_wipe(_bridge_dir_for_bridge_id(bridge_id))
    bridge_dir = prepare_bridge_dir(session_id, bridge_id=bridge_id, workspace=Path(workspace))
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
    # path the runner reuses its process-level auth context.
    server_url = os.environ.get("RUNNER_SERVER_URL", "http://localhost:6767")
    # Authenticate the runner's outbound POSTs the same way its other
    # HTTP calls are authenticated.
    _auth_factory = auth_token_factory
    if _auth_factory is None:
        _auth_factory = _make_auth_token_factory()
    # The hook reads an owner-only header snapshot that the parent refreshes.
    # The forwarder uses refresh-capable auth directly; ``None`` is a no-op.
    _auth_token = _auth_factory() if _auth_factory is not None else None
    # The hook subprocess replays these static headers from its config (no
    # refresh-capable auth of its own); the helper pairs the bearer with the
    # workspace-routing header so neither is dropped.
    from omnigent.cli_auth import databricks_request_headers

    _runner_headers = databricks_request_headers(server_url, bearer_token=_auth_token)
    _runner_auth = _RunnerDatabricksAuth(_auth_factory)

    from omnigent.claude_launcher import resolve_claude_launch
    from omnigent.claude_native import (
        augment_claude_args,
        build_native_claude_terminal_env,
        resolve_claude_native_model_selection,
        resolve_native_claude_config,
    )

    launch_metadata = await _load_claude_launch_metadata(
        server_client=server_client,
        session_id=session_id,
        session_init=session_init,
    )
    session_effort = launch_metadata.reasoning_effort
    session_model_override = launch_metadata.model_override
    session_launch_args = launch_metadata.terminal_launch_args
    session_external_id = launch_metadata.external_session_id
    fork_source_external_id = launch_metadata.fork_source_external_id
    fork_carry_history = launch_metadata.fork_carry_history

    # The server GET may miss the external_session_id binding when the
    # reconnect request arrives without a workspace-scoped context (the
    # ContextVar defaults to 0 on fresh tasks). Fall back to the claude_session_id
    # captured from the bridge state file before prepare_bridge_dir wiped it.
    if session_external_id is None and _pre_wipe_claude_sid is not None:
        session_external_id = _pre_wipe_claude_sid
        _logger.info(
            "cold-resume fallback: server snapshot missing external_session_id, "
            "using local bridge hint: session=%s local_claude_sid=%s",
            session_id,
            _pre_wipe_claude_sid,
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
    # back to Claude's own native config (empty env).
    # See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
    # Resolve the launch config across all offerings — a configured provider
    # (omnigent setup), a Databricks ucode profile from provider config, or
    # Claude's own login — so a host-spawned native-claude session honors the
    # provider selection just like the in-process claude-sdk harness and the
    # CLI path.
    claude_config: ClaudeNativeUcodeConfig | None = None
    try:
        if resolve_launch_config is not None:
            claude_config = await resolve_launch_config()
        else:
            claude_config = await asyncio.to_thread(resolve_native_claude_config, spec=None)
    except click.ClickException:
        # An authoritative Databricks response with no Claude models is a
        # configuration failure, not permission to bypass the gateway.
        raise
    except Exception:  # noqa: BLE001 — best-effort; fall back to native auth
        _logger.warning(
            "native-claude: could not derive a provider/ucode launch config "
            "— FALLING BACK to Claude Code's own login; "
            "your configured provider will NOT be used. Check "
            "`omnigent setup --no-internal-beta` "
            "and that the secret resolves in this process.",
            exc_info=True,
        )
    if record_launch_config is not None:
        record_launch_config(session_id, claude_config)
    _logger.info(
        "Claude terminal provider config resolved: session=%s configured=%s "
        "env_keys=%s api_key_helper_set=%s model_set=%s",
        session_id,
        claude_config is not None,
        sorted(claude_config.env) if claude_config is not None else [],
        bool(claude_config.api_key_helper) if claude_config is not None else False,
        bool(claude_config.model) if claude_config is not None else False,
    )

    launch_model = resolve_claude_native_model_selection(
        session_model_override
        or _claude_native_model_from_spec(agent_spec)
        or (claude_config.model if claude_config is not None else None),
        claude_config,
    )
    base_claude_args = _build_claude_native_base_args(
        reasoning_effort=session_effort,
        # Precedence: per-session ``/model`` override > agent-spec pin
        # (``executor.model``) > provider/ucode default. All three yield to an
        # explicit ``--model`` in the user's pass-through args (handled in the
        # helper).
        model_override=launch_model,
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

    # Let a registered launcher plugin (e.g. Databricks' isaac) rewrite the
    # command/args to wrap the same fully-augmented Claude launch on this
    # managed-host path. Identity by default. See omnigent.claude_launcher.
    launch_command, launch_args = resolve_claude_launch("claude", list(claude_args))

    claude_terminal_env_unset = _claude_terminal_env_unset(claude_config)

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
        command=launch_command,
        args=launch_args,
        # Tool Search env plus ucode gateway env (ANTHROPIC_BASE_URL
        # etc.) when derived. Empty provider config still forces
        # ENABLE_TOOL_SEARCH=true so MCP schemas are loaded on demand.
        env=build_native_claude_terminal_env(claude_config),
        # Names to strip (see ``_claude_terminal_env_unset``). Dropping
        # ``DATABRICKS_CONFIG_PROFILE`` matters because Claude's MCP servers
        # inherit this env and several build ``WorkspaceClient`` without pinning
        # ``auth_type``: a set profile makes the SDK prefer that profile's cached
        # OAuth token over the MCP's explicit token, 400ing against the wrong
        # workspace. Claude itself ignores the var (routing is
        # ``ANTHROPIC_BASE_URL`` / ``apiKeyHelper``), so this affects only MCPs;
        # ones needing a specific profile must set it in their own per-MCP env.
        env_unset=claude_terminal_env_unset,
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
        # Use the SAME bridge id the dir was prepared under (``bridge_id``,
        # which is the "-cleared" fork for a /clear-superseded resume, else
        # session_id). Hardcoding session_id here would write tmux.json into
        # D(session_id) while the executor + forwarder read D(bridge_id) — the
        # "tmux target not advertised yet" mismatch on a resumed old session.
        bridge_id=bridge_id,
        terminal_name="claude",
        session_key="main",
    )
    _logger.info(
        "Claude terminal tmux target published: session=%s bridge_id=%s",
        session_id,
        bridge_id,
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

    async def _supervise_bridge() -> None:
        refresh_task: asyncio.Task[None] | None = None
        if _auth_factory is not None:
            refresh_task = asyncio.create_task(
                _refresh_claude_permission_hook_auth(
                    bridge_dir=bridge_dir,
                    server_url=server_url,
                    auth_token_factory=_auth_factory,
                ),
                name=f"claude-hook-auth-{session_id}",
            )
        try:
            await supervise_forwarder(
                base_url=server_url,
                headers=_runner_headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=resume_external_session_id is not None,
                auth=_runner_auth,
            )
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                _ = await asyncio.gather(refresh_task, return_exceptions=True)

    _forwarder_task = asyncio.create_task(
        _supervise_bridge(),
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
    # pill (web TerminalFirstContext). Stamped here — not at session
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


async def _delete_native_bridge_dirs(
    *,
    server_client: httpx.AsyncClient | None,
    session_id: str,
) -> None:
    """
    Remove any native-harness bridge dirs left behind by a session.

    Each native harness keeps a per-conversation bridge dir under
    ``/tmp/omnigent-<uid>/<harness>-native/<digest>`` (some use ``~/.omnigent``)
    holding a bridge token / auth secret + MCP config — secret material. Closing
    the pane does not remove it, so without this they accumulate even on a clean
    session delete (issue #1350). We don't know which harness this session used,
    so delete every candidate dir for all 11 native families
    (antigravity/claude/codex/cursor/goose/hermes/kimi/kiro/opencode/pi/qwen);
    the per-target ``FileNotFoundError`` swallow makes wrong-harness / already-gone
    cases a no-op, while other ``OSError``s are logged at debug rather than hidden.
    Antigravity/claude/codex/opencode bridge ids can be rotated via a session
    label, so resolve those too (falling back to *session_id*, the un-rotated key);
    the remaining families key purely on *session_id*.

    :param server_client: Omnigent server client used to resolve rotated bridge
        id labels. ``None`` skips label resolution (session_id keys only).
    :param session_id: Omnigent session/conversation id, e.g. ``"conv_abc123"``.
    """
    from omnigent.antigravity_native_bridge import (
        ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    )
    from omnigent.antigravity_native_bridge import (
        bridge_dir_for_bridge_id as antigravity_bridge_dir,
    )
    from omnigent.claude_native_bridge import (
        BRIDGE_ID_LABEL_KEY,
    )
    from omnigent.claude_native_bridge import (
        bridge_dir_for_bridge_id as claude_bridge_dir,
    )
    from omnigent.codex_native_bridge import (
        CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    )
    from omnigent.codex_native_bridge import (
        bridge_dir_for_bridge_id as codex_bridge_dir,
    )
    from omnigent.cursor_native_bridge import (
        bridge_dir_for_session_id as cursor_bridge_dir,
    )
    from omnigent.goose_native_bridge import (
        bridge_dir_for_session_id as goose_bridge_dir,
    )
    from omnigent.hermes_native_bridge import (
        bridge_dir_for_session_id as hermes_bridge_dir,
    )
    from omnigent.kimi_native_bridge import (
        bridge_dir_for_session_id as kimi_bridge_dir,
    )
    from omnigent.kiro_native_bridge import (
        bridge_dir_for_session_id as kiro_bridge_dir,
    )
    from omnigent.opencode_native_bridge import (
        OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
    )
    from omnigent.opencode_native_bridge import (
        bridge_dir_for_bridge_id as opencode_bridge_dir,
    )
    from omnigent.pi_native_bridge import (
        bridge_dir_for_session_id as pi_bridge_dir,
    )
    from omnigent.qwen_native_bridge import (
        bridge_dir_for_session_id as qwen_bridge_dir,
    )

    labels: dict[str, str] = {}
    if server_client is not None:
        labels = await _session_labels_for_runner_spawn(
            server_client=server_client,
            session_id=session_id,
        )

    targets = {
        antigravity_bridge_dir(labels.get(ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id),
        antigravity_bridge_dir(session_id),
        claude_bridge_dir(labels.get(BRIDGE_ID_LABEL_KEY) or session_id),
        claude_bridge_dir(session_id),
        codex_bridge_dir(labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id),
        codex_bridge_dir(session_id),
        cursor_bridge_dir(session_id),
        goose_bridge_dir(session_id),
        hermes_bridge_dir(session_id),
        kimi_bridge_dir(session_id),
        kiro_bridge_dir(session_id),
        opencode_bridge_dir(labels.get(OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id),
        opencode_bridge_dir(session_id),
        pi_bridge_dir(session_id),
        qwen_bridge_dir(session_id),
    }
    for target in targets:
        try:
            shutil.rmtree(target, ignore_errors=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.debug(
                "Failed to remove native bridge dir %s for session %s: %s",
                target,
                session_id,
                exc,
            )


async def _claude_native_bridge_id_for_session(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
    session_labels: Mapping[str, str] | None = None,
) -> str:
    """Resolve the bridge id label for a Claude-native session.

    :param server_client: Omnigent server client used to fetch the session
        snapshot.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :param session_labels: Labels supplied by the initialization envelope.
        ``None`` selects the legacy labels callback.
    :returns: Opaque bridge id from
        ``omnigent.claude_native.bridge_id`` when present, otherwise
        *session_id* for legacy single-session bridges.
    """
    from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY

    labels = (
        session_labels
        if session_labels is not None
        else await _session_labels_for_runner_spawn(
            server_client=server_client,
            session_id=session_id,
        )
    )
    bridge_id = labels.get(BRIDGE_ID_LABEL_KEY)
    if isinstance(bridge_id, str) and bridge_id:
        return bridge_id
    return session_id


async def _claude_native_bridge_id_with_optional_labels(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
    session_labels: Mapping[str, str] | None,
) -> str:
    """Preserve the exact legacy helper call when no envelope labels exist."""
    if session_labels is None:
        return await _claude_native_bridge_id_for_session(
            server_client=server_client,
            session_id=session_id,
        )
    return await _claude_native_bridge_id_for_session(
        server_client=server_client,
        session_id=session_id,
        session_labels=session_labels,
    )


async def _claude_native_session_wants_rebuild(
    server_client: httpx.AsyncClient | None,
    session_id: str,
    session_init: RunnerSessionInitEnvelope | None = None,
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
    :param session_init: Versioned server snapshot. ``None`` selects the
        legacy session callback.
    :returns: ``True`` when ``external_session_id`` is unset AND the
        carry-history label is set (a pending rebuild), else ``False``.
    """
    if server_client is None:
        return False
    from omnigent.stores.conversation_store import FORK_CARRY_HISTORY_LABEL_KEY

    if session_init is not None:
        return (
            session_init.snapshot.external_session_id is None
            and session_init.snapshot.labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
        )

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
    session_labels: Mapping[str, str] | None = None,
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
    :param session_labels: Labels supplied by the initialization envelope.
        ``None`` selects the legacy labels callback.
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

    bridge_id = await _claude_native_bridge_id_with_optional_labels(
        server_client=server_client,
        session_id=session_id,
        session_labels=session_labels,
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


@dataclasses.dataclass
class ResolvedSpec:
    spec: Any
    workdir: Path

    def __getattr__(self, name: str) -> Any:
        return getattr(self.spec, name)


def _unwrap_resolved_spec(entry: Any) -> Any:
    return entry.spec if isinstance(entry, ResolvedSpec) else entry


def _forward_harness_response(resp: httpx.Response) -> Response:
    """Relay a non-streaming harness response through FastAPI."""
    if resp.status_code in _NO_BODY_STATUS_CODES:
        return Response(status_code=resp.status_code)
    content_type = resp.headers.get("content-type", "")
    if not resp.content:
        return Response(content=b"", status_code=resp.status_code, media_type=content_type or None)
    if "application/json" in content_type.lower():
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except ValueError:
            pass
    return Response(
        content=resp.content, status_code=resp.status_code, media_type=content_type or None
    )


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
