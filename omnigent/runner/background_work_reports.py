"""Report each session's background-work flag to the server, newest value last.

The flag reaches the server as an ``external_background_work`` session event.
One request runs per session at a time and always sends the newest value, so an
older value can never land after a newer one. Transient failures retry with
backoff. A server that predates the event rejects it as an unknown type, and the
reporter stops sending until the runner reconnects, possibly to an upgraded server.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

EVENT_TYPE = "external_background_work"
_RETRY_INITIAL_S = 1.0
_RETRY_MAX_S = 30.0

Post = Callable[..., Awaitable[httpx.Response]]


class BackgroundWorkReporter:
    """Send each session's newest background-work flag, one request per session at a time."""

    def __init__(self, post: Post) -> None:
        """
        :param post: The runner's authenticated server client's ``post``.
        """
        self._post = post
        self._wanted: dict[str, bool] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._disabled = False
        # Bumped on reconnect: an answer to a request sent before it is stale.
        self._generation = 0

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
        self._wanted[session_id] = running
        if session_id not in self._tasks:
            self._tasks[session_id] = asyncio.get_running_loop().create_task(
                self._send_newest(session_id)
            )

    async def _send_newest(self, session_id: str) -> None:
        sent: bool | None = None
        delay = _RETRY_INITIAL_S
        try:
            while not self._disabled:
                running = self._wanted.get(session_id)
                if running is None or running == sent:
                    return
                outcome = await self._send(session_id, running)
                if outcome == "rejected":
                    return
                if outcome == "acknowledged":
                    sent = running
                    delay = _RETRY_INITIAL_S
                    continue
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_S)
        finally:
            self._tasks.pop(session_id, None)
            self._wanted.pop(session_id, None)

    async def _send(self, session_id: str, running: bool) -> str:
        """Send one event; ``"acknowledged"``, ``"retry"`` or ``"rejected"``."""
        generation = self._generation
        try:
            response = await self._post(
                f"/v1/sessions/{quote(session_id, safe='')}/events",
                json={"type": EVENT_TYPE, "data": {"running": running}},
            )
        except (httpx.HTTPError, OSError):
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
