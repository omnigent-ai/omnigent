"""Compute each KPI metric from the same sources as the Summary page.

Each metric is read once as a dated series, then summed over any UTC range: the
status window, each day of the trend, and the 7 full days ending yesterday that
the on-target check uses (today is still in progress, so it's left out).

- ``merged_prs`` and ``spend_usd`` are the Summary's PRs-merged and spend sources.
- ``tickets_shipped`` counts distinct tickets moved into a completed stage, like
  the Summary's tickets done.
- ``rework_tickets`` counts distinct tickets reopened after finishing (moved out of a
  completed stage) or sent back from review (from a review stage to an earlier working
  stage). Moves back into review or verification, such as after a rebase, don't count.
- ``time_to_merged_*`` use tickets now in the final completed stage, dated by
  their latest move there: hours from creation to that move. The median is the
  usual middle value; p90 is nearest-rank.
- ``daily_active_humans`` is the mean, over the range's weekdays (UTC Monday to Friday,
  days with nobody active counting as 0), of distinct people active that day: anyone who
  filed, commented on, moved or labeled a ticket by hand, or sent a message in a project
  session. Agents' actions don't count. Over 7 days it's the weekday 7-day rolling average.
- ``spend_per_finished_ticket_usd`` is the range's spend divided by distinct tickets
  moved into a completed stage in it; no spend is counted twice (see the spend source).
  Spend is per UTC day, so a rolling 24h range takes the spend of both days it touches.
- ``changes_landed`` counts distinct commits: each ticket now at or past the landed
  stage contributes its ``verified`` (else ``approved``) SHA once, dated by its latest
  move from before that stage to it or later. Tickets sharing a SHA count once.
"""

from __future__ import annotations

import itertools
import logging
import math
import re
import statistics
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from omnigent.entities.project_kpi import (
    KPI_METRICS_BY_ID,
    UNMEASURED_METRIC,
    ProjectKpi,
)
from omnigent.server.project_status import (
    MemberKey,
    MemberTotals,
    StatusPeriod,
    StatusSources,
    user_member_key,
)
from omnigent.server.project_status_spend import micros_to_cents

if TYPE_CHECKING:
    from omnigent.entities import Project

logger = logging.getLogger(__name__)

KpiStatus = Literal["met", "missed", "no_data", "unavailable", "not_measured"]
StageHistories = dict[str, list[dict[str, Any]]]
SeriesKind = Literal["distinct", "daily_sum", "median", "p90", "weekday_mean", "ratio"]
#: Full UTC days, ending yesterday, that the on-target check and trend cover.
TREND_DAYS = 7
DAY = 86_400
#: Labels that record a ticket's landed commit, in order of preference.
LANDED_COMMIT_LABELS = ("verified", "approved")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_LANDED_STAGE_NAMES = ("implemented", "landed")
TicketLabels = dict[str, tuple[str | None, dict[str, str]]]


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


ItemKind = Literal["ticket", "pull_request", "session", "person"]


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
    #: Tickets and sessions behind a merge, so a role can claim it.
    claimed_by: tuple[str, ...] = ()


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
    #: The earliest time any range in the request starts; ``None`` reads all history.
    history_since: int | None = None
    #: Each ticket's current stage and ``LANDED_COMMIT_LABELS`` values; ``None`` when unread.
    ticket_labels: Callable[[], TicketLabels] | None = None
    #: ``(user_id, at)`` for each thing a person did by hand; ``None`` when unread.
    human_activity: Callable[[], list[tuple[str, int]]] | None = None


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


def item_in_range(item: SeriesItem, since: int | None, until: int) -> bool:
    """Whether *item* counts in ``[since, until)``.

    A day-granular item (spend) counts when its UTC day overlaps the range, so a
    rolling 24h window takes both days it touches, like the Summary's spend.
    """
    if item.day_utc is None:
        return _in_range(item.at, since, until)
    start = _day_start(item.day_utc)
    return (since is None or start + DAY > since) and start < until


def items_in_range(series: MetricSeries, since: int | None, until: int) -> list[SeriesItem]:
    return [item for item in series.items if item_in_range(item, since, until)]


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """The nearest-rank percentile of non-empty *values*, e.g. ``0.9`` for p90."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _is_weekday(day_utc: str) -> bool:
    return date.fromisoformat(day_utc).weekday() < 5


def weekday_mean(items: list[SeriesItem], days: Iterable[str]) -> MetricValue:
    """Mean distinct keys per weekday over *days*; a day with no items counts as 0."""
    weekdays = [day for day in days if _is_weekday(day)]
    if not weekdays:
        return MetricValue(None, sample_size=0)
    keys: dict[str, set[str]] = {day: set() for day in weekdays}
    for item in items:
        day = item_day(item)
        if day in keys:
            keys[day].add(item.key)
    mean = sum(len(found) for found in keys.values()) / len(weekdays)
    return MetricValue(round(mean, 1), sample_size=len(weekdays))


def aggregate(kind: SeriesKind, items: list[SeriesItem]) -> MetricValue:
    """The metric's value for a set of in-range items."""
    if kind == "weekday_mean":
        # Without a range, only the days the items fall on are known (one drill-down day).
        return weekday_mean(items, sorted({day for item in items if (day := item_day(item))}))
    if kind == "ratio":
        tickets = len({item.key for item in items if item.kind == "ticket"})
        micros = sum(int(item.value or 0) for item in items if item.kind == "session")
        if not tickets:
            return MetricValue(None, sample_size=0)
        return MetricValue(round(micros_to_cents(micros) / 100 / tickets, 2), sample_size=tickets)
    if kind == "distinct":
        return MetricValue(len({item.key for item in items}))
    if kind == "daily_sum":
        return MetricValue(micros_to_cents(sum(int(item.value or 0) for item in items)) / 100)
    sample = [item.value for item in items if item.value is not None]
    if not sample:
        return MetricValue(None, sample_size=0)
    measure = statistics.median(sample) if kind == "median" else nearest_rank(sample, 0.9)
    return MetricValue(round(measure, 1), sample_size=len(sample))


def _range_days(series: MetricSeries, since: int | None, until: int) -> list[str]:
    """Every UTC day ``[since, until)`` touches; unbounded starts at the first item's day."""
    last = datetime.fromtimestamp(until - 1, tz=UTC).date()
    if since is not None:
        first = datetime.fromtimestamp(since, tz=UTC).date()
    else:
        known = [day for item in series.items if (day := item_day(item))]
        if not known:
            return []
        first = date.fromisoformat(min(known))
    return [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]


def series_value(series: MetricSeries, since: int | None, until: int) -> MetricValue:
    """The series' value over ``[since, until)``; ``since=None`` has no lower bound."""
    if not series.available:
        return UNAVAILABLE_VALUE
    items = items_in_range(series, since, until)
    if series.kind == "weekday_mean":
        return weekday_mean(items, _range_days(series, since, until))
    return aggregate(series.kind, items)


MemberOfMove = Callable[[dict[str, Any]], "MemberKey | None"]


def _no_member(entry: dict[str, Any]) -> MemberKey | None:
    del entry
    return None


def review_stage_ids(stages: list[dict[str, Any]]) -> set[str]:
    """Stages whose ID or name contains "review", as for review rounds."""
    return {
        str(stage["id"])
        for stage in stages
        if "review" in str(stage["id"]).lower() or "review" in str(stage.get("name", "")).lower()
    }


def rework_items(
    histories: StageHistories, stages: list[dict[str, Any]], member_of: MemberOfMove = _no_member
) -> list[SeriesItem]:
    """Each rework move (see the module docstring), credited to whoever made it.

    ``detail["reason"]`` is ``reopened`` (out of a completed stage) or ``sent_back`` (from
    a review stage to an earlier stage that isn't a review or completed one). Other
    backward moves, like re-verifying or re-reviewing after a rebase, aren't rework.
    """
    rank = {str(stage["id"]): index for index, stage in enumerate(stages)}
    review = review_stage_ids(stages)
    items: list[SeriesItem] = []
    for ticket_id, history in histories.items():
        for before, after in itertools.pairwise(history):
            if before["is_complete"] and not after["is_complete"]:
                reason = "reopened"
            elif (
                before["stage_id"] in review
                and after["stage_id"] not in review
                and not after["is_complete"]
                and rank.get(after["stage_id"], len(rank)) < rank.get(before["stage_id"], -1)
            ):
                reason = "sent_back"
            else:
                continue
            items.append(
                SeriesItem(
                    after["at"],
                    ticket_id,
                    "ticket",
                    member=member_of(after),
                    detail={
                        "reason": reason,
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


def landed_stage_index(config: dict[str, Any], stages: list[dict[str, Any]]) -> int | None:
    """Where landed work starts in the stage order.

    The configured ``landing.landed_stage``, an ``implemented`` stage or one named
    Implemented or Landed; without one, the first completed stage.
    """
    ids = [str(stage["id"]) for stage in stages]
    raw = config.get("landing")
    configured = (raw if isinstance(raw, dict) else {}).get("landed_stage")
    if isinstance(configured, str) and configured in ids:
        return ids.index(configured)
    if "implemented" in ids:
        return ids.index("implemented")
    for index, stage in enumerate(stages):
        if str(stage.get("name") or "").strip().casefold() in _LANDED_STAGE_NAMES:
            return index
    return next((index for index, stage in enumerate(stages) if stage.get("is_complete")), None)


def ticket_times(
    histories: StageHistories, stages: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, dict[str, int | None]]:
    """Each ticket's ``completed_at`` and ``landed_at`` from its stage history.

    ``completed_at`` is its latest move into a completed stage, while it's still in one.
    ``landed_at`` is its latest move from before the landed stage to it or later, while
    it's still at or past it. Either is ``None`` otherwise.
    """
    start = landed_stage_index(config, stages)
    rank = {str(stage["id"]): index for index, stage in enumerate(stages)}
    times: dict[str, dict[str, int | None]] = {}
    for ticket_id, history in histories.items():
        last = history[-1] if history else None
        completed = (
            last["at"] if last and last["is_complete"] and not last.get("created") else None
        )
        landed = None
        if start is not None and last and rank.get(last["stage_id"], -1) >= start:
            crossings = [
                after["at"]
                for before, after in itertools.pairwise(history)
                if rank.get(before["stage_id"], -1) < start <= rank.get(after["stage_id"], -1)
            ]
            landed = crossings[-1] if crossings else None
        times[ticket_id] = {"completed_at": completed, "landed_at": landed}
    return times


def landed_commit(labels: dict[str, str]) -> str | None:
    """The ticket's landed commit: its first full-SHA label in ``LANDED_COMMIT_LABELS``."""
    for key in LANDED_COMMIT_LABELS:
        value = labels.get(key, "").lower()
        if _FULL_SHA.fullmatch(value):
            return value
    return None


def landed_items(
    histories: StageHistories,
    stages: list[dict[str, Any]],
    config: dict[str, Any],
    ticket_labels: TicketLabels,
    member_of: MemberOfMove = _no_member,
) -> tuple[list[SeriesItem], list[ExcludedItem]]:
    """Each distinct landed commit (see the module docstring), as the ticket that landed it."""
    start = landed_stage_index(config, stages)
    if start is None:
        return [], []
    rank = {str(stage["id"]): index for index, stage in enumerate(stages)}
    landed: list[SeriesItem] = []
    excluded: list[ExcludedItem] = []
    for ticket_id, (stage_id, labels) in ticket_labels.items():
        commit = landed_commit(labels)
        history = histories.get(ticket_id)
        if commit is None or rank.get(stage_id or "", -1) < start or not history:
            continue
        crossings = [
            after
            for before, after in itertools.pairwise(history)
            if rank.get(before["stage_id"], -1) < start <= rank.get(after["stage_id"], -1)
        ]
        detail = {"commit": commit}
        if not crossings:
            item = SeriesItem(history[-1]["at"], ticket_id, "ticket", detail=detail)
            excluded.append(ExcludedItem(item, "Never moved into the landed stage"))
            continue
        moved = crossings[-1]
        landed.append(
            SeriesItem(moved["at"], ticket_id, "ticket", member=member_of(moved), detail=detail)
        )
    items: list[SeriesItem] = []
    seen: set[str] = set()
    for item in sorted(landed, key=lambda entry: (entry.at or 0, entry.key)):
        if item.detail["commit"] in seen:
            excluded.append(ExcludedItem(item, "Same commit as an earlier ticket"))
        else:
            seen.add(item.detail["commit"])
            items.append(item)
    return items, excluded


def has_landed_commits(inputs: KpiInputs) -> bool:
    """Whether any ticket is at or past the landed stage with a landed commit."""
    if inputs.ticket_labels is None:
        return False
    stages = inputs.stages()
    start = landed_stage_index(inputs.project.config or {}, stages)
    rank = {str(stage["id"]): index for index, stage in enumerate(stages)}
    return start is not None and any(
        landed_commit(labels) and rank.get(stage_id or "", -1) >= start
        for stage_id, labels in inputs.ticket_labels().values()
    )


def changes_landed_total(inputs: KpiInputs) -> int | None:
    """Changes landed in the window; ``None`` when the project has never landed a commit."""
    if not has_landed_commits(inputs):
        return None
    value = compute_metrics(["changes_landed"], inputs)["changes_landed"]
    return int(value.value or 0) if value.available else None


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


def kpi_history_since(period: StatusPeriod) -> int | None:
    """The earliest moment a KPI request needs: its window's start or the trend's."""
    if period.since_epoch is None:
        return None
    return min(period.since_epoch, _day_start(trend_since_day(period.today_utc)))


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
            else (_call(source, "merged_items", project, inputs.history_since))
        )
        if merged is not None:
            items = [
                SeriesItem(
                    None if merge.merged_at is None else int(merge.merged_at),
                    merge.key,
                    "pull_request",
                    member=getattr(merge, "member", None),
                    claimed_by=tuple(getattr(merge, "claimed_by", ())),
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
    if "daily_active_humans" in wanted:
        if inputs.human_activity is None:
            series["daily_active_humans"] = UNAVAILABLE_SERIES
        else:
            series["daily_active_humans"] = MetricSeries(
                "weekday_mean",
                items=[
                    SeriesItem(at, user_id, "person", member=user_member_key(user_id))
                    for user_id, at in inputs.human_activity()
                ],
            )
    if "spend_per_finished_ticket_usd" in wanted:
        parts = load_series(["spend_usd", "tickets_shipped"], inputs, member_of)
        spend, shipped = parts["spend_usd"], parts["tickets_shipped"]
        series["spend_per_finished_ticket_usd"] = (
            MetricSeries("ratio", items=[*spend.items, *shipped.items])
            if spend.available and spend.dated
            else UNAVAILABLE_SERIES
        )
    if "changes_landed" in wanted:
        if inputs.ticket_labels is None:
            series["changes_landed"] = UNAVAILABLE_SERIES
        else:
            items, excluded = landed_items(
                inputs.stage_histories(),
                inputs.stages(),
                project.config or {},
                inputs.ticket_labels(),
                member_of,
            )
            series["changes_landed"] = MetricSeries("distinct", items=items, excluded=excluded)
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
                series["rework_tickets"] = MetricSeries(
                    "distinct", items=rework_items(histories, stages, member_of)
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


def role_series(
    series: dict[str, MetricSeries],
    ticket_ids: Collection[str],
    session_ids: Collection[str],
) -> dict[str, MetricSeries]:
    """Narrow each metric's series to one role's work.

    Ticket items count when the ticket is the role's (``assignee`` is the role),
    a merge when one of those tickets or the role's sessions claims it, and spend
    when it is one of the role's sessions. A metric known only as a project total
    can't be split, so a role sees it as unavailable.
    """

    def ours(item: SeriesItem) -> bool:
        if item.kind == "session":
            return item.key in session_ids
        if item.kind == "ticket" and item.key.removeprefix("ticket:") in ticket_ids:
            return True
        return any(owner in ticket_ids or owner in session_ids for owner in item.claimed_by)

    narrowed: dict[str, MetricSeries] = {}
    for metric_id, metric in series.items():
        if not metric.dated or not metric.available:
            narrowed[metric_id] = UNAVAILABLE_SERIES
            continue
        narrowed[metric_id] = replace(
            metric,
            items=[item for item in metric.items if ours(item)],
            excluded=[excluded for excluded in metric.excluded if ours(excluded.item)],
        )
    return narrowed
