"""Active-range gate on RRULE occurrences.

A scheduled task's trigger is an RFC 5545 recurrence rule (see
:mod:`omnigent.server.scheduled.rrule`) evaluated in a per-task IANA timezone.
An "active range" is an optional time-of-day window (e.g. ``09:00``-``17:00``)
that further restricts which rule occurrences are allowed to actually fire —
so an hourly automation can be confined to business hours, for example. Both
bounds are stored as ``"HH:MM"`` strings and are either both set or both
unset; unset means the task is unrestricted (fires on every rule occurrence,
today's behavior).

This module is pure and has no dependency beyond :mod:`omnigent.server.scheduled.rrule`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from omnigent.server.scheduled.rrule import RRuleTrigger

_TIME_OF_DAY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})$")

_UTC = ZoneInfo("UTC")

# Fixed anchor for the reachability check in `assert_fires_within_range`,
# mirroring `validate_rrule`'s fixed-anchor approach (rrule.py:34-39). A
# constant UTC instant — rather than `datetime.now` — makes the verdict
# deterministic: the same (trigger, range) pair always passes or fails
# regardless of when the check runs. The anchor is a leap year (Jan 1, 2016)
# so a rule pinned to Feb 29 still reaches an occurrence within the search
# horizon.
_REACHABILITY_ANCHOR = datetime(2016, 1, 1, tzinfo=_UTC)

# Hard cap on how many candidate occurrences `next_fire_in_active_range` pulls
# from the (lazily generated) trigger before giving up. Backstops the horizon
# below against a sub-daily cadence that would otherwise walk many years of
# candidates without ever tripping it.
_MAX_RANGE_SKIPS = 2000

# How far past `after` to search for a candidate landing inside the active
# range before giving up. A full year covers any calendar-anchored rule (e.g.
# a yearly BYMONTH/BYDAY combination) that only intersects the active range on
# a handful of days a year.
_RANGE_SEARCH_HORIZON = timedelta(days=366)


class ActiveRangeValidationError(ValueError):
    """Raised when an active-range bound is malformed or the range is invalid."""


def parse_time_of_day(value: str) -> int:
    """Parse a strict 24-hour ``"HH:MM"`` time-of-day string.

    Both components must be exactly two digits (so ``"9:00"`` is rejected),
    ``HH`` must be ``00``-``23``, and ``MM`` must be ``00``-``59``.

    :param value: The time-of-day string to parse.
    :returns: Minutes since midnight, ``0``-``1439``.
    :raises ActiveRangeValidationError: If *value* is not a string, or is not
        a valid strict ``HH:MM`` 24-hour time.
    """
    if not isinstance(value, str):
        raise ActiveRangeValidationError(f"expected a string HH:MM time, got {value!r}")
    match = _TIME_OF_DAY_RE.match(value)
    if match is None:
        raise ActiveRangeValidationError(
            f"invalid time of day {value!r}; expected strict 24-hour HH:MM"
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ActiveRangeValidationError(
            f"invalid time of day {value!r}; expected strict 24-hour HH:MM"
        )
    return hour * 60 + minute


@dataclass(frozen=True)
class ActiveRange:
    """A time-of-day window, evaluated in whatever tzinfo the query moment carries.

    :param start_minute: Range start, minutes since midnight (``0``-``1439``).
    :param end_minute: Range end, minutes since midnight (``0``-``1439``).
    """

    start_minute: int
    end_minute: int

    def contains(self, moment: datetime) -> bool:
        """Return whether *moment*'s wall-clock time falls inside this range.

        Both endpoints are inclusive, at whole-minute resolution. When
        ``start_minute < end_minute`` this is a same-day window (``start <= t
        <= end``); when ``start_minute > end_minute`` the range wraps past
        midnight (e.g. ``22:00``-``06:00``), so it admits ``t >= start`` OR
        ``t <= end``. *moment* is read in its own ``tzinfo`` — callers pass a
        datetime already localized to the task's timezone.

        :param moment: A tz-aware datetime to test.
        :returns: ``True`` if *moment* falls within the range.
        """
        t = moment.hour * 60 + moment.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= t <= self.end_minute
        return t >= self.start_minute or t <= self.end_minute


def validate_active_range(start: str | None, end: str | None) -> ActiveRange | None:
    """Validate a pair of active-range bounds and build an :class:`ActiveRange`.

    Both must be ``None`` (unrestricted — a task fires on every rule
    occurrence, today's behavior) or both must be set.

    :param start: Range start as ``"HH:MM"``, or ``None``.
    :param end: Range end as ``"HH:MM"``, or ``None``.
    :returns: An :class:`ActiveRange`, or ``None`` when both bounds are unset.
    :raises ActiveRangeValidationError: If only one of *start*/*end* is set,
        either is malformed, or they are equal.
    """
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ActiveRangeValidationError(
            "active_range_start and active_range_end must be set together"
        )
    start_minute = parse_time_of_day(start)
    end_minute = parse_time_of_day(end)
    if start_minute == end_minute:
        raise ActiveRangeValidationError(
            "start and end must differ; omit both for an always-active task"
        )
    return ActiveRange(start_minute=start_minute, end_minute=end_minute)


def next_fire_in_active_range(
    trigger: RRuleTrigger,
    after: datetime,
    tz: ZoneInfo,
    active_range: ActiveRange | None,
) -> datetime | None:
    """Return the next occurrence of *trigger* after *after* that lands in *active_range*.

    When *active_range* is ``None`` this delegates straight to
    :meth:`RRuleTrigger.next_fire_after` — byte-identical to the unrestricted
    behavior. Otherwise it walks candidate occurrences one at a time,
    re-querying from the last rejected candidate, until one falls inside the
    range. Bounded by :data:`_MAX_RANGE_SKIPS` and :data:`_RANGE_SEARCH_HORIZON`
    past *after*, whichever trips first, so a rule that never lands in range
    can't walk forever.

    :param trigger: The validated RRULE trigger to search.
    :param after: The instant to search after (tz-aware).
    :param tz: The timezone occurrences are evaluated in.
    :param active_range: The gating window, or ``None`` for unrestricted.
    :returns: The next in-range occurrence, or ``None`` if the trigger is
        exhausted or no occurrence lands in range within the search bound.
    """
    if active_range is None:
        return trigger.next_fire_after(after, tz)
    horizon = after + _RANGE_SEARCH_HORIZON
    candidate = after
    for _ in range(_MAX_RANGE_SKIPS):
        candidate = trigger.next_fire_after(candidate, tz)
        if candidate is None or candidate > horizon:
            return None
        if active_range.contains(candidate):
            return candidate
    return None


def assert_fires_within_range(
    trigger: RRuleTrigger,
    active_range: ActiveRange,
    tz: ZoneInfo,
) -> None:
    """Assert that *trigger* can ever land inside *active_range*.

    A create/update-time guard: searches from a fixed anchor (rather than
    "now") using the same bounded search as :func:`next_fire_in_active_range`,
    so the verdict is wall-clock-independent — mirroring `validate_rrule`'s
    fixed-anchor approach (rrule.py:34-39).

    :param trigger: The validated RRULE trigger to check.
    :param active_range: The gating window to check reachability against.
    :param tz: The timezone occurrences are evaluated in.
    :raises ActiveRangeValidationError: If no occurrence of *trigger* ever
        lands inside *active_range*.
    """
    if next_fire_in_active_range(trigger, _REACHABILITY_ANCHOR, tz, active_range) is None:
        raise ActiveRangeValidationError("schedule never fires inside the active range")
