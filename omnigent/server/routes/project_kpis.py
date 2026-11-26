"""``/v1/projects/{project_id}/kpis``: a project's KPI set, its values and its history."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Collection
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from omnigent.db.utils import now_epoch
from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.entities.project_kpi import (
    KPI_METRICS,
    KPI_METRICS_BY_ID,
    KPI_REVISIONS_PAGE_MAX,
    KpiDirection,
    KpiMetric,
    KpiMetricId,
    KpiUnit,
    ProjectKpiRevision,
    SetProjectKpis,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.project_kpis import (
    LANDED_COMMIT_LABELS,
    KpiInputs,
    KpiStatus,
    MetricSeries,
    SeriesItem,
    has_landed_commits,
    kpi_history_since,
    kpi_rows,
    load_series,
    role_series,
)
from omnigent.server.project_status import (
    StatusPeriod,
    StatusSources,
    StatusWindow,
    load_status_sources,
    status_period,
    user_member_key,
)
from omnigent.server.project_status_cache import project_status_cache
from omnigent.server.project_status_items import DrilldownRange, drilldown, range_bounds
from omnigent.server.project_status_trends import trend_rows
from omnigent.server.role_side_chat import refuse_role_side_chat
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.project_status import move_member_resolver
from omnigent.server.routes.projects import refuse_unless_owner_or_lead
from omnigent.server.usage_events import usage_event_store

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.entities.project import ProjectIdentity
    from omnigent.server.auth import AuthProvider
    from omnigent.stores import ConversationStore, ProjectStore
    from omnigent.stores.project_kpi_store import ProjectKpiStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore


WindowParam = Annotated[
    StatusWindow, Query(description="7d, 30d or all, in UTC days, as on the Summary page.")
]


class ProjectKpiDay(BaseModel):
    day_utc: str
    value: float | None = Field(description="The day's value; null without data or a source.")


class ProjectKpiTrend(BaseModel):
    """The KPI over the 7 full UTC days ending yesterday, plus today so far."""

    since_day_utc: str
    until_day_utc: str = Field(description="Yesterday (UTC), the last full day checked.")
    days: list[ProjectKpiDay] = Field(
        description="One entry per day from since_day_utc through today; today is last."
    )
    last_7_days: float | None
    last_7_days_sample_size: int | None = Field(
        description="Tickets behind a time-to-merged value; null for other metrics."
    )
    today: float | None = Field(description="Today so far (UTC).")
    status: KpiStatus = Field(description="The target check over the 7 full days.")


class ProjectKpiValue(BaseModel):
    """One KPI with its value for the window and its 7-day trend."""

    position: int
    name: str
    metric: KpiMetricId
    metric_label: str | None = Field(description="The metric's display name; null for 'none'.")
    unit: KpiUnit | None = Field(description="count, usd or hours; null for 'none'.")
    why: str
    target: float | None
    direction: KpiDirection | None
    not_measured_reason: str | None
    value: float | None = Field(
        description="The window's value in the unit; null when unmeasured or without data."
    )
    sample_size: int | None = Field(
        description="Tickets behind a time-to-merged value; null for other metrics."
    )
    status: KpiStatus = Field(
        description=(
            "met or missed against the target; no_data when nothing happened to measure; "
            "unavailable when the metric's source isn't on this server; not_measured for 'none'."
        )
    )
    trend: ProjectKpiTrend | None = Field(description="Null for a KPI with metric 'none'.")


class ProjectKpis(BaseModel):
    """The project's current KPI set with each value over a UTC window."""

    object: Literal["project.kpis"]
    project_id: str
    identity_id: str | None = Field(
        default=None, description="The role these KPIs belong to; null for the project's own."
    )
    window: StatusWindow
    today_utc: str
    since_day_utc: str | None
    generated_at: int
    revision: int = Field(description="The current revision; 0 when KPIs were never set.")
    updated_at: int | None
    updated_by_user_id: str | None
    updated_by_session_id: str | None
    can_edit: bool = Field(description="Whether the caller (the owner) may change the KPIs.")
    kpis: list[ProjectKpiValue]
    metrics: list[KpiMetric] = Field(description="The metrics a KPI may track.")


StatusItemMetric = Literal[
    "merged_prs",
    "spend_usd",
    "tickets_shipped",
    "rework_tickets",
    "time_to_merged_median_hours",
    "time_to_merged_p90_hours",
    "changes_landed",
    "daily_active_humans",
    "spend_per_finished_ticket_usd",
    "tickets_submitted",
]


class ProjectStatusItem(BaseModel):
    """One ticket, PR or session behind a number."""

    day_utc: str | None = Field(description="Its UTC day; null for an undated merge.")
    at: int | None = Field(description="When it happened; null for day-granular spend.")
    kind: Literal["ticket", "pull_request", "session", "person"]
    id: str = Field(description="Ticket ID, PR URL, or root session ID.")
    title: str | None
    url: str | None = Field(description="The PR's URL; null for tickets and sessions.")
    member: str | None = Field(description="The member key it counts for, when known.")
    value: float | None = Field(description="Hours for time metrics, USD for spend, else null.")
    detail: dict[str, Any] | None = None
    counted_in_total: bool = Field(
        default=True,
        description="False when the total counts it on a later day (it counts once).",
    )
    reason: str | None = Field(default=None, description="Why it's left out (excluded only).")


class ProjectStatusItemDay(BaseModel):
    day_utc: str
    value: float | None = Field(description="The day's value, as on the tile.")
    items: int = Field(description="Rows behind it; equals value for count metrics.")


class ProjectStatusItems(BaseModel):
    """The items behind one status number, by UTC day."""

    object: Literal["project.status_items"]
    project_id: str
    metric: StatusItemMetric
    unit: KpiUnit
    range: Literal["7d", "30d", "all", "trend"]
    member: str | None
    since_day_utc: str | None
    today_utc: str
    available: bool = Field(description="False when this server has no source for the metric.")
    total: float | None
    sample_size: int | None
    days: list[ProjectStatusItemDay]
    items: list[ProjectStatusItem]
    excluded: list[ProjectStatusItem] = Field(
        description="Items the counting rules leave out, each with its reason."
    )
    truncated: bool
    hidden_items: int = Field(
        default=0,
        description=(
            "Rows about other people the caller may not see: a member sees only their own "
            "person rows and agents see none. The day values and total still count them."
        ),
    )


class ProjectKpiRevisionPage(BaseModel):
    object: Literal["list"]
    data: list[ProjectKpiRevision]
    has_more: bool


class ProjectStatusTrendDay(BaseModel):
    """One chart bucket; a metric is null when its source isn't available."""

    day_utc: str = Field(description="The bucket's first UTC day.")
    tickets_filed: int = Field(description="Tickets created in the bucket.")
    tickets_finished: int = Field(description="Distinct tickets moved into a completed stage.")
    open_tickets: int = Field(description="Tickets not in a completed stage at the bucket's end.")
    changes_landed: int | None = Field(
        description="Distinct commits landed; null when the project never landed one."
    )
    prs_merged: int | None
    spend_usd: float | None


class ProjectStatusTrends(BaseModel):
    """The Summary's trend charts over a window, day by day or, for long ranges, by week."""

    object: Literal["project.status_trends"]
    project_id: str
    window: StatusWindow
    today_utc: str
    bucket: Literal["day", "week"]
    days: list[ProjectStatusTrendDay]


def submitted_series(project: Project, rows: list[dict[str, Any]]) -> MetricSeries:
    """Tickets each person requested, dated by creation, for the drill-down."""
    return MetricSeries(
        "distinct",
        items=[
            SeriesItem(
                row["created_at"],
                row["ticket_id"],
                "ticket",
                member=user_member_key(row.get("requested_by") or project.user_id),
            )
            for row in rows
        ],
    )


def is_agent(request: Request) -> bool:
    """Whether an agent session, not a person, made the request."""
    return (
        ORCHESTRATION_SOURCE_HEADER in request.headers
        or RUNNER_TUNNEL_TOKEN_HEADER in request.headers
    )


def human_activity(
    request: Request,
    ticket_store: ProjectTicketStore,
    project: Project,
    user_id: str | None,
    since: int | None,
) -> list[tuple[str, int]]:
    """``(user_id, at)`` for what people did by hand: ticket events and session messages."""
    rows = ticket_store.human_ticket_activity(project.id, user_id=user_id, since_epoch=since)
    usage = usage_event_store(request.app)
    if usage is not None:
        rows.extend(
            (event.user_id, event.created_at)
            for event in usage.events(project.id, since_epoch=since, names=("message.sent",))
            if event.user_id
        )
    return rows


def daily_trends(
    inputs: KpiInputs,
    filed: list[dict[str, Any]],
    ticket_ids: Collection[str] | None = None,
    session_ids: Collection[str] = (),
) -> dict[str, Any]:
    """The Summary's trend buckets (see ``project_status_trends``) for *inputs*' window.

    With *ticket_ids*, only that role's tickets and *session_ids* count, as for role KPIs.
    *inputs* should read the whole stage history, which the burndown replays.
    """
    series = load_series(["tickets_shipped", "merged_prs", "spend_usd", "changes_landed"], inputs)
    filed_series = submitted_series(inputs.project, filed)
    histories = inputs.stage_histories()
    if ticket_ids is not None:
        series = role_series(series, ticket_ids, session_ids)
        filed_series = role_series({"filed": filed_series}, ticket_ids, session_ids)["filed"]
        histories = {key: value for key, value in histories.items() if key in ticket_ids}
    return trend_rows(
        inputs.period,
        {
            "tickets_filed": filed_series,
            "tickets_finished": series["tickets_shipped"],
            "changes_landed": series["changes_landed"] if has_landed_commits(inputs) else None,
            "prs_merged": series["merged_prs"] if series["merged_prs"].dated else None,
            "spend_usd": series["spend_usd"] if series["spend_usd"].dated else None,
        },
        histories,
    )


def create_project_kpis_router(
    project_store: ProjectStore,
    kpi_store: ProjectKpiStore,
    ticket_store: ProjectTicketStore,
    conversation_store: ConversationStore | None,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()

    def sources(request: Request) -> StatusSources:
        state = request.app.state
        loaded = getattr(state, "project_status_sources", None)
        if loaded is None:
            loaded = load_status_sources(state)
            state.project_status_sources = loaded
        return loaded

    async def load_project(project_id: str, user_id: str | None) -> Project:
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project

    def inputs_for(
        request: Request,
        project: Project,
        period: StatusPeriod,
        user_id: str | None,
        since: int | None,
    ) -> KpiInputs:
        """Readers bounded to what a range starting at *since* can count."""
        return KpiInputs(
            project=project,
            period=period,
            sources=sources(request),
            # Cached per project, shared by the KPI tiles and their drill-downs.
            stage_histories=functools.cache(
                lambda: project_status_cache.get_or_compute(
                    project.id,
                    "tickets",
                    ("stage_histories", since),
                    lambda: ticket_store.stage_histories(
                        project.id, user_id=user_id, since_epoch=since
                    ),
                )
            ),
            stages=functools.cache(
                lambda: project_status_cache.get_or_compute(
                    project.id,
                    "tickets",
                    ("stages",),
                    lambda: ticket_store.stages(project.id, user_id=user_id)["stages"],
                )
            ),
            history_since=since,
            human_activity=functools.cache(
                lambda: human_activity(request, ticket_store, project, user_id, since)
            ),
            ticket_labels=functools.cache(
                lambda: project_status_cache.get_or_compute(
                    project.id,
                    "tickets",
                    ("ticket_labels",),
                    lambda: ticket_store.label_values(
                        project.id, LANDED_COMMIT_LABELS, user_id=user_id
                    ),
                )
            ),
        )

    def build(
        request: Request,
        project: Project,
        window: StatusWindow,
        current: ProjectKpiRevision | None,
        user_id: str | None,
        identity: ProjectIdentity | None = None,
    ) -> dict[str, Any]:
        period = status_period(window, now_epoch())
        kpis = current.kpis if current else []
        inputs = inputs_for(request, project, period, user_id, kpi_history_since(period))
        series = load_series((kpi.metric for kpi in kpis), inputs)
        if identity is not None:
            series = role_series(
                series,
                ticket_store.assigned_ticket_ids(project.id, identity.name, user_id=user_id),
                role_sessions(series, identity.id),
            )
        return {
            "object": "project.kpis",
            "project_id": project.id,
            "identity_id": identity.id if identity is not None else None,
            "window": window,
            "today_utc": period.today_utc,
            "since_day_utc": period.since_day_utc,
            "generated_at": period.until_epoch,
            "revision": current.revision if current else 0,
            "updated_at": current.created_at if current else None,
            "updated_by_user_id": current.created_by_user_id if current else None,
            "updated_by_session_id": current.created_by_session_id if current else None,
            "can_edit": project.user_id == user_id,
            "kpis": kpi_rows(kpis, series, period),
            "metrics": [metric.model_dump() for metric in KPI_METRICS],
        }

    def role_sessions(series: dict[str, Any], identity_id: str) -> set[str]:
        """Which sessions behind *series* (spend roots, merge claimers) are the role's.

        Decided by each session's identity label; ticket IDs among the merge
        claimers simply have no label.
        """
        candidates: set[str] = set()
        spend = series.get("spend_usd")
        if spend is not None:
            candidates.update(item.key for item in spend.items)
        merged = series.get("merged_prs")
        if merged is not None:
            candidates.update(owner for item in merged.items for owner in item.claimed_by)
        roots = sorted(candidates)
        if not roots or conversation_store is None:
            return set()
        labels = conversation_store.get_labels(roots, (PROJECT_IDENTITY_LABEL_KEY,))
        return {
            root
            for root, found in labels.items()
            if found.get(PROJECT_IDENTITY_LABEL_KEY) == identity_id
        }

    async def load_identity(project: Project, identity_id: str, user_id: str | None) -> Any:
        identity = await asyncio.to_thread(
            project_store.get_identity, project.id, identity_id, user_id=user_id
        )
        if identity is None:
            raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        return identity

    @router.get("/projects/{project_id}/identities/{identity_id}/kpis", response_model=ProjectKpis)
    async def get_role_kpis(
        request: Request,
        project_id: str,
        identity_id: str,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """A role's current KPIs, measured over its own tickets and sessions.

        Any project member may read them.
        """
        user_id = require_user(request, auth_provider)
        project = await load_project(project_id, user_id)
        identity = await load_identity(project, identity_id, user_id)
        current = await asyncio.to_thread(
            kpi_store.current, project.id, user_id=user_id, identity_id=identity.id
        )
        return await asyncio.to_thread(build, request, project, window, current, user_id, identity)

    @router.put("/projects/{project_id}/identities/{identity_id}/kpis", response_model=ProjectKpis)
    async def set_role_kpis(
        request: Request,
        project_id: str,
        identity_id: str,
        body: SetProjectKpis,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """Replace a role's KPI set by appending a revision. The owner or the Lead only."""
        user_id = require_user(request, auth_provider)
        if conversation_store is not None:
            await asyncio.to_thread(
                refuse_role_side_chat,
                request,
                action="Changing a role's KPIs",
                user_id=user_id,
                conversations=conversation_store,
                projects=project_store,
            )
        project = await load_project(project_id, user_id)
        identity = await load_identity(project, identity_id, user_id)
        session_id = await asyncio.to_thread(
            refuse_unless_owner_or_lead,
            request,
            project,
            OmnigentError(
                "Only the project owner or its Lead can change a role's KPIs",
                code=ErrorCode.FORBIDDEN,
            ),
            user_id=user_id,
            conversation_store=conversation_store,
        )
        revision = await asyncio.to_thread(
            kpi_store.append,
            project.id,
            body.kpis,
            user_id=user_id,
            session_id=session_id,
            base_revision=body.base_revision,
            identity_id=identity.id,
        )
        return await asyncio.to_thread(
            build, request, project, window, revision, user_id, identity
        )

    @router.get("/projects/{project_id}/kpis", response_model=ProjectKpis)
    async def get_project_kpis(
        request: Request,
        project_id: str,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """The current KPIs, each with its value, target and status for the window.

        Any project member may read them.
        """
        user_id = require_user(request, auth_provider)
        project = await load_project(project_id, user_id)
        current = await asyncio.to_thread(kpi_store.current, project.id, user_id=user_id)
        return await asyncio.to_thread(build, request, project, window, current, user_id)

    @router.put("/projects/{project_id}/kpis", response_model=ProjectKpis)
    async def set_project_kpis(
        request: Request,
        project_id: str,
        body: SetProjectKpis,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """Replace the KPI set by appending a revision; returns the new set's values.

        Only the project owner, or the project's Lead session, may write.
        """
        user_id = require_user(request, auth_provider)
        if conversation_store is not None:
            await asyncio.to_thread(
                refuse_role_side_chat,
                request,
                action="Changing the project's KPIs",
                user_id=user_id,
                conversations=conversation_store,
                projects=project_store,
            )
        project = await load_project(project_id, user_id)
        session_id = await asyncio.to_thread(
            refuse_unless_owner_or_lead,
            request,
            project,
            OmnigentError(
                "Only the project owner or its Lead can change the project's KPIs",
                code=ErrorCode.FORBIDDEN,
            ),
            user_id=user_id,
            conversation_store=conversation_store,
        )
        revision = await asyncio.to_thread(
            kpi_store.append,
            project.id,
            body.kpis,
            user_id=user_id,
            session_id=session_id,
            base_revision=body.base_revision,
        )
        return await asyncio.to_thread(build, request, project, window, revision, user_id)

    def build_items(
        request: Request,
        project: Project,
        metric: StatusItemMetric,
        range_id: DrilldownRange,
        member: str | None,
        user_id: str | None,
    ) -> dict[str, Any]:
        period = status_period("7d" if range_id == "trend" else range_id, now_epoch())
        inputs = inputs_for(request, project, period, user_id, range_bounds(range_id, period)[0])
        member_of = move_member_resolver(project, conversation_store)
        if metric == "tickets_submitted":
            series = submitted_series(
                project,
                ticket_store.requested_tickets(
                    project.id, user_id=user_id, since_epoch=range_bounds(range_id, period)[0]
                ),
            )
        else:
            series = load_series([metric], inputs, member_of)[metric]
        viewer = user_member_key(user_id)
        owner = project.user_id == user_id and not is_agent(request)
        if member is not None and series.kind == "weekday_mean" and not owner:
            # One person's activity by day is theirs and the owner's to see.
            if is_agent(request) or member != viewer:
                raise OmnigentError(
                    "Only the project owner can see another person's activity",
                    code=ErrorCode.FORBIDDEN,
                )
        result = drilldown(series, period, range_id, member) if series.dated else None
        hidden = 0
        if result is not None and not owner:
            for key in ("items", "excluded"):
                shown = [
                    row
                    for row in result[key]
                    if row["kind"] != "person"
                    or (not is_agent(request) and row["member"] == viewer)
                ]
                hidden += len(result[key]) - len(shown)
                result[key] = shown
        rows = [] if result is None else [*result["items"], *result["excluded"]]
        ticket_ids = {row["id"].removeprefix("ticket:") for row in rows if row["kind"] == "ticket"}
        titles = ticket_store.ticket_titles(project.id, sorted(ticket_ids), user_id=user_id)
        session_titles: dict[str, str | None] = {}
        for row in rows:
            if row["kind"] == "session" and row["id"] not in session_titles:
                session = (
                    conversation_store.get_conversation(row["id"]) if conversation_store else None
                )
                session_titles[row["id"]] = getattr(session, "title", None)
            if row["kind"] == "ticket":
                row["id"] = row["id"].removeprefix("ticket:")
                row["title"] = titles.get(row["id"])
                row["url"] = None
            elif row["kind"] == "session":
                row["title"] = session_titles[row["id"]]
                row["url"] = None
            elif row["kind"] == "person":
                row["title"] = None
                row["url"] = None
            else:
                row["title"] = None
                row["url"] = row["id"]
        metric_info = KPI_METRICS_BY_ID.get(metric)
        base = {
            "object": "project.status_items",
            "project_id": project.id,
            "metric": metric,
            "unit": metric_info.unit if metric_info else "count",
            "range": range_id,
            "member": member,
            "today_utc": period.today_utc,
        }
        if result is None:
            window = series.window_value
            return {
                **base,
                "since_day_utc": range_bounds(range_id, period)[1],
                "available": False,
                "total": None if window is None else window.value,
                "sample_size": None,
                "days": [],
                "items": [],
                "excluded": [],
                "truncated": False,
            }
        return {**base, **result, "available": series.available, "hidden_items": hidden}

    @router.get("/projects/{project_id}/status/items", response_model=ProjectStatusItems)
    async def project_status_items(
        request: Request,
        project_id: str,
        metric: Annotated[StatusItemMetric, Query(description="The metric to list items for.")],
        range: Annotated[
            DrilldownRange,
            Query(
                description=(
                    "7d, 30d or all as on the Summary, or trend: the KPI tiles' 7 full UTC "
                    "days ending yesterday plus today."
                )
            ),
        ] = "7d",
        member: Annotated[
            str | None,
            Query(description="Only what counts for this member key, e.g. user:a@example.com."),
        ] = None,
    ) -> dict[str, Any]:
        """The tickets, PRs or sessions behind a Summary number or KPI tile, by day.

        Any project member may read it. Each day's value is computed by the same code
        as the number it drills into; items the counting rules leave out are listed
        separately with the reason.
        """
        user_id = require_user(request, auth_provider)
        project = await load_project(project_id, user_id)
        return await asyncio.to_thread(
            build_items, request, project, metric, range, member, user_id
        )

    def build_trends(
        request: Request, project: Project, window: StatusWindow, user_id: str | None
    ) -> dict[str, Any]:
        period = status_period(window, now_epoch())
        # The burndown replays every ticket's moves, so read the whole history.
        inputs = inputs_for(request, project, period, user_id, None)
        filed = ticket_store.requested_tickets(
            project.id, user_id=user_id, since_epoch=period.since_epoch
        )
        return {
            "object": "project.status_trends",
            "project_id": project.id,
            "window": window,
            "today_utc": period.today_utc,
            **daily_trends(inputs, filed),
        }

    @router.get("/projects/{project_id}/status/trends", response_model=ProjectStatusTrends)
    async def project_status_trends(
        request: Request,
        project_id: str,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """Per-day tickets filed and finished, open tickets, changes landed, PRs and spend.

        Any project member may read it. Each day's value equals the Summary drill-down's
        value for that day; ranges over 90 days use 7-day buckets ending today.
        """
        user_id = require_user(request, auth_provider)
        project = await load_project(project_id, user_id)
        return await asyncio.to_thread(build_trends, request, project, window, user_id)

    @router.get("/projects/{project_id}/kpis/revisions", response_model=ProjectKpiRevisionPage)
    async def list_project_kpi_revisions(
        request: Request,
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=KPI_REVISIONS_PAGE_MAX)] = 20,
        before: Annotated[
            int | None, Query(ge=1, description="Only revisions older than this one.")
        ] = None,
    ) -> dict[str, Any]:
        """The KPI set's history, newest first, with who changed it and when."""
        user_id = require_user(request, auth_provider)
        project = await load_project(project_id, user_id)
        rows = await asyncio.to_thread(
            kpi_store.revisions, project.id, user_id=user_id, limit=limit + 1, before=before
        )
        return {
            "object": "list",
            "data": [row.model_dump() for row in rows[:limit]],
            "has_more": len(rows) > limit,
        }

    return router
