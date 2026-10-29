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

Schedules use a single rule, at most 24 distinct values per BY field and 24 time
combinations, an eight-year occurrence horizon, and work and elapsed-time limits.
Hourly-equivalent minute and second rules remain supported for persisted schedules.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from dateutil.rrule import rrule, rrulestr  # type: ignore[import-untyped]

from omnigent.server.scheduled._bounded_rrule import RRuleSearchLimitError, iter_bounded

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
_MAX_RULE_LENGTH = 4096
_SEARCH_YEARS = 8
_MAX_SEARCH_STEPS = 100_000
_MAX_SEARCH_SECONDS = 0.25
_MAX_BY_VALUES = 24


class RRuleValidationError(ValueError):
    """Raised when an RRULE string is malformed or violates a scheduler rule."""


class RRuleSearchError(RRuleValidationError):
    """The search budget was exceeded; this does not establish rule exhaustion."""


class _ParsedRRule(Protocol):
    def after(self, dt: datetime, inc: bool = False) -> datetime | None:
        raise NotImplementedError

    def __iter__(self) -> Iterator[datetime]:
        raise NotImplementedError


class _RRuleLimits(Protocol):
    _until: datetime | None
    _count: int | None


@dataclass(frozen=True)
class _BoundedRRule:
    rule: rrule
    count: int | None
    count_after: datetime | None = None

    def __iter__(self) -> Iterator[datetime]:
        return self._occurrences()

    def _occurrences(self, start_at: datetime | None = None) -> Iterator[datetime]:
        try:
            occurrences = iter_bounded(
                self.rule,
                max_steps=_MAX_SEARCH_STEPS,
                max_seconds=_MAX_SEARCH_SECONDS,
                # This wrapper applies COUNT after iteration, so it must retain history.
                start_at=start_at if self.count is None else None,
            )
            if self.count is not None and self.count_after is not None:
                # Occurrences at or before creation do not consume COUNT.
                occurrences = (occ for occ in occurrences if occ > self.count_after)
            yield from islice(occurrences, self.count)
        except RRuleSearchLimitError as exc:
            raise RRuleSearchError(str(exc)) from exc
        except ValueError as exc:
            raise RRuleValidationError(str(exc)) from exc

    def after(self, dt: datetime, inc: bool = False) -> datetime | None:
        return next(
            (occ for occ in self._occurrences(dt) if occ > dt or (inc and occ == dt)), None
        )


def _anchor_dtstart(after: datetime, tz: ZoneInfo) -> datetime:
    """Localize ``after`` to ``tz`` and return midnight of that local day.

    Anchoring at midnight gives occurrences a deterministic phase regardless of
    the instant we happen to query at: an hourly rule lands on the hour and a
    daily rule at its ``BYHOUR``/``BYMINUTE``.

    Persisted schedules supply the creation date as their reference, so
    INTERVAL and COUNT keep their phase across restarts. Legacy timers use
    the query date and retain their historical re-anchoring behavior.
    """
    local = after.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def get_next_fire_time(
    rule_str: str,
    after: datetime,
    tz: ZoneInfo,
    *,
    anchor: datetime | None = None,
) -> datetime | None:
    """Compute the next fire strictly after ``after``, evaluated in ``tz``.

    The rule is anchored at midnight of the reference day's local time so occurrences
    carry a deterministic wall-clock phase; the returned datetime is
    timezone-aware in ``tz``. Returns ``None`` when the rule is exhausted (a
    ``COUNT``/``UNTIL`` rule can legitimately end, unlike a bare cron).

    :param rule_str: An RFC 5545 recurrence rule, e.g. ``"FREQ=DAILY;BYHOUR=9"``.
    :param after: The instant to search after (any tz-aware datetime).
    :param tz: The IANA timezone occurrences are evaluated in.
    :param anchor: Creation time for persisted schedules. COUNT includes only
        occurrences strictly after this instant; interval phase uses its local date.
        Omit to retain the legacy timer's query-day anchor and COUNT behavior.
    :returns: The next fire as a tz-aware datetime, or ``None`` if the rule has
        no further occurrences.
    :raises RRuleValidationError: If ``rule_str`` is malformed.
    """
    dtstart = _anchor_dtstart(anchor if anchor is not None else after, tz)
    rule = _parse(
        rule_str,
        dtstart,
        horizon_from=after.astimezone(tz) if anchor is not None else None,
        count_after=anchor.astimezone(tz) if anchor is not None else None,
    )
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


def _parse(
    rule_str: str,
    dtstart: datetime,
    *,
    horizon_from: datetime | None = None,
    count_after: datetime | None = None,
) -> _ParsedRRule:
    """Parse an RRULE string anchored at ``dtstart``, normalizing errors.

    :raises RRuleValidationError: On any malformed input ``dateutil`` rejects.
    """
    try:
        if len(rule_str) > _MAX_RULE_LENGTH or "\n" in rule_str or "\r" in rule_str:
            raise ValueError("Use one RRULE of at most 4096 characters")
        text = rule_str.strip().upper().removeprefix("RRULE:")
        fields: dict[str, str] = {}
        for part in text.split(";"):
            key, value = part.split("=", 1)
            if key in fields or not value:
                raise ValueError("RRULE fields must be unique and non-empty")
            if key.startswith("BY"):
                values = []
                for item in value.split(","):
                    if key == "BYDAY":
                        match = re.fullmatch(r"([+-]?\d+)?([A-Z]{2})", item.strip())
                        if match is None:
                            raise ValueError("Invalid BYDAY value")
                        ordinal, weekday = match.groups()
                        item = f"{int(ordinal)}{weekday}" if ordinal is not None else weekday
                    else:
                        item = str(int(item))
                    if item not in values:
                        values.append(item)
                    if len(values) > _MAX_BY_VALUES:
                        raise ValueError(
                            f"{key} supports at most {_MAX_BY_VALUES} distinct values"
                        )
                value = ",".join(values)
            fields[key] = value
        frequency = fields.get("FREQ")
        if frequency not in {
            "SECONDLY",
            "MINUTELY",
            "HOURLY",
            "DAILY",
            "WEEKLY",
            "MONTHLY",
            "YEARLY",
        }:
            raise ValueError("Unknown recurrence frequency")
        scale = {"SECONDLY": 3600, "MINUTELY": 60}.get(frequency, 1)
        if not 1 <= int(fields.get("INTERVAL", "1")) <= 366 * scale:
            raise ValueError(f"INTERVAL must be between 1 and {366 * scale}")
        if "COUNT" in fields and not 1 <= int(fields["COUNT"]) <= 2**31 - 1:
            raise ValueError("COUNT must be a positive 32-bit integer")
        combinations = 1
        for key in ("BYHOUR", "BYMINUTE", "BYSECOND"):
            combinations *= len(fields.get(key, "0").split(","))
        if combinations > 24:
            raise ValueError("At most 24 time combinations per day are supported")
        parsed = rrulestr(
            ";".join(f"{key}={value}" for key, value in fields.items()), dtstart=dtstart
        )
        if not isinstance(parsed, rrule):
            raise ValueError("Use a single recurrence rule")
        # The iterator also checks the horizon before searching each period.
        reference = max(dtstart, horizon_from) if horizon_from is not None else dtstart
        horizon_year = min(reference.year + _SEARCH_YEARS, 9999)
        try:
            horizon = reference.replace(year=horizon_year)
        except ValueError:
            horizon = reference.replace(year=horizon_year, day=28)
        limits = cast(_RRuleLimits, parsed)
        original_until = limits._until
        until = min(original_until, horizon) if original_until is not None else horizon
        # Apply COUNT outside dateutil so the bounded rule does not combine
        # COUNT and UNTIL, which RFC 5545 disallows.
        bounded = parsed.replace(count=None, until=until)
        return _BoundedRRule(bounded, limits._count, count_after)
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
        assert window_end is not None
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
