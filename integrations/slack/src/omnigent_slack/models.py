from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def event_is_dm(event: dict[str, object]) -> bool:
    """Whether a Slack event arrived via a 1:1 DM rather than a channel.

    Slack marks 1:1 DMs with ``channel_type == "im"``; their channel ids also
    start with ``"D"``. Used by the service's event routing to decide whether a
    plain message is actionable (DMs are; channel messages need an @-mention).
    """
    return event.get("channel_type") == "im" or str(event.get("channel") or "").startswith("D")


@dataclass(frozen=True, slots=True)
class ThreadKey:
    team_id: str
    channel_id: str
    # The session key: the thread's root message ts — the same in a channel and a
    # DM. One session per thread, so a new top-level message starts a new session
    # and a threaded reply reuses it. (A DM is treated exactly like a channel here,
    # NOT as one standing session per channel.)
    thread_ts: str

    @classmethod
    def from_event(cls, team_id: str, event: dict[str, object]) -> ThreadKey:
        channel_id = str(event["channel"])
        thread_ts = str(event.get("thread_ts") or event["ts"])
        return cls(team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)

    @property
    def is_dm(self) -> bool:
        """Whether this key is for a 1:1 DM, by the channel id's ``D`` prefix.

        Slack 1:1 DM channels start with ``D`` (channels start with ``C``). This
        is a pure function of the channel, independent of the thread — a DM still
        maps one session per thread, like a channel.
        """
        return self.channel_id.startswith("D")

    @property
    def reply_ts(self) -> str:
        """The ``thread_ts`` to post replies under — always a real message ts.

        Both channels and DMs key on the thread root ts now, so replies thread
        under it in either case (a top-level message's own ts starts a new thread).
        """
        return self.thread_ts

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
