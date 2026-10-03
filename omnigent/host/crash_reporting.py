"""Crash and exit reporting for the host daemon process.

The host daemon runs detached with stdout/stderr redirected to its log file, so
an uncaught traceback or a signal death otherwise never reaches ``logging`` --
and therefore never reaches the debug-log sink. This module routes every exit
path through one structured ``host_exiting`` event and logs crashes that do not
end the process (background threads, asyncio tasks).
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType, TracebackType
from typing import IO, Literal

from omnigent._platform import IS_POSIX
from omnigent.debug_logging import close_debug_log_sink, debug_event
from omnigent.process_logging import process_log_dir

_logger = logging.getLogger(__name__)

HOST_STARTED_EVENT = "host_started"
HOST_EXITING_EVENT = "host_exiting"
HOST_THREAD_CRASHED_EVENT = "host_thread_crashed"
HOST_TASK_ERROR_EVENT = "host_task_error"

HostExitReason = Literal[
    "clean",
    "lifecycle_lost",
    "interrupted",
    "signal",
    "fatal_connect",
    "identity_error",
    "exit",
    "uncaught",
]

# Signals a supervisor or terminal sends to stop the daemon. SIGINT stays with
# asyncio, which turns it into the graceful KeyboardInterrupt path.
_STOP_SIGNAL_NAMES = ("SIGTERM", "SIGHUP")
# Bound on draining the debug sink after a stop signal; ``host stop`` waits 5s.
_SIGNAL_FLUSH_TIMEOUT_S = 2.0
# Hard deadline for the signal exit path, in case logging itself wedges.
_SIGNAL_EXIT_DEADLINE_S = 3.0

FAULTHANDLER_LOG_NAME = "faulthandler.log"


@dataclass
class _HostExitState:
    """Process-wide context stamped onto every host exit event."""

    started_monotonic: float = field(default_factory=time.monotonic)
    daemon_target: str | None = None
    host_id: str | None = None
    exit_reported: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _HostExitState()
_hooks_installed = False
_faulthandler_file: IO[bytes] | None = None
_signal_exit_started = threading.Event()


def install_host_crash_hooks(*, enable_faulthandler: bool = False) -> None:
    """Route uncaught host crashes to logging and start a new exit report.

    Chains to the existing ``sys.excepthook`` / ``threading.excepthook`` (e.g.
    the CLI's friendly crash screen for a foreground host), so terminal output
    is unchanged. Hooks install once per process; each call re-arms the single
    ``host_exiting`` report.

    :param enable_faulthandler: Also dump native crashes to
        ``<logs>/host/faulthandler.log``. The background daemon sets this; a
        foreground host already has the CLI crash handler's faulthandler.
    """
    global _hooks_installed
    with _state.lock:
        _state.exit_reported = False
    if enable_faulthandler:
        _enable_faulthandler()
    if _hooks_installed:
        return
    _hooks_installed = True
    _state.started_monotonic = time.monotonic()

    previous_excepthook = sys.excepthook
    previous_threading_hook = threading.excepthook

    def _excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        with contextlib.suppress(Exception):
            if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                report_host_exit("uncaught", exit_code=1, exc_info=(exc_type, exc, tb))
        previous_excepthook(exc_type, exc, tb)

    def _threading_hook(args: threading.ExceptHookArgs) -> None:
        with contextlib.suppress(Exception):
            if args.exc_value is not None and not issubclass(args.exc_type, SystemExit):
                thread_name = args.thread.name if args.thread is not None else None
                _logger.error(
                    "host background thread %s crashed: %s: %s",
                    thread_name,
                    args.exc_type.__name__,
                    args.exc_value,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                    extra=_event(HOST_THREAD_CRASHED_EVENT, thread=thread_name),
                )
        previous_threading_hook(args)

    sys.excepthook = _excepthook
    threading.excepthook = _threading_hook


def _enable_faulthandler() -> None:
    """Dump native crashes (segfaults) to the host log directory."""
    global _faulthandler_file
    if _faulthandler_file is not None:
        return
    try:
        log_dir = process_log_dir("host")
        log_dir.mkdir(parents=True, exist_ok=True)
        # Held open for the process lifetime: faulthandler writes to the fd
        # from its signal handler, after Python can no longer open files.
        _faulthandler_file = open(log_dir / FAULTHANDLER_LOG_NAME, "ab", buffering=0)  # noqa: SIM115
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
    except Exception:  # noqa: BLE001 — crash capture must never block startup
        _logger.debug("Could not enable faulthandler for the host", exc_info=True)


def set_host_exit_context(*, daemon_target: str | None = None, host_id: str | None = None) -> None:
    """Record identity fields stamped onto later host exit events.

    :param daemon_target: Normalized registry target, e.g. ``"local"``.
    :param host_id: Host identity, e.g. ``"host_abc123"``.
    """
    if daemon_target is not None:
        _state.daemon_target = daemon_target
    if host_id is not None:
        _state.host_id = host_id


def _context_attrs() -> dict[str, object]:
    return {
        "host_id": _state.host_id,
        "daemon_target": _state.daemon_target,
        "pid": os.getpid(),
    }


def _event(name: str, **attributes: object) -> dict[str, object]:
    """Build a debug-event ``extra`` stamped with the host exit context."""
    extra = debug_event(name)
    extra["attributes"] = {**_context_attrs(), **attributes}
    return extra


def _uptime_s() -> float:
    return round(time.monotonic() - _state.started_monotonic, 3)


def log_host_started() -> None:
    """Log the ``host_started`` lifecycle event."""
    _logger.info(
        "host started (pid %d)",
        os.getpid(),
        extra=_event(HOST_STARTED_EVENT),
    )


def report_host_exit(
    reason: HostExitReason,
    *,
    exit_code: int | None = None,
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None] | None = None,
    **attributes: object,
) -> bool:
    """Log the single ``host_exiting`` event for this process.

    :param reason: Why the host is exiting, e.g. ``"fatal_connect"``.
    :param exit_code: Process exit code, when known.
    :param exc_info: The exception ending the process, for ``"uncaught"``.
    :param attributes: Extra event attributes, e.g. ``signal="SIGTERM"``.
    :returns: ``True`` when this call logged the event; ``False`` when an
        earlier exit path already reported it.
    """
    with _state.lock:
        if _state.exit_reported:
            return False
        _state.exit_reported = True
    if reason == "uncaught":
        level = logging.CRITICAL
    elif reason in ("fatal_connect", "identity_error", "exit"):
        level = logging.ERROR
    else:
        level = logging.INFO
    detail = f" by {attributes['signal']}" if "signal" in attributes else ""
    if exc_info is not None:
        detail = f": {exc_info[0].__name__}: {exc_info[1]}"
    _logger.log(
        level,
        "host exiting (%s) after %.1fs%s",
        reason,
        _uptime_s(),
        detail,
        exc_info=exc_info,
        extra=_event(
            HOST_EXITING_EVENT,
            reason=reason,
            exit_code=exit_code,
            uptime_s=_uptime_s(),
            **attributes,
        ),
    )
    return True


def host_asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,  # noqa: ARG001 — signature mandated by asyncio
    context: dict[str, object],
) -> None:
    """Log an exception asyncio could not deliver (e.g. an unawaited task)."""
    exc = context.get("exception")
    message = context.get("message") or "Unhandled asyncio exception"
    task = context.get("task") or context.get("future")
    task_name = task.get_name() if isinstance(task, asyncio.Task) else None
    _logger.error(
        "host asyncio error: %s",
        message,
        exc_info=exc if isinstance(exc, BaseException) else None,
        extra=_event(HOST_TASK_ERROR_EVENT, task=task_name),
    )


def install_host_signal_handlers() -> Callable[[], None]:
    """Log and flush before a stop signal ends the host, then die by that signal.

    The handler keeps today's semantics: no graceful teardown. Runners notice
    the daemon is gone through their parent-death watchdog. After logging, the
    signal's default action is restored and re-raised, so supervisors still see
    a signal death. POSIX main thread only; elsewhere this is a no-op.

    :returns: A callable that restores the previous handlers.
    """
    if not IS_POSIX or threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[signal.Signals, object] = {}
    for name in _STOP_SIGNAL_NAMES:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, _on_stop_signal)
        except (OSError, ValueError):
            continue

    def _restore() -> None:
        for signum, handler in previous.items():
            with contextlib.suppress(OSError, ValueError, TypeError):
                signal.signal(signum, handler)  # type: ignore[arg-type]

    return _restore


def _on_stop_signal(signum: int, frame: FrameType | None) -> None:  # noqa: ARG001
    """Hand the stop off to a thread; signal handlers must stay minimal."""
    if _signal_exit_started.is_set():
        return
    _signal_exit_started.set()
    # Restore the default action now (only the main thread may): the exit
    # thread re-raises the signal, and a second signal kills immediately.
    with contextlib.suppress(OSError, ValueError):
        signal.signal(signum, signal.SIG_DFL)
    threading.Thread(
        target=_exit_on_signal, args=(signum,), name="host-signal-exit", daemon=True
    ).start()


def _exit_on_signal(signum: int) -> None:
    """Report the exit, drain the debug sink, then die by *signum*."""
    exit_code = 128 + signum
    watchdog = threading.Timer(_SIGNAL_EXIT_DEADLINE_S, os._exit, args=(exit_code,))
    watchdog.daemon = True
    watchdog.start()
    try:
        report_host_exit("signal", exit_code=exit_code, signal=signal.Signals(signum).name)
        close_debug_log_sink(timeout_s=_SIGNAL_FLUSH_TIMEOUT_S)
        for handler in logging.getLogger().handlers:
            with contextlib.suppress(Exception):
                handler.flush()
    finally:
        with contextlib.suppress(Exception):
            os.kill(os.getpid(), signum)
            # Delivery is asynchronous; give the default action time to land.
            time.sleep(1.0)
        os._exit(exit_code)
