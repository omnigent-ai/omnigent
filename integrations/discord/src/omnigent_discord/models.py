"""Value types the Discord bot routes on.

Discord's identifiers are snowflakes that are globally unique, which
simplifies the Slack analogues these mirror: a channel id alone identifies a
conversation (no workspace prefix), and a user id alone identifies a person
across every guild and DM. Ids are carried as ``str`` rather than ``int``
throughout — they are opaque keys, they are what SQLite stores, and Discord's
own JSON transports them as strings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omnigent_bot_core.events import HostType


@dataclass(frozen=True, slots=True)
class ChannelKey:
    """The conversation a session is bound to: one Discord channel.

    ``channel_id`` is a thread id in a guild (the bot always runs a session in
    a thread it created or was mentioned in) or a DM channel id in a direct
    message. Either way it is globally unique, so it alone is the session key.
    ``guild_id`` is carried for logging and the guild allow-list; it is
    ``None`` in a DM.
    """

    channel_id: str
    guild_id: str | None = None

    @property
    def is_dm(self) -> bool:
        """Whether this conversation is a 1:1 DM (no guild)."""
        return self.guild_id is None

    def display(self) -> str:
        return f"{self.guild_id or 'dm'}:{self.channel_id}"


@dataclass(frozen=True, slots=True)
class UserConfig:
    """A Discord user's chosen agent, host, and workspace.

    The Omnigent server is operator-fixed (``OMNIGENT_SERVER_URL``), so it is
    not part of a user's config. Stored per Discord user id — the snowflake is
    global, so one setup covers every guild and DM the bot shares with them.

    ``host_type`` picks between the two ways a session gets a host. It is
    ``"managed"`` when the user chose a server-provisioned sandbox, and then
    ``host_id`` and ``workspace`` are empty — the server chooses both.
    """

    agent_id: str
    agent_name: str
    workspace: str
    host_id: str | None = None
    host_name: str | None = None
    host_type: HostType = "external"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A Discord channel's Omnigent session and where it runs.

    ``host_type`` is recorded per session, not just per user: it decides whether
    a later turn in this channel may launch a runner, and it must survive both a
    bot restart and the user changing their setup mid-conversation.
    """

    session_id: str
    owner_user_id: str | None
    host_id: str | None
    workspace: str | None
    host_type: HostType = "external"


@dataclass(frozen=True, slots=True)
class DiscordTurn:
    """One user message being run against a session.

    ``channel`` is the messageable the reply is written into (a
    ``discord.Thread`` or ``discord.DMChannel`` in production, a fake in
    tests) — see :class:`omnigent_discord.streaming.MessageableProtocol`.
    """

    key: ChannelKey
    text: str
    user_id: str
    create_if_missing: bool
    title: str
    channel: Any
    agent_id: str
    owner_user_id: str
    workspace: str | None = None
    host_id: str | None = None
    host_type: HostType = "external"
    # Content blocks for any attached files. Empty for a text-only turn, which
    # is every turn when uploads are off.
    blocks: list[dict[str, Any]] = field(default_factory=list)
