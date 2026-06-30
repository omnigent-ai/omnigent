"""
Per-conversation harness subprocess lifecycle.

Owns the contract from §Process management of
``designs/SERVER_HARNESS_CONTRACT.md``: one subprocess per AP
conversation, lazily spawned on the conversation's first
``get_client`` call, lifecycle coupled to the conversation, idle
reaper for abandoned subprocesses, crash detection, and AP-startup
orphan sweep so a previous Omnigent crash doesn't leave runner processes
behind.

This module knows nothing about the harness API — it spawns a
:mod:`omnigent.runtime.harnesses._runner` subprocess per
conversation and hands callers an ``httpx.AsyncClient`` pointed at
the per-conversation Unix socket. Everything HTTP-shaped is in
:mod:`omnigent.server.schemas`; everything FastAPI-shaped is in
the per-harness ``create_app()`` factories.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent._platform import IS_WINDOWS
from omnigent.inner import _proc
from omnigent.inner._subprocess_lifecycle import close_subprocess_transport
from omnigent.runner.identity import strip_runner_auth_secrets
from omnigent.runtime.harnesses import _HARNESS_MODULES

_logger = logging.getLogger(__name__)

# Per-AP-instance directory holding all per-conversation Unix
# sockets (POSIX) and the AP_PID sentinel file. Each Omnigent instance gets a
# uuid-named subdir so concurrent Omnigent processes (zero-downtime restarts,
# multi-tenant single-machine deployments) don't step on each other.
#
# POSIX pins ``/tmp/omnigent`` deliberately: Unix socket paths have a tight
# length limit, so a short, predictable parent matters (gettempdir() can be a
# long ``/var/folders/...`` path on macOS). Windows uses TCP loopback for the
# harness IPC (no socket-path length concern) and has no ``/tmp`` — a literal
# ``/tmp/omnigent`` there resolves to ``\tmp\omnigent`` on the current drive —
# so use the real temp dir.
if IS_WINDOWS:
    _TMP_PARENT = Path(tempfile.gettempdir()) / "omnigent"
else:
    _TMP_PARENT = Path("/tmp/omnigent")
_TMP_PARENT_ENV_VAR = "OMNIGENT_HARNESS_TMP_PARENT"

# S1 (security): env var carrying the per-spawn bearer token for the harness
# control channel. The parent generates a fresh token per subprocess, ships it
# here (private to the parent/child pair), and presents it on every ``/v1``
# request; the harness scaffold's auth middleware compares against it. This is
# the access boundary on Windows, where the IPC is a loopback TCP listener
# reachable by any local process; on POSIX (uid-isolated UDS) it is defence in
# depth.
_HARNESS_AUTH_TOKEN_ENV = "OMNIGENT_HARNESS_AUTH_TOKEN"

# Sentinel file the Omnigent instance writes into its subdir on boot. The
# orphan sweep uses it to tell whether a sibling subdir belongs to
# a still-running Omnigent (leave alone) or a crashed one (kill its
# children, remove the dir).
_AP_PID_FILE = "AP_PID"

# Mode bits applied to the per-AP subdir and the per-conversation
# socket. Filesystem permissions + the per-AP-uuid scope are the v1
# auth boundary. mTLS / bearer tokens are deferred until federation
# (see §What's deferred in the design doc).
_DIR_MODE = 0o700
# Socket files inherit the parent dir permissions; setting 0o600
# explicitly on the file ensures the same restriction even on
# umask configurations that would otherwise loosen it.
_SOCKET_MODE = 0o600

# Default idle-reaper window: a subprocess that has not been
# touched (via ``get_client`` or an AP→harness HTTP call updating
# ``last_used_at``) for this many seconds is killed and unregistered.
# Per §Deployment knobs vs spec self-containment, this is a
# deployment-level capacity knob — operators may tune; specs MUST
# NOT depend on a specific value.
_DEFAULT_IDLE_TIMEOUT_S = 30 * 60  # 30 minutes

# How often the idle reaper wakes up to check for stale entries.
# Picking 1/30th of the timeout keeps reaping reasonably prompt
# without hammering ``time.monotonic()`` more than necessary.
_DEFAULT_REAPER_INTERVAL_S = 60

# Env override for the harness idle-reap window, so operators can tune it
# without code changes (mirrors the runner-level ``runner.idle_timeout_s``).
# ``0`` disables harness reaping entirely.
_HARNESS_IDLE_TIMEOUT_ENV = "OMNIGENT_HARNESS_IDLE_TIMEOUT_S"


def _resolve_harness_idle_timeout_s() -> float:
    """Resolve the harness idle-reap window in seconds.

    Honors :envvar:`OMNIGENT_HARNESS_IDLE_TIMEOUT_S` (``0`` disables reaping);
    otherwise the 30-minute default. An unparseable or negative value logs a
    warning and falls back to the default rather than failing the runner at
    boot — an env typo shouldn't take the runner down.
    """
    raw = os.environ.get(_HARNESS_IDLE_TIMEOUT_ENV)
    if not raw:
        return float(_DEFAULT_IDLE_TIMEOUT_S)
    try:
        value = float(raw)
    except ValueError:
        _logger.warning(
            "%s=%r is not a number; using default %ss",
            _HARNESS_IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_S,
        )
        return float(_DEFAULT_IDLE_TIMEOUT_S)
    if value < 0:
        _logger.warning(
            "%s=%r is negative; using default %ss",
            _HARNESS_IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_S,
        )
        return float(_DEFAULT_IDLE_TIMEOUT_S)
    return value


# Grace period between SIGTERM and SIGKILL when releasing a
# subprocess. Long enough for a well-behaved harness to flush
# in-flight responses + close the FastAPI app cleanly; short
# enough that a wedged subprocess doesn't block Omnigent shutdown.
_RELEASE_GRACE_S = 5.0

# Timeout for the per-conversation socket file to appear after
# uvicorn boots inside the runner. Cold-start of an external SDK
# (Claude Code, Codex, etc.) plus uvicorn is typically 1–3s; the
# generous cap catches genuinely-stuck spawns without waiting
# forever.
_SPAWN_READY_TIMEOUT_S = 30.0

# Polling interval while waiting for the socket to appear. Short
# enough that fast spawns return quickly; not so short that we
# hammer the filesystem.
_SPAWN_POLL_INTERVAL_S = 0.05

# httpx's default read timeout (5s) is too short for SSE streams
# that pause for tens of seconds during tool dispatch round-trips
# or model-thinking gaps. Omnigent doesn't bound harness-side latency —
# the harness controls its own work bounds (inner SDK request
# timeouts, executor per-turn timeouts) and emits the
# ``response.heartbeat`` SSE events the design defines for live-
# ness (§Heartbeats: ~5s cadence, ~3 missed = dead). The client
# layer is the right place for missed-heartbeat detection; a
# wall-clock httpx read timeout would either fire too early
# (slow legitimate tool call) or too late (the heartbeat path
# already noticed). Subprocess crashes still surface promptly
# because the closed UDS socket ends the stream.


# Grace period after SIGTERM before escalating to SIGKILL when
# cleaning orphaned runners during the boot-time orphan sweep.
# Shorter than ``_RELEASE_GRACE_S`` (which applies to managed
# shutdown) because orphans are by definition unresponsive to
# normal lifecycle — if SIGTERM doesn't land in 3 s, SIGKILL is
# the only recourse.
_ORPHAN_SIGTERM_GRACE_S = 3.0


class NoLiveHarnessError(RuntimeError):
    """Raised when ``get_client`` is called with ``harness="any"`` and no subprocess is live."""


def _default_tmp_parent() -> Path:
    """
    Resolve the deployment-level parent for harness Unix sockets.

    Operators can set :envvar:`OMNIGENT_HARNESS_TMP_PARENT` when
    the host's real ``/tmp`` is unavailable or unsuitable. Relative
    values are intentionally preserved: Unix socket paths have tight
    length limits, and a short path such as ``.tmp/oa`` is useful
    for local worktrees whose absolute path is long.

    :returns: Configured parent path, or the default
        ``/tmp/omnigent``.
    """
    configured = os.environ.get(_TMP_PARENT_ENV_VAR)
    if configured:
        return Path(configured).expanduser()
    return _TMP_PARENT


def _socket_path(instance_dir: Path, conversation_id: str) -> Path:
    """
    Per-conversation socket path under the per-AP-instance dir.

    :param instance_dir: This Omnigent instance's directory, e.g.
        ``/tmp/omnigent/ap-abc123``.
    :param conversation_id: AP-allocated conversation id, e.g.
        ``"conv_xyz789"``.
    :returns: Absolute Unix socket path the runner binds and AP's
        httpx client connects to.
    """
    return instance_dir / f"conv-{conversation_id}.sock"


def _resolve_module_path(harness: str) -> str:
    """
    Look up the harness name in :data:`_HARNESS_MODULES`.

    Resolves harness name → fully-qualified module path here in the
    parent so the registry stays the single source of truth.
    Subprocesses don't inherit runtime mutations of the registry,
    so doing this lookup in the child would force a roundabout
    env-var override every time tests want to register a fixture
    harness.

    :param harness: The harness name to resolve, e.g.
        ``"claude-sdk"``.
    :returns: The fully-qualified module path that exports
        ``create_app() -> FastAPI``.
    :raises RuntimeError: If ``harness`` is not registered. The
        message names the registered harnesses (or notes the
        registry is empty — common during Phase 1 step 2 before
        wraps land in step 4).
    """
    module_path = _HARNESS_MODULES.get(harness)
    if module_path is not None:
        return module_path
    if _HARNESS_MODULES:
        registered = sorted(_HARNESS_MODULES)
        raise RuntimeError(f"unknown harness {harness!r}; registered names: {registered}")
    raise RuntimeError(
        f"unknown harness {harness!r}; the registry is empty (no per-harness "
        f"wraps registered yet — see Phase 1 step 4 of "
        f"designs/SERVER_HARNESS_CONTRACT.md, or register a fixture "
        f"harness from a test by mutating "
        f"omnigent.runtime.harnesses._HARNESS_MODULES)"
    )


async def _wait_for_bind(
    process: asyncio.subprocess.Process,
    endpoint: _HarnessEndpoint,
    harness: str,
    conversation_id: str,
) -> None:
    """
    Poll until the runner subprocess is accepting connections.

    Probes ``connect()`` rather than just existence to close the
    bind/listen gap: uvicorn's ``bind()`` readies the endpoint
    before ``listen()`` wires the accept loop, so a caller racing
    that window hits ``ECONNREFUSED``. Works for both the UDS
    (POSIX) and TCP-loopback (Windows) endpoints.

    :param process: The just-spawned runner subprocess handle.
    :param endpoint: The transport endpoint the runner is expected
        to bind.
    :param harness: Human-readable harness name, used for the
        failure message.
    :param conversation_id: AP-allocated conversation id, used
        for the failure message.
    :raises RuntimeError: If the subprocess exits before binding
        or the deadline elapses (after killing the subprocess).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SPAWN_READY_TIMEOUT_S
    while True:
        if process.returncode is not None:
            # Subprocess inherits stderr so the failure message
            # surfaces on AP's own stderr — operators see the
            # full traceback there, not in this RuntimeError.
            raise RuntimeError(
                f"harness {harness!r} for conversation "
                f"{conversation_id!r} exited with "
                f"{process.returncode} during spawn (see Omnigent stderr)"
            )
        if await endpoint.can_connect():
            # Lock down the socket file's permissions defensively
            # (UDS only); the parent dir is already 0o700 so this is
            # belt-and-suspenders against an unusual umask.
            endpoint.harden()
            return
        if loop.time() >= deadline:
            # Process never bound — kill it and fail loud rather
            # than wait forever.
            process.kill()
            await process.wait()
            raise RuntimeError(
                f"harness {harness!r} for conversation "
                f"{conversation_id!r} did not bind its endpoint "
                f"within {_SPAWN_READY_TIMEOUT_S:.0f}s"
            )
        await asyncio.sleep(_SPAWN_POLL_INTERVAL_S)


async def _can_connect_uds(socket_path: Path) -> bool:
    """
    Probe whether the Unix socket is accepting connections.

    A successful connect proves the server reached ``listen()``,
    not merely ``bind()``. Returns ``False`` on any ``OSError``
    (pre-listen states surface as ``ConnectionRefusedError`` or
    ``FileNotFoundError``).
    """
    try:
        _, writer = await asyncio.open_unix_connection(str(socket_path))
    except OSError:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


def _pick_free_tcp_port() -> int:
    """Bind 127.0.0.1:0 to let the OS choose a free port, then release it.

    The standard allocation trick: the OS guarantees the port is free at the
    moment of bind. A small TOCTOU window exists before the runner re-binds it,
    but loopback collisions are vanishingly rare and ``_wait_for_bind`` fails
    loud if the child cannot bind.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _can_connect_tcp(host: str, port: int) -> bool:
    """Probe whether the loopback TCP listener is accepting connections.

    The Windows analog of :func:`_can_connect_uds`: a successful connect proves
    the runner reached ``listen()``, not merely ``bind()``.
    """
    try:
        _, writer = await asyncio.open_connection(host, port)
    except OSError:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


@dataclass
class _HarnessEndpoint:
    """How the parent reaches a harness subprocess.

    POSIX uses a Unix domain socket (a filesystem path under the per-instance
    dir). Windows has no usable filesystem UDS in asyncio's Proactor loop, so it
    uses a TCP listener on loopback. Exactly one of ``socket_path`` /
    (``host``, ``port``) is set. This object encapsulates the per-transport
    differences — spawn flags, readiness probe, httpx wiring, cleanup — so
    :class:`HarnessProcessManager` stays transport-agnostic.
    """

    socket_path: Path | None = None
    host: str | None = None
    port: int | None = None

    @classmethod
    def create(cls, instance_dir: Path, conversation_id: str) -> _HarnessEndpoint:
        """Allocate the platform-appropriate endpoint for a conversation."""
        if IS_WINDOWS:
            return cls(host="127.0.0.1", port=_pick_free_tcp_port())
        return cls(socket_path=_socket_path(instance_dir, conversation_id))

    @property
    def is_uds(self) -> bool:
        return self.socket_path is not None

    def spawn_args(self) -> list[str]:
        """The ``_runner`` CLI flags selecting this transport."""
        if self.socket_path is not None:
            return ["--socket", str(self.socket_path)]
        return ["--bind", f"{self.host}:{self.port}"]

    def make_transport(self) -> httpx.AsyncBaseTransport:
        """An httpx transport routed at this endpoint."""
        if self.socket_path is not None:
            return httpx.AsyncHTTPTransport(uds=str(self.socket_path))
        return httpx.AsyncHTTPTransport()

    @property
    def base_url(self) -> str:
        """The httpx base URL. Under UDS the host is cosmetic; under TCP it routes."""
        if self.socket_path is not None:
            return "http://harness.local"
        return f"http://{self.host}:{self.port}"

    async def can_connect(self) -> bool:
        """Whether the runner is accepting connections at this endpoint yet."""
        if self.socket_path is not None:
            return self.socket_path.exists() and await _can_connect_uds(self.socket_path)
        assert self.host is not None and self.port is not None
        return await _can_connect_tcp(self.host, self.port)

    def harden(self) -> None:
        """Post-bind hardening. Lock down the UDS file mode; no-op for TCP."""
        if self.socket_path is not None:
            os.chmod(self.socket_path, _SOCKET_MODE)

    def cleanup(self) -> None:
        """Best-effort removal of any on-disk artifact (the UDS file)."""
        if self.socket_path is not None and self.socket_path.exists():
            self.socket_path.unlink()


class _SubprocessEntry:
    """
    Bookkeeping for one harness subprocess.

    :param process: The :class:`asyncio.subprocess.Process` handle.
    :param client: The :class:`httpx.AsyncClient` Omnigent uses to talk
        to this subprocess over its Unix socket.
    :param socket_path: Absolute Unix socket path the runner
        bound, e.g.
        ``Path("/tmp/omnigent/ap-abc/conv-xyz.sock")``.
    :param harness: The harness name this subprocess serves
        (e.g. ``"claude-sdk"``). Recorded so the reaper can
        include it in log lines.
    :param last_used_at: Monotonic timestamp of the most recent
        ``get_client`` call for this conversation. Used by the
        idle reaper to detect abandoned entries.
    :param model: The ``HARNESS_<H>_MODEL`` value this subprocess
        was spawned with (or ``None`` when the spawn env set no
        model). The model is fixed at spawn time (it's a process
        env var), so :meth:`HarnessProcessManager.get_client`
        re-spawns when a later turn requests a different model —
        e.g. after the user runs ``/model``.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        client: httpx.AsyncClient,
        endpoint: _HarnessEndpoint,
        harness: str,
        model: str | None = None,
    ) -> None:
        self.process = process
        self.client = client
        self.endpoint = endpoint
        self.harness = harness
        self.model = model
        self.last_used_at: float = 0.0


def _model_env_key(harness: str) -> str:
    """
    Return the per-harness model env-var key for *harness*.

    Mirrors the ``HARNESS_<H>_MODEL`` convention the spawn-env builders
    use, so the process manager can tell whether a later turn's spawn env
    requests a different model than the running subprocess was started with.

    :param harness: Canonical harness name, e.g. ``"claude-sdk"`` or
        ``"codex"``.
    :returns: The env-var key, e.g. ``"HARNESS_CLAUDE_SDK_MODEL"`` for
        ``"claude-sdk"`` or ``"HARNESS_CODEX_MODEL"`` for ``"codex"``.
    """
    return f"HARNESS_{harness.upper().replace('-', '_')}_MODEL"


def _build_harness_spawn_env(env: dict[str, str] | None) -> dict[str, str]:
    """
    Build the environment for a spawned harness subprocess.

    Inherits the runner's ``os.environ`` (PATH / HOME / PYTHONPATH /
    provider creds), layers the caller's per-spawn overrides on top, then
    strips the runner-auth secrets: the harness runs the agent's
    (potentially untrusted) payload and must never see the tunnel binding
    token. Always returns an explicit dict — ``env=None`` to
    ``create_subprocess_exec`` would inherit the full env and re-leak the
    secret on the no-overrides path.

    :param env: Per-spawn overrides merged over ``os.environ`` (caller
        keys win), e.g. ``{"HARNESS_CLAUDE_SDK_MODEL": "claude-opus-4-6"}``.
        ``None`` means no overrides.
    :returns: The harness subprocess environment, runner-auth secrets removed.
    """
    merged = {**os.environ, **env} if env else dict(os.environ)
    return strip_runner_auth_secrets(merged)


class HarnessProcessManager:
    """
    One subprocess per conversation; lifecycle tied to conversation.

    Use ``start()`` once at Omnigent boot to create the per-instance
    directory and run the orphan sweep, then call ``get_client``
    per conversation to lazily spawn / look up its subprocess.
    Call ``release(conv_id)`` when the conversation reaches a
    terminal state, and ``shutdown()`` at Omnigent shutdown to release
    every remaining subprocess.

    See ``designs/SERVER_HARNESS_CONTRACT.md`` §Process management
    for the full contract.

    :param idle_timeout_s: Seconds of inactivity after which a
        subprocess gets reaped. Deployment-level capacity knob;
        defaults to 30 minutes. Specs MUST NOT depend on a
        specific value.
    :param reaper_interval_s: Seconds between idle-reaper passes.
        Defaults to 60.
    :param tmp_parent: Override for the parent ``/tmp/omnigent``
        directory; tests pass a temp path so concurrent test runs
        and the host's real ``/tmp`` don't interfere. Production
        callers normally leave this unset; set
        :envvar:`OMNIGENT_HARNESS_TMP_PARENT` to choose a
        deployment-specific parent.
    """

    def __init__(
        self,
        *,
        idle_timeout_s: float | None = None,
        reaper_interval_s: float = _DEFAULT_REAPER_INTERVAL_S,
        tmp_parent: Path | None = None,
    ) -> None:
        # ``None`` (the default at both construction sites) resolves from the
        # OMNIGENT_HARNESS_IDLE_TIMEOUT_S env var, else the 30-minute default.
        self._idle_timeout_s = (
            idle_timeout_s if idle_timeout_s is not None else _resolve_harness_idle_timeout_s()
        )
        self._reaper_interval_s = reaper_interval_s
        self._tmp_parent = tmp_parent if tmp_parent is not None else _default_tmp_parent()
        # Pre-allocate the instance dir path so it stays stable
        # across re-entrant ``start()`` calls (idempotent boot).
        self._instance_dir = self._tmp_parent / f"ap-{uuid.uuid4().hex}"
        self._entries: dict[str, _SubprocessEntry] = {}
        # Per-conversation in-flight harness response_id. The runner's
        # ``proxy_stream`` populates it via :meth:`mark_in_flight` when
        # the harness emits ``response.created`` and clears it via
        # :meth:`clear_in_flight` at stream end (``_on_proxy_stream_end``,
        # reached on every terminal path). Two readers depend on it:
        # :meth:`forward_cancel` translates an AP-side cancel into the
        # harness's own response_id (AP's ``task_id`` and the harness's
        # ``resp_<uuid>`` are different identifiers; the harness scaffold's
        # ``/cancel`` route keys ``_in_flight`` by the harness id only),
        # and the idle reaper skips any conversation present here so an
        # actively-streaming turn is never reaped mid-flight.
        self._in_flight_response_ids: dict[str, str] = {}
        # Per-conversation spawn lock — see §Process management:
        # Spawn lock. The lock guards the lazy-init window in
        # ``get_client``; uncontested after the first spawn for a
        # given conv_id.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        # Top-level lock for ``_entries`` / ``_spawn_locks`` dict
        # mutations themselves (the entries within are guarded by
        # their per-conv locks).
        self._registry_lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def instance_dir(self) -> Path:
        """
        This Omnigent instance's per-instance directory.

        :returns: Path like ``/tmp/omnigent/ap-<uuid>``.
        """
        return self._instance_dir

    def socket_path(self, conversation_id: str) -> Path:
        """
        Per-conversation Unix socket path.

        Useful for callers that need to construct a separate
        ``httpx.AsyncClient`` against the same socket — e.g.,
        tests that issue PATCH / cancel concurrently with an
        open streaming response from the manager-owned client
        (sharing one client across both ends can block the
        second request on the first's keepalive connection).

        :param conversation_id: AP-allocated conversation id.
        :returns: Absolute Unix socket path the runner bound /
            would bind for this conversation.
        """
        return _socket_path(self._instance_dir, conversation_id)

    async def start(self) -> None:
        """
        Initialize the per-instance dir, run the orphan sweep, and
        start the idle-reaper background task.

        Safe to call more than once; the second call is a no-op.
        Idempotent so AP's lifespan handler doesn't have to track
        whether boot already ran.
        """
        if self._started:
            return
        self._tmp_parent.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        # Sweep BEFORE creating our own dir, so a crashed prior
        # instance whose dir uuid happens to collide with ours
        # (vanishingly unlikely but possible) gets cleaned first.
        await self._sweep_orphans()
        self._instance_dir.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        # Write the AP_PID sentinel so other instances' sweeps can
        # tell our dir is live. Strict ``"x"`` because the dir is
        # exclusively ours; a pre-existing sentinel would mean the
        # uuid collided with a still-running instance — fail loud.
        sentinel = self._instance_dir / _AP_PID_FILE
        sentinel.write_text(str(os.getpid()), encoding="utf-8")
        self._reaper_task = asyncio.create_task(
            self._idle_reaper_loop(),
            name="harness-process-manager-idle-reaper",
        )
        self._started = True
        _logger.info(
            "HarnessProcessManager started; instance_dir=%s",
            self._instance_dir,
        )

    async def get_client(
        self,
        conversation_id: str,
        harness: str,
        env: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        """
        Return the per-conversation httpx client, spawning lazily.

        The first call for a given ``conversation_id`` spawns a
        runner subprocess of the right harness type, waits for the
        Unix socket to appear, and constructs an
        :class:`httpx.AsyncClient` over it. Subsequent calls
        return the cached client (``env`` is ignored on cache
        hits — config is fixed at first-spawn time).

        Crash detection: if the previously-spawned subprocess has
        exited (``returncode is not None``), the entry is dropped
        and a fresh subprocess is spawned with the ``env``
        provided on the call that triggered the respawn.
        Per-conversation lock ensures concurrent callers during
        the lazy-init window don't race two subprocesses onto the
        same socket — see §Process management: Spawn lock for the
        rationale.

        :param conversation_id: AP-allocated conversation id, e.g.
            ``"conv_abc123"``.
        :param harness: Registry key in
            :data:`omnigent.runtime.harnesses._HARNESS_MODULES`,
            e.g. ``"claude-sdk"``.
        :param env: Per-spawn environment variable overrides
            merged on top of ``os.environ`` for the subprocess,
            e.g. ``{"HARNESS_CLAUDE_SDK_GATEWAY": "true",
            "HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE": "<your-profile>"}``.
            Caller-supplied keys win on conflicts. ``None`` inherits
            ``os.environ`` minus the runner-auth secrets stripped by
            ``_build_harness_spawn_env``. Used by AP's
            workflow dispatch to thread per-spec executor config
            into the subprocess without polluting AP's own
            ``os.environ`` (which would race across concurrent
            conversations with different specs).
        :returns: The cached or freshly-constructed
            :class:`httpx.AsyncClient` bound to the
            per-conversation Unix socket.
        :raises RuntimeError: If ``start()`` was not called first
            (process manager not initialized) or the spawn fails
            to produce a usable socket within the readiness
            timeout.
        """
        if not self._started:
            raise RuntimeError("HarnessProcessManager.get_client called before start()")
        spawn_lock = await self._get_spawn_lock(conversation_id)
        async with spawn_lock:
            entry = self._entries.get(conversation_id)
            if entry is not None and entry.process.returncode is not None:
                # Prior subprocess died; drop the stale entry and
                # respawn below. The dead client is closed on
                # next ``release`` / ``shutdown`` — leaving it in
                # the dict here would race a fresh spawn.
                _logger.warning(
                    "harness %s for conversation %s exited with %s; respawning",
                    entry.harness,
                    conversation_id,
                    entry.process.returncode,
                )
                await self._close_entry(entry)
                entry = None
            if entry is not None and harness != "any" and entry.harness != harness:
                # The harness is fixed at spawn time (it selects which runner
                # module the subprocess loads), but the socket is keyed by
                # conversation only — so after an in-place agent switch
                # (``POST /v1/sessions/{id}/switch-agent``) a later turn
                # resolves a DIFFERENT harness and must respawn, otherwise the
                # cached subprocess keeps serving the old harness. Mirrors the
                # model-change respawn below.
                #
                # ``"any"`` is the harness-AGNOSTIC sentinel that steering /
                # cancel / interrupt callers pass to reuse the live subprocess
                # (it is not a real harness — see ``get_client(conv, "any")``
                # call sites). It must NOT count as a mismatch, or every such
                # call would tear down and respawn the running harness
                # mid-turn (killing in-flight openai-agents/claude turns).
                _logger.info(
                    "harness for conversation %s changed %r -> %r; respawning",
                    conversation_id,
                    entry.harness,
                    harness,
                )
                await self._close_entry(entry)
                entry = None
            if entry is not None:
                # The model is baked into the subprocess env at spawn time;
                # a later turn requesting a different model (e.g. after the
                # user runs ``/model``) must respawn, otherwise the cached
                # process keeps serving the old model. Only respawn when a
                # concrete different model is requested — a turn that sets no
                # model env (``None``) keeps the running process.
                requested_model = (env or {}).get(_model_env_key(harness))
                if requested_model is not None and requested_model != entry.model:
                    _logger.info(
                        "harness %s for conversation %s: model changed %r -> %r; respawning",
                        harness,
                        conversation_id,
                        entry.model,
                        requested_model,
                    )
                    await self._close_entry(entry)
                    entry = None
            if entry is None:
                if harness == "any":
                    raise NoLiveHarnessError(
                        f"no live harness subprocess for conversation {conversation_id!r}"
                    )
                entry = await self._spawn_entry(conversation_id, harness, env)
                self._entries[conversation_id] = entry
            # Use ``time.monotonic()`` directly rather than
            # ``asyncio.get_running_loop().time()`` so the value is
            # comparable across event loops. ``get_client`` may be
            # called from a plain asyncio loop (CPython's default
            # ``loop.time()`` returns ``mach_absolute_time`` on
            # macOS — excludes sleep), but the reaper runs on
            # uvicorn's uvloop event loop (uvloop's ``loop.time()``
            # uses libuv's clock — INCLUDES sleep on macOS). Two
            # different clock domains. Comparing them after a
            # system sleep gave bogus 9-hour diffs that triggered
            # the 30-min idle cutoff and reaped active streams.
            # ``time.monotonic()`` is a single process-wide source
            # both code paths agree on.
            entry.last_used_at = time.monotonic()
            return entry.client

    async def forward_cancel(
        self,
        conversation_id: str,
        timeout_s: float = 5.0,
    ) -> bool:
        """
        Forward an ``interrupt`` event to the harness subprocess
        for *conversation_id*.

        Sends ``{"type": "interrupt"}`` to
        ``POST /v1/sessions/{conversation_id}/events`` per
        ``designs/session_rearchitecture.md`` §3 / §7
        "Flow: interrupt".

        Used by the cancel route to actively interrupt a streaming
        harness turn. Without this direct forward, the harness keeps
        streaming until the LLM call finishes naturally — wasted
        compute and a flood of post-cancel deltas leaking into the
        REPL.

        The harness-side ``response_id`` is looked up from
        ``_in_flight_response_ids``, which the runner's ``proxy_stream``
        populates on ``response.created`` (via :meth:`mark_in_flight`)
        and clears at stream end (via :meth:`clear_in_flight`). If no
        turn is currently live the mapping has no entry and this method
        is a silent no-op.

        Best-effort: a missing harness (no entry registered for
        this conversation) OR a missing in-flight mapping (no turn
        currently running) is a silent no-op (returns ``False``);
        network / HTTP failures are logged and swallowed (return
        ``False``). The 5 s ``timeout_s`` matches the deadline
        ``_post_cancel_to_harness`` uses on the AP-side workflow
        teardown path, so a wedged harness can't block the cancel
        route's response.

        :param conversation_id: The Omnigent conversation id whose
            harness subprocess should receive the cancel.
        :param timeout_s: Max seconds to wait for the harness's
            204. Larger than the typical RTT but small enough to
            not block the cancel route on a wedged peer.
        :returns: ``True`` when the harness acknowledged with 2xx;
            ``False`` if no entry / no in-flight turn / on
            transport error / on harness-side error.
        """
        async with self._registry_lock:
            harness_response_id = self._in_flight_response_ids.get(conversation_id)
            entry = self._entries.get(conversation_id)
        if harness_response_id is None:
            # No live turn (already terminal, or default-LLM agent
            # with no harness). Silent no-op.
            return False
        if entry is None:
            # Defensive: release can prune the entry after an
            # in-flight id is registered but before cancel forwarding.
            return False
        try:
            cancel_url = f"/v1/sessions/{conversation_id}/events"
            resp = await asyncio.wait_for(
                entry.client.post(cancel_url, json={"type": "interrupt"}),
                timeout=timeout_s,
            )
        except (httpx.HTTPError, asyncio.TimeoutError):
            _logger.exception(
                "harness cancel forward failed: conversation_id=%s harness_response_id=%s",
                conversation_id,
                harness_response_id,
            )
            return False
        if resp.status_code >= 400:
            _logger.warning(
                "harness cancel returned %d for conversation_id=%s harness_response_id=%s",
                resp.status_code,
                conversation_id,
                harness_response_id,
            )
            return False
        return True

    def has_session(self, conversation_id: str) -> bool:
        """
        Check whether a harness subprocess is registered for
        the given conversation.

        :param conversation_id: AP-allocated conversation id,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if a subprocess entry exists.
        """
        return conversation_id in self._entries

    def has_active_turn(self, conversation_id: str) -> bool:
        """
        Check whether the given conversation has an in-flight
        harness response (i.e. a turn is currently streaming).

        :param conversation_id: AP-allocated conversation id,
            e.g. ``"conv_abc123"``.
        :returns: ``True`` if an in-flight response id is
            registered.
        """
        return conversation_id in self._in_flight_response_ids

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """
        Record that *conversation_id* has a live harness turn.

        Called by the runner's ``proxy_stream`` on ``response.created``.
        Registering the response id keeps the idle reaper from killing
        an actively-streaming turn (the reaper skips any conversation
        present in ``_in_flight_response_ids``) and lets
        :meth:`forward_cancel` translate an AP-side cancel into the
        harness's own response id. Always paired with a later
        :meth:`clear_in_flight`.

        :param conversation_id: AP-allocated conversation id,
            e.g. ``"conv_abc123"``.
        :param response_id: The harness-side ``resp_<uuid>`` from the
            ``response.created`` event.
        """
        self._in_flight_response_ids[conversation_id] = response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """
        Clear the live-turn marker for *conversation_id*.

        Called by the runner from ``_on_proxy_stream_end``, which is
        reached on every terminal path (success, error, status-fail,
        interrupt) — so a dropped or late terminal SSE event cannot
        leave an entry permanently marked in-flight and therefore never
        reaped. No-op if no marker is set.

        :param conversation_id: AP-allocated conversation id,
            e.g. ``"conv_abc123"``.
        """
        self._in_flight_response_ids.pop(conversation_id, None)

    async def release(self, conversation_id: str) -> None:
        """
        Terminate and unregister the subprocess for a conversation.

        Called when the conversation reaches a terminal state. No-op
        if no subprocess is registered for the id.

        Note: ``_spawn_locks[conversation_id]`` is intentionally NOT
        removed here. If we removed it, a concurrent caller already
        holding a reference to the lock could be racing a fresh
        ``_get_spawn_lock`` that would create a new lock object —
        the per-conv serialization invariant requires the SAME lock
        instance for all callers of a given conv_id over the AP
        instance's lifetime. The per-lock memory cost is tiny
        (one ``asyncio.Lock``), bounded by the count of unique
        conversation ids the Omnigent instance has ever spawned for. If
        that bound becomes a real problem, switch to a TTL-based
        lock cache.

        :param conversation_id: AP-allocated conversation id.
        """
        async with self._registry_lock:
            entry = self._entries.pop(conversation_id, None)
            # NOTE: ``_spawn_locks[conversation_id]`` intentionally
            # NOT popped — see this method's docstring for the
            # per-conv lock-identity invariant rationale.
        if entry is None:
            return
        await self._close_entry(entry)

    async def shutdown(self) -> None:
        """
        Stop the reaper, release every remaining subprocess, and
        remove the per-instance directory.

        Called from AP's lifespan teardown. After this returns the
        manager is reset to its pre-``start`` state; a subsequent
        ``start()`` would re-initialize it (uncommon — typically
        Omnigent exits after shutdown).
        """
        if not self._started:
            return
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        # Snapshot then iterate — release mutates ``_entries``.
        for conv_id in list(self._entries):
            await self.release(conv_id)
        # Best-effort cleanup of our instance dir. If a subprocess
        # we couldn't kill is still holding a socket file, the
        # rmtree leaves it behind; the next Omnigent boot's orphan
        # sweep handles it.
        shutil.rmtree(self._instance_dir, ignore_errors=True)
        self._started = False

    async def _get_spawn_lock(self, conversation_id: str) -> asyncio.Lock:
        """
        Return (creating if needed) the per-conversation spawn lock.

        :param conversation_id: AP-allocated conversation id.
        :returns: The :class:`asyncio.Lock` keyed on this id.
        """
        async with self._registry_lock:
            lock = self._spawn_locks.get(conversation_id)
            if lock is None:
                lock = asyncio.Lock()
                self._spawn_locks[conversation_id] = lock
            return lock

    async def _spawn_entry(
        self,
        conversation_id: str,
        harness: str,
        env: dict[str, str] | None,
    ) -> _SubprocessEntry:
        """
        Launch the runner subprocess and build its client.

        Caller MUST hold the per-conversation spawn lock.

        :param conversation_id: AP-allocated conversation id.
        :param harness: Registry key for the harness to spawn.
        :param env: Per-spawn env-var overrides merged on top of
            ``os.environ`` (caller-supplied keys win on
            conflicts). ``None`` means no overrides — the subprocess
            inherits ``os.environ`` minus the runner-auth secrets
            stripped by ``_build_harness_spawn_env``.
            See :meth:`get_client` for the rationale (per-spec config
            without polluting AP's own ``os.environ``).
        :returns: A populated :class:`_SubprocessEntry`.
        :raises RuntimeError: If the subprocess exits during
            spawn or fails to bind its socket within
            ``_SPAWN_READY_TIMEOUT_S``.
        """
        module_path = _resolve_module_path(harness)
        endpoint = _HarnessEndpoint.create(self._instance_dir, conversation_id)
        # Defensive: a stale socket file from a previous spawn
        # (released but not cleaned up because of an OS quirk)
        # would block uvicorn binding. Best-effort delete (UDS only).
        endpoint.cleanup()

        # Always build an explicit env dict (never ``None``): the
        # subprocess inherits AP's env (PATH, HOME, PYTHONPATH, provider
        # creds) plus any caller overrides, but the runner's
        # control-plane auth secret is stripped first so the agent
        # payload running in the harness can't impersonate the runner.
        # See ``_build_harness_spawn_env``.
        effective_env: dict[str, str] = _build_harness_spawn_env(env)
        # S1 (security): on Windows the harness IPC is a loopback-TCP listener
        # reachable by any local process, so mint a fresh per-spawn bearer token
        # as the access boundary the uid-isolated UDS provides on POSIX. This is
        # Windows-only: POSIX keeps the UDS and sets no token, leaving the
        # scaffold's auth gate inert. The token is delivered via the harness's
        # private env and presented by our client (below) on every /v1 request.
        auth_token: str | None = None
        if IS_WINDOWS:
            auth_token = secrets.token_urlsafe(32)
            effective_env[_HARNESS_AUTH_TOKEN_ENV] = auth_token

        parent_pid = os.getpid()

        # Subprocess inherits AP's stdout/stderr per §Process
        # management — operators see harness output interleaved
        # with Omnigent logs. The runner doesn't currently prefix lines
        # with ``[<harness> <conv>] `` itself; that's tracked as
        # a follow-up (the design calls for it but the simplest
        # correct behavior — inheritance — is fine for v1).
        #
        # ``--parent-pid`` enables the runner's parent-death
        # watchdog thread so orphaned runners self-terminate
        # when the spawning process exits.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "omnigent.runtime.harnesses._runner",
            "--harness",
            harness,
            "--module",
            module_path,
            *endpoint.spawn_args(),
            "--conversation-id",
            conversation_id,
            "--parent-pid",
            str(parent_pid),
            stdout=None,
            stderr=None,
            env=effective_env,
        )
        await _wait_for_bind(process, endpoint, harness, conversation_id)

        # ``base_url`` is required for relative-URL routing; the
        # actual host portion is irrelevant under uds transport,
        # but httpx insists on a syntactically-valid URL. The
        # default httpx read-timeout (5s) is too short for SSE
        # streams that may pause for tens of seconds during
        # tool dispatch round-trips (action_required → AP
        # call_tool → PATCH → resume); use a generous fixed
        # timeout that still surfaces a genuinely-stuck harness.
        client = httpx.AsyncClient(
            transport=endpoint.make_transport(),
            base_url=endpoint.base_url,
            # S1 (security): present the per-spawn bearer token (Windows only)
            # so the harness scaffold accepts this client and rejects any
            # unauthenticated local peer on the loopback-TCP channel. Empty on
            # POSIX, where the uid-isolated UDS is the access boundary.
            headers=({"Authorization": f"Bearer {auth_token}"} if auth_token else {}),
            # See the comment above the constant for rationale.
            # Connect/write/pool keep the 5s default so a vanished
            # harness still surfaces quickly; read=None defers
            # liveness to the heartbeat path.
            timeout=httpx.Timeout(5.0, read=None),
        )
        return _SubprocessEntry(
            process=process,
            client=client,
            endpoint=endpoint,
            harness=harness,
            # Record the model this subprocess was spawned with so a later
            # turn requesting a different model (e.g. after ``/model``)
            # triggers a respawn in ``get_client`` — the model is a fixed
            # process env var, not re-read per turn.
            model=(env or {}).get(_model_env_key(harness)),
        )

    async def _close_entry(self, entry: _SubprocessEntry) -> None:
        """
        Close the httpx client, terminate the subprocess, and
        remove its socket file.

        :param entry: The bookkeeping record to tear down.
        """
        await entry.client.aclose()
        if entry.process.returncode is None:
            try:
                entry.process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(entry.process.wait(), timeout=_RELEASE_GRACE_S)
            except asyncio.TimeoutError:
                # Wedged subprocess — kill outright. The harness
                # author is responsible for clean SIGTERM handling
                # if they want graceful shutdown of in-flight
                # responses.
                entry.process.kill()
                await entry.process.wait()
        close_subprocess_transport(entry.process)
        # Best-effort socket cleanup. uvicorn's atexit usually
        # handles this when SIGTERM lands cleanly, but a
        # hard-killed runner won't. No-op for TCP endpoints.
        entry.endpoint.cleanup()

    async def _idle_reaper_loop(self) -> None:
        """
        Background task: periodically reap idle subprocesses.

        Iterates the registry, releasing any entry whose
        ``last_used_at`` is older than ``idle_timeout_s`` AND
        has no in-flight response on the harness. Sleeps for
        ``reaper_interval_s`` between passes. Terminates on
        :class:`asyncio.CancelledError` from :meth:`shutdown`. A
        non-positive ``idle_timeout_s`` (e.g.
        ``OMNIGENT_HARNESS_IDLE_TIMEOUT_S=0``) disables reaping — each
        pass is a no-op.

        Two safety guards beyond raw ``last_used_at`` checks:

        1. **In-flight skip** — entries with an active
           harness ``response_id`` (set when the harness
           emits ``response.created``, cleared on the
           terminal event) are never reaped. ``last_used_at``
           is updated only by :meth:`get_client`, which is
           called ONCE per turn at the start. During a
           long-running streaming response (e.g. a 20-shell
           parallel-tool turn) the entry's ``last_used_at``
           stays frozen at the start-of-turn timestamp; if
           wall-clock minus that timestamp ever exceeds
           ``idle_timeout_s`` the reaper would close the
           per-conversation httpx client mid-stream and rip
           the underlying anyio UDS stream out from under
           the active ``aiter_text()``, surfacing as
           ``ReadError(ClosedResourceError())`` on the
           harness's ``response.completed`` await.

        2. **``time.monotonic()`` for the cutoff** rather
           than ``asyncio.get_running_loop().time()`` — see
           the comment on :meth:`get_client`'s
           ``last_used_at`` write. Different event loops use
           different clock sources; ``time.monotonic()`` is
           the single process-wide source both ends of the
           comparison agree on.
        """
        while True:
            try:
                await asyncio.sleep(self._reaper_interval_s)
            except asyncio.CancelledError:
                return
            # ``idle_timeout_s <= 0`` disables harness reaping entirely
            # (``OMNIGENT_HARNESS_IDLE_TIMEOUT_S=0``). Without this guard a 0
            # window makes ``cutoff == now``, so every entry (``last_used_at``
            # always <= now) is reaped on each pass — the inverse of "disabled".
            if self._idle_timeout_s <= 0:
                continue
            now = time.monotonic()
            cutoff = now - self._idle_timeout_s
            stale: list[str] = []
            # Snapshot under the lock; ``release`` runs outside so I/O can't block writers.
            async with self._registry_lock:
                for conv_id, entry in self._entries.items():
                    if entry.last_used_at > cutoff:
                        continue
                    if conv_id in self._in_flight_response_ids:
                        continue
                    stale.append(conv_id)
            for conv_id in stale:
                _logger.info(
                    "reaping idle harness subprocess for conversation %s",
                    conv_id,
                )
                await self.release(conv_id)

    async def _sweep_orphans(self) -> None:
        """
        Kill runner processes left behind by crashed prior AP
        instances and remove their per-instance directories.

        Iterates every ``ap-*`` subdir under ``_tmp_parent``. For
        each, reads the ``AP_PID`` sentinel; if the recorded PID
        is not a live process, the dir belongs to a crashed AP
        and gets cleaned. Sibling dirs whose PIDs are still live
        are left alone (zero-downtime restart, multi-tenant
        same-host case).

        Best-effort throughout — a permission error or unreadable
        sentinel logs and skips the dir rather than aborting boot.
        """
        if not self._tmp_parent.exists():
            return
        for child in self._tmp_parent.iterdir():
            if not child.is_dir() or not child.name.startswith("ap-"):
                continue
            sentinel = child / _AP_PID_FILE
            if not sentinel.exists():
                # No sentinel — directory either pre-dates the
                # convention or is mid-creation. Leave alone.
                continue
            try:
                pid_str = sentinel.read_text(encoding="utf-8").strip()
                pid = int(pid_str)
            except (OSError, ValueError) as exc:
                _logger.warning(
                    "could not read AP_PID sentinel at %s: %s; skipping",
                    sentinel,
                    exc,
                )
                continue
            if _pid_alive(pid):
                # Sibling Omnigent is still running — leave it alone.
                continue
            _logger.info(
                "sweeping orphaned Omnigent instance dir %s (pid %d not running)",
                child,
                pid,
            )
            await self._kill_orphan_runners(child)
            shutil.rmtree(child, ignore_errors=True)

    async def _kill_orphan_runners(self, instance_dir: Path) -> None:
        """
        Send SIGTERM to runner processes whose socket lives under
        ``instance_dir``, then escalate to SIGKILL for survivors.

        Identification works by listing the socket files in the
        dir — every active runner binds one. We don't have the
        runner PIDs because they're orphans of a crashed AP, so
        we shell out to ``lsof`` to find which PIDs hold each
        socket. ``lsof`` failures fall through silently (best
        effort).

        After SIGTERM, waits :data:`_ORPHAN_SIGTERM_GRACE_S`
        seconds, then sends SIGKILL to any runner that is still
        alive. Prior to this escalation, orphaned
        runners with stuck SIGTERM handlers survived the sweep
        indefinitely.

        :param instance_dir: The orphaned AP's per-instance dir
            whose runner subprocesses to terminate.
        """
        all_pids: set[int] = set()
        for socket_file in instance_dir.glob("conv-*.sock"):
            pids = await _pids_holding_socket(socket_file)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    all_pids.add(pid)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    _logger.warning(
                        "cannot signal orphan runner pid %d (permission denied)",
                        pid,
                    )
                    continue

        if not all_pids:
            return

        await asyncio.sleep(_ORPHAN_SIGTERM_GRACE_S)

        for pid in all_pids:
            if not _pid_alive(pid):
                continue
            _logger.warning(
                "orphan runner pid %d survived SIGTERM; escalating to SIGKILL",
                pid,
            )
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def _pid_alive(pid: int) -> bool:
    """
    Return True if ``pid`` is still in the process table.

    On POSIX this is ``os.kill(pid, 0)``: a process that was killed but not yet
    reaped is still a zombie in the table and counts as alive here. That matters
    — callers (the orphan sweep, and tests that SIGKILL a harness then wait on
    this before expecting a respawn) use ``not _pid_alive(pid)`` as a proxy for
    "fully reaped", which is the moment the asyncio child watcher sets the
    subprocess ``returncode`` and ``get_client`` respawns. A psutil probe that
    treats a zombie as already-dead would break that synchronization (the wait
    returns while ``returncode`` is still ``None``).

    ``os.kill(pid, 0)`` cannot be used on Windows — it maps to
    ``TerminateProcess`` and would *kill* the target — so there we fall back to
    the psutil probe. (Windows has no zombies, so the distinction is moot.)

    :param pid: OS process id to check.
    :returns: True if the process exists, False otherwise.
    """
    if IS_WINDOWS:
        return _proc.process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — for the orphan sweep that means
        # "live, leave alone".
        return True
    return True


async def _pids_holding_socket(socket_path: Path) -> list[int]:
    """
    Return the OS PIDs that have ``socket_path`` open.

    Used by the orphan sweep to find the runner subprocess
    holding an abandoned socket. Shells out to ``lsof`` for
    portability across Linux + macOS without a third-party dep
    (``psutil`` would also work but adds an install).

    Returns an empty list on any subprocess error so the caller
    can keep going — orphan cleanup is best-effort.

    :param socket_path: The socket file to look up holders for.
    :returns: List of holding PIDs (often a single one — the
        bound runner).
    """
    proc = await asyncio.create_subprocess_exec(
        "lsof",
        "-t",
        str(socket_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    pids: list[int] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids
