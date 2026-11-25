"""``/v1/projects/{id}/telemetry`` and ``/v1/telemetry``: how people work, from existing data."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.project_kpis import KpiInputs, compute_metrics
from omnigent.server.project_status import (
    StatusSources,
    StatusWindow,
    load_status_sources,
    status_period,
)
from omnigent.server.project_telemetry import (
    ProjectTelemetryReader,
    TelemetryInputs,
    compute_telemetry,
    review_rounds_and_stalled,
    visible_people,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.project_kpis import WindowParam
from omnigent.server.routes.project_status import _measure, move_member_resolver

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.server.auth import AuthProvider
    from omnigent.stores import ConversationStore, PermissionStore, ProjectStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore

THROUGHPUT_METRICS = (
    "tickets_shipped",
    "merged_prs",
    "rework_tickets",
    "time_to_merged_median_hours",
)


class WorkStart(BaseModel):
    """How people start work: sessions vs tickets."""

    sessions_started_by_people: int
    tickets_filed_directly: int = Field(description="Filed by a person on the board.")
    tickets_filed_from_a_chat: int = Field(description="Filed by an agent in a person's chat.")
    tickets_filed_by_agents: int = Field(description="Filed by the Lead, a role or a worker.")
    people: dict[Literal["session_first", "ticket_first", "both"], int] = Field(
        description="People who only started sessions, only filed tickets, or did both."
    )


class RoleInteraction(BaseModel):
    """How people talk to the Lead and roles.

    Message counts, and the people counts derived from them, are null until the usage
    log measures them: stored messages don't distinguish a person's words from the
    server's automatic notices.
    """

    messages_to_lead: int | None
    messages_to_roles: int | None
    people_messaging_roles: int | None
    side_chats_opened: int
    side_chats_with_roles: int
    ticket_comments_by_people: int
    people_ticket_only: int | None = Field(
        description="People who commented on tickets but never chatted with the Lead or a role."
    )


class TeamBuilding(BaseModel):
    """How the team of roles was built and adapted."""

    roles: int
    roles_created_by_lead: int
    roles_created_by_hand: int
    roles_created_in_window: int
    roles_created_in_window_by_lead: int
    roles_edited_after_creation: int
    median_hours_to_last_role_edit: float | None = Field(
        description="Creation to last edit, over edited roles; earlier edits aren't kept yet."
    )


class Throughput(BaseModel):
    """What the project shipped in the window; null when a source isn't on this server."""

    tickets_shipped: float | None
    merged_prs: float | None
    rework_tickets: float | None
    time_to_merged_median_hours: float | None
    spend_usd: float | None
    review_rounds_per_shipped_ticket: float | None = Field(
        description=(
            "Moves into a review stage per shipped ticket. A review stage is one whose ID or "
            "name contains 'review'; null when the project has none."
        )
    )
    stalled_tickets: int = Field(
        description=(
            "In a working stage (not the first or a completed one) whose ticket hasn't been "
            "updated for 3+ days; any update, including a label change, resets the clock."
        )
    )


class PersonActivity(BaseModel):
    user_id: str
    sessions_started: int
    tickets_filed: int
    messages_to_lead: int | None = Field(description="Null until people's messages are measured.")
    messages_to_roles: int | None = Field(description="Null until people's messages are measured.")
    side_chats_opened: int
    ticket_comments: int


class ProjectTelemetry(BaseModel):
    """One project's telemetry over a UTC window, from data Omnigent already keeps."""

    object: Literal["project.telemetry"]
    project_id: str
    project_name: str
    window: StatusWindow
    since_day_utc: str | None
    today_utc: str
    work_start: WorkStart
    role_interaction: RoleInteraction
    team_building: TeamBuilding
    throughput: Throughput
    people: list[PersonActivity] | None = Field(
        description="All people for the owner, the caller's own row for a member, null for agents."
    )


class TelemetryOverview(BaseModel):
    """Telemetry for every project the caller owns."""

    object: Literal["telemetry.overview"]
    window: StatusWindow
    since_day_utc: str | None
    today_utc: str
    projects: list[ProjectTelemetry]


def create_project_telemetry_router(
    project_store: ProjectStore,
    ticket_store: ProjectTicketStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    router = APIRouter()
    reader = ProjectTelemetryReader(conversation_store, permission_store)

    def sources(request: Request) -> StatusSources:
        state = request.app.state
        loaded = getattr(state, "project_status_sources", None)
        if loaded is None:
            loaded = load_status_sources(state)
            state.project_status_sources = loaded
        return loaded

    def is_agent(request: Request) -> bool:
        return (
            ORCHESTRATION_SOURCE_HEADER in request.headers
            or RUNNER_TUNNEL_TOKEN_HEADER in request.headers
        )

    def throughput(
        request: Request,
        project: Project,
        period: Any,
        user_id: str | None,
        histories: Any,
        stages: Any,
    ) -> dict[str, Any]:
        active = sources(request)
        inputs = KpiInputs(
            project=project,
            period=period,
            sources=active,
            stage_histories=histories,
            stages=stages,
            history_since=period.since_epoch,
        )
        values = compute_metrics(THROUGHPUT_METRICS, inputs)
        spend_totals = _measure(active.spend, "spend", project, period)
        spend = spend_totals.total / 100 if spend_totals.available else None
        return {
            **{
                metric: (value.value if value.available else None)
                for metric, value in values.items()
            },
            "spend_usd": spend,
            **review_rounds_and_stalled(
                histories(),
                stages(),
                ticket_store.ticket_stage_ages(project.id, user_id=user_id),
                period,
            ),
        }

    def build(
        request: Request, project: Project, window: StatusWindow, user_id: str | None
    ) -> dict[str, Any]:
        period = status_period(window, now_epoch())
        histories = functools.cache(
            lambda: ticket_store.stage_histories(
                project.id, user_id=user_id, since_epoch=period.since_epoch
            )
        )
        stages = functools.cache(
            lambda: ticket_store.stages(project.id, user_id=user_id)["stages"]
        )
        inputs = TelemetryInputs(
            project=project,
            period=period,
            sessions=lambda: reader.sessions(project),
            # People's messages are measured once the usage log records them as sent.
            person_messages=lambda _sessions: None,
            activity=lambda: ticket_store.activity_actors(
                project.id, user_id=user_id, since_epoch=period.since_epoch
            ),
            identities=lambda: project_store.list_identities(project.id, user_id=user_id),
            member_of_move=move_member_resolver(project, conversation_store),
        )
        result = compute_telemetry(inputs)
        rows = result.pop("people_rows")
        return {
            "object": "project.telemetry",
            "project_id": project.id,
            "project_name": project.name,
            "window": window,
            "since_day_utc": period.since_day_utc,
            "today_utc": period.today_utc,
            **result,
            "throughput": throughput(request, project, period, user_id, histories, stages),
            "people": visible_people(
                rows,
                viewer=user_id,
                owner=project.user_id == user_id,
                agent=is_agent(request),
            ),
        }

    @router.get("/projects/{project_id}/telemetry", response_model=ProjectTelemetry)
    async def project_telemetry(
        request: Request,
        project_id: str,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """How people work in a project: starting work, talking to roles, building the team.

        Any project member may read the totals. Per-person rows go to the owner;
        a member gets only their own, and agents get none.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await asyncio.to_thread(build, request, project, window, user_id)

    @router.get("/telemetry", response_model=TelemetryOverview)
    async def telemetry_overview(
        request: Request,
        window: WindowParam = "7d",
    ) -> dict[str, Any]:
        """Telemetry for every project the caller owns, side by side.

        Projects shared with the caller aren't included; each owner sees only their own.
        Agents are refused: an agent works in one project and reads that project's view.
        """
        user_id = require_user(request, auth_provider)
        if is_agent(request):
            raise OmnigentError(
                "Agents can read a project's telemetry, not the owner's overview",
                code=ErrorCode.FORBIDDEN,
            )
        projects = await asyncio.to_thread(project_store.list, user_id=user_id)
        owned = [project for project in projects if project.user_id == user_id]
        period = status_period(window, now_epoch())
        rows = [
            await asyncio.to_thread(build, request, project, window, user_id) for project in owned
        ]
        return {
            "object": "telemetry.overview",
            "window": window,
            "since_day_utc": period.since_day_utc,
            "today_utc": period.today_utc,
            "projects": rows,
        }

    return router
