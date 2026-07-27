"""Server-process scheduler for recurring scheduled tasks.

Two pieces live here:

* :mod:`omnigent.server.scheduled.rrule` — RRULE (RFC 5545) next-fire
  computation and the minimum-interval validator, backed by
  :mod:`dateutil.rrule`.
* :mod:`omnigent.server.scheduled.scheduler` — the
  :class:`~omnigent.server.scheduled.scheduler.ScheduledTaskScheduler`, which
  arms one self-rearming timer per active scheduled task and invokes an injected
  ``on_fire`` callback when a task is due.

The scheduler only decides *when* a task fires; the firing itself (creating an
agent session) is supplied by the caller via the ``on_fire`` seam.
"""

from __future__ import annotations

from omnigent.server.scheduled.rrule import (
    MIN_INTERVAL_SECONDS,
    RRuleTrigger,
    RRuleValidationError,
    get_next_fire_time,
    validate_rrule,
)
from omnigent.server.scheduled.scheduler import (
    MISFIRE_GRACE_TIME_S,
    OnFire,
    ScheduledTaskScheduler,
)

__all__ = [
    "MIN_INTERVAL_SECONDS",
    "MISFIRE_GRACE_TIME_S",
    "OnFire",
    "RRuleTrigger",
    "RRuleValidationError",
    "ScheduledTaskScheduler",
    "get_next_fire_time",
    "validate_rrule",
]
