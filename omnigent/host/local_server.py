"""Persistent background local Omnigent server lifecycle.

When ``run`` / ``claude`` / ``codex`` are invoked without a
``--server`` URL, the work happens against a server that lives on *this*
machine. Rather than a command-scoped server torn down on exit, that
server is detached, shared, and reused across invocations — tracked by a
pidfile like the connect daemon — so the Web UI stays reachable after the
command exits and a second invocation reuses the same server + state.

These helpers live under ``omnigent/host/`` (not ``cli.py``) so the
connect daemon can import and own the server's lifecycle: the daemon is
the only process that starts the local server, and the CLI discovers its
URL via the pidfile this module writes. The module deliberately avoids
importing ``cli.py`` to keep that dependency direction clean.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import click
import psutil

from omnigent.inner import _proc

_LOCAL_SERVER_READY_TIMEOUT_SECONDS = 45.0

# Max seconds to wait for a bind-race-doomed server child's natural
# EADDRINUSE exit before the terminate backstop. Matches the readiness
# budget, which a full child boot (cold imports + DB migrations + bind)
# is known to fit inside.
_DOOMED_CHILD_EXIT_GRACE_S = _LOCAL_SERVER_READY_TIMEOUT_SECONDS


def _local_data_dir() -> Path:
    """Return the local runtime data dir (db, artifacts, logs, pidfile).

    Honors ``OMNIGENT_DATA_DIR`` (the purpose-built data-isolation knob),
    else ``~/.omnigent``. This lets a checkout/worktree isolate its local
    runtime DB: two worktrees otherwise share ``~/.omnigent/chat.db``, and
    if their Alembic heads have diverged the shared DB can't migrate and the
    daemon-backed local server fails to boot ("schema is out of date").

    Must stay in lock-step with :func:`omnigent.chat._omnigent_persistent_dir`:
    the local server's DB lives here and ``omnigent run`` resolves the
    resume DB there, so the two MUST agree. ``OMNIGENT_CONFIG_HOME`` is
    deliberately NOT consulted — it isolates *config* (``config.yaml``) only;
    overloading it to move the DB breaks HOME-based data isolation (e.g. the
    resumption e2e tests, which set ``HOME`` to control the DB while
    inheriting a shared ``OMNIGENT_CONFIG_HOME``).

    :returns: The data directory path (callers create it lazily).
    """
    value = os.environ.get("OMNIGENT_DATA_DIR")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".omnigent"


# Pidfile carrying the background local server's PID + port (two lines).
# Read back by the CLI to discover the daemon-started server's URL.
_LOCAL_SERVER_PID_PATH = _local_data_dir() / "local_server.pid"

# Sidecar carrying the config signature (resolved auth source) the
# running local server was spawned under. Reuse is gated on this so a
# config change (e.g. flipping OMNIGENT_AUTH_ENABLED) respawns the
# server instead of silently reusing one in the old auth mode.
_LOCAL_SERVER_SIG_PATH = _local_data_dir() / "local_server.sig"

# Sidecar carrying the absolute path of the background server's captured
# stdout/stderr log file (one line). Lets `server start` / `server status`
# point at the exact ``logs/server/local-server-*.log`` even when reusing a
# server this invocation didn't spawn. Absent for a foreground
# ``omnigent server`` (its logs stream to the terminal, not a file).
_LOCAL_SERVER_LOG_REF_PATH = _local_data_dir() / "local_server.logpath"


def server_config_signature() -> str:
    """
    Compute a signature of the server-affecting config for one invocation.

    The daemon (in local mode) spawns the Omnigent server once and never
    re-reads its spawn config, so a reused server silently keeps the auth
    mode — and the *code* — it was born with. Stamping this signature lets
    reuse detect when a later invocation wants a *different* server (e.g.
    the user flips ``OMNIGENT_AUTH_ENABLED``, or upgrades the package) and
    respawn instead of serving the stale one.

    Covers the inputs that change server behavior at spawn time:

    * the resolved auth source — auth mode is baked at boot and cannot be
      reconfigured in place; and
    * the installed package version — a running server holds its code in
      memory, so after ``omni upgrade`` (or a manual ``uv tool upgrade``)
      the old process keeps serving pre-upgrade code until it is cycled.
      Folding the version in makes the next CLI command notice the drift
      and respawn the server on the new code through the existing
      config-drift path in :func:`ensure_local_omnigent_server` — no
      explicit restart required.

    Deliberately narrow otherwise, so unrelated env churn does not force
    needless restarts.

    :returns: A short hex digest, e.g. ``"3f9a1c2b4d5e6f70"``.
    """
    import hashlib
    import importlib.metadata
    import json

    from omnigent.server.auth import resolve_auth_source

    try:
        version = importlib.metadata.version("omnigent")
    except importlib.metadata.PackageNotFoundError:
        # Running from a source tree with no registered distribution —
        # nothing to key version-drift on, so leave it out of the payload.
        version = ""

    payload = json.dumps({"auth": resolve_auth_source(), "version": version}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    A zombie (exited but not yet reaped) process still exists in the
    process table but has already terminated and can never be signalled,
    so it is reported as dead here. Otherwise a daemon whose parent never
    reaps it blocks ``host`` startup with "already running" and makes
    ``host stop --force`` fail forever.

    :param pid: Process ID to check.
    :returns: ``True`` if the process exists and is not a zombie.
    """
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        # Includes psutil.ZombieProcess (a NoSuchProcess subclass), raised
        # on platforms where a zombie's status cannot be queried at all.
        return False
    except psutil.AccessDenied:
        # The process exists but belongs to another user.
        return True


def _read_local_server_pid_file() -> tuple[int, int] | None:
    """Read the local server pidfile (two lines: PID and port).

    :returns: ``(pid, port)`` if well-formed, ``None`` otherwise.
    """
    if not _LOCAL_SERVER_PID_PATH.exists():
        return None
    try:
        lines = _LOCAL_SERVER_PID_PATH.read_text().strip().splitlines()
        if len(lines) < 2:
            return None
        return int(lines[0]), int(lines[1])
    except (ValueError, OSError):
        return None


def local_server_url_if_healthy() -> str | None:
    """Return the URL of a live, reused local server, else ``None``.

    A pidfile alone is not trusted: the recorded PID must still be alive
    AND the server must answer ``/health`` on the recorded port. A stale
    or half-dead entry returns ``None`` so the caller spawns a fresh one.

    This is config-agnostic on purpose — it answers "is a local server
    reachable?" for URL discovery. Config-signature gating lives in
    :func:`ensure_local_omnigent_server`, the one place that decides reuse.

    :returns: ``"http://127.0.0.1:<port>"`` when reusable, else ``None``.
    """
    import httpx

    existing = _read_local_server_pid_file()
    if existing is None:
        return None
    pid, port = existing
    if not _pid_alive(pid):
        return None
    base_url = f"http://127.0.0.1:{port}"
    try:
        resp = httpx.get(f"{base_url}/health", timeout=2.0)
    except httpx.HTTPError:
        return None
    if resp.status_code == 200:
        return base_url
    return None


def _write_local_server_record(
    pid: int, port: int, sig: str, log_path: Path | None = None
) -> None:
    """Write the pidfile + config-signature sidecar for the canonical server.

    The single writer for all three files so the daemon-spawn and foreground
    (``omnigent server``) registration paths stay symmetric: a server
    that advertises itself in the pidfile ALWAYS stamps a matching sig,
    so reuse-matching in :func:`ensure_local_omnigent_server` can't spuriously
    fail and respawn. Stamp the sig first; the pidfile is what reuse keys
    on, so it must never exist without its sig.

    :param pid: PID to record as the canonical local server.
    :param port: Loopback port the server bound, e.g. ``6767``.
    :param sig: Config signature from :func:`server_config_signature`.
    :param log_path: Absolute path of the spawned server's captured log file,
        e.g. ``Path("/Users/alice/.omnigent/logs/server/local-server-ab12cd.log")``.
        ``None`` for a foreground server whose logs stream to the terminal —
        any stale log-ref sidecar is then removed so status never reports a
        log file that doesn't apply to the running server.
    """
    _LOCAL_SERVER_PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write each file atomically (temp + os.replace) so a concurrent
    # connect/run reader never observes a half-written record — a torn read
    # would parse as malformed and trigger a needless respawn. Sig first:
    # both replaces are individually atomic, so once the pidfile (what reuse
    # keys on) appears, its sig is already fully in place.
    _atomic_write(_LOCAL_SERVER_SIG_PATH, f"{sig}\n")
    if log_path is not None:
        _atomic_write(_LOCAL_SERVER_LOG_REF_PATH, f"{log_path}\n")
    else:
        with contextlib.suppress(OSError):
            _LOCAL_SERVER_LOG_REF_PATH.unlink()
    _atomic_write(_LOCAL_SERVER_PID_PATH, f"{pid}\n{port}\n")


def _read_local_server_log_path() -> Path | None:
    """Read the running local server's captured-log path from its sidecar.

    :returns: The absolute log path the background server writes to, e.g.
        ``Path("/Users/alice/.omnigent/logs/server/local-server-ab12cd.log")``, or
        ``None`` when the sidecar is absent (foreground server, legacy
        record, or no server) or unreadable.
    """
    try:
        text = _LOCAL_SERVER_LOG_REF_PATH.read_text().strip()
    except OSError:
        return None
    return Path(text) if text else None


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a same-dir temp + replace.

    :param path: Destination file, e.g. ``~/.omnigent/local_server.pid``.
    :param text: Full file contents to write.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        # fdopen didn't take ownership of the fd — close it ourselves. (Once
        # fdopen succeeds, the `with` below owns and closes it; closing here
        # too would risk a double-close of a reused fd in this multithreaded
        # process.)
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    try:
        with fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _read_local_server_sig() -> str | None:
    """Read the running local server's config signature sidecar.

    :returns: The stored signature, e.g. ``"3f9a1c2b4d5e6f70"``, or
        ``None`` if the sidecar is absent (legacy server) or unreadable.
    """
    try:
        sig = _LOCAL_SERVER_SIG_PATH.read_text().strip()
    except OSError:
        return None
    return sig or None


_STOP_GRACE_S = 10.0
"""Seconds to wait for the server process to exit after SIGTERM before
escalating to SIGKILL.  Must be long enough for the lifespan shutdown
(harness subprocess cleanup, MCP pool drain) to complete under normal
conditions."""

_STOP_POLL_INTERVAL_S = 0.1
"""Polling interval while waiting for the server process to exit."""


def _terminate_pid(pid: int) -> None:
    """SIGTERM a pid, wait up to the grace period, then SIGKILL if needed.

    Shared by :func:`stop_local_omnigent_server` (the pidfile-tracked server) and
    :func:`stop_untracked_local_server` (an orphan whose pidfile was lost).
    Waits for the process to exit so the listening socket is released and the
    port becomes immediately re-bindable. Best-effort: a dead pid is a no-op.

    :param pid: Process id to terminate, e.g. ``93359``.
    :returns: None.
    """
    import signal

    if not _pid_alive(pid):
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(_STOP_POLL_INTERVAL_S)
    # Grace period expired — force-kill so the port is freed. Windows has no
    # SIGKILL; os.kill with SIGTERM there maps to TerminateProcess (forceful).
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    # Brief wait for the kernel to reap after SIGKILL.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(_STOP_POLL_INTERVAL_S)


def stop_local_omnigent_server() -> None:
    """Stop the daemon-owned background local server and clear its files.

    Sends ``SIGTERM`` to the recorded server PID (if alive), waits up to
    :data:`_STOP_GRACE_S` for it to exit (so the listening socket is
    released and the port becomes immediately re-bindable), then escalates
    to ``SIGKILL`` if necessary. Removes the pidfile, config-signature
    sidecar, and log-path sidecar so a subsequent
    :func:`ensure_local_omnigent_server` spawns a fresh
    server rather than reusing the stopped one. Best-effort: a missing or
    dead server is a no-op.

    This is pidfile-scoped by design. An orphan whose pidfile was lost is
    NOT visible here — :func:`stop_untracked_local_server` covers that, and
    the off-switch (``omnigent stop`` / ``server stop``) calls both.

    :returns: None.
    """
    existing = _read_local_server_pid_file()
    if existing is not None:
        pid, _port = existing
        _terminate_pid(pid)
    with contextlib.suppress(OSError):
        _LOCAL_SERVER_PID_PATH.unlink()
    with contextlib.suppress(OSError):
        _LOCAL_SERVER_SIG_PATH.unlink()
    with contextlib.suppress(OSError):
        _LOCAL_SERVER_LOG_REF_PATH.unlink()


@dataclass(frozen=True)
class LocalServerInfo:
    """Status of the detached background local server.

    :param running: ``True`` only when the recorded PID is alive AND the
        server answers ``/health`` on the recorded port.
    :param pid: Recorded server PID, or ``None`` when no pidfile exists.
    :param port: Recorded server port, or ``None`` when no pidfile exists.
    :param url: Base URL when running, e.g. ``"http://127.0.0.1:8123"``;
        ``None`` when not running.
    :param log_path: Absolute path of the background server's captured log
        file, e.g. ``Path("/Users/alice/.omnigent/logs/server/local-server-ab12cd.log")``.
        ``None`` for a foreground server (logs stream to its terminal) or a
        legacy record without the log-path sidecar.
    """

    running: bool
    pid: int | None
    port: int | None
    url: str | None
    log_path: Path | None = None


def local_server_status() -> LocalServerInfo:
    """Report the detached background local server's status.

    Reads ``~/.omnigent/local_server.pid`` for the recorded pid/port and
    probes ``/health`` to decide ``running``. A stale pidfile (PID dead or
    health failing) reports ``running=False`` while still surfacing the
    recorded pid/port for diagnostics.

    :returns: A :class:`LocalServerInfo` describing the background server.
    """
    existing = _read_local_server_pid_file()
    url = local_server_url_if_healthy()
    log_path = _read_local_server_log_path()
    if existing is None:
        return LocalServerInfo(
            running=url is not None, pid=None, port=None, url=url, log_path=log_path
        )
    pid, port = existing
    return LocalServerInfo(running=url is not None, pid=pid, port=port, url=url, log_path=log_path)


@dataclass(frozen=True)
class LocalServerStartup:
    """Outcome of :func:`ensure_local_omnigent_server`.

    :param url: Base URL of the running server, e.g.
        ``"http://127.0.0.1:8123"``.
    :param spawned: ``True`` when this call started a NEW detached server
        process; ``False`` when it reused an already-running healthy server
        (one started earlier by ``omnigent server`` or by a prior
        ``connect`` / ``run`` daemon). Callers that own teardown — notably
        the connect Ctrl-C stop-server prompt — gate on this so they only
        offer to stop a server they actually brought up, never one the user
        started independently.
    :param log_path: Absolute path of the background server's captured log
        file, e.g. ``Path("/Users/alice/.omnigent/logs/server/local-server-ab12cd.log")``
        — surfaced so callers (``server start``) can point the user at the
        exact log. For a spawned server this is the freshly created log; for
        a reused one it is read back from the log-path sidecar, and may be
        ``None`` when the running server is a foreground ``omnigent server``
        (logs stream to its terminal) or a legacy record without the sidecar.
    """

    url: str
    spawned: bool
    log_path: Path | None = None


def ensure_local_omnigent_server() -> LocalServerStartup:
    """Ensure a persistent background local Omnigent server is running.

    Reuses a healthy server recorded in the pidfile; otherwise spawns a
    detached ``omnigent server`` on a free loopback port, backed by the
    persistent ``~/.omnigent`` data store so conversations survive across
    invocations (designs/RUN_OMNIGENT_SESSION_RESUMPTION.md). The server runs
    accounts mode (the default) so the daemon and CLI
    both authenticate via built-in accounts and runner launches authorize.

    The server runs without a pre-bound tunnel token: runners are launched
    on demand by the connect daemon and authenticate as token-bound
    loopback runners, matching the deployed server posture.

    :returns: A :class:`LocalServerStartup` carrying the server URL and
        whether this call spawned it (``spawned=True``) or reused an
        already-running one (``spawned=False``). A config-drift respawn
        (stop stale + start fresh) counts as ``spawned=True`` — the fresh
        server is ours.
    :raises click.ClickException: If the server does not become healthy
        within the startup timeout, or if port contention persists after
        a free-port respawn.
    """
    desired_sig = server_config_signature()
    reused = local_server_url_if_healthy()
    if reused is not None:
        if _read_local_server_sig() == desired_sig:
            return LocalServerStartup(
                url=reused, spawned=False, log_path=_read_local_server_log_path()
            )
        # Config drift: the running server was spawned under a different
        # auth source and cannot be reconfigured in place (auth
        # mode, cookie secret, etc. are baked at boot). Stop it and spawn
        # a fresh one below so the invocation's intent takes effect.
        stop_local_omnigent_server()

    # Prefer the stable :6767 so the daemon-spawned server lands on the
    # same URL as a manual `omnigent server` (and reuse via the pidfile
    # keeps them from ever both running); fall back to a free port if
    # taken.
    port = pick_local_port()
    retried = False
    while True:
        spawned = _spawn_local_server(port)
        startup_error: click.ClickException | None = None
        try:
            _wait_for_local_omnigent_server(spawned.base_url, spawned.proc, spawned.log_path)
        except click.ClickException as exc:
            startup_error = exc
        foreign_owner = _foreign_port_owner(port, spawned.proc.pid)
        if foreign_owner is None:
            if startup_error is not None:
                raise startup_error
            # Advertise the server ONLY now that it is healthy AND provably
            # ours. Writing the record any earlier publishes a port we may
            # abandon: concurrent discoverers (the CLI's daemon-wait loop
            # polls local_server_url_if_healthy every 0.2s) would adopt the
            # contended port while a foreign server answers its /health,
            # then lose it when that server's owner stops it.
            _write_local_server_record(
                spawned.proc.pid, port, desired_sig, log_path=spawned.log_path
            )
            return LocalServerStartup(
                url=spawned.base_url, spawned=True, log_path=spawned.log_path
            )
        # A DIFFERENT process owns the port. The stable-port preference
        # means two concurrent spawners (another HOME on this box, a
        # parallel test worker) can both pick the preferred port between
        # the bind probe and the child's actual bind; the loser's child
        # dies EADDRINUSE while the winner's server answers our /health
        # probe, so without this check the CLI silently adopts a server
        # it does not own (wrong owner, wrong DB) that can vanish at the
        # owner's whim. Never adopt it: let our doomed child run to its
        # natural EADDRINUSE exit, then respawn once on an OS-assigned
        # free port, which concurrent spawners never prefer.
        _await_doomed_child_exit(spawned.proc)
        if pinned_local_port() is not None:
            # The user pinned this port and asked to fail rather than wander
            # to a random one. A foreign owner means the pinned port is taken.
            raise click.ClickException(
                f"Local server port {port} is pinned but owned by pid "
                f"{foreign_owner} — refusing to start on a random port. Free it "
                f"(`lsof -nP -iTCP:{port} -sTCP:LISTEN`) or change "
                f"`local_server_port` in ~/.omnigent/config.yaml, then retry."
            )
        if retried:
            raise click.ClickException(
                f"Local server port contention persists: port {port} is owned by "
                f"pid {foreign_owner} even after a free-port respawn. "
                f"Server log: {spawned.log_path}"
            )
        retried = True
        port = _find_free_local_port()


@dataclass(frozen=True)
class _SpawnedLocalServer:
    """A just-spawned detached server subprocess, before readiness.

    :param proc: The ``omnigent server`` subprocess handle.
    :param log_path: File capturing the child's stdout/stderr, e.g.
        ``Path("~/.omnigent/logs/server/local-server-ab12cd.log")``.
    :param base_url: Loopback URL the child was asked to bind, e.g.
        ``"http://127.0.0.1:6767"``.
    """

    proc: subprocess.Popen[bytes]
    log_path: Path
    base_url: str


def _await_doomed_child_exit(proc: subprocess.Popen[bytes]) -> None:
    """Wait for a bind-race-doomed server child to exit on its own.

    The loser of a port bind race reaches its EADDRINUSE death only AFTER
    boot-time DB migrations complete (uvicorn binds last). SIGTERMing it
    mid-migration leaves a half-migrated sqlite DB (DDL committed, alembic
    version not yet stamped) that breaks the free-port respawn's own
    migration with "table agents already exists". Letting the child run to
    its natural exit leaves a fully-migrated DB the respawn reuses cleanly.
    A terminate backstop (after :data:`_DOOMED_CHILD_EXIT_GRACE_S`) covers
    a child that somehow never exits.

    :param proc: The doomed server subprocess.
    """
    deadline = time.monotonic() + _DOOMED_CHILD_EXIT_GRACE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(_STOP_POLL_INTERVAL_S)
    _terminate_pid(proc.pid)


def _foreign_port_owner(port: int, own_pid: int) -> int | None:
    """Return the pid of a foreign process listening on *port*, if any.

    Best-effort via ``lsof`` (:func:`_pid_listening_on_port`): when ``lsof``
    is missing or reports nothing, ownership cannot be disproven and the
    caller proceeds, matching the pre-existing degraded behavior of
    :func:`stop_untracked_local_server`.

    :param port: Loopback TCP port the child was asked to bind, e.g. ``6767``.
    :param own_pid: Our spawned child's pid.
    :returns: The listening pid when it is NOT *own_pid*, else ``None``.
    """
    owner = _pid_listening_on_port(port)
    if owner is None or owner == own_pid:
        return None
    return owner


def _spawn_local_server(port: int) -> _SpawnedLocalServer:
    """Spawn the detached background server subprocess on *port*.

    Deliberately does NOT write the pidfile record: the caller
    (:func:`ensure_local_omnigent_server`) records the server only after it
    is healthy and provably owns its port, so concurrent discoverers never
    observe a record for a port this spawn may abandon.

    :param port: Loopback TCP port for the child to bind, e.g. ``6767``.
    :returns: The spawned subprocess plus its log path and base URL; the
        caller awaits readiness via :func:`_wait_for_local_omnigent_server`.
    """
    data_dir = _local_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(exist_ok=True)
    db_path = data_dir / "chat.db"
    artifact_path = data_dir / "artifacts"
    # An explicit OMNIGENT_DATABASE_URI wins over the per-data-dir sqlite
    # file (e.g. to point a worktree at its own Postgres). Defaults to the
    # isolated sqlite db under the runtime data dir.
    db_uri = os.environ.get("OMNIGENT_DATABASE_URI") or f"sqlite:///{db_path}"

    log_dir = data_dir / "logs" / "server"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_fd, log_name = tempfile.mkstemp(prefix="local-server-", suffix=".log", dir=log_dir)
    log_path = Path(log_name)
    log_fh = os.fdopen(log_fd, "wb")

    # Pass the full parent env: this server IS the local runtime —
    # loopback-only, same single user, and it needs the LLM creds to
    # function.
    #
    # Accounts mode: when the parent env selects accounts mode
    # (OMNIGENT_AUTH_ENABLED=1 without OIDC config, or an explicit
    # OMNIGENT_AUTH_PROVIDER=accounts), inject the per-spawn
    # cookie secret + base URL. Persisted via
    # load_or_generate_cookie_secret so daemon restarts don't
    # invalidate every existing browser session. From the user's
    # POV, `omnigent run` (no --server) in accounts mode gets
    # "browser auto-opens signed in + TUI auto-signed in" once
    # the spawned server's bootstrap fires.
    child_env = {**os.environ}
    # Mirror create_auth_provider's resolution via the shared helper so the
    # daemon-owned server agrees with the server's own auth wiring: header is
    # the env-unset default; OMNIGENT_AUTH_ENABLED=1 opts into accounts (or
    # oidc when OMNIGENT_OIDC_* is set). In header/oidc mode we must NOT mint
    # an accounts cookie secret (those modes never read it, and writing the
    # secret file is pointless churn for a local-dev session).
    from omnigent.server.auth import resolve_auth_source

    # This server is the user's single-user loopback runtime. Mark it so the
    # host tunnel may re-own this machine's host_id across an auth-mode flip
    # (header↔accounts changes the owner). Deployed multi-user servers never
    # set this, preserving the W2-class host-hijack boundary.
    child_env["OMNIGENT_LOCAL_SINGLE_USER"] = "1"
    _accounts_mode = resolve_auth_source() == "accounts"
    if _accounts_mode:
        if "OMNIGENT_ACCOUNTS_COOKIE_SECRET" not in os.environ:
            from omnigent.server.accounts_secret import (
                load_or_generate_cookie_secret,
            )

            child_env["OMNIGENT_ACCOUNTS_COOKIE_SECRET"] = load_or_generate_cookie_secret(data_dir)
        # Always override BASE_URL — the parent's value (if any)
        # almost certainly points at a different port than the
        # freshly picked one.
        child_env["OMNIGENT_ACCOUNTS_BASE_URL"] = f"http://127.0.0.1:{port}"

    try:
        proc = subprocess.Popen(
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
                db_uri,
                "--artifact-location",
                str(artifact_path),
            ],
            env=child_env,
            stdout=log_fh,
            stderr=log_fh,
            **_proc.spawn_kwargs(),
        )
    finally:
        log_fh.close()

    return _SpawnedLocalServer(proc=proc, log_path=log_path, base_url=f"http://127.0.0.1:{port}")


def _find_free_local_port() -> int:
    """Find a free loopback TCP port for the background local server.

    :returns: An available port number on ``127.0.0.1``.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# Deliberately uncommon: 8000 is the default for FastAPI/uvicorn (and many
# other dev servers), so it was frequently already taken and the local server
# kept landing on a random fallback port — breaking bookmarked URLs.
# Deployments (Databricks Apps, Docker) pin their own port (typically 8000,
# the platform convention) and do NOT read this constant.
_DEFAULT_LOCAL_PORT = 6767


def pinned_local_port() -> int | None:
    """Return the user-pinned local server port, or ``None`` when unpinned.

    Precedence: the ``OMNIGENT_LOCAL_PORT`` env var (shell / CLI launches)
    over ``local_server_port`` in ``config.yaml`` (the latter survives GUI
    app launches that don't inherit the shell env). The config dir honors
    ``OMNIGENT_CONFIG_HOME`` like the CLI; this module reads the file
    directly rather than importing ``cli.py`` (see the module docstring).
    Invalid / out-of-range values are ignored (treated as unpinned).

    :returns: The pinned port, or ``None`` if not configured / invalid.
    """
    raw: object = os.environ.get("OMNIGENT_LOCAL_PORT")
    if not raw:
        config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
        config_path = (
            Path(config_home).expanduser() if config_home else Path.home() / ".omnigent"
        ) / "config.yaml"
        if config_path.exists():
            import yaml

            with contextlib.suppress(Exception):
                cfg = yaml.safe_load(config_path.read_text()) or {}
                if isinstance(cfg, dict):
                    raw = cfg.get("local_server_port")
    if raw in (None, ""):
        return None
    try:
        port = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _port_bindable(port: int) -> bool:
    """Return ``True`` if *port* can be bound on loopback right now."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def pick_local_port(preferred: int | None = None) -> int:
    """Return the loopback port for the local server.

    With no *preferred* (the daemon / ``omnigent run`` spawn path) the port
    is resolved from the user's pin (:func:`pinned_local_port`): a pinned
    port is returned when free, else a :class:`click.ClickException` is
    raised — the user asked to fail rather than silently land on a random
    port (which breaks bookmarked URLs). Unpinned, it prefers the stable
    :data:`_DEFAULT_LOCAL_PORT` (6767) and falls back to a free port when
    that is taken.

    An explicit *preferred* (the foreground ``omnigent server --port``)
    keeps the legacy prefer-or-free behavior.

    Reuse of an existing server happens via the pidfile
    (:func:`local_server_url_if_healthy`), NOT by assuming the port, so a
    fallback never breaks discovery.

    :param preferred: A caller-chosen port to try first, or ``None`` to
        resolve from the pin / stable default.
    :returns: An available loopback port.
    :raises click.ClickException: When a pinned port is busy.
    """
    if preferred is not None:
        return preferred if _port_bindable(preferred) else _find_free_local_port()

    pinned = pinned_local_port()
    if pinned is not None:
        if _port_bindable(pinned):
            return pinned
        raise click.ClickException(
            f"Local server port {pinned} is pinned but busy — refusing to start "
            f"on a random port. Free it (`lsof -nP -iTCP:{pinned} -sTCP:LISTEN`) "
            f"or change `local_server_port` in ~/.omnigent/config.yaml, then retry."
        )

    if _port_bindable(_DEFAULT_LOCAL_PORT):
        return _DEFAULT_LOCAL_PORT
    return _find_free_local_port()


def _pid_listening_on_port(port: int) -> int | None:
    """Return the PID listening on loopback *port*, via ``lsof``.

    Used to find an untracked local server (one whose pidfile was lost) so
    the off-switch can stop it. Cross-platform across macOS + Linux where
    ``lsof`` is present; returns ``None`` when ``lsof`` is missing, errors,
    or nothing is listening — the caller then degrades to a manual hint.

    :param port: Loopback TCP port, e.g. ``6767``.
    :returns: The first listening PID, or ``None``.
    """
    try:
        out = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        ).stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    for line in out.split():
        with contextlib.suppress(ValueError):
            return int(line)
    return None


def _local_server_health_ok(base_url: str) -> bool:
    """Return ``True`` if *base_url* answers ``/health`` as an Omnigent server.

    Confirms a listener is actually an Omnigent server (``GET /health`` →
    200 with ``{"status": "ok"}``) before the off-switch stops it, so we
    never kill an unrelated process that happens to hold the port.

    :param base_url: Loopback URL, e.g. ``"http://127.0.0.1:6767"``.
    :returns: ``True`` only on a 200 ``{"status": "ok"}`` response.
    """
    import httpx

    try:
        resp = httpx.get(f"{base_url}/health", timeout=2.0)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("status") == "ok"


def stop_untracked_local_server(port: int = _DEFAULT_LOCAL_PORT) -> int | None:
    """Stop an orphaned local server on *port* that the pidfile doesn't track.

    The pidfile can be lost while the server process lives (a torn/cleared
    record, a respawn that landed on a different port, a crash). Such a
    server then escapes :func:`stop_local_omnigent_server`, which only knows the
    pidfile PID — so ``omnigent stop`` / ``server stop`` would leave it
    running. This sweep covers that hole: if a live Omnigent server answers
    ``/health`` on the canonical loopback *port*, find its PID and terminate
    it. Call it AFTER :func:`stop_local_omnigent_server` so a normally-tracked
    server is already gone and ``/health`` no longer answers (this is a
    no-op). Best-effort: returns ``None`` when nothing untracked is found or
    ``lsof`` is unavailable.

    :param port: Canonical loopback port to sweep, e.g. ``6767``.
    :returns: The PID stopped, or ``None`` if there was nothing to stop.
    """
    base_url = f"http://127.0.0.1:{port}"
    if not _local_server_health_ok(base_url):
        return None
    pid = _pid_listening_on_port(port)
    if pid is None or not _pid_alive(pid):
        return None
    _terminate_pid(pid)
    return pid


def register_local_server(port: int) -> None:
    """Record THIS process as the canonical local server in the pidfile.

    Lets a foreground ``omnigent server`` advertise itself in the same
    ``local_server.pid`` the daemon reads, so ``omnigent run`` /
    ``connect`` reuse it instead of spawning a competitor.

    Stamps the config-signature sidecar alongside the pidfile (same writer
    as the daemon-spawn path). Without it, a foreground server presents no
    sig and reuse-matching in :func:`ensure_local_omnigent_server` always sees
    ``None != desired`` — silently stopping and respawning a perfectly good
    foreground server on the next ``connect`` / ``run``.

    :param port: The port this server bound, e.g. ``6767``.
    """
    _write_local_server_record(os.getpid(), port, server_config_signature())


def clear_local_server_record() -> None:
    """Remove the pidfile + sig sidecar if they still point at THIS process.

    Called on ``omnigent server`` shutdown so a clean exit doesn't
    leave a stale record. Guarded on the recorded pid matching ours so
    we never delete a daemon-spawned server's record. The pidfile, sig,
    and log-path sidecar are written together by
    :func:`_write_local_server_record`, so they must be cleared together
    too — leaving one behind would contradict its meaning ("state of the
    running server").
    """
    existing = _read_local_server_pid_file()
    if existing is not None and existing[0] == os.getpid():
        with contextlib.suppress(OSError):
            _LOCAL_SERVER_PID_PATH.unlink()
        with contextlib.suppress(OSError):
            _LOCAL_SERVER_SIG_PATH.unlink()
        with contextlib.suppress(OSError):
            _LOCAL_SERVER_LOG_REF_PATH.unlink()


def _wait_for_local_omnigent_server(
    base_url: str,
    proc: subprocess.Popen[bytes],
    log_path: Path,
    timeout: float = _LOCAL_SERVER_READY_TIMEOUT_SECONDS,
) -> None:
    """Poll the background local server's ``/health`` until ready.

    :param base_url: Loopback server URL, e.g. ``"http://127.0.0.1:8123"``.
    :param proc: The server subprocess; early exit is detected via
        ``proc.poll()`` so we fail fast instead of waiting the full timeout.
    :param log_path: Captured stdout/stderr log surfaced on failure so the
        underlying traceback (spec parse error, port clash, import failure)
        is visible.
    :param timeout: Max seconds to wait for readiness.
    :raises click.ClickException: If the server exits or never answers.
    """
    import time

    import httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _raise_local_server_failed(base_url, log_path)
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.TransportError:
            # Catch the whole transport family, not just ConnectError: a
            # slow-starting server also surfaces ConnectTimeout / ReadTimeout
            # (both TransportError, not ConnectError), which are transient
            # "not ready yet" conditions we want to keep polling through
            # rather than crash on.
            pass
        time.sleep(0.2)
    _raise_local_server_failed(base_url, log_path)


def _raise_local_server_failed(base_url: str, log_path: Path) -> None:
    """Raise a descriptive error for a failed background-server startup.

    :param base_url: The loopback URL the server was meant to bind.
    :param log_path: Captured stdout/stderr log file.
    :raises click.ClickException: Always.
    """
    try:
        lines = log_path.read_text(errors="replace").splitlines()
        tail = "\n".join(lines[-50:]) if lines else "(empty log file)"
    except OSError as exc:
        tail = f"(could not read log file: {exc})"
    # A failed spawn leaves a misleading pidfile; clear it (and the sig
    # sidecar) so the next invocation does not try to reuse a dead entry.
    with contextlib.suppress(OSError):
        _LOCAL_SERVER_PID_PATH.unlink()
    with contextlib.suppress(OSError):
        _LOCAL_SERVER_SIG_PATH.unlink()
    raise click.ClickException(
        f"Background local server failed to start ({base_url}).\n"
        f"  Server log: {log_path}\n"
        f"\n  Last 50 lines:\n{tail}"
    )
