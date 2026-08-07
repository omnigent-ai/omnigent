from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from omnigent_slack.approvals import (
    ClickTarget,
    ElicitationCoordinator,
    Verdict,
)
from omnigent_slack.auth_manager import pack_user_key
from omnigent_slack.elicitation import ElicitationController, ElicitationTurnState
from omnigent_slack.models import SlackTurn, ThreadKey, event_is_dm
from omnigent_slack.notifications import (
    SlackNotifier,
    format_output_file,
    format_policy_denied,
    format_todos,
)
from omnigent_slack.omnigent import (
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
from omnigent_slack.setup import SetupFlow, host_unavailable_text
from omnigent_slack.store import SQLiteStore
from omnigent_slack.streaming import (
    SlackClientProtocol,
    _AnswerReply,
)
from omnigent_slack.text import GENERIC_FAILURE_TEXT, strip_bot_mention

# Immediate acknowledgement shown while the session spins up and while the agent
# works before the first streamed tokens arrive. Deleted only once real content
# is actually on screen — on the first flushed delta, or after the finalizing
# stop() for a buffered answer — so the thread never shows an empty gap between
# the placeholder vanishing and the reply appearing.
_ACK_TEXT = "_Working on it…_"

# How long the read loop waits for the next stream event before treating the
# stream as idle and force-flushing any buffered-but-unshown answer text. The
# SDK flushes to Slack only when its buffer fills, so a short burst the agent
# then pauses after (a tool call, thinking) can stay invisible until more text
# arrives or the turn ends. This is a SENSITIVITY window, not a display delay:
# during active streaming the buffer still flushes immediately on fill; this only
# fires once the stream actually goes quiet. Small enough to feel live, large
# enough not to fragment a steady token stream into one API call per token.
_IDLE_FLUSH_SECONDS = 2.0

_SERVER_UNREACHABLE_TEXT = (
    ":warning: I couldn't reach your Omnigent server. If it moved or is "
    "down, run /omnigent to reconfigure."
)

# An unauthenticated turn (a grant that can no longer be refreshed, or a bot
# restart that dropped in-memory tokens) is NOT delivered through this plain-text
# path. The user was already set up, so they get a DM with a re-login setup
# button instead (see ``SlackOmnigentService._notify_auth_expired``), which is
# reliably delivered and actionable — unlike a thread ephemeral Slack may never
# render.

# Shown when the live turn stream kept dropping and reconnect was exhausted. The
# server was reachable throughout (a proxy severed the long-lived stream, e.g. a
# ~5-minute duration cap), and the turn may still be running server-side — so
# this is NOT the "server is down / reconfigure" case. Its result may still land
# in the thread when the turn finishes.
_STREAM_INTERRUPTED_TEXT = (
    ":warning: I lost my live connection to the running turn. Its result may "
    "still arrive here — send another message if it doesn't."
)


class _TurnAborted(Exception):
    """A turn can't proceed; ``text`` is the public user-facing reason to deliver."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _AuthExpired(Exception):
    """The server rejected the turn as unauthenticated (expired/lost token).

    Distinct from :class:`_TurnAborted` because it is not delivered as plain
    text: the caller DMs the user a re-login setup button instead (a reliable,
    actionable notice for a grant that can no longer be refreshed).
    """


@dataclass
class _StreamState:
    """Mutable per-turn state threaded through the stream event dispatch."""

    # Timestamp of the live plan/todo message, edited in place across updates.
    todos_ts: str | None = None
    # Whether the turn failed. The user-visible signal; a failure's raw detail is
    # logged at the point of failure (never stored — it can carry stack traces /
    # internal paths), and the user only ever sees the generic failure message.
    errored: bool = False
    # Set when a known error was delivered mid-stream and the turn should stop.
    aborted: bool = False
    # In-flight elicitation cards this turn (owned by the ElicitationController).
    elicitations: ElicitationTurnState = field(default_factory=ElicitationTurnState)


def _classify_turn_error(exc: BaseException, server_url: str) -> str | None:
    """Map a known startup/turn error to its public user-facing text.

    Single source of truth shared by the session-creation and mid-turn error
    paths, so the text can't drift. All these errors affect everyone on the
    thread and are delivered publicly. Returns ``None`` for an unrecognized error
    (the caller falls back to the generic failure). Auth errors do NOT flow
    through here — the caller intercepts them for a DM re-login prompt.
    """
    if isinstance(exc, StreamInterruptedError):
        # A mid-stream drop with reconnect exhausted — the server stayed
        # reachable, so this is NOT the "reconfigure" case. Its result may still land.
        return _STREAM_INTERRUPTED_TEXT
    if isinstance(exc, ServerUnreachableError):
        return _SERVER_UNREACHABLE_TEXT
    if isinstance(exc, HostUnavailableError):
        return host_unavailable_text(server_url)
    if isinstance(exc, HarnessNotConfiguredError):
        # The server's message is curated, actionable guidance for this code —
        # surface it so the user knows to run `omnigent setup` on the host.
        return f":warning: {exc}"
    return None


class SlackOmnigentService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        pool: OmnigentClientPool,
        setup: SetupFlow,
        server_url: str,
        bot_user_id: str | None = None,
        elicitations: ElicitationCoordinator | None = None,
    ) -> None:
        self._store = store
        self._pool = pool
        self._setup = setup
        # The one operator-configured Omnigent server. Always the routing
        # target — any server_url persisted on an older config/session row is
        # ignored, so a config change points every thread at the new server.
        self._server_url = server_url
        self._bot_user_id = bot_user_id
        self._logger = logging.getLogger(__name__)
        # All outbound Slack messages (acks, replies, ephemerals, todo plan,
        # deflection notices) — keeps message formatting out of this class.
        self._notifier = SlackNotifier(server_url=server_url, logger=self._logger)
        # Bridges an in-flight elicitation card to the button/form interaction
        # that answers it (and to the pushed elicitation_resolved). Shared with
        # the block-action handler.
        self._elicitations = elicitations or ElicitationCoordinator()
        # Owns all elicitation-card orchestration during a turn (post, resolver
        # task, finalize) — keeps this class to routing + turn lifecycle.
        self._elicitation = ElicitationController(
            self._elicitations,
            server_url=server_url,
            post_reply=self._notifier.post_reply,
            logger=self._logger,
        )
        # Threads with a turn actively streaming IN THIS PROCESS. Each turn opens
        # its own SSE stream; two at once would render the same events into Slack
        # twice. This is a LOCAL concurrency guard (reserved synchronously, before
        # any await, so two racing messages can't both pass) — necessary because
        # the server-activity check alone races: claude-native flips to `idle`
        # between streaming bursts, so a snapshot mid-turn can read "not busy"
        # while a local stream is still live. The guard is safe from stale-wedge
        # because every turn is bounded (the elicitation grace fix guarantees it
        # ends and releases). The server-activity check (see _route_turn) is the
        # SEPARATE cross-surface signal (web-UI busy / pending action).
        self._active_threads: set[ThreadKey] = set()
        # In-flight turn tasks, tracked so shutdown can cancel them.
        self._turn_tasks: set[asyncio.Task[None]] = set()

    @property
    def elicitations(self) -> ElicitationCoordinator:
        return self._elicitations

    async def shutdown(self) -> None:
        tasks = list(self._turn_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Cancel any elicitation resolver tasks still awaiting a click so they
        # aren't orphaned ("Task was destroyed but it is pending").
        await self._elicitation.shutdown()

    async def handle_app_mention(
        self,
        *,
        body: dict[str, Any],
        event: dict[str, Any],
        client: SlackClientProtocol,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._logger.info(
            "Received Slack app_mention team=%s channel=%s ts=%s user=%s event_id=%s",
            body.get("team_id") or event.get("team"),
            event.get("channel"),
            event.get("ts"),
            event.get("user"),
            body.get("event_id") or event.get("client_msg_id"),
        )
        accepted, bot_user_id = await self._accept_event(body, event, context, kind="app_mention")
        if not accepted:
            return

        # The event is now claimed and Bolt has auto-acked, so Slack won't
        # redeliver. If handling fails before the turn is underway, release the
        # claim so a redelivery / re-send isn't silently deduped away.
        try:
            team_id = _team_id(body, event)
            key = ThreadKey.from_event(team_id, event)
            text = strip_bot_mention(str(event.get("text") or ""), bot_user_id)
            if not text:
                self._logger.info(
                    "Slack app_mention had no text after mention thread=%s",
                    key.display(),
                )
                await client.chat_postMessage(
                    channel=key.channel_id,
                    thread_ts=key.reply_ts,
                    text="Send a message after mentioning me to start a session.",
                )
                return

            self._logger.info(
                "Accepted Slack app_mention thread=%s chars=%s", key.display(), len(text)
            )
            await self._route_turn(
                key=key,
                event=event,
                text=text,
                client=client,
                in_channel=not event_is_dm(event),
            )
        except Exception:
            await self._unclaim_event(body, event)
            raise

    async def handle_message(
        self,
        *,
        body: dict[str, Any],
        event: dict[str, Any],
        client: SlackClientProtocol,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._logger.info(
            "Received Slack message team=%s channel=%s ts=%s thread_ts=%s user=%s event_id=%s",
            body.get("team_id") or event.get("team"),
            event.get("channel"),
            event.get("ts"),
            event.get("thread_ts"),
            event.get("user"),
            body.get("event_id") or event.get("client_msg_id"),
        )
        accepted, bot_user_id = await self._accept_event(body, event, context, kind="message")
        if not accepted:
            return

        # The event is now claimed and Bolt has auto-acked, so Slack won't
        # redeliver. If handling fails before the turn is underway, release the
        # claim so a redelivery / re-send isn't silently deduped away.
        try:
            if not event_is_dm(event):
                # In channels Omnigent only joins a thread when @-mentioned (which
                # arrives as an app_mention event). Plain messages — even a reply in
                # a thread that already has a session, and even one that mentions the
                # bot (app_mention handles that copy) — are human discussion and must
                # not be added to the Omnigent session.
                self._logger.info(
                    "Ignoring channel message channel=%s ts=%s",
                    event.get("channel"),
                    event.get("ts"),
                )
                return

            team_id = _team_id(body, event)
            key = ThreadKey.from_event(team_id, event)

            # DMs do not fire app_mention, so a "<@bot>" here is the only event we
            # get — strip the mention (if any) and treat it like any other DM rather
            # than dropping it as a duplicate.
            text = strip_bot_mention(str(event.get("text") or ""), bot_user_id)
            if not text:
                self._logger.info("Ignoring empty Slack direct message thread=%s", key.display())
                return

            # A DM maps one session per thread (like a channel): a top-level message
            # starts a new session, a threaded reply continues it.
            self._logger.info(
                "Accepted Slack direct message thread=%s chars=%s",
                key.display(),
                len(text),
            )
            await self._route_turn(
                key=key,
                event=event,
                text=text,
                client=client,
                in_channel=False,
            )
        except Exception:
            await self._unclaim_event(body, event)
            raise

    async def _route_turn(
        self,
        *,
        key: ThreadKey,
        event: dict[str, Any],
        text: str,
        client: SlackClientProtocol,
        in_channel: bool,
    ) -> None:
        requester = str(event.get("user") or "")
        if not requester:
            # No authenticated Slack user on the event — we can't attribute the
            # message to an owner, so we refuse to route it. Never fall through to
            # an owner-less turn (that would be an unguarded, adoptable session).
            self._logger.warning("Dropping Slack event with no user thread=%s", key.display())
            return

        # Authoritative thread-ownership gate, independent of the session store.
        # Slack stamps ``parent_user_id`` (the thread root's author) on every
        # threaded event; when it's present and is NOT the requester, this is a
        # reply into someone else's thread → refuse, since a Slack thread maps 1:1
        # to a session. This is ground truth from Slack that survives a bot
        # restart, unlike the (ephemeral) store: without it, a restart that drops
        # the thread→session record would let ANY user's reply create a fresh
        # session in another user's thread. A root-level mention (new thread) has
        # no ``parent_user_id`` and falls through to the normal store-backed path.
        #
        # Skip this in a DM: a 1:1 DM has no cross-user ownership concern (only the
        # one user and the bot are there), and a reply threaded under a BOT message
        # carries ``parent_user_id == <bot id>`` — which would wrongly refuse the
        # user's own message.
        parent_user_id = str(event.get("parent_user_id") or "")
        if not key.is_dm and parent_user_id and parent_user_id != requester:
            self._logger.info(
                "Ignoring reply from non-owner thread=%s parent=%s requester=%s",
                key.display(),
                parent_user_id,
                requester,
            )
            await self._notifier.notify_non_owner(client, key, requester)
            return

        # LOCAL concurrency guard: reserve the thread SYNCHRONOUSLY here (no await
        # before this add) so two near-simultaneous messages can't both open a
        # stream and double-render. If already reserved, a turn is streaming in
        # this process → deflect. This is distinct from the server-activity check
        # below: claude-native reads `idle` between bursts, so the server snapshot
        # alone would let a 2nd turn slip in mid-stream. The reservation is held
        # until either a spawned turn's finally releases it, or we release it
        # below on any path that does NOT spawn.
        if key in self._active_threads:
            self._logger.info(
                "Thread already streaming in-process thread=%s; deflecting", key.display()
            )
            record = await self._store.get_session(key)
            if record is not None and record.owner_user_id != requester:
                await self._notifier.notify_non_owner(client, key, requester)
            else:
                # A parked turn (awaiting an approval/question) is STILL streaming,
                # so it holds this reservation — meaning this branch, not the
                # server-activity one below, handles a new message during a pending
                # elicitation. Ask the server whether the session needs user action
                # so we show "respond to the pending request above" rather than the
                # generic "still working" notice. Best-effort: no record / unknown
                # activity falls back to the busy notice.
                needs_action = False
                if record is not None:
                    omnigent = await self._pool.get(
                        self._server_url, pack_user_key(key.team_id, requester)
                    )
                    activity = await omnigent.get_session_activity(record.session_id)
                    needs_action = activity.needs_user_action
                await self._notifier.notify_thread_busy(
                    client,
                    key,
                    requester,
                    needs_action=needs_action,
                    session_id=record.session_id if record is not None else None,
                )
            return
        self._active_threads.add(key)
        spawned = False
        try:
            record = await self._store.get_session(key)

            if record is not None:
                # An existing thread belongs to whoever started it. A follow-up
                # from a different user (only possible in a channel) is not added
                # to the session. Tell that user — privately — why nothing
                # happened. A record with no stored owner is treated as locked
                # (fail closed): only match when owner is known AND == requester.
                if record.owner_user_id != requester:
                    self._logger.info(
                        "Ignoring follow-up from non-owner thread=%s owner=%s requester=%s",
                        key.display(),
                        record.owner_user_id,
                        requester,
                    )
                    await self._notifier.notify_non_owner(client, key, requester)
                    return
                # Cross-surface check: the SERVER decides busy/awaiting-action
                # (web UI or another client may be driving the session), mirroring
                # the web UI's send gate. The local guard above already prevents a
                # concurrent Slack stream; this catches activity elsewhere.
                omnigent = await self._pool.get(
                    self._server_url, pack_user_key(key.team_id, requester)
                )
                activity = await omnigent.get_session_activity(record.session_id)
                if activity.needs_user_action or activity.is_busy:
                    self._logger.info(
                        "Server busy thread=%s status=%s pending=%s; deflecting",
                        key.display(),
                        activity.status,
                        activity.pending_elicitation,
                    )
                    await self._notifier.notify_thread_busy(
                        client,
                        key,
                        requester,
                        needs_action=activity.needs_user_action,
                        session_id=record.session_id,
                    )
                    return
                self._spawn_turn(
                    SlackTurn(
                        key=key,
                        text=text,
                        user_id=requester,
                        create_if_missing=False,
                        # Title is only used when creating a session; an existing
                        # thread already has one, so skip the permalink lookup.
                        title="",
                        slack_client=client,
                        agent_id="",
                        owner_user_id=record.owner_user_id or requester,
                        workspace=record.workspace,
                        host_id=record.host_id,
                    )
                )
                spawned = True
                return

            config = await self._store.get_user_config(key.team_id, requester)
            if config is None:
                self._logger.info(
                    "Unconfigured user thread=%s user=%s; prompting setup",
                    key.display(),
                    requester,
                )
                await self._setup.prompt_unconfigured(
                    client,
                    requester,
                    channel=key.channel_id,
                    thread_ts=key.reply_ts,
                    in_channel=in_channel,
                )
                return

            self._spawn_turn(
                SlackTurn(
                    key=key,
                    text=text,
                    user_id=requester,
                    create_if_missing=True,
                    title=await _session_title(client, key, event),
                    slack_client=client,
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
                self._active_threads.discard(key)

    def _spawn_turn(self, turn: SlackTurn) -> None:
        """Run a reserved turn as a background task, tracked for shutdown.

        The thread is already reserved in ``_active_threads`` by ``_route_turn``
        (synchronously, before any await); ``_run_turn_tracked`` releases it when
        the turn ends.
        """
        task = asyncio.create_task(self._run_turn_tracked(turn))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_turn_tracked(self, turn: SlackTurn) -> None:
        try:
            await self._run_turn(turn)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Slack turn failed for %s", turn.key.display())
        finally:
            self._active_threads.discard(turn.key)

    async def _run_turn(self, turn: SlackTurn) -> None:
        self._logger.info("Starting turn thread=%s chars=%s", turn.key.display(), len(turn.text))
        omnigent = await self._pool.get(
            self._server_url, pack_user_key(turn.key.team_id, turn.user_id)
        )

        reply = _AnswerReply(
            turn.slack_client,
            turn.key,
            recipient_user_id=turn.owner_user_id,
            ack_ts=None,
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
            # No session and creation disabled (a follow-up on a dead thread):
            # nothing to run.
            return

        # Acknowledge now — AFTER any session-config summary — so a new thread
        # reads metadata → "Working on it…" → answer. The create + runner launch
        # is already done; the placeholder covers the wait until the first tokens
        # flush, and is cleared once the reply is actually on screen.
        reply.set_ack(await self._notifier.post_ack(turn.slack_client, turn.key, _ACK_TEXT))

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
            # notice would re-post the answer we just streamed). Compare the whole
            # (id, text) tuple so an id-less message is judged by its text.
            # (The pure-push elicitation model keeps the stream reading across a
            # park, so a post-approval answer now streams normally rather than
            # relying on this fetch.)
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
            # as a separate reply so the answer stays intact. The detail was
            # already logged in _stream_turn; never echo it to the channel.
            await self._notifier.post_failure_reply(turn.slack_client, turn.key)

        self._logger.info(
            "Completed Slack turn thread=%s session=%s streamed_chars=%s segments=%s errored=%s",
            turn.key.display(),
            session_id,
            reply.streamed_len,
            reply.segments,
            errored,
        )

    async def _notify_auth_expired(self, turn: SlackTurn, reply: _AnswerReply) -> None:
        """Deliver the expired-login re-login prompt as a DM with a setup button.

        Reached when a configured user's grant can no longer be refreshed
        (revoked, or the refresh token itself expired) or a restart dropped
        in-memory tokens — so this must be reliably seen and actionable rather
        than a thread ephemeral that Slack may never render. Clears the
        "Working on it…" placeholder first so a failed turn leaves nothing behind.
        Best-effort: a DM failure is logged, never raised (the turn is already
        aborting). In a channel, an ephemeral pointer nudges the user to their DM;
        in a DM the re-login post already lands in the same conversation, so no
        redundant pointer is posted (``in_channel`` is False there).
        """
        await reply.stop_with("")  # clear the ack placeholder without posting text
        try:
            await self._setup.prompt_relogin(
                turn.slack_client,
                turn.owner_user_id,
                channel=turn.key.channel_id,
                thread_ts=turn.key.reply_ts,
                in_channel=not turn.key.is_dm,
            )
        except Exception:
            self._logger.warning("Failed to deliver re-login prompt thread=%s", turn.key.display())

    async def _ensure_session(self, turn: SlackTurn, omnigent: OmnigentClient) -> str | None:
        """Return the session id for this turn, creating one if needed.

        Returns ``None`` when there's no session and creation is disabled (a
        follow-up on a thread whose session is gone). Raises :class:`_TurnAborted`
        with a user-facing message when session startup fails.
        """
        record = await self._store.get_session(turn.key)
        if record is not None:
            self._logger.info(
                "Using existing Omnigent session thread=%s session_id=%s",
                turn.key.display(),
                record.session_id,
            )
            return record.session_id

        if not turn.create_if_missing:
            self._logger.info(
                "No session found and creation disabled thread=%s", turn.key.display()
            )
            return None

        try:
            session_id = await omnigent.create_session(turn.agent_id, turn.title)
            runner_id = await omnigent.launch_runner(
                session_id, workspace=turn.workspace or "", host_id=turn.host_id
            )
        except AuthRequiredError as exc:
            # Expired/lost token: DM a re-login button rather than a plain notice.
            self._logger.info(
                "Session startup needs re-login thread=%s: %s", turn.key.display(), exc
            )
            raise _AuthExpired() from exc
        except (
            ServerUnreachableError,
            HostUnavailableError,
            HarnessNotConfiguredError,
        ) as exc:
            self._logger.info("Session startup failed thread=%s: %s", turn.key.display(), exc)
            # These are curated bot-composed messages; fall back to the generic
            # failure rather than str(exc) so no server detail can leak.
            raise _TurnAborted(
                _classify_turn_error(exc, self._server_url) or GENERIC_FAILURE_TEXT
            ) from exc
        except Exception as exc:
            # Any other startup failure (e.g. a 500 surfaced as OmnigentError)
            # must still report rather than strand the thread on "Working on it…".
            # The detail is logged here; the user gets a GENERIC message — the raw
            # error can carry a stack trace / internal path and the thread is
            # visible to the whole channel (DESIGN.md: server bodies are not echoed).
            self._logger.exception(
                "Failed to start Omnigent session thread=%s", turn.key.display()
            )
            raise _TurnAborted(
                ":warning: Something went wrong starting your Omnigent session. Please try "
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
            "Mapped Slack thread to new Omnigent session thread=%s session_id=%s runner_id=%s",
            turn.key.display(),
            session_id,
            runner_id,
        )
        # Orient the user on a NEW session: post a one-line config summary (agent
        # / harness / workspace + web-UI link) as the first durable message,
        # before the answer streams. Server-authoritative harness/agent from the
        # snapshot; best-effort so a snapshot/post failure never aborts the turn.
        try:
            info = await omnigent.get_session_info(session_id)
            await self._notifier.post_session_info(
                turn.slack_client,
                turn.key,
                harness=info.harness,
                agent_name=info.agent_name,
                workspace=turn.workspace,
                session_id=session_id,
            )
        except Exception:
            self._logger.warning(
                "Session-info summary failed thread=%s; continuing", turn.key.display()
            )
        return session_id

    async def _stream_turn(
        self,
        turn: SlackTurn,
        omnigent: OmnigentClient,
        session_id: str,
        reply: _AnswerReply,
    ) -> bool:
        """Stream the turn's events into ``reply``. Returns whether it errored.

        A failure's detail is logged server-side only; the caller surfaces the
        generic failure message (never the raw detail — see :data:`_StreamState`).

        Slack renders markdown server-side and owns chunking, so there's no
        mrkdwn conversion or msg_too_long handling here — just event routing.
        A known auth/reachability error aborts the turn with a user-facing
        message (delivered here); any other exception, or an in-band
        ``response.error`` event, becomes error text used at finalization.
        """
        # Timestamp of the live plan/todo message, edited in place across updates.
        state = _StreamState()
        try:
            # Explicit iteration (not ``async for``) so a gap between events can
            # be detected: when the stream goes quiet for ``_IDLE_FLUSH_SECONDS``
            # we force any buffered answer text onto the screen, rather than
            # letting the SDK's size-only buffer hold it invisible until the turn
            # ends. A single in-flight "next event" task is kept alive across
            # idle windows (a timeout must NOT cancel it — that would end the
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
                        # Stream idle this window — reveal any buffered text now.
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
                # Reap the in-flight read so the generator isn't left running when
                # its scope exits (mirrors _run_turn_once's teardown).
                if pending is not None:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending
        except AuthRequiredError as exc:
            # Token expired mid-turn: DM a re-login button (see _notify_auth_expired).
            self._logger.info(
                "Turn needs re-login mid-stream thread=%s: %s", turn.key.display(), exc
            )
            await self._notify_auth_expired(turn, reply)
            state.aborted = True
        except (
            ServerUnreachableError,
            StreamInterruptedError,
            HostUnavailableError,
            HarnessNotConfiguredError,
        ) as exc:
            self._logger.info("Turn error mid-stream thread=%s: %s", turn.key.display(), exc)
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
        turn: SlackTurn,
        omnigent: OmnigentClient,
        session_id: str,
        reply: _AnswerReply,
        state: _StreamState,
    ) -> None:
        """Route one stream event to the reply or an out-of-band message.

        Out-of-band messages (elicitation card, policy/file notice, first todo
        post) seal the current answer segment first so they sort in
        chronological order. Mutates ``state`` for the todo-message timestamp
        and any in-band error text.
        """
        client = turn.slack_client

        delta = extract_delta(event)
        if delta:
            # ``message_id`` (native terminal harnesses tag each assistant message
            # item; None for in-process streaming) lets the reply insert a
            # paragraph break between back-to-back messages.
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
            await self._notifier.post_reply(client, turn.key, format_policy_denied(denied_reason))
            return

        output_file = extract_output_file(event)
        if output_file is not None:
            await reply.seal_for_interruption()
            await self._notifier.post_reply(client, turn.key, format_output_file(output_file))
            return

        todos = extract_todos(event)
        if todos is not None:
            # The first plan post is a new out-of-band message → seal before it;
            # later updates edit it in place (no boundary, no fragmentation). Seal
            # ONLY when a message will actually land: an empty todos render posts
            # nothing and leaves todos_ts None, so sealing on it would fragment the
            # answer into extra segments with no notice between them.
            will_post = state.todos_ts is None and format_todos(todos) is not None
            if will_post:
                await reply.seal_for_interruption()
            state.todos_ts = await self._notifier.post_or_update_todos(
                client, turn.key, todos, state.todos_ts
            )
            return

        item_text = extract_assistant_text(event)
        if item_text:
            reply.set_final(item_text)

        event_error = extract_error_text(event)
        if event_error:
            # In-band server error (response.error / turn.failed). Its message can
            # embed a stack trace / internal path, so log it and show the generic
            # failure — do NOT echo it to the channel.
            self._logger.warning(
                "Omnigent in-band turn error thread=%s: %s", turn.key.display(), event_error
            )
            state.errored = True

    async def handle_elicitation_action(
        self, *, session_id: str, elicitation_id: str, verdict: Verdict
    ) -> bool:
        """Deliver a button/form verdict (block-action handler entry point)."""
        return await self._elicitation.handle_action(
            session_id=session_id, elicitation_id=elicitation_id, verdict=verdict
        )

    async def reject_non_owner_click(
        self, client: SlackClientProtocol, body: dict[str, Any], target: ClickTarget
    ) -> None:
        """Privately tell a non-owner their click on someone else's card was ignored."""
        await self._elicitation.reject_non_owner_click(client, body, target)

    async def _accept_event(
        self,
        body: dict[str, Any],
        event: dict[str, Any],
        context: dict[str, Any] | None,
        *,
        kind: str,
    ) -> tuple[bool, str | None]:
        # Shared gate for both event handlers: drop duplicates (Slack redelivers)
        # and bot/edit/delete echoes. Returns whether to proceed and the resolved
        # bot user id for mention stripping.
        if not await self._claim_event(body, event):
            self._logger.info(
                "Ignoring duplicate Slack %s event_id=%s",
                kind,
                body.get("event_id") or event.get("client_msg_id"),
            )
            return False, None
        bot_user_id = self._resolve_bot_user_id(context)
        if self._should_ignore_message(event, bot_user_id):
            self._logger.info(
                "Ignoring Slack %s subtype=%s bot_id=%s user=%s bot_user_id=%s",
                kind,
                event.get("subtype"),
                event.get("bot_id"),
                event.get("user"),
                bot_user_id,
            )
            return False, None
        return True, bot_user_id

    async def _claim_event(self, body: dict[str, Any], event: dict[str, Any]) -> bool:
        return await self._store.claim_event(_event_id(body, event))

    async def _unclaim_event(self, body: dict[str, Any], event: dict[str, Any]) -> None:
        """Release the event's dedup claim (best-effort) so a failed handle retries."""
        try:
            await self._store.unclaim_event(_event_id(body, event))
        except Exception:
            # Never mask the original failure with an unclaim error.
            self._logger.warning("Failed to unclaim Slack event after handler error")

    def _resolve_bot_user_id(self, context: dict[str, Any] | None) -> str | None:
        bot_user_id = None if context is None else context.get("bot_user_id")
        if isinstance(bot_user_id, str):
            self._bot_user_id = bot_user_id
            return bot_user_id
        return self._bot_user_id

    @staticmethod
    def _should_ignore_message(event: dict[str, Any], bot_user_id: str | None) -> bool:
        subtype = event.get("subtype")
        if subtype in {"bot_message", "message_changed", "message_deleted"}:
            return True
        if event.get("bot_id"):
            return True
        user_id = event.get("user")
        return bool(bot_user_id and user_id == bot_user_id)


def _team_id(body: dict[str, Any], event: dict[str, Any]) -> str:
    team_id = body.get("team_id") or event.get("team")
    if not team_id:
        raise ValueError("Slack event is missing team_id")
    return str(team_id)


def _event_id(body: dict[str, Any], event: dict[str, Any]) -> str | None:
    """The dedup key for a Slack event (``event_id``, else ``client_msg_id``)."""
    event_id = body.get("event_id") or event.get("client_msg_id")
    return str(event_id) if event_id else None


async def _session_title(
    client: SlackClientProtocol, key: ThreadKey, event: dict[str, Any]
) -> str:
    """Build the Omnigent session title: ``Slack: <thread permalink>``.

    A real Slack thread permalink (via ``chat.getPermalink``) is a clickable URL
    that the web UI linkifies, so the session list points back at the originating
    thread. Falls back to a plain channel/ts descriptor if the lookup fails (e.g.
    a missing scope) — the title is cosmetic and must never block session start.
    """
    ts = event.get("thread_ts") or event.get("ts")
    try:
        response = await client.chat_getPermalink(channel=key.channel_id, message_ts=ts)
        permalink = response.get("permalink")
        if isinstance(permalink, str) and permalink:
            return f"Slack: {permalink}"
    except Exception:
        pass
    return f"Slack thread {key.channel_id}/{ts}"
