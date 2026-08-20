"""A copy-on-write forkserver ("zygote") for runner processes.

Every session today spawns a fresh ``python -m omnigent.runner._entry``, each
paying the full import floor (omnigent's graph + pydantic/fastapi/httpx) — on a
host running N sessions that floor is duplicated N times. This module collapses
it: a single long-lived zygote imports the runner graph ONCE, then ``os.fork()``s
a child per session. On Linux the shared read-only import pages are copy-on-write,
so each additional runner costs only the pages it dirties, not another ~120MB.

The zygote is launched by the host daemon (``omnigent/host/connect.py``) as a
plain ``subprocess.Popen`` child — a fresh interpreter, so it inherits none of
the daemon's asyncio loop, websocket, or worker threads (forking from a
multithreaded async process would deadlock the child). It is single-threaded,
holds no event loop and no network sockets, and blocks on one ``AF_UNIX``
control socket handed to it by the daemon.

Protocol (newline-delimited JSON, one request → one response):

  {"cmd": "ping"}                          -> {"pong": true}
  {"cmd": "fork", "env": {...},            -> {"pid": 12345}
   "log_path": "/…/runner-ab12.log"}          or {"error": "..."}
  {"cmd": "poll", "pid": 12345}            -> {"returncode": 0} | {"returncode": null}

The forked child closes the control socket, points stdio at the session log
file, applies the request's env into ``os.environ``, and calls the unchanged
``omnigent.runner._entry.main()`` — so it behaves exactly like a cold
``python -m omnigent.runner._entry``.

**Parent-pid contract:** the runner's parent-death watchdog treats
``os.getppid() != RUNNER_PARENT_PID`` as "orphaned" (see
``omnigent/runner/_entry.py``). A zygote-forked runner's OS parent is the
*zygote*, so the daemon MUST set ``RUNNER_PARENT_PID`` to the zygote's pid, not
its own. When the daemon dies, the control socket hits EOF, the zygote exits,
its runner children reparent, ``getppid()`` changes, and each runner tears
itself down — preserving today's parent-death semantics through one extra hop.

POSIX (the gate is ``IS_POSIX``): the fork path runs on macOS too, though the
copy-on-write savings are Linux-specific. On by default; set
``OMNIGENT_RUNNER_ZYGOTE=0`` to opt out. The daemon falls back to direct
``Popen`` if the zygote is unavailable, so this is never a hard dependency.
"""

from __future__ import annotations

import contextlib
import gc
import importlib.util
import json
import os
import selectors
import socket
import sys
import threading
from pathlib import Path
from typing import Any, cast

from omnigent.process_logging import LOG_TTY_FD_ENV_VAR, env_truthy

# Env var the daemon sets to the inherited control-socket fd number.
ZYGOTE_CONTROL_FD_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_CONTROL_FD"
# Env var gating zygote use in the daemon (read there, documented here). The
# zygote is on by default; set this to 0/false/no/off to opt out.
ZYGOTE_ENABLED_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE"
# Env var the zygote sets on each forked runner: the fd of that runner's own
# connected control socket back to the zygote. The runner uses it to ask the
# zygote to fork its harness children (which then share the harness import
# graph copy-on-write), instead of exec'ing a fresh interpreter per harness.
ZYGOTE_HARNESS_FD_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD"
# Env var the zygote sets on each forked HARNESS child so its parent-death
# watchdog probes the runner pid explicitly rather than trusting os.getppid()
# (which is the zygote, not the runner, for a zygote-forked harness).
ZYGOTE_HARNESS_FORKED_ENV_VAR = "OMNIGENT_HARNESS_ZYGOTE_FORKED"
# Test-only seam: when present in a fork payload, the child exits with this code
# instead of running the real runner. Never set in production launches.
_ZYGOTE_TEST_CHILD_EXIT_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_TEST_CHILD_EXIT"
# Test-only seam: raise SystemExit(code) rather than os._exit, to exercise the
# child guard's SystemExit-code preservation. Never set in production launches.
_ZYGOTE_TEST_CHILD_RAISE_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_TEST_CHILD_RAISE"
# Test-only seam: when set, the child sleeps for this many seconds (staying
# genuinely alive) instead of exiting, so a test can kill the zygote out from
# under a live child and assert the crash-recovery path. Never set in prod.
_ZYGOTE_TEST_CHILD_SLEEP_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_TEST_CHILD_SLEEP"


def _disk_build_stamp(package_dir: Path | None = None) -> tuple[float, str] | None:
    """Read ``(BUILD_TIME_EPOCH, COMMIT_SHA)`` from the on-disk ``_build_info.py``.

    Loaded fresh from the file every call — never via ``sys.modules`` — so the
    value tracks what an in-place ``omnigent`` upgrade rewrote on disk, not
    what this process imported at boot.

    :param package_dir: Directory holding ``_build_info.py``; defaults to the
        ``omnigent`` package directory this module was loaded from.
    :returns: The stamp, or ``None`` when the file is missing or unreadable
        (e.g. an unbuilt source checkout, or a package mid-rewrite).
    """
    if package_dir is None:
        # Derived from this module's own location rather than the top-level
        # ``omnigent.__file__``: the zygote runs as ``python -m``, which puts
        # its cwd on sys.path, so a daemon started from a directory that holds
        # an ``omnigent`` checkout binds the top-level name to a namespace
        # package (``__file__`` is None) while this module still loads from the
        # real package.
        package_dir = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_omnigent_zygote_build_probe", package_dir / "_build_info.py"
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return float(module.BUILD_TIME_EPOCH), str(module.COMMIT_SHA)
    except Exception:  # noqa: BLE001 — any failure means "stamp unknown"
        return None


def _import_runner_graph() -> None:
    """Eagerly import the heavy runner graph so the ~120MB lands once.

    A forked child inherits every module imported here via copy-on-write, so
    this is the whole point of the zygote. Kept in sync with what
    ``omnigent.runner._entry.main`` pulls in on a cold start.

    The harness entrypoint + native harness graphs are imported too, so a
    harness child forked via ``fork_harness`` shares that floor as well (the
    heaviest part is the fastapi/pydantic/omnigent-core graph the runner
    already holds, so this adds little resident cost).
    """
    from omnigent.runner import _entry, app, native  # noqa: F401
    from omnigent.runtime.harnesses import _runner as _harness_runner  # noqa: F401


def _wire_child_stdio(log_path: str | None) -> None:
    """Point the forked child's stdio at the session log, stdin at /dev/null.

    Reproduces the daemon's direct-``Popen`` wiring (``stdin=DEVNULL``,
    ``stdout=stderr=<log file>``) without passing fds over the socket: the
    child reopens the log path itself. ``configure_process_logging`` in
    ``main()`` then also attaches its file handler via ``PROCESS_LOG_FILE``.

    :param log_path: Absolute session log path, or ``None`` to leave stdout/
        stderr inherited from the zygote.
    """
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(devnull, 0)
    finally:
        os.close(devnull)
    if log_path is None:
        return
    # Append + per-write (line-free) so interleaved runner output is not lost;
    # matches open_process_log_file's unbuffered "ab" handle. 0o600 because
    # runner logs can carry secrets (tokens, prompts) — matches
    # create_process_log_path, not a world-readable default.
    logfd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
    finally:
        if logfd > 2:
            os.close(logfd)


def _apply_child_env(request: dict[str, Any]) -> None:
    """Replace ``os.environ`` with the fork request's env (clean, not merged).

    Shared by runner and harness child bodies: a fresh env per fork means a
    stale var from a previous fork's payload can never leak into this child.
    The terminal-mirror fd (``--log-to-stderr``) is only valid as the number it
    holds in THIS process (the fd the zygote inherited), not the daemon-side
    number the payload carries, so it is preserved from the zygote's own env.

    :param request: The fork request carrying an ``env`` mapping.
    """
    inherited_tty_fd = os.environ.get(LOG_TTY_FD_ENV_VAR)
    env = request.get("env") or {}
    os.environ.clear()
    os.environ.update({str(k): str(v) for k, v in env.items()})
    # Drop control-fd hints so nothing downstream mistakes the child for a
    # zygote or reuses a now-closed fd.
    os.environ.pop(ZYGOTE_CONTROL_FD_ENV_VAR, None)
    os.environ.pop(ZYGOTE_HARNESS_FD_ENV_VAR, None)
    if inherited_tty_fd is not None:
        os.environ[LOG_TTY_FD_ENV_VAR] = inherited_tty_fd
    else:
        os.environ.pop(LOG_TTY_FD_ENV_VAR, None)


def _run_child(request: dict[str, Any], harness_fd: int) -> None:
    """Execute the runner in the freshly forked child. Never returns.

    The fork handler has already closed every inherited zygote control socket;
    this wires stdio, applies the request env, tells the runner which fd is its
    own zygote channel (for forking harness children), and hands off to the
    normal runner entrypoint.

    :param request: The ``fork`` request: ``env`` + optional ``log_path``.
    :param harness_fd: fd of this runner's own control socket back to the
        zygote, exported so the runner can request harness forks.
    """
    _apply_child_env(request)
    workspace = request.get("cwd")
    if not isinstance(workspace, str):
        raise ValueError("runner fork request requires a cwd")
    os.chdir(workspace)
    # Tell the runner its harness-fork channel fd (set after the env replace so
    # it survives the clear).
    os.environ[ZYGOTE_HARNESS_FD_ENV_VAR] = str(harness_fd)

    _wire_child_stdio(request.get("log_path"))

    # Test seam: exercise fork/reap/poll without booting a real runner. Honored
    # only when set in the fork payload's env (never in production launches).
    # Echoes this child's view of a marker var to stdout (its log) so a test can
    # prove one fork's env never leaks into another's. When the raise variant is
    # set it raises SystemExit instead of os._exit-ing, so the child guard's
    # SystemExit-code preservation can be tested end-to-end.
    _maybe_run_test_seam()

    from omnigent.runner._entry import main

    main()


def _maybe_run_test_seam() -> None:
    """Honor the fork-payload test seams (exit / raise / sleep). Never in prod.

    Returns normally when no seam is set; otherwise exits or sleeps and does
    not return. Shared by runner and harness child bodies so both can be driven
    deterministically (including staying alive for crash-recovery tests).
    """
    sleep_s = os.environ.get(_ZYGOTE_TEST_CHILD_SLEEP_ENV_VAR)
    if sleep_s is not None:
        import time

        time.sleep(float(sleep_s))
        os._exit(0)
    test_exit = os.environ.get(_ZYGOTE_TEST_CHILD_EXIT_ENV_VAR)
    if test_exit is not None:
        sys.stdout.write(f"marker={os.environ.get('OMNIGENT_ZYGOTE_MARKER', '')}\n")
        sys.stdout.write(f"tty_fd={os.environ.get(LOG_TTY_FD_ENV_VAR, '')}\n")
        sys.stdout.write(f"cwd={os.getcwd()}\n")
        sys.stdout.flush()
        if env_truthy(os.environ.get(_ZYGOTE_TEST_CHILD_RAISE_ENV_VAR)):
            raise SystemExit(int(test_exit))
        os._exit(int(test_exit))


def _run_harness_child(request: dict[str, Any]) -> None:
    """Execute a harness subprocess in the freshly forked child. Never returns.

    Reproduces ``python -m omnigent.runtime.harnesses._runner`` (which the
    process manager would otherwise exec) in-process, so the harness shares the
    zygote's already-imported graph copy-on-write. The fork handler has already
    closed every inherited zygote control socket.

    :param request: The ``fork_harness`` request: ``argv`` (the ``_runner`` CLI
        flags, incl. ``--parent-pid <runner_pid>``) + ``env``.
    """
    _apply_child_env(request)
    # The harness's OS parent is the zygote, not the runner, so its watchdog
    # must probe the runner pid explicitly rather than trust os.getppid().
    os.environ[ZYGOTE_HARNESS_FORKED_ENV_VAR] = "1"
    # A directly-exec'd harness inherits the runner's stdout/stderr; a
    # zygote-forked one inherits the zygote's, so point it at the harness log
    # (from PROCESS_LOG_FILE) to keep operator-visible output where it belongs.
    from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

    _wire_child_stdio(os.environ.get(PROCESS_LOG_FILE_ENV_VAR))

    # Test seam: a sleep seam keeps the harness genuinely alive (crash-recovery
    # tests); the exit seam echoes argv so a test can assert the payload
    # round-tripped. Never set in production.
    sleep_s = os.environ.get(_ZYGOTE_TEST_CHILD_SLEEP_ENV_VAR)
    if sleep_s is not None:
        import time

        time.sleep(float(sleep_s))
        os._exit(0)
    test_exit = os.environ.get(_ZYGOTE_TEST_CHILD_EXIT_ENV_VAR)
    if test_exit is not None:
        sys.stdout.write(f"harness_argv={' '.join(request.get('argv') or [])}\n")
        sys.stdout.flush()
        os._exit(int(test_exit))

    from omnigent.runtime.harnesses._runner import main

    main(list(request.get("argv") or []))


class _ZygoteServer:
    """Single-threaded ``selectors`` multiplexer over the zygote's sockets.

    The zygote watches the daemon control socket plus one control socket per
    forked runner (each runner uses its socket to request harness forks). All
    forked children — runners AND harnesses — are the zygote's OS children, so
    a single reap loop and one ``exit_codes`` map serve every ``poll``.

    Kept deliberately single-threaded: the zygote's whole value is forking from
    a thread-free, loop-free process, so it multiplexes rather than spawns a
    thread per connection.

    :param control_sock: The daemon control socket (role ``"daemon"``).
    :param graph_stamp: The on-disk build stamp captured when the zygote
        imported its graph, or ``None`` when unknown (unbuilt checkout).
        Both runner and harness forks are refused once the on-disk stamp no
        longer matches: the child would resolve its lazily-imported modules
        from the *new* files against the *old* pre-imported graph, breaking
        on any cross-version import.
    """

    def __init__(
        self,
        control_sock: socket.socket,
        graph_stamp: tuple[float, str] | None = None,
    ) -> None:
        self._graph_stamp = graph_stamp
        self._sel = selectors.DefaultSelector()
        # Exit codes of reaped children, held until the requester polls; the
        # daemon/runner are not these children's parents and cannot waitpid().
        self._exit_codes: dict[int, int] = {}
        self._live: set[int] = set()
        # Harness pids forked on behalf of each runner, keyed by that runner's
        # control-socket fileno. When a runner drops, its harnesses have no
        # remaining client to poll their exit code, so they are orphaned (see
        # _drop_runner) — reaped to avoid zombies but not retained in
        # _exit_codes, which would otherwise grow unbounded and risk pid-reuse
        # misattribution over a long-lived daemon.
        self._runner_harness_pids: dict[int, set[int]] = {}
        # Pids whose exit code must be discarded (not stored) when reaped: a
        # dropped runner's harness children that nothing will ever poll.
        self._orphaned: set[int] = set()
        # Every zygote-side control socket a forked child inherits and must
        # close (the daemon socket + all runner sockets).
        self._control_socks: set[socket.socket] = {control_sock}
        self._buffers: dict[int, bytearray] = {}
        self._sel.register(control_sock, selectors.EVENT_READ, "daemon")
        self._buffers[control_sock.fileno()] = bytearray()

    def serve(self) -> None:
        """Run the select loop until the daemon socket closes (daemon exit)."""
        try:
            while True:
                self._reap()
                # Timeout so idle periods still reap exited children promptly.
                for key, _mask in self._sel.select(timeout=1.0):
                    # Only sockets are ever registered, so key.fileobj (typed
                    # HasFileno | int by selectors) is always a socket here.
                    if not self._on_readable(cast("socket.socket", key.fileobj), key.data):
                        return
        finally:
            self._sel.close()

    def _reap(self) -> None:
        """Non-blocking reap of any exited children into ``exit_codes``.

        Orphaned pids (a dropped runner's harness children) are reaped to avoid
        zombies but their exit code is discarded rather than stored, since no
        client remains to poll it.
        """
        for pid in list(self._live):
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                self._forget_pid(pid)
                continue
            if waited == 0:
                continue
            self._live.discard(pid)
            if pid in self._orphaned:
                self._orphaned.discard(pid)
                continue
            self._exit_codes[pid] = os.waitstatus_to_exitcode(status)

    def _forget_pid(self, pid: int) -> None:
        """Drop all tracking state for a pid that is no longer reapable."""
        self._live.discard(pid)
        self._orphaned.discard(pid)
        self._release_harness_pid(pid)

    def _release_harness_pid(self, pid: int) -> None:
        """Drop *pid* from whichever runner's harness-ownership set holds it.

        Ownership must end the moment the pid is fully accounted for, because
        the OS is then free to reuse it. A pid left in a stale owner set would
        be re-orphaned by that runner's :meth:`_drop_runner` after the number
        was recycled for an unrelated harness, discarding the new harness's real
        exit code and hanging its legitimate owner's poll forever.

        :param pid: The harness pid to release.
        """
        for owned in self._runner_harness_pids.values():
            owned.discard(pid)

    def _take_exit_code(self, pid: int) -> int | None:
        """Pop *pid*'s exit code, releasing its harness ownership with it.

        :param pid: The child pid being polled.
        :returns: The exit code if the child has been reaped, else ``None``.
        """
        if pid not in self._exit_codes:
            return None
        code = self._exit_codes.pop(pid)
        self._release_harness_pid(pid)
        return code

    def _on_readable(self, conn: socket.socket, role: str) -> bool:
        """Drain a readable socket, dispatching each newline-delimited request.

        :param conn: The readable socket.
        :param role: ``"daemon"`` or ``"runner"``.
        :returns: ``False`` to stop the whole server (daemon socket closed),
            ``True`` otherwise.
        """
        try:
            chunk = conn.recv(65536)
        except OSError:
            chunk = b""
        if not chunk:
            if role == "daemon":
                return False  # daemon gone -> exit; children reparent + self-die
            self._drop_runner(conn)
            return True
        buf = self._buffers[conn.fileno()]
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            del buf[:]
            buf.extend(rest)
            # Last-resort guard: this is a single-threaded forkserver shared by
            # every session, so an unhandled exception from one malformed
            # request would tear down all runners and harnesses forked from it.
            # Answer with an error and keep serving instead.
            try:
                self._dispatch(conn, bytes(line))
            except Exception as exc:  # noqa: BLE001 — must not kill the server
                _send(conn, {"error": f"request failed: {exc}"})
        return True

    def _dispatch(self, conn: socket.socket, line: bytes) -> None:
        """Handle one request line on *conn*.

        :param conn: The socket the request arrived on (answer on the same one).
        :param line: One newline-stripped JSON request.
        """
        line = line.strip()
        if not line:
            return
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _send(conn, {"error": "malformed request"})
            return
        # json.loads happily returns a list/scalar; .get() would raise on those.
        if not isinstance(request, dict):
            _send(conn, {"error": "request must be a JSON object"})
            return
        cmd = request.get("cmd")
        if cmd == "ping":
            _send(conn, {"pong": True})
        elif cmd == "poll":
            self._reap()
            try:
                pid = int(request.get("pid", 0))
            except (TypeError, ValueError):
                _send(conn, {"error": "poll requires an integer pid"})
                return
            code = self._take_exit_code(pid)
            _send(conn, {"returncode": code})
        elif cmd == "fork":
            self._handle_fork(conn, request)
        elif cmd == "fork_harness":
            self._handle_fork_harness(conn, request)
        else:
            _send(conn, {"error": f"unknown cmd: {cmd!r}"})

    def _close_inherited_in_child(self, keep: socket.socket | None = None) -> None:
        """Close every zygote-side control socket in a freshly forked child.

        A fork inherits the entire zygote fd table — the daemon socket and every
        runner socket. A child must not hold any of them (it would wedge those
        peers' EOF detection and could reply on another connection). *keep* is
        the child's own newly minted runner socket (a runner fork), which is a
        separate fd and is never in the control set anyway.

        :param keep: An fd to leave open (unused by the control set; documented
            for intent), else ``None``.
        """
        for sock in self._control_socks:
            if sock is keep:
                continue
            with contextlib.suppress(OSError):
                sock.close()

    def _refuse_if_upgraded(self, conn: socket.socket, kind: str) -> bool:
        """Answer with an error when the package changed under this zygote.

        A forked child inherits the graph imported at boot but resolves every
        lazily-imported module from disk. Once an in-place upgrade rewrites the
        package, those two disagree — the child either misses a name its old
        in-memory modules gained, or fails to import a module the swapped-out
        directory no longer offers. Both callers' fallbacks re-launch through a
        fresh interpreter, which reads everything from disk coherently.

        :param conn: The socket to answer on.
        :param kind: Child being forked (``"runner"`` / ``"harness"``), named
            in the error so operator logs say which launch fell back.
        :returns: ``True`` when the request was refused (caller must return).
        """
        if self._graph_stamp is None or _disk_build_stamp() == self._graph_stamp:
            return False
        _send(
            conn,
            {
                "error": (
                    "omnigent was upgraded on disk after the zygote imported its "
                    f"graph; refusing to fork a mixed-version {kind}"
                )
            },
        )
        return True

    def _handle_fork(self, conn: socket.socket, request: dict[str, Any]) -> None:
        """Fork a runner, giving it its own control socket back to the zygote.

        :param conn: The daemon socket to answer on.
        :param request: The ``fork`` request (``env`` + optional ``log_path``).
        """
        if self._refuse_if_upgraded(conn, "runner"):
            return
        try:
            z_end, r_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as exc:
            _send(conn, {"error": f"socketpair failed: {exc}"})
            return
        try:
            pid = os.fork()
        except OSError as exc:
            z_end.close()
            r_end.close()
            _send(conn, {"error": f"fork failed: {exc}"})
            return
        if pid == 0:
            # Runner child: keep r_end (its harness channel), close every
            # inherited zygote-side socket including the just-made z_end.
            with contextlib.suppress(OSError):
                z_end.close()
            self._close_inherited_in_child()
            self._exit_child(lambda: _run_child(request, r_end.fileno()))
        # Zygote parent: keep z_end (watch for this runner's harness forks),
        # drop r_end (the child owns it).
        with contextlib.suppress(OSError):
            r_end.close()
        self._live.add(pid)
        self._control_socks.add(z_end)
        self._buffers[z_end.fileno()] = bytearray()
        self._sel.register(z_end, selectors.EVENT_READ, "runner")
        _send(conn, {"pid": pid})

    def _handle_fork_harness(self, conn: socket.socket, request: dict[str, Any]) -> None:
        """Fork a harness child on behalf of the runner that owns *conn*.

        :param conn: The runner socket to answer on.
        :param request: The ``fork_harness`` request (``argv`` + ``env``).
        """
        if self._refuse_if_upgraded(conn, "harness"):
            return
        try:
            pid = os.fork()
        except OSError as exc:
            _send(conn, {"error": f"fork failed: {exc}"})
            return
        if pid == 0:
            # Harness child: close every inherited zygote-side socket (it never
            # speaks the fork protocol; it talks to its runner over its own UDS).
            self._close_inherited_in_child()
            self._exit_child(lambda: _run_harness_child(request))
        self._live.add(pid)
        # Track under the requesting runner so _drop_runner can orphan this
        # harness's exit-code state if that runner disappears before polling.
        self._runner_harness_pids.setdefault(conn.fileno(), set()).add(pid)
        _send(conn, {"pid": pid})

    @staticmethod
    def _exit_child(body: Any) -> None:
        """Run a child body with the shared hard-exit guard. Never returns.

        :param body: Zero-arg callable running the child's real work.
        """
        try:
            body()
        except SystemExit as exc:
            # Preserve the exec'd entrypoint's exit code (main() raises
            # SystemExit) rather than flattening it to a traceback + code 1.
            code = exc.code
            os._exit(code if isinstance(code, int) else (0 if code is None else 1))
        except BaseException:  # noqa: BLE001 — last-resort child guard
            import traceback

            traceback.print_exc()
            os._exit(1)
        os._exit(0)

    def _drop_runner(self, conn: socket.socket) -> None:
        """Forget a runner whose control socket closed (the runner exited).

        Its harness children self-terminate via their own watchdog (which
        probes the runner pid). With the runner gone, nothing will ever poll
        their exit codes, so mark them orphaned: _reap still waitpid's them (no
        zombies) but discards the code instead of leaking it in _exit_codes —
        which would otherwise grow unbounded and risk pid-reuse misattribution.
        Any already-reaped codes for this runner's harnesses are dropped too.

        :param conn: The closed runner socket.
        """
        fileno = conn.fileno()
        with contextlib.suppress(KeyError):
            self._sel.unregister(conn)
        self._control_socks.discard(conn)
        self._buffers.pop(fileno, None)
        for harness_pid in self._runner_harness_pids.pop(fileno, set()):
            self._exit_codes.pop(harness_pid, None)
            if harness_pid in self._live:
                self._orphaned.add(harness_pid)
        with contextlib.suppress(OSError):
            conn.close()


def _send(control_sock: socket.socket, payload: dict[str, Any]) -> None:
    """Send one newline-delimited JSON response, swallowing a dead socket.

    :param control_sock: The control socket.
    :param payload: JSON-serializable response body.
    """
    # Peer went away mid-exchange; the read side will hit EOF and clean up.
    with contextlib.suppress(OSError):
        control_sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")


def main() -> None:
    """Zygote entrypoint: import the graph once, then serve fork requests.

    Reads the control-socket fd from ``OMNIGENT_RUNNER_ZYGOTE_CONTROL_FD``.
    Exits 0 on clean EOF (daemon closed the socket), 2 on a config error.

    :returns: None.
    """
    fd_raw = os.environ.get(ZYGOTE_CONTROL_FD_ENV_VAR)
    if not fd_raw:
        print(
            f"error: {ZYGOTE_CONTROL_FD_ENV_VAR} is required for the runner zygote",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        control_fd = int(fd_raw)
    except ValueError:
        print(f"error: {ZYGOTE_CONTROL_FD_ENV_VAR} must be an integer", file=sys.stderr)
        raise SystemExit(2) from None

    control_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=control_fd)

    # Capture the disk stamp of the graph about to be imported, so harness
    # forks can detect an in-place upgrade landing after this point.
    graph_stamp = _disk_build_stamp()
    _import_runner_graph()
    # The import graph is now static; move it out of GC's tracked set so cyclic
    # collections stay cheap and don't dirty shared pages in forked children.
    gc.freeze()

    # Note: we deliberately do NOT register our own logging-lock fork handler.
    # CPython already re-inits logging locks across os.fork() via its own
    # registered handlers; adding another that blindly releases the lock raises
    # "cannot release un-acquired lock" in the child.

    if threading.active_count() != 1:  # pragma: no cover — defense in depth
        # Forking from a multithreaded process risks child deadlocks. The graph
        # is audited to start no import-time threads; if that ever regresses,
        # fail loud here rather than ship silent deadlocks.
        names = [t.name for t in threading.enumerate()]
        print(
            f"error: runner zygote must be single-threaded before forking; saw {names}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    # Ctrl+C is a normal operator-driven shutdown; exit quietly without a traceback.
    with contextlib.suppress(KeyboardInterrupt):
        _ZygoteServer(control_sock, graph_stamp=graph_stamp).serve()


if __name__ == "__main__":
    main()
