"""Small opt-in startup profiler for CLI launch paths."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass
class StartupProfiler:
    """
    Emit elapsed startup timings to a text stream.

    The profiler is intentionally lightweight and synchronous so it can
    be used before async runtimes, HTTP clients, or rich terminal
    helpers are initialized.

    :param name: Human-readable launch name, e.g.
        ``"omnigent claude"``.
    :param enabled: Whether calls to :meth:`mark` should print.
    :param clock: Monotonic clock returning seconds, e.g.
        :func:`time.perf_counter`.
    :param stream: Destination stream. ``None`` uses ``sys.stderr`` at
        construction time.
    """

    name: str
    enabled: bool
    clock: Callable[[], float] = time.perf_counter
    stream: TextIO | None = None
    _start: float = field(init=False, repr=False)
    _last: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """
        Initialize timing anchors.

        :returns: None.
        """
        now = self.clock()
        self._start = now
        self._last = now
        if self.stream is None:
            self.stream = sys.stderr

    @classmethod
    def from_env(
        cls,
        *,
        name: str,
        env_var: str,
        explicit: bool = False,
        clock: Callable[[], float] = time.perf_counter,
        stream: TextIO | None = None,
    ) -> StartupProfiler:
        """
        Build a profiler from an explicit flag or environment variable.

        :param name: Human-readable launch name, e.g.
            ``"omnigent claude"``.
        :param env_var: Environment variable that enables profiling,
            e.g. ``"OMNIGENT_CLAUDE_STARTUP_PROFILE"``.
        :param explicit: ``True`` when the caller requested profiling
            through a CLI flag.
        :param clock: Monotonic clock returning seconds.
        :param stream: Destination stream. ``None`` uses ``sys.stderr``.
        :returns: Configured profiler.
        """
        enabled = explicit or _env_enabled(env_var)
        return cls(name=name, enabled=enabled, clock=clock, stream=stream)

    def mark(self, label: str, *, detail: str | None = None) -> None:
        """
        Print one timing mark if profiling is enabled.

        :param label: Short stage label, e.g. ``"backend ready"``.
        :param detail: Optional extra context, e.g.
            ``"server=http://127.0.0.1:8123"``.
        :returns: None.
        """
        if not self.enabled:
            return
        now = self.clock()
        total = now - self._start
        delta = now - self._last
        self._last = now
        suffix = f" - {detail}" if detail else ""
        stream = self.stream or sys.stderr
        print(
            f"[{self.name} startup +{total:.3f}s delta={delta:.3f}s] {label}{suffix}",
            file=stream,
            flush=True,
        )


def _env_enabled(env_var: str) -> bool:
    """
    Return whether *env_var* contains a truthy profiling value.

    :param env_var: Environment variable name, e.g.
        ``"OMNIGENT_CLAUDE_STARTUP_PROFILE"``.
    :returns: ``True`` for ``1``, ``true``, ``yes``, or ``on``
        (case-insensitive); otherwise ``False``.
    """
    value = os.environ.get(env_var)
    if value is None:
        return False
    return value.strip().lower() in _TRUE_VALUES
