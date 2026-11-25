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
from omnigent.server.project_status import MemberKey, MemberTotals, StatusPeriod, StatusSources
from omnigent.server.project_status_spend import micros_to_cents

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


ItemKind = Literal["ticket", "pull_request", "session"]


@dataclass(frozen=True)
class SeriesItem:
    """One counted thing behind a metric: a ticket move, a merge or a session's day of spend.

    :param at: When it happened (Unix seconds); ``None`` is undated.
    :param key: What distinct counts count: a ticket ID, a PR key or a root session ID.
    :param value: Hours for time metrics, micro-dollars for spend, else ``None``.
    :param day_utc: The UTC day for day-granular items (spend); else derived from *at*.
    :param member: Who it counts for, a member key, when known.
    :param detail: Extra display fields, e.g. the stages of a backward move.
    """

    at: int | None
    key: str
    kind: ItemKind
    value: float | None = None
    day_utc: str | None = None
    member: MemberKey | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExcludedItem:
    """An item the counting rules leave out, with the reason, for the drill-down."""

    item: SeriesItem
    reason: str
    #: Only left out of ranges with a lower bound (it's undated).
    only_when_bounded: bool = False


@dataclass(frozen=True)
class MetricSeries:
    """A metric's items, aggregated over a range by :func:`series_value`.

    :param kind: ``distinct`` counts distinct keys, ``daily_sum`` sums micro-dollars
        (reported in dollars, rounded to cents once), ``median``/``p90`` aggregate values.
    :param window_value: Overrides the window's value when a source has only a total.
    :param dated: ``False`` when the items aren't known, so per-day values can't be given.
    """

    kind: SeriesKind
    items: list[SeriesItem] = field(default_factory=list)
    excluded: list[ExcludedItem] = field(default_factory=list)
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


def item_epoch(item: SeriesItem) -> int | None:
    """When an item counts: its day's start for day-granular items, else its time."""
    return _day_start(item.day_utc) if item.day_utc is not None else item.at


def item_day(item: SeriesItem) -> str | None:
    if item.day_utc is not None:
        return item.day_utc
    if item.at is None:
        return None
    return datetime.fromtimestamp(item.at, tz=UTC).date().isoformat()


def items_in_range(series: MetricSeries, since: int | None, until: int) -> list[SeriesItem]:
    return [item for item in series.items if _in_range(item_epoch(item), since, until)]


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """The nearest-rank percentile of non-empty *values*, e.g. ``0.9`` for p90."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def aggregate(kind: SeriesKind, items: list[SeriesItem]) -> MetricValue:
    """The metric's value for a set of in-range items."""
    if kind == "distinct":
        return MetricValue(len({item.key for item in items}))
    if kind == "daily_sum":
        return MetricValue(micros_to_cents(sum(int(item.value or 0) for item in items)) / 100)
    sample = [item.value for item in items if item.value is not None]
    if not sample:
        return MetricValue(None, sample_size=0)
    measure = statistics.median(sample) if kind == "median" else nearest_rank(sample, 0.9)
    return MetricValue(round(measure, 1), sample_size=len(sample))


def series_value(series: MetricSeries, since: int | None, until: int) -> MetricValue:
    """The series' value over ``[since, until)``; ``since=None`` has no lower bound."""
    if not series.available:
        return UNAVAILABLE_VALUE
    return aggregate(series.kind, items_in_range(series, since, until))


MemberOfMove = Callable[[dict[str, Any]], "MemberKey | None"]


def _no_member(entry: dict[str, Any]) -> MemberKey | None:
    del entry
    return None


def rework_items(
    histories: StageHistories, stage_order: list[str], member_of: MemberOfMove = _no_member
) -> list[SeriesItem]:
    """Each backward move (see the module docstring), credited to whoever made it."""
    rank = {stage_id: index for index, stage_id in enumerate(stage_order)}
    items: list[SeriesItem] = []
    for ticket_id, history in histories.items():
        for before, after in itertools.pairwise(history):
            reopened = before["is_complete"] and not after["is_complete"]
            earlier = (
                before["stage_id"] in rank
                and after["stage_id"] in rank
                and rank[after["stage_id"]] < rank[before["stage_id"]]
            )
            if reopened or earlier:
                items.append(
                    SeriesItem(
                        after["at"],
                        ticket_id,
                        "ticket",
                        member=member_of(after),
                        detail={
                            "from_stage_id": before["stage_id"],
                            "to_stage_id": after["stage_id"],
                        },
                    )
                )
    return items


def shipped_items(
    histories: StageHistories, member_of: MemberOfMove = _no_member
) -> tuple[list[SeriesItem], list[ExcludedItem]]:
    """Each move into a completed stage; tickets created already complete are left out."""
    items: list[SeriesItem] = []
    excluded: list[ExcludedItem] = []
    for ticket_id, history in histories.items():
        for entry in history:
            if not entry["is_complete"]:
                continue
            item = SeriesItem(
                entry["at"],
                ticket_id,
                "ticket",
                member=member_of(entry),
                detail={"to_stage_id": entry["stage_id"]},
            )
            if entry.get("created"):
                excluded.append(
                    ExcludedItem(item, "Created in a completed stage, never moved into it")
                )
            else:
                items.append(item)
    return items, excluded


def hours_to_final_stage(
    histories: StageHistories, final_stage_id: str | None, member_of: MemberOfMove = _no_member
) -> tuple[list[SeriesItem], list[ExcludedItem]]:
    """Hours from creation to the latest move into *final_stage_id*, per ticket now there.

    Tickets that left the final stage, or have no recorded creation, are left out.
    """
    if final_stage_id is None:
        return [], []
    items: list[SeriesItem] = []
    excluded: list[ExcludedItem] = []
    for ticket_id, history in histories.items():
        reached = [entry for entry in history[1:] if entry["stage_id"] == final_stage_id]
        if not reached:
            continue
        last = history[-1]
        if last["stage_id"] != final_stage_id:
            excluded.append(
                ExcludedItem(
                    SeriesItem(
                        reached[-1]["at"], ticket_id, "ticket", member=member_of(reached[-1])
                    ),
                    "Left the final stage after reaching it",
                )
            )
            continue
        if not history[0].get("created"):
            excluded.append(
                ExcludedItem(
                    SeriesItem(last["at"], ticket_id, "ticket", member=member_of(last)),
                    "No recorded creation time",
                )
            )
            continue
        hours = max(0, last["at"] - history[0]["at"]) / 3600
        items.append(
            SeriesItem(last["at"], ticket_id, "ticket", value=hours, member=member_of(last))
        )
    return items, excluded


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


def load_series(
    metric_ids: Iterable[str], inputs: KpiInputs, member_of: MemberOfMove = _no_member
) -> dict[str, MetricSeries]:
    """Each requested metric's series; only the data those metrics need is read.

    :param member_of: Credits a ticket move (a stage-history entry) to a member.
    """
    wanted = set(metric_ids) - {UNMEASURED_METRIC}
    project, period = inputs.project, inputs.period
    series: dict[str, MetricSeries] = {}
    if "merged_prs" in wanted:
        source = inputs.sources.prs_merged
        merged = (
            None
            if source is None or not hasattr(source, "merged_items")
            else (_call(source, "merged_items", project))
        )
        if merged is not None:
            items = [
                SeriesItem(
                    None if merge.merged_at is None else int(merge.merged_at),
                    merge.key,
                    "ticket" if merge.key.startswith("ticket:") else "pull_request",
                    member=getattr(merge, "member", None),
                )
                for merge in merged
            ]
            series["merged_prs"] = MetricSeries(
                "distinct",
                items=items,
                excluded=[
                    ExcludedItem(
                        item,
                        "No merge time recorded, so it counts only in All time",
                        only_when_bounded=True,
                    )
                    for item in items
                    if item.at is None
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
        rows = (
            _call(source, "root_days", project, covering)
            if source is not None and hasattr(source, "root_days")
            else None
        )
        if rows is not None:
            series["spend_usd"] = MetricSeries(
                "daily_sum",
                items=[
                    SeriesItem(None, root_id, "session", value=micros, day_utc=day, member=member)
                    for root_id, day, micros, member in rows
                ],
            )
        elif source is not None:
            window = _total(_call(source, "spend", project, period), scale=100)
            series["spend_usd"] = MetricSeries(
                "daily_sum", available=window.available, dated=False, window_value=window
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
            items, excluded = shipped_items(histories, member_of)
            series["tickets_shipped"] = MetricSeries("distinct", items=items, excluded=excluded)
        if ticket_metrics - {"tickets_shipped"}:
            stages = inputs.stages()
            if "rework_tickets" in ticket_metrics:
                order = [str(stage["id"]) for stage in stages]
                series["rework_tickets"] = MetricSeries(
                    "distinct", items=rework_items(histories, order, member_of)
                )
            final = next(
                (str(stage["id"]) for stage in reversed(stages) if stage.get("is_complete")),
                None,
            )
            items, excluded = hours_to_final_stage(histories, final, member_of)
            for metric_id, kind in (
                ("time_to_merged_median_hours", "median"),
                ("time_to_merged_p90_hours", "p90"),
            ):
                if metric_id in ticket_metrics:
                    series[metric_id] = MetricSeries(kind, items=items, excluded=excluded)  # type: ignore[arg-type]
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
