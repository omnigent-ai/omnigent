"""Work with an existing session from a script: ``omnigent session send|get|read``.

``send`` queues a message, optionally waiting for the reply; ``get`` reports the
session's state with the fields ``sys_session_get_info`` gives an agent; ``read``
prints its latest messages. With ``session create`` a plain script can run a
pool of workers without an agent session calling ``sys_session_*`` tools in a
loop. Requests act as the signed-in user, like the web UI, and each goes to the
replica holding the session's host.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import click
import httpx

from omnigent.session_create_cli import (
    message_event,
    message_refusal,
    response_object,
    server_error,
)
from omnigent.util.json_types import JsonObject

#: A message can wait on an approval policy before the server answers.
_SEND_TIMEOUT = httpx.Timeout(30.0, read=3660.0)
_READ_TIMEOUT = httpx.Timeout(30.0)
#: A reply wait starts with a quick check and backs off to this ceiling, so a long
#: turn costs about one session read every few seconds.
_WAIT_FIRST_INTERVAL_S = 1.0
_WAIT_MAX_INTERVAL_S = 10.0
#: A native terminal can report its turn's end before the last reply is mirrored
#: into the transcript, so the end is confirmed by a second read this much later.
_SETTLE_S = 2.0
#: The items API's largest page.
_ITEMS_PAGE_LIMIT = 1000
_STATE_PARAMS = {
    "include_items": "false",
    "include_usage": "false",
    "include_liveness": "false",
    # On a status-cache miss the server asks the bound runner, so a finished turn reads idle.
    "include_live_status": "true",
}


@dataclass(frozen=True)
class TurnResult:
    """
    Where a session stood when ``session send --wait`` stopped waiting.

    :param status: The session's status, e.g. ``"idle"``, or ``"running"`` when it
        stopped to wait for an approval.
    :param pending_approvals: Approval prompts and questions waiting for a person.
    :param messages: The session's messages after the one sent, oldest first.
    """

    status: str | None
    pending_approvals: int
    messages: list[JsonObject]

    @property
    def reply(self) -> str | None:
        """:returns: The last assistant message's text, or ``None`` before one arrives."""
        for message in reversed(self.messages):
            text = message.get("text")
            if message.get("role") == "assistant" and isinstance(text, str):
                return text
        return None


@asynccontextmanager
async def open_session_client(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """
    Open a client that sends each session request to the replica holding its host.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :yields: An authenticated client. Its credential is refreshed per request, so a
        long reply wait outlives one token.
    """
    from omnigent.chat import _server_auth, _server_headers
    from omnigent.cli_auth import open_server_client

    async with open_server_client(
        base_url,
        auth=_server_auth(server_url=base_url),
        headers=_server_headers(),
        timeout=_READ_TIMEOUT,
    ) as client:
        yield client


async def send_message(client: httpx.AsyncClient, session_id: str, text: str) -> str | None:
    """
    Queue *text* as the session's next user message.

    :param client: Client from :func:`open_session_client`.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param text: The message.
    :returns: The stored message's id, or ``None`` while the server holds the
        message as a startup input that has no id yet.
    :raises click.ClickException: When the session is missing, or the server
        refused, denied or did not confirm the message.
    """
    try:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json=message_event(text),
            timeout=_SEND_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"The message could not be confirmed ({exc}). Check the session before "
            "sending it again."
        ) from exc
    if resp.status_code == 404:
        raise click.ClickException(f"Session {session_id} was not found.")
    refusal = message_refusal(resp, "the message")
    if refusal is not None:
        raise click.ClickException(f"{refusal[:1].upper()}{refusal[1:]}.")
    item_id = response_object(resp).get("item_id")
    return item_id if isinstance(item_id, str) and item_id else None


async def list_messages(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    last: int | None = None,
    after: str | None = None,
) -> list[JsonObject]:
    """
    Read the session's messages, oldest first, leaving out tool calls.

    :param client: Client from :func:`open_session_client`.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param last: Keep only the newest *last* messages, e.g. ``10``.
    :param after: Only messages after this message id, e.g. ``"msg_abc"``.
    :returns: Messages shaped by :func:`project_message`.
    :raises click.ClickException: When the session or *after* can't be read.
    """
    if after is None and last is not None:
        return await _newest_messages(client, session_id, last)
    messages = await _messages_after(client, session_id, after)
    return messages[-last:] if last is not None else messages


async def _newest_messages(
    client: httpx.AsyncClient, session_id: str, last: int
) -> list[JsonObject]:
    """
    Return the newest *last* messages, oldest first.

    Pages newest-first until it has *last* messages: older servers ignore the
    ``type`` filter, so a page can hold tool calls instead.
    """
    newest_first: list[JsonObject] = []
    params: dict[str, str | int] = {"limit": last, "order": "desc", "type": "message"}
    while len(newest_first) < last:
        page = await _read_json(client, session_id, f"/v1/sessions/{session_id}/items", params)
        newest_first.extend(_page_messages(page))
        last_id = page.get("last_id")
        if page.get("has_more") is not True or not isinstance(last_id, str):
            break
        params = {**params, "after": last_id, "limit": _ITEMS_PAGE_LIMIT}
    return list(reversed(newest_first[:last]))


async def _messages_after(
    client: httpx.AsyncClient, session_id: str, after: str | None
) -> list[JsonObject]:
    """
    Return every message after *after* (or every message), oldest first.
    """
    items = await _items_after(client, session_id, after, "message")
    return [message for item in items if (message := project_message(item)) is not None]


async def _items_after(
    client: httpx.AsyncClient, session_id: str, after: str | None, item_type: str
) -> list[JsonObject]:
    """
    Return every *item_type* item after *after* (or from the start), oldest first.

    Filters locally as well, because older servers ignore the ``type`` filter.
    """
    found: list[JsonObject] = []
    params: dict[str, str | int] = {"limit": _ITEMS_PAGE_LIMIT, "order": "asc", "type": item_type}
    if after is not None:
        params["after"] = after
    while True:
        page = await _read_json(client, session_id, f"/v1/sessions/{session_id}/items", params)
        data = page.get("data")
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("type") == item_type:
                found.append(cast(JsonObject, item))
        last_id = page.get("last_id")
        if page.get("has_more") is not True or not isinstance(last_id, str):
            return found
        params = {**params, "after": last_id}


def _page_messages(page: JsonObject) -> list[JsonObject]:
    data = page.get("data")
    items = data if isinstance(data, list) else []
    return [message for item in items if (message := project_message(item)) is not None]


def project_message(item: object) -> JsonObject | None:
    """
    Reduce a session item to ``{"id", "role", "text", "created_at"}``.

    :param item: One ``GET /v1/sessions/{id}/items`` entry, e.g. ``{"type":
        "message", "role": "assistant", "content": [{"type": "output_text",
        "text": "Done"}]}``.
    :returns: The message, or ``None`` for an item that is not a message.
    """
    if not isinstance(item, dict) or item.get("type") != "message":
        return None
    content = item.get("content")
    if isinstance(content, str):
        text = content
    else:
        blocks = content if isinstance(content, list) else []
        text = "\n".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return {
        "id": item.get("id"),
        "role": item.get("role"),
        "text": text,
        "created_at": item.get("created_at"),
    }


async def latest_message_id(client: httpx.AsyncClient, session_id: str) -> str | None:
    """
    Return the newest message's id, the point a reply wait reads after.

    :param client: Client from :func:`open_session_client`.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :returns: The id, or ``None`` when the session has no messages yet.
    """
    messages = await list_messages(client, session_id, last=1)
    message_id = messages[-1].get("id") if messages else None
    return message_id if isinstance(message_id, str) else None


async def send_and_wait(
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
    *,
    timeout_s: float | None,
) -> tuple[str | None, TurnResult]:
    """
    Send *text* and wait for the session's reply, all within *timeout_s*.

    The session's state and newest message are read before the send, so neither
    a failure nor a reply from before it is taken as this message's outcome.

    :param client: Client from :func:`open_session_client`.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param text: The message.
    :param timeout_s: Seconds for the whole exchange, including the send, or
        ``None`` to wait as long as the turn takes.
    :returns: The stored message id (see :func:`send_message`) and the outcome.
    :raises click.ClickException: When the send fails, the session can't be read,
        or time runs out.
    """
    posting = sent = False
    try:
        async with asyncio.timeout(timeout_s):
            baseline = await _read_json(
                client, session_id, f"/v1/sessions/{session_id}", _STATE_PARAMS
            )
            after = await latest_message_id(client, session_id)
            posting = True
            message_id = await send_message(client, session_id, text)
            sent = True
            result = await _wait_for_reply(
                client,
                session_id,
                after=after,
                baseline=baseline,
                sent=_SentMessage(message_id, text),
            )
    except TimeoutError:
        if not posting:
            raise click.ClickException(
                f"Timed out after {timeout_s:g}s reading session {session_id}; the message "
                "was not sent."
            ) from None
        if not sent:
            raise click.ClickException(
                f"Timed out after {timeout_s:g}s before the server confirmed the message. "
                "Check the session before sending it again."
            ) from None
        raise click.ClickException(
            f"Timed out after {timeout_s:g}s waiting for session {session_id} to reply. "
            "The message stays with the session."
        ) from None
    return message_id, result


@dataclass(frozen=True)
class _SentMessage:
    """
    The message ``session send`` posted, to find it in the transcript.

    :param item_id: The stored message's id, or ``None`` while the server holds it
        as a startup input.
    :param text: The message text.
    """

    item_id: str | None
    text: str


async def _wait_for_reply(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    after: str | None,
    baseline: JsonObject,
    sent: _SentMessage,
) -> TurnResult:
    """
    Poll until the sent message's turn ends, fails, or waits for a person.

    A status read can predate the turn: a session reads idle, or still reads its
    last turn's failure, until the runner reports the new turn. So the turn has
    ended only once it was seen, as ``running`` or as a reply to the sent message,
    and the session then reads idle with nothing waiting for its terminal. Two
    transcript reads ``_SETTLE_S`` apart must agree, because a native terminal can
    report the end before its last reply is mirrored. A failure counts once it can't
    be the old one: the session wasn't failed before the send, has shown another
    status since, reports a different error, or has an error item after the message.

    :param after: The newest message id before the send, or ``None``.
    :param baseline: The session's state read before the send.
    :param sent: The posted message.
    """
    loop = asyncio.get_running_loop()
    interval = _WAIT_FIRST_INTERVAL_S
    turn_seen = left_failed = False
    settling: tuple[list[object], float] | None = None
    while True:
        state = await _read_json(client, session_id, f"/v1/sessions/{session_id}", _STATE_PARAMS)
        raw_status = state.get("status")
        status = raw_status if isinstance(raw_status, str) else None
        prompts = state.get("pending_elicitations")
        approvals = len(prompts) if isinstance(prompts, list) else 0
        if approvals:
            found = await _messages_after_sent(client, session_id, after, sent)
            return TurnResult(status, approvals, found[1] if found else [])
        if status == "failed":
            found = await _messages_after_sent(client, session_id, after, sent)
            if (
                baseline.get("status") != "failed"
                or left_failed
                or state.get("last_task_error") != baseline.get("last_task_error")
                or (
                    found is not None and await _items_after(client, session_id, found[0], "error")
                )
            ):
                return TurnResult(status, 0, found[1] if found else [])
        else:
            left_failed = True
        if status == "running":
            turn_seen, settling = True, None
        elif status == "idle" and not state.get("pending_inputs"):
            found = await _messages_after_sent(client, session_id, after, sent)
            replies = found[1] if found is not None else None
            # Read after this idle status, a reply shows the turn had started.
            if replies and any(message["role"] == "assistant" for message in replies):
                turn_seen = True
            if turn_seen and replies is not None:
                tail = [message["id"] for message in replies]
                now = loop.time()
                if settling is not None and settling[0] == tail and now - settling[1] >= _SETTLE_S:
                    return TurnResult(status, 0, replies)
                if settling is None or settling[0] != tail:
                    settling = (tail, now)
        else:
            settling = None
        await asyncio.sleep(_SETTLE_S if settling is not None else interval)
        interval = min(interval * 1.5, _WAIT_MAX_INTERVAL_S)


async def _messages_after_sent(
    client: httpx.AsyncClient, session_id: str, after: str | None, sent: _SentMessage
) -> tuple[str, list[JsonObject]] | None:
    """
    Find the sent message after *after* and return its id and every message after it.

    The ack's item id finds it. A startup input has none yet, so the first user
    message with the same text is taken, else the first user message.

    :returns: ``(message id, later messages)``, or ``None`` while it isn't stored.
    """
    messages = await _messages_after(client, session_id, after)
    position = next(
        (index for index, message in enumerate(messages) if message["id"] == sent.item_id),
        None,
    )
    if position is None:
        users = [index for index, message in enumerate(messages) if message["role"] == "user"]
        matching = [
            index for index in users if str(messages[index]["text"]).strip() == sent.text.strip()
        ]
        candidates = matching or users
        if not candidates:
            return None
        position = candidates[0]
    message_id = messages[position]["id"]
    if not isinstance(message_id, str):
        return None
    return (message_id, messages[position + 1 :])


async def session_info(
    client: httpx.AsyncClient, session_id: str, *, include_usage: bool
) -> JsonObject:
    """
    Describe the session with the fields ``sys_session_get_info`` gives an agent.

    :param client: Client from :func:`open_session_client`.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param include_usage: Add the ``usage`` object (cost and tokens so far), which
        sums usage over the session's sub-agents.
    :returns: The tool's result object.
    :raises click.ClickException: When the session is missing or unreadable.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    raw = await execute_tool(
        tool_name="sys_session_get_info",
        arguments=json.dumps({"session_id": session_id, "include_usage": include_usage}),
        server_client=client,
        conversation_id=session_id,
    )
    try:
        decoded: object = json.loads(raw)
    except (TypeError, ValueError):
        decoded = None
    if not isinstance(decoded, dict):
        raise click.ClickException(f"Could not read session {session_id}.")
    info = cast(JsonObject, decoded)
    error = info.get("error")
    if error == "session_not_found":
        raise click.ClickException(f"Session {session_id} was not found.")
    if error == "access_denied":
        raise click.ClickException(
            f"The server refused to show session {session_id}; check that you're signed in."
        )
    if error is not None:
        raise click.ClickException(f"Could not read session {session_id}: {error}")
    return info


def format_messages(messages: list[JsonObject]) -> str:
    """
    Render messages for a terminal, each under a role and id header.

    :param messages: Messages shaped by :func:`project_message`.
    :returns: The text, e.g. ``"── assistant · msg_abc ──\\nDone"``.
    """
    return "\n\n".join(
        f"── {message.get('role')} · {message.get('id')} ──\n{message.get('text') or ''}".rstrip()
        for message in messages
    )


def format_info(info: JsonObject) -> str:
    """
    Render :func:`session_info`'s result as aligned ``label: value`` lines.

    :param info: The session description, optionally with ``usage`` and ``url``.
    :returns: The text, starting with the session id.
    """
    rows: list[tuple[str, object]] = []
    approvals = info.get("pending_elicitation_count")
    status = info.get("status")
    rows.append(("status", f"{status}, {approvals} waiting for you" if approvals else status))
    rows.append(("title", info.get("title")))
    rows.append(("agent", info.get("agent_name")))
    rows.append(("model", info.get("model")))
    rows.append(("permission mode", info.get("permission_mode")))
    host = info.get("host_id")
    online = info.get("runner_online")
    if host and isinstance(online, bool):
        host = f"{host} (runner {'online' if online else 'offline'})"
    rows.append(("host", host))
    rows.append(("workspace", info.get("workspace")))
    rows.append(("branch", info.get("git_branch")))
    rows.append(("project", info.get("project_name") or info.get("project_id")))
    rows.append(("last activity", _format_time(info.get("last_activity_at"))))
    usage = info.get("usage")
    if isinstance(usage, dict):
        rows.append(("cost", _format_cost(cast(JsonObject, usage))))
        rows.append(("tokens", _format_tokens(cast(JsonObject, usage))))
    rows.append(("url", info.get("url")))
    shown = [(label, str(value)) for label, value in rows if value not in (None, "")]
    width = max(len(label) for label, _ in shown) + 1
    lines = [f"{label + ':':<{width}} {value}" for label, value in shown]
    return "\n".join([str(info.get("session_id")), *lines])


def _format_time(epoch: object) -> str | None:
    if not isinstance(epoch, int | float) or isinstance(epoch, bool):
        return None
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_cost(usage: JsonObject) -> str:
    if usage.get("cost_hidden") is True:
        return "hidden by the server"
    cost = usage.get("total_cost_usd")
    if not isinstance(cost, int | float) or isinstance(cost, bool):
        return "not priced"
    return f"${cost:.2f}"


def _format_tokens(usage: JsonObject) -> str | None:
    labels = (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_read_input_tokens", "cache read"),
        ("cache_creation_input_tokens", "cache write"),
    )
    parts = [
        f"{count:,} {label}"
        for field, label in labels
        if isinstance(count := usage.get(field), int) and not isinstance(count, bool)
    ]
    return ", ".join(parts) or None


async def _read_json(
    client: httpx.AsyncClient,
    session_id: str,
    url: str,
    params: dict[str, str] | dict[str, str | int],
) -> JsonObject:
    """
    GET *url* for *session_id* and decode its JSON object.

    :raises click.ClickException: When the request fails, the session is missing,
        or the server refuses the read.
    """
    try:
        resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise click.ClickException(f"Could not read session {session_id} ({exc}).") from exc
    if resp.status_code == 404:
        raise click.ClickException(f"Session {session_id} was not found.")
    if resp.status_code >= 400:
        raise click.ClickException(
            f"Could not read session {session_id} ({resp.status_code}): {server_error(resp)}"
        )
    return response_object(resp)
