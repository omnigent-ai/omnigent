"""Runner-side session resource registry.

Authoritative owner/facade for all session-scoped resources: the
primary OS environment, terminal instances, and terminal-specific
environments.  The public server API and runner-local tools call
this registry rather than reaching into ``TerminalRegistry`` or
``create_os_environment()`` directly.

See ``designs/SESSION_RESOURCES_API_DESIGN.md`` §Runner internal model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from cachetools import TTLCache

from omnigent.entities.pagination import PagedList
from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    SessionResourceView,
    filter_resources_by_type,
    get_resource_by_id,
    list_session_resources_from_terminal_registry,
    terminal_environment_resource_id,
    terminal_resource_id,
    terminal_resource_view,
)

if TYPE_CHECKING:
    from omnigent.inner.os_env import OSEnvironment
    from omnigent.terminals.registry import TerminalRegistry

_logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_ROOT = os.path.join(
    os.environ.get("TMPDIR", "/tmp"),
    "omnigent-sessions",
)

CODEX_NATIVE_TERMINAL_ROLE = "codex-native"
CLAUDE_NATIVE_TERMINAL_ROLE = "claude-native"
PI_NATIVE_TERMINAL_ROLE = "pi-native"
OPENCODE_NATIVE_TERMINAL_ROLE = "opencode-native"
CURSOR_NATIVE_TERMINAL_ROLE = "cursor-native"
GOOSE_NATIVE_TERMINAL_ROLE = "goose-native"
# Role marker for the runner-owned native Antigravity (agy) TUI terminal.
# A generic terminal launched with ``terminal=antigravity`` shares the same
# public resource id, so the ensure path uses this private marker to tell a
# runner-owned agy TUI apart from an arbitrary terminal before reusing it.
ANTIGRAVITY_NATIVE_TERMINAL_ROLE = "antigravity-native"
QWEN_NATIVE_TERMINAL_ROLE = "qwen-native"
HERMES_NATIVE_TERMINAL_ROLE = "hermes-native"
# Role marker for the embedded Omnigent REPL terminal auto-created for
# runner-hosted SDK sessions (``omnigent attach`` in a tmux pane — the
# SDK mirror of the native terminals above). The attach WebSocket uses
# this marker to recreate the terminal when its tmux session has died
# (the REPL exited or crashed) instead of rejecting the attach.
OMNIGENT_REPL_TERMINAL_ROLE = "omnigent-repl"

_IS_ALIVE_CACHE_TTL_S = 2.0
_IS_ALIVE_CACHE_MAX = 256

# Diff-track idle threshold (seconds) for the claude-native agent
# terminal's status watcher. Claude Code redraws its busy line every
# ~200ms while a turn is in progress, so a poll that sees no pane change
# only happens once Claude has actually stopped — making a short
# threshold safe against mid-turn false-idle. Kept distinct from the
# generic terminal-activity watcher's longer default so the session's
# "Working…" indicator flips to idle promptly (~1s) after Claude stops,
# matching the responsiveness of the hook-based ``Stop`` edge it
# replaces.
_CLAUDE_NATIVE_STATUS_IDLE_THRESHOLD_SECONDS = 1.0

# Poll interval (seconds) for the claude-native agent terminal's status
# watcher. Tighter than the generic terminal-activity watcher's 1s so the
# session's running/idle transitions feel responsive (~200ms) and so a
# pane change can be attributed to a recent client interaction within a
# tight window. Applied ONLY to this watcher (gated by the role) so we
# don't 5x the capture-pane subprocess load on every terminal.
_CLAUDE_NATIVE_STATUS_POLL_INTERVAL_SECONDS = 0.2

# Minimum wall-clock interval (seconds) between consecutive
# ``session.terminal.activity`` emissions for a single terminal. The
# claude-native agent terminal polls its pane every 200ms
# (:data:`_CLAUDE_NATIVE_STATUS_POLL_INTERVAL_SECONDS`) and Claude redraws
# its busy line on nearly every poll, so emitting an event on every
# pane-changed tick would push ~5 activity events/second for the whole
# turn. The web only needs a pulse inside its 1.5s "active" window
# (``ACTIVE_OUTPUT_WINDOW_MS`` in ``useTerminalStatuses``) to keep the
# badge lit, so coalescing to at most one emit per second cuts the event
# volume ~5x while keeping the badge solid. Generic terminals already poll
# at 1s, so this throttle is a no-op for them (their own poll spacing
# already exceeds the threshold).
_TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS = 1.0
_TERMINAL_EXIT_OUTPUT_MAX_LINES = 40
_TERMINAL_EXIT_OUTPUT_MAX_CHARS = 4000


class TerminalLifecycle(Enum):
    """Session-lifecycle relationship for a terminal resource."""

    REQUIRED = "required"
    AUXILIARY = "auxiliary"


@dataclass(frozen=True)
class TerminalExitEvent:
    """Terminal exit event emitted by :class:`SessionResourceRegistry`.

    :param session_id: Owning session/conversation identifier.
    :param terminal_id: Opaque terminal resource id.
    :param terminal_name: Terminal spec/resource name.
    :param session_key: Per-launch terminal key.
    :param lifecycle: Required/auxiliary lifecycle relationship.
    :param command: Executable launched in the terminal, if known.
    :param args_count: Number of arguments passed to the executable, if known.
        The event intentionally does not expose argv contents because terminal
        specs may contain credentials or other launch-only secrets.
    :param cwd: Working directory used to launch the terminal, if known.
    :param last_output: Last visible pane text captured before exit, if any.
    :param session_was_idle: Whether the session's last PTY-derived status was
        ``idle`` at exit. ``True`` marks a clean shutdown after the turn
        finished; ``False`` (the default — last seen ``running``, or never
        observed) keeps a mid-turn crash or boot failure a failure.
    """

    session_id: str
    terminal_id: str
    terminal_name: str
    session_key: str
    lifecycle: TerminalLifecycle
    command: str | None = None
    args_count: int | None = None
    cwd: str | None = None
    last_output: str | None = None
    session_was_idle: bool = False


def _trim_terminal_exit_output(text: str | None) -> str | None:
    """Bound terminal-output diagnostics so a failure report stays compact."""
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    lines = stripped.splitlines()
    if len(lines) > _TERMINAL_EXIT_OUTPUT_MAX_LINES:
        lines = [
            f"... omitted {len(lines) - _TERMINAL_EXIT_OUTPUT_MAX_LINES} earlier line(s) ...",
            *lines[-_TERMINAL_EXIT_OUTPUT_MAX_LINES:],
        ]
    clipped = "\n".join(lines)
    if len(clipped) > _TERMINAL_EXIT_OUTPUT_MAX_CHARS:
        clipped = (
            f"... omitted {len(clipped) - _TERMINAL_EXIT_OUTPUT_MAX_CHARS} "
            "earlier character(s) ...\n"
            f"{clipped[-_TERMINAL_EXIT_OUTPUT_MAX_CHARS:]}"
        )
    return clipped


def _terminal_exit_diagnostics(
    instance: Any | None,
) -> tuple[str | None, int | None, str | None, str | None]:
    """Extract generic launch/output diagnostics from a terminal instance."""
    if instance is None:
        return None, None, None, None

    raw_command = getattr(instance, "command", None)
    command = raw_command if isinstance(raw_command, str) and raw_command else None

    raw_args = getattr(instance, "args", None)
    args_count = len(raw_args) if isinstance(raw_args, list) else None

    raw_cwd = getattr(instance, "launch_cwd", None)
    cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else None

    last_output: str | None = None
    read_last_output = getattr(instance, "last_pane_text", None)
    if callable(read_last_output):
        try:
            raw_last_output = read_last_output()
        except Exception:
            _logger.exception("Failed to read terminal pane diagnostics")
        else:
            if isinstance(raw_last_output, str):
                last_output = _trim_terminal_exit_output(raw_last_output)

    return command, args_count, cwd, last_output


def _monotonic() -> float:
    """Return a monotonic timestamp for activity-emit throttling.

    Thin indirection over :func:`time.monotonic` so tests can patch this
    module-local symbol (per the project's mock-integrity guidance)
    instead of mutating the process-wide ``time`` module.

    :returns: Seconds from an arbitrary monotonic reference point.
    """
    return time.monotonic()


def _sanitize_session_id(session_id: str) -> str:
    """Sanitize a session id for safe use as a filesystem path component.

    :param session_id: Raw session/conversation identifier,
        e.g. ``"conv_abc123"`` or ``"user/session"``.
    :returns: Sanitized string safe for directory names.
    """
    return session_id.replace("/", "_").replace("..", "_")


def _session_workspace(session_id: str) -> str:
    """Compute the workspace root for a session.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :returns: Absolute path to the session workspace directory.
    """
    root = os.environ.get(
        "OMNIGENT_RUNNER_OS_ENV_ROOT",
        _DEFAULT_WORKSPACE_ROOT,
    )
    return os.path.join(root, _sanitize_session_id(session_id), "workspace")


class SessionResourceRegistry:
    """Runner-side registry that owns session-scoped resources.

    Wraps :class:`TerminalRegistry` for terminal resources and
    manages the primary :class:`OSEnvironment` per session.  The
    primary environment is lazily materialized on first
    :meth:`resolve_environment` call with
    ``DEFAULT_ENVIRONMENT_ID``.

    :param terminal_registry: Existing terminal registry.
        ``None`` in test setups without terminals.
    :param runner_workspace: Optional CLI launch workspace.  When set,
        sessions whose spec cwd is unset or a placeholder (``"."``)
        use this path instead of a per-session temp directory, keeping
        the OS environment cwd aligned with the filesystem-registry
        watch path.
    :param per_session_workspace: When ``True`` and *runner_workspace*
        is set, each session gets an isolated subdirectory under
        *runner_workspace* instead of sharing the root.
        Out-of-process (shared) runners should set this to ``True``;
        in-process single-user runners leave it ``False`` so the agent
        sees the project root directly.
    """

    def __init__(
        self,
        terminal_registry: TerminalRegistry | None = None,
        runner_workspace: Path | None = None,
        *,
        per_session_workspace: bool = False,
    ) -> None:
        self._terminal_registry = terminal_registry
        self._runner_workspace = runner_workspace
        self._per_session_workspace = per_session_workspace
        self._primary_envs: dict[str, OSEnvironment] = {}
        self._terminal_roles: dict[tuple[str, str], str] = {}
        self._terminal_lifecycles: dict[tuple[str, str], TerminalLifecycle] = {}
        self._is_alive_cache: TTLCache[str, bool] = TTLCache(
            maxsize=_IS_ALIVE_CACHE_MAX,
            ttl=_IS_ALIVE_CACHE_TTL_S,
        )
        self._lock = threading.Lock()
        # Optional callback ``(session_id, terminal_id) -> None`` invoked
        # (on the event loop) when a terminal's pane produces output, so
        # the runner can emit a ``session.terminal.activity`` SSE event.
        # Set by the runner via :meth:`set_terminal_activity_publisher`.
        self._terminal_activity_publisher: Callable[[str, str], None] | None = None
        # Optional callback ``(session_id, status) -> None`` invoked (on
        # the event loop) when the claude-native *agent* terminal's pane
        # crosses an activity/idle edge, so the runner can emit a
        # ``session.status`` event. This is the PTY-activity-derived
        # working status that replaces the hook-based ``UserPromptSubmit``
        # → running / ``Stop`` → idle bracketing. Set by the runner via
        # :meth:`set_session_status_publisher`.
        self._session_status_publisher: Callable[[str, str], None] | None = None
        # Latest PTY-derived status (running/idle) per session. Lets
        # :meth:`_handle_terminal_exit` tell a clean shutdown (idle) from a
        # mid-turn crash. Written from the watcher thread and the turn-start
        # hook; all access goes through the ``_*_session_status_memo`` helpers
        # under ``self._lock``.
        self._last_session_status: dict[str, str] = {}
        # Optional callback invoked on the event loop when a watched terminal
        # disappears unexpectedly. The callback receives the terminal's
        # lifecycle relationship so the runner can decide whether the owning
        # session should fail.
        self._terminal_exit_publisher: Callable[[TerminalExitEvent], None] | None = None

    def set_terminal_activity_publisher(
        self,
        publisher: Callable[[str, str], None],
    ) -> None:
        """Install the terminal-activity publisher.

        The runner passes a callback that publishes a
        ``session.terminal.activity`` event onto the session's SSE
        queue. It is invoked on the event loop (the watcher thread hops
        via ``loop.call_soon_threadsafe``), so the callback itself may
        use the loop-only ``_publish_event`` directly.

        :param publisher: Callable ``(session_id, terminal_id) -> None``.
        """
        self._terminal_activity_publisher = publisher

    def set_session_status_publisher(
        self,
        publisher: Callable[[str, str], None],
    ) -> None:
        """Install the PTY-activity-derived session-status publisher.

        The runner passes a callback that publishes a ``session.status``
        event onto the session's SSE queue (which the Omnigent server relays
        through its normal status path). It is invoked on the event loop
        (the watcher thread hops via ``loop.call_soon_threadsafe``), so
        the callback itself may use the loop-only ``_publish_event``
        directly. Only the claude-native agent terminal's watcher calls
        it — see :meth:`_start_terminal_activity_watcher`.

        :param publisher: Callable ``(session_id, status) -> None`` where
            *status* is ``"running"`` or ``"idle"``.
        """
        self._session_status_publisher = publisher

    def set_terminal_exit_publisher(
        self,
        publisher: Callable[[TerminalExitEvent], None],
    ) -> None:
        """Install the terminal-exit publisher.

        The runner passes a callback that publishes resource/session lifecycle
        events when a watched terminal disappears. It is invoked on the event
        loop, not on the watcher thread.

        Mental model:
            required terminal:
                If this terminal dies, the session is dead.

            auxiliary terminal:
                If this terminal dies, only this terminal resource is gone.

        :param publisher: Callable receiving a :class:`TerminalExitEvent`.
        """
        self._terminal_exit_publisher = publisher

    def _set_session_status_memo(self, session_id: str, status: str) -> None:
        """Record the session's latest PTY status for exit classification."""
        with self._lock:
            self._last_session_status[session_id] = status

    def _take_session_status_memo(self, session_id: str) -> str | None:
        """Pop and return the session's recorded PTY status (or ``None``)."""
        with self._lock:
            return self._last_session_status.pop(session_id, None)

    def note_session_turn_started(self, session_id: str) -> None:
        """Mark a session as having an in-flight turn.

        Closes the window between a new turn starting and the watcher's first
        ``running`` edge: without it, a crash in that gap would read the prior
        turn's stale ``idle`` and be misclassified as a clean shutdown. The
        watcher flips the memo back to ``idle`` once the turn completes.

        :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
        """
        self._set_session_status_memo(session_id, "running")

    @property
    def terminal_registry(self) -> TerminalRegistry | None:
        """The wrapped terminal registry."""
        return self._terminal_registry

    def terminal_resource_role(
        self,
        session_id: str,
        terminal_id: str,
    ) -> str | None:
        """Return the internal role marker for a terminal resource.

        Role markers are runner-private state used to distinguish
        runner-owned native terminals from generic terminals with the same
        public id. They are intentionally not projected into public resource
        metadata.

        :param session_id: Session/conversation identifier, e.g.
            ``"conv_abc123"``.
        :param terminal_id: Opaque terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: Role marker such as ``"codex-native"``, or ``None``.
        """
        with self._lock:
            return self._terminal_roles.get((session_id, terminal_id))

    def list_resources(
        self,
        session_id: str,
        *,
        resource_type: Literal["environment", "terminal", "file"] | None = None,
        agent_spec: Any | None = None,
    ) -> PagedList[SessionResourceView]:
        """List all resources for a session.

        Includes the logical default environment, running terminals,
        and terminal-specific environments.  When *agent_spec* is
        provided and has no ``os_env``, the default environment
        resource is omitted from the listing.

        :param session_id: Session/conversation identifier.
        :param resource_type: Optional filter by resource type.
        :param agent_spec: Optional agent spec.  When provided and
            ``agent_spec.os_env`` is ``None``, the logical default
            environment resource is suppressed.  ``None`` (the default)
            preserves legacy behaviour and always includes the default
            environment.
        :returns: Paginated list of session resources.
        """
        primary_os_env_spec = (
            getattr(agent_spec, "os_env", None) if agent_spec is not None else None
        )
        has_os_env = agent_spec is None or primary_os_env_spec is not None
        page = list_session_resources_from_terminal_registry(
            session_id,
            self._terminal_registry,
            has_os_env=has_os_env,
            primary_os_env_spec=primary_os_env_spec,
        )
        if resource_type is not None:
            return filter_resources_by_type(page, resource_type)
        return page

    def get_resource(
        self,
        session_id: str,
        resource_id: str,
    ) -> SessionResourceView | None:
        """Find a single resource by id.

        :param session_id: Session/conversation identifier.
        :param resource_id: Opaque resource id,
            e.g. ``"default"`` or ``"terminal_bash_s1"``.
        :returns: The matching resource or ``None``.
        """
        page = list_session_resources_from_terminal_registry(
            session_id,
            self._terminal_registry,
        )
        return get_resource_by_id(page, resource_id)

    async def get_terminal_resource(
        self,
        session_id: str,
        terminal_id: str,
    ) -> SessionResourceView | None:
        """
        Return a terminal resource after verifying tmux is still alive.

        ``TerminalInstance.running`` is an optimistic in-memory flag.
        A terminal command can exit and take down the tmux server before
        any send/read/close path updates that flag. Terminal GET uses
        this method so clients do not reconnect to a stale socket that
        can only print tmux's ``"no sessions"`` error.

        The ``is_alive()`` subprocess probe is cached for a short TTL
        so rapid polling from web clients does not fork a ``tmux
        has-session`` process on every request.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_claude_main"``.
        :returns: A terminal resource view when the matching tmux
            server is alive; otherwise ``None``.
        """
        if self._terminal_registry is None:
            return None

        for entry in self._terminal_registry.list_for_conversation(
            session_id,
        ):
            if terminal_resource_id(entry.terminal_name, entry.session_key) != terminal_id:
                continue
            if not entry.instance.running:
                return None

            cache_key = f"{session_id}:{terminal_id}"
            cached = self._is_alive_cache.get(cache_key)
            if cached is not None:
                return terminal_resource_view(session_id, entry) if cached else None

            alive = await entry.instance.is_alive()
            if alive:
                self._is_alive_cache[cache_key] = True
            else:
                self._is_alive_cache.pop(cache_key, None)
                return None
            return terminal_resource_view(session_id, entry)
        return None

    def resolve_environment(
        self,
        session_id: str,
        environment_id: str,
        agent_spec: Any | None = None,
    ) -> OSEnvironment:
        """Resolve an environment id to a live OSEnvironment.

        For ``DEFAULT_ENVIRONMENT_ID``, lazily creates the primary
        environment from the agent spec (or synthesizes a default).
        For terminal environment ids, resolves from the terminal
        registry.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param agent_spec: Agent spec for primary env creation.
        :returns: The live :class:`OSEnvironment`.
        :raises ValueError: If the environment id cannot be resolved.
        """
        if environment_id == DEFAULT_ENVIRONMENT_ID:
            return self._resolve_primary(session_id, agent_spec)

        if self._terminal_registry is not None:
            for entry in self._terminal_registry.list_for_conversation(
                session_id,
            ):
                if not entry.instance.running:
                    continue
                env_id = terminal_environment_resource_id(
                    entry.terminal_name,
                    entry.session_key,
                )
                if env_id == environment_id and entry.instance.os_env is not None:
                    return entry.instance.os_env

        raise ValueError(f"Environment {environment_id!r} not found for session {session_id!r}")

    def _resolve_primary(
        self,
        session_id: str,
        agent_spec: Any | None,
    ) -> OSEnvironment:
        """Get or create the primary OSEnvironment for a session.

        :param session_id: Session/conversation identifier.
        :param agent_spec: Agent spec for env creation.
        :returns: The primary :class:`OSEnvironment`.
        """
        with self._lock:
            cached = self._primary_envs.get(session_id)
            if cached is not None:
                return cached

            os_env = self._create_primary_env(session_id, agent_spec)
            self._primary_envs[session_id] = os_env
            return os_env

    def _create_primary_env(
        self,
        session_id: str,
        agent_spec: Any | None,
    ) -> OSEnvironment:
        """Create a new primary OSEnvironment.

        Follows the creation policy from the design:
        1. If agent_spec.os_env exists, clone it
        2. Resolve cwd to session workspace if unset
        3. If agent_spec is None, synthesize a default spec

        The default branch (no agent_spec) serves the
        filesystem-resource endpoints — a read view, never agent
        tool execution. Pin ``sandbox.type="none"`` so it can't
        inherit the Linux platform default (bwrap), which
        raises when the ``bwrap`` binary is missing and broke the
        working-folder panel for runners without it.

        :param session_id: Session/conversation identifier.
        :param agent_spec: Agent spec for env creation.
        :returns: The newly created :class:`OSEnvironment`.
        :raises ValueError: If ``agent_spec`` is provided but its
            ``os_env`` field is ``None``.  Callers must gate on
            ``os_env`` presence before materialising an environment.
        """
        from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
        from omnigent.inner.os_env import create_os_environment

        # Prefer the CLI launch workspace so that the OS environment
        # cwd matches the filesystem-registry watch path.  Fall back
        # to the per-session temp dir for remote/cloud runners that
        # have no workspace affinity.
        if self._runner_workspace is not None:
            if self._per_session_workspace:
                # Isolate sessions under the shared workspace.
                default_cwd = str(self._runner_workspace / _sanitize_session_id(session_id))
                os.makedirs(default_cwd, mode=0o700, exist_ok=True)
                os.chmod(default_cwd, 0o700)  # ensure mode even if pre-existing
            else:
                default_cwd = str(self._runner_workspace)
        else:
            default_cwd = _session_workspace(session_id)
            # Restrict workspace visibility on shared hosts.
            os.makedirs(default_cwd, mode=0o700, exist_ok=True)
            os.chmod(default_cwd, 0o700)  # ensure mode even if pre-existing

        if agent_spec is not None:
            spec_os_env = getattr(agent_spec, "os_env", None)
            if spec_os_env is None:
                raise ValueError(
                    "Agent spec has no os_env; cannot create a primary filesystem environment."
                )
            # Precedence per designs/SESSION_WORKSPACE_SELECTION.md:
            # runner_workspace (env-var-driven) ALWAYS wins when set.
            # Otherwise the spec's absolute cwd wins; otherwise we
            # fall back to the per-session tmpdir (default_cwd).
            if (
                self._runner_workspace is not None
                or spec_os_env.cwd is None
                or spec_os_env.cwd in (".", "./")
            ):
                cwd = default_cwd
            else:
                cwd = spec_os_env.cwd
            effective_spec = OSEnvSpec(
                type=spec_os_env.type,
                cwd=cwd,
                sandbox=spec_os_env.sandbox,
                fork=spec_os_env.fork,
                start_in_scratch=spec_os_env.start_in_scratch,
            )
            env = create_os_environment(effective_spec)
            if env is not None:
                return env

        default_spec = OSEnvSpec(
            type="caller_process",
            cwd=default_cwd,
            sandbox=OSEnvSandboxSpec(type="none"),
        )
        env = create_os_environment(default_spec)
        if env is None:
            raise RuntimeError(
                f"Failed to create default OS environment for session {session_id!r}"
            )
        return env

    def compute_default_env_root(
        self,
        session_id: str,
        agent_spec: Any | None,
    ) -> str | None:
        """Compute the resolved filesystem root for the default environment.

        Mirrors the cwd resolution logic in :meth:`_create_primary_env` without
        materializing the :class:`OSEnvironment`.  Safe to call from listing and
        single-fetch endpoints that must remain logical/lazy.

        Precedence (per
        designs/SESSION_WORKSPACE_SELECTION.md "How this maps onto runtime"):

        1. ``self._runner_workspace`` (sourced from
           ``OMNIGENT_RUNNER_WORKSPACE``) — when set, ALWAYS
           wins. Both CLI- and host-launched sessions populate it
           with the authoritative starting cwd; any spec cwd is
           treated as a session-create-time boundary, not a
           runtime override.
        2. The agent's absolute ``os_env.cwd``, when present and
           not a relative placeholder. Used for pure local runs
           that bypass the env-var path (e.g. unit tests that
           construct an ``AgentSpec`` directly).
        3. The per-session default workspace tmpdir.

        :param session_id: Session/conversation identifier.
        :param agent_spec: Agent spec for the session.  When provided and its
            ``os_env`` field is ``None``, the session has no filesystem and
            ``None`` is returned.  When ``None`` (dev/standalone mode) the
            default workspace path is returned.
        :returns: Resolved absolute root path string, or ``None`` when the
            session has no filesystem.
        """
        # No-os_env agents have no filesystem regardless of how the
        # runner was launched — keep that signal so the route layer
        # 404s on filesystem endpoints for headless agents.
        if agent_spec is not None:
            spec_os_env = getattr(agent_spec, "os_env", None)
            if spec_os_env is None:
                return None

        # Runner workspace wins when set. Per-session subdirectory
        # isolation is preserved so concurrent sessions
        # don't share a cwd.
        if self._runner_workspace is not None:
            if self._per_session_workspace:
                default_cwd = str(self._runner_workspace / _sanitize_session_id(session_id))
            else:
                default_cwd = str(self._runner_workspace)
            return str(Path(default_cwd).resolve())

        # No runner workspace → fall back to the spec's cwd if it's
        # a real absolute path, otherwise the per-session tmpdir.
        if agent_spec is not None:
            spec_os_env = getattr(agent_spec, "os_env", None)
            cwd = getattr(spec_os_env, "cwd", None) if spec_os_env is not None else None
            if cwd is not None and cwd not in (".", "./"):
                return str(Path(cwd).resolve())

        # Last resort: compute path without os.makedirs — read-only computation.
        default_cwd = _session_workspace(session_id)
        return str(Path(default_cwd).resolve())

    async def launch_required_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        *,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: Any | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Launch a terminal required for the owning session to execute.

        Mental model:
            If this terminal dies, the session is dead.

        Use this when the terminal process is the session runtime, or an
        essential part of that runtime. This is agent-independent: callers
        declare a lifecycle relationship, not a vendor or harness type.
        """
        return await self._launch_terminal_with_lifecycle(
            TerminalLifecycle.REQUIRED,
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def launch_auxiliary_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        *,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: Any | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Launch a terminal resource attached to the owning session.

        Mental model:
            If this terminal dies, only this terminal resource is gone.

        Use this for terminals that provide UI access, logs, debugging, REPLs,
        or optional interaction. This is agent-independent: callers declare a
        lifecycle relationship, not a vendor or harness type.
        """
        return await self._launch_terminal_with_lifecycle(
            TerminalLifecycle.AUXILIARY,
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def _launch_terminal_with_lifecycle(
        self,
        lifecycle: TerminalLifecycle,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: Any,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: Any | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Launch a terminal, then observe it with the requested lifecycle."""
        if self._terminal_registry is None:
            raise RuntimeError("Terminal registry not configured")

        instance = await self._terminal_registry.launch(
            conversation_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            parent_os_env=parent_os_env,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
        )
        return await self._observe_terminal_with_lifecycle(
            lifecycle,
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            instance=instance,
            resource_role=resource_role,
        )

    async def observe_required_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        instance: Any,
        *,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Observe an existing terminal required for session execution.

        Mental model:
            If this terminal dies, the session is dead.
        """
        return await self._observe_terminal_with_lifecycle(
            TerminalLifecycle.REQUIRED,
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            instance=instance,
            resource_role=resource_role,
        )

    async def observe_auxiliary_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        instance: Any,
        *,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Observe an existing terminal attached to the owning session.

        Mental model:
            If this terminal dies, only this terminal resource is gone.
        """
        return await self._observe_terminal_with_lifecycle(
            TerminalLifecycle.AUXILIARY,
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            instance=instance,
            resource_role=resource_role,
        )

    async def _observe_terminal_with_lifecycle(
        self,
        lifecycle: TerminalLifecycle,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        instance: Any,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Project and observe an already-launched terminal instance."""
        if self._terminal_registry is None:
            raise RuntimeError("Terminal registry not configured")
        if not getattr(instance, "running", False) or not await instance.is_alive():
            await self._terminal_registry.close(session_id, terminal_name, session_key)
            raise RuntimeError(
                f"terminal {terminal_name}:{session_key} is not running for session {session_id}"
            )

        from omnigent.terminals.registry import TerminalListEntry

        resource_id = terminal_resource_id(terminal_name, session_key)
        with self._lock:
            previous = self._terminal_lifecycles.get((session_id, resource_id))
            if previous is not None and previous != lifecycle:
                raise RuntimeError(
                    f"terminal {terminal_name}:{session_key} for session {session_id} "
                    f"is already observed as {previous.value}"
                )
            self._terminal_lifecycles[(session_id, resource_id)] = lifecycle
            if resource_role is not None:
                self._terminal_roles[(session_id, resource_id)] = resource_role
        self._start_terminal_activity_watcher(
            session_id,
            terminal_name,
            session_key,
            instance,
            resource_role,
            lifecycle,
            replace=True,
        )
        return terminal_resource_view(
            session_id,
            TerminalListEntry(
                terminal_name=terminal_name,
                session_key=session_key,
                instance=instance,
            ),
        )

    def _start_terminal_activity_watcher(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        instance: Any,
        resource_role: str | None,
        lifecycle: TerminalLifecycle,
        *,
        replace: bool = False,
    ) -> None:
        """Start (idempotently) the per-terminal pane-activity watcher.

        Drives the runner-determined "PTY had output" signal that powers
        the web terminal-activity badge, replacing the removed
        per-terminal client WS attach. The same watcher also reports
        unexpected terminal exit so resource/session lifecycle stays
        aligned with the underlying tmux process. No-op when no publisher
        is installed (e.g. embedded/test runners).

        For the claude-native *agent* terminal (``resource_role`` ==
        :data:`CLAUDE_NATIVE_TERMINAL_ROLE` or
        :data:`PI_NATIVE_TERMINAL_ROLE`) the same watcher also drives the
        session's working status: pane activity → ``running`` and a short
        quiescence → ``idle``, emitted via the session-status publisher.
        This PTY-derived status catches cases lifecycle hooks can miss
        because it observes the terminal directly. The status edges are
        gated to these roles so a side shell's output never flips the
        session's status.

        :param session_id: Session/conversation identifier.
        :param terminal_name: Terminal name from the agent spec.
        :param session_key: Per-launch session key.
        :param instance: The launched :class:`TerminalInstance`.
        :param resource_role: Runner-private role marker for this
            terminal, e.g. :data:`CLAUDE_NATIVE_TERMINAL_ROLE`, or
            ``None`` for a generic terminal (activity badge only).
        :param lifecycle: Required/auxiliary relationship between this
            terminal and the owning session.
        :param replace: Whether to replace an existing watcher so callbacks
            can be rebound after terminal ownership transfer.
        """
        activity_publisher = self._terminal_activity_publisher
        status_publisher = self._session_status_publisher
        exit_publisher = self._terminal_exit_publisher
        # Status edges are derived only from native agent terminals — a
        # generic shell's output must not move the session's working status.
        emit_status = status_publisher is not None and resource_role in {
            CLAUDE_NATIVE_TERMINAL_ROLE,
            PI_NATIVE_TERMINAL_ROLE,
            # cursor-native has no forwarder/hook (run_turn returns immediately
            # after the paste), so — like pi/claude — the PTY watcher is its only
            # status source. Without this the web "Working…" badge never clears.
            CURSOR_NATIVE_TERMINAL_ROLE,
            # goose-native injects then returns (its forwarder only mirrors the
            # transcript, not status), so the PTY watcher is its status source too.
            GOOSE_NATIVE_TERMINAL_ROLE,
            # qwen-native appends then returns (its forwarder only mirrors the
            # JSON event transcript, not status), so the PTY watcher is its
            # status source too.
            QWEN_NATIVE_TERMINAL_ROLE,
            # hermes-native injects then returns (its forwarder only mirrors the
            # SQLite transcript, not status), so the PTY watcher is its status
            # source too.
            HERMES_NATIVE_TERMINAL_ROLE,
        }
        if activity_publisher is None and not emit_status and exit_publisher is None:
            return
        resource_id = terminal_resource_id(terminal_name, session_key)
        loop = asyncio.get_running_loop()

        # Last status edge emitted for this terminal, mutated only on the
        # watcher daemon thread (single-threaded), so the running/idle
        # transition is deduped without a lock. Scoped to this watcher's
        # lifetime, so a fresh terminal gets a fresh baseline — no stale
        # cross-launch state to clean up.
        last_status: dict[str, str | None] = {"value": None}
        # Monotonic time of the last activity pulse published for this
        # terminal, mutated only on the watcher daemon thread (so no lock),
        # used to throttle emissions to at most one per
        # :data:`_TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS`. ``None``
        # means "never emitted", so the first changed tick always fires.
        last_activity_emit: dict[str, float | None] = {"value": None}

        def _on_activity() -> None:
            # Runs on the watcher daemon thread; hop to the loop so the
            # loop-only publishers (queue.put_nowait) are touched safely.
            #
            # Throttle the activity pulse to one per second: the
            # claude-native pane changes on nearly every 200ms poll while
            # Claude works, but the web badge only needs a pulse inside its
            # 1.5s window — emitting on every tick would push ~5 events/sec
            # of redundant traffic.
            if activity_publisher is not None:
                now = _monotonic()
                previous = last_activity_emit["value"]
                if (
                    previous is None
                    or now - previous >= _TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS
                ):
                    last_activity_emit["value"] = now
                    loop.call_soon_threadsafe(activity_publisher, session_id, resource_id)
            # Pane changed → the agent is working. Coalesce to the
            # idle→running edge so a continuously-redrawing pane doesn't
            # re-emit ``running`` every poll.
            if emit_status and status_publisher is not None and last_status["value"] != "running":
                last_status["value"] = "running"
                # Track for exit classification: a pane exit now reads as a crash.
                self._set_session_status_memo(session_id, "running")
                loop.call_soon_threadsafe(status_publisher, session_id, "running")

        def _on_exit() -> None:
            def _schedule() -> None:
                task = asyncio.create_task(
                    self._handle_terminal_exit(
                        session_id=session_id,
                        terminal_name=terminal_name,
                        session_key=session_key,
                        lifecycle=lifecycle,
                        instance=instance,
                    )
                )
                task.add_done_callback(_log_terminal_exit_task_result)

            try:
                loop.call_soon_threadsafe(_schedule)
            except RuntimeError:
                _logger.debug(
                    "Event loop unavailable while handling terminal exit: "
                    "session=%s terminal=%s:%s",
                    session_id,
                    terminal_name,
                    session_key,
                )

        def _log_terminal_exit_task_result(task: asyncio.Task[None]) -> None:
            try:
                task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                _logger.exception(
                    "Terminal exit cleanup failed: session=%s terminal=%s:%s",
                    session_id,
                    terminal_name,
                    session_key,
                )

        if not emit_status:
            instance.start_idle_watcher_thread(
                on_activity=_on_activity if activity_publisher is not None else None,
                on_exit=_on_exit,
                replace=replace,
            )
            return

        def _on_idle() -> None:
            # Pane quiet for the claude-native status threshold → the
            # agent has stopped. Edge-triggered: re-arms only after new
            # output mutates the pane (which flips back to ``running``).
            if status_publisher is not None and last_status["value"] != "idle":
                last_status["value"] = "idle"
                # Track for exit classification: a pane exit now reads as clean.
                # Edge ordering: the watcher thread runs idle/exit serially, so
                # this idle commits before any later on_exit reads the memo.
                self._set_session_status_memo(session_id, "idle")
                loop.call_soon_threadsafe(status_publisher, session_id, "idle")
            # Clear the activity throttle so the next working episode emits
            # its first pulse immediately, keeping the activity badge
            # aligned with the running-status edge (which also re-fires on
            # the next pane change) rather than lagging up to a second
            # behind it.
            last_activity_emit["value"] = None

        instance.start_idle_watcher_thread(
            on_activity=_on_activity,
            on_idle=_on_idle,
            on_exit=_on_exit,
            idle_threshold_s=_CLAUDE_NATIVE_STATUS_IDLE_THRESHOLD_SECONDS,
            poll_interval_s=_CLAUDE_NATIVE_STATUS_POLL_INTERVAL_SECONDS,
            replace=replace,
        )

    async def _handle_terminal_exit(
        self,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        lifecycle: TerminalLifecycle,
        instance: Any | None = None,
    ) -> None:
        """Clean up and publish lifecycle events for an unexpected terminal exit."""
        terminal_id = terminal_resource_id(terminal_name, session_key)
        with self._lock:
            observed = self._terminal_lifecycles.pop((session_id, terminal_id), None)
            self._terminal_roles.pop((session_id, terminal_id), None)
        if observed is None:
            return
        if observed != lifecycle:
            _logger.warning(
                "Terminal lifecycle changed before exit handling: session=%s terminal=%s:%s "
                "observed=%s callback=%s",
                session_id,
                terminal_name,
                session_key,
                observed.value,
                lifecycle.value,
            )
            lifecycle = observed

        command, args_count, cwd, last_output = _terminal_exit_diagnostics(instance)
        # Idle = clean shutdown after the turn finished. Anything else (running,
        # or never observed → boot failure) stays a failure.
        session_was_idle = self._take_session_status_memo(session_id) == "idle"

        if self._terminal_registry is not None:
            try:
                await self._terminal_registry.close(session_id, terminal_name, session_key)
            except Exception:
                _logger.exception(
                    "Error evicting exited terminal: session=%s terminal=%s:%s",
                    session_id,
                    terminal_name,
                    session_key,
                )

        publisher = self._terminal_exit_publisher
        if publisher is not None:
            publisher(
                TerminalExitEvent(
                    session_id=session_id,
                    terminal_id=terminal_id,
                    terminal_name=terminal_name,
                    session_key=session_key,
                    lifecycle=lifecycle,
                    command=command,
                    args_count=args_count,
                    cwd=cwd,
                    last_output=last_output,
                    session_was_idle=session_was_idle,
                )
            )

    async def close_terminal(
        self,
        session_id: str,
        terminal_id: str,
    ) -> bool:
        """Close a terminal resource by id.

        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: ``True`` if a terminal was closed.
        """
        if self._terminal_registry is None:
            return False

        for entry in self._terminal_registry.list_for_conversation(
            session_id,
        ):
            if terminal_resource_id(entry.terminal_name, entry.session_key) == terminal_id:
                closed = await self._terminal_registry.close(
                    session_id,
                    entry.terminal_name,
                    entry.session_key,
                )
                if closed:
                    with self._lock:
                        self._terminal_roles.pop((session_id, terminal_id), None)
                        self._terminal_lifecycles.pop((session_id, terminal_id), None)
                return closed
        return False

    async def transfer_terminal(
        self,
        source_session_id: str,
        target_session_id: str,
        terminal_id: str,
    ) -> SessionResourceView | None:
        """Move a terminal resource between sessions without closing it.

        The underlying tmux pane remains live. Only the registry owner
        key changes, and the returned resource is projected under the
        target session id.

        :param source_session_id: Current owning session id, e.g.
            ``"conv_old"``.
        :param target_session_id: New owning session id, e.g.
            ``"conv_new"``.
        :param terminal_id: Opaque terminal resource id, e.g.
            ``"terminal_claude_main"``.
        :returns: The transferred terminal resource view under
            *target_session_id*, or ``None`` if no matching source
            terminal exists.
        :raises RuntimeError: If the target session already has a
            terminal with the same name and session key.
        """
        if self._terminal_registry is None:
            return None

        from omnigent.terminals.registry import TerminalListEntry

        for entry in self._terminal_registry.list_for_conversation(
            source_session_id,
        ):
            if not entry.instance.running:
                continue
            if terminal_resource_id(entry.terminal_name, entry.session_key) != terminal_id:
                continue
            moved = self._terminal_registry.transfer(
                source_session_id,
                target_session_id,
                entry.terminal_name,
                entry.session_key,
            )
            if not moved:
                return None
            with self._lock:
                role = self._terminal_roles.pop((source_session_id, terminal_id), None)
                if role is not None:
                    self._terminal_roles[(target_session_id, terminal_id)] = role
                lifecycle = self._terminal_lifecycles.pop((source_session_id, terminal_id), None)
                if lifecycle is not None:
                    self._terminal_lifecycles[(target_session_id, terminal_id)] = lifecycle
            # Move the PTY-status memo with the pane so a post-transfer exit is
            # classified against the right session. Don't clobber a status the
            # target already has from its own terminal.
            with self._lock:
                moved_status = self._last_session_status.pop(source_session_id, None)
                if moved_status is not None and target_session_id not in self._last_session_status:
                    self._last_session_status[target_session_id] = moved_status
            try:
                await entry.instance.set_conversation_link(
                    self._terminal_registry.conversation_link_for_id(target_session_id)
                )
            except (RuntimeError, OSError) as exc:
                _logger.warning(
                    "Failed to update terminal status link after transfer to %s: %s",
                    target_session_id,
                    exc,
                )
            if lifecycle is not None:
                self._start_terminal_activity_watcher(
                    target_session_id,
                    entry.terminal_name,
                    entry.session_key,
                    entry.instance,
                    role,
                    lifecycle,
                    replace=True,
                )
            return terminal_resource_view(
                target_session_id,
                TerminalListEntry(
                    terminal_name=entry.terminal_name,
                    session_key=entry.session_key,
                    instance=entry.instance,
                ),
            )
        return None

    async def cleanup_session(self, session_id: str) -> None:
        """Close all resources owned by a session.

        Closes the primary OSEnv and delegates terminal cleanup
        to the terminal registry.  Preserves workspace files for
        post-mortem inspection per the design.

        :param session_id: Session/conversation identifier.
        """
        self._take_session_status_memo(session_id)
        with self._lock:
            primary = self._primary_envs.pop(session_id, None)
            stale_role_keys = [key for key in self._terminal_roles if key[0] == session_id]
            for key in stale_role_keys:
                self._terminal_roles.pop(key, None)
            stale_lifecycle_keys = [
                key for key in self._terminal_lifecycles if key[0] == session_id
            ]
            for key in stale_lifecycle_keys:
                self._terminal_lifecycles.pop(key, None)
        if primary is not None:
            try:
                primary.close()
            except Exception:
                _logger.exception(
                    "Error closing primary env for session=%s",
                    session_id,
                )

        if self._terminal_registry is not None:
            try:
                await self._terminal_registry.cleanup_conversation(
                    session_id,
                )
            except Exception:
                _logger.exception(
                    "Error cleaning up terminals for session=%s",
                    session_id,
                )

    def has_primary_env(self, session_id: str) -> bool:
        """Check if a primary env has been materialized.

        :param session_id: Session/conversation identifier.
        :returns: ``True`` if the primary env is cached.
        """
        with self._lock:
            return session_id in self._primary_envs
