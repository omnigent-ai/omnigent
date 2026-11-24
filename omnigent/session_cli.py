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
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import click
import httpx

from omnigent.cli_invocation import cli_invocation
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
#: Page size for the read that looks for a tool call after the sent message.
_ACTIVITY_PAGE_LIMIT = 20
#: Items a turn writes as it runs. Metadata such as resource events doesn't count.
_TURN_OUTPUT_TYPES = frozenset(
    {
        "function_call",
        "function_call_output",
        "reasoning",
        "native_tool",
        "error",
        "compaction",
        "terminal_command",
    }
)
#: The longest a stored-login refresh may take, when no deadline is shorter.
_REFRESH_TIMEOUT_S = 10.0
_STATE_PARAMS = {
    "include_items": "false",
    "include_usage": "false",
    "include_liveness": "false",
    # On a status-cache miss the server asks the bound runner, so a finished turn reads idle.
    "include_live_status": "true",
}


def command_error(message: str) -> click.ClickException:
    """
    Build a ``session send|get|read`` error that ``omnigent stop`` can't fix.

    The CLI otherwise appends its stale-host recovery hint to every error.

    :param message: The error, e.g. ``"Session conv_abc123 was not found."``.
    :returns: The exception to raise.
    """
    from omnigent.cli_diagnostics import SUPPRESS_RECOVERY_HINT_ATTR

    error = click.ClickException(message)
    setattr(error, SUPPRESS_RECOVERY_HINT_ATTR, True)
    return error


@dataclass(frozen=True)
class TurnResult:
    """
    Where a session stood when ``session send --wait`` stopped waiting.

    :param status: The session's status, e.g. ``"idle"``, or ``"running"`` when it
        stopped to wait for an approval.
    :param pending_approvals: Approval prompts and questions waiting for a person.
    :param messages: The session's messages after the one sent, oldest first.
    :param delivered: ``False`` when the session gave up on delivering the message.
    """

    status: str | None
    pending_approvals: int
    messages: list[JsonObject]
    delivered: bool = True

    @property
    def reply(self) -> str | None:
        """:returns: The last assistant message's text, or ``None`` before one arrives."""
        for message in reversed(self.messages):
            text = message.get("text")
            if message.get("role") == "assistant" and isinstance(text, str):
                return text
        return None


@asynccontextmanager
async def open_session_client(
    base_url: str, *, deadline: float | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    """
    Open a client that sends each session request to the replica holding its host.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param deadline: Event-loop time the caller must finish by, which also bounds
        each login refresh, or ``None``.
    :yields: An authenticated client. A stored ``omnigent login`` token is renewed
        from its refresh grant before it lapses, so a long reply wait outlives it.
    """
    from omnigent.chat import _server_auth, _server_headers
    from omnigent.cli_auth import open_server_client

    # An already-expired login must be renewed before choosing how to authenticate.
    await asyncio.to_thread(_renew_stored_login, base_url, _remaining(deadline))
    auth = _server_auth(server_url=base_url)
    async with open_server_client(
        base_url,
        auth=_RenewingAuth(auth, base_url, deadline) if auth is not None else None,
        headers=_server_headers(),
        timeout=_READ_TIMEOUT,
    ) as client:
        yield client


def _remaining(deadline: float | None) -> float | None:
    """:returns: Seconds left before *deadline* on the running loop, or ``None``."""
    if deadline is None:
        return None
    return max(0.0, deadline - asyncio.get_running_loop().time())


def _renew_stored_login(server_url: str, budget_s: float | None = None) -> None:
    """
    Renew the stored ``omnigent login`` token for *server_url* if it is about to lapse.

    Does nothing with an ``OMNIGENT_REMOTE_AUTH_TOKEN`` override, a token with
    enough life left, no refresh grant to renew it with, or no time left.

    :param budget_s: Seconds the refresh may take, or ``None`` for its own limit.
    """
    from omnigent.chat import _REMOTE_AUTH_TOKEN_ENV
    from omnigent.cli_auth import REFRESH_MIN_REMAINING_SECONDS, load_token, refresh_stored_token

    if os.environ.get(_REMOTE_AUTH_TOKEN_ENV, "").strip() or budget_s == 0:
        return
    if load_token(server_url, min_remaining_seconds=REFRESH_MIN_REMAINING_SECONDS) is None:
        limit = _REFRESH_TIMEOUT_S if budget_s is None else min(_REFRESH_TIMEOUT_S, budget_s)
        refresh_stored_token(server_url, timeout=limit)


class _RenewingAuth(httpx.Auth):
    """The CLI's server auth, with the stored login renewed before each request."""

    def __init__(self, inner: httpx.Auth, server_url: str, deadline: float | None) -> None:
        self._inner = inner
        self._server_url = server_url
        self._deadline = deadline

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """Renew the login off the event loop, then authenticate as the CLI does."""
        await asyncio.to_thread(_renew_stored_login, self._server_url, _remaining(self._deadline))
        flow = self._inner.async_auth_flow(request)
        outgoing = await anext(flow)
        while True:
            response = yield outgoing
            try:
                outgoing = await flow.asend(response)
            except StopAsyncIteration:
                return


@dataclass(frozen=True)
class SentMessage:
    """
    A message the server queued, and how to find it again.

    :param text: The message text.
    :param item_id: The stored message's id, or ``None`` while the server holds it
        for a native terminal or a starting session.
    :param pending_id: The held input's id, e.g. ``"pending_abc"``, or ``None``.
    """

    text: str
    item_id: str | None = None
    pending_id: str | None = None


async def send_message(client: httpx.AsyncClient, session_id: str, text: str) -> SentMessage:
    """
    Queue *text* as the session's next user message.

    :param client: Client from :func:`open_session_client`.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param text: The message.
    :returns: The queued message and the ids the server acknowledged it with.
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
        raise command_error(
            f"The message could not be confirmed ({exc}). Check the session before "
            "sending it again."
        ) from exc
    if resp.status_code == 404:
        raise command_error(f"Session {session_id} was not found.")
    if resp.is_redirect:
        raise command_error(_redirect_error(resp))
    refusal = message_refusal(resp, "the message")
    if refusal is not None:
        raise command_error(f"{refusal[:1].upper()}{refusal[1:]}.")
    ack = response_object(resp)
    item_id, pending_id = ack.get("item_id"), ack.get("pending_id")
    return SentMessage(
        text,
        item_id if isinstance(item_id, str) and item_id else None,
        pending_id if isinstance(pending_id, str) and pending_id else None,
    )


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
        newest_first.extend(_page_messages(page, session_id))
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
        found.extend(
            item for item in _page_items(page, session_id) if item.get("type") == item_type
        )
        last_id = page.get("last_id")
        if page.get("has_more") is not True or not isinstance(last_id, str):
            return found
        params = {**params, "after": last_id}


def _page_messages(page: JsonObject, session_id: str) -> list[JsonObject]:
    items = _page_items(page, session_id)
    return [message for item in items if (message := project_message(item)) is not None]


def _page_items(page: JsonObject, session_id: str) -> list[JsonObject]:
    """
    Return a page's items, refusing a page with no item list.

    :raises click.ClickException: When the page isn't an items page.
    """
    data = page.get("data")
    if not isinstance(data, list):
        raise command_error(f"The server sent an unexpected item page for session {session_id}.")
    return [cast(JsonObject, item) for item in data if isinstance(item, dict)]


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
    base_url: str,
    session_id: str,
    text: str,
    *,
    timeout_s: float | None,
) -> tuple[SentMessage, TurnResult]:
    """
    Send *text* and wait for the session's reply, all within *timeout_s*.

    The session's state and newest message are read before the send, so neither
    a failure nor a reply from before it is taken as this message's outcome.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param text: The message.
    :param timeout_s: Seconds for the whole exchange, from opening the client
        through the reply, or ``None`` to wait as long as the turn takes.
    :returns: The queued message (see :func:`send_message`) and the outcome.
    :raises click.ClickException: When the send fails, the session can't be read,
        or time runs out.
    """
    loop = asyncio.get_running_loop()
    deadline = None if timeout_s is None else loop.time() + timeout_s
    posting = sent = False
    try:
        async with (
            asyncio.timeout(timeout_s),
            open_session_client(base_url, deadline=deadline) as client,
        ):
            baseline = await _read_state(client, session_id)
            after = await latest_message_id(client, session_id)
            posting = True
            message = await send_message(client, session_id, text)
            sent = True
            result = await _wait_for_reply(
                client, session_id, after=after, baseline=baseline, sent=message
            )
    except TimeoutError:
        if not posting:
            raise command_error(
                f"Timed out after {timeout_s:g}s reading session {session_id}; the message "
                "was not sent."
            ) from None
        if not sent:
            raise command_error(
                f"Timed out after {timeout_s:g}s before the server confirmed the message. "
                "Check the session before sending it again."
            ) from None
        raise command_error(
            f"Timed out after {timeout_s:g}s waiting for session {session_id} to reply. "
            "The message stays with the session."
        ) from None
    return message, result


async def _wait_for_reply(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    after: str | None,
    baseline: JsonObject,
    sent: SentMessage,
) -> TurnResult:
    """
    Poll until the sent message's turn ends, fails, or waits for a person.

    A status read can predate the turn: a session reads idle, or still reads its
    last turn's failure, until the runner reports the new turn. So the turn has
    ended only once it was seen (as ``running``, or as a reply, tool call or error
    after the sent message) and the session then reads idle with nothing waiting for
    its terminal. Two
    transcript reads ``_SETTLE_S`` apart must agree, because a native terminal can
    report the end before its last reply is mirrored. A failure counts once it can't
    be the old one: the session wasn't failed before the send, has shown another
    status since, reports a different error, or has an error item after the message.
    A held message the session listed as failed to deliver ends the wait too.

    :param after: The newest message id before the send, or ``None``.
    :param baseline: The session's state read before the send.
    :param sent: The posted message.
    """
    loop = asyncio.get_running_loop()
    interval = _WAIT_FIRST_INTERVAL_S
    turn_seen = left_failed = False
    settling: tuple[list[object], float] | None = None
    while True:
        state = await _read_state(client, session_id)
        status = cast(str, state["status"])
        prompts = state.get("pending_elicitations")
        approvals = len(prompts) if isinstance(prompts, list) else 0
        if approvals:
            found = await _messages_after_sent(client, session_id, after, sent)
            return TurnResult(status, approvals, found[1] if found else [])
        undelivered = state.get("failed_inputs")
        if sent.pending_id is not None and isinstance(undelivered, list):
            if any(
                isinstance(entry, dict) and entry.get("pending_id") == sent.pending_id
                for entry in undelivered
            ):
                return TurnResult("failed", 0, [], delivered=False)
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
            # Read after this idle status, a reply or turn output shows the turn had started.
            if found is not None and not turn_seen:
                turn_seen = (
                    found[2]
                    or any(message["role"] == "assistant" for message in found[1])
                    or await _turn_activity_after(client, session_id, found[0])
                )
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
    client: httpx.AsyncClient, session_id: str, after: str | None, sent: SentMessage
) -> tuple[str, list[JsonObject], bool] | None:
    """
    Find the sent message after *after* and return its id and every message after it.

    The ack's item id finds it. A held message has none until its turn records it,
    so the user message with the same text (ignoring whitespace) is taken, or, for a
    ``/command`` or ``!command``, the command item a native terminal records instead.
    Nobody else's message stands in for it.

    :returns: ``(anchor id, later messages, already ran)``, where *already ran* is
        ``True`` for a built-in command that ran when it was recorded, or ``None``
        while the message isn't stored.
    :raises click.ClickException: When more than one item matches the held text.
    """
    messages = await _messages_after(client, session_id, after)
    if sent.item_id is not None:
        positions = [
            index for index, message in enumerate(messages) if message["id"] == sent.item_id
        ]
        if not positions:
            return None
        return _anchored(messages, positions[0], ran=False)
    words = sent.text.split()
    positions = [
        index
        for index, message in enumerate(messages)
        if message["role"] == "user" and str(message["text"]).split() == words
    ]
    commands = await _sent_commands(client, session_id, after, words)
    if len(positions) + len(commands) > 1:
        raise command_error(
            f"Session {session_id} has more than one item matching the text sent, so its "
            f"reply can't be told apart. `{cli_invocation()} session read {session_id}` "
            "shows them."
        )
    if positions:
        return _anchored(messages, positions[0], ran=False)
    if not commands:
        return None
    command = commands[0]
    command_id = command.get("id")
    if not isinstance(command_id, str):
        return None
    later = await _messages_after(client, session_id, command_id)
    # A built-in such as /model runs as Claude records it; it starts no turn.
    built_in = command.get("type") == "slash_command" and command.get("kind") == "command"
    return command_id, later, built_in


def _anchored(
    messages: list[JsonObject], position: int, *, ran: bool
) -> tuple[str, list[JsonObject], bool] | None:
    message_id = messages[position]["id"]
    if not isinstance(message_id, str):
        return None
    return message_id, messages[position + 1 :], ran


async def _sent_commands(
    client: httpx.AsyncClient, session_id: str, after: str | None, words: list[str]
) -> list[JsonObject]:
    """
    Return the command items a native terminal recorded for a held ``/`` or ``!`` input.

    :param words: The sent text split on whitespace, e.g. ``["/compact"]``.
    """
    if not words:
        return []
    if words[0].startswith("/"):
        items = await _items_after(client, session_id, after, "slash_command")
        return [
            item
            for item in items
            if [f"/{item.get('name')}", *str(item.get("arguments") or "").split()] == words
        ]
    if words[0].startswith("!"):
        items = await _items_after(client, session_id, after, "terminal_command")
        return [
            item
            for item in items
            if item.get("kind") == "input" and f"!{item.get('input')}".split() == words
        ]
    return []


async def _turn_activity_after(client: httpx.AsyncClient, session_id: str, item_id: str) -> bool:
    """
    Report whether turn output (a tool call, reasoning, an error, an assistant
    message, ...) follows the sent message.

    A turn that only calls tools can start and end between two polls, so this is
    its only trace.
    """
    params: dict[str, str | int] = {
        "limit": _ACTIVITY_PAGE_LIMIT,
        "order": "asc",
        "after": item_id,
    }
    while True:
        page = await _read_json(client, session_id, f"/v1/sessions/{session_id}/items", params)
        for item in _page_items(page, session_id):
            if item.get("type") in _TURN_OUTPUT_TYPES or (
                item.get("type") == "message" and item.get("role") == "assistant"
            ):
                return True
        last_id = page.get("last_id")
        if page.get("has_more") is not True or not isinstance(last_id, str):
            return False
        params = {**params, "after": last_id}


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
        raise command_error(f"Could not read session {session_id}.")
    info = cast(JsonObject, decoded)
    error = info.get("error")
    if error == "session_not_found":
        raise command_error(f"Session {session_id} was not found.")
    if error == "access_denied":
        raise command_error(
            f"The server refused to show session {session_id}; check that you're signed in."
        )
    if error is not None:
        raise command_error(f"Could not read session {session_id}: {error}")
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


async def _read_state(client: httpx.AsyncClient, session_id: str) -> JsonObject:
    """
    Read the session's state for a reply wait.

    :raises click.ClickException: When the read fails or the state has no status.
    """
    state = await _read_json(client, session_id, f"/v1/sessions/{session_id}", _STATE_PARAMS)
    if not isinstance(state.get("status"), str):
        raise command_error(f"The server sent an unexpected state for session {session_id}.")
    return state


async def _read_json(
    client: httpx.AsyncClient,
    session_id: str,
    url: str,
    params: dict[str, str] | dict[str, str | int],
) -> JsonObject:
    """
    GET *url* for *session_id* and decode its JSON object.

    :raises click.ClickException: When the request fails, the session is missing,
        the server refuses or redirects the read, or the body isn't a JSON object.
    """
    try:
        resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise command_error(f"Could not read session {session_id} ({exc}).") from exc
    if resp.status_code == 404:
        raise command_error(f"Session {session_id} was not found.")
    if resp.is_redirect:
        raise command_error(_redirect_error(resp))
    if resp.status_code >= 400:
        raise command_error(
            f"Could not read session {session_id} ({resp.status_code}): {server_error(resp)}"
        )
    try:
        payload: object = resp.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise command_error(
            f"The server sent an unreadable response for session {session_id} "
            f"({resp.status_code})."
        )
    return cast(JsonObject, payload)


def _redirect_error(resp: httpx.Response) -> str:
    """
    Explain a redirect, which a sign-in page in front of the server sends.

    :param resp: The ``3xx`` response.
    :returns: The error message, naming where the server redirected.
    """
    location = resp.headers.get("location")
    where = f" to {location}" if location else ""
    return (
        f"The server redirected the request ({resp.status_code}{where}); sign in again with "
        f"`{cli_invocation()} login`."
    )
