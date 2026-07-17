from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from omnigent_slack.approvals import (
    ElicitationCoordinator,
    Verdict,
    elicitation_card_blocks,
    resolved_card_blocks,
)
from omnigent_slack.auth_manager import pack_user_key
from omnigent_slack.dispatcher import ThreadTurnDispatcher
from omnigent_slack.models import SlackTurn, ThreadKey
from omnigent_slack.notifications import (
    format_output_file,
    format_policy_denied,
    format_todos,
)
from omnigent_slack.omnigent import (
    AuthRequiredError,
    ElicitationRequest,
    HostUnavailableError,
    OmnigentClient,
    OmnigentClientPool,
    ServerUnreachableError,
    extract_assistant_text,
    extract_delta,
    extract_elicitation_request,
    extract_error_text,
    extract_output_file,
    extract_policy_denied,
    extract_todos,
)
from omnigent_slack.setup import SetupFlow, host_unavailable_text
from omnigent_slack.store import SQLiteStore
from omnigent_slack.text import strip_bot_mention, truncate_for_slack


class SlackStreamProtocol(Protocol):
    async def append(self, *, markdown_text: str) -> Any: ...

    async def stop(self, *, markdown_text: str | None = ...) -> Any: ...


class SlackClientProtocol(Protocol):
    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_stream(self, **kwargs: Any) -> SlackStreamProtocol: ...


# Immediate acknowledgement shown while the session spins up and while the agent
# works before the first streamed tokens arrive. Deleted only once real content
# is actually on screen — on the first flushed delta, or after the finalizing
# stop() for a buffered answer — so the thread never shows an empty gap between
# the placeholder vanishing and the reply appearing.
_ACK_TEXT = "_Working on it…_"

_SERVER_UNREACHABLE_TEXT = (
    ":warning: I couldn't reach your Omnigent server. If it moved or is "
    "down, run /omnigent to reconfigure."
)

# Shown when the server rejects the request as unauthenticated — the user's
# delegated login is missing or expired (e.g. the bot restarted and in-memory
# tokens were lost). They re-authenticate by running /omnigent.
_AUTH_REQUIRED_TEXT = (
    ":lock: Your Omnigent login has expired or isn't set up. Run /omnigent to log in again."
)

# Slack streaming messages have a limited lifetime: after a stretch with no
# activity Slack finalizes the message itself, and any further append/stop then
# fails with this error. A long-running turn (waiting on a sub-agent, a slow
# tool) can outlast that window, so the bot opens a fresh streaming reply and
# continues into it rather than treating this as a turn failure.
_STREAM_CLOSED_ERROR = "message_not_in_streaming_state"

# How often, while awaiting a Slack Approve/Deny click, to check whether the
# elicitation was resolved elsewhere (web UI, another client) so the turn can
# stop waiting and continue instead of blocking to the coordinator timeout.
_EXTERNAL_RESOLVE_POLL_SECONDS = 3.0

# Sentinel: the elicitation was resolved outside Slack, so the bot must NOT post
# its own verdict — just continue the turn.
_RESOLVED_EXTERNALLY = object()


class _TurnAborted(Exception):
    """A turn can't proceed; ``text`` is the user-facing reason to deliver."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass
class _StreamState:
    """Mutable per-turn state threaded through the stream event dispatch."""

    # Timestamp of the live plan/todo message, edited in place across updates.
    todos_ts: str | None = None
    # In-band ``response.error`` text captured for finalization.
    error_text: str | None = None
    # Set when a known error was delivered mid-stream and the turn should stop.
    aborted: bool = False


def _turn_error_text(exc: BaseException, server_url: str) -> str | None:
    """User-facing message for a known startup/turn error, else ``None``.

    Single source of truth shared by the session-creation and mid-turn error
    paths so the two stay in sync.
    """
    if isinstance(exc, AuthRequiredError):
        return _AUTH_REQUIRED_TEXT
    if isinstance(exc, ServerUnreachableError):
        return _SERVER_UNREACHABLE_TEXT
    if isinstance(exc, HostUnavailableError):
        return host_unavailable_text(server_url)
    return None


def _is_stream_closed_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, SlackApiError)
        and getattr(exc.response, "get", lambda _k: None)("error") == _STREAM_CLOSED_ERROR
    )


class _LiveReply:
    """A streaming Slack reply that reopens itself when Slack finalizes it.

    Slack finalizes a streaming message after an idle stretch, and a long turn
    (parked on a sub-agent, a slow tool) can outlast that window. When an
    append or stop hits ``message_not_in_streaming_state``, this opens a fresh
    streaming message in the same thread and continues, so the answer keeps
    streaming live across as many messages as the turn needs. The already-
    delivered messages stay intact — Slack has finalized them.
    """

    def __init__(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        *,
        recipient_user_id: str,
    ) -> None:
        self._client = client
        self._key = key
        self._recipient_user_id = recipient_user_id
        self._stream: SlackStreamProtocol | None = None
        # Number of streaming messages opened; >1 means the reply was split
        # because Slack closed an earlier segment mid-turn.
        self.segments = 0

    async def _open(self) -> SlackStreamProtocol:
        self._stream = await self._client.chat_stream(
            channel=self._key.channel_id,
            thread_ts=self._key.thread_ts,
            recipient_user_id=self._recipient_user_id,
            recipient_team_id=self._key.team_id,
        )
        self.segments += 1
        return self._stream

    async def append(self, markdown_text: str) -> bool:
        # The SDK buffers in memory and only calls Slack once the buffer fills,
        # returning a response on that flush and None while still buffering.
        # Return whether this append actually put text on screen so the caller
        # can hold the placeholder until the streamed message is visible.
        stream = self._stream or await self._open()
        try:
            flushed = await stream.append(markdown_text=markdown_text)
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise
            # Slack finalized the message out from under us; continue the answer
            # in a fresh streaming reply so nothing stalls or is lost.
            flushed = await (await self._open()).append(markdown_text=markdown_text)
        return flushed is not None

    async def stop(self, markdown_text: str | None = None) -> None:
        # chat.stopStream rejects empty text, so only pass markdown_text when
        # there is some. Nothing ever streamed and no tail to deliver → no-op.
        if self._stream is None:
            if not markdown_text:
                return
            await self._open()
        try:
            await self._stop_current(markdown_text)
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise
            if markdown_text:
                await self._open()
                await self._stop_current(markdown_text)

    async def seal(self) -> None:
        """Finalize the current streaming segment so a later message sorts after it.

        Slack orders messages by the timestamp fixed when a streaming message
        opens, so text appended to a long-lived stream stays anchored there.
        Before posting any out-of-band message mid-turn (an approval card, a
        policy/file notice), seal the current answer segment: it ends here, the
        out-of-band message sorts after it, and the next append opens a fresh
        segment that sorts after *that* — keeping chronological order across an
        interruption. No-op when nothing is streaming.
        """
        if self._stream is None:
            return
        stream = self._stream
        # Drop the reference first so the next append opens a fresh segment even
        # if the stop below races a Slack-side finalize.
        self._stream = None
        try:
            await stream.stop()
        except SlackApiError as exc:
            if not _is_stream_closed_error(exc):
                raise

    async def _stop_current(self, markdown_text: str | None) -> None:
        assert self._stream is not None
        if markdown_text:
            await self._stream.stop(markdown_text=markdown_text)
        else:
            await self._stream.stop()


class _AnswerReply:
    """Owns one turn's streamed answer: the live reply, the accumulated text,
    the "Working on it…" placeholder, and the interruption/finalization rules.

    Centralizes three invariants that were previously enforced by convention
    inside the turn loop:

    - **Placeholder visibility.** The ``ack`` is removed only once real content
      is on screen — the first append that actually flushes to Slack, or the
      finalizing ``stop()`` for a buffered answer — so the thread never shows a
      gap between the placeholder vanishing and the reply appearing.
    - **Seal ⇒ forget.** Sealing a segment before an out-of-band message
      (approval card, notice) also resets the accumulated text, so the tail
      reconciliation only ever considers the current segment.
    - **Tail reconciliation.** The final answer is whatever streamed; if the
      model reported a final item beyond the deltas, only the remainder is
      appended, and a no-delta answer falls back to the committed item.
    """

    def __init__(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        *,
        recipient_user_id: str,
        ack_ts: str | None,
        logger: logging.Logger,
    ) -> None:
        self._reply = _LiveReply(client, key, recipient_user_id=recipient_user_id)
        self._client = client
        self._key = key
        self._ack_ts = ack_ts
        self._logger = logger
        self._streamed = ""
        self._final: str | None = None

    @property
    def segments(self) -> int:
        return self._reply.segments

    @property
    def streamed_len(self) -> int:
        return len(self._streamed)

    async def add_delta(self, delta: str) -> None:
        # Append the delta; the SDK buffers and only flushes to Slack once the
        # buffer fills. Clear the placeholder only on the flush that actually
        # puts content on screen — never while still buffering — so there's no
        # empty gap.
        self._streamed += delta
        if await self._reply.append(delta):
            await self._clear_ack()

    def set_final(self, text: str) -> None:
        self._final = text

    async def seal_for_interruption(self) -> None:
        # Before an out-of-band message: drop the placeholder (it would sit
        # stale above the interruption for the whole wait), finalize the current
        # segment so the interruption sorts after it, and forget the accumulated
        # text so the next segment reconciles independently.
        await self._clear_ack()
        await self._reply.seal()
        self._streamed, self._final = "", None

    async def finalize(self, *, error_text: str | None) -> bool:
        # Deliver the answer tail, then clear the placeholder only after that
        # final flush (a short buffered answer becomes visible only at stop()).
        # Returns whether a real answer was delivered — when an error also
        # occurred, the caller posts the failure as a separate reply so the
        # answer stays intact; when nothing was produced, the error IS the reply.
        tail = self._tail()
        delivered_answer = bool(self._streamed or tail)
        if delivered_answer:
            await self._reply.stop(tail or None)
        else:
            await self._reply.stop(
                f"Omnigent request failed: {error_text}"
                if error_text
                else "Omnigent completed without returning response text."
            )
        await self._clear_ack()
        return delivered_answer

    def _tail(self) -> str:
        if self._final and self._final.startswith(self._streamed):
            return self._final[len(self._streamed) :]
        if self._final and not self._streamed:
            return self._final
        return ""

    def needs_fallback_text(self) -> bool:
        # True when nothing streamed and no final item — the caller should fetch
        # the latest assistant text from the server as a last resort.
        return not self._streamed and not self._tail()

    def set_fallback_text(self, text: str) -> None:
        self._final = text

    async def stop_with(self, text: str) -> None:
        # Terminal notice (auth/unreachable/host errors, or a no-op abort): clear
        # the placeholder, then deliver ``text`` as a plain thread reply. Empty
        # text is a silent stop (nothing to say). A notice is not a streamed
        # answer, so it goes via a normal message, not the streaming reply.
        await self._clear_ack()
        if text:
            await self._client.chat_postMessage(
                channel=self._key.channel_id,
                thread_ts=self._key.thread_ts,
                text=truncate_for_slack(text),
            )

    async def _clear_ack(self) -> None:
        # Best-effort, idempotent: a failed delete must not abort the turn.
        if not self._ack_ts:
            return
        ack_ts, self._ack_ts = self._ack_ts, None
        try:
            await self._client.chat_delete(channel=self._key.channel_id, ts=ack_ts)
        except Exception:
            self._logger.warning("Ack delete failed thread=%s; continuing", self._key.display())


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
        # Bridges a parked turn (blocked awaiting the user) to the button/form
        # interaction that answers it. Shared with the block-action handler.
        self._elicitations = elicitations or ElicitationCoordinator()
        # How often, while awaiting a Slack click, to poll for external
        # resolution (overridable in tests to avoid real-time waits).
        self._external_resolve_poll_seconds = _EXTERNAL_RESOLVE_POLL_SECONDS
        self._dispatcher = ThreadTurnDispatcher(self._run_turn)
        self._logger = logging.getLogger(__name__)

    @property
    def elicitations(self) -> ElicitationCoordinator:
        return self._elicitations

    async def shutdown(self) -> None:
        await self._dispatcher.shutdown()

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
                thread_ts=key.thread_ts,
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
            in_channel=not _is_direct_message(event),
        )

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

        if not _is_direct_message(event):
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

        # A DM has no human-only discussion to gate on: the whole thread maps to
        # one Omnigent session, created on the first message and reused after.
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
        record = await self._store.get_session(key)

        if record is not None:
            # An existing thread belongs to whoever started it. A follow-up from
            # a different user (only possible in a channel) is not added to the
            # session. Tell that user — privately, so the thread isn't cluttered
            # — why nothing happened and how to get their own session.
            if record.owner_user_id and record.owner_user_id != requester:
                self._logger.info(
                    "Ignoring follow-up from non-owner thread=%s owner=%s requester=%s",
                    key.display(),
                    record.owner_user_id,
                    requester,
                )
                await self._notify_non_owner(client, key, requester)
                return
            # If the session is parked awaiting a decision, a new message can't
            # proceed until that's answered. Nudge the user (privately) to
            # resolve the pending request rather than silently queueing behind it.
            if await self._notify_if_awaiting_input(client, key, requester):
                return
            will_wait = await self._dispatcher.enqueue(
                SlackTurn(
                    key=key,
                    text=text,
                    user_id=requester,
                    create_if_missing=False,
                    title=_session_title(event, text),
                    slack_client=client,
                    agent_id="",
                    owner_user_id=record.owner_user_id or requester,
                    workspace=record.workspace,
                    host_id=record.host_id,
                )
            )
            if will_wait:
                await self._notify_queued(client, key, requester)
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
                thread_ts=key.thread_ts,
                in_channel=in_channel,
            )
            return

        await self._dispatcher.enqueue(
            SlackTurn(
                key=key,
                text=text,
                user_id=requester,
                create_if_missing=True,
                title=_session_title(event, text),
                slack_client=client,
                agent_id=config.agent_id,
                owner_user_id=requester,
                workspace=config.workspace,
                host_id=config.host_id,
            )
        )

    async def _run_turn(self, turn: SlackTurn) -> None:
        self._logger.info("Starting turn thread=%s chars=%s", turn.key.display(), len(turn.text))
        omnigent = await self._pool.get(
            self._server_url, pack_user_key(turn.key.team_id, turn.user_id)
        )

        # Acknowledge immediately: a new session's create + runner launch can take
        # several seconds, and the streamed reply message only appears once the
        # first tokens flush, so post a lightweight placeholder now.
        ack_ts = await self._post_ack(turn.slack_client, turn.key)
        reply = _AnswerReply(
            turn.slack_client,
            turn.key,
            recipient_user_id=turn.owner_user_id,
            ack_ts=ack_ts,
            logger=self._logger,
        )

        try:
            session_id = await self._ensure_session(turn, omnigent, reply)
        except _TurnAborted as aborted:
            await reply.stop_with(aborted.text)
            return
        if session_id is None:
            # No session and creation disabled (a follow-up on a dead thread):
            # nothing to run. Drop the placeholder so it doesn't linger.
            await reply.stop_with("")
            return

        # Baseline the newest assistant message BEFORE the turn runs, so the
        # no-delta fallback below can tell this turn's answer from a prior one.
        baseline = await omnigent.latest_assistant_message(session_id)
        baseline_id = baseline[0] if baseline else None

        try:
            error_text = await self._stream_turn(turn, omnigent, session_id, reply)
        except _TurnAborted:
            # A known mid-stream error already delivered its message and stopped
            # the reply; nothing left to finalize.
            return

        if reply.needs_fallback_text():
            # The answer may have arrived only as a committed item we didn't see
            # streamed. Recover it — but ONLY if it's newer than the baseline, so
            # a turn that produced no answer (e.g. a denied approval) doesn't
            # resurrect the previous turn's message.
            latest = await omnigent.latest_assistant_message(session_id)
            if latest is not None and latest[0] != baseline_id:
                reply.set_fallback_text(latest[1])
        delivered_answer = await reply.finalize(error_text=error_text)
        if error_text and delivered_answer:
            await self._post_failure_reply(turn.slack_client, turn.key, error_text)

        self._logger.info(
            "Completed Slack turn thread=%s session=%s streamed_chars=%s segments=%s errored=%s",
            turn.key.display(),
            session_id,
            reply.streamed_len,
            reply.segments,
            bool(error_text),
        )

    async def _ensure_session(
        self, turn: SlackTurn, omnigent: OmnigentClient, reply: _AnswerReply
    ) -> str | None:
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
        except (AuthRequiredError, ServerUnreachableError, HostUnavailableError) as exc:
            self._logger.info("Session startup failed thread=%s: %s", turn.key.display(), exc)
            raise _TurnAborted(_turn_error_text(exc, self._server_url) or str(exc)) from exc
        except Exception as exc:
            # Any other startup failure (e.g. a 500 surfaced as OmnigentError)
            # must still report rather than strand the thread on "Working on it…".
            self._logger.exception(
                "Failed to start Omnigent session thread=%s", turn.key.display()
            )
            raise _TurnAborted(f":warning: Omnigent request failed: {exc}") from exc

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
        return session_id

    async def _stream_turn(
        self,
        turn: SlackTurn,
        omnigent: OmnigentClient,
        session_id: str,
        reply: _AnswerReply,
    ) -> str | None:
        """Stream the turn's events into ``reply``. Returns any error text.

        Slack renders markdown server-side and owns chunking, so there's no
        mrkdwn conversion or msg_too_long handling here — just event routing.
        A known auth/reachability error aborts the turn with a user-facing
        message (delivered here); any other exception, or an in-band
        ``response.error`` event, becomes error text used at finalization.
        """
        # Timestamp of the live plan/todo message, edited in place across updates.
        state = _StreamState()
        try:
            async for event in omnigent.run_turn(
                session_id, turn.text, workspace=turn.workspace, host_id=turn.host_id
            ):
                await self._dispatch_stream_event(event, turn, omnigent, session_id, reply, state)
        except (AuthRequiredError, ServerUnreachableError, HostUnavailableError) as exc:
            self._logger.info("Turn error mid-stream thread=%s: %s", turn.key.display(), exc)
            await reply.stop_with(_turn_error_text(exc, self._server_url) or str(exc))
            state.aborted = True
        except Exception as exc:
            self._logger.exception("Omnigent turn failed for %s", turn.key.display())
            state.error_text = str(exc)
        if state.aborted:
            raise _TurnAborted("")  # already delivered; signal the caller to stop
        return state.error_text

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
            await reply.add_delta(delta)
            return

        elicitation = extract_elicitation_request(event, session_id)
        if elicitation is not None:
            # Parked awaiting the user: seal the answer so far (it sorts before
            # the card), then post the card and block for the verdict. The stream
            # stays open (session sits in `waiting`), and resumed text opens a
            # fresh segment after the card.
            await reply.seal_for_interruption()
            await self._handle_elicitation(omnigent, client, turn.key, elicitation)
            return

        denied_reason = extract_policy_denied(event)
        if denied_reason is not None:
            await reply.seal_for_interruption()
            await self._post_reply(client, turn.key, format_policy_denied(denied_reason))
            return

        output_file = extract_output_file(event)
        if output_file is not None:
            await reply.seal_for_interruption()
            await self._post_reply(client, turn.key, format_output_file(output_file))
            return

        todos = extract_todos(event)
        if todos is not None:
            # The first plan post is a new out-of-band message → seal before it;
            # later updates edit it in place (no boundary, no fragmentation).
            if state.todos_ts is None:
                await reply.seal_for_interruption()
            state.todos_ts = await self._post_or_update_todos(
                client, turn.key, todos, state.todos_ts
            )
            return

        item_text = extract_assistant_text(event)
        if item_text:
            reply.set_final(item_text)

        event_error = extract_error_text(event)
        if event_error:
            state.error_text = event_error

    async def _post_ack(self, client: SlackClientProtocol, key: ThreadKey) -> str | None:
        # Best-effort: a failed ack must not abort the turn.
        try:
            response = await client.chat_postMessage(
                channel=key.channel_id,
                thread_ts=key.thread_ts,
                text=_ACK_TEXT,
            )
        except Exception:
            self._logger.warning("Ack post failed thread=%s; continuing", key.display())
            return None
        ts = response.get("ts")
        return str(ts) if ts else None

    async def _post_or_update_todos(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        todos: list[dict[str, Any]],
        todos_ts: str | None,
    ) -> str | None:
        # Render the plan once and edit it in place on later updates so the
        # thread carries a single, current plan message rather than a pile of
        # snapshots. Best-effort throughout.
        text = format_todos(todos)
        if text is None:
            return todos_ts
        try:
            if todos_ts is None:
                response = await client.chat_postMessage(
                    channel=key.channel_id, thread_ts=key.thread_ts, text=text
                )
                ts = response.get("ts")
                return str(ts) if ts else None
            await client.chat_update(channel=key.channel_id, ts=todos_ts, text=text)
            return todos_ts
        except Exception:
            self._logger.warning("Todo update failed thread=%s; continuing", key.display())
            return todos_ts

    async def _post_reply(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        text: str,
    ) -> None:
        await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            text=truncate_for_slack(text),
        )

    async def _post_failure_reply(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        error_text: str,
    ) -> None:
        # Post the failure as its own thread reply so the streamed answer stays
        # intact.
        await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            text=f":warning: Omnigent request failed: {error_text}",
        )

    async def _notify_non_owner(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        user_id: str,
    ) -> None:
        # Best-effort ephemeral ("Only visible to you") note so the follow-up
        # author isn't left wondering why the bot went silent. Posted in-thread
        # so Slack anchors it to where they typed. A failed post is non-fatal.
        try:
            await client.chat_postEphemeral(
                channel=key.channel_id,
                user=user_id,
                thread_ts=key.thread_ts,
                text=(
                    "This Omnigent thread belongs to whoever started it, so I can't "
                    "add your message to it. Start a new thread by mentioning me "
                    "(or DM me) to get your own session."
                ),
            )
        except Exception:
            self._logger.warning(
                "Non-owner ephemeral notice failed thread=%s; continuing", key.display()
            )

    async def _notify_queued(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        user_id: str,
    ) -> None:
        # Best-effort ephemeral: the thread is already handling an earlier turn,
        # so this message waits its turn (the bot processes one at a time per
        # thread). Tells the user privately rather than leaving them wondering
        # why there's no immediate reply.
        try:
            await client.chat_postEphemeral(
                channel=key.channel_id,
                user=user_id,
                thread_ts=key.thread_ts,
                text=(
                    ":hourglass: I'm still working on your previous message in this "
                    "thread — I'll get to this one right after."
                ),
            )
        except Exception:
            self._logger.warning(
                "Queued ephemeral notice failed thread=%s; continuing", key.display()
            )

    async def _notify_if_awaiting_input(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        user_id: str,
    ) -> bool:
        """If the session is parked on a pending elicitation, nudge the user.

        Returns ``True`` when a pending request was found and the user was told
        (privately) to answer it first — the caller then skips enqueuing, since
        a new message can't proceed until the decision is made. Best-effort: any
        failure resolving the session or posting falls through to ``False`` so
        the message is handled normally rather than dropped.
        """
        record = await self._store.get_session(key)
        if record is None:
            return False
        omnigent = await self._pool.get(self._server_url, pack_user_key(key.team_id, user_id))
        pending = await omnigent.get_pending_elicitation(record.session_id)
        if pending is None:
            return False
        self._logger.info(
            "Message while awaiting input thread=%s elicitation_id=%s; nudging user",
            key.display(),
            pending.elicitation_id,
        )
        try:
            await client.chat_postEphemeral(
                channel=key.channel_id,
                user=user_id,
                thread_ts=key.thread_ts,
                text=(
                    ":hourglass: I'm waiting on your response to the request above "
                    "before I can continue. Answer that first, then send your message again."
                ),
            )
        except Exception:
            self._logger.warning(
                "Awaiting-input ephemeral failed thread=%s; continuing", key.display()
            )
        return True

    async def handle_elicitation_action(self, *, elicitation_id: str, verdict: Verdict) -> bool:
        """Deliver a button/form verdict to the waiting turn worker.

        Returns whether a live waiter received it — ``False`` means the request
        already expired or was answered, so the caller can tell the user.
        """
        return await self._elicitations.resolve(elicitation_id, verdict)

    def _session_link(self, session_id: str, elicitation_id: str) -> str:
        # Deep link to the elicitation's approve page in the Omnigent web UI, so
        # a user can resolve a request the bot can't render in Slack.
        base = self._server_url.rstrip("/")
        return f"{base}/approve/{session_id}/{elicitation_id}"

    async def _handle_elicitation(
        self,
        omnigent: OmnigentClient,
        client: SlackClientProtocol,
        key: ThreadKey,
        request: ElicitationRequest,
    ) -> None:
        """Post the elicitation card, wait for the answer, and resolve it.

        Renders a multiple-choice form (``AskUserQuestion``) or a binary
        Approve/Deny, blocks the turn worker until the user answers or the wait
        times out (a timeout declines so the server-side park doesn't hang
        either), then updates the card in place with the outcome and forwards
        the verdict — including any form selections as ``content``.

        For an elicitation the bot can't render (a ``url``-mode page or a
        request for typed input), it posts a link to resolve in the Omnigent web
        UI and returns without blocking — the user completes it there and the
        stream resumes (the turn stays alive via the idle grace window).
        """
        if not request.is_supported:
            await self._post_reply(
                client,
                key,
                (
                    ":link: Omnigent needs input I can't collect here "
                    f"({request.message}). Open the session to respond:\n"
                    f"{self._session_link(request.session_id, request.elicitation_id)}"
                ),
            )
            self._logger.info(
                "Unsupported elicitation surfaced as web link thread=%s elicitation_id=%s mode=%s",
                key.display(),
                request.elicitation_id,
                request.mode,
            )
            return

        self._logger.info(
            "Elicitation requested thread=%s elicitation_id=%s policy=%s form=%s",
            key.display(),
            request.elicitation_id,
            request.policy_name,
            request.is_form,
        )
        posted = await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            text="Omnigent needs your input to continue.",
            blocks=elicitation_card_blocks(request),
        )
        card_ts = posted.get("ts")

        verdict = await self._await_verdict_or_external(omnigent, request)
        if verdict is _RESOLVED_EXTERNALLY:
            # The user answered elsewhere (web UI, another client). The server
            # already has the verdict; don't post our own. Just clear the card
            # and let the turn continue.
            outcome = "Answered elsewhere"
        else:
            assert verdict is None or isinstance(verdict, Verdict)
            if verdict is None:
                # Nobody answered in time — decline so the server park releases.
                verdict = Verdict(accepted=False)
                outcome = "Timed out"
            elif request.is_form:
                # A form Submit is an accept with selections; Cancel is a decline.
                outcome = "Answered" if verdict.accepted else "Cancelled"
            else:
                outcome = "Approved" if verdict.accepted else "Denied"
            await omnigent.resolve_elicitation(
                request.session_id,
                request.elicitation_id,
                accepted=verdict.accepted,
                content=verdict.content,
            )
        self._logger.info(
            "Elicitation resolved thread=%s elicitation_id=%s outcome=%s",
            key.display(),
            request.elicitation_id,
            outcome,
        )

        if isinstance(card_ts, str):
            # Best-effort: replace the card with its outcome (no controls). A
            # failed update must not abort the turn.
            try:
                await client.chat_update(
                    channel=key.channel_id,
                    ts=card_ts,
                    text=f"Request {outcome.lower()}.",
                    blocks=resolved_card_blocks(request, outcome=outcome),
                )
            except Exception:
                self._logger.warning(
                    "Elicitation card update failed thread=%s; continuing", key.display()
                )

    async def _await_verdict_or_external(
        self, omnigent: OmnigentClient, request: ElicitationRequest
    ) -> Verdict | None | object:
        """Wait for a Slack button verdict OR external resolution.

        The turn worker blocks here on the Slack card, but the user may instead
        answer in the web UI (or another client). Since this worker isn't
        reading the stream while blocked, it can't see ``elicitation_resolved`` —
        so it also polls the server, and if the elicitation is no longer pending
        it returns ``_RESOLVED_EXTERNALLY`` to stop waiting (the verdict is
        already recorded server-side; posting our own would be wrong). Otherwise
        returns the :class:`Verdict` from the click, or ``None`` on timeout.

        Without this, a web-UI answer would leave the worker blocked until the
        coordinator timeout — wedging the whole thread's serial queue.
        """
        verdict_task = asyncio.ensure_future(
            self._elicitations.await_verdict(request.elicitation_id)
        )
        try:
            while True:
                done, _ = await asyncio.wait(
                    {verdict_task}, timeout=self._external_resolve_poll_seconds
                )
                if verdict_task in done:
                    return verdict_task.result()
                if not await omnigent.is_elicitation_pending(
                    request.session_id, request.elicitation_id
                ):
                    return _RESOLVED_EXTERNALLY
        finally:
            # Stop the coordinator waiter if we returned on the external path, so
            # a later stray click doesn't resolve a dead future.
            if not verdict_task.done():
                verdict_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await verdict_task

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
        event_id = body.get("event_id") or event.get("client_msg_id")
        return await self._store.claim_event(str(event_id) if event_id else None)

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


def _is_direct_message(event: dict[str, Any]) -> bool:
    # Slack marks 1:1 DMs with channel_type "im"; channel ids also start with
    # "D". Either signal means the message reached the bot directly rather than
    # via a channel, so no @-mention is needed to engage.
    if event.get("channel_type") == "im":
        return True
    return str(event.get("channel") or "").startswith("D")


def _team_id(body: dict[str, Any], event: dict[str, Any]) -> str:
    team_id = body.get("team_id") or event.get("team")
    if not team_id:
        raise ValueError("Slack event is missing team_id")
    return str(team_id)


def _session_title(event: dict[str, Any], text: str) -> str:
    channel = str(event.get("channel") or "channel")
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "thread")
    summary = truncate_for_slack(text, limit=80).replace("\n", " ")
    return f"Slack {channel}/{thread_ts}: {summary}"
