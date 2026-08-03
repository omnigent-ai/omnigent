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

Linux-only and opt-in: the daemon gates all of this behind
``OMNIGENT_RUNNER_ZYGOTE=1`` and falls back to direct ``Popen`` if the zygote is
unavailable, so this is never a hard dependency.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import socket
import sys
import threading
from typing import Any

# Env var the daemon sets to the inherited control-socket fd number.
ZYGOTE_CONTROL_FD_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_CONTROL_FD"
# Env var gating zygote use in the daemon (read there, documented here).
ZYGOTE_ENABLED_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE"
# Test-only seam: when present in a fork payload, the child exits with this code
# instead of running the real runner. Never set in production launches.
_ZYGOTE_TEST_CHILD_EXIT_ENV_VAR = "OMNIGENT_RUNNER_ZYGOTE_TEST_CHILD_EXIT"


def _import_runner_graph() -> None:
    """Eagerly import the heavy runner graph so the ~120MB lands once.

    A forked child inherits every module imported here via copy-on-write, so
    this is the whole point of the zygote. Kept in sync with what
    ``omnigent.runner._entry.main`` pulls in on a cold start.
    """
    from omnigent.runner import _entry, app, native  # noqa: F401


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
    # matches open_process_log_file's unbuffered "ab" handle.
    logfd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
    finally:
        if logfd > 2:
            os.close(logfd)


def _run_child(control_sock: socket.socket, request: dict[str, Any]) -> None:
    """Execute the runner in the freshly forked child. Never returns.

    Closes the inherited control socket, wires stdio, applies the request env,
    and hands off to the normal runner entrypoint.

    :param control_sock: The zygote's control socket, closed here so the child
        never speaks the fork protocol.
    :param request: The ``fork`` request: ``env`` mapping + optional ``log_path``.
    """
    with contextlib.suppress(OSError):
        control_sock.close()
    # The child owns a clean env: replace, don't merge, so a stale var from a
    # previous fork's payload can never leak into this session.
    env = request.get("env") or {}
    os.environ.clear()
    os.environ.update({str(k): str(v) for k, v in env.items()})
    # Drop the control-fd hint so nothing downstream mistakes the child for a
    # zygote or tries to reuse the (now closed) fd.
    os.environ.pop(ZYGOTE_CONTROL_FD_ENV_VAR, None)

    _wire_child_stdio(request.get("log_path"))

    # Test seam: exercise fork/reap/poll without booting a real runner. Honored
    # only when set in the fork payload's env (never in production launches).
    # Echoes this child's view of a marker var to stdout (its log) so a test can
    # prove one fork's env never leaks into another's.
    test_exit = os.environ.get(_ZYGOTE_TEST_CHILD_EXIT_ENV_VAR)
    if test_exit is not None:
        sys.stdout.write(f"marker={os.environ.get('OMNIGENT_ZYGOTE_MARKER', '')}\n")
        sys.stdout.flush()
        os._exit(int(test_exit))

    from omnigent.runner._entry import main

    main()


def _serve(control_sock: socket.socket) -> None:
    """Block on the control socket, forking a runner per ``fork`` request.

    Runs until the socket hits EOF (the daemon died / closed it), then returns
    so ``main`` can exit and let the runner children reparent.

    :param control_sock: The ``AF_UNIX`` stream socket shared with the daemon.
    """
    # Remember exit codes of reaped children until the daemon polls for them;
    # the daemon is not these children's parent and cannot waitpid() itself.
    exit_codes: dict[int, int] = {}
    live: set[int] = set()

    def _reap() -> None:
        """Non-blocking reap of any exited children into ``exit_codes``."""
        for pid in list(live):
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                live.discard(pid)
                continue
            if waited == 0:
                continue
            exit_codes[pid] = os.waitstatus_to_exitcode(status)
            live.discard(pid)

    reader = control_sock.makefile("rb")
    try:
        for raw in reader:
            _reap()
            line = raw.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                _send(control_sock, {"error": "malformed request"})
                continue

            cmd = request.get("cmd")
            if cmd == "ping":
                _send(control_sock, {"pong": True})
            elif cmd == "poll":
                pid = int(request.get("pid", 0))
                _reap()
                if pid in exit_codes:
                    _send(control_sock, {"returncode": exit_codes.pop(pid)})
                else:
                    _send(control_sock, {"returncode": None})
            elif cmd == "fork":
                _handle_fork(control_sock, request, live)
            else:
                _send(control_sock, {"error": f"unknown cmd: {cmd!r}"})
    finally:
        reader.close()


def _handle_fork(control_sock: socket.socket, request: dict[str, Any], live: set[int]) -> None:
    """Fork one runner child and report its pid (or the fork error) back.

    :param control_sock: The control socket to answer on (parent side).
    :param request: The ``fork`` request.
    :param live: Set of live child pids to extend on success.
    """
    try:
        pid = os.fork()
    except OSError as exc:
        _send(control_sock, {"error": f"fork failed: {exc}"})
        return
    if pid == 0:
        # Child path. Any failure here must hard-exit, never unwind back into
        # the accept loop (that would fork-bomb the zygote).
        try:
            _run_child(control_sock, request)
        except BaseException:  # noqa: BLE001 — last-resort child guard
            import traceback

            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    live.add(pid)
    _send(control_sock, {"pid": pid})


def _send(control_sock: socket.socket, payload: dict[str, Any]) -> None:
    """Send one newline-delimited JSON response, swallowing a dead socket.

    :param control_sock: The control socket.
    :param payload: JSON-serializable response body.
    """
    # Daemon went away mid-exchange; the read side will hit EOF and stop.
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

    _serve(control_sock)


if __name__ == "__main__":
    main()
