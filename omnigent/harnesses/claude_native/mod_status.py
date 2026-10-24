"""Claude status from the mod's turn events, corrected by its heartbeat.

The mod reports when each main-loop turn starts and completes, and each heartbeat
names the main-loop turn it believes is running. Once a turn event has been
applied and the heartbeat is current, those decide the session's running and
idle status. The OSC and status-file source and the pane watcher stay silent
during that time, but they keep tracking state so that they can take over at once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from omnigent.harnesses.claude_native.mod_events import (
    MOD_EVENTS_FILE_NAME,
    ModEventsCursor,
    ModHeartbeat,
    ModTurnStart,
    heartbeat_is_current,
    read_mod_events,
    read_mod_heartbeat,
    read_server_pid,
)

_logger = logging.getLogger(__name__)

# A heartbeat may predate a turn event it arrives after; trust it only once this much newer.
MOD_HEARTBEAT_SETTLE_S = 5.0
# Matches the pane idle threshold so this source shows no idle between back-to-back turns.
MOD_TURN_IDLE_GRACE_S = 1.0

_RUNNING = "running"
_IDLE = "idle"

StatusEdgeCallback = Callable[[str, str | None], None]


class StructuredStatusSource(Protocol):
    """The OSC or status-file poller that the mod poller defers to."""

    @property
    def active(self) -> bool:
        """Whether this source currently owns the session status."""

    @property
    def blocked_on(self) -> str | None:
        """Reason the agent is parked on a dialog, or ``None``."""

    def tick(self) -> None:
        """Advance one poll."""

    def resync(self) -> None:
        """Re-assert the current value on the next poll."""

    def retire(self) -> None:
        """Stop reading after the terminal exits."""


class ModTurnStatusPoller:
    """Per-tick status source that prefers the mod's main-loop turn state.

    Once a main-loop turn event has been applied, the mod owns running and idle
    while its heartbeat is current. Otherwise the structured source and the pane
    watcher decide, as they did before this source existed.

    The heartbeat corrects a lost turn event (see :meth:`_reconcile_with_heartbeat`),
    and a long turn is never released on elapsed time while its heartbeat names it.

    A running status is published at once. An idle status, from a turn.complete or
    from an adopted heartbeat, is published only after ``idle_grace_s`` with no new
    turn, so that a quick follow-up turn does not look like a finished one.

    :param bridge_dir: Bridge directory holding ``mod.json``, ``server.json``
        and ``mod_events.jsonl``.
    :param build_structured: Builds the OSC or status-file source. It receives
        the callback that the source must use for its status edges.
    :param on_structured_status: Publishes an edge from the structured source.
    :param on_mod_status: Publishes an edge decided by the turn events.
    :param on_pane_status: Publishes an idle edge derived from pane quiescence.
    :param pane_quiet_s: Seconds without pane output that count as quiet.
    :param session_id: Omnigent session id, used for log attribution.
    :param heartbeat_settle_s: Age of a heartbeat relative to the last applied
        event that must be reached before the heartbeat may correct the events.
    :param idle_grace_s: Delay before an idle view is published, cancelled by a
        turn.start that arrives first.
    :param wall_clock: Server-clock time source for heartbeat freshness.
    :param monotonic: Monotonic time source for pane quiescence and the idle grace.
    """

    def __init__(
        self,
        *,
        bridge_dir: Path,
        build_structured: Callable[[StatusEdgeCallback], StructuredStatusSource],
        on_structured_status: StatusEdgeCallback,
        on_mod_status: StatusEdgeCallback,
        on_pane_status: StatusEdgeCallback,
        pane_quiet_s: float,
        session_id: str | None = None,
        heartbeat_settle_s: float = MOD_HEARTBEAT_SETTLE_S,
        idle_grace_s: float = MOD_TURN_IDLE_GRACE_S,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bridge_dir = bridge_dir
        self._on_structured_status = on_structured_status
        self._on_mod_status = on_mod_status
        self._on_pane_status = on_pane_status
        self._pane_quiet_s = pane_quiet_s
        self._session_id = session_id
        self._heartbeat_settle_s = heartbeat_settle_s
        self._idle_grace_s = idle_grace_s
        self._idle_due_at: float | None = None
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._owning = False
        # Structured edges are recorded even while muted, so a takeover knows the current value.
        self._latest_structured: tuple[str, str | None] | None = None
        self._structured = build_structured(self._on_structured_edge)
        self._cursor = ModEventsCursor()
        self._turn_status: str | None = None
        self._turn_event_at: float | None = None
        self._last_published: tuple[str, str | None] | None = None
        self._last_pane_activity: float | None = None
        self._retired = False
        self._resync_lock = threading.Lock()
        self._resync_requested = False

    @property
    def active(self) -> bool:
        """Whether the mod or the structured source owns status, muting pane edges."""
        return not self._retired and (self._owning or self._structured.active)

    @property
    def blocked_on(self) -> str | None:
        """Dialog reason to show: the structured source's, while the mod owns a running turn."""
        if self._retired:
            return None
        if self._owning:
            return self._annotation()
        return self._structured.blocked_on

    def tick(self) -> None:
        """Advance one poll: apply the mod's events and heartbeat, then publish the result."""
        if self._retired:
            return
        with self._resync_lock:
            resync_requested = self._resync_requested
            self._resync_requested = False
        was_owning = self._owning
        heartbeat = read_mod_heartbeat(self._bridge_dir)
        usable = heartbeat_is_current(
            heartbeat,
            server_pid=read_server_pid(self._bridge_dir),
            now=self._wall_clock(),
        )
        if usable and heartbeat is not None:
            self._consume_turn_events()
            self._reconcile_with_heartbeat(heartbeat)
        self._owning = usable and self._turn_status is not None
        if was_owning and not self._owning:
            self._hand_back()
        elif self._owning and not was_owning:
            _logger.info(
                "Mod turn events own the session status",
                extra={"session_id": self._session_id},
            )
        # Cleared only after the hand-back, which reads what was last published.
        if resync_requested:
            self._last_published = None
        self._structured.tick()
        if self._owning:
            self._publish_owned_status()

    def note_pane_activity(self) -> None:
        """Record agent output on the pane, for the quiet check used when handing back."""
        self._last_pane_activity = self._monotonic()

    def resync(self) -> None:
        """Re-assert the current status on the next poll, e.g. after a reconnect."""
        with self._resync_lock:
            self._resync_requested = True
        self._structured.resync()

    def retire(self) -> None:
        """Stop publishing once the terminal has exited."""
        self._retired = True
        self._owning = False
        self._structured.retire()

    def _consume_turn_events(self) -> None:
        """Apply newly appended main-loop turn events to the turn state."""
        read = read_mod_events(self._bridge_dir / MOD_EVENTS_FILE_NAME, self._cursor)
        self._cursor = read.cursor
        for event in read.events:
            # Subagent and teammate turns do not describe the main loop.
            if event.agent_id is not None:
                continue
            self._set_turn_status(_RUNNING if isinstance(event, ModTurnStart) else _IDLE)
            self._turn_event_at = event.at

    def _reconcile_with_heartbeat(self, heartbeat: ModHeartbeat) -> None:
        """Adopt the heartbeat's main-loop view when it disagrees with the applied events.

        This heals a lost turn.complete or turn.start, but only once a first turn event
        has been applied. A heartbeat written less than ``heartbeat_settle_s`` after the
        last applied event may have been computed before that event, so it must not
        flip the state back.

        :param heartbeat: The current, usable heartbeat.
        """
        if self._turn_status is None or self._turn_event_at is None:
            return
        view = _RUNNING if heartbeat.main_turn_id is not None else _IDLE
        if view == self._turn_status:
            return
        if heartbeat.updated_at - self._turn_event_at < self._heartbeat_settle_s:
            return
        _logger.info(
            "Mod heartbeat disagrees with its turn events; adopting its view: %s",
            view,
            extra={"session_id": self._session_id},
        )
        self._set_turn_status(view)

    def _set_turn_status(self, status: str) -> None:
        """Record the main-loop status, starting the idle grace when it becomes idle.

        :param status: ``"running"`` or ``"idle"``.
        """
        self._turn_status = status
        if status == _RUNNING:
            self._idle_due_at = None
        else:
            self._idle_due_at = self._monotonic() + self._idle_grace_s

    def _hand_back(self) -> None:
        """Give the status back to the structured source and pane when the mod lets go."""
        last_published = self._last_published
        self._last_published = None
        self._structured.resync()
        # The pane idle edge may have been muted while the mod showed running; re-check quiet.
        showed_running = last_published is not None and last_published[0] == _RUNNING
        if showed_running and not self._structured.active and self._pane_quiet():
            self._on_pane_status(_IDLE, None)

    def _publish_owned_status(self) -> None:
        """Publish the mod's running or idle status when it differs from the last publish."""
        status = self._turn_status
        if status is None:
            return
        if status == _IDLE and self._idle_due_at is not None:
            if self._monotonic() < self._idle_due_at:
                # A follow-up turn may start right after this completion; hold the last status.
                return
            self._idle_due_at = None
        blocked_on = self._annotation()
        desired = (status, blocked_on)
        if desired == self._last_published:
            return
        self._last_published = desired
        self._on_mod_status(status, blocked_on)

    def _annotation(self) -> str | None:
        """Return the structured source's dialog reason, shown only while the mod runs a turn."""
        if self._turn_status != _RUNNING or self._latest_structured is None:
            return None
        status, blocked_on = self._latest_structured
        return blocked_on if status == _RUNNING else None

    def _on_structured_edge(self, status: str, blocked_on: str | None = None) -> None:
        """Record a structured-source edge and publish it unless the mod owns the status.

        :param status: Runner-vocabulary status, e.g. ``"running"``.
        :param blocked_on: Dialog reason, or ``None``.
        """
        if self._retired:
            return
        self._latest_structured = (status, blocked_on)
        if not self._owning:
            self._on_structured_status(status, blocked_on)

    def _pane_quiet(self) -> bool:
        """Whether the pane has produced no agent output for the quiet window."""
        if self._last_pane_activity is None:
            return True
        return self._monotonic() - self._last_pane_activity >= self._pane_quiet_s
