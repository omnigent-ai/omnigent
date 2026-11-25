"""Compute each KPI metric from the same sources as the Summary page.

Each metric is read once as a dated series, then summed over any UTC range: the
status window, each day of the trend, and the 7 full days ending yesterday that
the on-target check uses (today is still in progress, so it's left out).

- ``merged_prs`` and ``spend_usd`` are the Summary's PRs-merged and spend sources.
- ``tickets_shipped`` counts distinct tickets moved into a completed stage, like
  the Summary's tickets done.
- ``rework_tickets`` counts distinct tickets with a backward move: to an earlier
  stage in the project's current order, or out of a completed stage.
- ``time_to_merged_*`` use tickets now in the final completed stage, dated by
  their latest move there: hours from creation to that move. The median is the
  usual middle value; p90 is nearest-rank.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from omnigent.entities.project_kpi import (
    KPI_METRICS_BY_ID,
    UNMEASURED_METRIC,
    ProjectKpi,
)
from omnigent.server.project_status import MemberTotals, StatusPeriod, StatusSources

if TYPE_CHECKING:
    from omnigent.entities import Project

logger = logging.getLogger(__name__)

KpiStatus = Literal["met", "missed", "no_data", "unavailable", "not_measured"]
StageHistories = dict[str, list[dict[str, Any]]]
SeriesKind = Literal["distinct", "daily_sum", "median", "p90"]
#: Full UTC days, ending yesterday, that the on-target check and trend cover.
TREND_DAYS = 7
DAY = 86_400


@dataclass(frozen=True)
class MetricValue:
    """A metric's value over a range.

    :param value: The number in the metric's unit, or ``None`` with no data.
    :param available: ``False`` when the metric's source is missing or failed.
    :param sample_size: Tickets behind a time metric, else ``None``.
    """

    value: float | None
    available: bool = True
    sample_size: int | None = None


UNAVAILABLE_VALUE = MetricValue(None, available=False)


@dataclass(frozen=True)
class MetricSeries:
    """A metric's dated data, summed over a range by :func:`series_value`.

    :param kind: ``distinct`` counts distinct keys, ``daily_sum`` sums per-day
        amounts, ``median``/``p90`` aggregate samples.
    :param events: ``(epoch, key)`` for ``distinct``; ``None`` epochs are undated
        and count only in a range without a lower bound.
    :param days: UTC day ``"YYYY-MM-DD"`` to amount, for ``daily_sum``.
    :param samples: ``(epoch, value)`` for ``median`` and ``p90``.
    :param scale: Divides ``daily_sum`` amounts, e.g. 100 for cents to dollars.
    :param window_value: Overrides the window's value when only a total is known.
    """

    kind: SeriesKind
    events: list[tuple[int | None, str]] = field(default_factory=list)
    days: dict[str, int] = field(default_factory=dict)
    samples: list[tuple[int, float]] = field(default_factory=list)
    scale: int = 1
    available: bool = True
    dated: bool = True
    window_value: MetricValue | None = None


UNAVAILABLE_SERIES = MetricSeries("distinct", available=False, dated=False)


@dataclass(frozen=True)
class KpiInputs:
    """Lazily read project data; each reader runs at most once per request."""

    project: Project
    period: StatusPeriod
    sources: StatusSources
    stage_histories: Callable[[], StageHistories]
    stages: Callable[[], list[dict[str, Any]]]


def _day_start(day_utc: str) -> int:
    parsed = date.fromisoformat(day_utc)
    return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC).timestamp())


def _in_range(epoch: int | None, since: int | None, until: int) -> bool:
    if epoch is None:
        return since is None
    return (since is None or epoch >= since) and epoch < until


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """The nearest-rank percentile of non-empty *values*, e.g. ``0.9`` for p90."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def series_value(series: MetricSeries, since: int | None, until: int) -> MetricValue:
    """The series' value over ``[since, until)``; ``since=None`` has no lower bound."""
    if not series.available:
        return UNAVAILABLE_VALUE
    if series.kind == "distinct":
        keys = {key for epoch, key in series.events if _in_range(epoch, since, until)}
        return MetricValue(len(keys))
    if series.kind == "daily_sum":
        total = sum(
            amount
            for day, amount in series.days.items()
            if _in_range(_day_start(day), since, until)
        )
        return MetricValue(total / series.scale if series.scale != 1 else total)
    sample = [value for epoch, value in series.samples if _in_range(epoch, since, until)]
    if not sample:
        return MetricValue(None, sample_size=0)
    measure = statistics.median(sample) if series.kind == "median" else nearest_rank(sample, 0.9)
    return MetricValue(round(measure, 1), sample_size=len(sample))


def rework_events(
    histories: StageHistories, stage_order: list[str]
) -> list[tuple[int | None, str]]:
    """Each backward move as ``(epoch, ticket_id)`` (see the module docstring)."""
    rank = {stage_id: index for index, stage_id in enumerate(stage_order)}
    events: list[tuple[int | None, str]] = []
    for ticket_id, history in histories.items():
        for before, after in itertools.pairwise(history):
            reopened = before["is_complete"] and not after["is_complete"]
            earlier = (
                before["stage_id"] in rank
                and after["stage_id"] in rank
                and rank[after["stage_id"]] < rank[before["stage_id"]]
            )
            if reopened or earlier:
                events.append((after["at"], ticket_id))
    return events


def shipped_events(histories: StageHistories) -> list[tuple[int | None, str]]:
    """Each move into a completed stage as ``(epoch, ticket_id)``."""
    return [
        (entry["at"], ticket_id)
        for ticket_id, history in histories.items()
        for entry in history
        if entry["is_complete"] and not entry.get("created")
    ]


def hours_to_final_stage(
    histories: StageHistories, final_stage_id: str | None
) -> list[tuple[int, float]]:
    """``(moved_at, hours)`` from creation to the latest move into *final_stage_id*.

    Only tickets now in that stage with a recorded creation count.
    """
    if final_stage_id is None:
        return []
    samples = []
    for history in histories.values():
        if len(history) < 2 or not history[0].get("created"):
            continue
        last = history[-1]
        if last["stage_id"] == final_stage_id:
            samples.append((last["at"], max(0, last["at"] - history[0]["at"]) / 3600))
    return samples


def _call(source: Any, method: str, *args: Any) -> Any:
    try:
        return getattr(source, method)(*args)
    except Exception:
        logger.warning("KPI source %s failed", method, exc_info=True)
        return None


def _total(result: Any, scale: int = 1) -> MetricValue:
    if not isinstance(result, MemberTotals) or not result.available:
        return UNAVAILABLE_VALUE
    return MetricValue(result.total / scale if scale != 1 else result.total)


def _covering(period: StatusPeriod, trend_since_day: str) -> StatusPeriod:
    """A period covering both the status window and the trend's days."""
    if period.since_day_utc is not None and period.since_day_utc <= trend_since_day:
        return period
    if period.since_day_utc is None:
        return period
    return StatusPeriod(
        period.window,
        period.today_utc,
        trend_since_day,
        _day_start(trend_since_day),
        period.until_epoch,
    )


def trend_since_day(today_utc: str) -> str:
    return (date.fromisoformat(today_utc) - timedelta(days=TREND_DAYS)).isoformat()


def load_series(metric_ids: Iterable[str], inputs: KpiInputs) -> dict[str, MetricSeries]:
    """Each requested metric's series; only the data those metrics need is read."""
    wanted = set(metric_ids) - {UNMEASURED_METRIC}
    project, period = inputs.project, inputs.period
    series: dict[str, MetricSeries] = {}
    if "merged_prs" in wanted:
        source = inputs.sources.prs_merged
        items = (
            None
            if source is None or not hasattr(source, "merged_items")
            else (_call(source, "merged_items", project))
        )
        if items is not None:
            series["merged_prs"] = MetricSeries(
                "distinct",
                events=[
                    (None if item.merged_at is None else int(item.merged_at), item.key)
                    for item in items
                ],
            )
        elif source is not None:
            window = _total(_call(source, "prs_merged", project, period))
            series["merged_prs"] = MetricSeries(
                "distinct", available=window.available, dated=False, window_value=window
            )
        else:
            series["merged_prs"] = UNAVAILABLE_SERIES
    if "spend_usd" in wanted:
        source = inputs.sources.spend
        covering = _covering(period, trend_since_day(period.today_utc))
        breakdown = (
            _call(source, "breakdown", project, covering)
            if source is not None and hasattr(source, "breakdown")
            else None
        )
        if source is not None:
            # The window total is rounded once, like the Summary; the days feed the trend.
            window = _total(_call(source, "spend", project, period), scale=100)
            series["spend_usd"] = MetricSeries(
                "daily_sum",
                days={} if breakdown is None else dict(breakdown.by_day),
                scale=100,
                available=window.available,
                dated=breakdown is not None,
                window_value=window,
            )
        else:
            series["spend_usd"] = UNAVAILABLE_SERIES
    ticket_metrics = wanted & {
        "tickets_shipped",
        "rework_tickets",
        "time_to_merged_median_hours",
        "time_to_merged_p90_hours",
    }
    if ticket_metrics:
        histories = inputs.stage_histories()
        if "tickets_shipped" in ticket_metrics:
            series["tickets_shipped"] = MetricSeries("distinct", events=shipped_events(histories))
        if ticket_metrics - {"tickets_shipped"}:
            stages = inputs.stages()
            if "rework_tickets" in ticket_metrics:
                order = [str(stage["id"]) for stage in stages]
                series["rework_tickets"] = MetricSeries(
                    "distinct", events=rework_events(histories, order)
                )
            final = next(
                (str(stage["id"]) for stage in reversed(stages) if stage.get("is_complete")),
                None,
            )
            samples = hours_to_final_stage(histories, final)
            for metric_id, kind in (
                ("time_to_merged_median_hours", "median"),
                ("time_to_merged_p90_hours", "p90"),
            ):
                if metric_id in ticket_metrics:
                    series[metric_id] = MetricSeries(kind, samples=samples)  # type: ignore[arg-type]
    return series


def window_value(series: MetricSeries, period: StatusPeriod) -> MetricValue:
    """The series' value over the status window (through request time)."""
    if series.window_value is not None:
        return series.window_value
    return series_value(series, period.since_epoch, period.until_epoch + 1)


def compute_metrics(metric_ids: Iterable[str], inputs: KpiInputs) -> dict[str, MetricValue]:
    """Each requested metric's value over the status window."""
    return {
        metric_id: window_value(series, inputs.period)
        for metric_id, series in load_series(metric_ids, inputs).items()
    }


def kpi_status(kpi: ProjectKpi, value: MetricValue | None) -> KpiStatus:
    """Whether a KPI meets its target for a value."""
    if kpi.metric == UNMEASURED_METRIC:
        return "not_measured"
    if value is None or not value.available:
        return "unavailable"
    if value.value is None or kpi.target is None:
        return "no_data"
    if kpi.direction == "at_most":
        return "met" if value.value <= kpi.target else "missed"
    return "met" if value.value >= kpi.target else "missed"


def kpi_trend(
    kpi: ProjectKpi, series: MetricSeries | None, period: StatusPeriod
) -> dict[str, Any]:
    """Per-day values for the 7 full days ending yesterday and today, with the target check."""
    today_start = _day_start(period.today_utc)
    first_day = date.fromisoformat(trend_since_day(period.today_utc))
    dated = series is not None and series.available and series.dated

    def over(since: int, until: int) -> MetricValue:
        if series is None:
            return MetricValue(None)
        if not dated:
            return UNAVAILABLE_VALUE
        return series_value(series, since, until)

    days = []
    for offset in range(TREND_DAYS + 1):
        day = first_day + timedelta(days=offset)
        start = _day_start(day.isoformat())
        until = start + DAY if offset < TREND_DAYS else period.until_epoch + 1
        days.append({"day_utc": day.isoformat(), "value": over(start, until).value})
    last_full = over(_day_start(first_day.isoformat()), today_start)
    today = over(today_start, period.until_epoch + 1)
    return {
        "since_day_utc": first_day.isoformat(),
        "until_day_utc": (date.fromisoformat(period.today_utc) - timedelta(days=1)).isoformat(),
        "days": days,
        "last_7_days": last_full.value,
        "last_7_days_sample_size": last_full.sample_size,
        "today": today.value,
        "status": kpi_status(kpi, None if series is None else last_full),
    }


def kpi_rows(
    kpis: list[ProjectKpi], series: dict[str, MetricSeries], period: StatusPeriod
) -> list[dict[str, Any]]:
    """Each KPI with its metric's label, unit, window value, status and trend, in order."""
    rows = []
    for position, kpi in enumerate(kpis):
        metric = KPI_METRICS_BY_ID.get(kpi.metric)
        metric_series = series.get(kpi.metric)
        value = None if metric_series is None else window_value(metric_series, period)
        rows.append(
            {
                "position": position,
                **kpi.model_dump(),
                "metric_label": metric.label if metric else None,
                "unit": metric.unit if metric else None,
                "value": value.value if value else None,
                "sample_size": value.sample_size if value else None,
                "status": kpi_status(kpi, value),
                "trend": None
                if kpi.metric == UNMEASURED_METRIC
                else kpi_trend(kpi, metric_series, period),
            }
        )
    return rows
