"""
Generic ``python -m`` entrypoint for harness subprocesses.

Invoked by :class:`omnigent.runtime.harnesses.process_manager.HarnessProcessManager`
with four required arguments (plus one optional):

- ``--harness <name>``: human-readable harness name, e.g.
  ``"claude-sdk"``. Stashed on ``app.state.harness`` for
  introspection / logging; not used for module resolution.
- ``--module <python_path>``: importable module that exports
  ``create_app() -> FastAPI``. The parent looks this up from
  :data:`omnigent.runtime.harnesses._HARNESS_MODULES` before
  spawning, so the registry stays the single source of truth in
  the parent process (necessary because subprocesses don't
  inherit registry mutations from their parent's runtime).
- ``--socket <path>``: absolute Unix socket path the process binds.
  AP's per-conversation HTTP client points at the same path via
  :class:`httpx.AsyncHTTPTransport(uds=...)`.
- ``--conversation-id <id>``: the conversation this process serves.
  Stashed on ``app.state.conversation_id`` so the harness can scope
  its in-memory state per §Harness in-memory state in the design
  doc. Omnigent allocates the id; the runner does NOT parse it from the
  socket path (the socket layout is a process-manager
  implementation detail, not a stable contract).
- ``--parent-pid <pid>`` (optional): PID of the spawning process.
  When supplied, a daemon thread checks that this process is still
  parented by that PID and polls ``os.kill(pid, 0)`` every second.
  It sends ``SIGTERM`` to this process when the parent disappears
  or the OS reparents the runner.

The runner is intentionally minimal — import, factory call, state
stash, uvicorn launch. All harness-specific behavior lives in the
per-harness ``create_app()`` factory.

See ``designs/SERVER_HARNESS_CONTRACT.md`` §Required harness
package shape and §Process management.
"""

from __future__ import annotations

import argparse
import importlib
import os
import signal
import sys
import threading
import time
from types import FrameType

import uvicorn
from fastapi import FastAPI

# uvicorn log level for harness subprocesses. ``"warning"`` keeps
# the per-process noise low (AP and the harness wrap both emit
# their own structured logs); set to ``"info"`` if you need to
# debug the request/response flow at the HTTP layer.
_UVICORN_LOG_LEVEL = "warning"

# Hard ceiling on uvicorn's graceful-shutdown phase. After SIGTERM,
# uvicorn waits at most this many seconds for active connections
# (SSE streams) to close before forcing exit. Without this, a
# stuck streaming response blocks the process forever — the root
# cause of SIGTERM-resistant orphaned runners.
_GRACEFUL_SHUTDOWN_TIMEOUT_S = float(os.environ.get("OMNIGENT_HARNESS_SHUTDOWN_TIMEOUT_S", "5"))

# Interval between parent-PID liveness probes in the watchdog
# thread. 1 s is responsive enough (parent crash → runner exit
# within ~1 s) without measurably loading the kernel's
# ``kill(pid, 0)`` path.
_PARENT_POLL_INTERVAL_S = 1.0

# Extra safety net for shutdown paths that hang outside uvicorn's
# request-drain timeout (for example, a wedged lifespan hook). Once
# the runner receives SIGTERM/SIGINT or decides it should exit because
# its parent disappeared, it hard-exits if the process is still alive
# after this deadline.
_HARD_EXIT_TIMEOUT_S = float(
    os.environ.get(
        "OMNIGENT_HARNESS_HARD_EXIT_TIMEOUT_S",
        str(_GRACEFUL_SHUTDOWN_TIMEOUT_S + 2.0),
    )
)

_HARD_EXIT_LOCK = threading.Lock()
_HARD_EXIT_ARMED = False


def _set_pdeathsig() -> None:
    """Ask the kernel to auto-kill this process when the parent dies.

    Linux-only. Uses ``prctl(PR_SET_PDEATHSIG, SIGKILL)`` so the
    signal is delivered even if the process is stuck in a blocking
    syscall or has a wedged SIGTERM handler. Complements the
    polling watchdog thread: ``prctl`` is instant and zero-cost
    but Linux-only; the watchdog covers macOS and other platforms.
    No-op on non-Linux or if ``prctl`` is unavailable.

    :returns: None.
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _load_harness_app(harness: str, module_path: str, conversation_id: str) -> FastAPI:
    """
    Import the harness module and instantiate its app.

    :param harness: Human-readable harness name, e.g.
        ``"claude-sdk"``. Stashed on ``app.state.harness`` for
        introspection.
    :param module_path: Fully-qualified Python module to import,
        e.g. ``"omnigent.inner.claude_sdk_harness"``. Must
        export ``create_app() -> FastAPI``.
    :param conversation_id: AP-allocated conversation identifier
        for the subprocess to scope its in-memory state by, e.g.
        ``"conv_abc123"``. Stashed on ``app.state.conversation_id``.
    :returns: The harness's :class:`FastAPI` app, ready to serve.
    :raises SystemExit: If the module fails to import or doesn't
        export ``create_app``. Both are operator-fixable
        misconfigurations; fail loud with a non-zero exit so the
        parent process surfaces the error immediately rather than
        waiting for a connection failure.
    """
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        # Fail loud at boot rather than as a connection refused
        # on the first request.
        print(
            f"runner: cannot import harness module {module_path!r}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    create_app = getattr(module, "create_app", None)
    if create_app is None:
        print(
            f"runner: harness module {module_path!r} does not export create_app() -> FastAPI",
            file=sys.stderr,
        )
        raise SystemExit(2)

    app: FastAPI = create_app()
    # Stash on app.state so individual route handlers can read the
    # conversation id without re-parsing CLI args. Layer 1 / 2 / 3
    # state containers (per §Harness in-memory state) key off this.
    app.state.conversation_id = conversation_id
    app.state.harness = harness
    return app


def _arm_hard_exit(reason: str, sig: int = signal.SIGTERM) -> None:
    """
    Start an idempotent hard-exit timer for wedged shutdown.

    Uvicorn bounds active request drain via
    ``timeout_graceful_shutdown``, but it still awaits ASGI lifespan
    shutdown without a separate timeout. A wedged harness
    ``on_shutdown`` hook or inner executor close could therefore keep
    a SIGTERM'ed runner alive forever. This timer is the final
    backstop for the orphaned-runner failure mode: after the normal graceful path has had a
    short chance to finish, force the process down.

    :param reason: Human-readable reason for diagnostics.
    :param sig: Signal that initiated shutdown; used for conventional
        shell-style exit status.
    """
    global _HARD_EXIT_ARMED
    with _HARD_EXIT_LOCK:
        if _HARD_EXIT_ARMED:
            return
        _HARD_EXIT_ARMED = True

    def _hard_exit() -> None:
        time.sleep(_HARD_EXIT_TIMEOUT_S)
        print(
            f"harness runner did not exit after {reason}; forcing exit",
            file=sys.stderr,
            flush=True,
        )
        os._exit(128 + int(sig))

    threading.Thread(target=_hard_exit, name="harness-hard-exit", daemon=True).start()


def _request_shutdown_with_hard_exit(reason: str) -> None:
    """
    Ask uvicorn to shut down, then force-exit if cleanup wedges.

    ``SIGTERM`` lets uvicorn run its normal graceful shutdown path.
    The timer is deliberately process-local and uses ``os._exit`` as
    a last resort because the failure mode is exactly a
    runner that remains alive after graceful shutdown stalls.

    :param reason: Human-readable reason for diagnostics.
    """
    _arm_hard_exit(reason, signal.SIGTERM)
    os.kill(os.getpid(), signal.SIGTERM)


def _start_parent_watchdog(parent_pid: int) -> threading.Thread:
    """
    Spawn a daemon thread that requests shutdown when *parent_pid* exits.

    Every :data:`_PARENT_POLL_INTERVAL_S` seconds, the thread first
    checks ``os.getppid()`` so OS reparenting is detected even if the
    original PID is still present as a zombie or has been reused. It
    also probes ``os.kill(parent_pid, 0)`` for platforms/situations
    where reparenting is not yet visible. On parent loss, it sends
    this process ``SIGTERM`` to trigger uvicorn's graceful-shutdown
    path and arms a hard-exit timer in case cleanup wedges.

    :param parent_pid: OS process id of the spawning parent,
        e.g. ``12345``.
    :returns: The started daemon thread (returned so tests can
        join it).
    """

    def _watch() -> None:
        while True:
            time.sleep(_PARENT_POLL_INTERVAL_S)
            if os.getppid() != parent_pid:
                _request_shutdown_with_hard_exit("parent process exit")
                return
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                _request_shutdown_with_hard_exit("parent process exit")
                return
            except PermissionError:
                # Process exists but we can't signal it — treat
                # as alive (same semantics as ``_pid_alive`` in
                # ``process_manager.py``).
                pass

    t = threading.Thread(target=_watch, name="harness-parent-watchdog", daemon=True)
    t.start()
    return t


class _HardExitServer(uvicorn.Server):
    """Uvicorn server that arms a hard-exit backstop on signals."""

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Handle SIGTERM/SIGINT and ensure shutdown cannot hang forever."""
        _arm_hard_exit(f"signal {sig}", sig)
        super().handle_exit(sig, frame)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse the runner's required CLI arguments.

    :param argv: Argument list (without the program name),
        e.g. ``["--harness", "claude-sdk", "--module",
        "omnigent.inner.claude_sdk_harness", "--socket",
        "/tmp/omnigent/<id>/conv-abc.sock", "--conversation-id",
        "conv_abc123", "--parent-pid", "12345"]``.
    :returns: Parsed namespace with ``harness``, ``module``,
        ``socket``, ``conversation_id``, and ``parent_pid``
        attributes.
    """
    parser = argparse.ArgumentParser(
        prog="python -m omnigent.runtime.harnesses._runner",
        description=(
            "Per-conversation harness subprocess entrypoint. "
            "Imports the given module, calls create_app(), "
            "and serves it over a Unix socket."
        ),
    )
    parser.add_argument(
        "--harness",
        required=True,
        help="Human-readable harness name (e.g. 'claude-sdk').",
    )
    parser.add_argument(
        "--module",
        required=True,
        help="Fully-qualified Python module exporting create_app().",
    )
    parser.add_argument(
        "--socket",
        required=True,
        help="Absolute Unix socket path to bind.",
    )
    parser.add_argument(
        "--conversation-id",
        required=True,
        help="AP-allocated conversation id (e.g. 'conv_abc123').",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help=(
            "PID of the spawning parent process. When set, a "
            "daemon thread monitors liveness and self-SIGTERMs "
            "on parent exit."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """
    Runner entrypoint.

    :param argv: Override for ``sys.argv[1:]``; defaults to
        the live process arguments. Tests pass an explicit list.
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    # Initialize agent-plane telemetry in the harness subprocess. The server
    # (cli.py / app.py) and runner (runner/_entry.py) each call telemetry.init()
    # in their own process, but the claude-sdk executor's tool-call gate — which
    # emits the TOOL span carrying the policy decision (gen_ai.tool.name +
    # gen_ai.agent.name) — runs HERE, inside the
    # per-conversation harness subprocess. Without this call mlflow tracing is
    # never enabled in this process, so mlflow.start_span() is a no-op and those
    # spans are silently dropped (see _start_tool_call_span in
    # omnigent.inner.claude_sdk_executor). OTEL_*/MLFLOW_* env vars are inherited
    # from the runner via _build_harness_spawn_env, so init() wires the OTLP
    # exporter just as it does in the parent. Best-effort: never block harness
    # startup on telemetry.
    try:
        from omnigent.runtime import telemetry

        telemetry.init()
    except Exception:  # pragma: no cover - defensive; init() is already guarded
        pass
    app = _load_harness_app(args.harness, args.module, args.conversation_id)
    if args.parent_pid is not None:
        _set_pdeathsig()
        _start_parent_watchdog(args.parent_pid)
    # ``uds=`` binds to the Unix socket path Omnigent allocated for this
    # conversation. ``log_level`` keeps per-process noise low — see
    # ``_UVICORN_LOG_LEVEL`` constant.
    # ``timeout_graceful_shutdown`` bounds how long uvicorn waits
    # for active connections after SIGTERM before force-exiting.
    config = uvicorn.Config(
        app,
        uds=args.socket,
        log_level=_UVICORN_LOG_LEVEL,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )
    _HardExitServer(config).run()


if __name__ == "__main__":
    main()
