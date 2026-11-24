"""Compute each KPI metric for a status window, from the same sources as the Summary page.

- ``merged_prs`` and ``spend_usd`` are the Summary's PRs-merged and spend totals.
- ``tickets_shipped`` is the Summary's tickets-done total: distinct tickets moved
  into a completed stage in the window.
- ``rework_tickets`` counts distinct tickets with a backward move in the window:
  to an earlier stage in the project's current order, or out of a completed stage.
- ``time_to_merged_*`` use tickets now in the final completed stage whose latest
  move there is in the window: hours from creation to that move. The median is the
  usual middle value; p90 is nearest-rank.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class MetricValue:
    """A metric's value for the window.

    :param value: The number in the metric's unit, or ``None`` with no data.
    :param available: ``False`` when the metric's source is missing or failed.
    :param sample_size: Tickets behind a time metric, else ``None``.
    """

    value: float | None
    available: bool = True
    sample_size: int | None = None


UNAVAILABLE_VALUE = MetricValue(None, available=False)


@dataclass(frozen=True)
class KpiInputs:
    """Lazily read project data; each reader runs at most once per request."""

    project: Project
    period: StatusPeriod
    sources: StatusSources
    completed_moves: Callable[[], list[dict[str, Any]]]
    stage_histories: Callable[[], StageHistories]
    stages: Callable[[], list[dict[str, Any]]]


def _source_total(source: Any, method: str, project: Project, period: StatusPeriod) -> int | None:
    if source is None:
        return None
    try:
        result = getattr(source, method)(project, period)
    except Exception:
        logger.warning("KPI source %s failed", method, exc_info=True)
        return None
    if not isinstance(result, MemberTotals) or not result.available:
        return None
    return result.total


def rework_count(histories: StageHistories, stage_order: list[str], period: StatusPeriod) -> int:
    """Distinct tickets moved backward in the window (see the module docstring)."""
    rank = {stage_id: index for index, stage_id in enumerate(stage_order)}
    reworked = set()
    for ticket_id, history in histories.items():
        for before, after in itertools.pairwise(history):
            if not period.contains(after["at"]):
                continue
            reopened = before["is_complete"] and not after["is_complete"]
            earlier = (
                before["stage_id"] in rank
                and after["stage_id"] in rank
                and rank[after["stage_id"]] < rank[before["stage_id"]]
            )
            if reopened or earlier:
                reworked.add(ticket_id)
                break
    return len(reworked)


def hours_to_final_stage(
    histories: StageHistories, final_stage_id: str | None, period: StatusPeriod
) -> list[float]:
    """Hours from creation to the latest move into *final_stage_id*, per ticket now there."""
    if final_stage_id is None:
        return []
    durations = []
    for history in histories.values():
        if not history or not history[0].get("created") or len(history) < 2:
            continue
        last = history[-1]
        if last["stage_id"] != final_stage_id or not period.contains(last["at"]):
            continue
        durations.append(max(0, last["at"] - history[0]["at"]) / 3600)
    return durations


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """The nearest-rank percentile of non-empty *values*, e.g. ``0.9`` for p90."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def compute_metrics(metric_ids: Iterable[str], inputs: KpiInputs) -> dict[str, MetricValue]:
    """Values for each requested metric ID; only the data those metrics need is read."""
    wanted = set(metric_ids) - {UNMEASURED_METRIC}
    project, period = inputs.project, inputs.period
    values: dict[str, MetricValue] = {}
    if "merged_prs" in wanted:
        total = _source_total(inputs.sources.prs_merged, "prs_merged", project, period)
        values["merged_prs"] = UNAVAILABLE_VALUE if total is None else MetricValue(total)
    if "spend_usd" in wanted:
        cents = _source_total(inputs.sources.spend, "spend", project, period)
        values["spend_usd"] = UNAVAILABLE_VALUE if cents is None else MetricValue(cents / 100)
    if "tickets_shipped" in wanted:
        shipped = {move["ticket_id"] for move in inputs.completed_moves()}
        values["tickets_shipped"] = MetricValue(len(shipped))
    timing = {"time_to_merged_median_hours", "time_to_merged_p90_hours"} & wanted
    if "rework_tickets" in wanted or timing:
        stages = inputs.stages()
        histories = inputs.stage_histories()
        if "rework_tickets" in wanted:
            order = [str(stage["id"]) for stage in stages]
            values["rework_tickets"] = MetricValue(rework_count(histories, order, period))
        if timing:
            final = next(
                (str(stage["id"]) for stage in reversed(stages) if stage.get("is_complete")),
                None,
            )
            hours = hours_to_final_stage(histories, final, period)
            for metric_id, measure in (
                ("time_to_merged_median_hours", statistics.median),
                ("time_to_merged_p90_hours", lambda sample: nearest_rank(sample, 0.9)),
            ):
                if metric_id in timing:
                    values[metric_id] = MetricValue(
                        round(measure(hours), 1) if hours else None, sample_size=len(hours)
                    )
    return values


def kpi_status(kpi: ProjectKpi, value: MetricValue | None) -> KpiStatus:
    """Whether a KPI meets its target for the window."""
    if kpi.metric == UNMEASURED_METRIC:
        return "not_measured"
    if value is None or not value.available:
        return "unavailable"
    if value.value is None or kpi.target is None:
        return "no_data"
    if kpi.direction == "at_most":
        return "met" if value.value <= kpi.target else "missed"
    return "met" if value.value >= kpi.target else "missed"


def kpi_rows(kpis: list[ProjectKpi], values: dict[str, MetricValue]) -> list[dict[str, Any]]:
    """Each KPI with its metric's label, unit, value and status, in display order."""
    rows = []
    for position, kpi in enumerate(kpis):
        metric = KPI_METRICS_BY_ID.get(kpi.metric)
        value = values.get(kpi.metric)
        rows.append(
            {
                "position": position,
                **kpi.model_dump(),
                "metric_label": metric.label if metric else None,
                "unit": metric.unit if metric else None,
                "value": value.value if value else None,
                "sample_size": value.sample_size if value else None,
                "status": kpi_status(kpi, value),
            }
        )
    return rows
