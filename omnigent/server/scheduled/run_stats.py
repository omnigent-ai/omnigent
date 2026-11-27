"""Daily outcome counts of a scheduled task's runs, for its run history chart."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Literal, get_args
from zoneinfo import ZoneInfo

DEFAULT_RUN_STATS_DAYS = 28
MAX_RUN_STATS_DAYS = 90
# The most runs one read counts. A range that holds more counts its newest.
MAX_RUN_STATS_RUNS = 10_000

RunOutcome = Literal["succeeded", "failed", "skipped", "in_progress"]
RUN_OUTCOMES: tuple[RunOutcome, ...] = get_args(RunOutcome)

# A run that hasn't finished, whether queued, starting or running, is in progress.
_STATUS_OUTCOMES: dict[str, RunOutcome] = {
    "succeeded": "succeeded",
    "failed": "failed",
    "skipped": "skipped",
    "scheduled": "in_progress",
    "starting": "in_progress",
    "running": "in_progress",
}


def run_stats_days(now: float, timezone: ZoneInfo, days: int) -> tuple[list[date], list[int]]:
    """Return the last *days* calendar days in *timezone* and when each starts.

    :param now: The current time, in epoch seconds.
    :param timezone: The timezone whose calendar days to use.
    :param days: How many days to cover, today included.
    :returns: ``(dates, starts)``: the days oldest first, and the epoch seconds
        each one starts at, followed by those today ends at.
    """
    today = datetime.fromtimestamp(now, timezone).date()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    starts = [
        int(datetime.combine(day, time.min, timezone).timestamp())
        for day in (*dates, today + timedelta(days=1))
    ]
    return dates, starts


def daily_run_counts(
    runs: Sequence[tuple[int, str]],
    dates: list[date],
    starts: list[int],
    limit: int,
) -> tuple[list[dict[str, int | str]], bool]:
    """Count the first *limit* of *runs* per day and outcome.

    :param runs: ``(scheduled_at, status)`` of each run, newest first, as
        ``list_run_statuses`` returns them.
    :param dates: The days to report, oldest first; a day with no runs gets zeros.
    :param starts: When each day starts and the last one ends, as
        ``run_stats_days`` returns them.
    :param limit: The most runs to count.
    :returns: ``(days, truncated)``: one ``{"date", "succeeded", "failed",
        "skipped", "in_progress"}`` row per day, with ``date`` in ISO format,
        and whether *runs* holds more than *limit*. The rows then start at the
        day of the oldest run counted, which is counted in part.
    """
    counted = runs[:limit]
    truncated = len(runs) > limit
    counts = [dict.fromkeys(RUN_OUTCOMES, 0) for _ in dates]
    for scheduled_at, status in counted:
        day = bisect_right(starts, scheduled_at) - 1
        outcome = _STATUS_OUTCOMES.get(status)
        if 0 <= day < len(dates) and outcome is not None:
            counts[day][outcome] += 1
    # Days before the oldest run counted were never read.
    first = max(bisect_right(starts, counted[-1][0]) - 1, 0) if truncated and counted else 0
    return [
        {"date": day.isoformat(), **count}
        for day, count in zip(dates[first:], counts[first:], strict=True)
    ], truncated
