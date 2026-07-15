"""Tests for the 5-field POSIX cron parser and next-fire computation.

Exercises :func:`parse_cron`, :func:`get_next_fire_time`, and
:func:`validate_cron` — including field-syntax variants, POSIX DOM/DOW union
semantics, timezone evaluation, the never-fires / fires-once bail-outs, and the
one-hour minimum-interval floor.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from omnigent.server.automations.cron import (
    MIN_INTERVAL_SECONDS,
    CronValidationError,
    get_next_fire_time,
    parse_cron,
    validate_cron,
)

UTC = ZoneInfo("UTC")


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0, tz: ZoneInfo = UTC) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# ── parse_cron: field syntax ─────────────────────────────────────────────────


def test_parse_wildcards() -> None:
    p = parse_cron("* * * * *")
    assert p.minute.values == list(range(60))
    assert p.hour.values == list(range(24))
    assert p.day.values == list(range(1, 32))
    assert p.month.values == list(range(1, 13))
    assert p.dow.values == list(range(7))


def test_parse_single_values() -> None:
    p = parse_cron("30 9 15 6 3")
    assert p.minute.values == [30]
    assert p.hour.values == [9]
    assert p.day.values == [15]
    assert p.month.values == [6]
    assert p.dow.values == [3]


def test_parse_step() -> None:
    assert parse_cron("*/15 * * * *").minute.values == [0, 15, 30, 45]


def test_parse_list() -> None:
    assert parse_cron("0,15,30,45 * * * *").minute.values == [0, 15, 30, 45]


def test_parse_range() -> None:
    assert parse_cron("0 9-17 * * *").hour.values == list(range(9, 18))


def test_parse_range_with_step() -> None:
    assert parse_cron("0 0-23/6 * * *").hour.values == [0, 6, 12, 18]


def test_parse_dedupes_and_sorts() -> None:
    assert parse_cron("30,0,30,15 * * * *").minute.values == [0, 15, 30]


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",  # too few fields
        "* * * * * *",  # too many fields
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 0 * *",  # day-of-month below 1
        "* * 32 * *",  # day-of-month above 31
        "* * * 13 *",  # month above 12
        "* * * * 7",  # dow above 6
        "*/0 * * * *",  # zero step
        "5-1 * * * *",  # inverted range
        "abc * * * *",  # non-numeric
    ],
)
def test_parse_rejects_invalid(expr: str) -> None:
    with pytest.raises(CronValidationError):
        parse_cron(expr)


# ── get_next_fire_time: basic scheduling ─────────────────────────────────────


def test_next_fire_is_strictly_after() -> None:
    p = parse_cron("*/5 * * * *")
    # exactly on a fire minute -> next is the following slot, not this one
    got = get_next_fire_time(p, _dt(2026, 1, 1, 0, 5), UTC)
    assert got == _dt(2026, 1, 1, 0, 10)


def test_next_fire_daily() -> None:
    p = parse_cron("0 9 * * *")
    got = get_next_fire_time(p, _dt(2026, 3, 10, 12, 0), UTC)
    assert got == _dt(2026, 3, 11, 9, 0)


def test_next_fire_specific_month_and_day() -> None:
    p = parse_cron("0 0 25 12 *")
    got = get_next_fire_time(p, _dt(2026, 6, 1, 0, 0), UTC)
    assert got == _dt(2026, 12, 25, 0, 0)


def test_next_fire_rolls_to_next_year() -> None:
    p = parse_cron("0 0 1 1 *")
    got = get_next_fire_time(p, _dt(2026, 6, 1, 0, 0), UTC)
    assert got == _dt(2027, 1, 1, 0, 0)


# ── POSIX DOM/DOW union semantics ────────────────────────────────────────────


def test_dom_and_dow_both_restricted_is_union() -> None:
    # Fire on the 13th OR any Friday. 2026-02-13 is a Friday (both match);
    # the next distinct match walking forward should include a non-13th Friday.
    p = parse_cron("0 0 13 * 5")
    # From Feb 1 2026: first Friday is Feb 6 (dow match), before the 13th.
    got = get_next_fire_time(p, _dt(2026, 2, 1, 0, 0), UTC)
    assert got == _dt(2026, 2, 6, 0, 0)


def test_dom_only_restricted_ignores_dow() -> None:
    # Only DOM restricted -> fires on the 15th regardless of weekday.
    p = parse_cron("0 0 15 * *")
    got = get_next_fire_time(p, _dt(2026, 1, 1, 0, 0), UTC)
    assert got == _dt(2026, 1, 15, 0, 0)


def test_dow_only_restricted_ignores_dom() -> None:
    # Only DOW restricted -> fires every Monday. 2026-01-05 is a Monday.
    p = parse_cron("0 0 * * 1")
    got = get_next_fire_time(p, _dt(2026, 1, 1, 0, 0), UTC)
    assert got == _dt(2026, 1, 5, 0, 0)


# ── never-fires / bail-out ───────────────────────────────────────────────────


def test_never_fires_returns_none() -> None:
    # Feb 31 never exists.
    p = parse_cron("0 0 31 2 *")
    assert get_next_fire_time(p, _dt(2026, 1, 1, 0, 0), UTC) is None


# ── timezone evaluation ──────────────────────────────────────────────────────


def test_fires_in_task_timezone_not_utc() -> None:
    la = ZoneInfo("America/Los_Angeles")
    p = parse_cron("0 9 * * *")  # 9am local
    # Start from a UTC instant; result is 09:00 LA wall-clock.
    got = get_next_fire_time(p, _dt(2026, 3, 10, 0, 0), la)
    assert got.hour == 9
    assert got.tzinfo == la
    # 2026-03-10 is after US DST begins (Mar 8) -> PDT (UTC-7) -> 16:00 UTC.
    assert got.astimezone(UTC).hour == 16


def test_default_timezone_is_utc_semantics() -> None:
    p = parse_cron("0 12 * * *")
    got = get_next_fire_time(p, _dt(2026, 1, 1, 0, 0), UTC)
    assert got == _dt(2026, 1, 1, 12, 0)


# ── validate_cron: floor + never/once ────────────────────────────────────────


def test_validate_accepts_hourly_cadence() -> None:
    # Top of every hour = exactly 3600s. The floor is a strict `<`, so a gap
    # equal to the floor passes: `3600 < 3600` is False.
    trig = validate_cron("0 * * * *")
    assert trig.expression == "0 * * * *"
    # trigger can compute a next fire
    assert trig.next_fire_after(_dt(2026, 1, 1, 0, 0), UTC) == _dt(2026, 1, 1, 1, 0)


def test_validate_rejects_every_minute() -> None:
    with pytest.raises(CronValidationError):
        validate_cron("* * * * *")


def test_validate_rejects_five_minute_cadence() -> None:
    # every 5 minutes = 300s < 3600s floor (the old accepted cadence).
    with pytest.raises(CronValidationError):
        validate_cron("*/5 * * * *")


def test_validate_rejects_half_hour_cadence() -> None:
    # every 30 minutes = 1800s < 3600s floor.
    with pytest.raises(CronValidationError):
        validate_cron("*/30 * * * *")


def test_validate_rejects_sub_floor_step() -> None:
    # every 4 minutes = 240s < 3600s floor
    with pytest.raises(CronValidationError):
        validate_cron("*/4 * * * *")


def test_validate_rejects_irregular_sub_floor_pair() -> None:
    # Fires at :00 and :01 each hour -> consecutive gaps alternate
    # [3540s, 60s, 3540s, ...]. The tightest pair (60s) is well under the
    # 3600s floor, but it is never the *first* pair, so a validator that only
    # measures the first gap would wrongly accept this. Rejection must not
    # depend on the wall-clock minute validation happens to run at.
    with pytest.raises(CronValidationError):
        validate_cron("0,1 * * * *")


def test_validate_rejects_never_fires() -> None:
    with pytest.raises(CronValidationError):
        validate_cron("0 0 31 2 *")


def test_validate_rejects_fires_only_once_in_window() -> None:
    # Feb 29 fires only on leap years -> no second fire within 366 days.
    with pytest.raises(CronValidationError):
        validate_cron("0 0 29 2 *")


def test_min_interval_constant() -> None:
    assert MIN_INTERVAL_SECONDS == 3600
