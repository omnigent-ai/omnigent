"""Correlated, content-free native CLI startup milestones.

Elapsed times use the client process's monotonic clock. Resource availability
and subprocess creation are not evidence of terminal rendering or model output.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, ParamSpec, TypeVar

STARTUP_CONTEXT_ENV = "OMNIGENT_STARTUP_CONTEXT"
_logger = logging.getLogger("omnigent.startup")
_P = ParamSpec("_P")
_R = TypeVar("_R")
StartupEvent = Literal[
    "launch_started",
    "session_resolved",
    "runner_requested",
    "runner_connected",
    "session_runner_bound",
    "terminal_available",
    "initial_prompt_submitted",
    "terminal_attach_started",
    "terminal_attach_exited",
    "launch_failed",
    "launch_cancelled",
    "launch_incomplete",
]


@dataclass(frozen=True)
class _Entry:
    monotonic_ns: int
    started_at_unix_ms: float
    attempt_id: str
    elapsed_offset_ms: float = 0.0
    start_boundary: str = "omnigent_cli_entry"


def _capture_entry() -> _Entry:
    now = time.monotonic_ns()
    wall_ms = time.time_ns() / 1_000_000
    raw = os.environ.pop(STARTUP_CONTEXT_ENV, None)
    if raw and len(raw) <= 2048:
        try:
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or type(data.get("version")) is not int
                or data["version"] != 1
            ):
                raise ValueError("unsupported context")
            # Only an exec handoff in this process can share this clock.
            if type(data.get("pid")) is not int or data["pid"] != os.getpid():
                raise ValueError("not a same-process handoff")
            attempt_id = str(uuid.UUID(data["attempt_id"]))
            offset = data["elapsed_ms"]
            started = data["started_at_unix_ms"]
            handoff = data["handoff_monotonic_ns"]
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in (offset, started)):
                raise ValueError("invalid timestamp")
            if offset < 0 or started <= 0 or type(handoff) is not int or not 0 < handoff <= now:
                raise ValueError("invalid elapsed time")
            return _Entry(
                now,
                started,
                attempt_id,
                offset + (now - handoff) / 1_000_000,
                "wrapper_entry",
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
            pass
    return _Entry(now, wall_ms, str(uuid.uuid4()))


_entry: ContextVar[_Entry | None] = ContextVar("startup_entry", default=None)


def capture_cli_entry(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Capture main entry before setup, without logging non-launch commands."""

    @functools.wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        token = _entry.set(_capture_entry())
        try:
            return function(*args, **kwargs)
        finally:
            _entry.reset(token)

    return wrapped


@dataclass
class _Attempt:
    entry: _Entry
    launch_kind: Literal["create", "resume"]
    session_id: str | None = None
    events: set[str] = field(default_factory=set)
    attached: bool = False

    def record(
        self,
        event: StartupEvent,
        *,
        session_id: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        if (self.attached and event != "terminal_attach_exited") or event in self.events:
            return
        if session_id is not None:
            self.session_id = session_id
        self.events.add(event)
        if event == "terminal_attach_started":
            self.attached = True
        attributes: dict[str, object] = {
            "schema_version": 1,
            "event": event,
            "attempt_id": self.entry.attempt_id,
            "harness": "codex-native",
            "launch_kind": self.launch_kind,
            "start_boundary": self.entry.start_boundary,
            "started_at_unix_ms": self.entry.started_at_unix_ms,
            "elapsed_ms": round(
                self.entry.elapsed_offset_ms
                + (time.monotonic_ns() - self.entry.monotonic_ns) / 1_000_000,
                3,
            ),
        }
        if exit_code is not None:
            attributes["exit_code"] = exit_code
        # This matches debug_logging's structured event seam without importing
        # its exporter on the launch path. Keep JSON for local diagnostics too.
        with contextlib.suppress(Exception):
            _logger.info(
                "client_startup %s",
                json.dumps({**attributes, "session_id": self.session_id}, separators=(",", ":")),
                extra={
                    "event_name": "client_startup",
                    "session_id": self.session_id,
                    "attributes": attributes,
                },
            )


_attempt: ContextVar[_Attempt | None] = ContextVar("startup_attempt", default=None)


def record_startup_event(
    event: StartupEvent, *, session_id: str | None = None, exit_code: int | None = None
) -> None:
    """Record a milestone only inside an instrumented CLI launch."""
    attempt = _attempt.get()
    if attempt is not None:
        attempt.record(event, session_id=session_id, exit_code=exit_code)


@contextlib.contextmanager
def codex_startup_attempt(
    *, launch_kind: Literal["create", "resume"] = "create"
) -> Iterator[None]:
    """Track one launch through attachment, retaining unsuccessful attempts."""
    attempt = _Attempt(_entry.get() or _capture_entry(), launch_kind)
    token = _attempt.set(attempt)
    # Fleet telemetry must survive a quieter CLI. Local file/stderr handlers
    # still apply their own configured level.
    _logger.setLevel(logging.INFO)
    # The general CLI diagnostics logger is local-only. Attach the existing
    # optional uploader to this content-free logger, before the first event.
    with contextlib.suppress(Exception):
        from omnigent.debug_logging import attach_debug_log_sink

        attach_debug_log_sink([_logger], source="cli", level=logging.INFO)
    attempt.record("launch_started")
    try:
        yield
    except BaseException as exc:
        cancelled = isinstance(exc, KeyboardInterrupt) or type(exc).__name__ in {
            "Abort",
            "CancelledError",
        }
        if isinstance(exc, SystemExit) and exc.code in (None, 0):
            cancelled = True
        attempt.record("launch_cancelled" if cancelled else "launch_failed")
        raise
    else:
        if not attempt.attached:
            attempt.record("launch_incomplete")
    finally:
        _attempt.reset(token)


def observe_codex_startup(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Instrument the command callback, including validation and backend setup."""

    @functools.wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        launch_kind = (
            "resume"
            if kwargs.get("resume") is not None or kwargs.get("session_id") is not None
            else "create"
        )
        with codex_startup_attempt(launch_kind=launch_kind):
            return function(*args, **kwargs)

    return wrapped
