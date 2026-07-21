from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def event_is_dm(event: dict[str, object]) -> bool:
    """Whether a Slack event arrived via a 1:1 DM rather than a channel.

    Slack marks 1:1 DMs with ``channel_type == "im"``; their channel ids also
    start with ``"D"``. Single source of truth for DM detection, used by both
    :meth:`ThreadKey.from_event` and the service's event routing.
    """
    return event.get("channel_type") == "im" or str(event.get("channel") or "").startswith("D")


@dataclass(frozen=True, slots=True)
class ThreadKey:
    team_id: str
    channel_id: str
    # The session key. In a CHANNEL this is the thread's root message ts (one
    # session per thread). In a DM it is the CHANNEL id itself, so every message
    # in that 1:1 DM — threaded or not — maps to the SAME session, matching "a DM
    # is one ongoing conversation". (A bare top-level DM otherwise keys on its own
    # unique ts and would spawn a new session per message.)
    thread_ts: str

    @classmethod
    def from_event(cls, team_id: str, event: dict[str, object]) -> ThreadKey:
        channel_id = str(event["channel"])
        if event_is_dm(event):
            return cls(team_id=team_id, channel_id=channel_id, thread_ts=channel_id)
        thread_ts = str(event.get("thread_ts") or event["ts"])
        return cls(team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)

    @property
    def is_dm(self) -> bool:
        """Whether this key is for a 1:1 DM (keyed on the channel, not a ts)."""
        return self.thread_ts == self.channel_id

    @property
    def reply_ts(self) -> str | None:
        """The ``thread_ts`` to post replies under, or ``None`` for a DM.

        In a channel, ``thread_ts`` is a real message ts and replies thread under
        it. In a DM the session is keyed on the channel itself (``thread_ts`` ==
        ``channel_id``, not a valid message ts), and the whole DM channel is the
        conversation — so replies post top-level (no parent to thread under).
        """
        return None if self.is_dm else self.thread_ts

    def display(self) -> str:
        return f"{self.team_id}:{self.channel_id}:{self.thread_ts}"


@dataclass(frozen=True, slots=True)
class UserConfig:
    """A Slack user's chosen agent, host, and workspace.

    The Omnigent server is operator-fixed (``OMNIGENT_SERVER_URL``), so it
    is not part of a user's config.
    """

    agent_id: str
    agent_name: str
    workspace: str
    host_id: str | None = None
    host_name: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A Slack thread's Omnigent session and where it runs."""

    session_id: str
    owner_user_id: str | None
    host_id: str | None
    workspace: str | None


@dataclass(frozen=True, slots=True)
class SlackTurn:
    key: ThreadKey
    text: str
    user_id: str
    create_if_missing: bool
    title: str
    slack_client: Any
    agent_id: str
    owner_user_id: str
    workspace: str | None = None
    host_id: str | None = None
