"""Mirror harness-reported ACP sub-agents into Omnigent child sessions.

Each sub-agent start, tool call, and completion edge becomes a post to the
child session's transcript so the sub-agent renders as a normal child row.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable

import httpx

_logger = logging.getLogger("omnigent.runner.app")

# Bounded wait for a sub-agent's start edge to mint its child session before the
# completion edge records the outcome. The mint POST carries its own timeout, so
# this only guards the unexpected case where the start task never resolves.
_SUBAGENT_MINT_WAIT_S: float = 30.0


async def _mint_acp_subagent_child(
    client: httpx.AsyncClient,
    *,
    parent_id: str,
    child_key: str,
    title: str,
    task: str,
    child_id_future: asyncio.Future[str],
) -> None:
    """Mint a child session for a harness-reported sub-agent (the start edge).

    POSTs ``external_acp_subagent_start`` — idempotent on ``child_key``
    server-side — then seeds the child's transcript with the delegated task as a
    user message, so opening the row shows what the sub-agent was asked to do
    instead of an empty chat. Resolves ``child_id_future`` with the child id so
    the completion edge can address it.

    Deliberately NOT the native ``external_subagent_start`` path: that one stamps
    a claude-native wrapper label, which makes the UI title the child "Claude
    Code" regardless of the real harness. The ACP path leaves the wrapper unset so
    the child inherits its parent's harness identity (e.g. Devin).

    Best-effort: a failure resolves the future with the exception (so the
    completion edge fails fast rather than hanging) and is logged, never raised
    into the turn.

    :param client: Omnigent HTTP client for the runner subprocess.
    :param parent_id: The parent conversation the sub-agent belongs to.
    :param child_key: Stable sub-agent id; the idempotency + correlation key.
    :param title: Row label for the child, e.g. ``"mathutils"``.
    :param task: The delegated instruction, seeded as the child's first message.
    :param child_id_future: Resolved with the minted child session id.
    """
    try:
        resp = await client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "external_acp_subagent_start",
                "data": {
                    "subagent_id": child_key,
                    "title": title or child_key,
                    "description": task,
                },
            },
        )
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        child_id = payload.get("child_session_id") if isinstance(payload, dict) else None
        if not (isinstance(child_id, str) and child_id):
            raise ValueError("external_acp_subagent_start returned no child_session_id")
        if not child_id_future.done():
            child_id_future.set_result(child_id)
    except Exception as exc:  # noqa: BLE001 — surfacing a sub-agent must never break the turn
        _logger.warning("acp sub-agent child mint failed (child_key=%s): %s", child_key, exc)
        if not child_id_future.done():
            child_id_future.set_exception(exc)
        return
    # Seed the child's chat with its task. Separate from the mint so a transcript
    # failure still leaves a working row (the panel entry already exists).
    if task:
        await _post_acp_subagent_message(
            client, child_id=child_id, child_key=child_key, role="user", text=task
        )


async def _post_acp_subagent_message(
    client: httpx.AsyncClient,
    *,
    child_id: str,
    child_key: str,
    role: str,
    text: str,
    agent: str | None = None,
) -> None:
    """Append one message to an ACP sub-agent's child transcript.

    Uses the existing ``external_conversation_item`` bridge (the same path the
    native forwarders use for sub-agent transcripts), so the child conversation
    renders normally when opened. Best-effort and never raised into the turn.

    :param client: Omnigent HTTP client for the runner subprocess.
    :param child_id: The child session to append to.
    :param child_key: Sub-agent id, used for the response id and log context.
    :param role: ``"user"`` (the delegated task) or ``"assistant"`` (its result).
    :param text: Message text.
    :param agent: Author name for an ``"assistant"`` message (ignored for
        ``"user"``). ``MessageData`` requires a non-empty ``agent`` on assistant
        messages, so this falls back to *child_key* when unset.
    """
    block_type = "input_text" if role == "user" else "output_text"
    item_data: dict[str, object] = {
        "role": role,
        "content": [{"type": block_type, "text": text}],
    }
    if role == "assistant":
        # MessageData rejects an assistant message with no ``agent`` (its
        # ``check_agent_for_assistant`` validator), so an assistant item that
        # omits it 400s and the summary silently never lands. Mirror codex's
        # assistant transcript post, which sets ``agent`` for this exact reason.
        item_data["agent"] = agent or child_key
    try:
        resp = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": item_data,
                    "response_id": f"resp_acpsub_{child_key}",
                },
            },
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — transcript is best-effort
        _logger.warning("acp sub-agent %s message failed (child_key=%s): %s", role, child_key, exc)


async def _complete_acp_subagent_child(
    client: httpx.AsyncClient,
    *,
    child_key: str,
    ok: bool,
    summary: str,
    child_id_future: asyncio.Future[str],
    title: str = "",
) -> None:
    """Record a harness-reported sub-agent's outcome on its child session (end edge).

    Waits (bounded) for the start edge to mint the child, then marks the child's
    status (``idle`` on success, ``failed`` otherwise) with the summary attached
    as its output. Best-effort: a missing or failed mint is logged and skipped,
    never raised into the turn.

    :param client: Omnigent HTTP client for the runner subprocess.
    :param child_key: Stable sub-agent id, matching the start edge.
    :param ok: Whether the sub-agent reported success.
    :param summary: The sub-agent's closing summary, attached as the child output.
    :param child_id_future: Future the start edge resolves with the child id.
    :param title: The sub-agent's display name, used as the summary message's
        author; falls back to *child_key* when empty.
    """
    from omnigent.native._native_post_delivery import post_external_session_status

    try:
        child_id = await asyncio.wait_for(
            asyncio.shield(child_id_future), timeout=_SUBAGENT_MINT_WAIT_S
        )
    except Exception as exc:  # noqa: BLE001 — includes the start edge's own mint failure
        _logger.warning(
            "acp sub-agent child unavailable for completion (child_key=%s): %s", child_key, exc
        )
        return
    # The sub-agent's closing summary is the only account of its work the agent
    # reports, so it goes in the child's transcript, not just the status edge.
    if summary:
        await _post_acp_subagent_message(
            client,
            child_id=child_id,
            child_key=child_key,
            role="assistant",
            text=summary,
            agent=title or child_key,
        )
    try:
        await post_external_session_status(
            client,
            session_id=child_id,
            status="idle" if ok else "failed",
            output=summary or None,
            # The harness reported this sub-agent's outcome itself, so a
            # success edge is a confirmed turn completion, not a guess.
            turn_completed=True if ok else None,
        )
    except Exception as exc:  # noqa: BLE001 — a status edge failure must not break the turn
        _logger.warning(
            "acp sub-agent completion status failed (child_key=%s): %s", child_key, exc
        )


async def _post_acp_subagent_tool_call(
    client: httpx.AsyncClient,
    *,
    child_key: str,
    call_id: str,
    name: str,
    arguments: str,
    child_id_future: asyncio.Future[str],
    title: str = "",
) -> None:
    """Append one of a sub-agent's own tool calls to its child transcript.

    Renders as a ``function_call`` card in the child's chat (the same shape the
    parent uses for an observed tool call), so opening a sub-agent shows the work
    it did, not just the task and summary. Shares the ``response_id`` the task and
    summary messages use, so the card groups into the same turn.

    Waits (bounded) for the start edge to mint the child. Best-effort: a missing
    or failed mint is logged and skipped, never raised into the turn.

    :param client: Omnigent HTTP client for the runner subprocess.
    :param child_key: Stable sub-agent id, matching the start edge.
    :param call_id: The tool call's id (the child item's ``call_id``).
    :param name: Human tool label, e.g. ``"Wrote mathutils.py"``.
    :param arguments: JSON-encoded arguments string (the tool's raw input).
    :param child_id_future: Future the start edge resolves with the child id.
    :param title: The sub-agent's display name, used as the item's author; falls
        back to *child_key* when empty.
    """
    try:
        child_id = await asyncio.wait_for(
            asyncio.shield(child_id_future), timeout=_SUBAGENT_MINT_WAIT_S
        )
    except Exception as exc:  # noqa: BLE001 — includes the start edge's own mint failure
        _logger.warning(
            "acp sub-agent child unavailable for tool call (child_key=%s): %s", child_key, exc
        )
        return
    try:
        resp = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "function_call",
                    "item_data": {
                        # FunctionCallData.agent (validated by name, serialized as "model").
                        "agent": title or child_key,
                        "name": name,
                        "arguments": arguments or "{}",
                        "call_id": call_id,
                    },
                    "response_id": f"resp_acpsub_{child_key}",
                },
            },
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — a transcript item must not break the turn
        _logger.warning("acp sub-agent tool-call item failed (child_key=%s): %s", child_key, exc)


def _chain_acp_subagent_post(
    prev: asyncio.Task[object] | None, coro: Awaitable[None]
) -> asyncio.Task[object]:
    """Serialize a child's transcript posts so its items land in stream order.

    The start/tool-call/completion edges for one sub-agent are dispatched as
    independent tasks; without ordering, the summary could race ahead of a tool
    card. Each post is chained after the child's previous one (mint → tool calls
    → summary), and the stream is never blocked because chaining only schedules a
    task. A failure in *prev* is suppressed so one bad post can't strand the rest.

    :param prev: The child's previous post task, or ``None`` for the first.
    :param coro: The post coroutine to run after *prev* completes.
    :returns: The new tail task for this child.
    """

    async def _run() -> None:
        if prev is not None:
            with contextlib.suppress(Exception):
                await prev
        await coro

    return asyncio.create_task(_run())
