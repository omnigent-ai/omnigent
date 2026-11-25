"""PRs merged per project and member, for the project status summary.

Two sources, deduplicated by canonical PR URL:

- **Session PRs:** pull requests linked to the project's sessions (the session
  Pull Requests panel), once a host fetch has found them merged. The merge time is
  that first fetch, so it can trail the real merge by the host's polling interval.
- **Merged tickets:** tickets currently in a project's completed stage when that
  stage is the Merged stage (id or name ``merged``). A ticket's ``pull_request``
  outputs count as its PRs; a ticket without one counts once as itself. The merge
  time is its latest move into that stage. In a project whose completed stage is
  something else (e.g. "Done"), only tickets with PR outputs count.

**Attribution** (via :func:`project_status.member_key_for_session`, on the root
session): a PR in a session's panel goes to the session that ``created`` it, else
the strongest relationship (worked on, attached, inferred), earliest seen first.
A ticket's PR that no session panel lists goes to the ticket's earliest-linked
session that isn't the Lead. Anything else counts in the total but no member.

**Limits:** PRs merged outside Omnigent, or never linked to a project session or
ticket, aren't counted. PRs stored as merged before merge times were recorded
have no merge time and count only in the ``"all"`` window. A PR attached to a
session after it merged is dated by its first fetch. A sub-agent session counts
only when its own metadata names the project.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlProjectTicket,
    SqlProjectTicketEvent,
    SqlProjectTicketSession,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.project_status import (
    LEAD_MEMBER_KEY,
    UNAVAILABLE,
    MemberKey,
    MemberTotals,
    StatusPeriod,
    member_key_for_session,
)
from omnigent.server.session_pull_requests import open_store
from omnigent.stores.project_ticket_store import EVENT_CODES
from omnigent.stores.session_pull_request_store import (
    _RELATIONSHIP_RANK,  # A stronger claim on the same PR wins attribution.
    StoredPullRequest,
)
from omnigent.util.uuid import uuid_to_bytes

if TYPE_CHECKING:
    from omnigent.entities import Project
    from omnigent.stores import ConversationStore, PermissionStore
    from omnigent.stores.project_ticket_store import ProjectTicketStore
    from omnigent.stores.session_pull_request_store import SessionPullRequestStore

_logger = logging.getLogger(__name__)

MERGED_STAGE = "merged"
_PR_PATH = re.compile(
    r"^/(?P<repo>[^/]+/[^/]+(?:/[^/]+)*?)/(?:pull|pulls|merge_requests|-/merge_requests)/(?P<number>\d+)"
)


def canonical_pr_url(url: str) -> str:
    """One key per PR: lowercase scheme and host, no query, fragment or sub-page."""
    match = re.match(
        r"^(?P<scheme>https?)://(?P<host>[^/?#]+)(?P<path>[^?#]*)", url.strip(), re.IGNORECASE
    )
    if match is None:
        return url.strip()
    path = match["path"]
    pr = _PR_PATH.match(path)
    if pr is not None:
        path = pr.group(0).removesuffix("/")
    return f"{match['scheme'].lower()}://{match['host'].lower()}{path.rstrip('/')}"


@dataclass(frozen=True)
class SessionMergedPr:
    """A merged PR in one session's panel."""

    session_id: str
    url: str
    relationship: str
    first_seen_at: float
    merged_at: float | None


@dataclass(frozen=True)
class MergedTicket:
    """A ticket in the Merged stage (or with PR outputs in a completed stage)."""

    ticket_id: str
    merged_at: int
    pr_urls: tuple[str, ...]
    linked_session_ids: tuple[str, ...]
    counts_without_pr: bool


@dataclass(frozen=True)
class MergedItem:
    """One counted merge: a PR by canonical URL, or ``ticket:<id>`` for a PR-less ticket."""

    key: str
    merged_at: float | None
    member: MemberKey | None


def _session_claim(pr: SessionMergedPr) -> tuple[int, float, str]:
    return (-_RELATIONSHIP_RANK.get(pr.relationship, 1), pr.first_seen_at, pr.session_id)


def combine(
    session_prs: Iterable[SessionMergedPr],
    tickets: Iterable[MergedTicket],
    member_of_session: Callable[[str], MemberKey | None],
) -> list[MergedItem]:
    """Deduplicate both sources into one item per merge, each with its member.

    :param member_of_session: A session's member key, or ``None`` when it can't be resolved.
    """
    claims: dict[str, list[SessionMergedPr]] = {}
    for pr in session_prs:
        claims.setdefault(canonical_pr_url(pr.url), []).append(pr)
    items: dict[str, MergedItem] = {}
    for key, prs in claims.items():
        best = min(prs, key=_session_claim)
        dated = [pr.merged_at for pr in prs if pr.merged_at is not None]
        items[key] = MergedItem(
            key, min(dated) if dated else None, member_of_session(best.session_id)
        )
    for ticket in tickets:
        worker = next(
            (
                member
                for session_id in ticket.linked_session_ids
                if (member := member_of_session(session_id)) not in (None, LEAD_MEMBER_KEY)
            ),
            None,
        )
        keys = [canonical_pr_url(url) for url in ticket.pr_urls]
        if not keys and ticket.counts_without_pr:
            keys = [f"ticket:{ticket.ticket_id}"]
        for key in keys:
            known = items.get(key)
            if known is None:
                items[key] = MergedItem(key, ticket.merged_at, worker)
            elif known.merged_at is None:
                # A session saw it merged but didn't date it; the ticket's move does.
                items[key] = MergedItem(key, ticket.merged_at, known.member or worker)
    return list(items.values())


def totals(items: Iterable[MergedItem], period: StatusPeriod) -> MemberTotals:
    """Count the items merged in *period*; undated ones count only with no lower bound."""
    total = 0
    by_member: dict[MemberKey, int] = {}
    for item in items:
        if item.merged_at is None:
            if period.since_epoch is not None:
                continue
        elif not period.contains(int(item.merged_at)):
            continue
        total += 1
        if item.member is not None:
            by_member[item.member] = by_member.get(item.member, 0) + 1
    return MemberTotals(total=total, by_member=by_member)


def _hex(value: str) -> str:
    return uuid_to_bytes(value).hex()


def _is_merged(pr: StoredPullRequest) -> bool:
    return pr.merged_at is not None or pr.state == "MERGED"


class ProjectPrsMergedSource:
    """Reads both sources for one project; see the module docstring for the rules."""

    def __init__(
        self,
        tickets: ProjectTicketStore,
        ticket_location: str,
        conversations: ConversationStore,
        pull_requests: SessionPullRequestStore,
        permissions: PermissionStore | None,
    ) -> None:
        self._tickets = tickets
        self._conversations = conversations
        self._pull_requests = pull_requests
        self._permissions = permissions
        self._ticket_db = make_named_managed_session_maker(
            get_or_create_engine(ticket_location), query_name_prefix="omnigent.project_status_prs"
        )
        self._session_db = make_named_managed_session_maker(
            get_or_create_engine(pull_requests.storage_location),
            query_name_prefix="omnigent.project_status_prs",
        )

    def prs_merged(self, project: Project, period: StatusPeriod) -> MemberTotals:
        return totals(self.merged_items(project, period.since_epoch), period)

    def merged_items(self, project: Project, since_epoch: int | None = None) -> list[MergedItem]:
        """Every counted merge with its date and member, for per-day series.

        :param since_epoch: Skip Merged-stage tickets not moved since then; they can't
            count in a range starting there. ``None`` reads every ticket.
        """
        session_prs = self._session_prs(project)
        if any(pr.merged_at is None for pr in session_prs):
            # An undated session PR takes its date from its ticket's move, however old.
            since_epoch = None
        return combine(
            session_prs,
            self._merged_tickets(project, since_epoch),
            self._member_resolver(project),
        )

    def _session_prs(self, project: Project) -> list[SessionMergedPr]:
        with self._session_db("list_project_session_ids_for_status") as session:
            session_ids = list(
                session.scalars(
                    select(SqlConversationMetadata.id).where(
                        SqlConversationMetadata.workspace_id == current_workspace_id(),
                        SqlConversationMetadata.project_id == _hex(project.id),
                    )
                ).all()
            )
        merged = []
        for session_id, linked in self._pull_requests.read_many(session_ids).items():
            for pr in linked.entries:
                if _is_merged(pr):
                    merged.append(
                        SessionMergedPr(
                            session_id, pr.url, pr.relationship, pr.first_seen_at, pr.merged_at
                        )
                    )
        return merged

    def _merged_tickets(
        self, project: Project, since_epoch: int | None = None
    ) -> list[MergedTicket]:
        stages = self._tickets.stages(project.id, user_id=project.user_id)["stages"]
        completed = next((stage for stage in reversed(stages) if stage.get("is_complete")), None)
        if completed is None:
            return []
        is_merged_stage = MERGED_STAGE in (
            str(completed["id"]).lower(),
            str(completed["name"]).lower(),
        )
        project_hex = _hex(project.id)
        with self._ticket_db("list_merged_tickets_for_status") as session:
            ticket_ids = list(
                session.scalars(
                    select(SqlProjectTicket.id).where(
                        SqlProjectTicket.workspace_id == current_workspace_id(),
                        SqlProjectTicket.project_id == project_hex,
                        SqlProjectTicket.stage_id == completed["id"],
                    )
                ).all()
            )
            if ticket_ids and since_epoch is not None:
                # A ticket's latest move into the stage is an update; none since means
                # it was moved there before the range.
                ticket_ids = list(
                    session.scalars(
                        select(SqlProjectTicketEvent.ticket_id)
                        .where(
                            SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                            SqlProjectTicketEvent.project_id == project_hex,
                            SqlProjectTicketEvent.ticket_id.in_(ticket_ids),
                            SqlProjectTicketEvent.kind == EVENT_CODES["updated"],
                            SqlProjectTicketEvent.created_at >= since_epoch,
                        )
                        .distinct()
                    ).all()
                )
            if not ticket_ids:
                return []
            events = session.scalars(
                select(SqlProjectTicketEvent)
                .where(
                    SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                    SqlProjectTicketEvent.project_id == project_hex,
                    SqlProjectTicketEvent.ticket_id.in_(ticket_ids),
                    SqlProjectTicketEvent.kind.in_(
                        (EVENT_CODES["updated"], EVENT_CODES["output"])
                    ),
                )
                .order_by(SqlProjectTicketEvent.created_at)
            ).all()
            links = session.execute(
                select(SqlProjectTicketSession.ticket_id, SqlProjectTicketSession.session_id)
                .where(
                    SqlProjectTicketSession.workspace_id == current_workspace_id(),
                    SqlProjectTicketSession.project_id == project_hex,
                    SqlProjectTicketSession.ticket_id.in_(ticket_ids),
                )
                .order_by(SqlProjectTicketSession.created_at, SqlProjectTicketSession.session_id)
            ).all()
        moved_at: dict[str, int] = {}
        pr_urls: dict[str, list[str]] = {}
        for event in events:
            payload: dict[str, Any] = event.payload or {}
            if event.kind == EVENT_CODES["output"]:
                if payload.get("type") == "pull_request" and isinstance(payload.get("url"), str):
                    pr_urls.setdefault(event.ticket_id, []).append(payload["url"])
                continue
            changes = payload.get("changes")
            if isinstance(changes, dict) and changes.get("stage_id") == completed["id"]:
                moved_at[event.ticket_id] = event.created_at
        linked: dict[str, list[str]] = {}
        for ticket_id, session_id in links:
            linked.setdefault(ticket_id, []).append(session_id)
        return [
            MergedTicket(
                ticket_id,
                moved_at[ticket_id],
                tuple(pr_urls.get(ticket_id, ())),
                tuple(linked.get(ticket_id, ())),
                counts_without_pr=is_merged_stage,
            )
            for ticket_id in ticket_ids
            # A ticket stored in the stage without a recorded move (imported, legacy) is undated.
            if ticket_id in moved_at
        ]

    def _member_resolver(self, project: Project) -> Callable[[str], MemberKey | None]:
        cache: dict[str, MemberKey | None] = {}

        def owner_of(session_id: str) -> str | None:
            if self._permissions is None:
                return None
            grants = self._permissions.list_for_sessions([session_id]).get(session_id, [])
            return next((grant.user_id for grant in grants if grant.level >= LEVEL_OWNER), None)

        def resolve(session_id: str) -> MemberKey | None:
            if session_id in cache:
                return cache[session_id]
            session = self._conversations.get_conversation(session_id)
            if session is not None and session.root_conversation_id not in (None, session.id):
                session = (
                    self._conversations.get_conversation(session.root_conversation_id) or session
                )
            key = (
                None
                if session is None
                else member_key_for_session(project, session, owner_user_id=owner_of(session.id))
            )
            cache[session_id] = key
            return key

        return resolve


class _Unavailable:
    def prs_merged(self, project: Project, period: StatusPeriod) -> MemberTotals:
        del project, period
        return UNAVAILABLE


def build_source(app_state: Any) -> ProjectPrsMergedSource | _Unavailable:
    """The server's PRs-merged source, or one that reports unavailable without SQL stores."""
    notifications = getattr(app_state, "project_ticket_notifications", None)
    tickets = getattr(notifications, "tickets", None)
    projects = getattr(notifications, "projects", None)
    conversations = getattr(getattr(notifications, "inputs", None), "conversations", None)
    pull_requests = open_store(conversations) if conversations is not None else None
    location = getattr(projects, "storage_location", None)
    if tickets is None or conversations is None or pull_requests is None or not location:
        _logger.info("PRs-merged status source is unavailable: no SQL ticket or session store")
        return _Unavailable()
    return ProjectPrsMergedSource(
        tickets,
        location,
        conversations,
        pull_requests,
        getattr(app_state, "permission_store", None),
    )
