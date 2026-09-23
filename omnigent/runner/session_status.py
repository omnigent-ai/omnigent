"""The runner's single record of each session's working status.

Every status edge the runner observes lands here exactly once, at observation
time, tagged with the channel it came from:

* ``RUNNER`` — a ``session.status`` the runner put on its own SSE queue.
* ``PTY`` — the pane watcher's activity/quiescence edge.
* ``STATUS_FILE`` — Claude's own ``sessions/<pid>.json`` status.
* ``RELAY`` — a forwarder edge the server relayed as ``external_session_status``.
* ``CONTROL`` — an accepted native interrupt/stop whose idle no other channel
  reports.
* ``RECONCILE`` — a probe or server answer that refuted a stale ``running``.

This replaces a push-only cache of whatever the runner last published, which
missed every relayed edge and every edge the wire dedup swallowed, and so could
pin a finished pane on ``running`` forever.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from omnigent.debug_logging import debug_event

_logger = logging.getLogger(__name__)

# ``reset`` without a mark: drop the session's status unconditionally.
_UNMARKED = object()


class StatusSource(StrEnum):
    """The channel a status edge arrived on."""

    RUNNER = "runner"
    PTY = "pty"
    STATUS_FILE = "status_file"
    RELAY = "relay"
    CONTROL = "control"
    RECONCILE = "reconcile"


#: Channels this runner observed itself. A ``running`` asserted only by a relay
#: can arrive late or be re-posted forever, so on its own it is not a claim.
LOCAL_SOURCES: frozenset[StatusSource] = frozenset(
    {
        StatusSource.RUNNER,
        StatusSource.PTY,
        StatusSource.STATUS_FILE,
        StatusSource.CONTROL,
        StatusSource.RECONCILE,
    }
)


@dataclass(frozen=True)
class StatusRecord:
    """One status episode for a session.

    :param status: ``running``, ``waiting``, ``idle`` or ``failed`` (other
        values are recorded verbatim).
    :param blocked_on: Why a running session is parked on a dialog, e.g.
        ``"permission prompt"``, or ``None``.
    :param since: Monotonic time this status value was first recorded.
    :param since_wall: Wall-clock twin of *since*, for logs.
    :param last_seen: Monotonic time any channel last asserted this value.
    :param sources: Every channel that asserted this value in this episode.
    :param origin: The channel that opened the episode.
    :param blocked_since: Monotonic time *blocked_on* was set, or ``None``.
    :param blocked_source: The channel that set *blocked_on*.
    :param response_id: Last response id a channel attached (diagnostic only).
    :param seq: Book-global monotonic sequence number of the last change.
    """

    status: str
    blocked_on: str | None
    since: float
    since_wall: float
    last_seen: float
    sources: frozenset[StatusSource]
    origin: StatusSource
    blocked_since: float | None
    blocked_source: StatusSource | None
    response_id: str | None
    seq: int


class _StatusView(Mapping[str, str]):
    """Live, read-only ``{session_id: status}`` view of a book."""

    def __init__(self, book: SessionStatusBook) -> None:
        self._book = book

    def __getitem__(self, session_id: str) -> str:
        record = self._book.current(session_id)
        if record is None:
            raise KeyError(session_id)
        return record.status

    def __iter__(self) -> Iterator[str]:
        return iter(self._book.session_ids())

    def __len__(self) -> int:
        return len(self._book.session_ids())


class SessionStatusBook:
    """Thread-safe store of each session's status, written by one recorder.

    Contract:

    * It is the only copy. Callers record through the audited recorders and
      read through the reader methods; nothing keeps its own status map, since
      a copy that misses one channel stays stale on ``running`` and pins
      whatever trusts it. The ``session-status-single-source`` custom-lint rule
      and ``tests/runner/test_session_status_single_recorder.py`` enforce this.
    * R0 — record at observation time, never again on a publish hop. The
      registry's publisher runs on the event loop after the watcher thread
      already recorded the edge; re-recording there could put a queued
      ``running`` back over a relayed ``idle`` that landed in between.
    * A duplicate of the current status only bumps ``last_seen`` and adds its
      source. It never moves ``since``, so a periodic re-assert cannot refresh
      an episode.
    * A change of status starts a new episode with ``sources={source}``.
    * A ``blocked_on`` change keeps ``since`` and restamps ``blocked_since``.
      A duplicate without a reason clears a reason only when it comes from the
      channel that set it.
    * :meth:`current` is the last edge from any channel. It is not liveness:
      only the pane reaper's assessment decides what a ``running`` means. The
      one exception is the runner idle watchdog's native-turn hold: a recorded
      ``running`` or ``waiting`` holds it until a ceiling after the last
      first-hand evidence of work, and a reported dialog (``blocked_on``) or an
      open prompt park until ``OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S`` after it
      opened.
    * What the server has heard (the wire dedup baseline) is deliberately not
      stored here.

    :param clock: Monotonic clock, e.g. ``time.monotonic``.
    :param wall_clock: Wall clock, e.g. ``time.time``.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._records: dict[str, StatusRecord] = {}
        self._dispatch_at: dict[str, float] = {}
        self._control_idle_at: dict[str, float] = {}
        self._seq = 0

    # ── writers ──────────────────────────────────────────────────────────

    def record(
        self,
        session_id: str,
        status: str,
        *,
        source: StatusSource,
        blocked_on: str | None = None,
        response_id: str | None = None,
    ) -> bool:
        """Record one observed status edge.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param status: Observed status, e.g. ``"running"`` or ``"idle"``.
        :param source: Channel the edge arrived on.
        :param blocked_on: Dialog reason carried by the edge, if any.
        :param response_id: Response id carried by the edge (diagnostics only).
        :returns: ``True`` when the status or its blocked reason changed.
        """
        blocked_on = blocked_on or None
        with self._lock:
            changed = self._record_locked(session_id, status, source, blocked_on, response_id)
        self._log_edge(session_id, status, source, changed, blocked_on)
        return changed

    def _record_locked(
        self,
        session_id: str,
        status: str,
        source: StatusSource,
        blocked_on: str | None,
        response_id: str | None,
    ) -> bool:
        # Caller holds the lock.
        now = self._clock()
        previous = self._records.get(session_id)
        if previous is None or previous.status != status:
            self._seq += 1
            record = StatusRecord(
                status=status,
                blocked_on=blocked_on,
                since=now,
                since_wall=self._wall_clock(),
                last_seen=now,
                sources=frozenset({source}),
                origin=source,
                blocked_since=now if blocked_on else None,
                blocked_source=source if blocked_on else None,
                response_id=response_id,
                seq=self._seq,
            )
            changed = True
        else:
            record, changed = self._merge_duplicate(previous, now, source, blocked_on)
            if response_id is not None:
                record = replace(record, response_id=response_id)
        self._records[session_id] = record
        self._note_projections(session_id, status, source, now)
        return changed

    def _log_edge(
        self,
        session_id: str,
        status: str,
        source: StatusSource,
        changed: bool,
        blocked_on: str | None,
    ) -> None:
        _logger.debug(
            "session status edge: session=%s source=%s status=%s changed=%s",
            session_id,
            source.value,
            status,
            changed,
            extra=debug_event(
                "session_status_edge",
                session_id=session_id,
                source=source.value,
                status=status,
                changed=changed,
                blocked_on=blocked_on,
            ),
        )

    def _merge_duplicate(
        self,
        previous: StatusRecord,
        now: float,
        source: StatusSource,
        blocked_on: str | None,
    ) -> tuple[StatusRecord, bool]:
        blocked_since = previous.blocked_since
        blocked_source = previous.blocked_source
        new_blocked = previous.blocked_on
        if blocked_on is not None and blocked_on != previous.blocked_on:
            new_blocked, blocked_since, blocked_source = blocked_on, now, source
        elif blocked_on is None and previous.blocked_on is not None:
            if source == previous.blocked_source:
                new_blocked, blocked_since, blocked_source = None, None, None
        changed = new_blocked != previous.blocked_on
        seq = previous.seq
        if changed:
            self._seq += 1
            seq = self._seq
        record = replace(
            previous,
            blocked_on=new_blocked,
            blocked_since=blocked_since,
            blocked_source=blocked_source,
            last_seen=now,
            sources=previous.sources | {source},
            seq=seq,
        )
        return record, changed

    def _note_projections(
        self, session_id: str, status: str, source: StatusSource, now: float
    ) -> None:
        # Caller holds the lock.
        if source == StatusSource.RUNNER and status == "running":
            self._dispatch_at[session_id] = now
        if source == StatusSource.CONTROL and status == "idle":
            self._control_idle_at[session_id] = now

    def reset(self, session_id: str, reason: str, *, mark: object = _UNMARKED) -> None:
        """Drop a session's status after its pane was torn down.

        Keeps the dispatch and interrupt stamps: they order evidence that
        outlives the pane, such as a hook log on disk that still shows an
        interrupted prompt as open.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param reason: Why, for the log, e.g. ``"native_terminal_closed"``.
        :param mark: From :meth:`edge_mark`, taken before the teardown awaited
            anything. When given, nothing is dropped if an edge was recorded
            since: it belongs to whatever started meanwhile (a new turn while
            the old pane closed), not to the torn-down pane.
        """
        dropped: StatusRecord | None = None
        with self._lock:
            spared = mark is not _UNMARKED and self._records.get(session_id) is not mark
            if not spared:
                dropped = self._records.pop(session_id, None)
        if spared:
            _logger.debug(
                "session status reset skipped: session=%s reason=%s (an edge landed since)",
                session_id,
                reason,
                extra=debug_event(
                    "session_status_reset_skipped", session_id=session_id, reason=reason
                ),
            )
        elif dropped is not None:
            _logger.debug(
                "session status reset: session=%s reason=%s status=%s",
                session_id,
                reason,
                dropped.status,
                extra=debug_event(
                    "session_status_reset",
                    session_id=session_id,
                    reason=reason,
                    status=dropped.status,
                ),
            )

    def forget(self, session_id: str) -> None:
        """Drop everything the book holds for a deleted session.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        """
        with self._lock:
            self._records.pop(session_id, None)
            self._dispatch_at.pop(session_id, None)
            self._control_idle_at.pop(session_id, None)

    def transfer(self, source_id: str, target_id: str) -> None:
        """Move a session's status with its pane, never clobbering the target.

        :param source_id: Session the pane leaves, e.g. ``"conv_old"``.
        :param target_id: Session the pane joins, e.g. ``"conv_new"``.
        """
        with self._lock:
            record = self._records.pop(source_id, None)
            if record is not None and target_id not in self._records:
                self._records[target_id] = record
            for stamps in (self._dispatch_at, self._control_idle_at):
                stamp = stamps.pop(source_id, None)
                if stamp is not None and target_id not in stamps:
                    stamps[target_id] = stamp

    # ── readers ──────────────────────────────────────────────────────────

    def edge_mark(self, session_id: str) -> object:
        """An opaque mark that moves whenever any channel records an edge.

        Duplicates move it too. It says nothing about the status; compare it
        only by identity, through :meth:`reset`'s *mark*.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        """
        with self._lock:
            return self._records.get(session_id)

    def current(self, session_id: str) -> StatusRecord | None:
        """Return the last recorded edge from any channel, or ``None``.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        """
        with self._lock:
            return self._records.get(session_id)

    def claim(self, session_id: str, *, include_relay: bool = False) -> StatusRecord | None:
        """Return the record when it claims the session is mid-turn.

        A claim is a ``running`` episode some local channel asserted (or any
        channel, with *include_relay*). ``waiting`` — the turn ended with
        background work left — is never a claim.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        :param include_relay: Whether a relay-only ``running`` counts.
        """
        with self._lock:
            record = self._records.get(session_id)
        if record is None or record.status != "running":
            return None
        if include_relay or record.sources & LOCAL_SOURCES:
            return record
        return None

    def blocked(self, session_id: str) -> tuple[str, float] | None:
        """Return ``(blocked_on, age_s)`` while a running session is parked.

        :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
        """
        with self._lock:
            record = self._records.get(session_id)
            now = self._clock()
        if (
            record is None
            or record.status != "running"
            or record.blocked_on is None
            or record.blocked_since is None
        ):
            return None
        return record.blocked_on, max(0.0, now - record.blocked_since)

    def age_s(self, timestamp: float) -> float:
        """Seconds elapsed since a monotonic *timestamp* from this book."""
        return max(0.0, self._clock() - timestamp)

    def last_dispatch_at(self, session_id: str) -> float | None:
        """Monotonic time of the last runner-published ``running``."""
        with self._lock:
            return self._dispatch_at.get(session_id)

    def last_control_idle_at(self, session_id: str) -> float | None:
        """Monotonic time of the last accepted native interrupt/stop idle."""
        with self._lock:
            return self._control_idle_at.get(session_id)

    def session_ids(self) -> list[str]:
        """Sessions that currently hold a record."""
        with self._lock:
            return list(self._records)

    def status_view(self) -> Mapping[str, str]:
        """A live, read-only ``{session_id: status}`` mapping."""
        return _StatusView(self)
