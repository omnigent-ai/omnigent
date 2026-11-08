"""Whether a runner counts as busy, and whether its sandbox should be kept warm.

A runner is busy while it has active work (a turn running or an event dispatch
pending) or while its work progress advanced since the previous check: a session
event was published (so a turn that started and ended between two checks still
counts) or mirrored native events kept flowing after the parent went idle. A
runner that is merely connected does not count: viewing a session keeps it
connected without any work.

The runner reports :meth:`RunnerBusyCheck.keep_warm` on each tunnel pong
(``PongFrame.keep_warm``), and the server extends a managed sandbox only while
the latest report says so (see :mod:`omnigent.server.managed_host_keepalive`).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable


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
        # Like the runner's own idle timeout, the warm window first counts from
        # the runner's start.
        self._last_busy_at = time.monotonic()

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

    def keep_warm(self, grace_s: float) -> bool:
        """Return whether the runner's sandbox should be kept warm.

        :param grace_s: Seconds to stay warm after the last work (or the
            runner's start); ``math.inf`` keeps it warm while connected.
        :returns: ``True`` while busy (see :meth:`check`) or within *grace_s*
            of the last work.
        """
        busy = self.check()
        if busy or math.isinf(grace_s):
            return True
        return time.monotonic() - self._last_busy_at < grace_s
