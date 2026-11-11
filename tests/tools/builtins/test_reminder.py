"""Tests for the ``sys_reminder_set`` schema and its time and rule helpers.

The rule a reminder saves must fire exactly once, at the requested minute, under
the database scheduler's creation-anchored ``COUNT``; these tests check that
against the scheduler's own :func:`get_next_fire_time`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from omnigent.server.scheduled.rrule import get_next_fire_time, validate_rrule
from omnigent.tools.builtins.scheduled_tasks import (
    SysReminderSetTool,
    reminder_name,
    reminder_prompt,
    reminder_rrule,
    resolve_reminder_time,
)

_NOW = datetime(2026, 11, 10, 22, 15, 30, tzinfo=UTC)
_JUNE_2027 = datetime(2027, 6, 1, tzinfo=UTC)
_WHOLE_MINUTE = datetime(2026, 11, 10, 22, 15, tzinfo=UTC)


def test_delay_rounds_up_to_the_next_minute() -> None:
    assert resolve_reminder_time({"delay_seconds": 2700}, _NOW) == datetime(
        2026, 11, 10, 23, 1, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "now,args,expected",
    [
        # Exactly one minute from a whole minute.
        (_WHOLE_MINUTE, {"delay_seconds": 60}, _WHOLE_MINUTE + timedelta(minutes=1)),
        # Exactly the largest delay from a whole minute.
        (_WHOLE_MINUTE, {"delay_seconds": 364 * 86400}, _WHOLE_MINUTE + timedelta(days=364)),
        # The largest delay from mid-minute rounds up past the limit and is still
        # accepted; the limit applies before rounding.
        (_NOW, {"delay_seconds": 364 * 86400}, datetime(2027, 11, 9, 22, 16, tzinfo=UTC)),
        (_WHOLE_MINUTE, {"at": "2026-11-10T22:16:00Z"}, _WHOLE_MINUTE + timedelta(minutes=1)),
    ],
)
def test_exact_limits_are_accepted(now: datetime, args: dict[str, object], expected: datetime):
    assert resolve_reminder_time(args, now) == expected


def test_at_is_converted_to_utc() -> None:
    when = resolve_reminder_time({"at": "2026-11-12T15:00:00-08:00"}, _NOW)
    assert when == datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
    assert resolve_reminder_time({"at": "2026-11-12T23:00:00Z"}, _NOW) == when


@pytest.mark.parametrize(
    "args,error",
    [
        ({}, "exactly one"),
        ({"at": "2026-11-12T15:00:00Z", "delay_seconds": 60}, "exactly one"),
        ({"at": "2026-11-12T15:00:00"}, "UTC offset"),
        ({"at": "tomorrow"}, "not an ISO-8601"),
        ({"at": 5}, "ISO-8601 string"),
        ({"delay_seconds": True}, "must be a number"),
        ({"delay_seconds": "60"}, "must be a number"),
        ({"delay_seconds": float("nan")}, "finite"),
        ({"delay_seconds": 10}, "at least one minute"),
        ({"delay_seconds": 364 * 86400 + 1}, "at most 364 days"),
        ({"at": "2026-11-10T22:00:00Z"}, "at least one minute"),
        ({"at": "2027-11-10T22:15:00Z"}, "at most 364 days"),
        # Values that would overflow a float or a timedelta before the range check.
        ({"delay_seconds": 10**400}, "at most 364 days"),
        ({"delay_seconds": -(10**400)}, "at least one minute"),
        ({"delay_seconds": -1e300}, "at least one minute"),
        ({"delay_seconds": 1e300}, "at most 364 days"),
        ({"at": "0001-01-01T00:00:00+05:00"}, "outside the supported date range"),
        ({"at": "9999-12-31T23:59:59-05:00"}, "outside the supported date range"),
        # Rounding up would overflow here, but the limit is checked first.
        ({"at": "9999-12-31T23:59:59Z"}, "at most 364 days"),
    ],
)
def test_invalid_times_are_rejected(args: dict[str, object], error: str) -> None:
    result = resolve_reminder_time(args, _NOW)
    assert isinstance(result, str)
    assert error in result


@pytest.mark.parametrize(
    "created,delay",
    [
        (_NOW, timedelta(minutes=2)),
        # Crosses a year boundary.
        (datetime(2026, 12, 31, 23, 50, tzinfo=UTC), timedelta(minutes=20)),
        # Same calendar date as creation, a few minutes earlier, next year.
        (_NOW, timedelta(days=364) - timedelta(minutes=30)),
        # A leap day, which a yearly rule reaches only every fourth year.
        (_JUNE_2027, datetime(2028, 2, 29, 9, tzinfo=UTC) - _JUNE_2027),
    ],
)
def test_rule_fires_once_at_the_requested_minute(created: datetime, delay: timedelta) -> None:
    when = resolve_reminder_time({"delay_seconds": delay.total_seconds()}, created)
    assert isinstance(when, datetime)
    rule = reminder_rrule(when)
    validate_rrule(rule, session_mode="continue", allow_single_fire=True)
    # The task is created a moment after the time is resolved.
    anchor = created + timedelta(seconds=2)
    assert get_next_fire_time(rule, anchor, UTC, anchor=anchor) == when
    assert get_next_fire_time(rule, when, UTC, anchor=anchor) is None


def test_rule_ignores_the_callers_offset() -> None:
    # 09:30 at +09:00 is 00:30 UTC; the rule must name the UTC calendar fields
    # even when handed a time that hasn't been converted yet.
    when = datetime(2026, 11, 11, 9, 30, tzinfo=timezone(timedelta(hours=9)))
    assert reminder_rrule(when) == (
        "FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=11;BYHOUR=0;BYMINUTE=30;BYSECOND=0;COUNT=1"
    )


def test_prompt_and_name() -> None:
    when = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
    assert reminder_prompt("Check CI on PR #12.", when) == (
        "[Reminder set for 2026-11-12 23:00 UTC]\nCheck CI on PR #12."
    )
    assert reminder_name("Check  CI\non PR #12.") == "Reminder: Check CI on PR #12."
    long_name = reminder_name("x" * 200)
    assert len(long_name) == len("Reminder: ") + 60
    assert long_name.endswith("...")


def test_schema_requires_only_the_message() -> None:
    schema = SysReminderSetTool().get_schema()["function"]
    assert schema["name"] == "sys_reminder_set"
    params = schema["parameters"]
    assert params["required"] == ["message"]
    assert set(params["properties"]) == {"message", "at", "delay_seconds"}
    assert params["additionalProperties"] is False
