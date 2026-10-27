"""Server-side state for the Pull Requests tab.

The server owns which pull requests belong to a session
(:class:`~omnigent.stores.session_pull_request_store.SessionPullRequestStore`).
The session's runner or host is the only forge client. It reports the PRs it
discovers, as ``external_pull_requests`` events and in the ``prs`` of each
response the server proxies, and fetches details for the PR the server names.
The server vouches for that PR (:func:`vouch`), so a host serves it even if it
never recorded it, and answers with its own list, so the tab shows the same
PRs whether or not the host is online.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from omnigent.git_providers import provider_display
from omnigent.server.schemas import SessionPullRequest, SessionPullRequestList
from omnigent.stores.session_pull_request_store import (
    FetchedState,
    PullRequestPage,
    PullRequestReport,
    SessionPullRequests,
    SessionPullRequestStore,
    SessionSummary,
    StoredPullRequest,
)

_logger = logging.getLogger(__name__)

# Bounds what one report can write; a session keeps at most 50 PRs.
_MAX_REPORTED_PRS = 200
_RELATIONSHIPS = frozenset({"created", "worked_on", "attached", "inferred"})
# How long a stored state counts as current; older open PRs are refreshed when listed.
FRESHNESS_S = 300.0
_ATTEMPTS_MAX = 10_000

# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, SessionPullRequestStore] = {}
_stores_lock = threading.Lock()


def open_store(conversation_store: object) -> SessionPullRequestStore | None:
    """Return the PR store beside the session metadata, or ``None`` without a SQL backend.

    :param conversation_store: The server's conversation store; PRs live in the
        database at its ``storage_location``.
    """
    location = getattr(conversation_store, "storage_location", None)
    if not isinstance(location, str) or not location:
        return None
    with _stores_lock:
        store = _stores.get(location)
        if store is None:
            try:
                store = SessionPullRequestStore(location)
            except Exception:
                _logger.warning("Session pull request storage is unavailable", exc_info=True)
                return None
            _stores[location] = store
        return store


class _ReportedPullRequest(BaseModel):
    """One PR as a runner's or host's registry describes it."""

    model_config = ConfigDict(extra="ignore")

    url: Annotated[str, StringConstraints(max_length=2048, pattern=r"^https?://\S+$")]
    # Hosts that predate git providers report only GitHub PRs and omit the field.
    provider: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "github"
    host: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    repository: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    number: int = Field(ge=0)
    # Newer hosts may report relationships this server doesn't know; they read as worked_on.
    relationship: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    first_seen_at: float = Field(ge=0)
    last_seen_at: float = Field(ge=0)
    title: str | None = None


def parse_reports(entries: object) -> list[PullRequestReport]:
    """Return the well-formed entries of a reported ``prs`` list; others are skipped.

    :param entries: The ``prs`` of a ``session.github.info`` payload or an
        ``external_pull_requests`` event.
    """
    if not isinstance(entries, list):
        return []
    reports: list[PullRequestReport] = []
    for entry in entries[:_MAX_REPORTED_PRS]:
        try:
            parsed = _ReportedPullRequest.model_validate(entry)
        except ValidationError:
            _logger.debug("Skipping a malformed reported pull request", exc_info=True)
            continue
        fields = parsed.model_dump()
        if fields["relationship"] not in _RELATIONSHIPS:
            fields["relationship"] = "worked_on"
        reports.append(PullRequestReport(**fields))
    return reports


def fetched_state(info: Mapping[str, Any]) -> FetchedState | None:
    """Read the selected PR's state, draft flag and title from a host payload.

    The body, checks, comments, changed files and diff stay with the host.

    :param info: A ``session.github.info`` payload from a runner or host.
    :returns: The fetch, or ``None`` when the payload carries no PR state.
    """
    selected, pr = info.get("selected_pr_url"), info.get("pr")
    if not isinstance(selected, str) or not isinstance(pr, dict):
        return None
    state, number, title = pr.get("state"), pr.get("number"), pr.get("title")
    if not isinstance(state, str) or not isinstance(number, int):
        return None
    return FetchedState(
        url=selected,
        number=number,
        state=state,
        is_draft=pr.get("is_draft") is True,
        title=title if isinstance(title, str) else None,
    )


def ingest_info(
    store: SessionPullRequestStore,
    session_id: str,
    info: Mapping[str, Any],
    *,
    requested_at: float,
) -> SessionPullRequests | None:
    """Merge a host payload's PRs and the selected PR's state into the store.

    :param store: The server's PR store.
    :param session_id: The session the payload describes.
    :param info: A ``session.github.info`` payload from a runner or host.
    :param requested_at: Epoch seconds the server sent the request it answers,
        which orders it against other fetches.
    :returns: The session's PRs afterwards, or ``None`` when the session does not exist.
    """
    return store.apply_report(
        session_id,
        parse_reports(info.get("prs")),
        fetched=fetched_state(info),
        requested_at=requested_at,
        complete=info.get("tracking_available") is True and isinstance(info.get("prs"), list),
    )


class NotAssociatedError(Exception):
    """The request names a PR the session does not link."""


def vouch(stored: SessionPullRequests | None, pr_url: object) -> dict[str, Any]:
    """Return the request params that vouch for the PR a request targets.

    Until the server has imported a session's PRs, the host's own list decides,
    so nothing is added.

    :param stored: The session's PRs, or ``None`` without PR storage.
    :param pr_url: The PR the request names, if any.
    :raises NotAssociatedError: The request names a PR the session does not link.
    """
    if stored is None or not stored.synced:
        return {}
    if isinstance(pr_url, str) and pr_url:
        if stored.get(pr_url) is None:
            raise NotAssociatedError("This pull request is not associated with the session")
        return {"server_tracked": True}
    if stored.entries:
        return {"server_tracked": True, "default_pr_url": stored.entries[0].url}
    return {}


def entry_payload(entry: StoredPullRequest) -> dict[str, Any]:
    """Serialize a stored PR in the shape of a ``session.github.info`` ``prs`` entry.

    :param entry: A PR from the store.
    """
    return {
        "url": entry.url,
        "provider": entry.provider,
        "host": entry.host,
        "repository": entry.repository,
        "number": entry.number,
        "relationship": entry.relationship,
        "first_seen_at": entry.first_seen_at,
        "last_seen_at": entry.last_seen_at,
        "title": entry.title,
        "provider_display": provider_display(entry.provider),
        "state": entry.state,
        "is_draft": entry.is_draft,
        "state_checked_at": entry.state_checked_at,
    }


def answer(info: dict[str, Any], stored: SessionPullRequests | None) -> dict[str, Any]:
    """Replace a host payload's PR list with the server's.

    Once the session is synced, a selection the server doesn't link (an unlinked
    PR an older host still selects, or one the session had no room for) is dropped.

    :param info: A ``session.github.info`` payload already passed to :func:`ingest_info`.
    :param stored: The session's PRs afterwards.
    :returns: ``info`` with ``prs`` from the server, or unchanged for a host that
        does not track session PRs.
    """
    if "prs" not in info or stored is None:
        return info
    updated = {**info, "prs": [entry_payload(entry) for entry in stored.entries]}
    selected = updated.get("selected_pr_url")
    if stored.synced and isinstance(selected, str) and stored.get(selected) is None:
        updated.pop("selected_pr_url")
        updated["pr"] = None
    return updated


def list_page(page: PullRequestPage) -> SessionPullRequestList:
    """Serialize a page of stored PRs for ``GET /v1/sessions/{id}/pull-requests``."""
    data = [SessionPullRequest(**entry_payload(entry)) for entry in page.entries]
    return SessionPullRequestList(
        data=data,
        first_id=data[0].url if data else None,
        last_id=data[-1].url if data else None,
        has_more=page.has_more,
        synced=page.synced,
    )


def primary_summary(entry: StoredPullRequest) -> dict[str, Any]:
    """Serialize a session's first PR for its ``SessionListItem``.

    :param entry: The session's first PR in display order.
    """
    return {
        "url": entry.url,
        "provider": entry.provider,
        "provider_display": provider_display(entry.provider),
        "repository": entry.repository,
        "number": entry.number,
        "title": entry.title,
        "state": entry.state,
        "is_draft": entry.is_draft,
        "state_checked_at": entry.state_checked_at,
    }


def needs_refresh(summary: SessionSummary, *, now: float) -> bool:
    """Whether a listed session should be refreshed from its runner or host.

    True until the session is imported, and while its first PR is open (or its
    state unknown) and older than :data:`FRESHNESS_S`. Merged and closed PRs
    refresh only with the session's own reads and turns.
    """
    if not summary.synced:
        return True
    primary = summary.primary
    if primary is None or primary.state not in (None, "OPEN"):
        return False
    checked = primary.state_checked_at
    return checked is None or now - checked >= FRESHNESS_S


class PullRequestRefresher:
    """Refreshes sessions' stored PRs from their runner or host in the background.

    One per app, shut down with it. A turn end refreshes after a short delay
    unless a client read already covered the turn, and a turn that ends during
    a refresh gets one more. Listed sessions are refreshed at most once per
    :data:`FRESHNESS_S` each. Jobs run in a copy of the scheduling context, so
    workspace scoping holds, and at most ``concurrency`` run at once.
    """

    def __init__(self, *, concurrency: int = 4, turn_end_delay_s: float = 5.0) -> None:
        """
        :param concurrency: The most refreshes that run at once.
        :param turn_end_delay_s: Seconds to wait after a turn ends before refreshing.
        """
        self._refresh: Callable[[str], Awaitable[None]] | None = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._delay_s = turn_end_delay_s
        # Sessions with a turn no read has covered yet, by when that turn ended.
        self._turn_ended: dict[str, float] = {}
        # When each listed session was last refreshed, on the monotonic clock.
        self._attempted: dict[str, float] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def bind(self, refresh: Callable[[str], Awaitable[None]]) -> None:
        """Set how one session is refreshed; failures are logged.

        :param refresh: Reads the session's PR info from its runner or host and stores it.
        """
        self._refresh = refresh

    def turn_ended(self, session_id: str) -> None:
        """Refresh after a turn ends; a no-op outside an event loop."""
        if self._refresh is None or self._closed:
            return
        self._turn_ended[session_id] = time.time()
        self._start(session_id, refresh_now=False)

    def note_read(self, session_id: str, requested_at: float) -> None:
        """Mark a session's ended turn as covered by a read sent after it ended."""
        ended = self._turn_ended.get(session_id)
        if ended is not None and requested_at >= ended:
            del self._turn_ended[session_id]

    def request(self, session_id: str) -> None:
        """Refresh a listed session that needs it, at most once per :data:`FRESHNESS_S`."""
        if self._refresh is None or self._closed or session_id in self._running:
            return
        now = time.monotonic()
        last = self._attempted.get(session_id)
        if last is not None and now - last < FRESHNESS_S:
            return
        if len(self._attempted) >= _ATTEMPTS_MAX:
            self._attempted = {
                key: value for key, value in self._attempted.items() if now - value < FRESHNESS_S
            }
        self._attempted[session_id] = now
        self._start(session_id, refresh_now=True)

    def _start(self, session_id: str, *, refresh_now: bool) -> None:
        if session_id in self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._running[session_id] = loop.create_task(self._run(session_id, refresh_now))

    async def _run(self, session_id: str, refresh_now: bool) -> None:
        try:
            if refresh_now:
                await self._refresh_once(session_id)
            while session_id in self._turn_ended:
                await asyncio.sleep(self._delay_s)
                if self._turn_ended.pop(session_id, None) is not None:
                    await self._refresh_once(session_id)
        finally:
            self._running.pop(session_id, None)

    async def _refresh_once(self, session_id: str) -> None:
        assert self._refresh is not None
        async with self._semaphore:
            try:
                await self._refresh(session_id)
            except Exception:
                _logger.debug("Pull request refresh failed", exc_info=True)

    async def aclose(self) -> None:
        """Cancel running refreshes; later requests are ignored."""
        self._closed = True
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# The app's refresher, for turn ends published outside a request.
_installed: PullRequestRefresher | None = None


def install_refresher(refresher: PullRequestRefresher | None) -> None:
    """Make ``refresher`` the one :func:`notify_turn_end` uses; ``None`` uninstalls it."""
    global _installed
    _installed = refresher


def notify_turn_end(session_id: str) -> None:
    """Refresh a session's stored PRs after its turn ended."""
    if _installed is not None:
        _installed.turn_ended(session_id)
