"""``/v1/projects/{id}/telemetry`` and ``/v1/telemetry``: how people work, from existing data."""

from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.project_kpis import (
    LANDED_COMMIT_LABELS,
    KpiInputs,
    changes_landed_total,
    compute_metrics,
    has_landed_commits,
    load_series,
    role_series,
    window_value,
)
from omnigent.server.project_spend_attribution import (
    finished_at,
    spend_by_role,
    spend_by_ticket,
)
from omnigent.server.project_status import (
    StatusSources,
    StatusWindow,
    identity_member_key,
    load_status_sources,
    status_period,
)
from omnigent.server.project_status_cache import project_status_cache
from omnigent.server.project_status_spend import micros_to_cents
from omnigent.server.project_telemetry import (
    PersonMessage,
    ProjectTelemetryReader,
    TelemetryInputs,
    add_usage_log_counts,
    compute_telemetry,
    review_rounds_and_stalled,
    visible_people,
)
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes.project_kpis import (
    ProjectStatusTrendDay,
    WindowParam,
    daily_trends,
)
from omnigent.server.routes.project_status import _measure, move_member_resolver
from omnigent.server.usage_events import usage_event_store

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
    sessions_by_client: dict[str, int] | None = Field(
        default=None,
        description=(
            "Sessions people started, by client (web, desktop, cli, slack, ...), from the "
            "usage log; null when it's off. Counts start at messages_measured_since."
        ),
    )


class RoleInteraction(BaseModel):
    """How people talk to the Lead and roles.

    Message counts come from the usage log, which records a message when a person sends
    it. They're null when the log is off: stored messages don't distinguish a person's
    words from the server's automatic notices.
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
    messages_measured_since: int | None = Field(
        default=None,
        description=(
            "When the usage log's oldest kept event was recorded (Unix seconds). A window "
            "starting earlier is only counted from then."
        ),
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
        description="Creation to last edit, over edited roles."
    )
    role_edits_in_window: int | None = Field(
        default=None,
        description="Role edits recorded by the usage log in the window; null when it's off.",
    )
    roles_edited_in_window: int | None = Field(
        default=None, description="Distinct roles edited in the window, from the usage log."
    )


class Throughput(BaseModel):
    """What the project shipped in the window; null when a source isn't on this server."""

    tickets_shipped: float | None
    merged_prs: float | None
    changes_landed: int | None = Field(
        default=None,
        description="Distinct commits landed in the window; null if the project never landed one.",
    )
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


class RoleSpend(BaseModel):
    """One role's spend: an agent role, the Lead, other agents, or people's own chats pooled."""

    key: str = Field(description="`identity:<id>`, the Lead's or workers' key, or `people`.")
    name: str
    identity_id: str | None
    spend_usd: float


class TicketSpend(BaseModel):
    ticket_id: str
    title: str | None
    spend_usd: float


class TicketSpendSplit(BaseModel):
    """Spend credited to tickets, the top ones listed; rows plus unattributed = the total."""

    tickets: list[TicketSpend] = Field(description="The most expensive tickets, largest first.")
    tickets_with_spend: int
    other_tickets_usd: float = Field(description="Spend on tickets beyond the listed ones.")
    attributed_usd: float
    unattributed_usd: float = Field(
        description="Spend of sessions with no ticket activity or open linked ticket that day."
    )


class SpendSplit(BaseModel):
    """How the window's spend splits by role and by ticket.

    A session-day's spend goes to the tickets that session (or its sub-agents) acted on
    that day, weighted by its events on each. Without any, it's split evenly over the
    tickets linked to the session that weren't finished before that day; otherwise it's
    unattributed. Each row is rounded to cents once, so sums may differ from
    ``total_usd`` by a cent per row.
    """

    total_usd: float
    by_role: list[RoleSpend]
    by_ticket: TicketSpendSplit


class TelemetryDailySeries(BaseModel):
    """The Summary's trend buckets: one per UTC day, or per week past 90 days."""

    bucket: Literal["day", "week"]
    days: list[ProjectStatusTrendDay]


class TelemetryRole(BaseModel):
    identity_id: str
    name: str


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
    spend: SpendSplit | None = Field(
        default=None, description="Spend by role and by ticket; null without a spend source."
    )
    daily: TelemetryDailySeries | None = Field(
        default=None,
        description="Per-day tickets filed and finished, open tickets, changes landed, PRs "
        "and spend, as the Summary's trend charts; only with series=daily.",
    )
    role: TelemetryRole | None = Field(
        default=None,
        description=(
            "Set when the request was filtered to one role: throughput and spend then count "
            "only its tickets (assignee) and sessions; the other sections stay project-wide."
        ),
    )
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


@dataclass(frozen=True)
class RoleScope:
    """One role's share of the project: its tickets, its root sessions and their spend."""

    ticket_ids: set[str]
    session_ids: set[str]
    spend_micros: int | None


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
        role: RoleScope | None = None,
    ) -> dict[str, Any]:
        active = sources(request)
        inputs = KpiInputs(
            project=project,
            period=period,
            sources=active,
            stage_histories=histories,
            stages=stages,
            history_since=period.since_epoch,
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
        stage_ages = ticket_store.ticket_stage_ages(project.id, user_id=user_id)
        if role is None:
            values = compute_metrics(THROUGHPUT_METRICS, inputs)
            spend_totals = _measure(active.spend, "spend", project, period)
            spend = spend_totals.total / 100 if spend_totals.available else None
            landed = changes_landed_total(inputs)
            role_histories = histories()
        else:
            series = role_series(
                load_series([*THROUGHPUT_METRICS, "changes_landed"], inputs),
                role.ticket_ids,
                role.session_ids,
            )
            values = {
                metric: window_value(series[metric], period) for metric in THROUGHPUT_METRICS
            }
            spend = None if role.spend_micros is None else micros_to_cents(role.spend_micros) / 100
            landed_value = window_value(series["changes_landed"], period)
            landed = (
                int(landed_value.value or 0)
                if has_landed_commits(inputs) and landed_value.available
                else None
            )
            role_histories = {
                ticket_id: history
                for ticket_id, history in histories().items()
                if ticket_id in role.ticket_ids
            }
            stage_ages = [row for row in stage_ages if row[0] in role.ticket_ids]
        return {
            **{
                metric: (value.value if value.available else None)
                for metric, value in values.items()
            },
            "spend_usd": spend,
            "changes_landed": landed,
            **review_rounds_and_stalled(role_histories, stages(), stage_ages, period),
        }

    def daily_series(
        request: Request,
        project: Project,
        period: Any,
        user_id: str | None,
        role: RoleScope | None,
    ) -> dict[str, Any]:
        """The trend buckets for *period*, narrowed to *role* when given."""
        inputs = KpiInputs(
            project=project,
            period=period,
            sources=sources(request),
            stage_histories=functools.cache(
                lambda: ticket_store.stage_histories(project.id, user_id=user_id)
            ),
            stages=functools.cache(
                lambda: ticket_store.stages(project.id, user_id=user_id)["stages"]
            ),
            ticket_labels=functools.cache(
                lambda: ticket_store.label_values(
                    project.id, LANDED_COMMIT_LABELS, user_id=user_id
                )
            ),
        )
        filed = ticket_store.requested_tickets(
            project.id, user_id=user_id, since_epoch=period.since_epoch
        )
        if role is None:
            return daily_trends(inputs, filed)
        return daily_trends(inputs, filed, role.ticket_ids, role.session_ids)

    def spend_split(
        project: Project,
        period: Any,
        user_id: str | None,
        rows: list[Any] | None,
        identities: list[Any],
    ) -> dict[str, Any] | None:
        """Spend by role and by ticket over *rows* (see :class:`SpendSplit`)."""
        if rows is None:
            return None
        sessions = {root for root, _, _, _ in rows}
        activity = ticket_store.session_ticket_events(
            project.id, user_id=user_id, since_epoch=period.since_epoch
        )
        links = ticket_store.ticket_session_links(project.id, user_id=user_id)
        sessions.update(session_id for _, session_id, _ in [*activity, *links])
        found = conversation_store.get_conversations(sorted(sessions)) if sessions else {}
        root_of = {
            session_id: conversation.root_conversation_id or session_id
            for session_id, conversation in found.items()
        }
        histories = ticket_store.stage_histories(project.id, user_id=user_id)
        by_ticket = spend_by_ticket(rows, root_of, activity, links, finished_at(histories), {})
        titles = ticket_store.ticket_titles(
            project.id, [row["ticket_id"] for row in by_ticket["tickets"]], user_id=user_id
        )
        for row in by_ticket["tickets"]:
            row["title"] = titles.get(row["ticket_id"])
        names = {identity_member_key(identity.id): identity.name for identity in identities}
        return {
            "total_usd": micros_to_cents(sum(micros for _, _, micros, _ in rows)) / 100,
            "by_role": spend_by_role(rows, names),
            "by_ticket": by_ticket,
        }

    def build(
        request: Request,
        project: Project,
        window: StatusWindow,
        user_id: str | None,
        identity_id: str | None = None,
        series: str | None = None,
    ) -> dict[str, Any]:
        period = status_period(window, now_epoch())
        identities = project_store.list_identities(project.id, user_id=user_id)
        identity = None
        if identity_id is not None:
            identity = next((item for item in identities if item.id == identity_id), None)
            if identity is None:
                raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
        spend_source = sources(request).spend
        spend_rows = (
            spend_source.root_days(project, period)
            if spend_source is not None and hasattr(spend_source, "root_days")
            else None
        )
        role = None
        if identity is not None:
            key = identity_member_key(identity.id)
            spend_rows = (
                None if spend_rows is None else [row for row in spend_rows if row[3] == key]
            )
            role = RoleScope(
                ticket_ids=ticket_store.assigned_ticket_ids(
                    project.id, identity.name, user_id=user_id
                ),
                session_ids={row[0] for row in spend_rows or ()},
                spend_micros=None if spend_rows is None else sum(row[2] for row in spend_rows),
            )
        histories = functools.cache(
            lambda: ticket_store.stage_histories(
                project.id, user_id=user_id, since_epoch=period.since_epoch
            )
        )
        stages = functools.cache(
            lambda: ticket_store.stages(project.id, user_id=user_id)["stages"]
        )
        usage = usage_event_store(request.app)
        sessions = functools.cache(lambda: reader.sessions(project))
        events = functools.cache(
            lambda: (
                []
                if usage is None
                else usage.events(
                    project.id,
                    since_epoch=period.since_epoch,
                    names=("message.sent", "session.started", "role.updated"),
                )
            )
        )
        inputs = TelemetryInputs(
            project=project,
            period=period,
            sessions=sessions,
            person_messages=lambda _sessions: (
                None
                if usage is None
                else [
                    PersonMessage(event.session_id, event.user_id or "local", event.created_at)
                    for event in events()
                    if event.name == "message.sent" and event.session_id and event.user_id
                ]
            ),
            activity=lambda: ticket_store.activity_actors(
                project.id, user_id=user_id, since_epoch=period.since_epoch
            ),
            identities=lambda: identities,
            member_of_move=move_member_resolver(project, conversation_store),
        )
        result = compute_telemetry(inputs)
        rows = result.pop("people_rows")
        if usage is not None:
            add_usage_log_counts(result, events(), sessions(), period, usage.first_event_at())
        return {
            "object": "project.telemetry",
            "project_id": project.id,
            "project_name": project.name,
            "window": window,
            "since_day_utc": period.since_day_utc,
            "today_utc": period.today_utc,
            **result,
            "throughput": throughput(request, project, period, user_id, histories, stages, role),
            "spend": spend_split(project, period, user_id, spend_rows, identities),
            "daily": None
            if series != "daily"
            else daily_series(request, project, period, user_id, role),
            "role": None
            if identity is None
            else {"identity_id": identity.id, "name": identity.name},
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
        identity_id: Annotated[
            str | None,
            Query(
                description="Only this role's throughput and spend; the rest stays project-wide."
            ),
        ] = None,
        series: Annotated[
            Literal["daily"] | None,
            Query(description="daily adds the Summary's per-day trend series as `daily`."),
        ] = None,
    ) -> dict[str, Any]:
        """How people work in a project: starting work, talking to roles, building the team.

        Any project member may read the totals. Per-person rows go to the owner;
        a member gets only their own, and agents get none.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return await asyncio.to_thread(
            build, request, project, window, user_id, identity_id, series
        )

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
