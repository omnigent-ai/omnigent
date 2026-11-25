"""PRs merged per project and member, for the project status summary.

Two sources, deduplicated by canonical PR URL:

- **Session PRs:** pull requests linked to the project's sessions (the session
  Pull Requests panel), once a host fetch has found them merged. The merge time is
  that first fetch, so it can trail the real merge by the host's polling interval.
- **Merged tickets:** tickets currently in a project's completed stage (e.g.
  "Merged" or "Done"). A ticket's ``pull_request`` outputs count as its PRs, dated
  by its latest move into that stage. A ticket without one counts nothing here:
  analysis and decision tickets finish without a PR.

**Attribution** (via :func:`project_status.member_key_for_session`, on the root
session): a PR in a session's panel goes to the session that ``created`` it, else
the strongest relationship (worked on, attached, inferred), earliest seen first.
A ticket's PR that no session panel lists goes to the ticket's earliest-linked
session that isn't the Lead. Anything else counts in the total but no member.

**Storage:** the deduplicated, attributed items are kept per project in
``project_merged_prs`` so a window's count is one indexed query. A project's rows
are recomputed with the rules above: on its first read, after any write that
clears its ``"prs"`` status input (in the background, coalesced
per project; a read of a project with such a write pending recomputes first), and
in the background once they are older than :data:`_STALE_AFTER_S`, which also
catches writes on another server process.

**Limits:** PRs merged outside Omnigent, or never linked to a project session or
ticket, aren't counted. PRs stored as merged before merge times were recorded
have no merge time and count only in the ``"all"`` window. A PR attached to a
session after it merged is dated by its first fetch. A sub-agent session counts
only when its own metadata names the project.
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, func, or_, select, update

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlProjectMergedPr,
    SqlProjectMergedPrRefresh,
    SqlProjectTicket,
    SqlProjectTicketEvent,
    SqlProjectTicketSession,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.project_status import (
    LEAD_MEMBER_KEY,
    UNAVAILABLE,
    MemberKey,
    MemberTotals,
    StatusPeriod,
    member_key_for_session,
)
from omnigent.server.project_status_cache import (
    add_invalidation_listener,
    invalidate_after_commit,
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

# Rows older than this are recomputed in the background on the next read.
_STALE_AFTER_S = 900
# A burst of writes to one project waits this long so it costs one recompute.
_REFRESH_COALESCE_S = 2.0
_PR_KEY_MAX_CHARS = 512
# Stored rows removed per DELETE, under SQLite's bound-parameter limit.
_DELETE_CHUNK = 500
# Rows stored for PR-less tickets before they stopped counting; the next refresh deletes them.
_COUNTED_ROW = ~SqlProjectMergedPr.pr_key.startswith("ticket:")
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
    """A ticket in the completed stage with its PR outputs."""

    ticket_id: str
    merged_at: int
    pr_urls: tuple[str, ...]
    linked_session_ids: tuple[str, ...]


@dataclass(frozen=True)
class MergedItem:
    """One counted merge: a PR by its canonical URL."""

    key: str
    merged_at: float | None
    member: MemberKey | None
    #: The tickets and sessions that claim this merge, for counting it per role.
    claimed_by: tuple[str, ...] = ()


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
            key,
            min(dated) if dated else None,
            member_of_session(best.session_id),
            tuple(sorted({pr.session_id for pr in prs})),
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
        for key in (canonical_pr_url(url) for url in ticket.pr_urls):
            known = items.get(key)
            if known is None:
                items[key] = MergedItem(key, ticket.merged_at, worker, (ticket.ticket_id,))
                continue
            claimed_by = tuple(sorted({*known.claimed_by, ticket.ticket_id}))
            if known.merged_at is None:
                # A session saw it merged but didn't date it; the ticket's move does.
                items[key] = MergedItem(key, ticket.merged_at, known.member or worker, claimed_by)
            else:
                items[key] = replace(known, claimed_by=claimed_by)
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
        projects: Any = None,
    ) -> None:
        self._tickets = tickets
        self._conversations = conversations
        self._pull_requests = pull_requests
        self._permissions = permissions
        self._ticket_db = make_named_managed_session_maker(
            get_or_create_engine(ticket_location), query_name_prefix="omnigent.project_status_prs"
        )
        self._ticket_write_db = make_named_managed_session_maker(
            get_or_create_engine(ticket_location),
            query_name_prefix="omnigent.project_status_prs",
            immediate=True,
        )
        self._session_db = make_named_managed_session_maker(
            get_or_create_engine(pull_requests.storage_location),
            query_name_prefix="omnigent.project_status_prs",
        )
        self.projects = projects

    def prs_merged(self, project: Project, period: StatusPeriod) -> MemberTotals:
        """The window's merges per member, counted from the project's stored rows."""
        self._ensure_fresh(project)
        in_window = SqlProjectMergedPr.merged_at <= period.until_epoch
        if period.since_epoch is not None:
            in_window = and_(in_window, SqlProjectMergedPr.merged_at >= period.since_epoch)
        else:
            in_window = or_(in_window, SqlProjectMergedPr.merged_at.is_(None))
        with self._ticket_db("count_project_merged_prs") as session:
            rows = session.execute(
                select(SqlProjectMergedPr.member_key, func.count())
                .where(
                    SqlProjectMergedPr.workspace_id == current_workspace_id(),
                    SqlProjectMergedPr.project_id == _hex(project.id),
                    in_window,
                    _COUNTED_ROW,
                )
                .group_by(SqlProjectMergedPr.member_key)
            ).all()
        return MemberTotals(
            total=sum(count for _member, count in rows),
            by_member={member: count for member, count in rows if member is not None},
        )

    def merged_items(self, project: Project, since_epoch: int | None = None) -> list[MergedItem]:
        """Every counted merge with its date and member, for per-day series.

        :param since_epoch: Leave out merges dated before then; undated ones are kept.
        """
        self._ensure_fresh(project)
        dated = (
            True
            if since_epoch is None
            else or_(
                SqlProjectMergedPr.merged_at.is_(None),
                SqlProjectMergedPr.merged_at >= since_epoch,
            )
        )
        with self._ticket_db("list_project_merged_prs") as session:
            rows = session.execute(
                select(
                    SqlProjectMergedPr.pr_key,
                    SqlProjectMergedPr.merged_at,
                    SqlProjectMergedPr.member_key,
                    SqlProjectMergedPr.claimed_by,
                ).where(
                    SqlProjectMergedPr.workspace_id == current_workspace_id(),
                    SqlProjectMergedPr.project_id == _hex(project.id),
                    dated,
                    _COUNTED_ROW,
                )
            ).all()
        return [
            MergedItem(key, merged_at, member, tuple(claimed_by.split()))
            for key, merged_at, member, claimed_by in rows
        ]

    def compute_items(self, project: Project) -> list[MergedItem]:
        """Every counted merge, computed from the session panels and Merged-stage tickets."""
        return combine(
            self._session_prs(project),
            self._merged_tickets(project),
            self._member_resolver(project),
        )

    def refresh(self, project: Project) -> None:
        """Recompute *project*'s stored rows; a write during it leaves the project stale."""
        workspace_id = current_workspace_id()
        project_hex = _hex(project.id)
        with _refresh_lock(workspace_id, project.id):
            started = int(time.time())
            _clear_stale(workspace_id, project.id)
            items = self.compute_items(project)

            def write(session: Any) -> None:
                wanted: dict[str, tuple[int | None, MemberKey | None, str]] = {}
                for item in items:
                    wanted.setdefault(
                        _row_key(item.key),
                        (
                            None if item.merged_at is None else int(item.merged_at),
                            item.member,
                            " ".join(item.claimed_by),
                        ),
                    )
                this_project = (
                    SqlProjectMergedPr.workspace_id == workspace_id,
                    SqlProjectMergedPr.project_id == project_hex,
                )
                stored = {
                    key: (merged_at, member, claimed_by)
                    for key, merged_at, member, claimed_by in session.execute(
                        select(
                            SqlProjectMergedPr.pr_key,
                            SqlProjectMergedPr.merged_at,
                            SqlProjectMergedPr.member_key,
                            SqlProjectMergedPr.claimed_by,
                        ).where(*this_project)
                    )
                }
                # Write only what changed; a refresh after one merge touches one row.
                gone = [key for key in stored if key not in wanted]
                for start in range(0, len(gone), _DELETE_CHUNK):
                    session.execute(
                        delete(SqlProjectMergedPr).where(
                            *this_project,
                            SqlProjectMergedPr.pr_key.in_(gone[start : start + _DELETE_CHUNK]),
                        )
                    )
                for key, (merged_at, member, claimed_by) in wanted.items():
                    if key not in stored:
                        session.add(
                            SqlProjectMergedPr(
                                workspace_id=workspace_id,
                                project_id=project_hex,
                                pr_key=key,
                                merged_at=merged_at,
                                member_key=member,
                                claimed_by=claimed_by,
                                updated_at=started,
                            )
                        )
                    elif stored[key] != (merged_at, member, claimed_by):
                        session.execute(
                            update(SqlProjectMergedPr)
                            .where(*this_project, SqlProjectMergedPr.pr_key == key)
                            .values(
                                merged_at=merged_at,
                                member_key=member,
                                claimed_by=claimed_by,
                                updated_at=started,
                            )
                        )
                marker = session.get(SqlProjectMergedPrRefresh, (workspace_id, project_hex))
                if marker is None:
                    session.add(
                        SqlProjectMergedPrRefresh(
                            workspace_id=workspace_id, project_id=project_hex, refreshed_at=started
                        )
                    )
                else:
                    marker.refreshed_at = started
                # Cached Summary numbers were read from the rows just replaced.
                invalidate_after_commit(session, project.id, ("prs",))

            token = _refreshing.set(True)
            try:
                run_write_transaction(self._ticket_write_db, "refresh_project_merged_prs", write)
            finally:
                _refreshing.reset(token)

    def _ensure_fresh(self, project: Project) -> None:
        workspace_id = current_workspace_id()
        _remember(self, workspace_id, project)
        with self._ticket_db("read_project_merged_pr_refresh") as session:
            refreshed_at = session.scalar(
                select(SqlProjectMergedPrRefresh.refreshed_at).where(
                    SqlProjectMergedPrRefresh.workspace_id == workspace_id,
                    SqlProjectMergedPrRefresh.project_id == _hex(project.id),
                )
            )
        if refreshed_at is None or _is_stale(workspace_id, project.id):
            self.refresh(project)
        elif time.time() - refreshed_at > _STALE_AFTER_S:
            mark_stale(project.id, workspace_id=workspace_id)

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

    def _merged_tickets(self, project: Project) -> list[MergedTicket]:
        stages = self._tickets.stages(project.id, user_id=project.user_id)["stages"]
        completed = next((stage for stage in reversed(stages) if stage.get("is_complete")), None)
        if completed is None:
            return []
        project_hex = _hex(project.id)
        # Move dates from the indexed stage-move table, so only PR outputs need decoding.
        latest_moves = self._tickets.latest_moves_into(
            project.id, completed["id"], user_id=project.user_id
        )
        event_kinds = (
            (EVENT_CODES["output"],)
            if latest_moves is not None
            else (EVENT_CODES["updated"], EVENT_CODES["output"])
        )
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
            if not ticket_ids:
                return []
            # Few PR outputs and links exist per project, so read them all and filter here:
            # a long IN list of ticket IDs costs more than the rows it saves.
            in_stage = set(ticket_ids)
            event_filter = (
                ()
                if latest_moves is not None
                else (SqlProjectTicketEvent.ticket_id.in_(ticket_ids),)
            )
            events = [
                event
                for event in session.scalars(
                    select(SqlProjectTicketEvent)
                    .where(
                        SqlProjectTicketEvent.workspace_id == current_workspace_id(),
                        SqlProjectTicketEvent.project_id == project_hex,
                        SqlProjectTicketEvent.kind.in_(event_kinds),
                        *event_filter,
                    )
                    .order_by(SqlProjectTicketEvent.created_at)
                )
                if event.ticket_id in in_stage
            ]
            links = [
                (ticket_id, session_id)
                for ticket_id, session_id in session.execute(
                    select(SqlProjectTicketSession.ticket_id, SqlProjectTicketSession.session_id)
                    .where(
                        SqlProjectTicketSession.workspace_id == current_workspace_id(),
                        SqlProjectTicketSession.project_id == project_hex,
                    )
                    .order_by(
                        SqlProjectTicketSession.created_at, SqlProjectTicketSession.session_id
                    )
                )
                if ticket_id in in_stage
            ]
        moved_at: dict[str, int] = dict(latest_moves or {})
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
            )
            for ticket_id in ticket_ids
            # A ticket stored in the stage without a recorded move (imported, legacy) is undated.
            if ticket_id in moved_at and ticket_id in pr_urls
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
    source = ProjectPrsMergedSource(
        tickets,
        location,
        conversations,
        pull_requests,
        getattr(app_state, "permission_store", None),
        projects,
    )
    add_invalidation_listener(_on_status_invalidated)
    return source


# ---- Keeping stored rows current -------------------------------------------
# (workspace, project) -> when a write first left its rows stale.
_stale: dict[tuple[int, str], float] = {}
# (workspace, project) -> the source and owner last used to read it, for background refreshes.
_known: dict[tuple[int, str], tuple[ProjectPrsMergedSource, str | None]] = {}
_state_lock = threading.Lock()
_wake = threading.Event()
_refresh_locks: dict[tuple[int, str], threading.Lock] = {}
_refresher: threading.Thread | None = None
# Set while a refresh commits, so its own cache invalidation doesn't mark it stale again.
_refreshing: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "project_merged_prs_refreshing", default=False
)


def _row_key(key: str) -> str:
    if len(key) <= _PR_KEY_MAX_CHARS:
        return key
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()


def _refresh_lock(workspace_id: int, project_id: str) -> threading.Lock:
    with _state_lock:
        return _refresh_locks.setdefault((workspace_id, str(project_id)), threading.Lock())


def _is_stale(workspace_id: int, project_id: str) -> bool:
    with _state_lock:
        return (workspace_id, str(project_id)) in _stale


def _clear_stale(workspace_id: int, project_id: str) -> None:
    with _state_lock:
        _stale.pop((workspace_id, str(project_id)), None)


def _remember(source: ProjectPrsMergedSource, workspace_id: int, project: Project) -> None:
    with _state_lock:
        _known[(workspace_id, str(project.id))] = (source, project.user_id)


def mark_stale(project_id: str, *, workspace_id: int | None = None) -> None:
    """Have *project_id*'s rows recomputed: in the background, or by its next read."""
    key = (current_workspace_id() if workspace_id is None else workspace_id, str(project_id))
    with _state_lock:
        _stale.setdefault(key, time.monotonic())
        known = key in _known
    if known:
        _ensure_refresher()
        _wake.set()


def _on_status_invalidated(project_id: str, inputs: tuple[str, ...]) -> None:
    # Ticket writes that can change merged PRs (moves, PR outputs, links, stages) clear "prs" too.
    if _refreshing.get() or "prs" not in inputs:
        return
    mark_stale(project_id)


def _ensure_refresher() -> None:
    global _refresher
    with _state_lock:
        if _refresher is not None and _refresher.is_alive():
            return
        _refresher = threading.Thread(
            target=_refresh_loop, name="project-merged-prs-refresher", daemon=True
        )
        _refresher.start()


def _refresh_loop() -> None:
    while True:
        _wake.wait(timeout=_REFRESH_COALESCE_S * 5)
        _wake.clear()
        time.sleep(_REFRESH_COALESCE_S)
        with _state_lock:
            due = [
                (key, _known[key])
                for key, since in _stale.items()
                if key in _known and time.monotonic() - since >= _REFRESH_COALESCE_S
            ]
            if any(key not in dict(due) for key in _stale if key in _known):
                _wake.set()
        for (workspace_id, project_id), (source, user_id) in due:
            try:
                with workspace_scope(workspace_id):
                    project = (
                        source.projects.get(project_id, user_id=user_id)
                        if source.projects is not None
                        else None
                    )
                    if project is None:
                        _clear_stale(workspace_id, project_id)
                        continue
                    source.refresh(project)
            except Exception:
                _logger.warning(
                    "Couldn't refresh merged PRs for project %s", project_id, exc_info=True
                )
