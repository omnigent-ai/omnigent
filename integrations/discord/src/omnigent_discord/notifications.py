"""All the bot's outbound Discord messages in one place.

Two Discord facts shape this module:

- **Shortcodes are not emoji.** Discord's API sends message content verbatim,
  so ``:white_check_mark:`` would appear literally. Every glyph here is a real
  Unicode character.
- **There is no ephemeral plain message.** Only an interaction response can be
  ephemeral, and a deflection notice has no interaction behind it. "Privately"
  therefore means a DM, falling back to a self-deleting channel message when
  the user's DMs are closed.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from omnigent_bot_core.events import OutputFile

from omnigent_discord.models import ChannelKey
from omnigent_discord.text import (
    GENERIC_FAILURE_TEXT,
    MESSAGE_CHAR_LIMIT,
    split_for_messages,
    truncate_for_message,
)

if TYPE_CHECKING:
    from omnigent_discord.streaming import MessageableProtocol, MessageProtocol


def mention_only(user_id: str) -> Any:
    """An allowed-mentions policy that pings exactly ``user_id`` and nothing else.

    Built lazily so this module stays importable without ``discord`` (the test
    suite drives it with recording fakes). Returns ``None`` — the client's
    deny-all default — when the library is absent or ``user_id`` isn't a
    snowflake. Degrading to "no ping" is right: the notice still reaches the
    channel, just without highlighting anyone, whereas raising here would take
    the whole notice down with it.
    """
    try:
        import discord

        return discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(id=int(user_id))]
        )
    except (ImportError, ValueError, TypeError):
        return None


# Resolves a Discord user id to their DM channel, or ``None`` when the bot
# can't open one (DMs closed, user gone).
DMResolver = Callable[[str], Awaitable["MessageableProtocol | None"]]

# How long a fallback in-channel private notice stays up before Discord deletes
# it. Long enough to read, short enough not to clutter a busy channel.
_NOTICE_TTL_SECONDS = 45.0

# Status → checkbox glyph for the rendered todo list.
_TODO_MARK = {
    "completed": "✅",
    "in_progress": "⏳",
    "pending": "⬜",
}


def format_todos(todos: list[dict[str, Any]]) -> str | None:
    """Render a todo-list update as a Discord message, or ``None`` if empty.

    Uses ``activeForm`` (the gerund) for the in-progress item and ``content``
    otherwise, mirroring how Claude Code presents its own list.
    """
    lines: list[str] = []
    for todo in todos:
        status = str(todo.get("status") or "pending")
        mark = _TODO_MARK.get(status, "⬜")
        if status == "in_progress":
            label = todo.get("activeForm") or todo.get("content") or ""
        else:
            label = todo.get("content") or todo.get("activeForm") or ""
        label = str(label).strip()
        if not label:
            continue
        lines.append(f"{mark} {label}")
    if not lines:
        return None
    return truncate_for_message("**Plan**\n" + "\n".join(lines))


def format_output_file(file: OutputFile) -> str:
    """Render a produced-file notice."""
    name = file.filename or file.file_id
    return f"📄 Produced a file: **{name}**"


def format_policy_denied(reason: str) -> str:
    """Render a policy-DENY notice (the block-without-asking counterpart)."""
    return f"⛔ Blocked by policy: {truncate_for_message(reason, limit=1500)}"


class DiscordNotifier:
    """Thin wrappers over the Discord send/edit surface.

    The channel is passed per call (it is per-turn, not fixed); the notifier
    holds only the logger, the server URL, and the DM resolver. Best-effort
    throughout — a failed side-channel post must never abort turn handling.
    """

    def __init__(
        self,
        *,
        server_url: str,
        logger: logging.Logger,
        dm_resolver: DMResolver | None = None,
    ) -> None:
        self._server_url = server_url
        self._logger = logger
        self._dm_resolver = dm_resolver

    async def post_reply(self, channel: MessageableProtocol, text: str) -> None:
        """Send ``text``, splitting it across messages if it exceeds the cap.

        The channel object is the address here, so unlike the Slack sibling
        there is no key to route by.
        """
        for chunk in split_for_messages(text, MESSAGE_CHAR_LIMIT):
            await channel.send(chunk)

    async def post_failure_reply(self, channel: MessageableProtocol) -> None:
        # Post the failure as its own message so the streamed answer stays
        # intact. A GENERIC message only — the raw error detail is logged
        # server-side and never echoed to the channel (it can carry stack traces
        # / internal paths, and the channel is visible to everyone in it).
        await channel.send(GENERIC_FAILURE_TEXT)

    async def post_session_info(
        self,
        channel: MessageableProtocol,
        key: ChannelKey,
        *,
        harness: str | None,
        agent_name: str | None,
        workspace: str | None,
        session_id: str,
    ) -> None:
        # Posted once when a session is created — the first durable message in
        # the channel, orienting the user to what they're talking to and linking
        # to the web UI. Best-effort: a failed post must not abort the turn.
        agent = agent_name or "agent"
        harness_note = f" ({harness})" if harness else ""
        lines = [f"🤖 **{agent}**{harness_note}"]
        if workspace:
            lines.append(f"📁 `{workspace}`")
        lines.append(f"🌐 [Open in Omnigent]({self.session_web_link(session_id)})")
        try:
            await channel.send("\n".join(lines))
        except Exception:
            self._logger.warning("Session-info post failed channel=%s; continuing", key.display())

    async def post_private(
        self,
        channel: MessageableProtocol,
        key: ChannelKey,
        user_id: str,
        text: str,
    ) -> None:
        """Tell one user something without addressing the whole channel.

        Prefers a DM. When the bot can't DM them (DMs closed to server members
        is common, and a fresh guild member often has them off), it falls back
        to a mention in the channel that Discord deletes shortly after — visible
        enough to be read, transient enough not to clutter the conversation.
        Best-effort: a failure on both paths is logged, never raised.
        """
        if self._dm_resolver is not None:
            try:
                dm = await self._dm_resolver(user_id)
                if dm is not None:
                    await dm.send(truncate_for_message(text))
                    return
            except Exception:
                self._logger.info("DM notice failed user=%s; falling back to channel", user_id)
        try:
            # The client denies every mention by default (agent-authored text
            # must not be able to ping a server), so this one deliberate ping
            # carries its own narrow allowance: this user, nobody else.
            await channel.send(
                truncate_for_message(f"<@{user_id}> {text}"),
                delete_after=_NOTICE_TTL_SECONDS,
                allowed_mentions=mention_only(user_id),
            )
        except Exception:
            self._logger.warning("Private notice failed channel=%s; continuing", key.display())

    async def post_or_update_todos(
        self,
        channel: MessageableProtocol,
        key: ChannelKey,
        todos: list[dict[str, Any]],
        todo_message: MessageProtocol | None,
    ) -> MessageProtocol | None:
        # Render the plan once and edit it in place on later updates so the
        # channel carries a single, current plan message rather than a pile of
        # snapshots. Best-effort throughout.
        text = format_todos(todos)
        if text is None:
            return todo_message
        try:
            if todo_message is None:
                return await channel.send(text)
            await todo_message.edit(content=text)
            return todo_message
        except Exception:
            self._logger.warning("Todo update failed channel=%s; continuing", key.display())
            return todo_message

    async def notify_non_owner(
        self, channel: MessageableProtocol, key: ChannelKey, user_id: str
    ) -> None:
        await self.post_private(
            channel,
            key,
            user_id,
            "This conversation's Omnigent session belongs to whoever started it, "
            "so I can't add your message to it. Mention me in the channel (or DM "
            "me) to get your own session.",
        )

    async def notify_busy(
        self,
        channel: MessageableProtocol,
        key: ChannelKey,
        user_id: str,
        *,
        needs_action: bool,
        session_id: str | None,
    ) -> None:
        """Tell the owner their message can't run because the server is busy.

        Mirrors the web UI's two "can't send now" states: (a) ``needs_action`` —
        the session is parked awaiting a decision, so the user must answer the
        pending request (here, or in the web UI); (b) otherwise the server is
        running/waiting, so wait for the reply or interrupt in the web UI. The
        message was NOT run and is NOT queued — a message to an idle session
        runs normally, so re-sending once it frees works.
        """
        link = self.session_web_link(session_id) if session_id else None
        if needs_action:
            text = (
                "⌛ I'm waiting on your response to the request above before I can "
                "continue. Answer it here"
            )
            text += f", or in the [web UI]({link})." if link else "."
        else:
            text = (
                "⌛ I'm still working on your previous message here — I handle one "
                "at a time, so send this again once I've replied"
            )
            text += f", or wait / interrupt in the [web UI]({link})." if link else "."
        await self.post_private(channel, key, user_id, text)

    def session_web_link(self, session_id: str) -> str:
        # Link to the session's conversation page in the Omnigent web UI, where
        # a user can continue a conversation that's mid-turn in Discord (the web
        # UI accepts concurrent input and shows any pending actions).
        #
        # A Databricks workspace-hosted server is configured by its API proxy
        # mount (``https://<ws>/api/2.0/omnigent``), but the web UI lives on the
        # workspace SPA mount (``https://<ws>/omnigent``) — linking to the API
        # mount answers JSON, not the UI. Map the path and keep any ``?o=<org>``
        # workspace selector from the configured URL so multi-workspace hosts
        # open in the right workspace. (This bot is standalone — it can't import
        # ``omnigent.server_url`` — so the mount mapping is mirrored here.)
        parts = urlsplit(self._server_url.rstrip("/"))
        if parts.path == "/api/2.0/omnigent":
            return urlunsplit(
                (parts.scheme, parts.netloc, f"/omnigent/c/{session_id}", parts.query, "")
            )
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return f"{base}/c/{session_id}"
