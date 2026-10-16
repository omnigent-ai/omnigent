"""Tests for the RRULE (RFC 5545) next-fire computation and interval validator.

Exercises :func:`get_next_fire_time` and :func:`validate_rrule` — including
strict-after semantics, timezone/DST evaluation, rule exhaustion, the
never-fires / fires-once bail-outs, and the one-hour minimum-interval floor
(sampled from a fixed anchor so the verdict is wall-clock-independent).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from omnigent.server.scheduled.rrule import (
    MIN_INTERVAL_SECONDS,
    RRuleValidationError,
    get_next_fire_time,
    validate_rrule,
)

UTC = ZoneInfo("UTC")


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0, tz: ZoneInfo = UTC) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# ── get_next_fire_time: basic scheduling ─────────────────────────────────────


def test_next_fire_is_strictly_after() -> None:
    # Exactly on a fire instant -> next is the following slot, not this one.
    got = get_next_fire_time("FREQ=HOURLY", _dt(2026, 1, 1, 1, 0), UTC)
    assert got == _dt(2026, 1, 1, 2, 0)


def test_next_fire_hourly_lands_on_the_hour() -> None:
    # Anchored at midnight, an hourly rule fires on the hour regardless of the
    # sub-hour query instant.
    got = get_next_fire_time("FREQ=HOURLY", _dt(2026, 1, 1, 0, 30), UTC)
    assert got == _dt(2026, 1, 1, 1, 0)


def test_next_fire_daily() -> None:
    got = get_next_fire_time("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", _dt(2026, 3, 10, 12, 0), UTC)
    assert got == _dt(2026, 3, 11, 9, 0)


def test_next_fire_weekly_weekdays_rolls_over_weekend() -> None:
    # 2026-01-03 is a Saturday; the next weekday fire is Monday the 5th.
    got = get_next_fire_time(
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0",
        _dt(2026, 1, 3, 12, 0),
        UTC,
    )
    assert got == _dt(2026, 1, 5, 9, 0)


# ── rule exhaustion ──────────────────────────────────────────────────────────


def test_exhausted_rule_returns_none() -> None:
    # A COUNT=1 rule whose single occurrence is already in the past has no
    # further fire -> None (RRULE can legitimately end, unlike a bare cron).
    got = get_next_fire_time("FREQ=HOURLY;COUNT=1", _dt(2026, 6, 1, 0, 0), UTC)
    assert got is None


def test_malformed_rule_raises() -> None:
    with pytest.raises(RRuleValidationError):
        get_next_fire_time("not a rule", _dt(2026, 1, 1, 0, 0), UTC)


# ── timezone evaluation ──────────────────────────────────────────────────────


def test_fires_in_task_timezone_not_utc() -> None:
    la = ZoneInfo("America/Los_Angeles")
    # 9am local, daily. Start from a UTC instant; result is 09:00 LA wall-clock.
    got = get_next_fire_time("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", _dt(2026, 3, 10, 0, 0), la)
    assert got.hour == 9
    assert got.tzinfo == la
    # 2026-03-10 is after US DST begins (Mar 8) -> PDT (UTC-7) -> 16:00 UTC.
    assert got.astimezone(UTC).hour == 16


def test_wall_clock_preserved_across_dst_spring_forward() -> None:
    la = ZoneInfo("America/Los_Angeles")
    # Query just before US spring-forward (2026-03-08): the pre-DST fire is
    # PST (UTC-8) and the post-DST fire is PDT (UTC-7), both at 09:00 local.
    before = get_next_fire_time("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", _dt(2026, 3, 6, 20, 0), la)
    after = get_next_fire_time("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", _dt(2026, 3, 8, 20, 0), la)
    assert before.hour == after.hour == 9
    assert before.astimezone(UTC).hour == 17  # PST
    assert after.astimezone(UTC).hour == 16  # PDT


def test_default_timezone_is_utc_semantics() -> None:
    got = get_next_fire_time("FREQ=DAILY;BYHOUR=12;BYMINUTE=0", _dt(2026, 1, 1, 0, 0), UTC)
    assert got == _dt(2026, 1, 1, 12, 0)


# ── validate_rrule: floor + never/once ────────────────────────────────────────


def test_validate_accepts_hourly_cadence() -> None:
    # FREQ=HOURLY = exactly 3600s. The floor is a strict `<`, so a gap equal to
    # the floor passes: `3600 < 3600` is False.
    trig = validate_rrule("FREQ=HOURLY")
    assert trig.rule == "FREQ=HOURLY"
    # The trigger can compute a next fire.
    assert trig.next_fire_after(_dt(2026, 1, 1, 0, 0), UTC) == _dt(2026, 1, 1, 1, 0)


def test_validate_accepts_daily_cadence() -> None:
    trig = validate_rrule("FREQ=DAILY;BYHOUR=9")
    assert trig.rule == "FREQ=DAILY;BYHOUR=9"


def test_validate_accepts_weekly_weekdays() -> None:
    trig = validate_rrule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9")
    assert trig.rule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9"


def test_validate_rejects_every_minute() -> None:
    # FREQ=MINUTELY = 60s < 3600s floor.
    with pytest.raises(RRuleValidationError):
        validate_rrule("FREQ=MINUTELY")


def test_validate_rejects_half_hour_cadence() -> None:
    # FREQ=MINUTELY;INTERVAL=30 = 1800s < 3600s floor.
    with pytest.raises(RRuleValidationError):
        validate_rrule("FREQ=MINUTELY;INTERVAL=30")


def test_validate_rejects_secondly_cadence() -> None:
    # FREQ=SECONDLY = 1s, well under the floor.
    with pytest.raises(RRuleValidationError):
        validate_rrule("FREQ=SECONDLY")


def test_validate_rejects_malformed_rule() -> None:
    with pytest.raises(RRuleValidationError):
        validate_rrule("this is not an rrule")


def test_validate_rejects_fires_only_once() -> None:
    # COUNT=1 -> a single occurrence, so no interval and no recurrence.
    with pytest.raises(RRuleValidationError):
        validate_rrule("FREQ=DAILY;COUNT=1")


def test_validate_rejects_never_fires() -> None:
    # UNTIL in the past (relative to the fixed 2016 anchor) -> zero occurrences.
    with pytest.raises(RRuleValidationError):
        validate_rrule("FREQ=DAILY;UNTIL=20000101T000000Z")


def test_validate_floor_is_deterministic() -> None:
    # Validation samples from a fixed anchor, not the wall clock, so the verdict
    # never depends on when the test runs: a sub-floor rule always rejects and
    # an at-floor rule always passes.
    for _ in range(3):
        with pytest.raises(RRuleValidationError):
            validate_rrule("FREQ=MINUTELY")
        assert validate_rrule("FREQ=HOURLY").rule == "FREQ=HOURLY"


def test_min_interval_constant() -> None:
    assert MIN_INTERVAL_SECONDS == 3600


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;INTERVAL=367",
        "FREQ=MINUTELY;INTERVAL=21961",
        "FREQ=SECONDLY;INTERVAL=1317601",
        "FREQ=DAILY;INTERVAL=0",
        "FREQ=DAILY;COUNT=-1",
        "FREQ=DAILY;COUNT=0",
        "FREQ=DAILY;COUNT=2147483648",
        "FREQ=DAILY;FREQ=WEEKLY",
        "FREQ=DAILY\nRRULE:FREQ=WEEKLY",
        "FREQ=DAILY;BYHOUR=0,1,2,3,4;BYMINUTE=0,1,2,3,4",
    ],
)
def test_rule_limits_apply_to_validation_and_next_fire(rule: str) -> None:
    with pytest.raises(RRuleValidationError):
        validate_rrule(rule)
    with pytest.raises(RRuleValidationError):
        get_next_fire_time(rule, _dt(2026, 1, 1), UTC)


@pytest.mark.parametrize(
    "field",
    [
        "BYMONTH",
        "BYWEEKNO",
        "BYYEARDAY",
        "BYMONTHDAY",
        "BYDAY",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
        "BYSETPOS",
        "BYEASTER",
    ],
)
def test_by_lists_have_a_distinct_value_limit(field: str) -> None:
    values = [f"{i}MO" if field == "BYDAY" else str(i) for i in range(1, 26)]
    rule = f"FREQ=YEARLY;{field}=" + ",".join(values)
    for evaluate in (
        validate_rrule,
        lambda value: get_next_fire_time(value, _dt(2026, 1, 1), UTC),
    ):
        with pytest.raises(RRuleValidationError, match=f"{field} supports at most 24"):
            evaluate(rule)


def test_duplicate_by_values_preserve_occurrences() -> None:
    rule = "FREQ=MONTHLY;BYDAY=+1MO,01MO,1MO;BYHOUR=" + ",".join(["09", "+9", "9"] * 10)
    rule += ";BYSETPOS=01,+1,1"
    validate_rrule(rule)
    assert get_next_fire_time(rule, _dt(2026, 1, 1), UTC) == _dt(2026, 1, 5, 9)


def test_position_selection_consumes_work() -> None:
    from dateutil.rrule import rrulestr

    from omnigent.server.scheduled._bounded_rrule import iter_bounded

    parsed = rrulestr("FREQ=DAILY;BYHOUR=9,11,13;BYSETPOS=1,2,3", dtstart=_dt(2026, 1, 1))
    # The first period and day leave only two position selections available.
    with pytest.raises(ValueError, match="work limit"):
        next(iter_bounded(parsed, max_steps=4, max_seconds=1))


def test_search_stops_at_elapsed_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from itertools import count

    for evaluate in (
        validate_rrule,
        lambda value: get_next_fire_time(value, _dt(2026, 1, 1), UTC),
    ):
        # Advance only the recurrence clock; no real delay or costly input is needed.
        ticks = count()
        monkeypatch.setattr("omnigent.server.scheduled._bounded_rrule.monotonic", ticks.__next__)
        with pytest.raises(RRuleValidationError, match="time limit"):
            evaluate("FREQ=DAILY")


def test_yearly_leap_day_rule_is_supported() -> None:
    rule = "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29"
    validate_rrule(rule)
    assert get_next_fire_time(rule, _dt(2026, 1, 1), UTC) == _dt(2028, 2, 29)


def test_next_fire_search_has_a_finite_horizon() -> None:
    assert get_next_fire_time("FREQ=YEARLY;INTERVAL=10", _dt(2026, 1, 1), UTC) is None


@pytest.mark.parametrize(
    "rule,expected",
    [
        ("FREQ=DAILY;INTERVAL=366", _dt(2027, 1, 2)),
        ("FREQ=DAILY;COUNT=2147483647", _dt(2026, 1, 2)),
        ("FREQ=DAILY;BYHOUR=" + ",".join(map(str, range(24))), _dt(2026, 1, 1, 1)),
        ("FREQ=MINUTELY;INTERVAL=60", _dt(2026, 1, 1, 1)),
        ("FREQ=MINUTELY;INTERVAL=90", _dt(2026, 1, 1, 1, 30)),
        ("FREQ=SECONDLY;INTERVAL=3600", _dt(2026, 1, 1, 1)),
        ("FREQ=MINUTELY;INTERVAL=21960", _dt(2026, 1, 16, 6)),
        ("FREQ=SECONDLY;INTERVAL=1317600", _dt(2026, 1, 16, 6)),
    ],
)
def test_supported_limits_and_persisted_frequencies(rule: str, expected: datetime) -> None:
    trigger = validate_rrule(rule)
    assert trigger.next_fire_after(_dt(2026, 1, 1), UTC) == expected


def test_rule_length_limit_precedes_parsing() -> None:
    rule = "FREQ=DAILY".ljust(4097)
    with pytest.raises(RRuleValidationError, match="at most 4096 characters"):
        validate_rrule(rule)
    with pytest.raises(RRuleValidationError, match="at most 4096 characters"):
        get_next_fire_time(rule, _dt(2026, 1, 1), UTC)


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=HOURLY;BYMONTH=2;BYMONTHDAY=30",
        "FREQ=DAILY;BYHOUR=9;BYSETPOS=2",
        "FREQ=MINUTELY;INTERVAL=60;BYHOUR=9",
        "FREQ=SECONDLY;INTERVAL=3600;BYHOUR=9",
    ],
)
def test_search_stops_at_work_budget(monkeypatch: pytest.MonkeyPatch, rule: str) -> None:
    # A tiny budget checks outer and inner search limits without a long search.
    monkeypatch.setattr("omnigent.server.scheduled.rrule._MAX_SEARCH_STEPS", 2)
    with pytest.raises(RRuleValidationError, match="work limit"):
        validate_rrule(rule)
    with pytest.raises(RRuleValidationError, match="work limit"):
        get_next_fire_time(rule, _dt(2026, 1, 1), UTC)


def test_empty_periods_stop_at_horizon() -> None:
    from dateutil.rrule import rrulestr

    from omnigent.server.scheduled._bounded_rrule import iter_bounded

    parsed = rrulestr(
        "FREQ=DAILY;BYHOUR=9;BYSETPOS=2;UNTIL=20260102T000000Z",
        dtstart=_dt(2026, 1, 1),
    )
    assert list(iter_bounded(parsed, max_steps=7, max_seconds=1)) == []


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29",
        "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=-1",
        "FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=9",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=15",
        "FREQ=HOURLY;INTERVAL=3;BYMINUTE=30",
        "FREQ=MINUTELY;INTERVAL=90;BYSECOND=15",
        "FREQ=SECONDLY;INTERVAL=3600",
        "FREQ=DAILY;BYHOUR=0;UNTIL=20260101T120000Z",
    ],
)
@pytest.mark.parametrize("zone", ["UTC", "Pacific/Kiritimati"])
def test_bounded_iterator_preserves_dateutil_occurrences(rule: str, zone: str) -> None:
    from itertools import islice

    from dateutil.rrule import rrulestr

    from omnigent.server.scheduled._bounded_rrule import iter_bounded

    tz = ZoneInfo(zone)
    start = datetime(2026, 1, 1, 23, tzinfo=tz)
    parsed = rrulestr(rule, dtstart=start)
    assert list(islice(iter_bounded(parsed, max_steps=100_000, max_seconds=1), 5)) == list(
        islice(parsed, 5)
    )
    midnight = start.replace(hour=0)
    assert get_next_fire_time(rule, start, tz) == rrulestr(rule, dtstart=midnight).after(start)
