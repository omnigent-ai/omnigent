"""Mirror a cline-native session's hub events into the Omnigent chat.

``cline hub`` broadcasts ``event`` frames to every subscribed client, so the
forwarder listens on its own connection — unlike per-connection ACP streams,
no per-turn correlation is needed. Each event maps to Omnigent mirror items
(see :mod:`omnigent.harnesses.cline_native.items`) POSTed as
``external_conversation_item`` events, plus ``external_session_status`` edges
on run lifecycle and cumulative ``external_session_usage`` on
``usage.updated`` (the hub payload is already cumulative).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from omnigent.harnesses.cline_native.items import MirroredItem, hub_event_to_items

logger = logging.getLogger(__name__)

_EVENTS_PATH = "/v1/sessions/{session_id}/events"
_DEFAULT_AGENT_NAME = "cline-native-ui"


async def post_mirrored_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: MirroredItem,
    agent_name: str = _DEFAULT_AGENT_NAME,
) -> None:
    """POST one mirrored item to the Sessions API."""
    url = base_url.rstrip("/") + _EVENTS_PATH.format(session_id=session_id)
    if item.kind == "reasoning":
        body: dict[str, Any] = {
            "type": "external_output_reasoning_delta",
            "data": {"delta": item.text, "started": True},
        }
    elif item.kind == "function_call":
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": agent_name,
                    "name": item.name,
                    "arguments": item.arguments,
                    "call_id": item.call_id,
                },
                "response_id": item.response_id,
            },
        }
    elif item.kind == "function_call_output":
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {"call_id": item.call_id, "output": item.text},
                "response_id": item.response_id,
            },
        }
    else:
        content_type = "input_text" if item.role == "user" else "output_text"
        item_data: dict[str, Any] = {
            "role": item.role,
            "content": [{"type": content_type, "text": item.text}],
        }
        if item.role == "assistant":
            item_data["agent"] = agent_name
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": item_data,
                "response_id": item.response_id,
            },
        }
    if not item.response_id:
        body["data"].pop("response_id", None)
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def post_status(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    status: str,
    output: str = "",
) -> None:
    """POST one ``external_session_status`` edge."""
    url = base_url.rstrip("/") + _EVENTS_PATH.format(session_id=session_id)
    resp = await client.post(
        url,
        headers=headers,
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )
    resp.raise_for_status()


async def post_usage(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    totals: dict[str, Any],
) -> None:
    """POST cumulative session usage (hub ``totals`` are already cumulative)."""
    data: dict[str, Any] = {
        "context_tokens": int(totals.get("inputTokens") or 0),
        "cumulative_input_tokens": int(totals.get("inputTokens") or 0),
        "cumulative_output_tokens": int(totals.get("outputTokens") or 0),
    }
    cost = totals.get("totalCost")
    if cost:
        data["cumulative_cost_usd"] = float(cost)
    resp = await client.post(
        base_url.rstrip("/") + _EVENTS_PATH.format(session_id=session_id),
        headers=headers,
        json={"type": "external_session_usage", "data": data},
    )
    resp.raise_for_status()


_TERMINAL_STATUS = {
    "run.completed": "idle",
    "run.aborted": "idle",
    "run.interrupted": "idle",
    "run.failed": "failed",
}


def make_event_sink(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    agent_name: str = _DEFAULT_AGENT_NAME,
):
    """Build the hub-event handler for :meth:`ClineHubClient.set_event_handler`.

    Each event's items are queued and POSTed in order; failures are logged,
    never raised (a dead mirror must not stall the agent's stream).

    :returns: ``(handler, drain_task)`` — set the handler on the client and
        keep the task alive for the session's lifetime.
    """

    async def _drain(queue: asyncio.Queue) -> None:
        while True:
            work = await queue.get()
            try:
                kind, payload = work
                if kind == "item":
                    await post_mirrored_item(
                        client,
                        base_url=base_url,
                        headers=headers,
                        session_id=session_id,
                        item=payload,
                        agent_name=agent_name,
                    )
                elif kind == "status":
                    await post_status(
                        client,
                        base_url=base_url,
                        headers=headers,
                        session_id=session_id,
                        status=payload,
                    )
                elif kind == "usage":
                    await post_usage(
                        client,
                        base_url=base_url,
                        headers=headers,
                        session_id=session_id,
                        totals=payload,
                    )
            except Exception:
                logger.exception("cline forwarder failed to mirror %s for %s", work[0], session_id)

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_drain(queue), name=f"cline-forwarder-{session_id}")

    def _on_event(envelope: dict[str, Any]) -> None:
        event = envelope.get("event")
        payload = envelope.get("payload")
        if event == "run.started":
            queue.put_nowait(("status", "running"))
            return
        if event in _TERMINAL_STATUS:
            queue.put_nowait(("status", _TERMINAL_STATUS[event]))
            return
        if event == "usage.updated" and isinstance(payload, dict):
            totals = payload.get("totals")
            if isinstance(totals, dict):
                queue.put_nowait(("usage", totals))
            return
        for item in hub_event_to_items(envelope):
            queue.put_nowait(("item", item))

    return _on_event, task


async def run_cline_forwarder(
    *,
    ws_url: str,
    auth_token: str,
    hub_session_id: str,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    agent_name: str = _DEFAULT_AGENT_NAME,
) -> None:
    """Own the forwarder connection for one session's lifetime.

    Connects to the hub, subscribes to the session's event stream, and holds
    the socket open so :func:`make_event_sink` drains events into Omnigent
    posts. Runs until cancelled; a dead connection logs and exits rather than
    looping (the runner's task registry treats exit as terminal).
    """
    from omnigent.harnesses.cline_native.client import ClineHubClient

    client = await ClineHubClient.connect(ws_url, auth_token)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
            _on_event, drain = make_event_sink(
                http,
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                agent_name=agent_name,
            )
            client.set_event_handler(_on_event)
            await client.subscribe(hub_session_id)
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain
    finally:
        await client.close()


__all__ = [
    "make_event_sink",
    "post_mirrored_item",
    "post_status",
    "post_usage",
    "run_cline_forwarder",
]
