"""Project KPIs: a small set of targets, each tied to a metric computed from project data.

The set is versioned and append-only. Every change stores a new revision holding the
whole set; the newest revision is the current one. Only the project owner, or the
project's Lead session acting for the owner, may write a revision.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KpiMetricId = Literal[
    "merged_prs",
    "spend_usd",
    "tickets_shipped",
    "rework_tickets",
    "time_to_merged_median_hours",
    "time_to_merged_p90_hours",
    "changes_landed",
    "daily_active_humans",
    "spend_per_finished_ticket_usd",
    "none",
]
KpiDirection = Literal["at_least", "at_most"]
KpiUnit = Literal["count", "usd", "hours"]

#: The metric ID for a KPI the project tracks but can't measure yet.
UNMEASURED_METRIC: KpiMetricId = "none"
PROJECT_KPIS_MAX = 6
KPI_NAME_MAX = 80
KPI_WHY_MAX = 500
KPI_REASON_MAX = 300
KPI_REVISIONS_PAGE_MAX = 100


class KpiMetric(BaseModel):
    """One entry of the fixed metric catalog."""

    id: KpiMetricId
    label: str
    unit: KpiUnit
    description: str


KPI_METRICS: tuple[KpiMetric, ...] = (
    KpiMetric(
        id="merged_prs",
        label="PRs merged",
        unit="count",
        description="Pull requests merged in the window, as counted on the Summary page.",
    ),
    KpiMetric(
        id="spend_usd",
        label="Spend",
        unit="usd",
        description="Agent spend in the window in US dollars, as on the Summary page.",
    ),
    KpiMetric(
        id="tickets_shipped",
        label="Tickets shipped",
        unit="count",
        description="Distinct tickets that reached a completed stage in the window.",
    ),
    KpiMetric(
        id="rework_tickets",
        label="Rework (reopened or sent back)",
        unit="count",
        description=(
            "Distinct tickets in the window reopened after finishing, or sent back from "
            "review to an earlier working stage. Re-review or re-verify moves after a "
            "rebase don't count."
        ),
    ),
    KpiMetric(
        id="time_to_merged_median_hours",
        label="Time to merged (median)",
        unit="hours",
        description=(
            "Median hours from a ticket's creation to its move into the final stage, "
            "over tickets that reached it in the window."
        ),
    ),
    KpiMetric(
        id="time_to_merged_p90_hours",
        label="Time to merged (p90)",
        unit="hours",
        description=(
            "90th-percentile (nearest-rank) hours from a ticket's creation to its move "
            "into the final stage, over tickets that reached it in the window."
        ),
    ),
    KpiMetric(
        id="changes_landed",
        label="Changes landed",
        unit="count",
        description=(
            "Distinct commits landed in the window: each ticket now at or past the landed "
            "stage counts its verified (else approved) commit once, dated by its move there."
        ),
    ),
    KpiMetric(
        id="daily_active_humans",
        label="Daily active people",
        unit="count",
        description=(
            "People active per weekday, averaged over the window's weekdays (UTC Mon-Fri; "
            "quiet days count as 0): anyone who filed, commented on, moved or labeled a "
            "ticket by hand, or sent a message in a project session. Agents don't count."
        ),
    ),
    KpiMetric(
        id="spend_per_finished_ticket_usd",
        label="Spend per finished ticket",
        unit="usd",
        description=(
            "Agent spend in the window divided by the tickets that reached a completed "
            "stage in it, in US dollars."
        ),
    ),
)
KPI_METRICS_BY_ID: dict[str, KpiMetric] = {metric.id: metric for metric in KPI_METRICS}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split()) or None


class ProjectKpi(BaseModel):
    """One KPI definition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1, max_length=KPI_NAME_MAX, description="Short name, e.g. 'Weekly PRs'."
    )
    metric: KpiMetricId = Field(
        description=(
            "The computed metric this KPI tracks, or 'none' when it can't be measured yet "
            "(then give not_measured_reason)."
        )
    )
    why: str = Field(
        min_length=1,
        max_length=KPI_WHY_MAX,
        description="Why it matters to the project, in a sentence.",
    )
    target: float | None = Field(
        default=None,
        ge=0,
        description="The target value in the metric's unit (count, USD or hours).",
    )
    direction: KpiDirection | None = Field(
        default=None,
        description="at_least: the value should reach the target; at_most: stay under it.",
    )
    not_measured_reason: str | None = Field(
        default=None,
        max_length=KPI_REASON_MAX,
        description="Required when metric is 'none': why it isn't measured yet.",
    )

    @field_validator("name", "why", "not_measured_reason")
    @classmethod
    def _normalize(cls, value: str | None) -> str | None:
        return _clean(value)

    @model_validator(mode="after")
    def _consistent(self) -> ProjectKpi:
        if not self.name or not self.why:
            raise ValueError("name and why must not be blank")
        if self.metric == UNMEASURED_METRIC:
            if not self.not_measured_reason:
                raise ValueError("A KPI with metric 'none' needs a not_measured_reason")
        else:
            if self.not_measured_reason is not None:
                raise ValueError("not_measured_reason is only for metric 'none'")
            if self.target is None or self.direction is None:
                raise ValueError("A measured KPI needs a target and a direction")
        if (self.target is None) != (self.direction is None):
            raise ValueError("Give target and direction together")
        return self


class SetProjectKpis(BaseModel):
    """Replace the project's KPI set by appending a new revision."""

    model_config = ConfigDict(extra="forbid")

    kpis: list[ProjectKpi] = Field(
        max_length=PROJECT_KPIS_MAX,
        description="The complete new set, in display order; an empty list clears it.",
    )
    base_revision: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The revision this edit starts from (0 when none exists yet). When given and "
            "it isn't the current revision, the write is refused so a concurrent edit "
            "isn't overwritten."
        ),
    )

    @model_validator(mode="after")
    def _unique_names(self) -> SetProjectKpis:
        names = [kpi.name.casefold() for kpi in self.kpis]
        if len(names) != len(set(names)):
            raise ValueError("KPI names must be unique")
        return self


class ProjectKpiRevision(BaseModel):
    """A stored revision of the KPI set: the set itself and who wrote it, when."""

    revision: int
    kpis: list[ProjectKpi]
    created_by_user_id: str | None
    created_by_session_id: str | None
    created_at: int
