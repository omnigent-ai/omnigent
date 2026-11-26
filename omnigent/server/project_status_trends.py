"""The Summary's trend charts: per-day (or per-week) values of a few status numbers.

Each bucket's value is the metric over that bucket, exactly as the drill-down's day
for the same metric: distinct tickets filed and finished, changes landed, PRs merged
and spend. ``open_tickets`` is the burndown: tickets not in a completed stage at the
bucket's end, replayed from the stage-move history. "All time" switches to 7-day
buckets ending today once it spans more than ``DAILY_MAX`` days.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from omnigent.server.project_kpis import (
    DAY,
    MetricSeries,
    StageHistories,
    item_day,
    series_value,
)
from omnigent.server.project_kpis import _day_start as day_start
from omnigent.server.project_status import StatusPeriod

#: Longest range shown day by day; longer "All time" ranges use weeks.
DAILY_MAX = 90


@dataclass(frozen=True)
class Bucket:
    """One chart bucket: ``[since, until)``, labelled by its first UTC day."""

    day_utc: str
    since: int
    until: int


def buckets(period: StatusPeriod, earliest_day: str | None) -> tuple[str, list[Bucket]]:
    """The window's buckets and their size, ``day`` or ``week``; today ends at request time."""
    today = date.fromisoformat(period.today_utc)
    first_day = period.since_day_utc or earliest_day or period.today_utc
    first = min(date.fromisoformat(first_day), today)
    span = (today - first).days + 1
    size = "day" if span <= DAILY_MAX else "week"
    step = 1 if size == "day" else 7
    starts: list[date] = []
    start = today - timedelta(days=step - 1)
    while start + timedelta(days=step - 1) >= first:
        starts.append(start)
        start -= timedelta(days=step)
    end = period.until_epoch + 1
    result = []
    for begin in reversed(starts):
        since = day_start(begin.isoformat())
        result.append(Bucket(begin.isoformat(), since, min(since + step * DAY, end)))
    return size, result


def open_tickets_at(histories: StageHistories, moments: list[int]) -> list[int]:
    """Tickets not in a completed stage at each moment (ascending), from their stage moves."""
    changes: list[tuple[int, int]] = []
    for history in histories.values():
        was_open = False
        for entry in history:
            now_open = not entry["is_complete"]
            if now_open != was_open:
                changes.append((entry["at"], 1 if now_open else -1))
                was_open = now_open
    changes.sort()
    times = [at for at, _ in changes]
    running = [0]
    for _, delta in changes:
        running.append(running[-1] + delta)
    # A moment counts the moves made strictly before it.
    return [running[bisect.bisect_left(times, moment)] for moment in moments]


def earliest_day(series: Iterable[MetricSeries], histories: StageHistories) -> str | None:
    """The first UTC day with any data, for "All time"'s first bucket."""
    days = [day for one in series for item in one.items if (day := item_day(item))]
    firsts = [history[0]["at"] for history in histories.values() if history]
    if firsts:
        days.append(datetime.fromtimestamp(min(firsts), tz=UTC).date().isoformat())
    return min(days, default=None)


def trend_rows(
    period: StatusPeriod,
    series: dict[str, MetricSeries | None],
    histories: StageHistories,
) -> dict[str, Any]:
    """Every bucket's values; a metric whose series is ``None`` or unavailable is null."""
    size, chart = buckets(period, earliest_day((s for s in series.values() if s), histories))
    opened = open_tickets_at(histories, [bucket.until for bucket in chart])
    rows = []
    for bucket, open_count in zip(chart, opened, strict=True):
        row: dict[str, Any] = {"day_utc": bucket.day_utc, "open_tickets": open_count}
        for name, one in series.items():
            value = None if one is None else series_value(one, bucket.since, bucket.until)
            row[name] = None if value is None or not value.available else value.value
        rows.append(row)
    return {"bucket": size, "days": rows}
