"""Tests for the daily run counts behind an automation's run history chart."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from omnigent.db.enum_codecs import SCHEDULED_TASK_RUN_STATUS
from omnigent.server.scheduled.run_stats import daily_run_counts, run_stats_days


def _epoch(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def test_days_are_whole_local_days_across_a_clock_change() -> None:
    """The days end today in the timezone, and a 25-hour day is still one day."""
    los_angeles = ZoneInfo("America/Los_Angeles")
    # 3 November, noon in Los Angeles; its clocks went back on 1 November.
    now = _epoch(2026, 11, 3, 20)

    dates, starts = run_stats_days(now, los_angeles, 3)

    assert dates == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]
    # Midnight on 1 November is still daylight time (UTC-7); the later ones are UTC-8.
    assert starts == [
        _epoch(2026, 11, 1, 7),
        _epoch(2026, 11, 2, 8),
        _epoch(2026, 11, 3, 8),
        _epoch(2026, 11, 4, 8),
    ]


def test_days_use_the_timezones_date_not_the_servers() -> None:
    # 23:30 UTC on 3 November is already 4 November in Tokyo.
    dates, starts = run_stats_days(_epoch(2026, 11, 3, 23, 30), ZoneInfo("Asia/Tokyo"), 1)

    assert dates == [date(2026, 11, 4)]
    assert starts == [_epoch(2026, 11, 3, 15), _epoch(2026, 11, 4, 15)]


def test_runs_count_on_the_local_day_they_were_scheduled_for_by_outcome() -> None:
    # Noon on 4 November in Kolkata, which is UTC+5:30: its days start at 18:30 UTC.
    dates, starts = run_stats_days(_epoch(2026, 11, 4, 6, 30), ZoneInfo("Asia/Kolkata"), 3)
    midnight = _epoch(2026, 11, 2, 18, 30)
    assert starts[1] == midnight

    days, truncated = daily_run_counts(
        [
            # Queued, starting and running runs haven't finished.
            (midnight + 9 * 3600, "running"),
            (midnight + 8 * 3600, "starting"),
            (midnight + 7 * 3600, "scheduled"),
            (midnight + 6 * 3600, "skipped"),
            (midnight, "failed"),
            (midnight, "succeeded"),
            # A status this release doesn't know.
            (midnight, "paused"),
            # The last second of 2 November, and a run from before the days reported.
            (midnight - 1, "succeeded"),
            (midnight - 3 * 86400, "succeeded"),
        ],
        dates,
        starts,
        limit=100,
    )

    assert not truncated
    assert days == [
        {"date": "2026-11-02", "succeeded": 1, "failed": 0, "skipped": 0, "in_progress": 0},
        {"date": "2026-11-03", "succeeded": 1, "failed": 1, "skipped": 1, "in_progress": 3},
        {"date": "2026-11-04", "succeeded": 0, "failed": 0, "skipped": 0, "in_progress": 0},
    ]


def test_every_run_status_counts_under_an_outcome() -> None:
    """A status added to the store is never dropped from the counts unnoticed."""
    dates, starts = run_stats_days(_epoch(2026, 11, 4, 12), ZoneInfo("UTC"), 1)

    for status in SCHEDULED_TASK_RUN_STATUS:
        [day], _ = daily_run_counts([(starts[0], status)], dates, starts, limit=1)
        assert sum(count for outcome, count in day.items() if outcome != "date") == 1, status


def test_a_range_with_more_runs_than_the_limit_counts_its_newest() -> None:
    dates, starts = run_stats_days(_epoch(2026, 11, 4, 12), ZoneInfo("UTC"), 4)
    runs = [
        (_epoch(2026, 11, 4, 9), "succeeded"),
        (_epoch(2026, 11, 3, 9), "failed"),
        (_epoch(2026, 11, 3, 8), "succeeded"),
        # Past the limit, though scheduled on the same day as the last run counted.
        (_epoch(2026, 11, 3, 7), "succeeded"),
        (_epoch(2026, 11, 1, 9), "succeeded"),
    ]

    days, truncated = daily_run_counts(runs, dates, starts, limit=3)

    assert truncated
    # 1 and 2 November were never read, so they are left out instead of shown empty.
    assert days == [
        {"date": "2026-11-03", "succeeded": 1, "failed": 1, "skipped": 0, "in_progress": 0},
        {"date": "2026-11-04", "succeeded": 1, "failed": 0, "skipped": 0, "in_progress": 0},
    ]

    # A range that holds exactly the limit is counted whole.
    days, truncated = daily_run_counts(runs[:3], dates, starts, limit=3)
    assert not truncated
    assert [day["date"] for day in days] == [day.isoformat() for day in dates]
