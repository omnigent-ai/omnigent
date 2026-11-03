"""Report each session's pull request discoveries to the server until it acknowledges them.

The server owns which PRs belong to a session. The runner records what it
discovers in :class:`~omnigent.runner.session_prs.SessionPrRegistry`, which keeps
the entries the server has not acknowledged, and sends them as an
``external_pull_requests`` session event. Entries are cleared only after a 2xx
response, and failures retry with backoff, so a PR opened during a turn reaches
the server even if nobody has the session open.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx
from filelock import Timeout as FileLockTimeout

from omnigent.runner.session_prs import SessionPrRegistry, SessionPullRequest

_logger = logging.getLogger(__name__)

EVENT_TYPE = "external_pull_requests"
# The server keeps at most this many PRs per session.
_MAX_PER_EVENT = 50
_RETRY_INITIAL_S = 5.0
_RETRY_MAX_S = 300.0

Post = Callable[..., Awaitable[httpx.Response]]


def report_payload(entry: SessionPullRequest) -> dict[str, Any]:
    """Return one association as the server's ``external_pull_requests`` event carries it."""
    return entry.model_dump(
        include={
            "url",
            "provider",
            "host",
            "repository",
            "number",
            "relationship",
            "first_seen_at",
            "last_seen_at",
            "title",
        }
    )


class PullRequestReporter:
    """Send each session's unacknowledged associations, one session at a time."""

    def __init__(self, post: Post) -> None:
        """
        :param post: The runner's authenticated server client's ``post``.
        """
        self._post = post
        self._loop = asyncio.get_running_loop()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Sessions notified while their report was running.
        self._again: set[str] = set()
        self._disabled = False

    def notify(self, session_id: str) -> None:
        """Report the session's unacknowledged associations; safe to call from any thread."""
        if self._disabled or not session_id:
            return
        try:
            self._loop.call_soon_threadsafe(self._start, session_id)
        except RuntimeError:  # The runner is shutting down.
            return

    def _start(self, session_id: str) -> None:
        if self._disabled:
            return
        if session_id in self._tasks:
            self._again.add(session_id)
            return
        self._tasks[session_id] = self._loop.create_task(self._report(session_id))

    async def _report(self, session_id: str) -> None:
        registry = SessionPrRegistry(session_id)
        delay = _RETRY_INITIAL_S
        try:
            while not self._disabled:
                self._again.discard(session_id)
                try:
                    revision, entries = await asyncio.to_thread(registry.pending_reports)
                except (OSError, ValueError, FileLockTimeout):
                    _logger.debug("Could not read unreported pull requests", exc_info=True)
                    entries, revision = None, 0
                if entries is not None and not entries:
                    if session_id in self._again:
                        continue
                    return
                outcome = "retry" if entries is None else await self._send(session_id, entries)
                if outcome == "acknowledged":
                    assert entries is not None
                    sent = [entry.url for entry in entries[:_MAX_PER_EVENT]]
                    try:
                        await asyncio.to_thread(registry.mark_reported, revision, sent)
                    except (OSError, ValueError, FileLockTimeout):
                        _logger.debug("Could not mark pull requests reported", exc_info=True)
                    delay = _RETRY_INITIAL_S
                    continue
                if outcome == "rejected":
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_S)
        finally:
            self._tasks.pop(session_id, None)

    async def _send(self, session_id: str, entries: Sequence[SessionPullRequest]) -> str:
        """Send one event; ``"acknowledged"``, ``"retry"`` or ``"rejected"``."""
        event = {
            "type": EVENT_TYPE,
            "data": {"prs": [report_payload(entry) for entry in entries[:_MAX_PER_EVENT]]},
        }
        try:
            response = await self._post(
                f"/v1/sessions/{quote(session_id, safe='')}/events", json=event
            )
        except (httpx.HTTPError, OSError):
            return "retry"
        if response.is_success:
            return "acknowledged"
        if response.status_code in (408, 429) or response.status_code >= 500:
            return "retry"
        if "Unknown event type" in response.text:
            # The server predates server-side PR tracking; nothing here can succeed.
            _logger.info("The server does not accept pull request reports")
            self._disabled = True
        else:
            _logger.warning(
                "The server rejected a pull request report (HTTP %s)",
                response.status_code,
                extra={"session_id": session_id},
            )
        return "rejected"

    async def aclose(self) -> None:
        """Stop reporting; unacknowledged entries stay in the registry for the next runner."""
        self._disabled = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


_reporter: PullRequestReporter | None = None


def install(reporter: PullRequestReporter | None) -> None:
    """Make ``reporter`` the one :func:`notify` uses; ``None`` uninstalls it."""
    global _reporter
    _reporter = reporter


def notify(session_id: str) -> None:
    """Report the session's discoveries; a no-op in processes without a server client."""
    reporter = _reporter
    if reporter is not None:
        reporter.notify(session_id)
