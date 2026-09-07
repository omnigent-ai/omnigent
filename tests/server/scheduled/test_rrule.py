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
