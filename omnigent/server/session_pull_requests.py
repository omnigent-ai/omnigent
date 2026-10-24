"""Server-side state for the Pull Requests tab.

The session's runner or host is the only forge client. Each pull request
response the server proxies carries the host's local registry (``prs``) and the
selected PR's details (``pr``). The server merges both into
:class:`~omnigent.stores.session_pull_request_store.SessionPullRequestStore`
and answers with its own list, so the tab shows the same PRs whether or not the
host is online. Requests carry the URLs the user unlinked on the server
(:data:`REMOVED_PRS_PARAM`) so the host stops selecting them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from omnigent.git_providers import provider_display
from omnigent.runner.session_prs import REMOVED_PRS_PARAM as REMOVED_PRS_PARAM
from omnigent.stores.session_pull_request_store import (
    PullRequestReport,
    SessionPullRequestStore,
    StoredPullRequest,
)

_logger = logging.getLogger(__name__)

# Bounds what one response can write; a session links a handful of PRs.
_MAX_REPORTED_PRS = 200

# custom-lint: disable-next=workspace-scoped-cache -- keyed by database URL; queries scope rows
_stores: dict[str, SessionPullRequestStore] = {}
_stores_lock = threading.Lock()


def open_store(conversation_store: object) -> SessionPullRequestStore | None:
    """Return the PR store beside the session metadata, or ``None`` without a SQL backend.

    :param conversation_store: The server's conversation store; PR rows live in
        the database at its ``storage_location``.
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
    """One ``prs`` entry of a host's ``session.github.info`` payload."""

    model_config = ConfigDict(extra="ignore")

    url: Annotated[str, StringConstraints(max_length=2048, pattern=r"^https?://\S+$")]
    # Hosts that predate git providers report only GitHub PRs and omit the field.
    provider: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "github"
    host: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    repository: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    number: int = Field(ge=0)
    relationship: Literal["created", "worked_on", "attached", "inferred"]
    first_seen_at: float = Field(ge=0)
    last_seen_at: float = Field(ge=0)
    title: str | None = None


def reports_from_info(info: Mapping[str, Any]) -> list[PullRequestReport]:
    """Return the well-formed ``prs`` entries of a host payload; others are skipped.

    :param info: A ``session.github.info`` payload from a runner or host.
    """
    entries = info.get("prs")
    if not isinstance(entries, list):
        return []
    reports: list[PullRequestReport] = []
    for entry in entries[:_MAX_REPORTED_PRS]:
        try:
            parsed = _ReportedPullRequest.model_validate(entry)
        except ValidationError:
            _logger.debug("Skipping a malformed reported pull request", exc_info=True)
            continue
        reports.append(PullRequestReport(**parsed.model_dump()))
    return reports


def fetched_state(pr: object) -> tuple[str, bool, str | None] | None:
    """Read the state, draft flag and title the tab shows while the host is offline.

    The body, checks, comments, changed files and diff stay with the host.

    :param pr: The ``pr`` object of a ``session.github.info`` payload.
    :returns: ``(state, is_draft, title)``, or ``None`` when ``pr`` is not a PR object.
    """
    if not isinstance(pr, dict) or not isinstance(pr.get("state"), str):
        return None
    title = pr.get("title")
    return pr["state"], pr.get("is_draft") is True, title if isinstance(title, str) else None


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


def list_payload(entries: Sequence[StoredPullRequest]) -> dict[str, Any]:
    """Serialize a session's PRs for ``GET /v1/sessions/{id}/pull-requests``."""
    return {"object": "list", "data": [entry_payload(entry) for entry in entries]}


def removed_param(urls: Sequence[str]) -> str | None:
    """Encode unlinked URLs for ``REMOVED_PRS_PARAM``, or ``None`` when there are none."""
    return json.dumps(list(urls)) if urls else None


def ingest_info(
    store: SessionPullRequestStore,
    session_id: str,
    info: Mapping[str, Any],
    *,
    synced_at: float | None = None,
) -> bool:
    """Merge a host payload's PRs and the selected PR's state into the store.

    :param store: The server's PR store.
    :param session_id: The session the payload describes.
    :param info: A ``session.github.info`` payload from a runner or host.
    :param synced_at: Epoch seconds the host produced it; defaults to now.
    :returns: Whether the stored state changed.
    """
    reports = reports_from_info(info)
    changed = store.record(session_id, reports)
    selected = info.get("selected_pr_url")
    pr = info.get("pr")
    fetched = fetched_state(pr)
    if isinstance(selected, str) and fetched is not None and isinstance(pr, dict):
        reported = next((report for report in reports if report.url == selected), None)
        # The host may describe a branch PR that the selection does not name.
        if reported is not None and pr.get("number") == reported.number:
            state, is_draft, title = fetched
            changed = (
                store.update_state(
                    session_id,
                    selected,
                    state=state,
                    is_draft=is_draft,
                    title=title,
                    checked_at=time.time() if synced_at is None else synced_at,
                )
                or changed
            )
    return changed


def apply_server_state(
    store: SessionPullRequestStore, session_id: str, info: dict[str, Any]
) -> dict[str, Any]:
    """Replace a host payload's PR list with the server's.

    :param store: The server's PR store.
    :param session_id: The session the payload describes.
    :param info: A ``session.github.info`` payload already passed to :func:`ingest_info`.
    :returns: ``info`` with ``prs`` from the server, or unchanged for a host that
        does not track session PRs.
    """
    if "prs" not in info:
        return info
    entries = store.list_for_session(session_id)
    updated = {**info, "prs": [entry_payload(entry) for entry in entries]}
    selected = updated.get("selected_pr_url")
    # @deprecated server-pr-tracking compat, remove in 0.24.0 (~2026-11-24): hosts before
    # 0.20.0 ignore REMOVED_PRS_PARAM and can still select a PR the user unlinked.
    if isinstance(selected, str) and all(entry.url != selected for entry in entries):
        updated.pop("selected_pr_url")
        updated["pr"] = None
    return updated


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
    }


class TurnEndSync:
    """Refresh a session's PRs from its host after each turn, one refresh at a time.

    A turn can open a PR while no client watches the session; reading the PR
    resource when the turn ends records it before the host can go offline.
    """

    def __init__(self, refresh: Callable[[str], Awaitable[None]]) -> None:
        """
        :param refresh: Reads and stores one session's PRs; failures are logged.
        """
        self._refresh = refresh
        self._running: dict[str, asyncio.Task[None]] = {}

    def notify(self, session_id: str) -> None:
        """Start a refresh unless one is running; a no-op outside an event loop."""
        if session_id in self._running:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._run(session_id))
        self._running[session_id] = task
        task.add_done_callback(lambda _: self._running.pop(session_id, None))

    async def _run(self, session_id: str) -> None:
        try:
            await self._refresh(session_id)
        except Exception:
            _logger.debug("Turn-end pull request refresh failed", exc_info=True)


_turn_end_sync: TurnEndSync | None = None


def set_turn_end_sync(sync: TurnEndSync | None) -> None:
    """Install the app's turn-end refresher, or ``None`` to remove it."""
    global _turn_end_sync
    _turn_end_sync = sync


def notify_turn_end(session_id: str) -> None:
    """Tell the installed refresher that a session's turn ended."""
    if _turn_end_sync is not None:
        _turn_end_sync.notify(session_id)
