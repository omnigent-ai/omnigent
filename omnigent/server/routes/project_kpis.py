"""``/v1/projects/{project_id}/kpis``: a project's KPI set, its values and its history."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from omnigent.db.utils import now_epoch
from omnigent.entities.project_kpi import (
    KPI_METRICS,
    KPI_REVISIONS_PAGE_MAX,
    KpiDirection,
    KpiMetric,
    KpiMetricId,
    KpiUnit,
    ProjectKpiRevision,
    SetProjectKpis,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.project_kpis import KpiInputs, KpiStatus, compute_metrics, kpi_rows
from omnigent.server.project_status import (
    StatusSources,
    StatusWindow,
    load_status_sources,
    status_period,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.projects import refuse_unless_owner_or_lead

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.server.auth import AuthProvider
    from omnigent.stores import ConversationStore, ProjectStore
    from omnigent.stores.project_kpi_store import ProjectKpiStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore


WindowParam = Annotated[
    StatusWindow, Query(description="7d, 30d or all, in UTC days, as on the Summary page.")
]


class ProjectKpiValue(BaseModel):
    """One KPI with its value for the window."""

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


class ProjectKpis(BaseModel):
    """The project's current KPI set with each value over a UTC window."""

    object: Literal["project.kpis"]
    project_id: str
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


class ProjectKpiRevisionPage(BaseModel):
    object: Literal["list"]
    data: list[ProjectKpiRevision]
    has_more: bool


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

    def build(
        request: Request,
        project: Project,
        window: StatusWindow,
        current: ProjectKpiRevision | None,
        user_id: str | None,
    ) -> dict[str, Any]:
        period = status_period(window, now_epoch())
        kpis = current.kpis if current else []
        inputs = KpiInputs(
            project=project,
            period=period,
            sources=sources(request),
            completed_moves=functools.cache(
                lambda: ticket_store.completed_moves(
                    project.id, user_id=user_id, since_epoch=period.since_epoch
                )
            ),
            stage_histories=functools.cache(
                lambda: ticket_store.stage_histories(project.id, user_id=user_id)
            ),
            stages=functools.cache(
                lambda: ticket_store.stages(project.id, user_id=user_id)["stages"]
            ),
        )
        values = compute_metrics((kpi.metric for kpi in kpis), inputs)
        return {
            "object": "project.kpis",
            "project_id": project.id,
            "window": window,
            "today_utc": period.today_utc,
            "since_day_utc": period.since_day_utc,
            "generated_at": period.until_epoch,
            "revision": current.revision if current else 0,
            "updated_at": current.created_at if current else None,
            "updated_by_user_id": current.created_by_user_id if current else None,
            "updated_by_session_id": current.created_by_session_id if current else None,
            "can_edit": project.user_id == user_id,
            "kpis": kpi_rows(kpis, values),
            "metrics": [metric.model_dump() for metric in KPI_METRICS],
        }

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
