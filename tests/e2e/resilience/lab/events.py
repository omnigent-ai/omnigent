"""Append-only event log shared by every lab component."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LabEvent:
    """One recorded lab event.

    :param wall: Wall-clock seconds since the epoch, e.g. ``1760000000.12``.
    :param mono: Monotonic seconds, comparable within one lab run.
    :param source: Emitting component, e.g. ``"proxy:host"`` or ``"lab"``.
    :param kind: Event name, e.g. ``"connect"`` or ``"fault_start"``.
    :param fields: Event-specific attributes, e.g. ``{"tag": "runner.tunnel"}``.
    """

    wall: float
    mono: float
    source: str
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)


class EventLog:
    """Thread-safe JSONL log that also keeps events in memory for assertions.

    :param path: JSONL file to append to, or ``None`` to keep events in memory only.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._events: list[LabEvent] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path | None:
        """The JSONL file events are appended to, if any."""
        return self._path

    def emit(self, source: str, kind: str, **fields: Any) -> LabEvent:
        """Record one event.

        :param source: Emitting component, e.g. ``"proxy:client"``.
        :param kind: Event name, e.g. ``"reset"``.
        :param fields: JSON-serializable attributes.
        :returns: The recorded event.
        """
        event = LabEvent(time.time(), time.monotonic(), source, kind, dict(fields))
        line = json.dumps(
            {"wall": event.wall, "mono": event.mono, "source": source, "kind": kind, **fields},
            default=str,
        )
        with self._lock:
            self._events.append(event)
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        return event

    def events(self, *, source: str | None = None, kind: str | None = None) -> list[LabEvent]:
        """Return recorded events, optionally filtered.

        :param source: Exact source to keep, e.g. ``"proxy:host"``.
        :param kind: Exact kind to keep, e.g. ``"connect"``.
        :returns: Matching events in emission order.
        """
        with self._lock:
            snapshot = list(self._events)
        return [
            event
            for event in snapshot
            if (source is None or event.source == source) and (kind is None or event.kind == kind)
        ]
