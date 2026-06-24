"""Implementation of the ``omnigent chat`` command.

The CLI always ends by connecting an Omnigent client to a server URL. For
path targets it first ensures the agent is registered on that server
(a local subprocess by default, or ``--server`` when supplied). URL
targets skip setup and use the existing server's registered agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

import click
import httpx
import yaml
from omnigent_client import (
    OmnigentClient,
    SessionToolCallInfo,
    ToolCallable,
    ToolCallInfo,
    ToolHandler,
)
from omnigent_client import (
    OmnigentError as ClientOmnigentError,
)
from omnigent_client._events import (
    ErrorEvent,
    ResponseCancelled,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    TextDelta,
)
from rich.console import Console

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE as _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent._wrapper_labels import (
    WRAPPER_LABEL_KEY as _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
)
from omnigent.conversation_browser import open_conversation_link_if_enabled
from omnigent.errors import OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inner.databricks_executor import _DatabricksBearerAuth, _read_databrickscfg
from omnigent.native_coding_agents import native_coding_agent_for_wrapper_label
from omnigent.spec import load as load_spec
from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE
from omnigent.spec.parser import discover_host_skills
from omnigent.spec.types import AgentSpec, SkillSpec

if TYPE_CHECKING:
    from omnigent._runner_startup import RunnerStartupProgress

console = Console()

# YAML mapping shape — heterogeneous JSON-shaped values
# (strings, ints, lists, nested dicts) so ``Any`` is the
# narrowest safe element type. Used as the parsed-spec
# return / input shape across this module's helpers.
_YamlMapping: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

logger = logging.getLogger(__name__)

# Local server readiness polling: use a short initial interval so
# freshly-launched ``omnigent run`` sessions don't burn a
# fixed 500 ms before noticing the server is ready, then back off
# slightly while still remaining responsive on slower cold starts.
_SERVER_READY_INITIAL_POLL_SECONDS = 0.05
_SERVER_READY_BACKOFF_POLL_SECONDS = 0.1
_SERVER_READY_FAST_POLL_WINDOW_SECONDS = 1.0

# Remote ``--server`` runners are disposable subprocesses created for
# the CLI session. A one-second grace gives SIGTERM enough time to
# flush runner logs and unregister without noticeably slowing CLI exit.
# Grace period before the CLI escalates SIGTERM → SIGKILL on the
# runner subprocess. Must be long enough for the runner's shutdown
# chain to complete: cancel async tasks → app.router.shutdown() →
# _stop_pm() → _terminal_registry.shutdown() → tmux kill-server
# per session → pm.shutdown() → SIGTERM each harness. 1 s was too
# short — the runner was SIGKILL'd before tmux sessions were reaped,
# leaving zombie codex/claude processes.
_REMOTE_RUNNER_STOP_GRACE_SECONDS = 8.0

# Fallback model when the YAML declares neither ``executor.model``
# nor ``executor.harness`` AND no ``--model`` / ``--harness``
# override is supplied. Mirrors the legacy argparse CLI's
# ``_DEFAULT_AD_HOC_MODEL`` so ``omnigent run examples/hello_world.yaml``
# (a spec with no executor block) launches cleanly instead of
# failing the strict omnigent validator with a cryptic
# "executor.config.harness: required" error.
_DEFAULT_AD_HOC_MODEL = "databricks-gpt-5-4"

# How many of the NEWEST transcript items ``_persisted_turn_text``
# fetches when reconciling a headless ``-p`` turn against the durable
# store. The current turn's items are always the newest, and no single
# one-shot turn emits anywhere near this many items, so the latest turn
# is fully captured regardless of how long a resumed session's history
# is. Fetched ``order="desc"`` (newest first) precisely so the window
# tracks the end of the conversation, not its start.
_RECONCILE_ITEMS_LIMIT = 100

# Optional bearer token for remote omnigent servers that sit
# behind an auth proxy (for example Databricks Apps). When set, the
# CLI sends ``Authorization: Bearer <value>`` on every HTTP request it
# makes to the remote server.
_REMOTE_AUTH_TOKEN_ENV = "OMNIGENT_REMOTE_AUTH_TOKEN"

# Env-var override name. ``OMNIGENT_MODEL=foo`` lets a user
# pin a default model per shell session without needing to pass
# ``--model foo`` on every invocation. Resolved once at spec
# materialization time (not at runtime), so the materialized
# bundle stays self-contained — identical behavior on any host
# that runs the bundle, regardless of that host's env. Mirrors
# the legacy ``_default_cli_model`` at
# ``omnigent/inner/cli.py:344``.
_OMNIGENT_MODEL_ENV_VAR = "OMNIGENT_MODEL"
_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_OPENAI_BASE_URL_ENV_VAR = "OPENAI_BASE_URL"
_OPENAI_AGENTS_HARNESSES = frozenset({"openai-agents", "openai-agents-sdk"})
_MATERIALIZED_OVERRIDE_DIRS: dict[Path, Path] = {}


def _default_cli_model() -> str:
    """
    Return the model used when neither YAML nor CLI flag picks one.

    Reads ``OMNIGENT_MODEL`` from the environment with
    :data:`_DEFAULT_AD_HOC_MODEL` as the final fallback. The read
    happens at YAML-materialization time so the resolved model
    gets baked into the bundle's executor block — the materialized
    spec is self-contained and independent of any later env state.

    Mirrors :func:`omnigent.inner.cli._default_cli_model` so
    legacy and Omnigent paths agree on the env-var contract.

    :returns: The default model identifier, e.g.
        ``"databricks-gpt-5-4"`` or whatever the user pinned in
        ``OMNIGENT_MODEL``.
    """
    return os.environ.get(_OMNIGENT_MODEL_ENV_VAR, _DEFAULT_AD_HOC_MODEL)


@dataclass(frozen=True)
class ChatOverrides:
    """
    CLI overrides from ``omnigent run`` flags.

    Applied by materializing a rewritten copy of the agent YAML in a
    temp dir and pointing the local server at that copy — the user's
    source YAML is never mutated.

    :param harness: ``--harness`` value, e.g. ``"claude-sdk"``.
        ``None`` leaves the YAML value unchanged. Written to the flat
        ``executor.harness`` key for single-file omnigent YAMLs and to
        ``executor.config.harness`` for ``spec_version`` bundles (the
        only location that format's parser reads).
    :param model: ``--model`` value, e.g.
        ``"databricks-claude-sonnet-4-6"``. ``None`` unchanged.
    :param system_prompt: ``--system-prompt`` value — overrides the
        YAML's top-level ``prompt`` field (mapped to
        ``AgentSpec.instructions`` by the adapter). ``None``
        unchanged.
    """

    harness: str | None = None
    model: str | None = None
    system_prompt: str | None = None

    @property
    def has_any(self) -> bool:
        """True when at least one override flag was supplied."""
        return any(v is not None for v in (self.harness, self.model, self.system_prompt))


@dataclass(frozen=True)
class LocalServer:
    """
    Handle to a locally-launched omnigent server and its sibling runner.

    Returned by :func:`_start_local_server` so callers can pass the
    handle to :func:`_wait_for_server`, :func:`_stop_local_server`,
    and :func:`_raise_server_failed` without losing track of the
    subprocess's stdout/stderr log path. The log path is the only
    durable record of startup tracebacks (spec parse errors,
    unresolved env vars, executor import failures), so the failure
    helper surfaces it in its error message.

    :param proc: The server subprocess handle.
    :param log_path: Path to the file that captures the
        subprocess's combined stdout/stderr stream,
        e.g. ``Path("~/.omnigent/logs/server/server-abc123.log")``.
    :param runner_id: Stable runner id expected to register over
        the WebSocket tunnel, e.g. ``"runner_0123456789abcdef"``.
    :param runner_proc: The runner subprocess handle, spawned as a
        sibling of the server by :func:`_start_local_server`.
        ``None`` when no runner was started (shouldn't happen in
        normal operation).
    """

    proc: subprocess.Popen[bytes]
    log_path: Path
    runner_id: str | None = None
    runner_proc: subprocess.Popen[bytes] | None = None


@dataclass(frozen=True)
class _SessionToolAdapter:
    """
    Adapt a legacy :class:`ToolHandler` to a sessions-API tool callable.

    :param tool_handler: Legacy client-side tool handler from the
        responses-API path.
    :param agent_name: Agent display name for the legacy
        :class:`ToolCallInfo`, e.g. ``"coding_supervisor"``.
    """

    tool_handler: ToolHandler
    agent_name: str

    def __call__(self, info: SessionToolCallInfo) -> Awaitable[str] | str:
        """
        Execute the legacy tool handler for a sessions-API tool call.

        :param info: Sessions-API tool call context.
        :returns: Tool output string or awaitable string.
        """
        arguments = dict(info.arguments)
        legacy_info = ToolCallInfo(
            name=info.name,
            arguments=arguments,
            call_id=info.call_id,
            agent_name=self.agent_name,
            response_id=info.item_id if info.item_id is not None else info.call_id,
            iteration=0,
        )
        return self.tool_handler.execute(legacy_info)


def run_chat(
    target: str,
    client_tools: str | None,
    *,
    server_url: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    prompt: str | None = None,
    system_prompt: str | None = None,
    ephemeral: bool = False,
    resume_conversation_id: str | None = None,
    resume_latest: bool = False,
    resume_picker: bool = False,
    fork_session_id: str | None = None,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Main entry point for ``omnigent run`` (and the ``attach`` client).

    :param target: Path to an agent directory/bundle, or a server URL.
    :param client_tools: Optional client-side tool set name.
    :param server_url: Optional server URL to use with a local
        agent path, e.g. ``"https://example.databricksapps.com"``.
        ``None`` starts a local server for path targets.
    :param harness: CLI ``--harness`` override, e.g. ``"claude-sdk"``.
        Applied only to local-mode targets (YAML path / directory);
        ignored for remote server URLs.
    :param model: CLI ``--model`` override, e.g.
        ``"databricks-claude-sonnet-4-6"``. Local-mode only.
    :param prompt: CLI ``-p`` / ``--prompt`` — send one user turn,
        print the response, and exit.
    :param system_prompt: CLI ``--system-prompt`` — overrides the
        YAML's top-level ``prompt`` field. Local-mode only.
    :param ephemeral: When ``True``, place the local server's
        SQLite DB and artifacts in a per-run tmpdir instead of
        the persistent ``~/.omnigent`` location. Maps to
        ``--no-session`` on the CLI. Local-mode only — passing
        this with a remote URL target raises
        :class:`click.ClickException` (the remote server owns
        its own persistence).
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing conversation instead of
        creating a fresh one — replays recent items and
        threads new turns onto the existing
        ``previous_response_id`` chain. Maps to
        ``--resume <id>`` on the CLI.
    :param resume_latest: When ``True``, resolve "the most
        recent conversation for this agent" against the
        persistent store after the server boots and attach
        the REPL to it. Maps to ``--continue`` on the CLI.
        Local-mode only — passing this with a remote URL
        target raises :class:`click.ClickException`. Mutually
        exclusive with *resume_conversation_id* — the latter
        takes precedence if both are set.
    :param resume_picker: When ``True``, open the interactive
        stderr/stdin picker after the server boots and let
        the user choose a conversation. Maps to ``--resume``
        / ``-r`` with no value on the CLI. Local-mode only — passing this
        with a remote URL target raises
        :class:`click.ClickException` (the picker has no way
        to scope to a single agent on a multi-agent remote
        server without an explicit hand-off). Precedence:
        ``resume_conversation_id`` wins over ``resume_picker``
        wins over ``resume_latest``; user-cancelled picker
        falls through to a fresh conversation.
    :param fork_session_id: When set, fork this session before
        entering the REPL. The fork creates a deep copy of the
        source session's items into a new session; the REPL then
        opens attached to the fork. Maps to ``--fork ID`` on the
        CLI. Mutually exclusive with ``resume_conversation_id``,
        ``resume_latest``, and ``resume_picker``.
    :param log: When ``True``, write a JSON dump of the active
        conversation to ``~/.omnigent/logs/`` on REPL exit.
        Maps to ``--log`` on the CLI (default-on for the legacy
        path, default-off here so it stays explicit on
        Omnigent mode). See ``omnigent.repl._session_log`` for the
        schema. Local-mode only — passing this with a remote
        URL target raises :class:`click.ClickException`
        (no client-side conversation hand-off to dump).
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline (event tape overlay via ``Ctrl+E``, JSONL event
        logging to ``~/.omnigent/debug/``, and pipeline stage
        counters in the toolbar). Maps to ``--debug-events`` on the
        CLI.
    :param resume_parts: Pre-built argument list prefix for the
        resume hint, e.g. ``["omnigent", "run", "agent.yaml",
        "--server", "https://example.com"]``.  Built from Click's
        parsed context at CLI dispatch time.  ``None`` omits the
        resume hint on exit.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    """
    # Client-side tools are a CLI/TUI convenience (e.g. shell access
    # for coding agents). They don't affect agent behavior — the spec
    # is self-contained.
    tool_handler = _load_tool_handler(client_tools) if client_tools else None

    overrides = ChatOverrides(
        harness=harness,
        model=model,
        system_prompt=system_prompt,
    )

    if server_url is not None and _is_url(target):
        raise click.ClickException(
            "--server is for binding a local agent YAML to a server. "
            "Pass a YAML path as the target (got a URL)."
        )

    if _is_url(target):
        if any(
            v is not None for v in (overrides.harness, overrides.model, overrides.system_prompt)
        ):
            raise click.ClickException(
                "--harness / --model / --system-prompt only apply to local "
                "agent paths. The remote server controls its own agent registrations."
            )
        # Local-only resume / persistence flags would silently
        # vanish on the remote path (the server owns its own
        # store; we have no client-side conversation id to feed
        # the picker, no client-side log target). Fail loud rather
        # than letting a legacy/AP resume mode mismatch appear to work.
        if ephemeral or resume_latest or resume_picker or log:
            raise click.ClickException(
                "--no-session / --continue / --resume / --log only apply to "
                "local agent paths. "
                "The remote server owns its own persistence and conversation lookup. "
                "Pass --resume <id> with a remote URL to attach to a specific conversation."
            )
        # Discover host-scope skills from cwd so ``/skill-name`` slash
        # commands work even when connecting to a remote server with no
        # local agent spec.
        host_skills = discover_host_skills(Path.cwd(), "all")
        _chat_with_server(
            target,
            tool_handler,
            initial_message=prompt,
            resume_conversation_id=resume_conversation_id,
            fork_session_id=fork_session_id,
            debug_events=debug_events,
            resume_parts=resume_parts,
            skills=host_skills or None,
            auto_open_conversation=auto_open_conversation,
        )
    elif ephemeral:
        # ``--no-session`` keeps the legacy in-process ephemeral server: the
        # daemon-backed server is persistent + shared and has no per-run DB
        # isolation. Not combinable with an explicit ``--server``.
        if server_url:
            raise click.ClickException(
                "--no-session is not supported with --server; the uploaded agent "
                "is already scoped to the CLI session."
            )
        _chat_local(
            target,
            tool_handler,
            overrides=overrides,
            initial_message=prompt,
            ephemeral=True,
            resume_conversation_id=resume_conversation_id,
            resume_latest=resume_latest,
            resume_picker=resume_picker,
            fork_session_id=fork_session_id,
            log=log,
            debug_events=debug_events,
            resume_parts=resume_parts,
            auto_open_conversation=auto_open_conversation,
        )
    else:
        # Non-URL target → the host daemon is the backend. It connects to
        # the given ``--server`` URL, or starts (and connects to) a persistent
        # local Omnigent server when none is provided; this returns that concrete
        # URL. The agent is uploaded as a session and the daemon spawns +
        # *owns* the runner (the CLI only attaches the REPL), matching
        # claude-native.
        from omnigent.cli import _ensure_backend

        base_url = _ensure_backend(server_url)
        _chat_via_daemon(
            target,
            base_url,
            tool_handler,
            overrides=overrides,
            initial_message=prompt,
            resume_conversation_id=resume_conversation_id,
            resume_latest=resume_latest,
            resume_picker=resume_picker,
            fork_session_id=fork_session_id,
            log=log,
            debug_events=debug_events,
            resume_parts=resume_parts,
            auto_open_conversation=auto_open_conversation,
        )


def run_prompt(
    target: str,
    client_tools: str | None,
    *,
    harness: str | None = None,
    model: str | None = None,
    prompt: str,
    system_prompt: str | None = None,
    ephemeral: bool = False,
) -> None:
    """Run one prompt headlessly and print only the assistant text.

    This is the non-interactive sibling of :func:`run_chat` for
    ``omnigent run ... -p``. It deliberately bypasses the
    Rich/prompt-toolkit REPL startup path so ``-p`` behaves like a
    scriptable CLI mode: send one turn, print the assistant response,
    and return.

    :param target: Path to an agent directory/bundle, or a server URL.
    :param client_tools: Optional client-side tool set name.
    :param harness: CLI ``--harness`` override for local targets.
    :param model: CLI ``--model`` override for local targets.
    :param prompt: User prompt to send.
    :param system_prompt: CLI ``--system-prompt`` override for local targets.
    :param ephemeral: When ``True``, use a fresh per-run local
        server database and artifact directory.
    """
    tool_handler = _load_tool_handler(client_tools) if client_tools else None
    overrides = ChatOverrides(
        harness=harness,
        model=model,
        system_prompt=system_prompt,
    )

    if _is_url(target):
        if any(
            v is not None for v in (overrides.harness, overrides.model, overrides.system_prompt)
        ):
            raise click.ClickException(
                "--harness / --model / --system-prompt only apply to local "
                "agent paths. The remote server controls its own agent registrations."
            )
        base_url = target.rstrip("/")
        agent_name = _pick_agent(base_url, quiet=True)
        _run_headless_prompt(
            base_url,
            agent_name,
            tool_handler,
            prompt=prompt,
        )
        return

    _run_local_headless_prompt(
        target,
        tool_handler,
        overrides=overrides,
        prompt=prompt,
        ephemeral=ephemeral,
    )


def run_attach(
    *,
    base_url: str,
    conversation_id: str,
    client_tools: str | None = None,
    debug_events: bool = False,
    auto_open_conversation: bool = False,
    resume_parts: list[str] | None = None,
) -> None:
    """
    Attach the REPL to a LIVE conversation, dispatching to its existing runner.

    ``attach`` is a pure co-drive client: it never launches OR binds a runner.
    Turns post to the runner the host already bound (``POST /v1/sessions/{id}/
    events``, which needs only edit access), exactly like the web UI co-drive,
    and the server routes them to that runner. Binding a runner is owner-only
    server-side — so a teammate attaching to a shared session must NOT re-bind;
    post-only is what makes cross-user co-drive work. A read-only pre-flight
    confirms the session's host runner is online (``attach`` can't start one),
    failing loud otherwise.

    :param base_url: Omnigent server hosting the session, e.g.
        ``"http://127.0.0.1:6767"``.
    :param conversation_id: Live conversation/session id to join, e.g.
        ``"conv_abc123"``.
    :param client_tools: Optional client-side tool set name, e.g. ``"coding"``.
    :param debug_events: When ``True``, enable the SSE debug pipeline overlay.
    :param auto_open_conversation: When ``True``, open the browser conversation
        URL once attached.
    :param resume_parts: Argument-list prefix for the on-exit resume hint, e.g.
        ``["cli", "attach", "conv_abc123", "--server", "http://..."]``.
    :raises click.ClickException: If the session has no online runner (its host
        is offline) — ``attach`` never starts one.
    """
    base_url = base_url.rstrip("/")
    # Pre-flight (read-only): a co-drive client can only run turns if the
    # session's host runner is online; attach never launches one. The same
    # snapshot gives the agent name + harness for an honest banner.
    info = _attach_session_info(base_url=base_url, conversation_id=conversation_id)
    if not info.runner_online:
        raise click.ClickException(
            f"Session {conversation_id} has no online runner on {base_url} — its "
            "host is offline. `attach` never starts a runner; bring the host back "
            "(`omnigent run` locally, or reconnect it with `omnigent host`), "
            "then attach again."
        )

    tool_handler = _load_tool_handler(client_tools) if client_tools else None
    # Post-only co-drive: no runner_id / recover, ``attach_only`` so the REPL
    # adapter never PATCHes the (owner-only) runner binding — turns dispatch to
    # the host's already-bound runner. ``agent_name`` is the session's own (so
    # we skip the server agent-picker + its "Agent: …" echo); ``attach_harness``
    # makes the banner reflect what the host is running.
    _chat_with_server(
        base_url,
        tool_handler,
        agent_name=info.agent_name,
        resume_conversation_id=conversation_id,
        attach_only=True,
        attach_harness=info.harness,
        debug_events=debug_events,
        resume_parts=resume_parts,
        auto_open_conversation=auto_open_conversation,
    )


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def _is_url(target: str) -> bool:
    """
    Check if the target looks like a URL.

    :param target: The target string.
    :returns: True if it starts with http:// or https://.
    """
    return target.startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# Server URL client helpers
# ---------------------------------------------------------------------------


def _remote_headers(
    server_url: str | None = None,
) -> dict[str, str]:
    """
    Build headers for remote AP-server requests.

    Resolution order:
      1. explicit ``OMNIGENT_REMOTE_AUTH_TOKEN`` env var
      2. stored OIDC token from ``~/.omnigent/auth_tokens.json``
         (populated by ``omnigent login``)
      3. stored Databricks Apps pointer record for ``server_url``
         (populated by ``omnigent login <apps-url>``) — mints a
         fresh workspace OAuth token via the SDK
      4. ambient Databricks CLI / ``~/.databrickscfg`` credentials
         (the SDK's default resolution; no profile is threaded)

    This lets ``omnigent run --server <apps-url>`` work against
    Databricks Apps after a one-time ``omnigent login <apps-url>``,
    without forcing the user to manually copy a bearer into an env var.

    :param server_url: Optional remote server URL for looking up
        stored OIDC tokens, e.g. ``"http://localhost:6767"``.
    :returns: Headers to pass to httpx / OmnigentClient.
    """
    token = os.environ.get(_REMOTE_AUTH_TOKEN_ENV)
    if token and (token := token.strip()):
        return {"Authorization": f"Bearer {token}"}
    # Check stored OIDC token from `omnigent login`.
    if server_url:
        from omnigent.cli_auth import load_token

        oidc_token = load_token(server_url)
        if oidc_token:
            return {"Authorization": f"Bearer {oidc_token}"}
        record_token = _stored_databricks_record_token(server_url)
        if record_token:
            return {"Authorization": f"Bearer {record_token}"}
    creds = _read_databrickscfg(None)
    if creds is None or not creds.token:
        return {}
    return {"Authorization": f"Bearer {creds.token}"}


def _stored_databricks_record_token(server_url: str) -> str | None:
    """Mint a workspace token from a stored Databricks Apps record.

    ``omnigent login <apps-url>`` stores a pointer record naming the
    workspace that fronts the app; this resolves it to a fresh bearer
    via the Databricks CLI's host-keyed OAuth cache. One-shot — callers
    that issue many requests should use :class:`_DatabricksTokenAuth`,
    which reuses the SDK config across requests.

    :param server_url: The remote server URL, e.g.
        ``"https://myapp-123.aws.databricksapps.com"``.
    :returns: A bearer token, or ``None`` when no pointer record is
        stored or the workspace credentials don't resolve.
    """
    from omnigent.cli_auth import load_databricks_workspace_host
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    workspace_host = load_databricks_workspace_host(server_url)
    if workspace_host is None:
        return None
    try:
        auth, _host = _resolve_databricks_auth(host=workspace_host)
        return auth.current_token()
    except (DatabricksAuthError, ImportError, ValueError):
        return None


class _DatabricksTokenAuth(httpx.Auth):
    """
    httpx Auth that authenticates via the Databricks SDK, refreshing
    OAuth tokens transparently.

    Resolution order:
      1. static env-var token (``OMNIGENT_REMOTE_AUTH_TOKEN``)
      2. stored OIDC token (from ``omnigent login``)
      3. Databricks SDK credentials — resolved ONCE and reused, so the
         SDK serves the cached token from memory and only re-runs the
         Databricks CLI near expiry (not on every request).
    """

    def __init__(
        self,
        server_url: str | None = None,
    ) -> None:
        """
        :param server_url: Remote server URL for looking up stored
            OIDC tokens, e.g. ``"http://localhost:6767"``.
        """
        self._server_url = server_url
        raw = os.environ.get(_REMOTE_AUTH_TOKEN_ENV)
        self._static_token = raw.strip() if raw else None
        # Lazily-resolved, then reused, SDK auth (one Config → one token
        # cache). Resolving per request rebuilt Config and shelled out to
        # the Databricks CLI (~0.5s) every time — a heavy tax on the
        # long-lived transcript-forwarder client that posts reply items.
        self._sdk_auth: _DatabricksBearerAuth | None = None
        self._sdk_auth_resolved = False

    def _sdk_token(self) -> str | None:
        """
        Return a bearer token from the reused SDK auth, or ``None``.

        Resolves Databricks SDK auth on first use and reuses it, so
        repeat requests hit the SDK's in-memory token cache instead of
        re-shelling to the Databricks CLI. A stored Databricks Apps
        pointer record for the server (from ``omnigent login
        <apps-url>``) takes precedence over profile/ambient resolution
        — the record names the exact workspace the Apps edge accepts
        tokens from.

        :returns: Bearer token string, or ``None`` when no Databricks
            credentials resolve.
        """
        from omnigent.cli_auth import load_databricks_workspace_host
        from omnigent.inner.databricks_executor import (
            DatabricksAuthError,
            _resolve_databricks_auth,
        )

        if not self._sdk_auth_resolved:
            workspace_host = (
                load_databricks_workspace_host(self._server_url) if self._server_url else None
            )
            try:
                if workspace_host is not None:
                    self._sdk_auth, _host = _resolve_databricks_auth(host=workspace_host)
                else:
                    self._sdk_auth, _host = _resolve_databricks_auth()
            except (DatabricksAuthError, ImportError, ValueError):
                self._sdk_auth = None
            self._sdk_auth_resolved = True
        if self._sdk_auth is None:
            return None
        try:
            return self._sdk_auth.current_token()
        except DatabricksAuthError:
            return None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """
        Inject an ``Authorization`` header before each request.

        Static env-var token takes precedence, then stored OIDC token,
        then the reused Databricks SDK auth (which refreshes expired
        OAuth tokens transparently).

        :param request: The outgoing httpx request.
        :yields: The request with auth header set.
        """
        if self._static_token:
            request.headers["Authorization"] = f"Bearer {self._static_token}"
            yield request
            return
        # Check stored OIDC token from `omnigent login`.
        if self._server_url:
            from omnigent.cli_auth import load_token

            oidc_token = load_token(self._server_url)
            if oidc_token:
                request.headers["Authorization"] = f"Bearer {oidc_token}"
                yield request
                return
        token = self._sdk_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


def _server_headers(
    *,
    runner_id: str | None = None,
) -> dict[str, str]:
    """
    Build non-auth HTTP headers for an Omnigent server client.

    Auth is handled separately via :func:`_server_auth` which
    returns an ``httpx.Auth`` that refreshes the Databricks OAuth
    token on every request.

    :param runner_id: Optional runner UUID, e.g.
        ``"runner_0123456789abcdef"``. Accepted for callers that
        already threaded the value here; runner affinity is now
        persisted through ``PATCH /v1/sessions/{id}``, not a
        request header.
    :returns: Static headers for ``httpx`` / ``OmnigentClient``.
    """
    del runner_id
    return {}


def _server_auth(
    server_url: str | None = None,
) -> httpx.Auth | None:
    """
    Build an httpx Auth for a remote Omnigent server client.

    Returns a :class:`_DatabricksTokenAuth` when any credential
    source is available (env var, stored ``omnigent login`` record,
    or ambient Databricks credentials). Returns ``None`` for local
    servers that don't need auth, so the caller can pass it straight
    to ``OmnigentClient(auth=...)``.

    :param server_url: Optional remote server URL for looking up
        stored OIDC tokens.
    :returns: Auth instance, or ``None``.
    """
    raw = os.environ.get(_REMOTE_AUTH_TOKEN_ENV)
    if raw and raw.strip():
        return _DatabricksTokenAuth(server_url=server_url)
    # Check stored `omnigent login` records: a session JWT or a
    # Databricks Apps pointer record.
    if server_url:
        from omnigent.cli_auth import load_databricks_workspace_host, load_token

        if load_token(server_url) or load_databricks_workspace_host(server_url):
            return _DatabricksTokenAuth(server_url=server_url)
    creds = _read_databrickscfg(None)
    if creds is not None and creds.token:
        return _DatabricksTokenAuth(server_url=server_url)
    return None


def _chat_with_server(
    server_url: str,
    tool_handler: ToolHandler | None,
    *,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    fork_session_id: str | None = None,
    agent_name: str | None = None,
    runner_id: str | None = None,
    runner_recover: Callable[[], str] | None = None,
    log: bool = False,
    agent_yaml: Path | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    ephemeral: bool = False,
    debug_events: bool = False,
    server_log_path: Path | None = None,
    runner_log_path: Path | None = None,
    resume_parts: list[str] | None = None,
    skills: list[SkillSpec] | None = None,
    auto_open_conversation: bool = False,
    progress: RunnerStartupProgress | None = None,
    attach_only: bool = False,
    attach_harness: str | None = None,
) -> None:
    """
    Connect to a server URL and run a one-shot query or REPL.

    Lists available agents and lets the user pick one unless
    *agent_name* is supplied by an upstream setup step such as
    ephemeral ``--server`` upload.

    :param server_url: The server URL.
    :param tool_handler: Optional client-side tool handler.
    :param initial_message: If set without
        ``resume_conversation_id``, run one request and exit. If set
        with ``resume_conversation_id``, auto-send when the REPL
        opens.
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing conversation on the remote
        server instead of creating a fresh one.
    :param fork_session_id: When set, fork this session before
        entering the REPL. The REPL opens attached to the fork.
    :param agent_name: Optional already-selected agent name,
        e.g. ``"hello_world"``.
    :param runner_id: Optional preferred runner id to send on
        requests, e.g. ``"runner_0123456789abcdef"``.
    :param runner_recover: Optional callback that restarts a local
        runner if it exits and returns the live runner id.
    :param log: When ``True``, write a session log on REPL exit.
    :param agent_yaml: Optional local agent YAML path for tmux
        pane re-launch metadata.
    :param session_bundle: Optional gzipped agent bundle bytes used
        to create a fresh ``/v1/sessions`` session. Required for
        fresh sessions on the sessions API path.
    :param session_bundle_filename: Filename for the multipart
        ``bundle`` part, e.g. ``"agent.tar.gz"``.
    :param ephemeral: When ``True``, suppress the resume hint on
        exit — the session data lives in a tmpdir that won't
        survive process exit.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline. Forwarded to ``_run_repl``.
    :param server_log_path: Path to the local server's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/server/server-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` for remote servers.
    :param runner_log_path: Path to the local runner's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/runner/runner-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` when no local runner is used.
    :param resume_parts: Pre-built argument list prefix for the
        resume command shown on exit, e.g.
        ``["omnigent", "run", "agent.yaml", "--harness", "codex"]``.
        ``None`` uses the current process argv.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    :param skills: Parsed skill list from the agent spec, e.g.
        ``[SkillSpec(name="code-review", ...)]``. Each skill is
        registered as a ``/<name>`` slash command in the REPL.
        ``None`` (default) means no skill commands are registered.
    :param progress: Active startup spinner handed off from the daemon
        bring-up path, or ``None``. It stays up (on its last label,
        ``"Launching your agent…"``) across the wrapper-redirect probe and
        REPL setup below — so there's no empty gap there — and is cleared
        (``progress.finish()``) the instant before this function produces
        terminal output — a native-wrapper redirect notice, the one-shot
        reply, or the REPL's first paint.
    """
    base_url = server_url.rstrip("/")

    # The spinner (still showing the last bring-up phase, "Launching your
    # agent…") is intentionally left running through the wrapper-redirect
    # probe (a ``GET /v1/sessions/{id}`` that can take a few seconds) and REPL
    # setup, so the user never sees a cleared spinner over an empty gap here.
    # The label lags the exact step on purpose — better than a vaguer one.

    # Wrapper-aware resume redirect: if the conversation we're about to
    # resume was originally created by a terminal-native wrapper, the AP
    # REPL is the WRONG surface to attach to. Detect via the
    # ``omnigent.wrapper`` label on the conversation and re-dispatch
    # into the native wrapper carrying ``--server`` through. Without
    # this, the REPL renders an empty chat on top of a
    # session whose state lives in a tmux terminal it can't see.
    if resume_conversation_id is not None and _redirect_native_resume_if_needed(
        base_url=base_url,
        conversation_id=resume_conversation_id,
        auto_open_conversation=auto_open_conversation,
        progress=progress,
    ):
        return

    selected_agent = agent_name or _pick_agent(base_url)

    # Bring-up is done — clear the spinner the instant before we produce
    # terminal output (the one-shot reply or the REPL's first paint), so it
    # never lingers across the hand-off but also never leaves a gap before it.
    if progress is not None:
        progress.finish()

    if initial_message is not None:
        _run_one_shot(
            base_url=base_url,
            agent_name=selected_agent,
            tool_handler=tool_handler,
            prompt=initial_message,
            runner_id=runner_id,
            session_bundle=session_bundle,
            session_bundle_filename=session_bundle_filename,
            resume_conversation_id=resume_conversation_id,
            auto_open_conversation=auto_open_conversation,
        )
        return

    _run_repl(
        base_url,
        selected_agent,
        tool_handler,
        initial_message=initial_message,
        resume_conversation_id=resume_conversation_id,
        fork_session_id=fork_session_id,
        log=log,
        agent_yaml=agent_yaml,
        runner_id=runner_id,
        runner_recover=runner_recover,
        session_bundle=session_bundle,
        session_bundle_filename=session_bundle_filename,
        ephemeral=ephemeral,
        debug_events=debug_events,
        server_log_path=server_log_path,
        runner_log_path=runner_log_path,
        resume_parts=resume_parts,
        skills=skills,
        auto_open_conversation=auto_open_conversation,
        attach_only=attach_only,
        attach_harness=attach_harness,
    )


def _is_claude_native_conversation(
    *,
    base_url: str,
    conversation_id: str,
) -> bool:
    """
    Return whether *conversation_id* is a claude-native wrapper session.

    :param base_url: Omnigent server base URL.
    :param conversation_id: Omnigent conversation id.
    :returns: ``True`` only when the wrapper label matches Claude native.
    """
    return (
        _wrapper_label_for_conversation(
            base_url=base_url,
            conversation_id=conversation_id,
        )
        == _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE
    )


def _redirect_native_resume_if_needed(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None = None,
) -> bool:
    """
    Redirect a terminal-native resume before Omnigent attach liveness runs.

    :param base_url: Omnigent server base URL, e.g. ``"https://example.com"``.
    :param conversation_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional startup spinner to finish before redirect.
    :returns: ``True`` when a native wrapper handled the resume.
    """
    wrapper_label = _wrapper_label_for_conversation(
        base_url=base_url, conversation_id=conversation_id
    )
    native_agent = native_coding_agent_for_wrapper_label(wrapper_label)
    if native_agent is None:
        return False
    if native_agent.key == "claude":
        _run_claude_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    if native_agent.key == "codex":
        _run_codex_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    if native_agent.key == "pi":
        _run_pi_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    if native_agent.key == "cursor":
        _run_cursor_native_resume_redirect(
            base_url=base_url,
            conversation_id=conversation_id,
            auto_open_conversation=auto_open_conversation,
            progress=progress,
        )
        return True
    return False


def _finish_native_redirect_progress(
    *,
    progress: RunnerStartupProgress | None,
    conversation_id: str,
    wrapper_name: str,
    native_command: str,
) -> None:
    """
    Finish any Omnigent startup progress and print the native redirect notice.

    :param progress: Optional startup spinner to finish before writing.
    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param wrapper_name: Wrapper label for display, e.g. ``"codex-native"``.
    :param native_command: Native command to show, e.g. ``"codex"``.
    :returns: None.
    """
    if progress is not None:
        progress.finish()
    click.echo(
        (
            f"\n  Conversation {conversation_id} is a {wrapper_name} "
            f"session — redirecting to `omnigent {native_command} --resume`.\n"
        ),
        err=True,
    )


def _run_claude_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a claude-native conversation back to ``omnigent claude``.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="claude-native",
        native_command="claude",
    )
    from omnigent.claude_native import run_claude_native

    run_claude_native(
        server=base_url,
        session_id=conversation_id,
        claude_args=(),
        auto_open_conversation=auto_open_conversation,
    )


def _run_codex_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a codex-native conversation back to ``omnigent codex``.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricksapps.com"``.
    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="codex-native",
        native_command="codex",
    )
    from omnigent.codex_native import run_codex_native

    run_codex_native(
        server=base_url,
        session_id=conversation_id,
        codex_args=(),
        auto_open_conversation=auto_open_conversation,
    )


def _run_pi_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a pi-native conversation back to ``omnigent pi``.

    :param base_url: Omnigent server base URL.
    :param conversation_id: Omnigent conversation id.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="pi-native",
        native_command="pi",
    )
    from omnigent.pi_native import run_pi_native

    run_pi_native(
        server=base_url,
        session_id=conversation_id,
        pi_args=(),
        auto_open_conversation=auto_open_conversation,
    )


def _run_cursor_native_resume_redirect(
    *,
    base_url: str,
    conversation_id: str,
    auto_open_conversation: bool,
    progress: RunnerStartupProgress | None,
) -> None:
    """
    Hand a cursor-native conversation back to ``omnigent cursor``.

    The cursor-native session is driven by the ``cursor-agent`` TUI in a
    runner-owned tmux pane, and the forwarder mirrors that transcript back
    into the conversation. Resuming through the Omnigent REPL would instead
    run an Omnigent turn per message (which persists its own user item) *and*
    leave the forwarder mirroring the same message from the cursor store —
    recording each user message twice. Redirecting to ``omnigent cursor``'s
    direct tmux attach keeps the TUI the single source of turns.

    :param base_url: Omnigent server base URL.
    :param conversation_id: Omnigent conversation id.
    :param auto_open_conversation: Browser-open preference for the wrapper.
    :param progress: Optional Omnigent startup spinner to finish before redirect.
    :returns: None.
    """
    _finish_native_redirect_progress(
        progress=progress,
        conversation_id=conversation_id,
        wrapper_name="cursor-native",
        native_command="cursor",
    )
    from omnigent.cursor_native import run_cursor_native

    run_cursor_native(
        server=base_url,
        session_id=conversation_id,
        cursor_args=(),
        auto_open_conversation=auto_open_conversation,
    )


def _wrapper_label_for_conversation(
    *,
    base_url: str,
    conversation_id: str,
) -> str | None:
    """
    Return a conversation's wrapper label, if it can be read.

    Single-shot ``GET /v1/sessions/{id}`` against *base_url*, inspecting
    the response's ``labels.omnigent.wrapper`` field. ``None`` on any
    transport / parse error so a flaky server doesn't silently misroute
    the resume — the caller falls back to the normal Omnigent REPL path and
    surfaces a clear failure there.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Omnigent conversation id,
        e.g. ``"conv_abc123"``.
    :returns: Wrapper label value, or ``None``.
    """
    try:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{conversation_id}",
            headers=_remote_headers(server_url=base_url),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "wrapper-label probe failed for %s on %s: %s",
            conversation_id,
            base_url,
            exc,
        )
        return None
    if resp.status_code != 200:
        logger.warning(
            "wrapper-label probe got %s for %s on %s; treating as no wrapper",
            resp.status_code,
            conversation_id,
            base_url,
        )
        return None
    try:
        body = resp.json()
    except ValueError as exc:
        logger.warning(
            "wrapper-label probe for %s returned non-JSON body: %s",
            conversation_id,
            exc,
        )
        return None
    if not isinstance(body, dict):
        logger.warning(
            "wrapper-label probe for %s returned non-object body",
            conversation_id,
        )
        return None
    labels = body.get("labels")
    if not isinstance(labels, dict):
        return None
    value = labels.get(_CLAUDE_NATIVE_WRAPPER_LABEL_KEY)
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class _AttachSessionInfo:
    """Facts ``attach`` reads from one ``GET /v1/sessions/{id}`` snapshot.

    :param runner_online: ``True`` when the session is bound to a runner the
        server does not report as offline — i.e. a host is live to dispatch
        co-drive turns to. ``attach`` fails loud when ``False``.
    :param agent_name: The session's agent name, e.g. ``"polly"``, used as
        the REPL display name (so ``attach`` never has to pick from the
        server's agent list). ``None`` if the snapshot omits it.
    :param harness: The session's harness, e.g. ``"codex"``, shown in the
        attach banner so it reflects what the host is actually running.
        ``None`` if the snapshot omits it.
    """

    runner_online: bool
    agent_name: str | None
    harness: str | None


def _attach_session_info(
    *,
    base_url: str,
    conversation_id: str,
) -> _AttachSessionInfo:
    """
    Read the facts ``attach`` needs from one ``GET /v1/sessions/{id}``.

    ``attach`` dispatches co-drive turns to the host's already-bound runner
    (never launching one), so it only needs to know the runner is live plus
    the agent name + harness for an honest banner. ``runner_online`` is
    ``True`` only when a ``runner_id`` is present and the session snapshot
    does not report it offline. When ``runner_online`` is absent (older
    servers), attach stays optimistic and lets turn dispatch surface any real
    liveness failure; probing ``/v1/runners/{id}/status`` would be wrong here
    because that endpoint intentionally reports other users' runners as
    offline, while attach must support cross-user co-drive on shared sessions.
    A missing/unreachable session yields all-empty facts and the caller fails
    loud.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :returns: The session facts; ``runner_online=False`` on any failure.
    """
    empty = _AttachSessionInfo(runner_online=False, agent_name=None, harness=None)
    try:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{conversation_id}",
            headers=_remote_headers(server_url=base_url),
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        logger.warning("session probe failed for %s on %s: %s", conversation_id, base_url, exc)
        return empty
    if resp.status_code != 200:
        return empty
    try:
        body = resp.json()
    except ValueError:
        return empty
    if not isinstance(body, dict):
        return empty
    runner_id = body.get("runner_id")
    snapshot_online = body.get("runner_online")
    if not isinstance(runner_id, str) or not runner_id:
        runner_online = False
    elif isinstance(snapshot_online, bool):
        runner_online = snapshot_online
    else:
        # Older servers omit runner_online on the single-session snapshot. Stay
        # optimistic rather than falling back to the owner-scoped runner-status
        # endpoint, which reports a teammate's live runner as offline by design.
        runner_online = True
    agent_name = body.get("agent_name")
    harness = body.get("harness")
    return _AttachSessionInfo(
        runner_online=runner_online,
        agent_name=agent_name if isinstance(agent_name, str) and agent_name else None,
        harness=harness if isinstance(harness, str) and harness else None,
    )


def _pick_agent(base_url: str, *, quiet: bool = False) -> str:
    """
    Discover agent names from existing sessions and let the user pick.

    If only one agent name is found, selects it automatically.
    Falls back to requiring the user to specify ``--agent`` if no
    sessions exist yet.

    :param base_url: Server base URL.
    :param quiet: When ``True``, suppress interactive prompts and
        auto-select the first available agent.
    :returns: The chosen agent name.
    :raises click.ClickException: If no sessions exist or no
        agent name can be discovered.
    """
    resp = httpx.get(
        f"{base_url}/v1/sessions",
        headers=_remote_headers(server_url=base_url),
        params={"limit": 100},
        timeout=10.0,
    )
    resp.raise_for_status()
    sessions = resp.json()["data"]

    # Collect unique agent names from sessions.
    names: list[str] = []
    seen: set[str] = set()
    for s in sessions:
        name = s.get("agent_name")
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    if not names:
        raise click.ClickException(
            "No sessions found on the server. Start a session first "
            "or specify the agent with --agent."
        )

    if len(names) == 1:
        if not quiet:
            click.echo(f"\n  Agent: {names[0]}")
        return names[0]

    click.echo("\n  Available agents:\n")
    for i, name in enumerate(names, 1):
        click.echo(f"    {i}. {name}")

    while True:
        raw = str(click.prompt("\n  Agent", default="1"))
        try:
            choice = int(raw)
            if 1 <= choice <= len(names):
                return names[choice - 1]
        except ValueError:
            if raw.strip() in seen:
                return raw.strip()
        click.echo(f"  Enter a number between 1 and {len(names)}.")


# ---------------------------------------------------------------------------
# Local agent path bound to an existing server
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DaemonChatSession:
    """A chat session bound to a daemon-spawned runner.

    :param session_id: The created/resolved conversation id, e.g.
        ``"conv_abc123"``.
    :param runner_id: The daemon-spawned runner bound to the session, e.g.
        ``"runner_abc123"``.
    """

    session_id: str
    runner_id: str


_DAEMON_CHAT_HOST_ONLINE_TIMEOUT_S = 30.0
_DAEMON_CHAT_RUNNER_ONLINE_TIMEOUT_S = 60.0
_ACCOUNTS_SETUP_POLL_INTERVAL_S = 1.0
_ACCOUNTS_SETUP_TIMEOUT_S = 600.0


def _await_accounts_first_run_setup(
    base_url: str,
    *,
    timeout_s: float = _ACCOUNTS_SETUP_TIMEOUT_S,
    progress: RunnerStartupProgress | None = None,
) -> None:
    """Block until a fresh accounts-mode local server has its first admin.

    When ``omnigent run`` (re)spawns the local Omnigent server in accounts mode on
    a machine with no admin yet, the server reports ``needs_setup`` and (by
    default) opens a browser to its Create-admin form. Until an admin is
    claimed there is no CLI credential, so the first authenticated call would
    401. Rather than crash, print the setup URL — so it works whether or not
    the browser auto-opened (e.g. ``OMNIGENT_ACCOUNTS_AUTO_OPEN=0``) — and
    poll until the admin is created; ``/auth/setup`` then mints this CLI's
    loopback token, which we detect and return on.

    No-op when the server is not in accounts mode, when this CLI already holds
    a token for *base_url*, or when an admin already exists (the server mints
    our token at boot in that case).

    :param base_url: Resolved local Omnigent server URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param timeout_s: Max seconds to wait for setup, e.g. ``600.0``.
    :param progress: Active startup spinner, if any. Cleared before the
        interactive setup prompt below so the spinner doesn't animate over
        it. ``None`` (the default) when no spinner is running.
    :raises click.ClickException: If setup does not complete in time.
    """
    from omnigent import cli_auth

    # Already authenticated to this server — nothing to wait for.
    if cli_auth.load_token(base_url) is not None:
        return
    try:
        info = httpx.get(f"{base_url}/v1/info", timeout=5.0).json()
    except (httpx.HTTPError, ValueError):
        # /v1/info unreachable / unparseable: don't block — let the normal
        # path run and surface any real error.
        return
    if not (isinstance(info, dict) and info.get("accounts_enabled") and info.get("needs_setup")):
        # Header / OIDC, or an admin already exists (token minted at boot):
        # the normal headers/auth path handles it.
        return

    # We're about to print an interactive prompt and poll — drop the startup
    # spinner first so it doesn't render over the message.
    if progress is not None:
        progress.finish()
    setup_url = base_url.rstrip("/")
    click.echo(
        "\n  Accounts mode is enabled and needs a one-time admin account.\n"
        f"  Open  {setup_url}  in your browser to create it"
        " (it may have opened automatically),\n"
        "  then come back here. Waiting for setup to complete… (Ctrl-C to cancel)\n"
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(_ACCOUNTS_SETUP_POLL_INTERVAL_S)
        if cli_auth.load_token(base_url) is not None:
            click.echo("  ✓ Admin created — signed in. Continuing.\n")
            return
    raise click.ClickException(
        f"Timed out after {timeout_s:.0f}s waiting for admin setup at {setup_url}. "
        "Create the admin in the browser, then re-run."
    )


async def _prepare_chat_session_via_daemon(
    *,
    base_url: str,
    headers: dict[str, str],
    auth: httpx.Auth | None,
    host_id: str,
    bundle: bytes,
    resume_conversation_id: str | None,
    fork_session_id: str | None,
    workspace: str,
    progress: RunnerStartupProgress | None = None,
) -> _DaemonChatSession:
    """
    Create/resolve a chat session and launch a daemon-owned runner for it.

    Resolves the target session — fork > resume > fresh create — then asks
    the daemon to spawn a runner bound to it (the daemon owns the runner;
    the CLI only attaches the REPL afterward). Mirrors claude-native's
    ``_prepare_claude_terminal_via_daemon`` minus the terminal bring-up.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:8123"``.
    :param headers: Static HTTP auth headers (empty for a loopback server).
    :param auth: Per-request ``httpx.Auth`` for token refresh on the SDK
        client, or ``None`` for a loopback server.
    :param host_id: This machine's host id, e.g. ``"host_abc123"``.
    :param bundle: Gzipped agent bundle for a fresh session create.
    :param resume_conversation_id: Existing conversation id to attach to,
        or ``None`` to create a fresh session.
    :param fork_session_id: When set, fork this session and bind the runner
        to the fork; takes precedence over *resume_conversation_id*.
    :param workspace: Absolute host path for the runner cwd, e.g.
        ``"/Users/me/proj"``.
    :param progress: Optional startup-progress handle whose label is
        advanced through plain-language phases ("Connecting…",
        "Launching your agent…") as the host and runner come online, so a
        slow cold start is not silent. ``None`` (the default) runs without
        any progress updates.
    :returns: The prepared session id + bound runner id.
    :raises click.ClickException: If session create/fork or runner launch
        fails.
    """
    from omnigent_client import OmnigentClient

    from omnigent._runner_startup import (
        STARTUP_PHASE_CONNECTING,
        STARTUP_PHASE_LAUNCHING_AGENT,
    )
    from omnigent.host.daemon_launch import (
        launch_or_reuse_daemon_runner,
        wait_for_host_online,
        wait_for_runner_online,
    )
    from omnigent.native_terminal import bind_session_runner

    async with OmnigentClient(base_url=base_url, headers=headers, auth=auth) as sdk:
        if fork_session_id is not None:
            fork_result = await sdk.sessions.fork(fork_session_id)
            session_id = fork_result["id"]
        elif resume_conversation_id is not None:
            session_id = resume_conversation_id
        else:
            created = await sdk.sessions.create(
                bundle, filename="agent.tar.gz", workspace=workspace
            )
            session_id = created.id

    # A separate raw httpx client for the host-runner protocol (the daemon
    # launch helpers operate on httpx, not the SDK).
    timeout = httpx.Timeout(30.0, read=120.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        if progress is not None:
            progress.update(STARTUP_PHASE_CONNECTING)
        await wait_for_host_online(client, host_id, timeout_s=_DAEMON_CHAT_HOST_ONLINE_TIMEOUT_S)
        if progress is not None:
            progress.update(STARTUP_PHASE_LAUNCHING_AGENT)
        runner_id = await launch_or_reuse_daemon_runner(
            client, host_id=host_id, session_id=session_id, workspace=workspace
        )
        await wait_for_runner_online(
            client, runner_id, timeout_s=_DAEMON_CHAT_RUNNER_ONLINE_TIMEOUT_S
        )
        # launch_or_reuse_daemon_runner's atomic-bind / online-reuse paths
        # don't pass through replace_runner_id, so re-bind via PATCH to
        # clear the ``omnigent.stopped`` marker on resumed sessions. Must run
        # AFTER wait_for_runner_online — a freshly launched runner isn't
        # registered until then, and replace_runner_id 400s on an unregistered id.
        await bind_session_runner(client, session_id, runner_id)
    return _DaemonChatSession(session_id=session_id, runner_id=runner_id)


def _chat_via_daemon(
    agent_path: str,
    base_url: str,
    tool_handler: ToolHandler | None,
    *,
    overrides: ChatOverrides,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    resume_latest: bool = False,
    resume_picker: bool = False,
    fork_session_id: str | None = None,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Run a local agent against a daemon-backed server with a daemon-owned runner.

    Uploads the agent as a session, asks the host daemon to spawn the
    runner bound to that session (so the daemon owns its lifecycle), then
    attaches the REPL — the CLI never spawns or tears down the runner. On a
    clean exit the server idle-reaps the runner; if it dies mid-session the
    server relaunches it (host-bound auto-relaunch).

    :param agent_path: Local YAML path or directory.
    :param base_url: Resolved Omnigent server base URL (the daemon is already
        ensured for it), e.g. ``"http://127.0.0.1:8123"``.
    :param tool_handler: Optional client-side tool handler.
    :param overrides: CLI overrides to bake into the uploaded spec.
    :param initial_message: Optional one-shot input (``-p``).
    :param resume_conversation_id: Explicit conversation id
        (``--resume <id>``).
    :param resume_latest: ``True`` for ``--continue`` / ``-c``.
    :param resume_picker: ``True`` for bare ``--resume`` / ``-r``.
    :param fork_session_id: When set, fork this session and attach to the fork.
    :param log: When ``True``, write a session log on REPL exit.
    :param debug_events: When ``True``, enable the SSE-to-UI debug pipeline.
    :param resume_parts: Argument-list prefix for the resume hint on exit.
    :param auto_open_conversation: When ``True``, open the browser
        conversation URL once the session id is known.
    :returns: None.
    """
    from omnigent.host.identity import load_or_create_host_identity

    path = Path(agent_path)
    if not path.exists():
        raise click.ClickException(f"Agent path not found: {agent_path}")
    path = _canonicalize_local_agent_path(path)

    from omnigent._runner_startup import (
        STARTUP_PHASE_PREPARING_AGENT,
        runner_startup_progress,
    )

    spec_path = _materialize_override_bundle(path, overrides)
    try:
        # One spinner spans the entire agent/runner bring-up — bundle prep,
        # session upload, host + runner coming online, and the
        # wrapper-redirect probe inside ``_chat_with_server`` — and is torn
        # down (via ``progress.finish()``) exactly when the REPL is about to
        # paint. Holding a single spinner across these steps means there is
        # never an empty, cleared gap between them; the label can lag the
        # actual step (it stays on the last phase through the tail of
        # bring-up), which is the accepted trade for "no blank gaps". The
        # local-server cold start before this is covered by a sibling spinner
        # in ``_ensure_backend``.
        with runner_startup_progress(initial_message=STARTUP_PHASE_PREPARING_AGENT) as progress:
            _validate_agent_spec(spec_path)

            agent_spec = load_spec(spec_path)
            agent_name = agent_spec.name or _fallback_label(spec_path)
            all_skills = _merge_host_skills(agent_spec, spec_path)
            bundle_bytes = _bundle_agent(spec_path)

            # Accounts first-run: if the (re)spawned local server is in
            # accounts mode awaiting its one-time admin and this CLI has no
            # credential yet, block here with a clear prompt until setup
            # completes (the server then mints our CLI token), so we continue
            # into the REPL instead of 401-ing on the first authenticated call
            # below. Resolved BEFORE the headers/auth below so they pick up the
            # freshly-written token. ``progress`` is threaded so it can clear
            # the spinner before printing its interactive prompt.
            _await_accounts_first_run_setup(base_url, progress=progress)

            headers = _remote_headers(server_url=base_url)
            auth = _server_auth(server_url=base_url)
            host_id = load_or_create_host_identity().host_id
            workspace = str(Path.cwd().resolve())

            # The interactive resume picker reads stdin, so clear the spinner
            # first — it must not animate over the prompt. ``--continue`` and an
            # explicit ``--resume <id>`` are silent lookups and keep the spinner.
            if resume_picker:
                progress.finish()
            # Resolve --continue / --resume / picker to a concrete conversation
            # id. Fork is resolved server-side inside the prep step, so skip it.
            effective_resume_id = (
                None
                if fork_session_id is not None
                else _resolve_resume_target(
                    base_url=base_url,
                    agent_name=agent_name,
                    resume_conversation_id=resume_conversation_id,
                    resume_latest=resume_latest,
                    resume_picker=resume_picker,
                    headers=headers,
                )
            )

            prepared = asyncio.run(
                _prepare_chat_session_via_daemon(
                    base_url=base_url,
                    headers=headers,
                    auth=auth,
                    host_id=host_id,
                    bundle=bundle_bytes,
                    resume_conversation_id=effective_resume_id,
                    fork_session_id=fork_session_id,
                    workspace=workspace,
                    progress=progress,
                )
            )

            # Attach the REPL to the prepared session. ``resume_conversation_id``
            # makes the sessions adapter attach (get) instead of creating from
            # the bundle; the bundle is still passed so the one-shot path takes
            # its sessions-API branch. ``runner_recover=None``: the daemon owns
            # the runner — a dead runner is relaunched server-side, and the SDK
            # client refreshes its own auth per request. ``progress`` is handed
            # off so ``_chat_with_server`` clears the spinner the instant before
            # the REPL paints (or before it redirects to a native wrapper).
            _chat_with_server(
                base_url,
                tool_handler,
                initial_message=initial_message,
                resume_conversation_id=prepared.session_id,
                fork_session_id=None,
                agent_name=agent_name,
                runner_id=prepared.runner_id,
                runner_recover=None,
                log=log,
                agent_yaml=spec_path,
                session_bundle=bundle_bytes,
                debug_events=debug_events,
                resume_parts=resume_parts,
                skills=all_skills or None,
                auto_open_conversation=auto_open_conversation,
                progress=progress,
            )
    finally:
        _cleanup_materialized_override_bundle(spec_path)


def _wait_for_remote_runner(
    base_url: str,
    runner_id: str,
    headers: dict[str, str],
    runner_proc: subprocess.Popen[bytes],
    timeout: float = 60.0,
    *,
    log_path: Path | None = None,
    show_progress: bool = True,
) -> None:
    """Wait until the remote server sees the local runner tunnel.

    :param base_url: Remote server base URL with no trailing slash,
        e.g. ``"https://example.databricksapps.com"``.
    :param runner_id: Runner id the local process advertises, e.g.
        ``"runner_0123456789abcdef"``.
    :param headers: Auth headers for the remote server.
    :param runner_proc: Spawned local runner subprocess.
    :param timeout: Max seconds to wait for registration.
    :param log_path: Optional path to the captured runner log
        produced by ``_start_cli_runner_process(capture_logs=True)``,
        e.g. ``Path("~/.omnigent/logs/runner/runner-abcd.log")``.
        Included (with a tail) in the error message when the
        runner fails to register so users can diagnose the root
        cause without hunting for the file.
    :param show_progress: When ``True`` (default), render a rich
        spinner on stderr while polling. Set to ``False`` for
        callers running after the terminal has entered raw mode
        (e.g. the ``claude-native`` reconnect path) where a
        rich-rendered line would corrupt the attached PTY.
        Auto-falls back to plain ``click.echo`` updates on a
        non-TTY stderr; see :mod:`omnigent._runner_startup`.
    :returns: None.
    :raises click.ClickException: If the runner exits early or the
        server does not report it online before timeout. The
        exception message includes the runner log path and a
        ~20-line tail when ``log_path`` is provided.
    """
    from omnigent._runner_startup import (
        runner_startup_progress,
    )

    host_label = base_url.split("://", 1)[-1]
    initial_msg = f"Starting local runner (waiting for {host_label})\u2026"

    # The two branches return different concrete CMs (rich Status
    # vs nullcontext) but both honor the ``with``-statement
    # protocol. ``Any`` keeps mypy from forcing a structural cast
    # while still allowing the unified branch below.
    progress_cm: contextlib.AbstractContextManager[Any]  # type: ignore[explicit-any]
    if show_progress:
        progress_cm = runner_startup_progress(initial_message=initial_msg)
    else:
        progress_cm = contextlib.nullcontext()

    with progress_cm:
        _poll_remote_runner(
            base_url=base_url,
            runner_id=runner_id,
            headers=headers,
            runner_proc=runner_proc,
            timeout=timeout,
            log_path=log_path,
        )
        return


def _poll_remote_runner(
    *,
    base_url: str,
    runner_id: str,
    headers: dict[str, str],
    runner_proc: subprocess.Popen[bytes],
    timeout: float,
    log_path: Path | None,
) -> None:
    """
    Poll the server's runner-status endpoint until ``online=true``.

    Extracted from :func:`_wait_for_remote_runner` so the polling
    logic is independent of the progress renderer wrapping it.
    Tests that patch ``time.monotonic`` / ``time.sleep`` /
    ``httpx.get`` target this function directly without needing
    to suppress the rich spinner.

    :param base_url: Remote server base URL with no trailing slash.
    :param runner_id: Runner id to poll for.
    :param headers: Auth headers for the remote server.
    :param runner_proc: Spawned local runner subprocess. Used to
        detect early exit so the caller does not poll a dead
        runner for the full timeout.
    :param timeout: Max seconds to wait for registration.
    :param log_path: Captured runner log path threaded into the
        ``ClickException`` raised on failure. ``None`` skips the
        log-tail block in the error message.
    :returns: None on successful registration.
    :raises click.ClickException: Same conditions as
        :func:`_wait_for_remote_runner`.
    """
    from omnigent._runner_startup import format_runner_log_tail

    start = time.monotonic()
    deadline = start + timeout
    status_url = f"{base_url}/v1/runners/{runner_id}/status"
    last_error: httpx.HTTPError | None = None
    last_status: int | None = None
    while time.monotonic() < deadline:
        if runner_proc.poll() is not None:
            raise click.ClickException(
                f"Local runner exited early with code {runner_proc.returncode}."
                f"{format_runner_log_tail(log_path)}"
            )
        try:
            resp = httpx.get(status_url, headers=headers, timeout=2.0)
            if resp.status_code == 200 and resp.json().get("online") is True:
                return
            last_status = resp.status_code
            if resp.status_code in {401, 403}:
                raise click.ClickException(
                    f"Remote runner status check was rejected ({resp.status_code}); "
                    "run `omnigent login <server-url>` or check remote auth credentials."
                    f"{format_runner_log_tail(log_path)}"
                )
        except httpx.HTTPError as exc:
            last_error = exc
        elapsed = time.monotonic() - start
        poll_interval = (
            _SERVER_READY_INITIAL_POLL_SECONDS
            if elapsed < _SERVER_READY_FAST_POLL_WINDOW_SECONDS
            else _SERVER_READY_BACKOFF_POLL_SECONDS
        )
        time.sleep(poll_interval)
    detail = ""
    if last_status is not None:
        detail = f" Last status check returned HTTP {last_status}."
    elif last_error is not None:
        detail = f" Last status check failed: {last_error}."
    raise click.ClickException(
        f"Local runner did not register with {base_url} within {timeout:.0f}s."
        f"{detail}{format_runner_log_tail(log_path)}"
    )


def _bundle_agent(agent_path: Path) -> bytes:
    """
    Build a gzipped agent bundle for ``POST /v1/sessions``.

    Keeps the import of the CLI bundler local to avoid loading the
    full click command tree at module import time.

    :param agent_path: Local YAML file or agent directory.
    :returns: Gzipped tarball bytes suitable for the sessions
        multipart ``bundle`` part.
    :raises OmnigentError: If bundling fails, for example due to
        unresolved environment variables.
    """
    from omnigent.cli import _bundle

    return _bundle(agent_path)


# ---------------------------------------------------------------------------
# Local mode
# ---------------------------------------------------------------------------


def _chat_local(
    agent_path: str,
    tool_handler: ToolHandler | None,
    *,
    overrides: ChatOverrides | None = None,
    initial_message: str | None = None,
    ephemeral: bool = False,
    resume_conversation_id: str | None = None,
    resume_latest: bool = False,
    resume_picker: bool = False,
    fork_session_id: str | None = None,
    log: bool = False,
    debug_events: bool = False,
    resume_parts: list[str] | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Start a local server with the agent and open the REPL.

    The spec is parsed and validated in-process before launching the
    server subprocess so that config errors (unresolved env vars,
    invalid YAML, missing required fields) surface with the real
    exception message instead of being lost to the subprocess's
    silenced stderr.

    When *overrides* has any non-None field (or the YAML declares
    neither harness nor model), the source spec is materialized into
    a temp directory with the overrides + default-model fallback
    baked into its ``executor`` block, and the server is pointed at
    that copy. The user's source YAML is never mutated.

    :param agent_path: Path to the agent directory or bundle.
    :param tool_handler: Optional client-side tool handler.
    :param overrides: CLI overrides to bake into the spec before
        starting the server. ``None`` means no override (same shape
        as ``ChatOverrides()`` with all-None fields).
    :param initial_message: If set without a resume target, run one
        request and exit. If set with a resume target, auto-send on
        REPL start.
    :param ephemeral: When ``True``, point the local server at a
        fresh per-run tmpdir for its data store. ``False``
        (default) uses the persistent ``~/.omnigent``
        location so prior conversations remain reachable —
        see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md.
    :param resume_conversation_id: When set, open the REPL
        attached to this existing conversation rather than
        creating a fresh one.
    :param resume_latest: When ``True``, resolve "the most
        recent conversation for this agent" via the API
        after the server boots and use it as the resume
        target. Ignored when *resume_conversation_id* is
        already set. Maps to ``--continue`` on the CLI.
    :param resume_picker: When ``True``, open the
        interactive picker after the server boots. Maps to
        ``--resume`` / ``-r`` with no value on the CLI.
    :param log: When ``True``, write a JSON dump of the active
        conversation to ``~/.omnigent/logs/`` on REPL exit.
        Maps to ``--log`` on the CLI.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline. Forwarded to ``_chat_with_server``.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    """
    path = Path(agent_path)
    if not path.exists():
        raise click.ClickException(f"Agent path not found: {agent_path}")
    path = _canonicalize_local_agent_path(path)

    effective_overrides = overrides if overrides is not None else ChatOverrides()
    spec_path = _materialize_override_bundle(path, effective_overrides)
    try:
        # Parse once: validate + extract name + skills in a single pass.
        # Wraps the same exceptions as _validate_agent_spec so config
        # errors surface as clean ClickExceptions.
        try:
            agent_spec = load_spec(spec_path)
        except (OmnigentError, FileNotFoundError) as exc:
            raise click.ClickException(str(exc)) from exc
        agent_name = agent_spec.name or _fallback_label(spec_path)
        all_skills = _merge_host_skills(agent_spec, spec_path)
        port = _find_free_port()
        server = _start_local_server(
            spec_path,
            port,
            ephemeral=ephemeral,
        )

        try:
            _wait_for_server(port, server)
            base_url = f"http://127.0.0.1:{port}"
            _web_ui_dist = Path(__file__).parent / "server" / "static" / "web-ui"
            if _web_ui_dist.is_dir() and (_web_ui_dist / "index.html").is_file():
                console.print(f"\n  Web UI: [bold]{base_url}[/bold]")
                console.print("  Open in your browser for a visual interface\n")
            effective_resume_id = _resolve_resume_target(
                base_url=base_url,
                agent_name=agent_name,
                resume_conversation_id=resume_conversation_id,
                resume_latest=resume_latest,
                resume_picker=resume_picker,
            )
            bundle_bytes = _bundle_agent(spec_path)
            _chat_with_server(
                base_url,
                tool_handler,
                agent_name=agent_name,
                initial_message=initial_message,
                resume_conversation_id=effective_resume_id,
                runner_id=server.runner_id,
                fork_session_id=fork_session_id,
                log=log,
                agent_yaml=spec_path,
                session_bundle=bundle_bytes,
                ephemeral=ephemeral,
                debug_events=debug_events,
                server_log_path=server.log_path,
                resume_parts=resume_parts,
                skills=all_skills or None,
                auto_open_conversation=auto_open_conversation,
            )
        finally:
            _stop_local_server(server)
    finally:
        _cleanup_materialized_override_bundle(spec_path)


def _run_local_headless_prompt(
    agent_path: str,
    tool_handler: ToolHandler | None,
    *,
    overrides: ChatOverrides,
    prompt: str,
    ephemeral: bool = False,
) -> None:
    """
    Start a local server, run one prompt, print response, and stop.

    :param agent_path: Local YAML file or agent directory.
    :param tool_handler: Optional client-side tool handler.
    :param overrides: CLI overrides to bake into the spec.
    :param prompt: User prompt for the single turn.
    :param ephemeral: When ``True``, use a fresh per-run local
        server database and artifact directory.
    :returns: None.
    """
    path = Path(agent_path)
    if not path.exists():
        raise click.ClickException(f"Agent path not found: {agent_path}")
    path = _canonicalize_local_agent_path(path)

    spec_path = _materialize_override_bundle(path, overrides)
    try:
        _validate_agent_spec(spec_path)

        agent_name = _extract_agent_name(spec_path)
        port = _find_free_port()
        server = _start_local_server(
            spec_path,
            port,
            ephemeral=ephemeral,
        )

        try:
            _wait_for_server(port, server)
            _run_headless_prompt(
                f"http://127.0.0.1:{port}",
                agent_name,
                tool_handler,
                prompt=prompt,
                runner_id=server.runner_id,
                session_bundle=_bundle_agent(spec_path),
            )
        finally:
            _stop_local_server(server)
    finally:
        _cleanup_materialized_override_bundle(spec_path)


def _run_headless_prompt(
    base_url: str,
    agent_name: str,
    tool_handler: ToolHandler | None,
    *,
    prompt: str,
    runner_id: str | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
) -> None:
    """
    POST one prompt through the SDK and print the final assistant text.

    Uses the sessions API: create the session, bind the runner,
    send one message, print text, and return.

    :param base_url: Server base URL, e.g.
        ``"http://127.0.0.1:8123"``.
    :param agent_name: Agent display name, e.g. ``"hello_world"``.
    :param tool_handler: Optional client-side tool handler.
    :param prompt: User prompt for the single turn.
    :param runner_id: Registered runner id, e.g.
        ``"runner_0123456789abcdef"``. Required with
        *session_bundle*.
    :param session_bundle: Optional gzipped agent bundle bytes.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :raises SystemExit: Exits with code 1 after printing the server
        error text when the stream emits ``response.error`` or
        returns ``ResponseFailed`` without output text.
    :returns: None.
    """

    async def _main() -> None:
        async with OmnigentClient(
            base_url=base_url,
            headers=_server_headers(runner_id=runner_id),
            auth=_server_auth(server_url=base_url),
        ) as client:
            if session_bundle is not None:
                result_text = await _query_sessions_once(
                    client=client,
                    agent_name=agent_name,
                    tool_handler=tool_handler,
                    prompt=prompt,
                    session_bundle=session_bundle,
                    session_bundle_filename=session_bundle_filename,
                    runner_id=runner_id,
                )
                if result_text:
                    print(result_text)
                return

            session = client.session(model=agent_name, tool_handler=tool_handler)
            chunks: list[str] = []
            terminal_text: str | None = None
            error_text: str | None = None
            async for event in session.send(prompt):
                if isinstance(event, TextDelta):
                    chunks.append(event.delta)
                elif isinstance(event, ErrorEvent):
                    error_text = event.error.message or event.error.code
                elif isinstance(
                    event,
                    ResponseCompleted | ResponseFailed | ResponseIncomplete | ResponseCancelled,
                ):
                    terminal_text = _response_output_text(event.response.output)

            streamed_text = "".join(chunks)
            # Prefer the real error from a response.error SSE event over the
            # generic terminal-event message ("Failed to retrieve final response")
            # that _build_terminal_event substitutes when it can't read the task.
            if streamed_text:
                print(streamed_text)
            elif error_text:
                print(f"Error: {error_text}", file=sys.stderr)
                raise SystemExit(1)
            elif terminal_text:
                print(terminal_text)

    try:
        asyncio.run(_main())
    except ClientOmnigentError as exc:
        # SETUP-phase failure: SessionsChat.send raises on a terminal
        # ``session.status: failed`` (no response.failed is emitted).
        # Surface it the same way as a response.error event so headless
        # ``-p`` exits non-zero with the real message.
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _query_sessions_once(
    *,
    client: OmnigentClient,
    agent_name: str,
    tool_handler: ToolHandler | None,
    prompt: str,
    session_bundle: bytes,
    session_bundle_filename: str,
    runner_id: str | None,
    resume_conversation_id: str | None = None,
    on_session_ready: Callable[[str], None] | None = None,
) -> str | None:
    """
    Create, bind, and query a sessions-API session for headless ``-p``.

    :param client: Connected SDK client.
    :param agent_name: Agent display name, e.g. ``"hello_world"``.
        Used only for tool-handler validation messages.
    :param tool_handler: Optional client-side tool handler.
    :param prompt: User prompt for the single turn.
    :param session_bundle: Gzipped agent tarball bytes.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :param runner_id: Registered runner id, e.g.
        ``"runner_0123456789abcdef"``.
    :param resume_conversation_id: When set, resumes an existing
        session instead of creating a new one, e.g.
        ``"conv_abc123"``. ``None`` creates a fresh session.
    :param on_session_ready: Optional callback invoked after the
        session has been created/resumed and bound to the runner.
    :returns: Final assistant text, or ``None`` when no text was
        emitted.
    :raises RuntimeError: If no runner id was supplied.
    """
    from omnigent_client import SessionsChat

    if runner_id is None:
        raise RuntimeError(
            "Sessions API headless prompt requires a registered runner id. "
            "Start through `omnigent run <agent>` or pass --server so the CLI "
            "can launch and bind a runner."
        )
    tool_callables = _sessions_tool_callables(tool_handler, agent_name)
    if resume_conversation_id is not None:
        bound = await client.sessions.get(resume_conversation_id)
        await client.sessions.bind_runner(resume_conversation_id, runner_id=runner_id)
    else:
        created = await client.sessions.create(
            session_bundle,
            filename=session_bundle_filename,
            # Record CLI cwd so the Web UI can show "ran locally
            # in <workspace>" for one-shot sessions. CLI sessions
            # don't set host_id; this column is purely informational.
            workspace=os.getcwd(),
        )
        bound = await client.sessions.bind_runner(created.id, runner_id=runner_id)
    if on_session_ready is not None:
        on_session_ready(bound.id)
    session_files = client.files.for_session(bound.id)
    chat = SessionsChat(
        namespace=client.sessions,
        files_uploader=session_files.upload,
        files_getter=session_files.get,
        session=bound,
        tool_callables=tool_callables,
        agent_tools_getter=client._fetch_agent_tools,
    )
    del agent_name
    # A transport-level runner disconnect publishes ``session.status:
    # failed`` for every session pinned to that runner (server
    # ``_on_runner_disconnect``), even when the turn already completed
    # and its assistant response was persisted. ``SessionsChat.send``
    # raises ``OmnigentError`` on that ``failed`` status, and the
    # no-replay SSE subscription can additionally miss the terminal
    # ``response.completed`` event (subscribe-after-post race), leaving
    # the collected text empty. In both cases the runner has still
    # persisted the assistant message server-side, so reconcile against
    # the transcript before surfacing a failure — only a turn that
    # produced no output is a genuine error worth raising. The
    # interactive REPL is immune by construction (it renders a ``failed``
    # status as a transient error and polls the snapshot as a backstop),
    # so this brings headless ``-p`` to parity.
    try:
        result = await chat.query(prompt)
    except ClientOmnigentError:
        reconciled = await _persisted_turn_text(client, bound.id)
        if reconciled is not None:
            return reconciled
        raise
    all_text_parts: list[str] = []
    if result.text:
        all_text_parts.append(result.text)
    elif (reconciled := await _persisted_turn_text(client, bound.id)) is not None:
        all_text_parts.append(reconciled)

    # Multi-turn loop for async orchestrators (e.g. polly) that dispatch
    # sub-agents and are auto-woken by inbox completions across multiple
    # turns.
    #
    # Why not _collect_query for the fast-exit signal: the runtime emits
    # ``session.status: waiting`` AFTER ``response.completed`` (the runner
    # finishes dispatching tools, then enters the async drain). _collect_query
    # exits at CompletedEvent and never sees the subsequent "waiting".
    #
    # Why not refresh() for the fast-exit signal: the snapshot API collapses
    # the ``"waiting"`` relay status to ``"idle"`` once the turn loop exits,
    # even while sub-agents are still running.
    #
    # Probe approach: subscribe to the live stream for a short window after
    # the first turn. The "waiting" event arrives O(ms–s) after CompletedEvent
    # (runner dispatches tools, spawns sub-agents, then parks). The probe
    # catches it before sub-agents have a chance to complete.
    #
    # ``await_turn`` resets ``last_turn_saw_waiting`` to False on
    # ``session.status: running`` (synthesis starting), so the flag cleanly
    # reflects only the most recent dispatch state after each call.
    #
    # Single-turn agents: no "waiting" event ever → probe times out in
    # _STATUS_PROBE_TIMEOUT_S (~30 s) and the loop exits.
    _MAX_EXTRA_TURNS = 30
    # The runner emits session.status:waiting (not idle) when a turn ends with
    # running sub-agents. The relay cache holds "waiting", which the snapshot
    # collapses to "running". refresh() is therefore the authoritative signal:
    # "running" → async orchestrator still waiting for inbox; "idle" → done.
    #
    # A short probe await_turn runs first: it catches synthesis text or the
    # status event if the subscription opens before the event arrives. Both
    # "waiting" and "idle" break the probe immediately so the generator closes
    # cleanly without hitting the timeout.
    #
    # refresh() is called after every await_turn (probe + loop) — it is correct
    # even when await_turn times out (sub-agents still running), unlike the
    # last_turn_saw_waiting flag which would incorrectly exit on timeout.
    _STATUS_PROBE_TIMEOUT_S = 5.0  # brief window; status events arrive fast
    _PER_TURN_TIMEOUT_S = 120.0  # race-window guard per synthesis turn
    _LOOP_TIMEOUT_S = 1800.0  # 30 min total

    async def _drain_extra_turns() -> None:
        # Probe: collect synthesis text or status events that arrive quickly.
        probe = await chat.await_turn(timeout=_STATUS_PROBE_TIMEOUT_S)
        if probe.text:
            all_text_parts.append(probe.text)
        # refresh() is the authoritative check: "running" means the runner's
        # relay cache holds "waiting" (sub-agents still running); "idle" means
        # truly done (single-turn agent, or synthesis completed in the probe).
        await chat.refresh()
        if chat.status not in ("running", "launching"):
            return
        # Async orchestrator confirmed. Loop, refreshing after each turn.
        for _ in range(_MAX_EXTRA_TURNS):
            extra = await chat.await_turn(timeout=_PER_TURN_TIMEOUT_S)
            if extra.text:
                all_text_parts.append(extra.text)
            await chat.refresh()
            if chat.status not in ("running", "launching"):
                return  # Idle: synthesis done or all sub-agents complete.
        logger.warning(
            "headless -p hit the %d-turn guard for session %s; "
            "the orchestrator may still be running",
            _MAX_EXTRA_TURNS,
            bound.id,
        )

    try:
        async with asyncio.timeout(_LOOP_TIMEOUT_S):
            await _drain_extra_turns()
    except asyncio.TimeoutError:
        logger.warning(
            "headless -p timed out after %.0fs waiting for session %s to complete",
            _LOOP_TIMEOUT_S,
            bound.id,
        )

    if all_text_parts:
        return "\n\n".join(p for p in all_text_parts if p)
    # No assistant text at all. If the runner persisted a terminal
    # ``error`` item (e.g. a harness start failure like the cursor SDK's
    # invalid-model rejection), surface it instead of returning ``None`` —
    # otherwise the headless caller renders a failed turn as a silent,
    # exit-0 empty success. The callers wrap this in ``except
    # ClientOmnigentError`` and print the message to stderr + exit non-zero.
    turn_error = await _persisted_turn_error(client, bound.id)
    if turn_error is not None:
        raise ClientOmnigentError(turn_error)
    return None


def _sessions_tool_callables(
    tool_handler: ToolHandler | None,
    agent_name: str,
) -> dict[str, ToolCallable] | None:
    """
    Convert a legacy tool handler into sessions-API callables.

    :param tool_handler: Optional legacy client-side tool handler.
    :param agent_name: Agent display name for legacy tool context,
        e.g. ``"coding_supervisor"``.
    :returns: Mapping from declared tool name to callable, or
        ``None`` when no handler is configured.
    """
    if tool_handler is None:
        return None
    adapter = _SessionToolAdapter(tool_handler=tool_handler, agent_name=agent_name)
    callables: dict[str, ToolCallable] = {}
    for schema in tool_handler.schemas:
        raw_name = schema.get("name")
        if not isinstance(raw_name, str):
            continue
        callables[raw_name] = adapter
    return callables


_ResponseOutput: TypeAlias = list[dict[str, Any]]  # type: ignore[explicit-any]


def _response_output_text(output: _ResponseOutput) -> str | None:
    """Extract assistant text from an Omnigent response ``output`` list."""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts) if parts else None


async def _persisted_turn_text(
    client: OmnigentClient,
    session_id: str,
) -> str | None:
    """
    Read the latest turn's persisted assistant text from a session.

    The headless ``-p`` path consumes a turn over a single no-replay
    SSE subscription. Two failure modes leave that subscription without
    the turn's text even though the runner persisted an assistant
    response server-side:

    * A transport-level runner disconnect publishes
      ``session.status: failed`` for the session (server
      ``_on_runner_disconnect``) after the turn completed;
      :meth:`SessionsChat.send` raises ``OmnigentError`` on it.
    * The subscriber misses the terminal ``response.completed`` event
      (subscribe-after-post race), so the collected text is empty.

    This reconciles against the durable transcript via
    ``GET /v1/sessions/{id}/items``. It anchors on the most recent
    user message and returns the concatenated ``output_text`` of every
    ``completed`` assistant message that follows it, so a resumed
    session's earlier-turn output is never mistaken for the current
    turn. Only ``completed`` assistant items count: a turn that truly
    errored mid-stream persists a non-``completed`` partial item, and
    masking that as success would swallow a genuine failure — whereas
    the target bug (a completed turn flipped to ``failed`` by a
    transport disconnect) always persists a ``completed`` item.

    :param client: Connected SDK client bound to the session's server.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :returns: The current turn's assistant text, or ``None`` when this
        turn persisted no ``completed`` assistant text (a genuine
        failure the caller should surface).
    """
    try:
        # ``order="desc"`` (newest first) so the window tracks the END
        # of the transcript — the current turn — not its start. A long
        # resumed session can have far more than the limit of history;
        # fetching ``asc`` would return the oldest items and miss this
        # turn entirely.
        recent: _ResponseOutput = await client.sessions.list_items(
            session_id, limit=_RECONCILE_ITEMS_LIMIT, order="desc"
        )
    except ClientOmnigentError as exc:
        # The reconcile read is itself best-effort: if the items
        # endpoint is unreachable, fall back to the original outcome
        # (the caller re-raises the turn error or prints nothing). Log
        # for observability rather than swallowing silently.
        logger.debug("reconcile transcript read failed for %s: %r", session_id, exc)
        return None
    # Walk newest → oldest, collecting ``completed`` assistant messages
    # until the current turn's user message is reached. This isolates
    # THIS turn's output: a prior turn's assistant text sits on the far
    # side of the current user message and is never collected.
    this_turn_assistant: _ResponseOutput = []
    for item in recent:
        if item.get("type") != "message":
            continue
        role = item.get("role")
        if role == "user":
            break  # reached the start of the current turn
        if role == "assistant" and item.get("status") == "completed":
            this_turn_assistant.append(item)
    # Restore chronological order so multi-message output joins correctly.
    this_turn_assistant.reverse()
    return _response_output_text(this_turn_assistant)


async def _persisted_turn_error(
    client: OmnigentClient,
    session_id: str,
) -> str | None:
    """Read the latest turn's persisted terminal error message, if any.

    Companion to :func:`_persisted_turn_text`. When a turn produced no
    ``completed`` assistant text, the runner may still have persisted a
    terminal ``error`` item — e.g. a harness *start* failure such as the
    cursor SDK rejecting an unknown model. Without this, the headless ``-p``
    path renders that as a silent, exit-0 empty success; returning the message
    lets the caller surface it and exit non-zero.

    Mirrors :func:`_persisted_turn_text`'s walk: newest → oldest, stopping at
    the current turn's user message, so a prior turn's error is never
    attributed to this turn.

    :param client: Connected SDK client bound to the session's server.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :returns: The current turn's terminal error message, or ``None``.
    """
    try:
        recent: _ResponseOutput = await client.sessions.list_items(
            session_id, limit=_RECONCILE_ITEMS_LIMIT, order="desc"
        )
    except ClientOmnigentError as exc:
        logger.debug("reconcile error read failed for %s: %r", session_id, exc)
        return None
    for item in recent:
        if item.get("type") == "message" and item.get("role") == "user":
            break  # reached the start of the current turn
        if item.get("type") == "error":
            message = item.get("message")
            if isinstance(message, str) and message:
                return message
    return None


def _resolve_resume_target(
    *,
    base_url: str,
    agent_name: str,
    resume_conversation_id: str | None,
    resume_latest: bool,
    resume_picker: bool = False,
    headers: dict[str, str] | None = None,
) -> str | None:
    """
    Decide which conversation the REPL should resume from.

    Doing this here (vs. inside ``run_repl``) gives a clean
    fail-fast when ``--continue`` finds no prior conversation:
    raise ``ClickException`` before the REPL renders anything,
    matching the native shape at
    ``omnigent/inner/cli.py:3082-3084`` ("No saved sessions
    to continue.").

    Precedence (highest to lowest):

    1. ``resume_conversation_id`` (``--resume <id>``) —
       explicit pin always wins.
    2. ``resume_picker`` (``--resume`` / ``-r`` with no value) —
       interactive picker. Returns ``None`` when the user cancels
       (treated as "start fresh"); raises when no conversations
       exist.
    3. ``resume_latest`` (``--continue`` / ``-c``) —
       silent auto-pick of the newest. Raises when no prior.
    4. None of the above — fresh conversation.

    :param base_url: Server base URL the SDK should target for
        the lookup, e.g. ``"http://127.0.0.1:9123"`` or
        ``"https://example.databricksapps.com"``.
    :param agent_name: The agent's registered name from the
        YAML's ``name:`` field.
    :param resume_conversation_id: An explicit
        ``--resume <id>``.
    :param resume_latest: ``True`` when ``--continue`` was
        passed.
    :param resume_picker: ``True`` when bare ``--resume`` was
        passed. Runs the interactive picker on the agent's
        conversations and returns the user's choice. ``None``
        return means user cancelled — caller should treat as
        "start fresh" rather than a hard error.
    :param headers: Optional auth headers for the server,
        e.g. ``{"Authorization": "Bearer <token>"}``. Required
        for remote servers; ``None`` for localhost.
    :returns: The conversation_id to attach to, or ``None``
        when no resumption flag matched (or the picker was
        cancelled).
    :raises click.ClickException: When ``--continue`` /
        ``--resume`` was requested but the agent has no prior
        conversation.
    """
    if resume_conversation_id is not None:
        # Fail loud on a bogus id instead of booting the REPL, swallowing
        # the failed attach, and silently starting fresh (which loses the
        # thread the user meant to resume). Mirrors the --continue path.
        _assert_resume_conversation_exists(
            base_url=base_url,
            conversation_id=resume_conversation_id,
            headers=headers,
        )
        return resume_conversation_id
    if resume_picker:
        # ``None`` from the picker is a clean cancel — pass it
        # through so the REPL opens fresh. The empty-list case is
        # raised explicitly inside ``_run_picker`` so it lands as
        # a ClickException with a parity message.
        return _run_picker(
            base_url=base_url,
            agent_name=agent_name,
            headers=headers,
        )
    if not resume_latest:
        return None
    resolved = _resolve_latest_conversation_id(
        base_url=base_url,
        agent_name=agent_name,
        headers=headers,
    )
    if resolved is None:
        raise click.ClickException(f"No prior conversation for agent {agent_name!r}.")
    return resolved


def _assert_resume_conversation_exists(
    *,
    base_url: str,
    conversation_id: str,
    headers: dict[str, str] | None = None,
) -> None:
    """
    Fail fast when an explicit ``--resume <id>`` names a conversation
    that does not exist on the server.

    :param base_url: Server base URL the lookup targets.
    :param conversation_id: The id passed to ``--resume``.
    :param headers: Optional auth headers for the server.
    :raises click.ClickException: When the id is not found (404). Other
        errors propagate so a transient failure isn't mislabeled.
    """

    async def _lookup() -> None:
        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            await client.sessions.get(conversation_id)

    try:
        asyncio.run(_lookup())
    except ClientOmnigentError as exc:
        if exc.status_code == 404:
            raise click.ClickException(f"Conversation {conversation_id!r} not found.") from exc
        raise


def _run_picker(
    *,
    base_url: str,
    agent_name: str,
    headers: dict[str, str] | None = None,
) -> str | None:
    """
    Drive the ``--resume`` picker against a server.

    Looks up this agent's id (so the picker only shows THIS
    agent's conversations, not pooled across agents that share
    the persistent store), fetches the conversation list via the
    SDK, and runs the stderr/stdin picker.

    :param base_url: Server base URL,
        e.g. ``"http://127.0.0.1:9123"`` or
        ``"https://example.databricksapps.com"``.
    :param agent_name: Agent's registered name.
    :param headers: Optional auth headers for the server,
        e.g. ``{"Authorization": "Bearer <token>"}``. Required
        for remote servers; ``None`` for localhost.
    :returns: Selected conversation_id, or ``None`` if the user
        cancelled.
    :raises click.ClickException: When the agent has no prior
        conversations — ``--resume`` should fail-loud rather
        than silently open a picker the user can only cancel
        out of.
    """
    from omnigent.repl._resume_picker import pick_conversation_from_sdk

    async def _lookup() -> str | None:
        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            # Multipart ``omnigent run <yaml>`` uploads now create a
            # fresh session-scoped agent for every session so users who
            # choose the same YAML ``name:`` never share a bundle. Resume
            # lookup therefore scopes by the user-authored name across
            # those distinct session agents rather than by a template
            # agent id returned from ``agents.get_by_name``.
            return await pick_conversation_from_sdk(
                client,
                agent_name=agent_name,
                agent_id=None,
                agent_name_filter=agent_name,
            )

    return asyncio.run(_lookup())


def _resolve_latest_conversation_id(
    *,
    base_url: str,
    agent_name: str,
    headers: dict[str, str] | None = None,
) -> str | None:
    """
    Find the most-recent conversation for *agent_name* on a
    server.

    Used to translate ``--continue`` into a concrete
    ``conversation_id`` after the server is reachable. The
    server-side filter joins through ``Task.agent_id``, so the
    returned conversation is guaranteed to belong to *this
    agent* (not pooled across agents that happen to share the
    DB).

    :param base_url: Server base URL,
        e.g. ``"http://127.0.0.1:9123"`` or
        ``"https://example.databricksapps.com"``.
    :param agent_name: The agent's registered name from the
        YAML's ``name:`` field.
    :param headers: Optional auth headers for the server,
        e.g. ``{"Authorization": "Bearer <token>"}``. Required
        for remote servers; ``None`` for localhost.
    :returns: The conversation_id, or ``None`` if no session
        with this agent name has prior conversations.
    """

    async def _lookup() -> str | None:
        async with OmnigentClient(base_url=base_url, headers=headers) as client:
            return await _resolve_latest_conversation_id_async(
                client=client,
                agent_name=agent_name,
            )

    return asyncio.run(_lookup())


async def _resolve_latest_conversation_id_async(
    *,
    client: OmnigentClient,
    agent_name: str,
) -> str | None:
    """
    Async core of :func:`_resolve_latest_conversation_id`.

    Factored out so tests can drive it against an in-process
    ASGI test client without spawning a subprocess server +
    re-opening a real HTTP connection. The sync entry point
    above wraps this with ``asyncio.run`` and an
    ``OmnigentClient`` connected to a real URL — the path
    used in production by ``_chat_local``.

    :param client: A connected :class:`OmnigentClient`.
    :param agent_name: The agent's registered name.
    :returns: The conversation_id of the agent's most-recent
        conversation, or ``None`` when the agent has no prior
        conversations (first-ever run, or a fresh persistent
        store).
    """
    sessions = await client.sessions.list(
        agent_name=agent_name,
        limit=1,
        order="desc",
        sort_by="updated_at",
    )
    if not sessions:
        return None
    return sessions[0].id


def _materialize_override_bundle(source: Path, overrides: ChatOverrides) -> Path:
    """
    Copy *source* into a temp dir and apply CLI overrides to its YAML.

    Also materializes when the spec is a single-file YAML with no
    ``executor.harness`` AND no ``executor.model`` — the strict
    omnigent validator rejects that shape, and the legacy
    argparse CLI used to paper over it by injecting
    :data:`_DEFAULT_AD_HOC_MODEL`. This preserves that behavior so
    ``omnigent run examples/hello_world.yaml`` (minimal spec) still
    launches cleanly.

    When no override is set and the spec already declares harness or
    model, returns *source* unchanged — no temp materialization.

    :param source: Path to the agent YAML or directory.
    :param overrides: CLI overrides. All-None means "no user
        override"; a default-model fallback may still apply.
    :returns: Path that the server should register — either the
        original *source* or a rewritten copy under a tempdir.
    """
    raw_peek = _load_yaml_if_single_file(source)
    raw_override_peek = _load_yaml_for_override_peek(source)
    needs_fallback = raw_peek is not None and not _spec_declares_harness_or_model(raw_peek)
    needs_openai_env_auth = raw_override_peek is not None and _should_materialize_openai_env_auth(
        raw_override_peek, overrides
    )
    if not overrides.has_any and not needs_fallback and not needs_openai_env_auth:
        return source

    # ``_cleanup_materialized_override_bundle`` removes this tempdir
    # once validation, bundling, or the attached REPL/server path no
    # longer needs the rewritten spec.
    tmpdir = Path(tempfile.mkdtemp(prefix="omnigent-override-"))
    try:
        if source.is_file():
            target = tmpdir / source.name
            target.write_bytes(source.read_bytes())
        else:
            # Copy the whole bundle so bundled tools / sub-agent dirs /
            # skills travel with the rewritten config.yaml. The user's
            # source tree is never touched.
            target_dir = tmpdir / source.name
            shutil.copytree(source, target_dir)
            config = target_dir / "config.yaml"
            if not config.is_file():
                raise click.ClickException(f"{source}: directory has no config.yaml to override.")
            target = config

        raw = yaml.safe_load(target.read_text())
        if not isinstance(raw, dict):
            raise click.ClickException(
                f"{source}: expected YAML mapping at top level, got {type(raw).__name__}"
            )
        _apply_overrides_to_raw(raw, overrides)
        target.write_text(yaml.safe_dump(raw, default_flow_style=False))
        materialized = target if source.is_file() else target.parent
        _MATERIALIZED_OVERRIDE_DIRS[materialized.resolve()] = tmpdir
        return materialized
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


def _cleanup_materialized_override_bundle(materialized: Path) -> None:
    """
    Remove the temp directory created for a materialized override bundle.

    Override materialization can bake provider credentials from the CLI
    environment into the copied YAML before bundling. The copy is needed
    only while the caller validates, starts a local server, or uploads the
    bundle, so delete the tempdir explicitly instead of leaving secrets for
    OS temp reaping.

    :param materialized: Path returned by
        :func:`_materialize_override_bundle`, e.g.
        ``Path("/tmp/omnigent-override-abc/agent.yaml")``.
    :returns: None.
    """
    tempdir = _MATERIALIZED_OVERRIDE_DIRS.pop(materialized.resolve(), None)
    if tempdir is None:
        return
    shutil.rmtree(tempdir, ignore_errors=True)


def _load_yaml_for_override_peek(source: Path) -> _YamlMapping | None:
    """
    Load the YAML that override materialization would rewrite.

    Single-file specs rewrite the file itself. Agent-image
    directories rewrite ``config.yaml``. Invalid or non-mapping YAML
    returns ``None`` so the normal validation path can surface the
    precise user-facing error later.

    :param source: Path to a YAML file or agent directory.
    :returns: Parsed top-level YAML mapping, or ``None`` when no
        rewrite target can be inspected.
    """
    if source.is_dir():
        config = source / "config.yaml"
        if not config.is_file():
            return None
        parsed = yaml.safe_load(config.read_text())
        return parsed if isinstance(parsed, dict) else None
    return _load_yaml_if_single_file(source)


def _load_yaml_if_single_file(source: Path) -> _YamlMapping | None:
    """
    Load the YAML at *source* if it's a single-file spec; else None.

    Directories (omnigent-style with ``config.yaml``) are handled
    separately by the materializer — this helper just peeks at the
    single-file case so the caller can decide whether the
    default-model fallback applies.

    :param source: Path to a YAML file or agent directory.
    :returns: Parsed top-level dict, or None if *source* is a
        directory or the YAML isn't a mapping.
    """
    if not source.is_file():
        return None
    parsed = yaml.safe_load(source.read_text())
    return parsed if isinstance(parsed, dict) else None


def _spec_declares_harness_or_model(raw: _YamlMapping) -> bool:
    """
    True when the YAML's ``executor:`` block has harness or model.

    Either signal is enough for the spec-adapter's harness auto-pick
    (``databricks-claude-*`` → ``claude-sdk``, etc.) — the
    default-model fallback only kicks in when BOTH are absent.

    Recognizes the harness in either shape: a flat ``executor.harness``
    or the bundle-style nested ``executor.config.harness`` (e.g.
    ``examples/polly``). Without the nested check, an unpinned bundle
    that declares its harness only under ``config`` would look
    harness-less and get force-fed :data:`_DEFAULT_AD_HOC_MODEL` — a
    GPT endpoint the claude-sdk harness can't speak.

    :param raw: Parsed top-level YAML mapping.
    :returns: True if ``executor.harness``, ``executor.model``, or
        ``executor.config.harness`` is a non-empty value.
    """
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        return False
    if executor.get("harness") or executor.get("model"):
        return True
    config = executor.get("config")
    return isinstance(config, dict) and bool(config.get("harness"))


def _should_materialize_openai_env_auth(
    raw: _YamlMapping,
    overrides: ChatOverrides,
) -> bool:
    """
    Return whether materialization would inject OpenAI env credentials.

    Daemon-backed local runs launch a daemon-owned runner whose
    environment intentionally strips provider secrets. Specs that rely
    only on ambient ``OPENAI_API_KEY`` therefore need those credentials
    baked into ``executor.auth`` before bundling, but only when the
    effective harness is OpenAI-compatible and no explicit spec/provider
    auth already wins.

    :param raw: Parsed top-level YAML mapping.
    :param overrides: CLI overrides that will be applied to ``raw``.
    :returns: ``True`` when a rewritten bundle is needed solely to add
        ``executor.auth`` from ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``.
    """
    executor = raw.get("executor")
    executor_block = executor if isinstance(executor, dict) else {}
    harness = _effective_openai_auth_harness(raw, executor_block, overrides)
    return _should_inject_openai_env_auth_for_executor(executor_block, harness)


def _effective_openai_auth_harness(
    raw: _YamlMapping,
    executor_block: _YamlMapping,
    overrides: ChatOverrides,
) -> str | None:
    """
    Resolve the harness relevant to OpenAI env-auth injection.

    This mirrors the edge of the normal spec path closely enough for
    materialization decisions: explicit CLI harness wins, then YAML
    harness, then model-prefix inference from the effective model.

    :param raw: Parsed top-level YAML mapping.
    :param executor_block: Parsed ``executor`` mapping from ``raw``.
    :param overrides: CLI overrides that will be applied.
    :returns: Canonical harness name, e.g. ``"openai-agents"``, or
        ``None`` when no harness can be inferred.
    """
    raw_harness = executor_block.get("harness")
    harness = overrides.harness if overrides.harness is not None else raw_harness
    if isinstance(harness, str) and harness:
        return canonicalize_harness(harness) or harness

    model = _effective_openai_auth_model(raw, executor_block, overrides)
    if model is None:
        return None
    from omnigent.llms.routing import infer_harness_from_model

    inferred = infer_harness_from_model(model)
    return inferred or None


def _effective_openai_auth_model(
    raw: _YamlMapping,
    executor_block: _YamlMapping,
    overrides: ChatOverrides,
) -> str | None:
    """
    Resolve the model relevant to OpenAI env-auth injection.

    :param raw: Parsed top-level YAML mapping.
    :param executor_block: Parsed ``executor`` mapping from ``raw``.
    :param overrides: CLI overrides that will be applied.
    :returns: Effective model string, e.g. ``"databricks-gpt-5-4-mini"``,
        or ``None`` when neither CLI nor YAML names a model.
    """
    if overrides.model is not None:
        return overrides.model
    raw_model = executor_block.get("model")
    if isinstance(raw_model, str) and raw_model:
        return raw_model
    llm = raw.get("llm")
    if isinstance(llm, dict):
        llm_model = llm.get("model")
        if isinstance(llm_model, str) and llm_model:
            return llm_model
    return None


def _should_inject_openai_env_auth_for_executor(
    executor_block: _YamlMapping,
    harness: str | None,
) -> bool:
    """
    Return whether ``executor.auth`` should be populated from env.

    :param executor_block: Effective parsed ``executor`` mapping.
    :param harness: Effective harness name, e.g. ``"openai-agents"``.
    :returns: ``True`` when ambient OpenAI-compatible credentials are
        present and no explicit auth/profile/provider declaration
        should take precedence.
    """
    if harness not in _OPENAI_AGENTS_HARNESSES:
        return False
    if not os.environ.get(_OPENAI_API_KEY_ENV_VAR):
        return False
    if executor_block.get("auth") is not None:
        return False
    if executor_block.get("connection") is not None:
        return False
    if executor_block.get("profile"):
        return False
    config = executor_block.get("config")
    if isinstance(config, dict) and config.get("profile"):
        return False
    # A configured credential — a provider default serving this harness's
    # family, or the legacy global ``auth:`` block — is the user's explicit
    # setup choice; an ambient env key must NOT be baked over it (a shell
    # with OPENAI_API_KEY exported would silently hijack the configured
    # Databricks/gateway routing). Configured sources reach the runner via
    # OMNIGENT_CONFIG_HOME, so skipping injection loses nothing. The env
    # bake remains only for users whose SOLE credential is the env key.
    from omnigent.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )
    from omnigent.runtime.workflow import _load_global_auth

    if default_provider_for_harness(load_config(), harness) is not None:
        return False
    if _load_global_auth() is not None:
        return False
    return True


def _inject_openai_env_auth_if_needed(raw: _YamlMapping) -> None:
    """
    Add explicit OpenAI-compatible auth to ``raw`` when env fallback is unsafe.

    Daemon-owned runners do not inherit provider secret environment
    variables. For an OpenAI-compatible harness that otherwise relies on
    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``, bake the resolved values into
    the materialized spec so the uploaded bundle remains self-contained.

    :param raw: Parsed top-level YAML mapping, mutated in place.
    :returns: None.
    """
    executor = raw.get("executor")
    executor_block: _YamlMapping
    if isinstance(executor, dict):
        executor_block = executor
    else:
        executor_block = {}
        raw["executor"] = executor_block
    harness = _effective_openai_auth_harness(raw, executor_block, ChatOverrides())
    if not _should_inject_openai_env_auth_for_executor(executor_block, harness):
        return
    auth: dict[str, str] = {
        "type": "api_key",
        "api_key": os.environ[_OPENAI_API_KEY_ENV_VAR],
    }
    base_url = os.environ.get(_OPENAI_BASE_URL_ENV_VAR)
    if base_url:
        auth["base_url"] = base_url
    executor_block["auth"] = auth


def _apply_overrides_to_raw(raw: _YamlMapping, overrides: ChatOverrides) -> None:
    """
    Mutate *raw* to reflect CLI overrides + the default-model fallback.

    Mirrors the legacy argparse CLI's ``_apply_overrides_to_yaml``
    so behavior is unchanged post-unification. The harness override is
    format-aware — see :func:`_apply_harness_override_to_executor`.

    :param raw: Parsed YAML mapping (mutated in place).
    :param overrides: CLI overrides to bake into the ``executor``
        block.
    """
    executor_block = raw.get("executor")
    if not isinstance(executor_block, dict):
        executor_block = {}
        raw["executor"] = executor_block
    if overrides.model is not None:
        executor_block["model"] = overrides.model
    if overrides.harness is not None:
        _apply_harness_override_to_executor(raw, executor_block, overrides.harness)
    # When neither harness nor model is declared — after overrides —
    # inject the ad-hoc default. Gated on harness absence so a YAML
    # like ``claude_code_agent.yaml`` (declares harness, no model)
    # doesn't get silently paired with the gpt-5-4 default, which
    # the Databricks FM API rejects for Claude-typed entities.
    # Uses ``_spec_declares_harness_or_model`` — must agree with the
    # ``needs_fallback`` gate in :func:`_materialize_override_bundle`.
    # Uses ``_default_cli_model`` (env-var-aware) instead of
    # ``_DEFAULT_AD_HOC_MODEL`` directly so ``OMNIGENT_MODEL=foo``
    # is honored on the ``omnigent/cli.py`` → ``run_chat`` direct
    # path. Without this, that env var was silently dropped on the
    # Omnigent path invoked through the ``omnigent`` console
    # script (see ``designs/RUN_OMNIGENT_REPL_PARITY.md``).
    if not _spec_declares_harness_or_model(raw):
        executor_block["model"] = _default_cli_model()
    _inject_openai_env_auth_if_needed(raw)
    if overrides.system_prompt is not None:
        raw["prompt"] = overrides.system_prompt


def _apply_harness_override_to_executor(
    raw: _YamlMapping,
    executor_block: _YamlMapping,
    harness: str,
) -> None:
    """
    Write the ``--harness`` override where the spec's format reads it.

    Single-file omnigent YAMLs (``name`` + ``prompt``, no
    ``spec_version``) read the flat ``executor.harness`` key.
    ``spec_version`` bundles (e.g. ``examples/polly``) read ONLY
    ``executor.config.harness`` — writing the flat key there is a
    silent no-op, which made ``omnigent run examples/polly
    --harness pi`` keep the claude-sdk brain.

    :param raw: Parsed top-level YAML mapping (used to detect the
        spec format via the ``spec_version`` discriminator).
    :param executor_block: The ``executor:`` mapping inside *raw*
        (mutated in place).
    :param harness: The ``--harness`` value, e.g. ``"pi"``.
    :raises click.ClickException: If a ``spec_version`` bundle
        declares a non-omnigent ``executor.type`` — those executors
        have no ``config.harness``, so the override cannot apply.
    """
    canonical = canonicalize_harness(harness) or harness
    # "spec_version" is the format discriminator (see is_omnigent_yaml).
    if "spec_version" not in raw:
        executor_block["harness"] = canonical
        return
    etype = str(executor_block.get("type", OMNIGENT_EXECUTOR_TYPE))
    if etype != OMNIGENT_EXECUTOR_TYPE:
        raise click.ClickException(
            f"--harness only applies to specs with executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r}; this spec declares executor.type {etype!r}."
        )
    config = executor_block.get("config")
    if not isinstance(config, dict):
        config = {}
        executor_block["config"] = config
    config["harness"] = canonical


def _validate_agent_spec(agent_path: Path) -> None:
    """
    Parse and validate the agent spec in this process.

    Mirrors the work the server subprocess will do at startup so that
    config errors surface as a clean ``ClickException`` here instead
    of being swallowed by the server's silenced stderr (see
    ``_start_local_server``). Both ``OmnigentError`` (parse/
    validation/env-expansion failures) and ``FileNotFoundError``
    (missing ``config.yaml``) are converted; everything else
    propagates so genuine bugs aren't masked.

    :param agent_path: Path to the agent directory,
        e.g. ``Path("examples/archer")``.
    :raises click.ClickException: If the spec is missing, malformed,
        or references unresolved environment variables.
    """
    try:
        load_spec(agent_path)
    except (OmnigentError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc


def _extract_agent_name(agent_path: Path) -> str:
    """
    Resolve the display name for the REPL banner.

    Accepts both agent-image directories and standalone omnigent
    YAML files. The heavy lifting lives in
    :func:`omnigent.spec.load`, which dispatches on the source
    shape and returns a validated :class:`AgentSpec` whose ``name``
    field is the authoritative label. On any load failure (missing
    ``config.yaml``, malformed YAML, unresolved ``${VAR}``
    references) fall back to a filesystem-derived label so the
    banner always prints — the server subprocess will surface the
    real error moments later.

    :param agent_path: Path to an agent directory or standalone
        omnigent YAML file.
    :returns: The agent name for REPL display.
    """
    try:
        return load_spec(agent_path).name or _fallback_label(agent_path)
    except (OmnigentError, FileNotFoundError):
        # Server subprocess will surface the real error; give the
        # banner SOMETHING to show in the meantime.
        return _fallback_label(agent_path)


def _merge_host_skills(
    agent_spec: AgentSpec,
    spec_path: Path,
) -> list[SkillSpec]:
    """
    Merge bundled skills with host-scope skills for the REPL.

    Discovers ``.claude/skills/`` and ``.agents/skills/`` walking
    up from the agent root, deduplicates by name (bundled wins),
    and returns the combined list.

    :param agent_spec: Parsed AgentSpec with ``.skills`` and
        ``.skills_filter``.
    :param spec_path: Path to the agent YAML or directory.
    :returns: Combined skill list, or empty list.
    """
    bundled: list[SkillSpec] = agent_spec.skills or []
    skills_filter = agent_spec.skills_filter
    agent_root = spec_path if spec_path.is_dir() else spec_path.parent
    host = discover_host_skills(agent_root, skills_filter)
    bundled_names = {s.name for s in bundled}
    merged = list(bundled)
    for hs in host:
        if hs.name not in bundled_names:
            merged.append(hs)
    return merged


def _fallback_label(agent_path: Path) -> str:
    """
    Derive a reasonable display label from a path when the spec
    didn't supply one.

    Directories use the directory name (the standard AGENTSPEC.md
    convention). Files use the stem — e.g. ``foo.yaml`` → ``"foo"``
    — rather than the full filename so the banner doesn't carry
    redundant extensions.

    :param agent_path: Path to an agent directory or YAML file.
    :returns: A human-readable label.
    """
    return agent_path.stem if agent_path.is_file() else agent_path.name


def _canonicalize_local_agent_path(agent_path: Path) -> Path:
    """
    Normalize a local agent path before materialization and bundling.

    Directory-agent bundles are commonly invoked via their root
    ``config.yaml``. Treating that file as a standalone YAML would drop
    sibling directories such as ``agents/`` and ``skills/`` from the
    uploaded bundle, so canonicalize it to the bundle root.

    :param agent_path: Existing local path supplied to ``omnigent run``,
        e.g. ``Path("examples/polly/config.yaml")``.
    :returns: The bundle root for root ``config.yaml`` paths, otherwise
        the original path.
    """
    if agent_path.is_file() and agent_path.name == "config.yaml":
        return agent_path.parent
    return agent_path


def _find_free_port() -> int:
    """
    Find a free TCP port.

    :returns: An available port number.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _omnigent_log_dir() -> Path:
    """
    Resolve the shared Omnigent process log directory.

    Server and captured runner stdout/stderr logs live under the
    same per-user state root as session transcripts and CLI
    diagnostics, rather than under the system temp directory.

    :returns: ``~/.omnigent/logs``, created if needed.
    """
    log_dir = Path.home() / ".omnigent" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _omnigent_persistent_dir() -> Path:
    """
    Resolve the persistent omnigent data directory.

    Honors ``OMNIGENT_DATA_DIR`` (the data-isolation knob a worktree sets
    to avoid sharing ``~/.omnigent/chat.db``), else lives at
    ``~/.omnigent`` alongside the native paths ``sessions/`` and ``logs/``
    (see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md). Created on first access;
    subsequent calls are idempotent.

    Must resolve identically to
    :func:`omnigent.host.local_server._local_data_dir` — the local server
    writes its DB under that dir while ``omnigent run`` reads the resume DB
    from here, so a divergence would silently lose history. ``OMNIGENT_CONFIG_HOME``
    is intentionally not consulted (it isolates config, not data).

    :returns: The absolute path to the persistent dir,
        guaranteed to exist along with the ``artifacts/``
        subdir.
    """
    override = os.environ.get("OMNIGENT_DATA_DIR")
    ap_dir = Path(override).expanduser() if override else Path.home() / ".omnigent"
    ap_dir.mkdir(parents=True, exist_ok=True)
    (ap_dir / "artifacts").mkdir(exist_ok=True)
    return ap_dir


def _start_local_server(
    agent_path: Path,
    port: int,
    *,
    ephemeral: bool = False,
) -> LocalServer:
    """
    Launch a local Omnigent server.

    Server stdout/stderr are routed to ``server.log`` in a
    per-run directory under ``~/.omnigent/logs`` so concurrent Omnigent sessions don't
    interleave. The log path is returned to the caller (via
    :class:`LocalServer`) so :func:`_raise_server_failed`
    can surface it in its error message — critical because
    the REPL only surfaces the wrapped ``PermanentLLMError``
    string, not the underlying cause (e.g. Codex App Server
    403s, missing binaries, credential resolution mismatches).

    The data store (SQLite DB + artifacts) lives at
    ``~/.omnigent/{chat.db,artifacts/}`` by default —
    persistent across runs so ``--continue`` / ``--resume``
    can resume prior conversations
    (designs/RUN_OMNIGENT_SESSION_RESUMPTION.md). Pass
    ``ephemeral=True`` to opt back into a fresh per-run
    tmpdir (the ``--no-session`` shape).

    :param agent_path: Path to the agent directory,
        e.g. ``Path("examples/archer")``.
    :param port: Port the server will listen on, e.g. ``8900``.
    :param ephemeral: When ``True``, place the SQLite DB and
        artifacts in a fresh tmpdir instead of the persistent
        ``~/.omnigent`` location. Used for ``--no-session``
        runs and for tests that want isolation between
        invocations.
    :returns: The server handle bundling the subprocess and
        the path to its captured stdout/stderr log file.
    """
    log_dir = _omnigent_log_dir() / "server"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fd, log_name = tempfile.mkstemp(prefix="server-", suffix=".log", dir=log_dir)
    log_path = Path(log_name)
    log_fh = os.fdopen(log_fd, "wb")
    if ephemeral:
        data_tmpdir = tempfile.mkdtemp(prefix="ap-chat-data-")
        db_path = Path(data_tmpdir) / "chat.db"
        artifact_path = Path(data_tmpdir) / "artifacts"
    else:
        data_dir = _omnigent_persistent_dir()
        db_path = data_dir / "chat.db"
        artifact_path = data_dir / "artifacts"

    # Plain file for stdout/stderr — not subprocess.PIPE. PIPE would
    # deadlock once the kernel's ~64 KB pipe buffer fills (e.g. under
    # the Codex MCP notification firehose) because nothing drains it;
    # a file has no such limit. The child dup's the fd at Popen time,
    # so we close our parent-side handle immediately after spawn —
    # explicit close beats GC ordering for fd lifetime management.
    from omnigent.cli import _start_cli_runner_process
    from omnigent.runner.identity import token_bound_runner_id

    # Generate a binding token and derive the runner_id from it.
    # Both the server and runner receive the token so the server's
    # tunnel route accepts exactly this runner's WebSocket upgrade.
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # Build the server's child environment. The tunnel token lets the
    # server restrict its runner-tunnel allowlist to the sibling runner
    # we spawn below (read by server() via OMNIGENT_RUNNER_TUNNEL_TOKEN).
    #
    # Accounts opt-in: when the parent shell selects accounts mode
    # (OMNIGENT_AUTH_PROVIDER=accounts), inject the per-spawn
    # base URL + cookie secret so the spawned server's
    # AccountsConfig.from_env() can satisfy its required-fields check.
    # Mirrors the same logic in cli.py:_ensure_local_omnigent_server; see
    # the comment block there for the full UX explanation. (Two
    # spawn paths exist because `omnigent run --server ""` goes
    # through _ensure_local_omnigent_server while `omnigent run` with
    # an agent spec and no --server flag goes through here.)
    child_env = {
        **os.environ,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        # Single-user loopback runtime — see ensure_local_omnigent_server for why
        # this lets the host tunnel re-own this machine's host_id across an
        # auth-mode flip without weakening the deployed multi-user boundary.
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    # Mirror create_auth_provider's resolution so this spawn path agrees
    # with the daemon path (host/local_server.py::ensure_local_omnigent_server):
    # header is the env-unset default; OMNIGENT_AUTH_ENABLED=1 opts into
    # accounts (or oidc when OMNIGENT_OIDC_* is set). In header/oidc mode we
    # must NOT mint an accounts cookie secret (those modes never read it).
    from omnigent.server.auth import resolve_auth_source

    _accounts_mode = resolve_auth_source() == "accounts"
    if _accounts_mode:
        if "OMNIGENT_ACCOUNTS_COOKIE_SECRET" not in os.environ:
            child_env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = secrets.token_hex(32)
        # Always override BASE_URL — the parent's value (if any) almost
        # certainly points at a different port than the freshly picked
        # one. Surprises here ("why is my magic URL wrong?") are worse
        # than discarding an out-of-date setting.
        child_env["OMNIGENT_ACCOUNTS_BASE_URL"] = f"http://127.0.0.1:{port}"
    # Propagate executor.profile from the spec as DATABRICKS_CONFIG_PROFILE
    # (spec self-containment: the YAML's own declaration is the only thing
    # that selects a Databricks workspace here — there is no CLI override).
    # This ensures the Omnigent server and its runner subprocess resolve credentials
    # for the right Databricks workspace (LLM calls, compaction, etc.).
    if "DATABRICKS_CONFIG_PROFILE" not in child_env:
        _spec = load_spec(agent_path)
        if _spec.executor.profile:
            child_env["DATABRICKS_CONFIG_PROFILE"] = _spec.executor.profile

    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(artifact_path),
                "--agent",
                str(agent_path),
            ],
            env=child_env,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    finally:
        log_fh.close()

    # Spawn the runner as a sibling subprocess (not a child of the
    # server). The runner retries its WS tunnel connection until the
    # server is ready, so launching them concurrently is safe.
    # If the runner fails to start, kill the already-running server
    # so the caller's finally block doesn't orphan it.
    _prewarm_spec = agent_path if agent_path.exists() else None
    try:
        runner = _start_cli_runner_process(
            server_url=f"http://127.0.0.1:{port}",
            tunnel_token=binding_token,
            runner_id=runner_id,
            workspace_cwd=Path.cwd(),
            prewarm_spec_path=_prewarm_spec,
            isolate_session=True,
            # Route runner stdio to a log file; otherwise its INFO logs
            # paint onto the parent REPL / one-shot stderr.
            capture_logs=True,
        )
    except BaseException:
        _stop_server(server_proc)
        raise

    return LocalServer(
        proc=server_proc,
        log_path=log_path,
        runner_id=runner_id,
        runner_proc=runner.proc,
    )


def _wait_for_server(port: int, server: LocalServer, timeout: float = 45.0) -> None:
    """
    Poll until the server responds.

    :param port: The server port.
    :param server: The launched server handle. Used to detect
        early exit (via ``server.proc.poll()``) and to surface
        ``server.log_path`` in the failure message.
    :param timeout: Max seconds to wait.
    :raises click.ClickException: If the server doesn't start.
    """
    base_url = f"http://127.0.0.1:{port}"
    start = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.proc.poll() is not None:
            _raise_server_failed(server)
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                runner_id = server.runner_id
                if runner_id is None:
                    return
                runner_resp = httpx.get(
                    f"{base_url}/v1/runners/{runner_id}/status",
                    timeout=2.0,
                )
                if runner_resp.status_code == 200 and runner_resp.json()["online"] is True:
                    return
        except httpx.ConnectError:
            pass
        elapsed = time.monotonic() - start
        poll_interval = (
            _SERVER_READY_INITIAL_POLL_SECONDS
            if elapsed < _SERVER_READY_FAST_POLL_WINDOW_SECONDS
            else _SERVER_READY_BACKOFF_POLL_SECONDS
        )
        time.sleep(poll_interval)
    _raise_server_failed(server)


_SERVER_LOG_TAIL_LINES = 50


def _raise_server_failed(server: LocalServer) -> None:
    """
    Raise a descriptive error for a failed server startup.

    Includes the tail of the server log inline so CI failures (where
    the user can't tail the file by hand) carry the underlying
    traceback in the test's stderr. The path is still printed for
    local runs where the user may want the full file.

    :param server: The launched server handle, used to recover
        the subprocess command line and log file location.
    :raises click.ClickException: Always.
    """
    args = server.proc.args
    if isinstance(args, list):
        parts = [p.decode() if isinstance(p, bytes) else str(p) for p in args]
        cmd_display = " ".join(parts)
    elif isinstance(args, bytes):
        cmd_display = args.decode()
    else:
        cmd_display = str(args)
    try:
        lines = server.log_path.read_text(errors="replace").splitlines()
        tail = "\n".join(lines[-_SERVER_LOG_TAIL_LINES:]) if lines else "(empty log file)"
    except OSError as e:
        tail = f"(could not read log file: {e})"
    raise click.ClickException(
        "Server failed to start.\n"
        f"  Server log:  {server.log_path}\n"
        "  Re-run the server directly to see its output:\n"
        f"    {cmd_display}\n"
        f"\n  Last {_SERVER_LOG_TAIL_LINES} lines of {server.log_path}:\n"
        f"{tail}"
    )


def _stop_server(proc: subprocess.Popen[bytes]) -> None:
    """
    Gracefully stop the server subprocess.

    :param proc: The server subprocess.
    """
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=5)


def _stop_local_server(server: LocalServer) -> None:
    """
    Stop both the server and its sibling runner subprocess.

    :param server: The :class:`LocalServer` handle returned by
        :func:`_start_local_server`.
    """
    from omnigent.cli import _stop_cli_runner_process

    if server.runner_proc is not None:
        try:
            _stop_cli_runner_process(server.runner_proc)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            logger.warning(
                "Failed to stop local runner cleanly",
                exc_info=True,
            )
    _stop_server(server.proc)


# ---------------------------------------------------------------------------
# REPL launch
# ---------------------------------------------------------------------------


def _spec_used_families(agent_yaml: Path | None) -> list[str]:
    """Best-effort: the harness surfaces a local agent's harnesses consume.

    Walks the agent's executor harness plus every sub-agent's harness and
    maps each to its surface, so the REPL startup header can show a
    per-surface creds line for a multi-vendor agent (e.g. polly's
    ``claude-sdk`` brain + ``claude-native`` / ``codex-native`` sub-agents
    yield ``["anthropic", "openai"]``; polly's ``pi`` brain adds the
    ``pi`` surface). Parsing is done WITHOUT env expansion — only harness
    names are needed, so unresolved secrets must not make this fail — and
    any error degrades to an empty list (the header simply omits the
    creds line).

    :param agent_yaml: Path to the agent directory, its ``config.yaml``,
        or a standalone YAML file; ``None`` for remote-URL targets.
    :returns: Sorted unique surface names, e.g. ``["anthropic", "openai",
        "pi"]``; empty when *agent_yaml* is ``None``, points at a
        standalone file (no sub-agents), or parsing fails.
    """
    # parse() reads an agent *directory* and discovers sub-agents under
    # ``<root>/agents/``. Resolve the root whether the caller passed the
    # directory itself or its ``config.yaml``. A standalone single-file agent
    # has no sub-agent directory to walk, so the launch harness alone drives
    # the header — skip it here.
    if agent_yaml is None:
        return []
    if agent_yaml.is_dir():
        root = agent_yaml
    elif agent_yaml.name in ("config.yaml", "config.yml"):
        root = agent_yaml.parent
    else:
        return []
    try:
        from omnigent.onboarding.provider_config import PI_SURFACE, harness_family
        from omnigent.spec import parse

        spec = parse(root, expand_env=False)
    except Exception:  # noqa: BLE001 — best-effort startup-header hint: a spec parse must never break `run`
        logger.debug("startup-header family parse failed for %s", agent_yaml, exc_info=True)
        return []

    families: set[str] = set()

    def _walk(node: AgentSpec) -> None:
        """Accumulate the surface for *node*'s harness and recurse into sub-agents."""
        harness = canonicalize_harness(node.executor.harness_kind) or node.executor.harness_kind
        fam = harness_family(harness)
        if fam is not None:
            families.add(fam)
        elif harness == PI_SURFACE:
            # pi spans both model families, so it has no single family —
            # it contributes its own surface, and the header resolves that
            # surface's effective credential (explicit pi default, else
            # the cross-family fallback).
            families.add(PI_SURFACE)
        for child in node.sub_agents:
            _walk(child)

    _walk(spec)
    return sorted(families)


def _run_repl(
    base_url: str,
    agent_name: str,
    tool_handler: ToolHandler | None,
    *,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    fork_session_id: str | None = None,
    log: bool = False,
    agent_yaml: Path | None = None,
    runner_id: str | None = None,
    runner_recover: Callable[[], str] | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    ephemeral: bool = False,
    debug_events: bool = False,
    server_log_path: Path | None = None,
    runner_log_path: Path | None = None,
    resume_parts: list[str] | None = None,
    skills: list[SkillSpec] | None = None,
    auto_open_conversation: bool = False,
    attach_only: bool = False,
    attach_harness: str | None = None,
) -> None:
    """
    Open the REPL connected to the server.

    :param base_url: Server base URL.
    :param agent_name: Agent name to chat with.
    :param tool_handler: Optional client-side tool handler.
    :param initial_message: If set, auto-send on REPL start (maps
        to ``run_repl``'s ``initial_message`` kwarg, which the REPL
        treats as the first user turn — same hook the onboarding
        flow uses to auto-greet the user).
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing conversation (replays recent
        items, threads new turns onto the existing
        ``previous_response_id``) rather than starting a fresh
        one. Resolved upstream from ``--continue`` /
        ``--resume <id>``.
    :param fork_session_id: When set, fork this session before
        entering the REPL. The REPL opens attached to the fork.
        Resolved upstream from ``--fork ID``.
    :param log: When ``True``, write a JSON dump of the active
        conversation to ``~/.omnigent/logs/`` on REPL exit.
        Maps to ``--log`` on the CLI.
    :param agent_yaml: Path to the agent spec on the local
        filesystem, when known. Threaded through to the tmux
        pane-integration helper so a sibling pane spawned via
        ``prefix + <split-key>`` can re-launch the same agent.
        ``None`` for remote-URL targets (the spec lives on the
        server, not locally) — the chooser falls back to
        ``OPT_LAUNCH_ARGV`` in that case.
    :param runner_id: Optional preferred runner id to send on
        requests, e.g. ``"runner_0123456789abcdef"``.
    :param runner_recover: Optional callback that restarts the
        local runner if it has exited and returns the live runner
        id.
    :param session_bundle: Optional gzipped agent bundle bytes used
        to create a fresh sessions-API session. Required for fresh
        sessions.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :param ephemeral: When ``True``, suppress the resume hint on
        exit — the session data lives in a tmpdir that's gone
        after the process exits, so the hint would be misleading.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline (event tape overlay, JSONL log, toolbar counters).
        Maps to ``--debug-events`` on the CLI.
    :param server_log_path: Path to the local server's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/server/server-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` for remote servers.
    :param runner_log_path: Path to the local runner's
        stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/runner/runner-abc123.log")``. Shown in the
        Ctrl+O debug overview. ``None`` when no local runner is used.
    :param resume_parts: Pre-built argument list prefix for the
        resume command shown on exit, e.g.
        ``["omnigent", "run", "agent.yaml", "--harness", "codex"]``.
        ``None`` uses the current process argv.
    :param skills: Parsed skill list from the agent spec, e.g.
        ``[SkillSpec(name="code-review", ...)]``. Each skill is
        registered as a ``/<name>`` slash command in the REPL.
        ``None`` (default) means no skill commands are registered.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL when the session id becomes known.
    """
    from omnigent.repl import run_repl
    from omnigent.repl._session_log import DEFAULT_LOG_DIR
    from omnigent.repl._tmux_pane import register_pane

    log_dir = DEFAULT_LOG_DIR if log else None

    # Mark this tmux pane as an omnigent context source and wrap
    # the user's prefix-table split bindings. No-op outside tmux.
    # ``resume_conversation_id`` (if present) becomes the pane's
    # initial conv id; otherwise a placeholder is used until the
    # first conversation is created.
    register_pane(
        conv_id=resume_conversation_id,
        agent_name=agent_name,
        agent_yaml=agent_yaml,
        launch_argv=list(sys.argv),
        server_url=base_url,
    )

    conversation_id: str | None = None

    async def _main() -> None:
        nonlocal conversation_id

        # Route unhandled asyncio exceptions (fire-and-forget tasks,
        # "Task exception was never retrieved") to the CLI diagnostics
        # log instead of stderr noise.
        from omnigent.cli_diagnostics import install_asyncio_exception_handler

        install_asyncio_exception_handler(asyncio.get_running_loop())

        # Derive the launch harness from the local spec so the REPL's
        # `/model` readout knows the right harness (and thus the right
        # provider family) before the first turn binds the session. ``None``
        # for URL targets / bundles, where the snapshot's harness fills it in.
        launch_harness: str | None = None
        agent_description: str | None = None
        if agent_yaml is not None:
            # Resolve the spec's config.yaml whether the user passed the agent
            # directory, its config.yaml, or a standalone single-file YAML — so
            # the startup header (harness → model + credential, summary)
            # populates in every case (a bare directory path would otherwise
            # peek at a directory and yield nothing).
            spec_config = agent_yaml / "config.yaml" if agent_yaml.is_dir() else agent_yaml
            raw_spec = _load_yaml_if_single_file(spec_config)
            executor = raw_spec.get("executor") if isinstance(raw_spec, dict) else None
            if isinstance(executor, dict):
                harness_name = executor.get("harness")
                if not harness_name and isinstance(executor.get("config"), dict):
                    harness_name = executor["config"].get("harness")
                if isinstance(harness_name, str) and harness_name:
                    launch_harness = canonicalize_harness(harness_name) or harness_name
            # One-line summary for the startup header (folded scalars are
            # normalized to a single line by the header builder).
            if isinstance(raw_spec, dict):
                desc = raw_spec.get("description")
                if isinstance(desc, str) and desc.strip():
                    agent_description = desc

        # Families the agent's harnesses (incl. sub-agents) consume — drives
        # the per-family creds line in the startup header for multi-vendor
        # agents like polly. Best-effort; empty on any failure.
        used_families = _spec_used_families(agent_yaml)

        # Attach has no local spec; the host's harness comes from the session
        # snapshot so the (lean) attach banner reflects what's actually running.
        if attach_harness is not None:
            launch_harness = attach_harness

        async with OmnigentClient(
            base_url=base_url,
            headers=_server_headers(runner_id=runner_id),
            auth=_server_auth(server_url=base_url),
        ) as client:
            # When --fork is set, call the fork endpoint before
            # entering the REPL so the user lands in the fork.
            effective_resume_id = resume_conversation_id
            if fork_session_id is not None:
                try:
                    fork_result = await client.sessions.fork(fork_session_id)
                except Exception as exc:
                    raise click.ClickException(f"Fork failed: {exc}") from exc
                effective_resume_id = fork_result["id"]
                click.echo(
                    f"Conversation forked. To return to the previous "
                    f"conversation, run --resume {fork_session_id}",
                    err=True,
                )
            conversation_id = await run_repl(
                client,
                agent_name,
                tool_handler,
                initial_message=initial_message,
                resume_conversation_id=effective_resume_id,
                log_dir=log_dir,
                debug_events=debug_events,
                server_log_path=server_log_path,
                runner_log_path=runner_log_path,
                session_bundle=session_bundle,
                session_bundle_filename=session_bundle_filename,
                runner_id=runner_id,
                runner_recover=runner_recover,
                resume_parts=resume_parts,
                ephemeral=ephemeral,
                skills=skills,
                server_url=base_url,
                harness=launch_harness,
                agent_description=agent_description,
                used_families=used_families,
                attach_only=attach_only,
                on_session_start=(
                    lambda session_id: open_conversation_link_if_enabled(
                        base_url=base_url,
                        conversation_id=session_id,
                        enabled=auto_open_conversation,
                        warn=lambda message: click.echo(message, err=True),
                    )
                ),
            )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())


def _run_one_shot(
    *,
    base_url: str,
    agent_name: str,
    tool_handler: ToolHandler | None,
    prompt: str,
    runner_id: str | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    resume_conversation_id: str | None = None,
    auto_open_conversation: bool = False,
) -> None:
    """
    Send a single prompt to a remote server and print the final text.

    :param base_url: Remote server base URL.
    :param agent_name: Registered agent name to invoke.
    :param tool_handler: Optional client-side tool handler for local
        tool execution.
    :param prompt: User prompt to send as the single turn.
    :param runner_id: Optional preferred runner id to send on
        requests, e.g. ``"runner_0123456789abcdef"``.
    :param session_bundle: Optional gzipped agent bundle bytes used
        to create a fresh sessions-API session.
    :param session_bundle_filename: Multipart filename, e.g.
        ``"agent.tar.gz"``.
    :param resume_conversation_id: When set, resumes an existing
        session instead of creating a new one, e.g.
        ``"conv_abc123"``. ``None`` creates a fresh session.
    :param auto_open_conversation: When ``True``, open the
        browser conversation URL after the session is created or resumed.
    :returns: None.
    """

    async def _main() -> None:
        """Run the one-shot SDK query inside an async client context."""
        async with OmnigentClient(
            base_url=base_url,
            headers=_server_headers(runner_id=runner_id),
            auth=_server_auth(server_url=base_url),
        ) as client:
            if session_bundle is not None:
                text = await _query_sessions_once(
                    client=client,
                    agent_name=agent_name,
                    tool_handler=tool_handler,
                    prompt=prompt,
                    session_bundle=session_bundle,
                    session_bundle_filename=session_bundle_filename,
                    runner_id=runner_id,
                    resume_conversation_id=resume_conversation_id,
                    on_session_ready=(
                        lambda session_id: open_conversation_link_if_enabled(
                            base_url=base_url,
                            conversation_id=session_id,
                            enabled=auto_open_conversation,
                            warn=lambda message: click.echo(message, err=True),
                        )
                    ),
                )
                if text:
                    click.echo(text)
                return
            result = await client.query(
                model=agent_name,
                input=prompt,
                tool_handler=tool_handler,
            )
            if result.text:
                click.echo(result.text)

    try:
        asyncio.run(_main())
    except ClientOmnigentError as exc:
        # A turn that fails before the LLM stream starts (SETUP-phase
        # failure: spec resolution, spawn-env build) ends with only a
        # ``session.status: failed`` event, which SessionsChat.send
        # raises as an OmnigentError. Surface its message as a clean
        # CLI error instead of an opaque traceback so ``-p`` users see
        # why the turn produced no output.
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# Client-side tools
# ---------------------------------------------------------------------------


def _load_tool_handler(name: str) -> ToolHandler:
    """
    Load a client-side tool set by name and wrap it as a ToolHandler.

    Prefers the modern ``@tool``-decorated functions (exposed
    by the tool set as ``_TOOL_FNS``) so the SDK's D6 lifecycle
    can detect ``synchronous: false`` properties on the wire
    schema. Falls back to the legacy ``TOOLS`` + ``execute_tool``
    surface for tool sets that haven't migrated yet — same
    behavior as before, just constructed manually rather than
    via ``build_tool_handler``.

    :param name: Tool set name, e.g. ``"coding"``.
    :returns: A ToolHandler with schemas and execute function.
    :raises click.ClickException: If the tool set is not found.
    """
    try:
        from omnigent.client_tools import get_tool_set

        tool_set = get_tool_set(name)
    except (ImportError, SystemExit) as exc:
        raise click.ClickException(
            f"Tool set {name!r} not found. Available: coding, async_demo"
        ) from exc

    # `_TOOL_FNS` is the "modern path" marker on a tool-set module —
    # @tool-decorated functions exported as a module-level list.
    # Legacy tool-sets instead expose a `build()` callable. Use
    # hasattr so mypy can narrow the attribute access below.
    if hasattr(tool_set, "_TOOL_FNS"):
        fns = tool_set._TOOL_FNS
        # Modern path: @tool-decorated functions. The SDK's
        # build_tool_handler derives schemas from type hints +
        # docstrings, strips ``synchronous`` routing hints
        # before invoking the user fn, and bridges sync vs
        # async ``execute`` correctly.
        from omnigent_client.tools import build_tool_handler

        return build_tool_handler(fns)

    # Legacy path: hand-written TOOLS dict + sync
    # execute_tool dispatcher.
    def execute(call: ToolCallInfo) -> str:
        """
        Execute a client-side tool call (legacy sync path).

        :param call: The tool call info with name and arguments.
        :returns: The tool result string.
        """
        return str(tool_set.execute_tool(call.name, call.arguments))

    return ToolHandler(schemas=tool_set.TOOLS, execute=execute)
