"""Owner-private, project-scoped ticket APIs. There is intentionally no delete route."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import StatementError

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.entities.project_routing import LEAD_NAME
from omnigent.entities.project_ticket import (
    TICKET_PAGE_MAX,
    CommentOnTicket,
    CreateTicket,
    FileIntakeTicket,
    ImportPullRequest,
    LandTicketRequest,
    LinkRelatedTicket,
    LinkTicketSession,
    MarkTicketSeen,
    RevertDraftRequest,
    TicketActor,
    TicketEventKind,
    TicketLabel,
    TicketStageId,
    UpdateTicket,
    UpdateTicketStages,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.builder import load_session_usage_and_tree
from omnigent.runtime.project_ticket_instructions import (
    FINAL_UPDATE_REMINDER,
    PROJECT_TICKET_WORK_INSTRUCTION,
)
from omnigent.server import (
    github_pr_intake,
    github_pr_status,
    product_approval,
    project_release,
    running_commit,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    RESERVED_USER_LOCAL,
    RESERVED_USER_PUBLIC,
    AuthProvider,
)
from omnigent.server.persona_mentions import (
    Mention,
    MentionRateLimiter,
    PersonaMentionTargets,
    deliver_mentions,
    find_mentions,
    person_aliases,
    split_mentions,
)
from omnigent.server.project_kpis import ticket_times
from omnigent.server.project_landing import RISK_OK_LABEL, could_supersede, is_pure_rebase
from omnigent.server.project_reliability import settings as reliability_settings
from omnigent.server.project_router import router_session_ids
from omnigent.server.project_status_spend import project_total_cents
from omnigent.server.role_side_chat import refuse_role_side_chat
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._sessions.common import (
    _session_running_since,
    _session_status_cache,
)
from omnigent.server.routes._sessions.helpers import (
    _permission_level_from_grants,
    _resolve_harness,
)
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.server.ticket_review import (
    APPROVED_LABEL,
    REVIEW_STAGE_ID,
    RouteLimiter,
    approval_can_be_superseded,
    check_approval_change,
    find_reviewer,
    is_reviewer_session,
    label_value,
    reviewer_handoff_message,
    superseding_sha,
    to_reviewer_message,
    to_worker_message,
)
from omnigent.server.ticket_stall import (
    BoardStall,
    WorkerState,
    card_stall_after,
    in_working_stage,
    ticket_stall,
    ticket_wait,
    ticket_working,
)
from omnigent.server.ticket_summary import TicketSummarizer, build_ticket_summarizer
from omnigent.server.usage_events import record_usage_event
from omnigent.stores import AgentStore, ConversationStore, PermissionStore, ProjectStore
from omnigent.stores.conversation_store import SessionActivity
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.telemetry.surface import resolve_client_surface
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except StatementError as exc:
        if not isinstance(exc.orig, ValueError):
            raise
        raise OmnigentError(
            "Ticket content exceeds its storage limit", code=ErrorCode.INVALID_INPUT
        ) from exc


def trailing_window_start(days: int) -> str:
    """First UTC day of a trailing window of *days* days ending today, as ``"YYYY-MM-DD"``."""
    from omnigent.db.utils import now_epoch

    today = datetime.fromtimestamp(now_epoch(), tz=UTC).date()
    return (today - timedelta(days=days - 1)).isoformat()


def linked_sessions_cost(
    session_ids: list[str], conversations: ConversationStore
) -> dict[str, Any]:
    """
    Sum the recorded cost of *session_ids*, each including its sub-agent subtree.

    A linked session spawned inside another linked session is already in its
    ancestor's subtree, so it is listed but not added again. A session with no
    priced turn reports ``cost_usd: None`` rather than ``0``.
    """
    linked = set(session_ids)
    rows: list[dict[str, Any]] = []
    total = 0.0
    unpriced = 0
    for session_id in session_ids:
        usage, tree = load_session_usage_and_tree(session_id, conversations)
        parents = {conv.id: conv.parent_conversation_id for conv in tree}
        ancestor = parents.get(session_id)
        nested = False
        while ancestor is not None:
            if ancestor in linked:
                nested = True
                break
            ancestor = parents.get(ancestor)
        cost: float | None = None
        if "total_cost_usd" in usage:
            try:
                cost = float(usage["total_cost_usd"])
            except (TypeError, ValueError):
                cost = None
        if cost is None:
            unpriced += 1
        elif not nested:
            total += cost
        rows.append({"session_id": session_id, "cost_usd": cost, "nested": nested})
    return {
        "cost_usd": total,
        "session_count": len(session_ids),
        "unpriced_sessions": unpriced,
        "sessions": rows,
    }


def _has_running_agents(
    session_ids: list[str], sessions: dict[str, SessionActivity], *, has_more: bool
) -> bool | None:
    unknown = has_more
    for session_id in session_ids:
        activity = sessions.get(session_id)
        if activity is None or activity.archived is None:
            unknown = True
            continue
        if activity.archived:
            continue
        # Use this linked session's own turn, without project or child rollups.
        status = _session_status_cache.get(session_id, activity.live_status)
        if status == "running":
            return True
        if status not in ("idle", "waiting", "failed"):
            unknown = True
    return None if unknown else False


_logger = logging.getLogger(__name__)

# Intake tickets one project may file on another project's board per rolling hour.
INTAKE_TICKETS_PER_HOUR = 10
INTAKE_REQUESTER_PREFIX = "project:"


def intake_description(source_name: str, sender: str | None, description: str) -> str:
    """Prefix an intake ticket's description with where it came from."""
    on_behalf = f" on behalf of {sender}" if sender else ""
    note = (
        f"> **From the {source_name} project.** Filed by one of its agents{on_behalf}. "
        "This project's own rules and approvals apply."
    )
    return f"{note}\n\n{description}".rstrip()


_mention_limiter = MentionRateLimiter()
_mention_tasks: set[asyncio.Task[None]] = set()
# A ticket rarely has more; it bounds GitHub reads per view.
_PULL_REQUEST_STATUS_MAX = 20


def _spawn_mention_delivery(
    request: Request,
    targets: PersonaMentionTargets,
    project: Any,
    ticket: dict[str, Any],
    text: str,
    mentions: list[Mention],
    who: TicketActor,
) -> None:
    """Wake each cc'd persona in the background, so posting never waits on a restore."""
    app = request.app
    notifications = getattr(app.state, "project_ticket_notifications", None)
    if notifications is None:
        return
    if not mentions:
        return

    async def deliver(session_id: str, stable_id: str, message: str) -> bool:
        return await notifications.deliver_to_session(
            app, project, session_id, stable_id=stable_id, text=message
        )

    async def run() -> None:
        wanted = mentions
        # A human comment already wakes the router through the ticket update itself.
        if who.session_id is None:
            from omnigent.server.project_router import project_router

            router_id = (await project_router(app, project)).identity_id
            wanted = [entry for entry in mentions if entry.id != router_id]
            if not wanted:
                return
        # A person's comment with mentions goes only to the named identities, linked or not.
        outcomes = await deliver_mentions(
            project,
            ticket,
            text,
            wanted,
            event_id=f"{ticket['id']}:{ticket['comment_count']}",
            author_session_id=who.session_id,
            open_session=lambda mention: targets.open(request, project, mention),
            deliver=deliver,
            limiter=_mention_limiter,
        )
        _logger.info("Ticket %s mentions: %s", ticket["id"], outcomes)

    task = asyncio.create_task(run(), name=f"ticket-mentions-{ticket['id']}")
    _mention_tasks.add(task)
    task.add_done_callback(_mention_tasks.discard)


_review_limiter = RouteLimiter()
# Reviewer rounds since the ticket last entered Review, for the escalation reminder.
_review_rounds: dict[str, int] = {}
# Tickets whose next Reviewer round only re-approves a pure rebase, so it isn't counted.
_rebase_only_rounds: set[str] = set()


def _count_review_round(ticket_id: str) -> None:
    if ticket_id in _rebase_only_rounds:
        _rebase_only_rounds.discard(ticket_id)
        return
    _review_rounds[ticket_id] = _review_rounds.get(ticket_id, 0) + 1


def _stable_id(*parts: object) -> str:
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()[:32]


def _spawn_review_routing(
    request: Request,
    targets: PersonaMentionTargets,
    project: Any,
    reviewer: Mention,
    ticket: dict[str, Any],
    text: str | None,
    who: TicketActor,
    *,
    entered_review: bool,
    author_is_reviewer: bool,
) -> None:
    """Carry a ticket in Review between its worker and the Reviewer, in the background."""
    app = request.app
    notifications = getattr(app.state, "project_ticket_notifications", None)
    if notifications is None or who.session_id is None:
        return
    if not entered_review and not text:
        return

    async def deliver(session_id: str, stable_id: str, message: str) -> bool:
        return await notifications.deliver_to_session(
            app, project, session_id, stable_id=stable_id, text=message
        )

    async def run() -> None:
        if not _review_limiter.allow(ticket["id"]):
            _logger.warning("Review routing paused for ticket %s: too many turns", ticket["id"])
            return
        event = f"{ticket['id']}:{ticket['comment_count']}:{ticket.get('updated_at')}"
        if entered_review:
            _review_rounds[ticket["id"]] = 0
            _rebase_only_rounds.discard(ticket["id"])
            session_id = await targets.open(request, project, reviewer)
            if session_id and session_id != who.session_id:
                message = reviewer_handoff_message(ticket, text, who.session_id)
                await deliver(session_id, _stable_id("review-handoff", event), message)
            return
        assert text is not None
        if author_is_reviewer:
            _count_review_round(ticket["id"])
            linked = await asyncio.to_thread(
                notifications.tickets.ticket_session_ids, project.id, ticket["id"]
            )
            from omnigent.server.project_router import router_session_ids

            skipped = {who.session_id, *await router_session_ids(request.app, project)}
            for session_id in linked:
                if session_id in skipped:
                    continue
                message = to_worker_message(ticket, text)
                await deliver(
                    session_id, _stable_id("review-to-worker", event, session_id), message
                )
            return
        session_id = await targets.open(request, project, reviewer)
        if session_id and session_id != who.session_id:
            rounds = _review_rounds.get(ticket["id"], 0)
            message = to_reviewer_message(ticket, text, who.session_id, rounds)
            await deliver(session_id, _stable_id("review-to-reviewer", event), message)

    async def guarded() -> None:
        try:
            await run()
        except Exception:
            _logger.warning("Could not route review traffic for %s", ticket["id"], exc_info=True)

    task = asyncio.create_task(guarded(), name=f"ticket-review-{ticket['id']}")
    _mention_tasks.add(task)
    task.add_done_callback(_mention_tasks.discard)


# A ticket update with only these fields is a comment, which a role's side chat may post.
_TICKET_COMMENT_FIELDS = frozenset({"comment", "audience"})


def create_project_tickets_router(
    store: ProjectTicketStore,
    conversations: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permissions: PermissionStore | None = None,
    agents: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    projects: ProjectStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}/tickets")

    async def actor(
        request: Request, project_id: str, coordination: str | None = None
    ) -> TicketActor:
        """The caller; *coordination* names a write a role's side chat may not make."""
        user_id = require_user(request, auth_provider)
        raw_source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if raw_source is None:
            return TicketActor(user_id)
        if coordination is not None:
            await asyncio.to_thread(
                refuse_role_side_chat,
                request,
                action=coordination,
                user_id=user_id,
                conversations=conversations,
                projects=projects,
            )
        await asyncio.to_thread(
            require_named_source_private, request, user_id, conversations, permissions
        )
        try:
            source_id = uuid_to_bytes(raw_source).hex()
            project = uuid_to_bytes(project_id).hex()
        except InvalidUuidError as exc:
            raise OmnigentError(
                "Invalid calling session or project", code=ErrorCode.FORBIDDEN
            ) from exc
        source = await asyncio.to_thread(conversations.get_conversation, source_id)
        if source is None or source.project_id != project or source.archived:
            raise OmnigentError(
                "Ticket tools are scoped to their calling session's project",
                code=ErrorCode.FORBIDDEN,
            )
        return TicketActor(user_id, source.id, source.agent_id)

    async def load_project(request: Request, project_id: str, who: TicketActor) -> Any:
        """The project as the caller sees it, with its owner and config."""
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        try:
            normalized = uuid_to_bytes(project_id).hex()
        except InvalidUuidError:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND) from None
        project = (
            await asyncio.to_thread(notifications.projects.get, normalized, user_id=who.user_id)
            if notifications is not None
            else None
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project

    async def refuse_owner_label_change(
        request: Request,
        project_id: str,
        before_labels: list[dict[str, Any]] | None,
        after_labels: list[dict[str, Any]],
        who: TicketActor,
        requested_by: str | None,
    ) -> None:
        """Sign-off labels are a person's own edit, and only the right person's.

        - ``owner-ok``: the owner only, never an agent.
        - ``risk-ok``: the owner, or a product approver who didn't request the ticket.
        - ``product-ok``: a product approver only, never an agent.
        - ``approved``: people other than the owner are refused (the Reviewer's session
          is checked separately).
        - ``kind``: the orchestrator, the Reviewer, the owner or a product approver,
          so a worker or member can't relabel a feature as a fix.
        """
        keys = (
            APPROVED_LABEL,
            RISK_OK_LABEL,
            github_pr_intake.OWNER_OK_LABEL,
            product_approval.PRODUCT_OK_LABEL,
            product_approval.KIND_LABEL,
        )
        changed = [
            key
            for key in keys
            if label_value(after_labels, key) != label_value(before_labels, key)
        ]
        if not changed:
            return
        is_agent = who.session_id is not None or who.agent_id is not None
        sign_offs = [
            key
            for key in changed
            if key
            in (RISK_OK_LABEL, github_pr_intake.OWNER_OK_LABEL, product_approval.PRODUCT_OK_LABEL)
        ]
        if is_agent and sign_offs:
            ask = "ask them on the ticket with needs-human"
            if product_approval.PRODUCT_OK_LABEL in sign_offs:
                project = await load_project(request, project_id, who)
                ask = (
                    "ask them on the ticket with needs-human; only these people can add "
                    f"`{product_approval.PRODUCT_OK_LABEL}`: "
                    f"{', '.join(product_approval.approver_list(project))}"
                )
            raise OmnigentError(
                f"Only a person can set {', '.join(f'`{key}`' for key in sign_offs)}; {ask}",
                code=ErrorCode.FORBIDDEN,
            )
        project = await load_project(request, project_id, who)
        approvers = product_approval.product_approvers(project)
        if product_approval.KIND_LABEL in changed:
            if is_agent:
                from omnigent.server.project_router import router_session_id

                context = await review_context(request, project_id, who)
                router = await router_session_id(request.app, project, restore=False)
                allowed = who.session_id is not None and (
                    who.session_id == router or (context is not None and context[3])
                )
            else:
                allowed = who.user_id in approvers
            if not allowed:
                raise OmnigentError(
                    "Only the project's router (its Lead by default), the Reviewer or a product "
                    "approver can set `kind`",
                    code=ErrorCode.FORBIDDEN,
                )
            kind = product_approval.ticket_kind(after_labels)
            if kind is not None and kind not in product_approval.KINDS:
                raise OmnigentError(
                    f"`kind` must be one of {', '.join(product_approval.KINDS)}",
                    code=ErrorCode.INVALID_INPUT,
                )
        if is_agent:
            return
        if product_approval.PRODUCT_OK_LABEL in changed and who.user_id not in approvers:
            raise OmnigentError(
                product_approval.approver_refusal(project), code=ErrorCode.FORBIDDEN
            )
        if RISK_OK_LABEL in changed and who.user_id not in product_approval.risk_ok_givers(
            project, requested_by
        ):
            raise OmnigentError(
                product_approval.risk_ok_refusal(project, requested_by),
                code=ErrorCode.FORBIDDEN,
            )
        owner_only = [
            key for key in changed if key in (APPROVED_LABEL, github_pr_intake.OWNER_OK_LABEL)
        ]
        if owner_only and project.user_id != who.user_id:
            raise OmnigentError(
                f"Only the project owner can set {', '.join(f'`{key}`' for key in owner_only)}",
                code=ErrorCode.FORBIDDEN,
            )

    async def review_context(
        request: Request, project_id: str, who: TicketActor
    ) -> tuple[PersonaMentionTargets, Any, Mention, bool] | None:
        """The project's Reviewer and whether *who* is its session, or None without one."""
        targets: PersonaMentionTargets | None = getattr(
            request.app.state, "persona_mention_targets", None
        )
        if targets is None:
            return None
        project = await targets.project(project_id, who.user_id)
        reviewer = find_reviewer(await targets.personas(project))
        if reviewer is None:
            return None
        conversation = (
            await asyncio.to_thread(conversations.get_conversation, who.session_id)
            if who.session_id is not None
            else None
        )
        return targets, project, reviewer, is_reviewer_session(conversation, reviewer)

    async def after_review_activity(
        request: Request,
        project_id: str,
        ticket: dict[str, Any],
        text: str | None,
        who: TicketActor,
        *,
        entered_review: bool,
        context: tuple[PersonaMentionTargets, Any, Mention, bool] | None,
    ) -> dict[str, Any]:
        """Clear a superseded approval and route Review traffic; returns the latest ticket."""
        if who.session_id is None:
            return ticket
        context = context or await review_context(request, project_id, who)
        if context is None:
            return ticket
        targets, project, reviewer, author_is_reviewer = context
        approved = label_value(ticket.get("labels"), APPROVED_LABEL)
        newer = None if author_is_reviewer else superseding_sha(text, approved)
        if newer is not None:
            assert approved is not None
            stage_ids = [
                stage["id"]
                for stage in (
                    await asyncio.to_thread(store.stages, project_id, user_id=who.user_id)
                )["stages"]
            ]
            if not approval_can_be_superseded(
                ticket.get("stage_id"), stage_ids
            ) or not await asyncio.to_thread(could_supersede, project.config, approved, newer):
                newer = None
        if newer is not None:
            assert approved is not None
            rebase_only = await asyncio.to_thread(is_pure_rebase, project.config, approved, newer)
            if rebase_only:
                _rebase_only_rounds.add(ticket["id"])
            kept = [
                TicketLabel(**label)
                for label in ticket.get("labels") or []
                if label.get("key") != APPROVED_LABEL
            ]
            with _storage_errors():
                ticket = await asyncio.to_thread(
                    store.update,
                    project_id,
                    ticket["id"],
                    UpdateTicket(
                        labels=kept,
                        comment=(
                            f"Approval cleared: commit `{newer}` supersedes the approved "
                            f"`{approved}`. "
                            + (
                                "It is the identical patch rebased, so the landing queue "
                                "carries the approval; re-review is optional and doesn't "
                                "count as a review round."
                                if rebase_only
                                else "The Reviewer re-reviews it."
                            )
                        ),
                    ),
                    who,
                )
        if entered_review or ticket.get("stage_id") == REVIEW_STAGE_ID:
            _spawn_review_routing(
                request,
                targets,
                project,
                reviewer,
                ticket,
                text,
                who,
                entered_review=entered_review,
                author_is_reviewer=author_is_reviewer,
            )
        return ticket

    def wake(request: Request) -> None:
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        if notifications is not None:
            notifications.wake()

    async def check_session(who: TicketActor, project_id: str, session_id: str) -> str:
        try:
            session_id = uuid_to_bytes(session_id).hex()
        except InvalidUuidError as exc:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND) from exc
        await require_access(who.user_id, session_id, LEVEL_EDIT, permissions, conversations)
        target = await asyncio.to_thread(conversations.get_conversation, session_id)
        if target is None or target.project_id != uuid_to_bytes(project_id).hex():
            raise OmnigentError(
                "Ticket and session must belong to the same project",
                code=ErrorCode.INVALID_INPUT,
            )
        return session_id

    async def visible_sessions(who: TicketActor, session_ids: list[str]) -> list[str]:
        if permissions is None or not session_ids:
            return session_ids
        if who.user_id is None:
            return []
        grants = await asyncio.to_thread(
            permissions.list_for_sessions, session_ids, user_id=who.user_id
        )
        public_grants = await asyncio.to_thread(
            permissions.list_for_sessions, session_ids, user_id=RESERVED_USER_PUBLIC
        )
        is_admin = await asyncio.to_thread(permissions.is_admin, who.user_id)
        # Members of a shared project see every session filed in it.
        via_project = await asyncio.to_thread(permissions.project_levels, who.user_id, session_ids)
        return [
            sid
            for sid in session_ids
            if sid in via_project
            or _permission_level_from_grants(
                who.user_id, grants.get(sid, []) + public_grants.get(sid, []), is_admin
            )
            is not None
        ]

    async def enrich(
        project_id: str,
        tickets: list[dict[str, Any]],
        who: TicketActor,
        *,
        board: BoardStall | None = None,
    ) -> None:
        """Add agents, running state and seen state; with *board*, the stall and working fields."""
        if not tickets:
            return
        links = await asyncio.to_thread(
            store.activity_sessions,
            project_id,
            [ticket["id"] for ticket in tickets],
            user_id=who.user_id,
        )
        session_ids = await visible_sessions(
            who, sorted({sid for link in links.values() for sid in link.session_ids})
        )
        sessions = await asyncio.to_thread(conversations.get_session_activity, session_ids)

        def identities() -> dict[str, tuple[str, str | None, str | None]]:
            rows = conversations.get_conversations(session_ids)
            project = uuid_to_bytes(project_id).hex()
            known_agents = (
                agents.get_many(list({row.agent_id for row in rows.values() if row.agent_id}))
                if agents is not None
                else {}
            )
            personas = _persona_names(project) if rows else {}
            return {
                row.id: (
                    row.sub_agent_name or known_agents[row.agent_id].name,
                    _resolve_harness(row, agent_cache=agent_cache, agents_by_id=known_agents),
                    personas.get(row.id)
                    or personas.get((row.labels or {}).get(PROJECT_IDENTITY_LABEL_KEY, "")),
                )
                for row in rows.values()
                if row.project_id == project
                and row.agent_id is not None
                and row.agent_id in known_agents
            }

        def _persona_names(project: str) -> dict[str, str]:
            """Persona names keyed by identity id, plus the Lead's current and former sessions."""
            if projects is None:
                return {}
            record = projects.get(project, user_id=who.user_id)
            if record is None:
                return {}
            names = {
                identity.id: identity.name
                for identity in projects.list_identities(project, user_id=who.user_id)
            }
            for session_id in [*record.retired_main_session_ids, record.main_session_id]:
                if session_id:
                    names[session_id] = LEAD_NAME
            return names

        metadata = await asyncio.to_thread(identities)
        # Seen state belongs to the person viewing the board, not to agents reading tickets.
        unseen = (
            await asyncio.to_thread(
                store.unseen_activity,
                project_id,
                [ticket["id"] for ticket in tickets],
                user_id=who.user_id,
            )
            if who.session_id is None
            else {}
        )
        for ticket in tickets:
            ticket.update(unseen.get(ticket["id"], {}))
            linked = links[ticket["id"]]
            ticket["has_running_agents"] = _has_running_agents(
                linked.session_ids, sessions, has_more=linked.has_more
            )
            distinct = dict.fromkeys(
                metadata[sid] for sid in linked.session_ids if sid in metadata
            )
            ticket["agents"] = [
                {"agent_name": name, "harness": harness, "persona": persona}
                for name, harness, persona in distinct
            ]
            ticket["agents_has_more"] = linked.has_more
            if board is not None:
                workers = [
                    WorkerState(
                        status=_session_status_cache.get(sid, sessions[sid].live_status),
                        updated_at=int(sessions[sid].updated_at or 0),
                        name=(metadata[sid][2] or metadata[sid][0]) if sid in metadata else "",
                        running_since=_session_running_since.get(sid),
                    )
                    for sid in linked.session_ids
                    if sid not in board.router_session_ids
                    and sid in sessions
                    and not sessions[sid].archived
                ]
                known = not linked.has_more and all(
                    sid in board.router_session_ids
                    or (sid in sessions and sessions[sid].archived is not None)
                    for sid in linked.session_ids
                )
                ticket["wait"] = (
                    ticket_wait(ticket, board.project)
                    if in_working_stage(board.stages, ticket["stage_id"])
                    else None
                )
                ticket["stall"] = (
                    None
                    if ticket["wait"] is not None
                    else ticket_stall(
                        ticket,
                        card_stall_after(board.stages, ticket["stage_id"], board.settings),
                        workers,
                        workers_known=known,
                    )
                )
                ticket["working"] = ticket_working(workers)

    def add_ticket_times(project_id: str, page: dict[str, Any], user_id: str | None) -> None:
        """Set each row's ``completed_at`` and ``landed_at`` from one history read."""
        rows = page["data"]
        if not rows:
            return
        histories = store.stage_histories(
            project_id, user_id=user_id, ticket_ids=[row["id"] for row in rows]
        )
        record = projects.get(project_id, user_id=user_id) if projects is not None else None
        times = ticket_times(histories, page["stages"], (record.config if record else None) or {})
        for row in rows:
            row.update(times.get(row["id"], {"completed_at": None, "landed_at": None}))

    async def board_stall(
        request: Request, page: dict[str, Any], who: TicketActor
    ) -> BoardStall | None:
        """The project's stall settings and router sessions, for a page of board cards."""
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        if notifications is None or not page["data"]:
            return None
        project = await asyncio.to_thread(
            notifications.projects.get, page["data"][0]["project_id"], user_id=who.user_id
        )
        if project is None:
            return None
        return BoardStall(
            stages=page.get("stages") or [],
            settings=reliability_settings(project.config),
            router_session_ids=frozenset(await router_session_ids(request.app, project)),
            project=project,
        )

    @router.get("")
    async def list_tickets(
        request: Request,
        project_id: str,
        stage_id: TicketStageId | None = None,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        include_guidance: bool = False,
        mine: bool = Query(False, description="Only tickets the caller requested."),
        completed_since: int | None = Query(
            None,
            ge=0,
            description=(
                "Only tickets now in a completed stage that last entered it at or after this "
                "Unix time, e.g. what finished in the last 24 hours."
            ),
        ),
    ) -> dict[str, Any]:
        """List a page of tickets.

        Each ticket's ``latest_comment`` is its newest comment by a person or for the
        human (``text``, ``user_id``, ``session_id``, ``by_person``, ``position``,
        ``created_at``), or null. Unlike ``latest_update``, stage moves and label edits
        don't replace it. ``completed_at`` and ``landed_at`` come from its stage history:
        its latest move into a completed stage, and into the landed stage or later, while
        it's still there; else null.
        """
        who = await actor(request, project_id)
        finished = (
            None
            if completed_since is None
            else await asyncio.to_thread(
                store.completed_since, project_id, completed_since, user_id=who.user_id
            )
        )
        page = await asyncio.to_thread(
            store.list,
            project_id,
            user_id=who.user_id,
            stage_id=stage_id,
            after=after,
            limit=limit,
            requested_by=who.user_id if mine else None,
            ticket_ids=finished,
        )
        await asyncio.to_thread(add_ticket_times, project_id, page, who.user_id)
        # Project sessions already carry this guidance in their instructions.
        if include_guidance and who.session_id is not None:
            page["agent_guidance"] = PROJECT_TICKET_WORK_INSTRUCTION
        # Agents read the list as data and never draw cards, so they skip the stall lookup.
        board = await board_stall(request, page, who) if who.session_id is None else None
        await enrich(project_id, page["data"], who, board=board)
        return page

    async def text_mentions(
        targets: PersonaMentionTargets, project_id: str, who: TicketActor, text: str
    ) -> tuple[Any, list[Mention], list[dict[str, str]]]:
        """The personas *text* @mentions and the people it cc's, with the project."""
        project = await targets.project(project_id, who.user_id)
        personas = await targets.personas(project)
        # Only people cc people; agents ask a person with needs-human instead.
        people = (
            await targets.people(project)
            if targets.people is not None and who.session_id is None and who.agent_id is None
            else []
        )
        candidates = [
            *personas,
            *person_aliases([p for p in people if p != who.user_id], personas),
        ]
        mentions, cc = split_mentions(find_mentions(text, candidates))
        return project, mentions, cc

    @router.post("", status_code=201)
    async def create_ticket(
        request: Request, project_id: str, body: CreateTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id, "Creating tickets")
        await refuse_owner_label_change(
            request,
            project_id,
            [],
            [label.model_dump() for label in body.labels],
            who,
            who.user_id,
        )
        targets: PersonaMentionTargets | None = getattr(
            request.app.state, "persona_mention_targets", None
        )
        project = None
        mentions: list[Mention] = []
        cc: list[dict[str, str]] = []
        if targets is not None and "@" in body.description:
            project, mentions, cc = await text_mentions(targets, project_id, who, body.description)
        with _storage_errors():
            ticket = await asyncio.to_thread(
                store.create,
                project_id,
                body,
                who,
                mentions=[{"id": entry.id, "name": entry.name} for entry in mentions],
                cc=cc,
            )
        await record_usage_event(
            request.app,
            "ticket.filed",
            project_id=project_id,
            user_id=None if who.session_id else (who.user_id or RESERVED_USER_LOCAL),
            session_id=who.session_id,
            surface="agent" if who.session_id else resolve_client_surface(request.headers),
            subject_id=ticket["id"],
        )
        wake(request)
        if targets is not None and project is not None and mentions:
            _spawn_mention_delivery(
                request, targets, project, ticket, body.description, mentions, who
            )
        return ticket

    @router.post("/intake", status_code=201)
    async def file_intake_ticket(
        request: Request, project_id: str, body: FileIntakeTicket
    ) -> dict[str, Any]:
        """File a ticket on this board from an agent session in another project.

        For a project shared with the person whose session calls: owner or member.
        The ticket starts in the first stage with no labels, ``requested_by`` is
        ``project:<source project id>`` so this project's own gates apply, and the
        response returns only the new ticket's ID, title and stage. Each source project
        may file :data:`INTAKE_TICKETS_PER_HOUR` per hour on one board.
        """
        user_id = require_user(request, auth_provider)
        raw_source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if raw_source is None:
            raise OmnigentError(
                "Intake tickets come from an agent session in another project; "
                "people file tickets on the board",
                code=ErrorCode.FORBIDDEN,
            )
        await asyncio.to_thread(
            refuse_role_side_chat,
            request,
            action="Filing tickets in another project",
            user_id=user_id,
            conversations=conversations,
            projects=projects,
        )
        await asyncio.to_thread(
            require_named_source_private, request, user_id, conversations, permissions
        )
        try:
            source_id = uuid_to_bytes(raw_source).hex()
            target_id = uuid_to_bytes(project_id).hex()
        except InvalidUuidError:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND) from None
        source = await asyncio.to_thread(conversations.get_conversation, source_id)
        if source is None or source.archived or not source.project_id:
            raise OmnigentError(
                "File this session into a project before filing tickets elsewhere",
                code=ErrorCode.FORBIDDEN,
            )
        if source.project_id == target_id:
            raise OmnigentError(
                "This is your own project's board; use sys_ticket_create",
                code=ErrorCode.INVALID_INPUT,
            )
        source_project = (
            await asyncio.to_thread(projects.get, source.project_id, user_id=user_id)
            if projects is not None
            else None
        )
        if source_project is None:
            raise OmnigentError(
                "The calling session's project is not available", code=ErrorCode.FORBIDDEN
            )
        requested_by = INTAKE_REQUESTER_PREFIX + source_project.id
        since = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
        with _storage_errors():
            recent = await asyncio.to_thread(
                store.count_requested_since, target_id, requested_by, since, user_id=user_id
            )
        if recent >= INTAKE_TICKETS_PER_HOUR:
            raise OmnigentError(
                f"{source_project.name} already filed {recent} tickets on this board in the "
                f"last hour (limit {INTAKE_TICKETS_PER_HOUR}); try again later",
                code=ErrorCode.QUEUE_FULL,
            )
        create = CreateTicket(
            title=body.title,
            description=intake_description(source_project.name, user_id, body.description),
        )
        # A person-shaped actor: the sender's session gets no reply channel into this board.
        with _storage_errors():
            ticket = await asyncio.to_thread(
                store.create,
                target_id,
                create,
                TicketActor(user_id),
                requested_by=requested_by,
                source={
                    "project_id": source_project.id,
                    "project_name": source_project.name,
                    "session_id": source.id,
                },
            )
        wake(request)
        return {
            "object": "project.intake_ticket",
            "id": ticket["id"],
            "project_id": target_id,
            "title": ticket["title"],
            "stage_id": ticket["stage_id"],
        }

    @router.post("/import-pr", status_code=201)
    async def import_pull_request(
        request: Request, project_id: str, body: ImportPullRequest
    ) -> dict[str, Any]:
        """File an outside GitHub PR as a ticket, read-only; re-importing returns the same ticket.

        The ticket's requester is ``github:<login>``, which puts it in the outside-author
        landing tier. A recorded ``record`` replaces the GitHub read for dry runs.
        """
        who = await actor(request, project_id, "Importing pull requests")
        owner, repo, number = github_pr_intake.parse_pr_url(body.url)
        pr_key = f"{owner}/{repo}#{number}"
        after: str | None = None
        for _ in range(50):
            page = await asyncio.to_thread(
                store.list, project_id, user_id=who.user_id, after=after, limit=100
            )
            for row in page.get("data", []):
                if any(
                    label.get("key") == github_pr_intake.PR_LABEL and label.get("value") == pr_key
                    for label in row.get("labels") or []
                ):
                    return {**row, "imported": False}
            if not page.get("has_more"):
                break
            after = page.get("last_id")
        record = body.record or await asyncio.to_thread(
            github_pr_intake.fetch_pr, owner, repo, number
        )
        fields = github_pr_intake.ticket_from_pr(body.url, record)
        create = CreateTicket(
            title=fields["title"],
            description=fields["description"],
            labels=[TicketLabel(**label) for label in fields["labels"]],
        )
        with _storage_errors():
            ticket = await asyncio.to_thread(
                store.create, project_id, create, who, requested_by=fields["requested_by"]
            )
            await asyncio.to_thread(
                store.comment,
                project_id,
                ticket["id"],
                CommentOnTicket(
                    text=f"Imported {pr_key} by `{fields['requested_by']}` (outside-author tier).",
                    outputs=[{"type": "pull_request", "url": fields["url"]}],
                    audience="agents",
                ),
                who,
            )
        wake(request)
        return {**ticket, "imported": True}

    @router.get("/stages")
    async def ticket_stages(
        request: Request, project_id: str, include_usage: bool = False
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.stages, project_id, user_id=who.user_id, include_usage=include_usage
        )

    @router.put("/stages")
    async def update_ticket_stages(
        request: Request, project_id: str, body: UpdateTicketStages
    ) -> dict[str, Any]:
        who = await actor(request, project_id, "Changing the board's stages")
        with _storage_errors():
            configuration = await asyncio.to_thread(store.update_stages, project_id, body, who)
        wake(request)
        return configuration

    @router.get("/search")
    async def search_tickets(
        request: Request,
        project_id: str,
        q: str = Query(..., min_length=1, max_length=200, description="Words to find."),
        after: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Find the project's tickets holding every word of *q*, most recently updated first.

        Matches titles, descriptions, label keys and values, ticket ID prefixes, and each
        ticket's newest comments, case-insensitively. A page reads a bounded number of
        tickets, so it can hold fewer than *limit* matches, or none, with ``has_more``
        still true: pass ``last_id`` as *after* to keep searching older tickets.
        """
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.search, project_id, q, user_id=who.user_id, after=after, limit=limit
        )

    @router.get("/summary")
    async def ticket_summary(
        request: Request,
        project_id: str,
        created_after: int | None = Query(None, ge=0),
        responses: bool = Query(True, description="False omits the human-request counts."),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.summary,
            project_id,
            user_id=who.user_id,
            created_after=created_after,
            include_responses=responses,
        )

    @router.get("/cost")
    async def project_cost(
        request: Request, project_id: str, days: int = Query(7, ge=1, le=366)
    ) -> dict[str, Any]:
        """Spend by the project's sessions and sub-agents over the trailing *days* UTC days."""
        who = await actor(request, project_id)
        # Reading the stages checks the caller can see this project.
        await asyncio.to_thread(store.stages, project_id, user_id=who.user_id)
        since = trailing_window_start(days)
        today = trailing_window_start(1)
        # The status summary's spend definition, so sub-agents count for their root's project.
        cents = await asyncio.to_thread(
            project_total_cents, conversations, project_id, since, today
        )
        return {"cost_usd": cents / 100, "days": days, "since_day_utc": since}

    @router.get("/push-prefix")
    async def push_prefix(request: Request, project_id: str) -> dict[str, Any]:
        """The longest pushable prefix of the landing branch's unpushed commits, by ticket."""
        who = await actor(request, project_id)
        project = await load_project(request, project_id, who)
        with _storage_errors():
            return await project_release.project_push_prefix(project, store, who.user_id)

    @router.get("/{ticket_id}")
    async def get_ticket(
        request: Request, project_id: str, ticket_id: str, include_guidance: bool = False
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        ticket = await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        if include_guidance and who.session_id is not None:
            ticket["agent_guidance"] = PROJECT_TICKET_WORK_INSTRUCTION
        # The only people whose `product-ok` counts, so agents ask them and nobody else.
        with contextlib.suppress(OmnigentError):
            project = await load_project(request, project_id, who)
            ticket["product_approvers"] = product_approval.approver_list(project)
            # One-click approvals: the sign-offs this person may give the open ask.
            if who.session_id is None:
                ticket["sign_offs"] = product_approval.sign_offs_for(project, ticket, who.user_id)
            # Whether the landed commit is in the code this server runs; absent if unknown.
            shipped = await asyncio.to_thread(running_commit.ship_status, project.config, ticket)
            if shipped is not None:
                ticket["ship_status"] = shipped
        await enrich(project_id, [ticket], who)
        return ticket

    @router.patch("/{ticket_id}")
    async def update_ticket(
        request: Request, project_id: str, ticket_id: str, body: UpdateTicket
    ) -> dict[str, Any]:
        changed = {
            name for name in body.model_fields_set if getattr(body, name) not in (None, False)
        }
        comment_only = changed <= _TICKET_COMMENT_FIELDS
        who = await actor(request, project_id, None if comment_only else "Editing tickets")
        before = await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        context = None
        # The same permission checks for a full list or for adds/removes, on what they change.
        try:
            proposed = body.labels_after(before.get("labels"))
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        if proposed is not None:
            await refuse_owner_label_change(
                request,
                project_id,
                before.get("labels"),
                proposed,
                who,
                before.get("requested_by"),
            )
        if proposed is not None:
            approved_after = label_value(proposed, APPROVED_LABEL)
            approved_before = label_value(before.get("labels"), APPROVED_LABEL)
            if approved_after is not None and approved_after != approved_before:
                context = await review_context(request, project_id, who)
                check_approval_change(
                    approved_before,
                    approved_after,
                    author_session_id=who.session_id,
                    author_is_reviewer=context is not None and context[3],
                )
        with _storage_errors():
            ticket = await asyncio.to_thread(store.update, project_id, ticket_id, body, who)
        ticket = await after_review_activity(
            request,
            project_id,
            ticket,
            body.comment,
            who,
            entered_review=body.stage_id == REVIEW_STAGE_ID
            and before.get("stage_id") != REVIEW_STAGE_ID,
            context=context,
        )
        # A needs-human ask reaches its responders as a ticket request in their Inbox
        # (GET /v1/inbox/ticket-requests), not through the asking session.
        if who.session_id is not None and body.stage_id is not None:
            current = await asyncio.to_thread(
                store.get, project_id, ticket_id, user_id=who.user_id
            )
            if current["needs_final_update"]:
                ticket["next_step"] = FINAL_UPDATE_REMINDER
        wake(request)
        return ticket

    @router.put("/{ticket_id}/read-state")
    async def mark_seen(
        request: Request, project_id: str, ticket_id: str, body: MarkTicketSeen
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.mark_seen, project_id, ticket_id, body, user_id=who.user_id
        )

    @router.post("/{ticket_id}/land", include_in_schema=False)
    async def land(
        request: Request, project_id: str, ticket_id: str, body: LandTicketRequest
    ) -> dict[str, Any]:
        """Queue a Reviewer-approved commit for the landing queue; refusals are 409s."""
        who = await actor(request, project_id, "Landing")
        queue = getattr(request.app.state, "project_landing", None)
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        if queue is None or notifications is None:
            raise OmnigentError(
                "The landing queue is unavailable", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        project = await asyncio.to_thread(
            notifications.projects.get, uuid_to_bytes(project_id).hex(), user_id=who.user_id
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        with _storage_errors():
            return await queue.submit(project, ticket_id, body.sha, who, dry_run=body.dry_run)

    @router.post("/{ticket_id}/revert-draft", include_in_schema=False)
    async def revert_draft(
        request: Request, project_id: str, ticket_id: str, body: RevertDraftRequest
    ) -> dict[str, Any]:
        """Draft revert commits for a landed ticket on a new branch; never moves a branch."""
        who = await actor(request, project_id, "Drafting a revert")
        project = await load_project(request, project_id, who)
        found = await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        ticket = found.get("ticket", found)
        with _storage_errors():
            return await project_release.project_revert_draft(
                project, store, who.user_id, ticket["id"], body.shas
            )

    def summarizer(request: Request) -> TicketSummarizer:
        state = request.app.state
        existing = getattr(state, "ticket_summarizer", None)
        if existing is None:
            existing = build_ticket_summarizer()
            state.ticket_summarizer = existing
        return existing

    def refuse_agent_summary(request: Request) -> None:
        # Summaries spend the operator's model budget; agents read the ticket itself.
        if ORCHESTRATION_SOURCE_HEADER in request.headers or (
            RUNNER_TUNNEL_TOKEN_HEADER in request.headers
        ):
            raise OmnigentError(
                "Ticket summaries are for people; agents read the ticket directly",
                code=ErrorCode.FORBIDDEN,
            )

    def all_events(project_id: str, ticket_id: str, user_id: str | None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        after = -1
        while True:
            page = store.events(
                project_id, ticket_id, user_id=user_id, after=after, limit=TICKET_PAGE_MAX
            )
            events.extend(page["data"])
            if not page.get("has_more") or not page["data"]:
                return events
            after = int(page["data"][-1]["position"])

    def role_names(project_id: str, user_id: str | None) -> dict[str, str]:
        """Session ID -> role name, so the summary says who did what."""
        if projects is None:
            return {}
        project = projects.get(project_id, user_id=user_id)
        if project is None:
            return {}
        names = {
            identity.active_session_id: identity.name
            for identity in projects.list_identities(project_id, user_id=user_id)
            if identity.active_session_id
        }
        for session_id in (project.main_session_id, *project.retired_main_session_ids):
            if session_id:
                names[session_id] = LEAD_NAME
        return names

    @router.get("/{ticket_id}/summary")
    async def get_ticket_summary(
        request: Request, project_id: str, ticket_id: str
    ) -> dict[str, Any]:
        """The last summary of the ticket's history, if one is cached; never calls a model.

        ``stale`` is true when activity followed it. ``available`` says whether
        this server can summarize at all.
        """
        refuse_agent_summary(request)
        who = await actor(request, project_id)
        ticket = await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        engine = summarizer(request)
        latest = engine.latest(ticket["id"])
        stale = False
        if latest is not None:
            newer = await asyncio.to_thread(
                store.events,
                project_id,
                ticket["id"],
                user_id=who.user_id,
                after=latest.through_position,
                limit=1,
            )
            stale = bool(newer["data"])
        return {
            "object": "project.ticket.summary",
            "available": engine.available,
            "summary": latest.as_dict(cached=True) if latest is not None else None,
            "stale": stale,
        }

    @router.post("/{ticket_id}/summary")
    async def summarize_ticket(
        request: Request, project_id: str, ticket_id: str
    ) -> dict[str, Any]:
        """Summarize the ticket's history for the caller, reusing the cached one when unchanged.

        Shown only to the person who asked; nothing is posted to the ticket.
        """
        refuse_agent_summary(request)
        who = await actor(request, project_id)
        ticket = await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        events = await asyncio.to_thread(all_events, project_id, ticket["id"], who.user_id)
        names = await asyncio.to_thread(role_names, project_id, who.user_id)
        summary, cached = await summarizer(request).summarize(
            ticket["id"], events, names, user_key=who.user_id or "local"
        )
        return {
            "object": "project.ticket.summary",
            "available": True,
            "summary": summary.as_dict(cached=cached),
            "stale": False,
        }

    @router.post("/{ticket_id}/comments", status_code=201)
    async def comment(
        request: Request, project_id: str, ticket_id: str, body: CommentOnTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        targets: PersonaMentionTargets | None = getattr(
            request.app.state, "persona_mention_targets", None
        )
        project = mentions = None
        cc: list[dict[str, str]] = []
        if targets is not None and body.kind == "comment" and "@" in body.text:
            project, mentions, cc = await text_mentions(targets, project_id, who, body.text)
        with _storage_errors():
            ticket = await asyncio.to_thread(
                store.comment,
                project_id,
                ticket_id,
                body,
                who,
                mentions=[{"id": entry.id, "name": entry.name} for entry in mentions or []],
                cc=cc,
            )
        wake(request)
        if targets is not None and project is not None and mentions:
            _spawn_mention_delivery(request, targets, project, ticket, body.text, mentions, who)
        if body.kind == "comment":
            ticket = await after_review_activity(
                request, project_id, ticket, body.text, who, entered_review=False, context=None
            )
        return ticket

    @router.get("/{ticket_id}/events")
    async def events(
        request: Request,
        project_id: str,
        ticket_id: str,
        after: int = Query(-1, ge=-1),
        limit: int = Query(50, ge=1, le=100),
        kind: TicketEventKind | None = None,
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.events,
            project_id,
            ticket_id,
            user_id=who.user_id,
            after=after,
            limit=limit,
            kind=kind,
        )

    @router.get("/{ticket_id}/pull-requests")
    async def pull_requests(request: Request, project_id: str, ticket_id: str) -> dict[str, Any]:
        """Live GitHub state and CI of the ticket's pull request outputs, read-only.

        A PR GitHub won't show is left out, so the client keeps a plain link.
        """
        who = await actor(request, project_id)
        urls: list[str] = []
        after = -1
        while True:
            page = await asyncio.to_thread(
                store.events,
                project_id,
                ticket_id,
                user_id=who.user_id,
                after=after,
                limit=100,
                kind="output",
            )
            for event in page["data"]:
                payload = event.get("payload") or {}
                url = payload.get("url")
                if payload.get("type") == "pull_request" and isinstance(url, str):
                    if url not in urls:
                        urls.append(url)
            if not page.get("has_more") or len(urls) >= _PULL_REQUEST_STATUS_MAX:
                break
            after = page["last_position"]
        statuses = await asyncio.gather(
            *(
                asyncio.to_thread(github_pr_status.pull_request_status, url)
                for url in urls[:_PULL_REQUEST_STATUS_MAX]
            )
        )
        return {"data": [status for status in statuses if status is not None]}

    @router.get("/{ticket_id}/sessions")
    async def sessions(
        request: Request,
        project_id: str,
        ticket_id: str,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.sessions, project_id, ticket_id, user_id=who.user_id, after=after, limit=limit
        )

    @router.get("/{ticket_id}/cost")
    async def ticket_cost(request: Request, project_id: str, ticket_id: str) -> dict[str, Any]:
        """All-time spend of the ticket's linked sessions, each including its sub-agents."""
        who = await actor(request, project_id)
        linked: list[str] = []
        after: str | None = None
        while True:
            page = await asyncio.to_thread(
                store.sessions,
                project_id,
                ticket_id,
                user_id=who.user_id,
                after=after,
                limit=100,
            )
            linked.extend(row["session_id"] for row in page["data"])
            if not page["has_more"]:
                break
            after = page["last_id"]
        visible = await visible_sessions(who, linked)
        return await asyncio.to_thread(linked_sessions_cost, visible, conversations)

    @router.post("/{ticket_id}/sessions")
    async def link_session(
        request: Request, project_id: str, ticket_id: str, body: LinkTicketSession
    ) -> dict[str, Any]:
        who = await actor(request, project_id, "Linking sessions to tickets")
        await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        session_id = await check_session(who, project_id, body.session_id)
        with _storage_errors():
            result = await asyncio.to_thread(
                store.link_session,
                project_id,
                ticket_id,
                session_id,
                body.action,
                who,
                involvement=body.involvement,
            )
        wake(request)
        return result

    @router.get("/{ticket_id}/related")
    async def related_tickets(
        request: Request,
        project_id: str,
        ticket_id: str,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.related, project_id, ticket_id, user_id=who.user_id, after=after, limit=limit
        )

    @router.post("/{ticket_id}/related")
    async def link_related_ticket(
        request: Request,
        project_id: str,
        ticket_id: str,
        body: LinkRelatedTicket,
    ) -> dict[str, Any]:
        who = await actor(request, project_id, "Linking tickets")
        with _storage_errors():
            result = await asyncio.to_thread(
                store.link_ticket, project_id, ticket_id, body.related_ticket_id, body.action, who
            )
        wake(request)
        return result

    return router
