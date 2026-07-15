from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from omnigent_slack.dispatcher import ThreadTurnDispatcher
from omnigent_slack.models import SlackTurn, ThreadKey
from omnigent_slack.omnigent import (
    OmnigentClient,
    extract_assistant_text,
    extract_delta,
    extract_error_text,
)
from omnigent_slack.store import SQLiteStore
from omnigent_slack.text import (
    normalize_whitespace,
    split_for_slack,
    strip_bot_mention,
    to_mrkdwn,
    truncate_for_slack,
)


class SlackClientProtocol(Protocol):
    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]: ...

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]: ...


class SlackOmnigentService:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        omnigent: OmnigentClient,
        omnigent_agent_id: str,
        update_interval_seconds: float = 1.0,
        bot_user_id: str | None = None,
    ) -> None:
        self._store = store
        self._omnigent = omnigent
        self._omnigent_agent_id = omnigent_agent_id
        self._update_interval_seconds = update_interval_seconds
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
        if not await self._claim_event(body, event):
            self._logger.info(
                "Ignoring duplicate Slack app_mention event_id=%s",
                body.get("event_id") or event.get("client_msg_id"),
            )
            return
        bot_user_id = self._resolve_bot_user_id(context)
        if self._should_ignore_message(event, bot_user_id):
            self._logger.info(
                "Ignoring Slack app_mention subtype=%s bot_id=%s user=%s bot_user_id=%s",
                event.get("subtype"),
                event.get("bot_id"),
                event.get("user"),
                bot_user_id,
            )
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
        await self._dispatcher.enqueue(
            SlackTurn(
                key=key,
                text=text,
                user_id=str(event.get("user") or ""),
                create_if_missing=True,
                title=_session_title(event, text),
                slack_client=client,
            )
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
        if not await self._claim_event(body, event):
            self._logger.info(
                "Ignoring duplicate Slack message event_id=%s",
                body.get("event_id") or event.get("client_msg_id"),
            )
            return
        bot_user_id = self._resolve_bot_user_id(context)
        if self._should_ignore_message(event, bot_user_id):
            self._logger.info(
                "Ignoring Slack message subtype=%s bot_id=%s user=%s bot_user_id=%s",
                event.get("subtype"),
                event.get("bot_id"),
                event.get("user"),
                bot_user_id,
            )
            return

        raw_text = str(event.get("text") or "")
        if bot_user_id and f"<@{bot_user_id}" in raw_text:
            self._logger.info("Ignoring generic message containing bot mention")
            return

        team_id = _team_id(body, event)
        key = ThreadKey.from_event(team_id, event)
        if await self._store.get_session_id(key) is None:
            self._logger.info(
                "Ignoring Slack message with no Omnigent session thread=%s",
                key.display(),
            )
            return

        text = normalize_whitespace(raw_text)
        if not text:
            self._logger.info("Ignoring empty Slack message thread=%s", key.display())
            return

        self._logger.info(
            "Accepted Slack thread reply thread=%s chars=%s",
            key.display(),
            len(text),
        )
        await self._dispatcher.enqueue(
            SlackTurn(
                key=key,
                text=text,
                user_id=str(event.get("user") or ""),
                create_if_missing=False,
                title=_session_title(event, text),
                slack_client=client,
            )
        )

    async def _run_turn(self, turn: SlackTurn) -> None:
        self._logger.info("Starting turn thread=%s chars=%s", turn.key.display(), len(turn.text))
        session_id = await self._store.get_session_id(turn.key)
        if session_id is None:
            if not turn.create_if_missing:
                self._logger.info(
                    "No session found and creation disabled thread=%s",
                    turn.key.display(),
                )
                return
            session_id = await self._omnigent.create_session(self._omnigent_agent_id, turn.title)
            runner_id = await self._omnigent.bind_random_runner(session_id)
            await self._store.upsert_session(turn.key, session_id, turn.title)
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

        placeholder = await slack_client.chat_postMessage(
            channel=turn.key.channel_id,
            thread_ts=turn.key.thread_ts,
            text="Working...",
        )
        message_ts = str(placeholder.get("ts") or "")
        if not message_ts:
            self._logger.error("Slack placeholder response missing ts: %r", placeholder)
            return
        self._logger.info(
            "Posted Slack placeholder thread=%s message_ts=%s",
            turn.key.display(),
            message_ts,
        )

        streamed_text = ""
        final_text: str | None = None
        error_text: str | None = None
        last_update = 0.0

        try:
            async for omnigent_event in self._omnigent.run_turn(session_id, turn.text):
                delta = extract_delta(omnigent_event)
                if delta:
                    streamed_text += delta
                    self._logger.debug(
                        "Accumulated Omnigent delta thread=%s total_chars=%s",
                        turn.key.display(),
                        len(streamed_text),
                    )
                    now = time.monotonic()
                    if now - last_update >= self._update_interval_seconds:
                        # A progress edit is best-effort: a failure here (e.g. a
                        # transient Slack error) must not abort the turn or
                        # clobber the real answer delivered below.
                        try:
                            await self._update_slack(
                                slack_client, turn.key, message_ts, streamed_text
                            )
                        except Exception:
                            self._logger.warning(
                                "Slack progress update failed thread=%s; continuing",
                                turn.key.display(),
                                exc_info=True,
                            )
                        last_update = now

                item_text = extract_assistant_text(omnigent_event)
                if item_text:
                    final_text = item_text

                event_error = extract_error_text(omnigent_event)
                if event_error:
                    error_text = event_error
        except Exception as exc:
            self._logger.exception("Omnigent turn failed for %s", turn.key.display())
            error_text = str(exc)

        # Resolve the answer independently of any error so a failure never
        # erases what the user already saw stream in.
        if not final_text:
            final_text = streamed_text.strip() or await self._omnigent.latest_assistant_text(
                session_id
            )

        if final_text:
            # Deliver the real answer, then, if the turn also errored, report
            # the failure as a separate reply instead of overwriting it.
            await self._deliver_final(slack_client, turn.key, message_ts, final_text)
            if error_text:
                await self._post_failure_reply(slack_client, turn.key, error_text)
        else:
            # Nothing to preserve — surface the error (or a fallback) in the
            # placeholder itself.
            fallback = (
                f"Omnigent request failed: {error_text}"
                if error_text
                else "Omnigent completed without returning response text."
            )
            await self._deliver_final(slack_client, turn.key, message_ts, fallback)

        self._logger.info(
            "Completed Slack turn thread=%s session_id=%s final_chars=%s errored=%s",
            turn.key.display(),
            session_id,
            len(final_text or ""),
            bool(error_text),
        )

    async def _post_failure_reply(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        error_text: str,
    ) -> None:
        # Post the failure as its own thread reply so the already-delivered
        # answer stays intact. Keep it to a single message.
        await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.thread_ts,
            text=truncate_for_slack(f":warning: Omnigent request failed: {error_text}"),
        )

    async def _deliver_final(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        message_ts: str,
        text: str,
    ) -> None:
        # The server returns standard Markdown; convert it to Slack's mrkdwn
        # dialect before display. A single Slack message can't hold a long
        # answer, so split the converted text and edit the placeholder to the
        # first chunk, posting the rest as thread replies — delivering the full
        # answer instead of truncating it.
        chunks = split_for_slack(to_mrkdwn(text))
        await self._update_slack(client, key, message_ts, chunks[0])
        for chunk in chunks[1:]:
            await client.chat_postMessage(
                channel=key.channel_id,
                thread_ts=key.thread_ts,
                text=chunk,
            )
        if len(chunks) > 1:
            self._logger.info(
                "Delivered long Slack answer across parts thread=%s parts=%s",
                key.display(),
                len(chunks),
            )

    async def _update_slack(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        message_ts: str,
        text: str,
    ) -> None:
        self._logger.debug(
            "Updating Slack message thread=%s message_ts=%s chars=%s",
            key.display(),
            message_ts,
            len(text),
        )
        await client.chat_update(
            channel=key.channel_id,
            ts=message_ts,
            text=truncate_for_slack(text),
        )

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
