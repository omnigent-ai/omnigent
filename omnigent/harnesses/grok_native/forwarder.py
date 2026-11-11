"""Mirror a grok-native session's ACP updates into the Omnigent chat.

``grok agent serve`` emits ``session/update`` notifications over the session's
WebSocket. The forwarder maps each update to Omnigent mirror items (see
:mod:`omnigent.harnesses.grok_native.items`) and POSTs them as
``external_conversation_item`` events, plus ``external_session_status`` edges,
so the reply renders in the web UI. See docs/GROK_NATIVE_DESIGN.md.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from omnigent.harnesses.grok_native.client import GrokAcpClient
from omnigent.harnesses.grok_native.items import MirroredItem, session_update_to_items

logger = logging.getLogger(__name__)

_EVENTS_PATH = "/v1/sessions/{session_id}/events"
_DEFAULT_AGENT_NAME = "grok-native-ui"


async def post_mirrored_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: MirroredItem,
    agent_name: str = _DEFAULT_AGENT_NAME,
) -> None:
    """POST one mirrored item (and its turn-status edge) to the Sessions API."""
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
    """POST one ``external_session_status`` edge (``running``/``idle``)."""
    url = base_url.rstrip("/") + _EVENTS_PATH.format(session_id=session_id)
    resp = await client.post(
        url,
        headers=headers,
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )
    resp.raise_for_status()


def make_update_sink(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    agent_name: str = _DEFAULT_AGENT_NAME,
):
    """Build the ``session/update`` handler for :meth:`GrokAcpClient.set_update_handler`.

    Each notification's items are queued and POSTed in order; failures are
    logged, never raised (a dead mirror must not stall the agent's stream).
    """

    async def _drain(queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            try:
                await post_mirrored_item(
                    client,
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    item=item,
                    agent_name=agent_name,
                )
            except Exception:
                logger.exception(
                    "grok forwarder failed to mirror %s for %s", item.kind, session_id
                )

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(_drain(queue), name=f"grok-forwarder-{session_id}")

    def _on_update(params: dict[str, Any]) -> None:
        for item in session_update_to_items(params):
            queue.put_nowait(item)

    return _on_update, task


__all__ = [
    "make_update_sink",
    "post_mirrored_item",
    "post_status",
]
