"""``GET /v1/projects/{project_id}/status/summary``: the project's status for a window."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.project_status import (
    UNAVAILABLE,
    MemberKey,
    MemberTotals,
    StatusPeriod,
    StatusSources,
    StatusWindow,
    build_summary,
    load_status_sources,
    member_key_for_session,
    status_period,
    summary_members,
    user_member_key,
)
from omnigent.server.project_status_cache import period_key, project_status_cache
from omnigent.server.routes._auth_helpers import require_user

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.server.auth import AuthProvider
    from omnigent.stores import ConversationStore, ProjectStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ProjectStatusTotals(BaseModel):
    """Window totals. ``None`` means the metric's source isn't available."""

    tickets_done: int
    tickets_submitted: int = Field(
        default=0, description="Tickets created in the window, counted for who requested them."
    )
    prs_merged: int | None
    spend_cents: int | None
    cost_per_merged_pr_cents: int | None
    members: int


class ProjectStatusMember(BaseModel):
    """One member's numbers: a person, an agent identity, or the other-agents pool."""

    key: str
    kind: Literal["human", "identity", "agents"]
    name: str
    user_id: str | None
    identity_id: str | None
    tickets_done: int
    tickets_submitted: int = Field(
        default=0, description="Tickets this person requested in the window; 0 for agents."
    )
    prs_merged: int | None
    spend_cents: int | None
    cost_per_merged_pr_cents: int | None


class ProjectStatusSources(BaseModel):
    spend: Literal["available", "unavailable"]
    prs_merged: Literal["available", "unavailable"]


class ProjectStatusSummary(BaseModel):
    """A project's status over a UTC window; money is in integer US cents."""

    object: Literal["project.status_summary"]
    project_id: str
    window: Literal["7d", "30d", "all"]
    time_zone: Literal["UTC"]
    today_utc: str
    since_day_utc: str | None
    generated_at: int
    totals: ProjectStatusTotals
    members: list[ProjectStatusMember]
    sources: ProjectStatusSources
    # The project's charter for people (mission, scope, roster), or None when unset.
    description: str | None = None


def move_member_resolver(
    project: Project, conversations: ConversationStore | None
) -> Callable[[dict[str, Any]], MemberKey]:
    """Credit a ticket move to its session's member, else to the person who made it."""
    cache: dict[str, MemberKey | None] = {}

    def session_member(session_id: str, user_id: str | None) -> MemberKey | None:
        if session_id in cache:
            return cache[session_id]
        session = conversations.get_conversation(session_id) if conversations else None
        if session is None:
            return None
        if session.root_conversation_id and session.root_conversation_id != session.id:
            session = conversations.get_conversation(session.root_conversation_id) or session
        key = member_key_for_session(project, session, owner_user_id=user_id)
        cache[session_id] = key
        return key

    def resolve(move: dict[str, Any]) -> MemberKey:
        if move.get("member_key"):
            return move["member_key"]
        session_id = move.get("session_id")
        return (session_member(session_id, move.get("user_id")) if session_id else None) or (
            user_member_key(move.get("user_id") or project.user_id)
        )

    return resolve


def tickets_submitted_by_person(project: Project, rows: list[dict[str, Any]]) -> MemberTotals:
    """Tickets each person requested: the person the work is for, not who typed it."""
    by_member: dict[MemberKey, int] = {}
    for row in rows:
        key = user_member_key(row.get("requested_by") or project.user_id)
        by_member[key] = by_member.get(key, 0) + 1
    return MemberTotals(total=len(rows), by_member=by_member)


def tickets_done_by_member(
    project: Project,
    moves: list[dict[str, Any]],
    conversations: ConversationStore | None,
) -> MemberTotals:
    """Count distinct tickets completed per member: the session's, else the person's.

    A ticket reopened and completed again in the window counts once, for whoever
    made its latest completing move (*moves* are in time order).
    """
    member_of = move_member_resolver(project, conversations)
    latest = {move["ticket_id"]: move for move in moves}
    by_member: dict[MemberKey, int] = {}
    for move in latest.values():
        key = move.get("member_key") or member_of(move)
        by_member[key] = by_member.get(key, 0) + 1
    return MemberTotals(total=len(latest), by_member=by_member)


def _measure(source: Any, method: str, project: Project, period: StatusPeriod) -> MemberTotals:
    """Call one source, reporting it unavailable rather than failing the summary."""
    if source is None:
        return UNAVAILABLE
    try:
        result = getattr(source, method)(project, period)
    except Exception:
        logger.warning("Project status source %s failed", method, exc_info=True)
        return UNAVAILABLE
    return result if isinstance(result, MemberTotals) else UNAVAILABLE


def create_project_status_router(
    project_store: ProjectStore,
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

    @router.get("/projects/{project_id}/status/summary", response_model=ProjectStatusSummary)
    async def project_status_summary(
        request: Request,
        project_id: str,
        window: Annotated[StatusWindow, Query(description="7d, 30d or all, in UTC days.")] = "7d",
    ) -> dict[str, Any]:
        """Tickets done, PRs merged, spend and per-member numbers for a window.

        Any project member may read it. A window of N days is today (UTC) plus
        the N-1 UTC days before it.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        period = status_period(window, now_epoch())
        active = sources(request)

        def count_tickets_done() -> MemberTotals:
            moves = ticket_store.completed_moves(
                project.id, user_id=user_id, since_epoch=period.since_epoch
            )
            return tickets_done_by_member(project, moves, conversation_store)

        def count_tickets_submitted() -> MemberTotals:
            rows = ticket_store.requested_tickets(
                project.id, user_id=user_id, since_epoch=period.since_epoch
            )
            return tickets_submitted_by_person(project, rows)

        timings: dict[str, float] = {}

        async def timed(name: str, call: Callable[..., T], *args: Any) -> T:
            started = time.perf_counter()
            try:
                return await asyncio.to_thread(call, *args)
            finally:
                timings[name] = round((time.perf_counter() - started) * 1000, 1)

        # Independent reads run side by side; each source is cached per project and window.
        identities, shares, tickets_done, submitted, spend, prs = await asyncio.gather(
            timed(
                "identities",
                functools.partial(project_store.list_identities, user_id=user_id),
                project.id,
            ),
            timed(
                "shares", functools.partial(project_store.list_shares, user_id=user_id), project.id
            ),
            timed(
                "tickets_done",
                project_status_cache.get_or_compute,
                project.id,
                "tickets",
                ("tickets_done", period_key(period)),
                count_tickets_done,
            ),
            timed(
                "tickets_submitted",
                project_status_cache.get_or_compute,
                project.id,
                "tickets",
                ("tickets_submitted", period_key(period)),
                count_tickets_submitted,
            ),
            timed("spend", _measure, active.spend, "spend", project, period),
            timed("prs_merged", _measure, active.prs_merged, "prs_merged", project, period),
        )
        logger.debug("Project status summary %s %s timings (ms): %s", project.id, window, timings)
        seen = {*tickets_done.by_member, *submitted.by_member, *spend.by_member, *prs.by_member}
        roster = summary_members(project, identities, [share.user_id for share in shares], seen)
        summary = build_summary(
            project,
            period,
            roster=roster,
            tickets_done=tickets_done,
            spend=spend,
            prs_merged=prs,
            tickets_submitted=submitted,
        )
        return {**summary, "description": project.description}

    return router
