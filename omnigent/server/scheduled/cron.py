"""5-field POSIX cron parser, next-fire computation, and interval validator.

A self-contained cron evaluator for the scheduled-task scheduler. Supports the
classic 5-field syntax — ``minute hour day-of-month month day-of-week`` — with
``*``, ``*/N``, ``N``, ``N-M``, comma lists ``a,b,c``, and ``N-M/S`` steps. No
name aliases (``MON``/``JAN``); expressions are numeric.

Cron fields are evaluated in a caller-supplied IANA timezone so a preset such as
"Daily at 9:00 AM" fires at 09:00 local wall-clock. :func:`validate_cron`
additionally enforces a minimum interval (:data:`MIN_INTERVAL_SECONDS`) and
rejects expressions that never fire or fire only once within the search window —
each fire spawns a real agent session, so a runaway cadence is expensive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Reject anything more frequent than this. Each fire spawns a real agent
# session, so a tight cadence gets expensive fast. One hour is the tightest
# cadence we allow: hourly is a useful ceiling with a hard bound on runaway cost.
MIN_INTERVAL_SECONDS = 60 * 60

# Search horizon for the next fire. A year-and-a-day covers every annual
# expression while bounding the walk on impossible ones (e.g. ``0 0 31 2 *``).
_SEARCH_DAYS = 366

_UTC = ZoneInfo("UTC")

# Fixed anchor for the interval check. Using a constant UTC instant (rather
# than ``datetime.now``) makes validation deterministic — the same expression
# always passes or fails regardless of when it runs. UTC has no DST, so folds
# can't perturb the sampled gaps. The anchor is a leap year (Jan 1, 2016) so a
# ``… 29 2 *`` expression still reaches Feb 29 within the search horizon and is
# rejected as "fires only once" rather than "never fires".
_INTERVAL_ANCHOR = datetime(2016, 1, 1, tzinfo=_UTC)

# How far past the anchor to sample consecutive fires when measuring the
# minimum interval. A sub-floor gap can only occur between minute- or
# hour-adjacent fires, both of which recur every hour, so a full 25-hour span
# is guaranteed to contain any tight pair the expression can produce.
_INTERVAL_WINDOW = timedelta(hours=25)


class CronValidationError(ValueError):
    """Raised when a cron expression is malformed or violates a scheduler rule."""


@dataclass(frozen=True)
class CronField:
    """One parsed cron field: its allowed values (sorted, deduplicated) and
    whether the source token was an unrestricted ``*``."""

    values: list[int]
    is_wildcard: bool


@dataclass(frozen=True)
class ParsedCron:
    """A parsed 5-field cron expression."""

    minute: CronField
    hour: CronField
    day: CronField  # day of month, 1-31
    month: CronField  # 1-12
    dow: CronField  # 0-6, Sunday = 0


def _parse_field(spec: str, lo_bound: int, hi_bound: int) -> CronField:
    """Parse one comma-separated cron field into a :class:`CronField`.

    :param spec: The raw field text, e.g. ``"0-23/6"`` or ``"0,15,30"``.
    :param lo_bound: Minimum legal value for this field (inclusive).
    :param hi_bound: Maximum legal value for this field (inclusive).
    :returns: The parsed field.
    :raises CronValidationError: On empty segments, bad steps, non-numeric
        tokens, inverted ranges, or out-of-range values.
    """
    out: set[int] = set()
    is_wildcard = False
    for part in spec.split(","):
        token = part.strip()
        if not token:
            raise CronValidationError(f"Empty field segment: {spec!r}")
        step = 1
        if "/" in token:
            base, _, step_str = token.partition("/")
            try:
                step = int(step_str)
            except ValueError:
                raise CronValidationError(f"Invalid step in field: {part!r}") from None
            if step <= 0:
                raise CronValidationError(f"Invalid step in field: {part!r}")
            token = base

        if token in ("*", ""):
            lo, hi = lo_bound, hi_bound
            is_wildcard = True
        elif "-" in token:
            a, _, b = token.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise CronValidationError(f"Invalid range in field: {part!r}") from None
        else:
            try:
                lo = hi = int(token)
            except ValueError:
                raise CronValidationError(f"Non-numeric field: {part!r}") from None

        if lo < lo_bound or hi > hi_bound or lo > hi:
            raise CronValidationError(f"Field {part!r} out of range [{lo_bound}-{hi_bound}]")
        out.update(range(lo, hi + 1, step))

    return CronField(values=sorted(out), is_wildcard=is_wildcard)


def parse_cron(expression: str) -> ParsedCron:
    """Parse a 5-field cron expression.

    :param expression: A 5-field cron string, e.g. ``"0 9 * * 1-5"``.
    :returns: The :class:`ParsedCron`.
    :raises CronValidationError: If the expression does not have exactly 5
        fields or any field is invalid.
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise CronValidationError(f"Invalid cron expression: expected 5 fields, got {len(parts)}")
    m, h, dom, mon, dow = parts
    return ParsedCron(
        minute=_parse_field(m, 0, 59),
        hour=_parse_field(h, 0, 23),
        day=_parse_field(dom, 1, 31),
        month=_parse_field(mon, 1, 12),
        dow=_parse_field(dow, 0, 6),
    )


def _day_matches(parsed: ParsedCron, dom: int, dow: int) -> bool:
    """Apply POSIX DOM/DOW union semantics for a given calendar day.

    When both day-of-month and day-of-week are restricted (neither is ``*``),
    a day matches if *either* matches (union). When only one is restricted,
    only that field applies.

    :param parsed: The parsed expression.
    :param dom: Day of month (1-31).
    :param dow: Day of week (0-6, Sunday = 0).
    :returns: ``True`` if the day is a candidate fire day.
    """
    dom_ok = dom in parsed.day.values
    dow_ok = dow in parsed.dow.values
    if parsed.day.is_wildcard or parsed.dow.is_wildcard:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def get_next_fire_time(
    parsed: ParsedCron,
    after: datetime,
    tz: ZoneInfo,
) -> datetime | None:
    """Compute the next fire strictly after ``after``, evaluated in ``tz``.

    Cron fields are interpreted as wall-clock values in ``tz`` so presets fire
    at local time; the returned datetime is timezone-aware in ``tz``. Walks a
    bounded horizon (:data:`_SEARCH_DAYS`) and returns ``None`` for expressions
    that never fire within it (e.g. ``0 0 31 2 *``).

    :param parsed: The parsed cron expression.
    :param after: The instant to search after (any tz-aware datetime).
    :param tz: The IANA timezone the cron fields are evaluated in.
    :returns: The next fire as a tz-aware datetime, or ``None`` if none within
        the search horizon.
    """
    # Work in wall-clock local time: convert `after` into `tz`, advance one
    # minute, and truncate seconds so we search minute boundaries.
    local = after.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = local + timedelta(days=_SEARCH_DAYS)

    while local < limit:
        if local.month not in parsed.month.values:
            # Jump to the first day of the next month.
            if local.month == 12:
                local = local.replace(year=local.year + 1, month=1, day=1, hour=0, minute=0)
            else:
                local = local.replace(month=local.month + 1, day=1, hour=0, minute=0)
            continue
        if not _day_matches(parsed, local.day, (local.weekday() + 1) % 7):
            # weekday(): Mon=0..Sun=6; cron dow: Sun=0..Sat=6.
            local = (local + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if local.hour not in parsed.hour.values:
            local = (local + timedelta(hours=1)).replace(minute=0)
            continue
        if local.minute not in parsed.minute.values:
            local = local + timedelta(minutes=1)
            continue
        # All fields match. `local` is already aware in `tz`, so no fold
        # resolution happens here: a spring-forward "imaginary" wall-clock
        # time maps to some instant via zoneinfo, and a fall-back duplicated
        # time picks the earlier of the two. Both are acceptable at an hourly
        # floor — the schedule can slip by at most an hour across a DST edge.
        return local

    return None


@dataclass(frozen=True)
class CronTrigger:
    """A validated cron expression that can compute its next fire."""

    parsed: ParsedCron
    expression: str

    def next_fire_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """Return the next fire strictly after ``after`` in ``tz``.

        :param after: The instant to search after (tz-aware).
        :param tz: The timezone the cron fields are evaluated in.
        :returns: The next fire, or ``None`` if none within the search horizon.
        """
        return get_next_fire_time(self.parsed, after, tz)


def validate_cron(expression: str, tz: ZoneInfo | None = None) -> CronTrigger:  # noqa: ARG001
    """Parse and validate a cron expression for use as a recurring trigger.

    Beyond syntax, enforces that the expression (a) fires at least twice within
    the search horizon and (b) has a minimum gap of at least
    :data:`MIN_INTERVAL_SECONDS` between *any* two consecutive fires.

    The interval check samples fires from a fixed UTC anchor, so the verdict is
    deterministic (independent of the wall-clock instant it runs at) and immune
    to DST folds.

    :param expression: The 5-field cron string.
    :param tz: Accepted for API compatibility but not used by the interval
        check, which is timezone-agnostic for the cadences we allow.
    :returns: A :class:`CronTrigger`.
    :raises CronValidationError: On bad syntax, never-fires, fires-once, or a
        sub-minimum interval.
    """
    parsed = parse_cron(expression)
    # Sample fires from a fixed UTC anchor so validation is deterministic and
    # DST-agnostic — the `tz` parameter is retained for API compatibility but
    # the interval check is timezone-agnostic for the cadences we allow.
    prev = get_next_fire_time(parsed, _INTERVAL_ANCHOR, _UTC)
    if prev is None:
        raise CronValidationError("Cron expression never fires")
    cur = get_next_fire_time(parsed, prev, _UTC)
    if cur is None:
        raise CronValidationError("Cron expression fires only once")

    # Take the *minimum* gap across every consecutive pair in the window, not
    # just the first pair: an expression like ``0,1 * * * *`` spaces fires
    # irregularly, so the tightest (sub-floor) pair need not be the first one.
    window_end = prev + _INTERVAL_WINDOW
    min_gap = (cur - prev).total_seconds()
    while cur < window_end:
        nxt = get_next_fire_time(parsed, cur, _UTC)
        if nxt is None:
            break
        min_gap = min(min_gap, (nxt - cur).total_seconds())
        prev, cur = cur, nxt

    if min_gap < MIN_INTERVAL_SECONDS:
        raise CronValidationError(
            f"Minimum interval is {MIN_INTERVAL_SECONDS // 60} minutes "
            f"(this expression fires every {int(min_gap)}s)"
        )
    return CronTrigger(parsed=parsed, expression=expression)
