from __future__ import annotations

import logging
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError

from omnigent_slack.auth_manager import pack_user_key
from omnigent_slack.dispatcher import ThreadTurnDispatcher
from omnigent_slack.models import SlackTurn, ThreadKey
from omnigent_slack.omnigent import (
    AuthRequiredError,
    HostUnavailableError,
    OmnigentClientPool,
    ServerUnreachableError,
    extract_assistant_text,
    extract_delta,
    extract_error_text,
)
from omnigent_slack.setup import SetupFlow, host_unavailable_text
from omnigent_slack.store import SQLiteStore
from omnigent_slack.text import strip_bot_mention, truncate_for_slack


class SlackStreamProtocol(Protocol):
    async def append(self, *, markdown_text: str) -> Any: ...

    async def stop(self, *, markdown_text: str | None = ...) -> Any: ...


class SlackClientProtocol(Protocol):
    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_stream(self, **kwargs: Any) -> SlackStreamProtocol: ...


# Immediate acknowledgement shown while the session spins up and before the
# first streamed tokens arrive; deleted once real content starts streaming.
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

    async def _stop_current(self, markdown_text: str | None) -> None:
        assert self._stream is not None
        if markdown_text:
            await self._stream.stop(markdown_text=markdown_text)
        else:
            await self._stream.stop()


class SlackOmnigentService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        pool: OmnigentClientPool,
        setup: SetupFlow,
        server_url: str,
        bot_user_id: str | None = None,
    ) -> None:
        self._store = store
        self._pool = pool
        self._setup = setup
        # The one operator-configured Omnigent server. Always the routing
        # target — any server_url persisted on an older config/session row is
        # ignored, so a config change points every thread at the new server.
        self._server_url = server_url
        self._bot_user_id = bot_user_id
        self._dispatcher = ThreadTurnDispatcher(self._run_turn)
        self._logger = logging.getLogger(__name__)

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

        self._logger.info("Accepted Slack app_mention thread=%s chars=%s", key.display(), len(text))
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
            # session for now — silently ignore it.
            if record.owner_user_id and record.owner_user_id != requester:
                self._logger.info(
                    "Ignoring follow-up from non-owner thread=%s owner=%s requester=%s",
                    key.display(),
                    record.owner_user_id,
                    requester,
                )
                return
            await self._dispatcher.enqueue(
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
        # first tokens flush, so post a lightweight placeholder now and delete it
        # once real content starts streaming (or when the turn ends).
        ack_ts = await self._post_ack(turn.slack_client, turn.key)

        record = await self._store.get_session(turn.key)
        session_id = record.session_id if record is not None else None
        if session_id is None:
            if not turn.create_if_missing:
                self._logger.info(
                    "No session found and creation disabled thread=%s",
                    turn.key.display(),
                )
                return
            try:
                session_id = await omnigent.create_session(turn.agent_id, turn.title)
                runner_id = await omnigent.launch_runner(
                    session_id,
                    workspace=turn.workspace or "",
                    host_id=turn.host_id,
                )
            except AuthRequiredError:
                self._logger.info("Auth required thread=%s; prompting re-login", turn.key.display())
                await self._clear_ack(turn.slack_client, turn.key, ack_ts)
                await self._post_reply(turn.slack_client, turn.key, _AUTH_REQUIRED_TEXT)
                return
            except ServerUnreachableError:
                self._logger.info("Server unreachable thread=%s", turn.key.display())
                await self._clear_ack(turn.slack_client, turn.key, ack_ts)
                await self._post_reply(turn.slack_client, turn.key, _SERVER_UNREACHABLE_TEXT)
                return
            except HostUnavailableError:
                self._logger.info("Host unavailable thread=%s", turn.key.display())
                await self._clear_ack(turn.slack_client, turn.key, ack_ts)
                await self._post_reply(
                    turn.slack_client, turn.key, host_unavailable_text(self._server_url)
                )
                return
            except Exception as exc:
                # Any other failure spinning up the session (e.g. a 500 from
                # create_session/launch_runner surfaced as OmnigentError) must
                # still clear the placeholder and report — otherwise the thread
                # is stranded showing "Working on it…".
                self._logger.exception(
                    "Failed to start Omnigent session thread=%s", turn.key.display()
                )
                await self._clear_ack(turn.slack_client, turn.key, ack_ts)
                await self._post_failure_reply(turn.slack_client, turn.key, str(exc))
                return
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
        else:
            self._logger.info(
                "Using existing Omnigent session thread=%s session_id=%s",
                turn.key.display(),
                session_id,
            )

        slack_client = turn.slack_client

        # Stream the reply live: append each delta and finalize with a stop.
        # Slack renders markdown_text server-side and owns chunking, so there's
        # no mrkdwn conversion, no progress-edit throttle, and no msg_too_long
        # handling on our side. _LiveReply transparently opens a fresh streaming
        # message if Slack finalizes one mid-turn, so a long turn keeps streaming
        # across as many messages as it needs.
        reply = _LiveReply(slack_client, turn.key, recipient_user_id=turn.owner_user_id)

        streamed_text = ""
        final_text: str | None = None
        error_text: str | None = None

        try:
            async for omnigent_event in omnigent.run_turn(
                session_id, turn.text, workspace=turn.workspace, host_id=turn.host_id
            ):
                delta = extract_delta(omnigent_event)
                if delta:
                    # Drop the placeholder only once an append actually flushes to
                    # Slack (the SDK buffers deltas in memory first). Deleting it
                    # any earlier would leave the thread empty for the seconds
                    # until the streamed message is really on screen.
                    streamed_text += delta
                    if await reply.append(delta):
                        await self._clear_ack(slack_client, turn.key, ack_ts)
                        ack_ts = None

                item_text = extract_assistant_text(omnigent_event)
                if item_text:
                    final_text = item_text

                event_error = extract_error_text(omnigent_event)
                if event_error:
                    error_text = event_error
        except AuthRequiredError:
            self._logger.info("Auth required mid-turn thread=%s", turn.key.display())
            await self._clear_ack(slack_client, turn.key, ack_ts)
            await reply.stop(_AUTH_REQUIRED_TEXT)
            return
        except ServerUnreachableError:
            self._logger.info("Server unreachable mid-turn thread=%s", turn.key.display())
            await self._clear_ack(slack_client, turn.key, ack_ts)
            await reply.stop(_SERVER_UNREACHABLE_TEXT)
            return
        except HostUnavailableError:
            self._logger.info("Host unavailable mid-turn thread=%s", turn.key.display())
            await self._clear_ack(slack_client, turn.key, ack_ts)
            await reply.stop(host_unavailable_text(self._server_url))
            return
        except Exception as exc:
            self._logger.exception("Omnigent turn failed for %s", turn.key.display())
            error_text = str(exc)

        # The full answer is whatever streamed; if the model reported a final
        # item that adds text beyond the deltas, append only the remainder so we
        # don't duplicate what already streamed. When nothing streamed, fall back
        # to the latest assistant item.
        tail = ""
        if final_text and final_text.startswith(streamed_text):
            tail = final_text[len(streamed_text) :]
        elif final_text and not streamed_text:
            tail = final_text
        if not streamed_text and not tail:
            tail = (await omnigent.latest_assistant_text(session_id)) or ""

        if streamed_text or tail:
            await reply.stop(tail or None)
            if error_text:
                await self._post_failure_reply(slack_client, turn.key, error_text)
        else:
            fallback = (
                f"Omnigent request failed: {error_text}"
                if error_text
                else "Omnigent completed without returning response text."
            )
            await reply.stop(fallback)

        # Clear the placeholder only after final delivery. A short answer buffers
        # entirely in the SDK and doesn't reach Slack until stop() flushes it, so
        # deleting the placeholder any earlier would leave a gap where the thread
        # shows nothing.
        await self._clear_ack(slack_client, turn.key, ack_ts)
        ack_ts = None

        self._logger.info(
            "Completed Slack turn thread=%s session_id=%s streamed_chars=%s segments=%s errored=%s",
            turn.key.display(),
            session_id,
            len(streamed_text),
            reply.segments,
            bool(error_text),
        )

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

    async def _clear_ack(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        ack_ts: str | None,
    ) -> None:
        # Best-effort: a failed delete must not abort the turn or clobber the
        # streamed answer.
        if not ack_ts:
            return
        try:
            await client.chat_delete(channel=key.channel_id, ts=ack_ts)
        except Exception:
            self._logger.warning("Ack delete failed thread=%s; continuing", key.display())

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
