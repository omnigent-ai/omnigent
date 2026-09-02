from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnigent_bot_core.omnigent import (
    AuthRequiredError,
    HarnessNotConfiguredError,
    HostUnavailableError,
    OmnigentClient,
    OmnigentClientPool,
    ServerUnreachableError,
    StreamInterruptedError,
    extract_assistant_text,
    extract_delta,
    extract_elicitation_request,
    extract_elicitation_resolved,
    extract_error_text,
    extract_output_file,
    extract_policy_denied,
    extract_todos,
)

from omnigent_discord.approvals import (
    DEFAULT_ELICITATION_TIMEOUT_SECONDS,
    ElicitationCoordinator,
)
from omnigent_discord.elicitation import ElicitationController, ElicitationTurnState
from omnigent_discord.models import ChannelKey, DiscordTurn
from omnigent_discord.notifications import (
    DiscordNotifier,
    format_output_file,
    format_policy_denied,
    format_todos,
)
from omnigent_discord.setup import (
    host_unavailable_text,
    relogin_required_text,
    setup_required_text,
)
from omnigent_discord.store import SQLiteStore
from omnigent_discord.streaming import _AnswerReply
from omnigent_discord.text import GENERIC_FAILURE_TEXT, strip_bot_mention

if TYPE_CHECKING:
    from omnigent_discord.streaming import MessageableProtocol, MessageProtocol

# Immediate acknowledgement shown while the session spins up and while the agent
# works before the first streamed tokens arrive. The reply is streamed INTO this
# message, so it is replaced by real content rather than deleted — the channel
# never shows a gap between the placeholder vanishing and the answer appearing.
_ACK_TEXT = "_Working on it…_"

# How long the read loop waits for the next stream event before treating the
# stream as idle and force-flushing any accumulated-but-unwritten answer text.
# Message edits are throttled to a cadence, so a short burst the agent then
# pauses after (a tool call, thinking) can stay invisible until more text
# arrives or the turn ends. This is a SENSITIVITY window, not a display delay:
# during active streaming the cadence still writes on schedule; this only fires
# once the stream actually goes quiet.
_IDLE_FLUSH_SECONDS = 2.0

# Discord caps a thread name at 100 characters.
_THREAD_NAME_LIMIT = 100
# Auto-archive a session thread after a day of silence (Discord accepts 60,
# 1440, 4320, or 10080 minutes). A day keeps an active investigation open
# without leaving dead threads in the channel list forever.
_THREAD_AUTO_ARCHIVE_MINUTES = 1440

_SERVER_UNREACHABLE_TEXT = (
    "⚠️ I couldn't reach your Omnigent server. If it moved or is down, run "
    "`/omnigent config` to reconfigure."
)

# Shown when the live turn stream kept dropping and reconnect was exhausted. The
# server was reachable throughout (a proxy severed the long-lived stream, e.g. a
# ~5-minute duration cap), and the turn may still be running server-side — so
# this is NOT the "server is down / reconfigure" case.
_STREAM_INTERRUPTED_TEXT = (
    "⚠️ I lost my live connection to the running turn. Its result may still "
    "arrive here — send another message if it doesn't."
)

_NO_THREAD_PERMISSION_TEXT = (
    "⚠️ I need the **Create Public Threads** permission in this channel to start "
    "a session. Ask a moderator to grant it, or DM me instead."
)


class _TurnAborted(Exception):
    """A turn can't proceed; ``text`` is the public user-facing reason."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _AuthExpired(Exception):
    """The server rejected the turn as unauthenticated (expired/lost token).

    Distinct from :class:`_TurnAborted` because it is not delivered as a channel
    message: the caller privately tells the user to run ``/omnigent config``
    instead, which is the only way to re-drive the login flow.
    """


@dataclass
class _StreamState:
    """Mutable per-turn state threaded through the stream event dispatch."""

    # The live plan/todo message, edited in place across updates.
    todo_message: MessageProtocol | None = None
    # Whether the turn failed. The user-visible signal; a failure's raw detail
    # is logged at the point of failure (never stored — it can carry stack
    # traces / internal paths), and the user only ever sees the generic message.
    errored: bool = False
    # Set when a known error was delivered mid-stream and the turn should stop.
    aborted: bool = False
    # In-flight elicitation cards this turn (owned by the ElicitationController).
    elicitations: ElicitationTurnState = field(default_factory=ElicitationTurnState)


def _classify_turn_error(exc: BaseException, server_url: str) -> str | None:
    """Map a known startup/turn error to its public user-facing text.

    Single source of truth shared by the session-creation and mid-turn error
    paths, so the text can't drift. All these errors affect everyone in the
    conversation and are delivered publicly. Returns ``None`` for an
    unrecognized error (the caller falls back to the generic failure). Auth
    errors do NOT flow through here — the caller intercepts them for a private
    re-login prompt.
    """
    if isinstance(exc, StreamInterruptedError):
        # A mid-stream drop with reconnect exhausted — the server stayed
        # reachable, so this is NOT the "reconfigure" case.
        return _STREAM_INTERRUPTED_TEXT
    if isinstance(exc, ServerUnreachableError):
        return _SERVER_UNREACHABLE_TEXT
    if isinstance(exc, HostUnavailableError):
        return host_unavailable_text(server_url)
    if isinstance(exc, HarnessNotConfiguredError):
        # The server's message is curated, actionable guidance for this code —
        # surface it so the user knows to run `omnigent setup` on the host.
        return f"⚠️ {exc}"
    return None


def thread_name_for(text: str) -> str:
    """A thread title derived from the user's first message.

    Discord shows this in the channel's thread list, so it should read like a
    topic. Capped at Discord's 100-character limit.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return "Omnigent session"
    if len(cleaned) <= _THREAD_NAME_LIMIT:
        return cleaned
    return cleaned[: _THREAD_NAME_LIMIT - 1] + "…"


class DiscordOmnigentService:
    """Event acceptance, turn routing, and turn lifecycle.

    Delegates streaming to ``streaming``, elicitation to ``elicitation``, and
    outbound messages to ``notifications``, so this class stays about *which*
    messages become turns and *how* a turn is run.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        pool: OmnigentClientPool,
        notifier: DiscordNotifier,
        server_url: str,
        bot_user_id: str | None = None,
        guild_allowed: Callable[[str | None], bool] | None = None,
        elicitations: ElicitationCoordinator | None = None,
        elicitation_timeout_seconds: float = DEFAULT_ELICITATION_TIMEOUT_SECONDS,
        stream_edit_interval_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._pool = pool
        # The one operator-configured Omnigent server. Always the routing
        # target — any server_url persisted on an older config/session row is
        # ignored, so a config change points every conversation at the new server.
        self._server_url = server_url
        self._bot_user_id = bot_user_id
        self._guild_allowed = guild_allowed or (lambda _guild_id: True)
        self._stream_edit_interval = stream_edit_interval_seconds
        self._logger = logging.getLogger(__name__)
        self._notifier = notifier
        # Bridges an in-flight elicitation card to the interaction that answers
        # it (and to the pushed elicitation_resolved).
        self._elicitations = elicitations or ElicitationCoordinator(elicitation_timeout_seconds)
        # Owns all elicitation-card orchestration during a turn (post, resolver
        # task, finalize) — keeps this class to routing + turn lifecycle.
        self._elicitation = ElicitationController(
            self._elicitations,
            server_url=server_url,
            post_reply=self._notifier.post_reply,
            logger=self._logger,
            timeout_seconds=elicitation_timeout_seconds,
        )
        # Channels with a turn actively streaming IN THIS PROCESS. Each turn
        # opens its own SSE stream; two at once would render the same events
        # into Discord twice. This is a LOCAL concurrency guard (reserved
        # synchronously, before any await, so two racing messages can't both
        # pass) — necessary because the server-activity check alone races:
        # claude-native flips to `idle` between streaming bursts, so a snapshot
        # mid-turn can read "not busy" while a local stream is still live. The
        # server-activity check (see _route_turn) is the SEPARATE cross-surface
        # signal (web-UI busy / pending action).
        self._active_channels: set[ChannelKey] = set()
        # In-flight turn tasks, tracked so shutdown can cancel them — and keyed
        # by channel so the owner can cancel their own stuck turn. The idle
        # grace eventually ends a wedged turn on its own, but that is ten
        # minutes of a channel the owner cannot use.
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._turns_by_channel: dict[ChannelKey, asyncio.Task[None]] = {}

    def set_bot_user_id(self, bot_user_id: str) -> None:
        """Record the bot's own id once the gateway reports it (on ready)."""
        self._bot_user_id = bot_user_id

    async def shutdown(self) -> None:
        tasks = list(self._turn_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Cancel any elicitation resolver tasks still awaiting an answer so they
        # aren't orphaned ("Task was destroyed but it is pending").
        await self._elicitation.shutdown()

    # ── event acceptance ──────────────────────────────────────────────────

    async def handle_message(self, message: Any) -> None:
        """Entry point for every gateway message the bot can see.

        Decides whether the message is addressed to the bot at all, then routes
        it to a session. Discord delivers ordinary channel chatter to the bot,
        so most messages end here.
        """
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        guild = getattr(message, "guild", None)
        guild_id = str(guild.id) if guild is not None else None
        if not self._guild_allowed(guild_id):
            self._logger.debug("Ignoring message from non-allowed guild=%s", guild_id)
            return

        channel = message.channel
        requester = str(author.id)
        raw_text = str(getattr(message, "content", "") or "")
        mentioned = self._is_mentioned(message)
        is_dm = guild is None

        if is_dm:
            # A DM is a private 1:1 conversation, so every message is for the
            # bot. Discord DMs have no threads, so the DM channel itself is the
            # session key — ``/omnigent new`` starts a fresh one.
            key = ChannelKey(channel_id=str(channel.id), guild_id=None)
            await self._accept(message, key, channel, requester, raw_text)
            return

        if self._is_thread(channel):
            key = ChannelKey(channel_id=str(channel.id), guild_id=guild_id)
            record = await self._store.get_session(key)
            if record is not None:
                # Inside a session thread the bot is the point of the thread, so
                # the owner's plain messages continue the conversation without a
                # mention. Anyone else is chatting alongside — stay quiet unless
                # they explicitly @-mention the bot, which earns an explanation.
                if record.owner_user_id != requester:
                    if mentioned:
                        await self._notifier.notify_non_owner(channel, key, requester)
                    return
                await self._accept(message, key, channel, requester, raw_text)
                return
            # A thread with no session: treat a mention exactly like one in a
            # channel, but run in this thread rather than creating a nested one.
            if not mentioned:
                return
            if not await self._thread_starter_matches(channel, requester):
                await self._notifier.notify_non_owner(channel, key, requester)
                return
            await self._accept(message, key, channel, requester, raw_text)
            return

        # A regular guild channel: the bot joins only when @-mentioned, and it
        # always moves the conversation into its own thread so the channel isn't
        # taken over by a streaming answer.
        if not mentioned:
            return
        text = strip_bot_mention(raw_text, self._bot_user_id)
        if not text:
            await channel.send(
                "Mention me with a message to start a session — for example "
                "`@Omnigent help me inspect this failure`."
            )
            return
        if not await self._store.claim_event(str(message.id)):
            self._logger.info("Ignoring duplicate Discord message id=%s", message.id)
            return
        # Check setup BEFORE creating the thread: an unconfigured user would
        # otherwise be left with an empty thread in the channel that no session
        # will ever use.
        if await self._store.get_user_config(requester) is None:
            self._logger.info(
                "Unconfigured user channel=%s user=%s; prompting setup",
                channel.id,
                requester,
            )
            await self._notifier.post_private(
                channel,
                ChannelKey(channel_id=str(channel.id), guild_id=guild_id),
                requester,
                setup_required_text(),
            )
            return
        thread = await self._open_thread(message, text)
        if thread is None:
            await self._store.unclaim_event(str(message.id))
            return
        key = ChannelKey(channel_id=str(thread.id), guild_id=guild_id)
        try:
            await self._route_turn(
                key=key,
                channel=thread,
                text=text,
                requester=requester,
                title=self._session_title(message),
            )
        except Exception:
            await self._store.unclaim_event(str(message.id))
            raise

    async def _accept(
        self,
        message: Any,
        key: ChannelKey,
        channel: MessageableProtocol,
        requester: str,
        raw_text: str,
    ) -> None:
        """De-duplicate, clean the text, and route an in-conversation message."""
        if not await self._store.claim_event(str(message.id)):
            self._logger.info("Ignoring duplicate Discord message id=%s", message.id)
            return
        # A mention may still be present (a DM, or the owner @-ing the bot in
        # its own thread) — strip it and treat the rest as the prompt.
        text = strip_bot_mention(raw_text, self._bot_user_id)
        if not text:
            self._logger.info("Ignoring empty Discord message channel=%s", key.display())
            await self._store.unclaim_event(str(message.id))
            return
        self._logger.info("Accepted Discord message channel=%s chars=%s", key.display(), len(text))
        try:
            await self._route_turn(
                key=key,
                channel=channel,
                text=text,
                requester=requester,
                title=self._session_title(message),
            )
        except Exception:
            # Handling failed before the turn was underway; release the claim so
            # a re-send isn't silently deduped away.
            await self._store.unclaim_event(str(message.id))
            raise

    def _is_mentioned(self, message: Any) -> bool:
        """Whether this message addresses the bot.

        Two forms count, and only two. A direct user mention (``<@id>``) is the
        obvious one. Discord also auto-creates a **managed role** named after
        each bot, and its autocomplete offers that role alongside the user — so
        someone typing ``@YourBot`` frequently picks the role (``<@&id>``) and
        would otherwise get silence.

        A role mention counts ONLY when it is the bot's own managed integration
        role, whose sole member is the bot. ``@everyone``/``@here`` never reach
        either list, and an ordinary role the bot happens to hold is not an
        address to the bot, so neither triggers a turn.
        """
        if self._bot_user_id is None:
            return False
        mentions = getattr(message, "mentions", None) or []
        if any(str(getattr(user, "id", "")) == self._bot_user_id for user in mentions):
            return True
        return any(
            self._is_own_managed_role(role)
            for role in (getattr(message, "role_mentions", None) or [])
        )

    def _is_own_managed_role(self, role: Any) -> bool:
        """Whether ``role`` is the integration role Discord created for this bot.

        Identified by the role's tags naming this bot, which is what
        distinguishes it from any other role the bot may have been given.
        """
        if not getattr(role, "managed", False):
            return False
        tags = getattr(role, "tags", None)
        bot_id = getattr(tags, "bot_id", None) if tags is not None else None
        return bot_id is not None and str(bot_id) == self._bot_user_id

    @staticmethod
    def _is_thread(channel: Any) -> bool:
        """Whether a channel is a thread (it exposes a ``parent_id``)."""
        return getattr(channel, "parent_id", None) is not None

    async def _thread_starter_matches(self, thread: Any, requester: str) -> bool:
        """Whether ``requester`` owns the thread, for a thread with no session record.

        Reached when the record is gone (a logout, a cleared database) but the
        thread lives on. Discord still knows who created it, so a stranger's
        mention must not be able to adopt someone else's thread — and, worse,
        take ownership of it, locking the original owner out of their own
        conversation.

        ``Thread.owner_id`` comes off the gateway payload, so the common case
        needs no API call and cannot fail. The starter-message fetch is only a
        fallback for a thread object that lacks it, and every uncertain path
        here answers **False**: refusing costs the requester a new thread, while
        granting hands away someone else's.
        """
        owner_id = getattr(thread, "owner_id", None)
        if owner_id is not None:
            return str(owner_id) == requester

        starter = getattr(thread, "starter_message", None)
        if starter is None:
            fetch = getattr(thread, "fetch_message", None)
            starter_id = getattr(thread, "id", None)
            if fetch is None or starter_id is None:
                return False
            try:
                starter = await fetch(starter_id)
            except Exception:
                self._logger.info(
                    "Could not read thread starter for %s; refusing adoption", thread
                )
                return False
        author = getattr(starter, "author", None)
        author_id = getattr(author, "id", None)
        if author_id is None:
            return False
        return str(author_id) == requester

    async def _open_thread(self, message: Any, text: str) -> Any | None:
        """Create the session thread hanging off a channel mention.

        Returns ``None`` (after telling the channel why) when the bot lacks the
        thread permission — the one failure a moderator can fix.
        """
        try:
            return await message.create_thread(
                name=thread_name_for(text),
                auto_archive_duration=_THREAD_AUTO_ARCHIVE_MINUTES,
            )
        except Exception as exc:
            self._logger.info(
                "Could not create session thread channel=%s: %s", message.channel.id, exc
            )
            with contextlib.suppress(Exception):
                await message.channel.send(_NO_THREAD_PERMISSION_TEXT)
            return None

    @staticmethod
    def _session_title(message: Any) -> str:
        """The Omnigent session title: ``Discord: <message permalink>``.

        ``jump_url`` is a real clickable Discord permalink, so the web UI's
        session list points back at the originating conversation.
        """
        jump_url = getattr(message, "jump_url", None)
        if isinstance(jump_url, str) and jump_url:
            return f"Discord: {jump_url}"
        return f"Discord message {getattr(message, 'id', 'unknown')}"

    # ── turn routing ──────────────────────────────────────────────────────

    async def _route_turn(
        self,
        *,
        key: ChannelKey,
        channel: MessageableProtocol,
        text: str,
        requester: str,
        title: str,
    ) -> None:
        # LOCAL concurrency guard: reserve the channel SYNCHRONOUSLY here (no
        # await before this add) so two near-simultaneous messages can't both
        # open a stream and double-render. If already reserved, a turn is
        # streaming in this process → deflect. The reservation is held until
        # either a spawned turn's finally releases it, or we release it below on
        # any path that does NOT spawn.
        if key in self._active_channels:
            self._logger.info(
                "Channel already streaming in-process channel=%s; deflecting", key.display()
            )
            record = await self._store.get_session(key)
            if record is not None and record.owner_user_id != requester:
                await self._notifier.notify_non_owner(channel, key, requester)
                return
            # A parked turn (awaiting an approval/question) is STILL streaming,
            # so it holds this reservation — meaning this branch, not the
            # server-activity one below, handles a new message during a pending
            # elicitation. Ask the server whether the session needs user action
            # so we say "respond to the pending request above" rather than the
            # generic "still working" notice.
            needs_action = False
            if record is not None:
                omnigent = await self._pool.get(self._server_url, requester)
                activity = await omnigent.get_session_activity(record.session_id)
                needs_action = activity.needs_user_action
            await self._notifier.notify_busy(
                channel,
                key,
                requester,
                needs_action=needs_action,
                session_id=record.session_id if record is not None else None,
            )
            return

        self._active_channels.add(key)
        spawned = False
        try:
            record = await self._store.get_session(key)

            if record is not None:
                # An existing conversation belongs to whoever started it. A
                # follow-up from a different user is not added to the session.
                # A record with no stored owner is treated as locked (fail
                # closed): only match when the owner is known AND == requester.
                if record.owner_user_id != requester:
                    self._logger.info(
                        "Ignoring follow-up from non-owner channel=%s owner=%s requester=%s",
                        key.display(),
                        record.owner_user_id,
                        requester,
                    )
                    await self._notifier.notify_non_owner(channel, key, requester)
                    return
                # Cross-surface check: the SERVER decides busy/awaiting-action
                # (the web UI or another client may be driving the session),
                # mirroring the web UI's send gate. The local guard above already
                # prevents a concurrent Discord stream; this catches activity
                # elsewhere.
                omnigent = await self._pool.get(self._server_url, requester)
                activity = await omnigent.get_session_activity(record.session_id)
                if activity.needs_user_action or activity.is_busy:
                    self._logger.info(
                        "Server busy channel=%s status=%s pending=%s; deflecting",
                        key.display(),
                        activity.status,
                        activity.pending_elicitation,
                    )
                    await self._notifier.notify_busy(
                        channel,
                        key,
                        requester,
                        needs_action=activity.needs_user_action,
                        session_id=record.session_id,
                    )
                    return
                self._spawn_turn(
                    DiscordTurn(
                        key=key,
                        text=text,
                        user_id=requester,
                        create_if_missing=False,
                        # Title is only used when creating a session; an existing
                        # conversation already has one.
                        title="",
                        channel=channel,
                        agent_id="",
                        owner_user_id=record.owner_user_id or requester,
                        workspace=record.workspace,
                        host_id=record.host_id,
                    )
                )
                spawned = True
                return

            config = await self._store.get_user_config(requester)
            if config is None:
                self._logger.info(
                    "Unconfigured user channel=%s user=%s; prompting setup",
                    key.display(),
                    requester,
                )
                await self._notifier.post_private(channel, key, requester, setup_required_text())
                return

            self._spawn_turn(
                DiscordTurn(
                    key=key,
                    text=text,
                    user_id=requester,
                    create_if_missing=True,
                    title=title,
                    channel=channel,
                    agent_id=config.agent_id,
                    owner_user_id=requester,
                    workspace=config.workspace,
                    host_id=config.host_id,
                )
            )
            spawned = True
        finally:
            # Release the reservation unless a turn was spawned — the spawned
            # turn's ``_run_turn_tracked`` finally owns the release from here on.
            if not spawned:
                self._active_channels.discard(key)

    def _spawn_turn(self, turn: DiscordTurn) -> None:
        """Run a reserved turn as a background task, tracked for shutdown."""
        task = asyncio.create_task(self._run_turn_tracked(turn))
        self._turn_tasks.add(task)
        self._turns_by_channel[turn.key] = task
        task.add_done_callback(self._turn_tasks.discard)
        task.add_done_callback(lambda _t, key=turn.key: self._forget_turn(key, _t))

    def _forget_turn(self, key: ChannelKey, task: asyncio.Task[None]) -> None:
        """Drop a finished turn's channel entry, unless a newer turn replaced it."""
        if self._turns_by_channel.get(key) is task:
            del self._turns_by_channel[key]

    async def _run_turn_tracked(self, turn: DiscordTurn) -> None:
        try:
            await self._run_turn(turn)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Discord turn failed for %s", turn.key.display())
        finally:
            self._active_channels.discard(turn.key)

    # ── turn lifecycle ────────────────────────────────────────────────────

    async def _run_turn(self, turn: DiscordTurn) -> None:
        self._logger.info("Starting turn channel=%s chars=%s", turn.key.display(), len(turn.text))
        omnigent = await self._pool.get(self._server_url, turn.user_id)

        reply = _AnswerReply(
            turn.channel,
            turn.key,
            placeholder=_ACK_TEXT,
            edit_interval_seconds=self._stream_edit_interval,
            logger=self._logger,
        )

        try:
            session_id = await self._ensure_session(turn, omnigent)
        except _AuthExpired:
            await self._notify_auth_expired(turn, reply)
            return
        except _TurnAborted as aborted:
            await reply.stop_with(aborted.text)
            return
        if session_id is None:
            # No session and creation disabled (a follow-up on a dead
            # conversation): nothing to run.
            return

        # Acknowledge now — AFTER any session-config summary — so a new
        # conversation reads metadata → "Working on it…" → answer. The create +
        # runner launch is already done; the placeholder covers the wait until
        # the first tokens land, and the answer is streamed into this same
        # message.
        await reply.acknowledge()

        # Baseline the newest assistant message BEFORE the turn runs, so the
        # no-delta fallback below can tell this turn's answer from a prior one.
        baseline = await omnigent.latest_assistant_message(session_id)

        try:
            errored = await self._stream_turn(turn, omnigent, session_id, reply)
        except _TurnAborted:
            # A known mid-stream error already delivered its message and stopped
            # the reply; nothing left to finalize.
            return

        if reply.needs_fallback_text():
            # Last-resort safety net: the turn delivered no answer text on the
            # stream at all. Recover the server's newest assistant message, but
            # only when it's genuinely new: it must differ from the pre-turn
            # baseline (else a no-answer turn like a denied approval would
            # resurrect the PREVIOUS turn's message) AND not be something an
            # earlier sealed segment this turn already showed (else a trailing
            # notice would re-post the answer we just streamed).
            latest = await omnigent.latest_assistant_message(session_id)
            if (
                latest is not None
                and latest != baseline
                and not reply.already_delivered(latest[1])
            ):
                reply.set_fallback_text(latest[1])
        delivered_answer = await reply.finalize(errored=errored)
        if errored and delivered_answer:
            # An answer streamed AND the turn errored — post the generic failure
            # as a separate message so the answer stays intact. The detail was
            # already logged in _stream_turn; never echo it to the channel.
            await self._notifier.post_failure_reply(turn.channel)

        self._logger.info(
            "Completed Discord turn channel=%s session=%s streamed_chars=%s "
            "segments=%s errored=%s",
            turn.key.display(),
            session_id,
            reply.streamed_len,
            reply.segments,
            errored,
        )

    async def _notify_auth_expired(self, turn: DiscordTurn, reply: _AnswerReply) -> None:
        """Privately tell the user their login expired and how to fix it.

        Reached when a configured user's grant can no longer be refreshed
        (revoked, or the refresh token itself expired) or a restart dropped
        in-memory tokens. Discord has no ephemeral message outside an
        interaction, so this goes to their DM (falling back to a self-deleting
        channel mention). The placeholder is removed first so a failed turn
        leaves nothing behind. Best-effort: a delivery failure is logged, never
        raised (the turn is already aborting).
        """
        await reply.stop_with("")  # remove the placeholder without posting text
        try:
            await self._notifier.post_private(
                turn.channel, turn.key, turn.owner_user_id, relogin_required_text()
            )
        except Exception:
            self._logger.warning(
                "Failed to deliver re-login prompt channel=%s", turn.key.display()
            )

    async def _ensure_session(self, turn: DiscordTurn, omnigent: OmnigentClient) -> str | None:
        """Return the session id for this turn, creating one if needed.

        Returns ``None`` when there's no session and creation is disabled (a
        follow-up on a conversation whose session is gone). Raises
        :class:`_TurnAborted` with a user-facing message when startup fails.
        """
        record = await self._store.get_session(turn.key)
        if record is not None:
            self._logger.info(
                "Using existing Omnigent session channel=%s session_id=%s",
                turn.key.display(),
                record.session_id,
            )
            return record.session_id

        if not turn.create_if_missing:
            self._logger.info(
                "No session found and creation disabled channel=%s", turn.key.display()
            )
            return None

        try:
            session_id = await omnigent.create_session(turn.agent_id, turn.title)
            runner_id = await omnigent.launch_runner(
                session_id, workspace=turn.workspace or "", host_id=turn.host_id
            )
        except AuthRequiredError as exc:
            # Expired/lost token: prompt a re-login rather than a plain notice.
            self._logger.info(
                "Session startup needs re-login channel=%s: %s", turn.key.display(), exc
            )
            raise _AuthExpired() from exc
        except (
            ServerUnreachableError,
            HostUnavailableError,
            HarnessNotConfiguredError,
        ) as exc:
            self._logger.info("Session startup failed channel=%s: %s", turn.key.display(), exc)
            # These are curated bot-composed messages; fall back to the generic
            # failure rather than str(exc) so no server detail can leak.
            raise _TurnAborted(
                _classify_turn_error(exc, self._server_url) or GENERIC_FAILURE_TEXT
            ) from exc
        except Exception as exc:
            # Any other startup failure (e.g. a 500 surfaced as OmnigentError)
            # must still report rather than strand the conversation on "Working
            # on it…". The detail is logged here; the user gets a GENERIC
            # message — the raw error can carry a stack trace / internal path
            # and the channel is visible to everyone in it.
            self._logger.exception(
                "Failed to start Omnigent session channel=%s", turn.key.display()
            )
            raise _TurnAborted(
                "⚠️ Something went wrong starting your Omnigent session. Please try "
                "again; if it keeps happening, contact your Omnigent operator."
            ) from exc

        await self._store.upsert_session(
            turn.key,
            session_id,
            turn.title,
            owner_user_id=turn.owner_user_id,
            host_id=turn.host_id,
            workspace=turn.workspace,
        )
        self._logger.info(
            "Mapped Discord channel to new Omnigent session channel=%s session_id=%s runner_id=%s",
            turn.key.display(),
            session_id,
            runner_id,
        )
        # Orient the user on a NEW session: post a one-line config summary
        # (agent / harness / workspace + web-UI link) as the first durable
        # message, before the answer streams. Server-authoritative harness/agent
        # from the snapshot; best-effort so a failure never aborts the turn.
        try:
            info = await omnigent.get_session_info(session_id)
            await self._notifier.post_session_info(
                turn.channel,
                turn.key,
                harness=info.harness,
                agent_name=info.agent_name,
                workspace=turn.workspace,
                session_id=session_id,
            )
        except Exception:
            self._logger.warning(
                "Session-info summary failed channel=%s; continuing", turn.key.display()
            )
        return session_id

    async def _stream_turn(
        self,
        turn: DiscordTurn,
        omnigent: OmnigentClient,
        session_id: str,
        reply: _AnswerReply,
    ) -> bool:
        """Stream the turn's events into ``reply``. Returns whether it errored.

        A failure's detail is logged server-side only; the caller surfaces the
        generic failure message (never the raw detail).

        Discord renders a markdown subset client-side and the reply layer owns
        chunking, so there's no conversion here — just event routing. A known
        auth/reachability error aborts the turn with a user-facing message
        (delivered here); any other exception, or an in-band ``response.error``
        event, becomes error text used at finalization.
        """
        state = _StreamState()
        try:
            # Explicit iteration (not ``async for``) so a gap between events can
            # be detected: when the stream goes quiet for ``_IDLE_FLUSH_SECONDS``
            # we force any accumulated answer text onto the screen, rather than
            # letting the edit cadence hold it invisible until the turn ends. A
            # single in-flight "next event" task is kept alive across idle
            # windows (a timeout must NOT cancel it — that would end the
            # generator); we re-await it next window.
            events = omnigent.run_turn(
                session_id, turn.text, workspace=turn.workspace, host_id=turn.host_id
            ).__aiter__()
            pending: asyncio.Task[dict[str, Any]] | None = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(events.__anext__())
                    done, _ = await asyncio.wait({pending}, timeout=_IDLE_FLUSH_SECONDS)
                    if not done:
                        # Stream idle this window — reveal any held text now.
                        await reply.flush_if_buffered()
                        continue
                    try:
                        event = await pending
                    except StopAsyncIteration:
                        break
                    pending = None
                    await self._dispatch_stream_event(
                        event, turn, omnigent, session_id, reply, state
                    )
            finally:
                # Reap the in-flight read so the generator isn't left running
                # when its scope exits.
                if pending is not None:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending
        except AuthRequiredError as exc:
            # Token expired mid-turn: prompt a re-login.
            self._logger.info(
                "Turn needs re-login mid-stream channel=%s: %s", turn.key.display(), exc
            )
            await self._notify_auth_expired(turn, reply)
            state.aborted = True
        except (
            ServerUnreachableError,
            StreamInterruptedError,
            HostUnavailableError,
            HarnessNotConfiguredError,
        ) as exc:
            self._logger.info("Turn error mid-stream channel=%s: %s", turn.key.display(), exc)
            # Curated bot-composed messages; fall back to the generic failure
            # rather than str(exc) so no server detail can leak.
            await reply.stop_with(
                _classify_turn_error(exc, self._server_url) or GENERIC_FAILURE_TEXT
            )
            state.aborted = True
        except Exception:
            # Log the detail here (never surfaced — it can carry a stack trace /
            # internal path); the user gets the generic failure via ``errored``.
            self._logger.exception("Omnigent turn failed for %s", turn.key.display())
            state.errored = True
        finally:
            # Settle any card still open (turn ended before its resolution push,
            # or was torn down) so no resolver task leaks; an unanswered one is
            # declined server-side to release the park.
            await self._elicitation.finish_pending(omnigent, turn, state.elicitations)
        if state.aborted:
            raise _TurnAborted("")  # already delivered; signal the caller to stop
        return state.errored

    async def _dispatch_stream_event(
        self,
        event: dict[str, Any],
        turn: DiscordTurn,
        omnigent: OmnigentClient,
        session_id: str,
        reply: _AnswerReply,
        state: _StreamState,
    ) -> None:
        """Route one stream event to the reply or an out-of-band message.

        Out-of-band messages (elicitation card, policy/file notice, first todo
        post) seal the current answer segment first so they sort in
        chronological order. Mutates ``state`` for the todo message and any
        in-band error text.
        """
        channel = turn.channel

        delta = extract_delta(event)
        if delta:
            # ``message_id`` (native terminal harnesses tag each assistant
            # message item; None for in-process streaming) lets the reply insert
            # a paragraph break between back-to-back messages.
            mid = event.get("message_id")
            await reply.add_delta(delta, mid if isinstance(mid, str) else None)
            return

        elicitation = extract_elicitation_request(event, session_id)
        if elicitation is not None:
            # Seal the answer so far (it sorts before the card), then post the
            # card and spawn a background resolver — WITHOUT blocking this loop.
            # Keeping the read loop live is the whole point: the continuation
            # deltas and the ``elicitation_resolved`` push arrive as normal
            # events (the web UI's model), so no polling is needed.
            await reply.seal_for_interruption()
            await self._elicitation.start(omnigent, turn, elicitation, state.elicitations)
            return

        resolved_eid = extract_elicitation_resolved(event)
        if resolved_eid is not None:
            # The server resolved the elicitation (our own posted verdict, or an
            # answer elsewhere). Wake the resolver so it stops waiting, and
            # finalize the card in place. Idempotent via the `finalized` guard.
            await self._elicitation.on_resolved(turn, resolved_eid, state.elicitations)
            return

        denied_reason = extract_policy_denied(event)
        if denied_reason is not None:
            await reply.seal_for_interruption()
            await self._notifier.post_reply(channel, format_policy_denied(denied_reason))
            return

        output_file = extract_output_file(event)
        if output_file is not None:
            await reply.seal_for_interruption()
            await self._notifier.post_reply(channel, format_output_file(output_file))
            return

        todos = extract_todos(event)
        if todos is not None:
            # The first plan post is a new out-of-band message → seal before it;
            # later updates edit it in place (no boundary, no fragmentation).
            # Seal ONLY when a message will actually land: an empty todos render
            # posts nothing, so sealing on it would fragment the answer into
            # extra messages with no notice between them.
            will_post = state.todo_message is None and format_todos(todos) is not None
            if will_post:
                await reply.seal_for_interruption()
            state.todo_message = await self._notifier.post_or_update_todos(
                channel, turn.key, todos, state.todo_message
            )
            return

        item_text = extract_assistant_text(event)
        if item_text:
            reply.set_final(item_text)

        event_error = extract_error_text(event)
        if event_error:
            # In-band server error (response.error / turn.failed). Its message
            # can embed a stack trace / internal path, so log it and show the
            # generic failure — do NOT echo it to the channel.
            self._logger.warning(
                "Omnigent in-band turn error channel=%s: %s", turn.key.display(), event_error
            )
            state.errored = True

    # ── commands ──────────────────────────────────────────────────────────

    async def start_new_session(self, interaction: Any) -> None:
        """Handle ``/omnigent new``: forget this conversation's session.

        A Discord DM has no threads, so without this a DM would stay bound to
        one Omnigent session forever. In a guild the same command resets the
        current thread, which is occasionally useful after a session goes wrong.
        """
        channel = interaction.channel
        guild = getattr(interaction, "guild", None)
        key = ChannelKey(
            channel_id=str(channel.id), guild_id=str(guild.id) if guild is not None else None
        )
        requester = str(interaction.user.id)
        record = await self._store.get_session(key)
        if record is None:
            await interaction.response.send_message(
                "There's no Omnigent session here yet — just send me a message.",
                ephemeral=True,
            )
            return
        if record.owner_user_id != requester:
            await interaction.response.send_message(
                "This conversation's session belongs to whoever started it.",
                ephemeral=True,
            )
            return
        # A turn still streaming holds the channel reservation. Refusing here
        # would be a dead end: the requester is the owner, and in a DM this
        # command is the only reset there is. Cancel their own turn and reset.
        cancelled = await self._cancel_turn(key)
        await self._store.clear_session(key)
        self._logger.info(
            "Cleared session mapping channel=%s session_id=%s", key.display(), record.session_id
        )
        note = " I stopped the turn that was running." if cancelled else ""
        await interaction.response.send_message(
            f"🆕 Started fresh.{note} Your next message begins a new Omnigent session.",
            ephemeral=True,
        )

    async def _cancel_turn(self, key: ChannelKey) -> bool:
        """Stop a turn streaming in this channel. Returns whether one was running."""
        task = self._turns_by_channel.get(key)
        if task is None or task.done():
            return False
        self._logger.info("Cancelling turn on owner request channel=%s", key.display())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._active_channels.discard(key)
        return True
