"""Claude-native ``/goal`` reads for the runner's ``/events`` route.

Claude Code keeps its goal in the session, not behind an API, so the runner
derives it from the transcript the bridge's hooks reported.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable

import httpx
from fastapi.responses import JSONResponse, Response

from omnigent.harnesses.claude_native.goal_state import ClaudeGoal, ClaudeGoalStateReader

CLAUDE_GOAL_GET_EVENT = "claude_goal_get"

_logger = logging.getLogger(__name__)


def _unavailable() -> JSONResponse:
    return JSONResponse(status_code=200, content={"goal": None, "available": False})


class ClaudeGoalRunner:
    """
    Answer ``claude_goal_get`` events from each session's Claude transcript.

    :param bridge_id_for_session: Resolves a session's claude-native bridge id,
        e.g. ``"bridge_abc"``, falling back to the session id when the session
        has no bridge label or its labels could not be read. Only a labelled id
        is cached, so a failed lookup is retried on the next read.
    """

    def __init__(self, *, bridge_id_for_session: Callable[[str], Awaitable[str]]) -> None:
        self._bridge_id_for_session = bridge_id_for_session
        self._bridge_ids: dict[str, str] = {}
        self._readers: dict[str, ClaudeGoalStateReader] = {}
        self._readers_lock = threading.Lock()

    def forget(self, conv_id: str) -> None:
        """Drop the cached bridge id and transcript reader for a released session."""
        self._bridge_ids.pop(conv_id, None)
        self._readers.pop(conv_id, None)

    def _read(self, conv_id: str, bridge_id: str) -> tuple[bool, ClaudeGoal | None]:
        """Return ``(available, goal)``; unavailable until a transcript is readable."""
        from omnigent.harnesses.claude_native.bridge import (
            bridge_dir_for_bridge_id,
            read_transcript_path,
        )

        transcript_path = read_transcript_path(bridge_dir_for_bridge_id(bridge_id))
        if transcript_path is None:
            return False, None
        with self._readers_lock:
            reader = self._readers.get(conv_id)
            if reader is None or reader.transcript_path != transcript_path:
                reader = self._readers[conv_id] = ClaudeGoalStateReader(transcript_path)
        try:
            return True, reader.read()
        except OSError:
            _logger.debug("claude goal transcript unreadable for session=%s", conv_id)
            return False, None

    async def handle_event(
        self,
        conv_id: str,
        body_type: object,
        *,
        session_harness_name: Callable[[str], str | None],
    ) -> Response | None:
        """
        Handle a ``claude_goal_get`` event.

        :param conv_id: Session id, e.g. ``"conv_abc123"``.
        :param body_type: The event's ``type``, e.g. ``"claude_goal_get"``.
        :param session_harness_name: Resolves the harness this runner loaded
            for a session, or ``None`` before it loads one.
        :returns: ``{"goal": …, "available": …}``, or ``None`` for other events.
        """
        if body_type != CLAUDE_GOAL_GET_EVENT:
            return None
        if session_harness_name(conv_id) not in (None, "claude-native"):
            return _unavailable()
        bridge_id = self._bridge_ids.get(conv_id)
        if bridge_id is None:
            try:
                bridge_id = await self._bridge_id_for_session(conv_id)
            except (httpx.HTTPError, RuntimeError, ValueError):
                _logger.debug("claude goal bridge lookup failed for session=%s", conv_id)
                return _unavailable()
            if bridge_id != conv_id:
                self._bridge_ids[conv_id] = bridge_id
        available, goal = await asyncio.to_thread(self._read, conv_id, bridge_id)
        if not available:
            return _unavailable()
        wire = goal.to_wire() if goal is not None else None
        return JSONResponse(status_code=200, content={"goal": wire, "available": True})
