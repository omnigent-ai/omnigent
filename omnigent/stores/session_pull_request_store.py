"""Persistent store of the pull requests linked to each session.

The server owns which PRs belong to a session. Runners and hosts report the
PRs they discover and the states they fetch; this store keeps them in one JSON
document on the session's metadata row
(``omnigent_conversation_metadata.pull_requests``), so the Pull Requests tab
can list them while the host is offline. Reports merge idempotently, and a
report that changes nothing does not write. Unlinking drops the PR and
remembers the unlink, so later reports cannot link it again.

The document has two parts with separate limits: membership (each PR's
identity and the unlinks), which is checked when a PR is added, and cached
details (titles, states), which are trimmed first when the document outgrows
its byte budget. Refreshing details never removes a link.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal, cast

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Select, select, update
from sqlalchemy.orm import Session

from omnigent.db import compression
from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)

_logger = logging.getLogger(__name__)

Relationship = Literal["created", "worked_on", "attached", "inferred"]

MAX_PRS_PER_SESSION = 50
MAX_UNLINKS_PER_SESSION = 50
# Below the 16,384-byte limit on a stored column value, after compression.
_MAX_STORED_BYTES = 16_000
# Identities and unlinks alone; titles and states fill the rest.
_MAX_MEMBERSHIP_BYTES = 12_000
# Headroom for compression framing on incompressible text.
_FRAMING_BYTES = 32

# A stronger relationship survives any later report; "created" is never downgraded.
_RELATIONSHIP_RANK: dict[str, int] = {"inferred": 0, "worked_on": 1, "attached": 2, "created": 3}
_STATES = frozenset({"OPEN", "CLOSED", "MERGED"})
# GitHub's own limit on a PR title.
_TITLE_MAX = 256
# Hosts re-report the branch PR on every poll; coarser steps keep those polls read-only.
_SEEN_STEP_S = 60.0
_STATE_REFRESH_S = 60.0
_WATERMARKS_MAX = 10_000


class SessionPullRequestLimitError(Exception):
    """The session cannot link another pull request."""


class UnreadablePullRequestsError(Exception):
    """The stored document cannot be read, so it is left unchanged."""


@dataclass(frozen=True)
class PullRequestReport:
    """One PR as a runner or host describes it; times are epoch seconds.

    :ivar url: Canonical PR URL, as the host's git provider normalized it.
    :ivar provider: Git provider id, e.g. ``"github"``.
    :ivar host: Forge host, e.g. ``"github.com"``.
    :ivar repository: Base repository, e.g. ``"owner/repo"``.
    :ivar number: The PR number.
    :ivar relationship: How the PR joined the session.
    :ivar first_seen_at: When the PR was first observed.
    :ivar last_seen_at: When the PR was last observed.
    :ivar title: The host's latest title, or ``None`` when it has none.
    """

    url: str
    provider: str
    host: str
    repository: str
    number: int
    relationship: Relationship
    first_seen_at: float
    last_seen_at: float
    title: str | None = None


@dataclass(frozen=True)
class StoredPullRequest(PullRequestReport):
    """A linked PR with the last state its host fetched.

    :ivar state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``, or ``None`` until fetched.
    :ivar is_draft: Whether the PR was a draft at that fetch.
    :ivar state_checked_at: Epoch seconds of that fetch's request, or ``None``.
    """

    state: str | None = None
    is_draft: bool = False
    state_checked_at: float | None = None


@dataclass(frozen=True)
class FetchedState:
    """The state a host fetched for one PR.

    :ivar url: The PR the request named.
    :ivar number: The number of the PR the host described; a branch PR that the
        request did not name is not applied to it.
    :ivar state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``.
    :ivar is_draft: Whether the PR is a draft.
    :ivar title: The fetched title, or ``None`` to keep the stored one.
    """

    url: str
    number: int
    state: str
    is_draft: bool
    title: str | None = None


@dataclass(frozen=True)
class SessionPullRequests:
    """A session's linked PRs in display order.

    :ivar entries: The linked PRs, explicit ones newest first, then inferred ones oldest first.
    :ivar synced: Whether the server has imported the session's PRs from its host. Until
        then the list may be incomplete, so the host's own list still decides.
    """

    entries: list[StoredPullRequest]
    synced: bool

    def get(self, url: str) -> StoredPullRequest | None:
        """Return the linked PR with ``url``, or ``None``."""
        return next((entry for entry in self.entries if entry.url == url), None)


@dataclass(frozen=True)
class PullRequestPage:
    """One page of a session's linked PRs in display order.

    :ivar entries: The PRs on this page.
    :ivar has_more: Whether PRs follow the last one.
    :ivar synced: As in :class:`SessionPullRequests`.
    """

    entries: list[StoredPullRequest]
    has_more: bool
    synced: bool


@dataclass(frozen=True)
class SessionSummary:
    """What list surfaces show for one session.

    :ivar primary: The first linked PR, or ``None``.
    :ivar synced: As in :class:`SessionPullRequests`.
    """

    primary: StoredPullRequest | None
    synced: bool


@dataclass(frozen=True)
class _Unlink:
    url: str
    unlinked_at: float


@dataclass(frozen=True)
class _Document:
    synced_at: float | None = None
    prs: tuple[StoredPullRequest, ...] = ()
    # Oldest first.
    unlinked: tuple[_Unlink, ...] = ()


_DOCUMENT = TypeAdapter(_Document)


def _decode(text: str | None) -> _Document:
    """Parse a stored document; unknown relationships from newer servers read as ``worked_on``.

    :raises UnreadablePullRequestsError: The document cannot be read.
    """
    if text is None:
        return _Document()
    try:
        raw = json.loads(text)
        for entry in raw.get("prs", []):
            if isinstance(entry, dict) and entry.get("relationship") not in _RELATIONSHIP_RANK:
                entry["relationship"] = "worked_on"
        return _DOCUMENT.validate_python(raw)
    except (ValueError, AttributeError, TypeError, ValidationError) as exc:
        raise UnreadablePullRequestsError("Unreadable session pull request document") from exc


def _encode(document: _Document) -> str:
    return _DOCUMENT.dump_json(document).decode()


def _fits(document: _Document, limit: int) -> bool:
    """Whether ``document`` stores in at most ``limit`` bytes once compressed."""
    text = _encode(document)
    if len(text.encode()) + _FRAMING_BYTES <= limit:
        return True
    return len(compression.encode(text) or b"") <= limit


def _membership(document: _Document) -> _Document:
    """The document without cached details."""
    return replace(
        document,
        prs=tuple(
            replace(pr, title=None, state=None, is_draft=False, state_checked_at=None)
            for pr in document.prs
        ),
    )


def _fit(document: _Document) -> _Document:
    """Drop cached details, last PRs first, until the document can be stored.

    Membership alone always fits, because additions are checked against
    :data:`_MAX_MEMBERSHIP_BYTES`.
    """
    prs = list(document.prs)
    index = len(prs) - 1
    while not _fits(replace(document, prs=tuple(prs)), _MAX_STORED_BYTES) and index >= 0:
        pr = prs[index]
        if pr.title is not None:
            prs[index] = replace(pr, title=None)
        else:
            prs[index] = replace(pr, state=None, is_draft=False, state_checked_at=None)
            index -= 1
    return replace(document, prs=tuple(prs))


def _clean_title(title: str | None) -> str | None:
    return title.strip()[:_TITLE_MAX] if title and title.strip() else None


def _display_order(prs: Iterable[StoredPullRequest]) -> tuple[StoredPullRequest, ...]:
    """Explicit PRs newest first, then inferred PRs oldest first."""

    def key(pr: StoredPullRequest) -> tuple[bool, float, str]:
        inferred = pr.relationship == "inferred"
        return inferred, pr.first_seen_at if inferred else -pr.last_seen_at, pr.url

    return tuple(sorted(prs, key=key))


def _new_entry(report: PullRequestReport) -> StoredPullRequest:
    return StoredPullRequest(**{**asdict(report), "title": _clean_title(report.title)})


def _merge(
    entry: StoredPullRequest, report: PullRequestReport, requested_at: float | None
) -> StoredPullRequest:
    """Fold a report into a linked PR."""
    relationship = entry.relationship
    if _RELATIONSHIP_RANK[report.relationship] > _RELATIONSHIP_RANK[relationship]:
        relationship = report.relationship
    last_seen = entry.last_seen_at
    if report.last_seen_at >= last_seen + _SEEN_STEP_S:
        last_seen = report.last_seen_at
    title = _clean_title(report.title)
    # A response requested before the last full fetch carries an older title.
    fresh = (
        requested_at is None
        or entry.state_checked_at is None
        or requested_at >= entry.state_checked_at
    )
    return replace(
        entry,
        relationship=relationship,
        first_seen_at=min(entry.first_seen_at, report.first_seen_at),
        last_seen_at=last_seen,
        title=title if title is not None and fresh else entry.title,
    )


def _add(
    document: _Document, entries: dict[str, StoredPullRequest], report: PullRequestReport
) -> bool:
    """Add a new member if the session has room for it; ``entries`` is updated in place."""
    if len(entries) >= MAX_PRS_PER_SESSION:
        return False
    candidate = {**entries, report.url: _new_entry(report)}
    membership = _membership(replace(document, prs=_display_order(candidate.values())))
    if not _fits(membership, _MAX_MEMBERSHIP_BYTES):
        return False
    entries[report.url] = candidate[report.url]
    return True


def _merge_reports(
    document: _Document, reports: Sequence[PullRequestReport], requested_at: float | None
) -> _Document:
    unlinked = {unlink.url for unlink in document.unlinked}
    entries = {pr.url: pr for pr in document.prs}
    for report in reports:
        if report.url in entries:
            entries[report.url] = _merge(entries[report.url], report, requested_at)
        elif report.url not in unlinked and not _add(document, entries, report):
            _logger.debug("A session's pull request list is full; ignoring a reported PR")
    return replace(document, prs=_display_order(entries.values()))


def _apply_fetch(document: _Document, fetched: FetchedState, checked_at: float) -> _Document:
    title = _clean_title(fetched.title)

    def refresh(entry: StoredPullRequest) -> StoredPullRequest:
        if entry.url != fetched.url:
            return entry
        changed = (entry.state, entry.is_draft) != (fetched.state, fetched.is_draft) or (
            title is not None and title != entry.title
        )
        stale = (
            entry.state_checked_at is None
            or checked_at - entry.state_checked_at >= _STATE_REFRESH_S
        )
        if not (changed or stale):
            return entry
        return replace(
            entry,
            state=fetched.state,
            is_draft=fetched.is_draft,
            state_checked_at=checked_at,
            title=title or entry.title,
        )

    return replace(document, prs=tuple(refresh(pr) for pr in document.prs))


def _result(document: _Document) -> SessionPullRequests:
    return SessionPullRequests(entries=list(document.prs), synced=document.synced_at is not None)


class _Watermarks:
    """The newest fetch each session's PR answered in this process, including unwritten ones.

    A response is ingested by the process that sent its request, so this orders
    every response the process sees, while unchanged ones still skip the write.
    """

    def __init__(self) -> None:
        self._seen: OrderedDict[tuple[int, str, str], float] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[int, str, str]) -> float:
        with self._lock:
            return self._seen.get(key, 0.0)

    def advance(self, key: tuple[int, str, str], checked_at: float) -> None:
        with self._lock:
            if checked_at > self._seen.get(key, 0.0):
                self._seen[key] = checked_at
            self._seen.move_to_end(key)
            while len(self._seen) > _WATERMARKS_MAX:
                self._seen.popitem(last=False)


class SessionPullRequestStore:
    """SQLAlchemy-backed store of the pull requests linked to each session."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy URI of the database holding
            ``omnigent_conversation_metadata``, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store", immediate=True
        )
        self._watermarks = _Watermarks()

    def _rows(self, conversation_ids: Sequence[str]) -> Select[tuple[str, str | None]]:
        return select(SqlConversationMetadata.id, SqlConversationMetadata.pull_requests).where(
            SqlConversationMetadata.workspace_id == current_workspace_id(),
            SqlConversationMetadata.id.in_(conversation_ids),
        )

    def _load(self, session: Session, conversation_id: str, *, lock: bool) -> _Document | None:
        """Read the session's document, or ``None`` when the session does not exist."""
        stmt = self._rows([conversation_id])
        row = session.execute(stmt.with_for_update() if lock else stmt).one_or_none()
        return None if row is None else _decode(row.pull_requests)

    def read(self, conversation_id: str) -> SessionPullRequests | None:
        """Return the session's linked PRs, or ``None`` when the session does not exist.

        An unreadable document reads as not synced, so the host's own list decides.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        with self._session("get_session_pull_requests") as session:
            try:
                document = self._load(session, conversation_id, lock=False)
            except UnreadablePullRequestsError:
                _logger.warning("Unreadable session pull request document", exc_info=True)
                return SessionPullRequests(entries=[], synced=False)
        return None if document is None else _result(document)

    def _update(
        self, operation: str, conversation_id: str, change: Callable[[_Document], _Document]
    ) -> SessionPullRequests | None:
        """Apply ``change`` to the session's document, writing only when it changes it.

        :returns: The resulting PRs, or ``None`` when the session does not exist.
        :raises UnreadablePullRequestsError: The stored document cannot be read.
        """

        def apply(current: _Document) -> _Document:
            updated = change(current)
            return current if updated == current else _fit(updated)

        with self._session("get_session_pull_requests") as session:
            current = self._load(session, conversation_id, lock=False)
        if current is None:
            return None
        if apply(current) == current:
            return _result(current)

        def write(session: Session) -> _Document | None:
            current = self._load(session, conversation_id, lock=True)
            if current is None:
                return None
            updated = apply(current)
            if updated != current:
                session.execute(
                    update(SqlConversationMetadata)
                    .where(
                        SqlConversationMetadata.workspace_id == current_workspace_id(),
                        SqlConversationMetadata.id == conversation_id,
                    )
                    .values(pull_requests=_encode(updated))
                )
            return updated

        written = cast(
            "_Document | None", run_write_transaction(self._session_immediate, operation, write)
        )
        return None if written is None else _result(written)

    def list_page(
        self, conversation_id: str, *, limit: int, after_url: str | None = None
    ) -> PullRequestPage:
        """Return a page of the session's linked PRs in display order.

        A session holds at most :data:`MAX_PRS_PER_SESSION` PRs, so the cursor
        is the previous page's last URL.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param limit: The most PRs to return.
        :param after_url: The last PR of the previous page, or ``None`` for the first page.
        :raises LookupError: ``after_url`` is not linked to the session.
        """
        stored = self.read(conversation_id) or SessionPullRequests(entries=[], synced=False)
        start = 0
        if after_url is not None:
            start = next(
                (index + 1 for index, pr in enumerate(stored.entries) if pr.url == after_url), -1
            )
            if start < 0:
                raise LookupError(after_url)
        return PullRequestPage(
            entries=stored.entries[start : start + limit],
            has_more=len(stored.entries) > start + limit,
            synced=stored.synced,
        )

    def summaries(self, conversation_ids: Sequence[str]) -> dict[str, SessionSummary]:
        """Return each session's first linked PR and whether it is synced, in one query.

        :param conversation_ids: Sessions to summarize; ones without a metadata row are absent.
        """
        if not conversation_ids:
            return {}
        stmt = self._rows(list(dict.fromkeys(conversation_ids)))
        with self._session("list_session_pull_request_summaries") as session:
            rows = session.execute(stmt).all()
        summaries: dict[str, SessionSummary] = {}
        for row in rows:
            try:
                document = _decode(row.pull_requests)
            except UnreadablePullRequestsError:
                _logger.warning("Unreadable session pull request document", exc_info=True)
                document = _Document()
            summaries[row.id] = SessionSummary(
                primary=document.prs[0] if document.prs else None,
                synced=document.synced_at is not None,
            )
        return summaries

    def apply_report(
        self,
        conversation_id: str,
        reports: Sequence[PullRequestReport],
        *,
        fetched: FetchedState | None = None,
        requested_at: float | None = None,
        complete: bool = False,
    ) -> SessionPullRequests | None:
        """Merge what a runner or host reported, in one read and at most one write.

        Unlinked PRs are ignored, and a PR past the session's limits is not added.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param reports: PRs as the runner's or host's registry describes them.
        :param fetched: The state the host fetched for one of the session's PRs, if any.
        :param requested_at: Epoch seconds the server sent the request the report
            answers, or ``None`` for a report the runner pushed. Orders fetches:
            one older than the newest seen is ignored.
        :param complete: Whether ``reports`` is the host's whole list, which
            marks the session synced.
        :returns: The resulting PRs, or ``None`` when the session does not exist.
        :raises UnreadablePullRequestsError: The stored document cannot be read.
        """
        unique = list({report.url: report for report in reports}.values())
        accepted: tuple[tuple[int, str, str], float] | None = None
        if fetched is not None and requested_at is not None and fetched.state in _STATES:
            key = (current_workspace_id(), conversation_id, fetched.url)
            if requested_at >= self._watermarks.get(key):
                accepted = (key, requested_at)
        synced_at = time.time()

        def change(document: _Document) -> _Document:
            updated = _merge_reports(document, unique, requested_at) if unique else document
            if complete and updated.synced_at is None:
                updated = replace(updated, synced_at=synced_at)
            if accepted is not None and fetched is not None:
                entry = next((pr for pr in updated.prs if pr.url == fetched.url), None)
                checked_at = accepted[1]
                if (
                    entry is not None
                    and entry.number == fetched.number
                    and (entry.state_checked_at is None or checked_at >= entry.state_checked_at)
                ):
                    updated = _apply_fetch(updated, fetched, checked_at)
            return updated

        result = self._update("apply_session_pull_request_report", conversation_id, change)
        if accepted is not None:
            self._watermarks.advance(*accepted)
        return result

    def check_room(self, conversation_id: str, url: str) -> None:
        """Fail before a host links a PR the session has no room for.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The PR the user is linking, as they entered it.
        :raises SessionPullRequestLimitError: The session links too many PRs already.
        """
        stored = self.read(conversation_id)
        if stored is None or stored.get(url) is not None:
            return
        if len(stored.entries) >= MAX_PRS_PER_SESSION:
            raise SessionPullRequestLimitError(
                f"A session can link at most {MAX_PRS_PER_SESSION} pull requests"
            )

    def attach(
        self, conversation_id: str, report: PullRequestReport
    ) -> SessionPullRequests | None:
        """Link a PR the user chose, clearing an earlier unlink.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param report: The PR as the host that verified it describes it.
        :returns: The resulting PRs, or ``None`` when the session does not exist.
        :raises SessionPullRequestLimitError: The session has no room for another PR.
        :raises UnreadablePullRequestsError: The stored document cannot be read.
        """
        attached = replace(report, relationship="attached")

        def change(document: _Document) -> _Document:
            entries = {pr.url: pr for pr in document.prs}
            unlinked = tuple(unlink for unlink in document.unlinked if unlink.url != report.url)
            document = replace(document, unlinked=unlinked)
            if report.url in entries:
                entries[report.url] = _merge(entries[report.url], attached, None)
            elif not _add(document, entries, attached):
                raise SessionPullRequestLimitError(
                    f"A session can link at most {MAX_PRS_PER_SESSION} pull requests"
                )
            return replace(document, prs=_display_order(entries.values()))

        return self._update("attach_session_pull_request", conversation_id, change)

    def remove(self, conversation_id: str, url: str) -> bool:
        """Unlink a PR, remembering it so later reports cannot link it again.

        The oldest unlinks are forgotten beyond :data:`MAX_UNLINKS_PER_SESSION`.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :returns: Whether the PR was linked.
        :raises UnreadablePullRequestsError: The stored document cannot be read.
        """
        unlinked_at = now_epoch_us() / 1_000_000
        removed: list[bool] = []

        def change(document: _Document) -> _Document:
            removed[:] = [any(pr.url == url for pr in document.prs)]
            unlinked = [unlink for unlink in document.unlinked if unlink.url != url]
            unlinked.append(_Unlink(url=url, unlinked_at=unlinked_at))
            updated = replace(
                document,
                prs=tuple(pr for pr in document.prs if pr.url != url),
                unlinked=tuple(unlinked[-MAX_UNLINKS_PER_SESSION:]),
            )
            # Unlinks share the membership budget; the oldest give way first.
            while len(updated.unlinked) > 1 and not _fits(
                _membership(updated), _MAX_MEMBERSHIP_BYTES
            ):
                updated = replace(updated, unlinked=updated.unlinked[1:])
            return updated

        self._update("unlink_session_pull_request", conversation_id, change)
        return bool(removed and removed[0])
