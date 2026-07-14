"""Server-process cron scheduler for recurring scheduled tasks (Routines).

Two pieces live here:

* :mod:`omnigent.server.automations.cron` — a self-contained 5-field POSIX cron
  parser, next-fire computation, and the minimum-interval validator.
* :mod:`omnigent.server.automations.scheduler` — the
  :class:`~omnigent.server.automations.scheduler.AutomationScheduler`, which
  arms one self-rearming timer per active scheduled task and invokes an injected
  ``on_fire`` callback when a task is due.

The scheduler only decides *when* a task fires; the firing itself (creating an
agent session) is supplied by the caller via the ``on_fire`` seam.
"""

from __future__ import annotations

from omnigent.server.automations.cron import (
    MIN_INTERVAL_SECONDS,
    CronTrigger,
    CronValidationError,
    get_next_fire_time,
    parse_cron,
    validate_cron,
)
from omnigent.server.automations.scheduler import (
    MISFIRE_GRACE_TIME_S,
    AutomationScheduler,
    OnFire,
)

__all__ = [
    "MIN_INTERVAL_SECONDS",
    "MISFIRE_GRACE_TIME_S",
    "AutomationScheduler",
    "CronTrigger",
    "CronValidationError",
    "OnFire",
    "get_next_fire_time",
    "parse_cron",
    "validate_cron",
]
