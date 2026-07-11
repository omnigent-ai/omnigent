"""Structured progress events and sinks for bench runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tests.harness_bench.verdict import Verdict


@dataclass(frozen=True)
class HarnessStarted:
    """A harness began provisioning its transport."""

    harness: str
    transport: str
    model: str


@dataclass(frozen=True)
class HarnessSkipped:
    """A harness was skipped whole (unavailable, or provisioning failed).

    ``transport`` is the resolved transport the skip applies to (``None`` when
    it could not be resolved), so a live row can still be labelled with it.
    """

    harness: str
    reason: str
    transport: str | None = None


@dataclass(frozen=True)
class ProbeStarted:
    """A probe began running against a harness."""

    harness: str
    probe: str
    title: str


@dataclass(frozen=True)
class ProbeFinished:
    """A probe produced a verdict (or was skipped as a prerequisite casualty)."""

    harness: str
    probe: str
    title: str
    verdict: Verdict
    note: str = ""


@dataclass(frozen=True)
class HarnessFinished:
    """A harness completed all its probes."""

    harness: str


BenchEvent = HarnessStarted | HarnessSkipped | ProbeStarted | ProbeFinished | HarnessFinished


@runtime_checkable
class ProgressSink(Protocol):
    """Consumes :data:`BenchEvent`\\ s as a bench run emits them.

    ``emit`` is called from the orchestrator (possibly from several concurrent
    harness tasks under ``--jobs`` > 1), so a sink that mutates shared state
    must tolerate interleaved calls. The built-in sinks are called on one
    event loop thread, so no locking is needed there.
    """

    def emit(self, event: BenchEvent) -> None:
        """Handle one event."""

    def close(self) -> None:
        """Finalize (flush a live display, etc.). Called once at run end."""


class LineSink:
    """A :class:`ProgressSink` that writes the plain per-probe status lines.

    This is the default, TTY-agnostic renderer — the same output the bench
    emitted before structured events existed, so a piped or CI run is
    unchanged. ``write`` defaults to stderr (the report goes to stdout).
    """

    # Per-line progress does not paint the grid, so the stdout report still
    # prints it in full (see the rich sink's ``drew_grid = True``).
    drew_grid = False

    def __init__(self, write) -> None:  # write: Callable[[str], None]
        self._write = write

    def emit(self, event: BenchEvent) -> None:
        if isinstance(event, HarnessStarted):
            self._write(
                f"[{event.harness}] provisioning {event.transport} transport "
                f"(model={event.model}); first turn may take ~10-30s..."
            )
        elif isinstance(event, HarnessSkipped):
            self._write(f"[{event.harness}] skipped: {event.reason}")
        elif isinstance(event, ProbeStarted):
            self._write(f"[{event.harness}]   {event.title}: running...")
        elif isinstance(event, ProbeFinished):
            suffix = f" ({event.note})" if event.note else ""
            self._write(f"[{event.harness}]   {event.title}: {event.verdict.name}{suffix}")
        # HarnessFinished is silent in line mode (the per-probe lines suffice).

    def close(self) -> None:
        pass


__all__ = [
    "BenchEvent",
    "HarnessFinished",
    "HarnessSkipped",
    "HarnessStarted",
    "LineSink",
    "ProbeFinished",
    "ProbeStarted",
    "ProgressSink",
]
