"""RRULE (RFC 5545) next-fire computation and interval validator.

A thin wrapper over :mod:`dateutil.rrule` for the scheduled-task scheduler.
A trigger is an RFC 5545 recurrence rule string — e.g. ``"FREQ=HOURLY"`` or
``"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"`` — evaluated in a
caller-supplied IANA timezone so a preset such as "Daily at 9:00 AM" fires at
09:00 local wall-clock.

The rule is anchored at midnight of the reference day (localized to the task
timezone), so occurrence phase is deterministic: an hourly rule fires on the
hour, a daily rule at its ``BYHOUR``/``BYMINUTE``. :func:`validate_rrule`
additionally enforces a minimum interval (:data:`MIN_INTERVAL_SECONDS`) and
rejects rules that never fire or fire only once within the search window — each
fire spawns a real agent session, so a runaway cadence is expensive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

# Reject anything more frequent than this. Each fire spawns a real agent
# session, so a tight cadence gets expensive fast. One hour is the tightest
# cadence we allow: hourly is a useful ceiling with a hard bound on runaway cost.
MIN_INTERVAL_SECONDS = 60 * 60

_UTC = ZoneInfo("UTC")

# Fixed anchor for the interval check. Using a constant UTC instant (rather
# than ``datetime.now``) makes validation deterministic — the same rule always
# passes or fails regardless of when it runs. UTC has no DST, so folds can't
# perturb the sampled gaps. The anchor is a leap year (Jan 1, 2016) so a rule
# pinned to Feb 29 still reaches an occurrence within the search horizon.
_INTERVAL_ANCHOR = datetime(2016, 1, 1, tzinfo=_UTC)

# How far past the anchor to sample consecutive fires when measuring the
# minimum interval. A sub-floor gap can only occur between minute- or
# hour-adjacent fires, both of which recur within an hour, so a 25-hour span is
# guaranteed to contain any tight pair a sub-hourly cadence can produce.
_INTERVAL_WINDOW = timedelta(hours=25)

# Hard cap on how many occurrences the validator pulls from the (lazily
# generated) rule. ``dateutil`` generates on demand, so the window bound
# normally stops the walk first; this backstops the minute-cadence case, where
# 25 hours is ~1500 occurrences, against an unbounded pull.
_MAX_SAMPLE_OCCURRENCES = 2000


class RRuleValidationError(ValueError):
    """Raised when an RRULE string is malformed or violates a scheduler rule."""


def _anchor_dtstart(after: datetime, tz: ZoneInfo) -> datetime:
    """Localize ``after`` to ``tz`` and return midnight of that local day.

    Anchoring at midnight gives occurrences a deterministic phase regardless of
    the instant we happen to query at: an hourly rule lands on the hour and a
    daily rule at its ``BYHOUR``/``BYMINUTE``.

    Caveat for ``INTERVAL>1`` recurrences (e.g. biweekly
    ``FREQ=WEEKLY;INTERVAL=2`` or interval-monthly): dateutil counts active
    periods relative to ``dtstart``, so re-anchoring to midnight of the query
    day ties the phase to whichever day the timer last re-armed. A restart on a
    different weekday can slip such a rule by one period. ``INTERVAL=1`` rules
    (hourly/daily/simple-weekly) are unaffected. This is acceptable for the
    current preset set; a proper fix — persisting a stable per-task ``dtstart``
    — is deferred to future work if unbounded-interval rules become user-facing.
    """
    local = after.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def get_next_fire_time(
    rule_str: str,
    after: datetime,
    tz: ZoneInfo,
) -> datetime | None:
    """Compute the next fire strictly after ``after``, evaluated in ``tz``.

    The rule is anchored at midnight of ``after``'s local day so occurrences
    carry a deterministic wall-clock phase; the returned datetime is
    timezone-aware in ``tz``. Returns ``None`` when the rule is exhausted (a
    ``COUNT``/``UNTIL`` rule can legitimately end, unlike a bare cron).

    :param rule_str: An RFC 5545 recurrence rule, e.g. ``"FREQ=DAILY;BYHOUR=9"``.
    :param after: The instant to search after (any tz-aware datetime).
    :param tz: The IANA timezone occurrences are evaluated in.
    :returns: The next fire as a tz-aware datetime, or ``None`` if the rule has
        no further occurrences.
    :raises RRuleValidationError: If ``rule_str`` is malformed.
    """
    dtstart = _anchor_dtstart(after, tz)
    rule = _parse(rule_str, dtstart)
    # `rule.after` compares in dtstart's timezone, so localize `after` too. A
    # spring-forward "imaginary" wall time maps to some instant via zoneinfo and
    # a fall-back duplicated time picks the earlier of the two; both are
    # acceptable at an hourly floor — the schedule slips by at most an hour
    # across a DST edge.
    return rule.after(after.astimezone(tz), inc=False)


@dataclass(frozen=True)
class RRuleTrigger:
    """A validated RRULE string that can compute its next fire."""

    rule: str

    def next_fire_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """Return the next fire strictly after ``after`` in ``tz``.

        :param after: The instant to search after (tz-aware).
        :param tz: The timezone occurrences are evaluated in.
        :returns: The next fire, or ``None`` if the rule is exhausted.
        """
        return get_next_fire_time(self.rule, after, tz)


def _parse(rule_str: str, dtstart: datetime):
    """Parse an RRULE string anchored at ``dtstart``, normalizing errors.

    :raises RRuleValidationError: On any malformed input ``dateutil`` rejects.
    """
    try:
        return rrulestr(rule_str, dtstart=dtstart)
    except (ValueError, TypeError) as exc:
        raise RRuleValidationError(f"Invalid RRULE {rule_str!r}: {exc}") from exc


def validate_rrule(rule_str: str, tz: ZoneInfo | None = None) -> RRuleTrigger:  # noqa: ARG001
    """Parse and validate an RRULE string for use as a recurring trigger.

    Beyond syntax, enforces that the rule (a) fires at least twice within the
    search window and (b) has a minimum gap of at least
    :data:`MIN_INTERVAL_SECONDS` between *any* two consecutive fires.

    The interval check samples fires from a fixed UTC anchor, so the verdict is
    deterministic (independent of the wall-clock instant it runs at) and immune
    to DST folds.

    :param rule_str: The RFC 5545 recurrence rule string.
    :param tz: Accepted for API compatibility but not used by the interval
        check, which is timezone-agnostic for the cadences we allow.
    :returns: An :class:`RRuleTrigger`.
    :raises RRuleValidationError: On bad syntax, never-fires, fires-once, or a
        sub-minimum interval.
    """
    rule = _parse(rule_str, _INTERVAL_ANCHOR)

    # Pull consecutive occurrences from the fixed anchor, bounded by the sample
    # window (and a hard count cap) so a lazily-generated rule can't walk
    # forever. Track the tightest gap across every consecutive pair, not just
    # the first: an irregular cadence can hide its sub-floor pair mid-window.
    prev: datetime | None = None
    window_end: datetime | None = None
    min_gap = float("inf")
    count = 0
    for occ in rule:
        count += 1
        if prev is None:
            window_end = occ + _INTERVAL_WINDOW
            prev = occ
            continue
        min_gap = min(min_gap, (occ - prev).total_seconds())
        prev = occ
        if occ >= window_end or count >= _MAX_SAMPLE_OCCURRENCES:
            break

    if count == 0:
        raise RRuleValidationError("RRULE never fires")
    if count == 1:
        raise RRuleValidationError("RRULE fires only once")
    if min_gap < MIN_INTERVAL_SECONDS:
        raise RRuleValidationError(
            f"Minimum interval is {MIN_INTERVAL_SECONDS // 60} minutes "
            f"(this rule fires every {int(min_gap)}s)"
        )
    return RRuleTrigger(rule=rule_str)
