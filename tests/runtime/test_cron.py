"""Tests for the dependency-free 5-field cron matcher (#6).

Locks :func:`omnigent.runtime.cron.next_cron_time` — the only cron primitive
the scheduler engine needs — across the field syntaxes it must support and the
standard-cron edge cases (Sunday as 0/7, the day-of-month/day-of-week OR rule,
leap day) plus malformed-input rejection.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from omnigent.runtime.cron import next_cron_time

_UTC = timezone.utc


@pytest.mark.parametrize(
    ("cron", "base", "expected"),
    [
        pytest.param(
            "* * * * *",
            datetime(2026, 1, 1, 0, 0, 30, tzinfo=_UTC),
            datetime(2026, 1, 1, 0, 1, tzinfo=_UTC),
            id="every-minute-next-boundary",
        ),
        pytest.param(
            "0 22 * * *",
            datetime(2026, 1, 1, 21, 59, tzinfo=_UTC),
            datetime(2026, 1, 1, 22, 0, tzinfo=_UTC),
            id="daily-later-today",
        ),
        pytest.param(
            "0 22 * * *",
            datetime(2026, 1, 1, 22, 0, tzinfo=_UTC),
            datetime(2026, 1, 2, 22, 0, tzinfo=_UTC),
            id="daily-strictly-after-match-rolls-to-tomorrow",
        ),
        pytest.param(
            "0 22 * * FRI",
            datetime(2026, 1, 1, 0, 0, tzinfo=_UTC),  # Thu
            datetime(2026, 1, 2, 22, 0, tzinfo=_UTC),  # Fri
            id="weekday-name",
        ),
        pytest.param(
            "0 22 * * 5",
            datetime(2026, 1, 1, 0, 0, tzinfo=_UTC),
            datetime(2026, 1, 2, 22, 0, tzinfo=_UTC),
            id="weekday-number",
        ),
        pytest.param(
            "*/15 * * * *",
            datetime(2026, 1, 1, 0, 7, tzinfo=_UTC),
            datetime(2026, 1, 1, 0, 15, tzinfo=_UTC),
            id="minute-step",
        ),
        pytest.param(
            "0 9,17 * * *",
            datetime(2026, 1, 1, 10, 0, tzinfo=_UTC),
            datetime(2026, 1, 1, 17, 0, tzinfo=_UTC),
            id="hour-list",
        ),
        pytest.param(
            "0 9 * * 1-5",
            datetime(2026, 1, 3, 12, 0, tzinfo=_UTC),  # Sat
            datetime(2026, 1, 5, 9, 0, tzinfo=_UTC),  # Mon
            id="weekday-range-skips-weekend",
        ),
        pytest.param(
            "0 0 * * 0",
            datetime(2026, 1, 1, 0, 0, tzinfo=_UTC),
            datetime(2026, 1, 4, 0, 0, tzinfo=_UTC),
            id="sunday-as-0",
        ),
        pytest.param(
            "0 0 * * 7",
            datetime(2026, 1, 1, 0, 0, tzinfo=_UTC),
            datetime(2026, 1, 4, 0, 0, tzinfo=_UTC),
            id="sunday-as-7",
        ),
        pytest.param(
            "0 0 29 FEB *",
            datetime(2026, 3, 1, 0, 0, tzinfo=_UTC),
            datetime(2028, 2, 29, 0, 0, tzinfo=_UTC),
            id="leap-day-with-month-name",
        ),
        pytest.param(
            "0 0 1 * 1",  # 1st OR Monday (both restricted -> OR)
            datetime(2026, 1, 2, 0, 0, tzinfo=_UTC),  # Fri
            datetime(2026, 1, 5, 0, 0, tzinfo=_UTC),  # next Monday beats next 1st (Feb)
            id="dom-dow-or-rule",
        ),
    ],
)
def test_next_cron_time(cron: str, base: datetime, expected: datetime) -> None:
    assert next_cron_time(cron, base) == expected


def test_result_preserves_tzinfo() -> None:
    got = next_cron_time("* * * * *", datetime(2026, 1, 1, 0, 0, tzinfo=_UTC))
    assert got.tzinfo == _UTC


@pytest.mark.parametrize(
    "bad",
    [
        "* * * *",  # too few fields
        "* * * * * *",  # too many fields
        "60 * * * *",  # minute out of range
        "0 24 * * *",  # hour out of range
        "0 0 0 * *",  # day-of-month out of range
        "* * * * 8",  # day-of-week out of range
        "0 0 * * MOO",  # unknown name
        "*/0 * * * *",  # non-positive step
        "a b c d e",  # garbage
    ],
)
def test_invalid_cron_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        next_cron_time(bad, datetime(2026, 1, 1, tzinfo=_UTC))
