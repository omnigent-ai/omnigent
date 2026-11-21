"""Slack agent sessions: the status and title a thread shows in Slack's agent UI.

An app declared an agent (``features.agent_view``) gets one Slack session per
thread it answers in; ``chat.startStream`` creates it and marks it
``processing``. With ``OMNIGENT_SLACK_AGENT_SESSIONS`` on, the bot sets the rest
through ``agents.sessions.setStatus``: a title and the initiator when a turn
starts, ``suspended`` while a card waits for an answer, and ``active`` when the
turn ends. Every call is best-effort: a refusal (``feature_disabled`` in a
workspace without agent sessions, a missing scope) is logged and never fails a
turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from slack_sdk.errors import SlackApiError

from omnigent_slack.models import ThreadKey
from omnigent_slack.streaming import SlackClientProtocol
from omnigent_slack.text import normalize_whitespace

SessionStatus = Literal["processing", "active", "suspended"]

# Slack accepts 200 characters; a shorter title reads better in the session list.
_TITLE_CHAR_LIMIT = 80

# A status call must not hold up a turn, nor a shutdown waiting on one.
_CALL_TIMEOUT_SECONDS = 5.0

# Slack markup in message text (``<@U123>``, ``<#C123|general>``,
# ``<https://example.com|label>``), shown as its label, else its target.
_MARKUP_RE = re.compile(r"<([^<>|]*)(?:\|([^<>]*))?>")


def _markup_label(match: re.Match[str]) -> str:
    # A person's or a special mention with no label (``<@U123>``, ``<!here>``) is
    # only an id: Slack's own titles resolve it to a name, which the bot cannot
    # without ``users:read``, so it is left out rather than shown as an id.
    target, label = match.group(1), match.group(2)
    if label:
        return label
    return "" if target.startswith(("@", "!")) else target


def session_title(text: str) -> str:
    """A short, plain session title cut from the message that starts the session."""
    plain = normalize_whitespace(_MARKUP_RE.sub(_markup_label, text))
    if len(plain) <= _TITLE_CHAR_LIMIT:
        return plain
    return plain[: _TITLE_CHAR_LIMIT - 1].rstrip() + "…"


class AgentSessions:
    """Sets a thread's Slack agent-session status when the feature is on.

    Off (the default), every call is a no-op, so a deployment whose app is a
    plain bot makes no session calls at all.
    """

    def __init__(self, *, enabled: bool, logger: logging.Logger) -> None:
        self._enabled = enabled
        self._logger = logger

    @property
    def stream_seal_status(self) -> SessionStatus | None:
        """The status a stream stopped mid-turn leaves, or ``None`` when off.

        ``chat.stopStream`` marks the session ``active`` by default, which would
        read as finished while the turn goes on.
        """
        return "processing" if self._enabled else None

    async def set_status(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        status: SessionStatus,
        *,
        title: str = "",
        initiator_user_id: str | None = None,
    ) -> None:
        """Set the thread's session status, creating the session if needed.

        Slack applies ``title`` and ``initiator_user_id`` only when this call
        creates the session. A failure is logged, never raised.
        """
        if not self._enabled:
            return
        body = {"channel_id": key.channel_id, "thread_ts": key.reply_ts, "status": status}
        if title:
            body["title"] = title
        if initiator_user_id:
            body["initiator_user_id"] = initiator_user_id
        try:
            # ``api_call``: slack-sdk has the typed method only from 3.44.0.
            await asyncio.wait_for(
                client.api_call("agents.sessions.setStatus", json=body), _CALL_TIMEOUT_SECONDS
            )
        except Exception as exc:
            self._logger.warning(
                "Agent session status=%s failed thread=%s error=%s; continuing",
                status,
                key.display(),
                _slack_error(exc),
            )


def _slack_error(exc: Exception) -> str:
    # Slack's error code (e.g. ``feature_disabled``), else the exception itself.
    if isinstance(exc, SlackApiError):
        error = getattr(exc.response, "get", lambda _k: None)("error")
        if isinstance(error, str):
            return error
    return repr(exc)
