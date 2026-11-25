"""The items behind a status number: what each day and the total counted, and what they left out.

Every value comes from the same :class:`~omnigent.server.project_kpis.MetricSeries`
the Summary and KPI tiles use, aggregated by the same code, so a drill-down always
agrees with the number it opened from. For count metrics, each day lists one row
per distinct ticket or PR, so a day's row count equals its value. A ticket that
counts on two days appears on both, and the total counts it once, on its latest
day (``counted_in_total``).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from omnigent.server.project_kpis import (
    DAY,
    MetricSeries,
    SeriesItem,
    _day_start,
    _in_range,
    aggregate,
    item_day,
    item_epoch,
    trend_since_day,
)
from omnigent.server.project_status import MemberKey, StatusPeriod, status_period
from omnigent.server.project_status_spend import micros_to_cents

DrilldownRange = Literal["7d", "30d", "all", "trend"]
#: Most rows returned per list; ``truncated`` says when more exist.
ITEMS_MAX = 500


def range_bounds(range_id: DrilldownRange, period: StatusPeriod) -> tuple[int | None, str | None]:
    """The range's inclusive start as ``(epoch, day)``; ``trend`` is the KPI tiles' 8 days."""
    if range_id == "trend":
        first = trend_since_day(period.today_utc)
        return _day_start(first), first
    window = status_period(range_id, period.until_epoch)
    return window.since_epoch, window.since_day_utc


def _row(item: SeriesItem, metric_kind: str) -> dict[str, Any]:
    value: float | None = None
    if metric_kind == "daily_sum":
        value = micros_to_cents(int(item.value or 0)) / 100
    elif item.value is not None:
        value = round(item.value, 1)
    return {
        "day_utc": item_day(item),
        "at": item.at,
        "kind": item.kind,
        "id": item.key,
        "member": item.member,
        "value": value,
        **({"detail": item.detail} if item.detail else {}),
    }


def _latest_per_key(items: list[SeriesItem]) -> dict[str, SeriesItem]:
    latest: dict[str, SeriesItem] = {}
    for item in sorted(items, key=lambda entry: item_epoch(entry) or 0):
        latest[item.key] = item
    return latest


def drilldown(
    series: MetricSeries,
    period: StatusPeriod,
    range_id: DrilldownRange,
    member: MemberKey | None = None,
) -> dict[str, Any]:
    """Per-day values, the total, the counted rows and the excluded rows for one metric.

    :param member: Only what counts for this member, as in the Summary's member table.
    """
    since, since_day = range_bounds(range_id, period)
    until = period.until_epoch + 1
    in_range = [item for item in series.items if _in_range(item_epoch(item), since, until)]
    if series.kind == "distinct":
        # A key counts once, for whoever made its latest move in the range.
        credited = _latest_per_key(in_range)
        if member is not None:
            in_range = [item for item in in_range if credited[item.key].member == member]
        total = aggregate(series.kind, in_range)
    else:
        if member is not None:
            in_range = [item for item in in_range if item.member == member]
        total = aggregate(series.kind, in_range)
        credited = {}
    first_day = since_day or min(
        (day for item in in_range if (day := item_day(item)) is not None),
        default=period.today_utc,
    )
    days = []
    day = date.fromisoformat(first_day)
    today = date.fromisoformat(period.today_utc)
    by_day: dict[str, list[SeriesItem]] = {}
    for item in in_range:
        key = item_day(item)
        if key is not None:
            by_day.setdefault(key, []).append(item)
    while day <= today:
        iso = day.isoformat()
        start = _day_start(iso)
        day_items = [
            item
            for item in by_day.get(iso, [])
            if _in_range(item_epoch(item), start, min(start + DAY, until))
        ]
        days.append(
            {
                "day_utc": iso,
                "value": aggregate(series.kind, day_items).value,
                "items": len({item.key for item in day_items})
                if series.kind == "distinct"
                else len(day_items),
            }
        )
        day += timedelta(days=1)
    rows: list[dict[str, Any]] = []
    if series.kind == "distinct":
        # One row per key per day: its latest move that day.
        seen: dict[tuple[str | None, str], SeriesItem] = {}
        for item in sorted(in_range, key=lambda entry: item_epoch(entry) or 0):
            seen[(item_day(item), item.key)] = item
        for (_, key), item in seen.items():
            rows.append({**_row(item, series.kind), "counted_in_total": credited[key] is item})
    else:
        rows = [{**_row(item, series.kind), "counted_in_total": True} for item in in_range]
    rows.sort(key=lambda row: (row["day_utc"] or "", row["at"] or 0), reverse=True)
    excluded = [
        {**_row(entry.item, series.kind), "reason": entry.reason}
        for entry in series.excluded
        if (not entry.only_when_bounded or since is not None)
        and (entry.only_when_bounded or _in_range(item_epoch(entry.item), since, until))
        and (member is None or entry.item.member == member)
    ]
    return {
        "since_day_utc": since_day,
        "today_utc": period.today_utc,
        "total": total.value,
        "sample_size": total.sample_size,
        "days": days,
        "items": rows[:ITEMS_MAX],
        "excluded": excluded[:ITEMS_MAX],
        "truncated": len(rows) > ITEMS_MAX or len(excluded) > ITEMS_MAX,
    }
