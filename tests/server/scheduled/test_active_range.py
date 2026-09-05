"""Tests for the active-range gate on RRULE occurrences.

Exercises :func:`parse_time_of_day`, :class:`ActiveRange` boundary/overnight
semantics, :func:`validate_active_range` rejections, and the bounded
skip-search in :func:`next_fire_in_active_range` / :func:`assert_fires_within_range`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from omnigent.server.scheduled.active_range import (
    ActiveRangeValidationError,
    assert_fires_within_range,
    next_fire_in_active_range,
    parse_time_of_day,
    validate_active_range,
)
from omnigent.server.scheduled.rrule import RRuleTrigger

UTC = ZoneInfo("UTC")


def _dt(y: int, mo: int, d: int, h: int = 0, mi: int = 0, tz: ZoneInfo = UTC) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=tz)


# ── parse_time_of_day ─────────────────────────────────────────────────────────


def test_parse_time_of_day_returns_minutes_since_midnight() -> None:
    assert parse_time_of_day("00:00") == 0
    assert parse_time_of_day("09:00") == 540
    assert parse_time_of_day("23:59") == 1439


@pytest.mark.parametrize(
    "value",
    ["9:00", "25:00", "09:60", "", "9am", "09:0", "24:00"],
)
def test_parse_time_of_day_rejects_bad_format(value: str) -> None:
    with pytest.raises(ActiveRangeValidationError):
        parse_time_of_day(value)


def test_parse_time_of_day_rejects_non_string() -> None:
    with pytest.raises(ActiveRangeValidationError):
        parse_time_of_day(540)  # type: ignore[arg-type]


# ── ActiveRange.contains ───────────────────────────────────────────────────────


def test_daytime_range_boundary_inclusive_at_both_ends() -> None:
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    assert rng.contains(_dt(2026, 1, 1, 9, 0))
    assert rng.contains(_dt(2026, 1, 1, 17, 0))


def test_daytime_range_excludes_just_outside_both_ends() -> None:
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    assert not rng.contains(_dt(2026, 1, 1, 8, 59))
    assert not rng.contains(_dt(2026, 1, 1, 17, 1))


def test_overnight_range_wraps_midnight() -> None:
    rng = validate_active_range("22:00", "06:00")
    assert rng is not None
    assert rng.contains(_dt(2026, 1, 1, 23, 0))
    assert rng.contains(_dt(2026, 1, 1, 2, 0))
    assert rng.contains(_dt(2026, 1, 1, 6, 0))
    assert not rng.contains(_dt(2026, 1, 1, 7, 0))
    assert not rng.contains(_dt(2026, 1, 1, 21, 59))


# ── validate_active_range ──────────────────────────────────────────────────────


def test_validate_active_range_none_when_both_unset() -> None:
    assert validate_active_range(None, None) is None


def test_validate_active_range_rejects_one_sided() -> None:
    with pytest.raises(ActiveRangeValidationError):
        validate_active_range("09:00", None)
    with pytest.raises(ActiveRangeValidationError):
        validate_active_range(None, "17:00")


def test_validate_active_range_rejects_bad_format() -> None:
    with pytest.raises(ActiveRangeValidationError):
        validate_active_range("9:00", "17:00")


def test_validate_active_range_rejects_equal_start_and_end() -> None:
    with pytest.raises(ActiveRangeValidationError):
        validate_active_range("09:00", "09:00")


# ── next_fire_in_active_range ──────────────────────────────────────────────────


def test_no_range_delegates_to_next_fire_after() -> None:
    trigger = RRuleTrigger(rule="FREQ=HOURLY")
    after = _dt(2026, 1, 1, 8, 30)
    assert next_fire_in_active_range(trigger, after, UTC, None) == trigger.next_fire_after(
        after, UTC
    )


def test_hourly_skips_to_next_day_after_close_of_range() -> None:
    trigger = RRuleTrigger(rule="FREQ=HOURLY")
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    got = next_fire_in_active_range(trigger, _dt(2026, 1, 1, 17, 0), UTC, rng)
    assert got == _dt(2026, 1, 2, 9, 0)


def test_overnight_range_admits_early_morning_and_late_night_fires() -> None:
    trigger = RRuleTrigger(rule="FREQ=HOURLY")
    rng = validate_active_range("22:00", "06:00")
    assert rng is not None
    assert next_fire_in_active_range(trigger, _dt(2026, 1, 1, 22, 30), UTC, rng) == _dt(
        2026, 1, 1, 23, 0
    )
    assert next_fire_in_active_range(trigger, _dt(2026, 1, 2, 1, 30), UTC, rng) == _dt(
        2026, 1, 2, 2, 0
    )


def test_exhausted_trigger_returns_none() -> None:
    # COUNT=1 fires once, outside the range, then the trigger is exhausted.
    trigger = RRuleTrigger(rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0;COUNT=1")
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    assert next_fire_in_active_range(trigger, _dt(2026, 1, 1, 0, 0), UTC, rng) is None


def test_search_gives_up_when_never_in_range_within_horizon() -> None:
    # Fires every day at 03:00, forever, never once landing in 09:00-17:00.
    trigger = RRuleTrigger(rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0")
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    assert next_fire_in_active_range(trigger, _dt(2026, 1, 1, 0, 0), UTC, rng) is None


# ── assert_fires_within_range ───────────────────────────────────────────────────


def test_assert_fires_within_range_passes_for_reachable_schedule() -> None:
    trigger = RRuleTrigger(rule="FREQ=HOURLY")
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    assert_fires_within_range(trigger, rng, UTC)


def test_assert_fires_within_range_raises_when_never_reachable() -> None:
    trigger = RRuleTrigger(rule="FREQ=DAILY;BYHOUR=3;BYMINUTE=0")
    rng = validate_active_range("09:00", "17:00")
    assert rng is not None
    with pytest.raises(ActiveRangeValidationError):
        assert_fires_within_range(trigger, rng, UTC)
