"""Dependency-free 5-field cron parsing for the scheduler engine (#6).

The scheduler only needs one thing from a cron expression: given "now", when
does it next fire? :func:`next_cron_time` answers that for standard 5-field
cron (``minute hour day-of-month month day-of-week``) with ``*``, ``*/step``,
``a-b`` ranges, ``a,b,c`` lists, and ``MON``/``JAN`` names — enough for the
recurring-prompt loops this feature schedules, and with no third-party
dependency (a cron parser is a few dozen lines; a runtime dep is not worth it).

Semantics match standard cron:

- Day-of-week accepts ``0`` **or** ``7`` for Sunday.
- When BOTH day-of-month and day-of-week are restricted (neither is ``*``), a
  day matches if EITHER field matches — the classic cron OR rule (e.g.
  ``0 0 1 * MON`` fires on the 1st *and* every Monday).
"""

from __future__ import annotations

from datetime import datetime, timedelta

_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MONTH_NAMES = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
# Bound the forward search so an impossible expression (a day/month combo that
# never occurs) raises instead of looping forever. A leap day recurs within
# four years — the longest real gap between valid occurrences.
_MAX_MINUTES = 4 * 366 * 24 * 60


def _val(token: str, names: dict[str, int]) -> int:
    """Resolve a cron token to an int, honoring MON/JAN-style names."""
    token = token.strip().upper()
    if token in names:
        return names[token]
    return int(token)


def _parse_field(field: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    """Parse one cron field into the set of matching ints in ``[lo, hi]``.

    Supports ``*``, ``*/step``, ``a-b``, ``a-b/step``, ``a,b,c`` lists, and the
    ``names`` mapping (case-insensitive). Raises :class:`ValueError` on an
    out-of-range value, an inverted range, or a non-positive step.
    """
    names = names or {}
    out: set[int] = set()
    for part in field.strip().split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field component")
        step = 1
        rng = part
        if "/" in part:
            rng, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"cron step must be positive: {part!r}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = _val(a, names), _val(b, names)
        else:
            start = end = _val(rng, names)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron value out of range [{lo},{hi}] or inverted: {part!r}")
        out.update(range(start, end + 1, step))
    return out


def next_cron_time(cron: str, base: datetime) -> datetime:
    """Return the next datetime after ``base``'s minute that matches ``cron``.

    :param cron: A standard 5-field cron expression, e.g. ``"0 22 * * FRI"``.
    :param base: The reference time (its seconds/microseconds are ignored; the
        result is strictly in a later minute).
    :returns: The next matching datetime, carrying ``base``'s tzinfo.
    :raises ValueError: If ``cron`` is malformed, has an out-of-range field, or
        has no occurrence within a four-year horizon.
    """
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f"cron must have 5 fields, got {len(fields)}: {cron!r}")
    minutes = _parse_field(fields[0], 0, 59)
    hours = _parse_field(fields[1], 0, 23)
    doms = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12, _MONTH_NAMES)
    dows = _parse_field(fields[4], 0, 7, _DOW_NAMES)
    dows = {0 if d == 7 else d for d in dows}  # normalize Sunday (7 -> 0)
    dom_restricted = fields[2].strip() != "*"
    dow_restricted = fields[4].strip() != "*"

    candidate = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(_MAX_MINUTES):
        if candidate.month in months and candidate.hour in hours and candidate.minute in minutes:
            dom_ok = candidate.day in doms
            # Python weekday(): Mon=0..Sun=6; cron day-of-week: Sun=0..Sat=6.
            dow_ok = ((candidate.weekday() + 1) % 7) in dows
            if dom_restricted and dow_restricted:
                day_ok = dom_ok or dow_ok
            elif dom_restricted:
                day_ok = dom_ok
            elif dow_restricted:
                day_ok = dow_ok
            else:
                day_ok = True
            if day_ok:
                return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"cron {cron!r} has no occurrence within {_MAX_MINUTES // (24 * 60)} days")
