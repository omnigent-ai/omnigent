from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from omnigent_slack.models import ThreadKey
from omnigent_slack.omnigent import OutputFile
from omnigent_slack.text import GENERIC_FAILURE_TEXT, truncate_for_slack

if TYPE_CHECKING:
    from omnigent_slack.streaming import SlackClientProtocol

# Status → checkbox glyph for the rendered todo list.
_TODO_MARK = {
    "completed": ":white_check_mark:",
    "in_progress": ":hourglass_flowing_sand:",
    "pending": ":white_large_square:",
}


def format_todos(todos: list[dict[str, Any]]) -> str | None:
    """Render a todo-list update as a Slack message, or ``None`` if empty.

    Uses ``activeForm`` (the gerund) for the in-progress item and ``content``
    otherwise, mirroring how Claude Code presents its own list.
    """
    lines: list[str] = []
    for todo in todos:
        status = str(todo.get("status") or "pending")
        mark = _TODO_MARK.get(status, ":white_large_square:")
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
    return truncate_for_slack("*Plan*\n" + "\n".join(lines))


def format_output_file(file: OutputFile) -> str:
    """Render a produced-file notice."""
    name = file.filename or file.file_id
    return f":page_facing_up: Produced a file: *{name}*"


def format_policy_denied(reason: str) -> str:
    """Render a policy-DENY notice (the block-without-asking counterpart)."""
    return f":no_entry: Blocked by policy: {truncate_for_slack(reason, limit=2000)}"


class SlackNotifier:
    """All the bot's outbound Slack messages in one place.

    Thin wrappers over the Slack client: the ack placeholder, plain/failure
    thread replies, ephemeral ("only visible to you") notices, the in-place todo
    plan message, and the two owner-facing deflection notices. The Slack
    ``client`` is passed per-call (it's per-turn/per-event, not fixed); the
    notifier only holds the logger and server URL. Best-effort throughout — a
    failed side-channel post must never abort turn handling.
    """

    def __init__(self, *, server_url: str, logger: logging.Logger) -> None:
        self._server_url = server_url
        self._logger = logger

    async def post_ack(self, client: SlackClientProtocol, key: ThreadKey, text: str) -> str | None:
        # Best-effort: a failed ack must not abort the turn.
        try:
            response = await client.chat_postMessage(
                channel=key.channel_id, thread_ts=key.reply_ts, text=text
            )
        except Exception:
            self._logger.warning("Ack post failed thread=%s; continuing", key.display())
            return None
        ts = response.get("ts")
        return str(ts) if ts else None

    async def post_reply(self, client: SlackClientProtocol, key: ThreadKey, text: str) -> None:
        await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.reply_ts,
            text=truncate_for_slack(text),
        )

    async def post_failure_reply(self, client: SlackClientProtocol, key: ThreadKey) -> None:
        # Post the failure as its own thread reply so the streamed answer stays
        # intact. A GENERIC message only — the raw error detail is logged
        # server-side and never echoed to the channel (it can carry stack traces
        # / internal paths, and the thread is visible to everyone).
        await client.chat_postMessage(
            channel=key.channel_id,
            thread_ts=key.reply_ts,
            text=GENERIC_FAILURE_TEXT,
        )

    async def post_session_info(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        *,
        harness: str | None,
        agent_name: str | None,
        workspace: str | None,
        session_id: str,
    ) -> None:
        # Posted once when a session is created — the first durable message in the
        # thread, orienting the user to what they're talking to and linking to the
        # web UI. Best-effort: a failed post must not abort the turn.
        agent = agent_name or "agent"
        harness_note = f" ({harness})" if harness else ""
        lines = [f":robot_face: *{agent}*{harness_note}"]
        if workspace:
            lines.append(f":file_folder: `{workspace}`")
        lines.append(
            f":globe_with_meridians: <{self._session_web_link(session_id)}|Open in Omnigent>"
        )
        try:
            await client.chat_postMessage(
                channel=key.channel_id,
                thread_ts=key.reply_ts,
                text="\n".join(lines),
            )
        except Exception:
            self._logger.warning("Session-info post failed thread=%s; continuing", key.display())

    async def post_ephemeral(
        self, client: SlackClientProtocol, key: ThreadKey, user_id: str, text: str
    ) -> None:
        # Best-effort "Only visible to you" note, anchored in-thread. Used to
        # explain privately why a message wasn't acted on, without cluttering the
        # thread. A failed post must never abort handling.
        try:
            await client.chat_postEphemeral(
                channel=key.channel_id,
                user=user_id,
                thread_ts=key.reply_ts,
                text=text,
            )
        except Exception:
            self._logger.warning("Ephemeral notice failed thread=%s; continuing", key.display())

    async def post_or_update_todos(
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
                    channel=key.channel_id, thread_ts=key.reply_ts, text=text
                )
                ts = response.get("ts")
                return str(ts) if ts else None
            await client.chat_update(channel=key.channel_id, ts=todos_ts, text=text)
            return todos_ts
        except Exception:
            self._logger.warning("Todo update failed thread=%s; continuing", key.display())
            return todos_ts

    async def notify_non_owner(
        self, client: SlackClientProtocol, key: ThreadKey, user_id: str
    ) -> None:
        await self.post_ephemeral(
            client,
            key,
            user_id,
            "This thread's Omnigent session belongs to whoever started it, so I "
            "can't add your message to it. Start a new thread by mentioning me "
            "(or DM me) to get your own session.",
        )

    async def notify_thread_busy(
        self,
        client: SlackClientProtocol,
        key: ThreadKey,
        user_id: str,
        *,
        needs_action: bool,
        session_id: str | None,
    ) -> None:
        """Tell the owner their message can't run because the server is busy.

        Mirrors the web UI's two "can't send now" states: (a) ``needs_action`` —
        the session is parked awaiting a decision, so the user must answer the
        pending request (in Slack above, or the web UI); (b) otherwise the server
        is running/waiting, so wait for the reply or interrupt in the web UI. The
        message was NOT run and is NOT queued — a message to an idle thread runs
        normally, so re-sending once the session frees works.
        """
        link = self._session_web_link(session_id) if session_id else None
        if needs_action:
            text = (
                ":hourglass: I'm waiting on your response to the request above before I can "
                "continue. Answer it here"
            )
            text += f", or in the <{link}|web UI>." if link else "."
        else:
            text = (
                ":hourglass: I'm still working on your previous message in this thread — "
                "I handle one at a time here, so send this again once I've replied"
            )
            text += f", or wait / interrupt in the <{link}|web UI>." if link else "."
        await self.post_ephemeral(client, key, user_id, text)

    def _session_web_link(self, session_id: str) -> str:
        # Link to the session's conversation page in the Omnigent web UI, where a
        # user can continue a thread that's mid-turn in Slack (the web UI accepts
        # concurrent input and shows any pending actions).
        base = self._server_url.rstrip("/")
        return f"{base}/c/{session_id}"
