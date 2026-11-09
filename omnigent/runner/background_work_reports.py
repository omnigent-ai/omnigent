"""Report each session's background-work flag to the server, newest value last.

The flag reaches the server as an ``external_background_work`` session event.
One request runs per session at a time and always sends the newest value. Each
report carries when it was decided, strictly increasing, so the server keeps the
newest even if an abandoned request lands late. Transient failures, including a
request with no answer by its deadline, retry with backoff. A server that predates
the event rejects it as an unknown type, and the reporter stops sending until the
runner reconnects, possibly to an upgraded server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

EVENT_TYPE = "external_background_work"
_RETRY_INITIAL_S = 1.0
_RETRY_MAX_S = 30.0
# The runner's client has no read timeout, so an unanswered request would hold
# back every later value for its session.
_REQUEST_TIMEOUT_S = 10.0

Post = Callable[..., Awaitable[httpx.Response]]


class BackgroundWorkReporter:
    """Send each session's newest background-work flag, one request per session at a time."""

    def __init__(self, post: Post) -> None:
        """
        :param post: The runner's authenticated server client's ``post``.
        """
        self._post = post
        # Newest value per session, with when it was decided in epoch nanoseconds.
        self._wanted: dict[str, tuple[bool, int]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._disabled = False
        # Bumped on reconnect: an answer to a request sent before it is stale.
        self._generation = 0
        self._last_reported_at = 0

    def resume(self) -> None:
        """Send again after a reconnect, which may reach an upgraded server."""
        self._generation += 1
        self._disabled = False

    def report(self, session_id: str, running: bool) -> None:
        """
        Send *running* for *session_id*; call on the runner's event loop.

        :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param running: Whether the session's work outlives its ended turn.
        """
        if self._disabled:
            return
        self._last_reported_at = max(time.time_ns(), self._last_reported_at + 1)
        self._wanted[session_id] = (running, self._last_reported_at)
        if session_id not in self._tasks:
            self._tasks[session_id] = asyncio.get_running_loop().create_task(
                self._send_newest(session_id)
            )

    async def _send_newest(self, session_id: str) -> None:
        # The acknowledged value, with the connection generation it was sent on.
        sent: tuple[bool, int] | None = None
        delay = _RETRY_INITIAL_S
        try:
            while not self._disabled:
                wanted = self._wanted.get(session_id)
                if wanted is None:
                    return
                running, reported_at = wanted
                # A reconnect's resync must reach the new connection's server.
                if sent == (running, self._generation):
                    return
                generation = self._generation
                outcome = await self._send(session_id, running, reported_at)
                if outcome == "rejected":
                    return
                if outcome == "acknowledged":
                    sent = (running, generation)
                    delay = _RETRY_INITIAL_S
                    continue
                # A failed request may still have reached the server, so its value is unknown.
                sent = None
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_S)
        finally:
            self._tasks.pop(session_id, None)
            self._wanted.pop(session_id, None)

    async def _send(self, session_id: str, running: bool, reported_at: int) -> str:
        """Send one event; ``"acknowledged"``, ``"retry"`` or ``"rejected"``."""
        generation = self._generation
        try:
            response = await asyncio.wait_for(
                self._post(
                    f"/v1/sessions/{quote(session_id, safe='')}/events",
                    json={
                        "type": EVENT_TYPE,
                        "data": {"running": running, "reported_at": reported_at},
                    },
                ),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except (httpx.HTTPError, OSError):  # OSError includes the deadline's TimeoutError.
            return "retry"
        if response.is_success:
            return "acknowledged"
        if response.status_code in (408, 429) or response.status_code >= 500:
            return "retry"
        if "Unknown event type" in response.text:
            if generation != self._generation:
                # The server that refused it was replaced by a reconnect; try the new one.
                return "retry"
            # The server predates the flag, so nothing sent to it can succeed.
            _logger.info("The server does not accept background-work reports")
            self._disabled = True
        else:
            _logger.warning(
                "The server rejected a background-work report (HTTP %s)",
                response.status_code,
                extra={"session_id": session_id},
            )
        return "rejected"
