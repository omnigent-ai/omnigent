"""Whether a runner counts as busy for keeping its sandbox awake.

A runner is busy while it has active work (a turn running or an event dispatch
pending) or while its work progress advanced since the previous check: a session
event was published (so a turn that started and ended between two checks still
counts) or mirrored native events kept flowing after the parent went idle. A
runner that is merely alive does not count: runners are retained for
``runner.idle_timeout_s`` after their last activity, and keeping a sandbox awake
for that whole window defeats the platform's idle stop.

The runner reports this on each tunnel pong (``PongFrame.busy``), and the server
extends a managed sandbox only for intervals in which the runner was busy (see
:mod:`omnigent.server.managed_host_keepalive`).
"""

from __future__ import annotations

import time
from collections.abc import Callable

# The runner's built-in idle timeout, used when no runner.idle_timeout_s is set.
# Here (not in omnigent.runner._entry) so the server can read it cheaply.
DEFAULT_RUNNER_IDLE_TIMEOUT_S = 60 * 60


class RunnerBusyCheck:
    """Whether a runner did work since the previous :meth:`check`.

    Each consumer owns its own instance, because "since the previous check" is
    per consumer: two consumers polling at different cadences must each see the
    progress made since their own last poll.

    :param has_active_work: Side-effect-free callback, ``True`` while a turn runs
        or an event dispatch is pending.
    :param get_last_activity: Optional callback returning the monotonic time of
        the newest work progress, or ``None`` if there has been none.
    """

    def __init__(
        self,
        has_active_work: Callable[[], bool],
        get_last_activity: Callable[[], float | None] | None = None,
    ) -> None:
        self._has_active_work = has_active_work
        self._get_last_activity = get_last_activity
        self._last_activity_at: float | None = None
        self._last_busy_at: float | None = None
        self._started_at = time.monotonic()

    def check(self) -> bool:
        """Return whether the runner is busy now or made progress since the last call.

        :returns: ``True`` while work is active or work progress advanced.
        """
        activity_at = self._get_last_activity() if self._get_last_activity is not None else None
        # Catches work that finished between checks, and mirrored children that
        # make progress while their parent reports idle.
        advanced = activity_at is not None and (
            self._last_activity_at is None or activity_at > self._last_activity_at
        )
        self._last_activity_at = activity_at
        busy = self._has_active_work() or advanced
        if busy:
            self._last_busy_at = time.monotonic()
        return busy

    def report(self) -> tuple[bool, float | None]:
        """Return :meth:`check` and the seconds since this check last saw work.

        The idle time lets a server that lost its own history (a restart, or a
        reconnect to another replica) resume a keep-warm grace where it was.
        Before any work it counts from this check's creation, as the runner's own
        idle timeout does, so a reconnect cannot restart an expired grace.

        :returns: ``(busy, idle_s)``.
        """
        busy = self.check()
        if busy:
            return busy, 0.0
        return busy, time.monotonic() - (
            self._started_at if self._last_busy_at is None else self._last_busy_at
        )
