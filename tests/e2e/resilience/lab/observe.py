"""Record what a user would see of one session, independent of the faulted links.

:class:`SessionWatcher` subscribes to the session's live stream and polls its
snapshot directly on the server (never through a proxy). It keeps a timeline
that oracles read after the scenario: every ``session.status`` edge as it was
published, plus the polled status, liveness and pending approvals.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

_POLL_S = 0.5
_STREAM_RETRY_S = 0.5


@dataclass(frozen=True)
class Observation:
    """One recorded moment.

    :param wall: Wall-clock time.
    :param source: ``stream`` for a published event, ``snapshot`` for a poll.
    :param status: Session status, e.g. ``"running"``; ``None`` when unknown.
    :param error_code: Failure code carried by the status, e.g. ``"runner_disconnected"``.
    :param runner_online: Snapshot liveness, ``None`` for stream events.
    :param host_online: Snapshot host liveness, ``None`` for stream events.
    :param pending_approvals: Snapshot count of pending approval prompts.
    :param event_type: Stream event type, e.g. ``"session.status"``.
    """

    wall: float
    source: str
    status: str | None = None
    error_code: str | None = None
    runner_online: bool | None = None
    host_online: bool | None = None
    pending_approvals: int | None = None
    event_type: str | None = None


@dataclass
class SessionWatcher:
    """Background recorder for one session's user-visible state.

    :param server_url: Direct server URL, e.g. ``lab.server_url``.
    :param session_id: Session to watch.
    """

    server_url: str
    session_id: str
    observations: list[Observation] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _threads: list[threading.Thread] = field(default_factory=list)

    def start(self) -> SessionWatcher:
        """Begin streaming and polling; returns ``self``."""
        for target in (self._stream, self._poll):
            thread = threading.Thread(target=target, daemon=True, name=f"watch-{target.__name__}")
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        """Stop recording."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5)

    def __enter__(self) -> SessionWatcher:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def between(self, start: float, end: float) -> list[Observation]:
        """Observations recorded in ``[start, end]`` (wall-clock)."""
        with self._lock:
            return [obs for obs in self.observations if start <= obs.wall <= end]

    def statuses(self, start: float = 0.0, end: float = float("inf")) -> list[Observation]:
        """Published status edges and polled statuses in a window."""
        return [obs for obs in self.between(start, end) if obs.status is not None]

    def describe(self, start: float = 0.0, end: float = float("inf")) -> str:
        """Compact human-readable timeline, collapsing repeated polls."""
        lines: list[str] = []
        last: tuple[object, ...] | None = None
        for obs in self.between(start, end):
            key = (
                obs.source,
                obs.status,
                obs.error_code,
                obs.runner_online,
                obs.host_online,
                obs.pending_approvals,
            )
            if key == last:
                continue
            last = key
            stamp = time.strftime("%H:%M:%S", time.localtime(obs.wall))
            parts = [f"{stamp}.{int(obs.wall * 10) % 10}", obs.source, f"status={obs.status}"]
            if obs.error_code:
                parts.append(f"error={obs.error_code}")
            if obs.source == "snapshot":
                parts.append(f"runner={obs.runner_online} host={obs.host_online}")
                parts.append(f"approvals={obs.pending_approvals}")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def _record(self, observation: Observation) -> None:
        with self._lock:
            self.observations.append(observation)

    def _stream(self) -> None:
        url = f"{self.server_url}/v1/sessions/{self.session_id}/stream"
        timeout = httpx.Timeout(5.0, read=60.0)
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=timeout, trust_env=False) as client:
                    with client.stream("GET", url) as response:
                        for line in response.iter_lines():
                            if self._stop.is_set():
                                return
                            self._consume(line)
            except httpx.HTTPError:
                pass
            self._stop.wait(_STREAM_RETRY_S)

    def _consume(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return
        with contextlib.suppress(json.JSONDecodeError):
            event = json.loads(payload)
            if not isinstance(event, dict) or event.get("type") != "session.status":
                return
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            self._record(
                Observation(
                    wall=time.time(),
                    source="stream",
                    status=event.get("status"),
                    error_code=error.get("code"),
                    event_type="session.status",
                )
            )

    def _poll(self) -> None:
        url = f"{self.server_url}/v1/sessions/{self.session_id}"
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while not self._stop.is_set():
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    response = client.get(url)
                    if response.status_code == 200:
                        self._record(_snapshot_observation(response.json()))
                self._stop.wait(_POLL_S)


def _snapshot_observation(snapshot: dict[str, Any]) -> Observation:
    labels = snapshot.get("labels") if isinstance(snapshot.get("labels"), dict) else {}
    pending = snapshot.get("pending_elicitations")
    return Observation(
        wall=time.time(),
        source="snapshot",
        status=snapshot.get("status"),
        error_code=labels.get("omnigent.last_task_error_code") or None,
        runner_online=snapshot.get("runner_online"),
        host_online=snapshot.get("host_online"),
        pending_approvals=len(pending) if isinstance(pending, list) else None,
    )
