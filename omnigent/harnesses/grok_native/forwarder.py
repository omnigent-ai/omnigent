"""Mirror a grok-native session's ACP updates into the Omnigent chat.

``grok agent serve`` emits ``session/update`` notifications over the session's
WebSocket. The forwarder maps each update to Omnigent mirror items (see
:mod:`omnigent.harnesses.grok_native.items`) and POSTs them as
``external_conversation_item`` events, plus ``external_session_status`` edges,
so the reply renders in the web UI. See docs/GROK_NATIVE_DESIGN.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx

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


def grok_updates_log_path(workspace: str, grok_session_id: str) -> Path:
    """Return the ``updates.jsonl`` grok persists for one session.

    Serve writes ``~/.grok/sessions/<percent-encoded cwd>/<session_id>/updates.jsonl``
    with the same ``session/update`` payloads the WebSocket emits — so the
    forwarder tails the file rather than racing a per-connection stream.
    """
    from urllib.parse import quote

    slug = quote(str(Path(workspace).resolve()), safe="")
    return Path.home() / ".grok" / "sessions" / slug / grok_session_id / "updates.jsonl"


_COST_TICKS_PER_USD = 10_000_000_000


async def _post_usage(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    usage_state: dict[str, Any],
) -> None:
    """POST cumulative session usage; persists {context_tokens, cumulative_*}."""
    data: dict[str, Any] = {
        "context_tokens": usage_state["input_tokens"],
        "cumulative_input_tokens": usage_state["input_tokens"],
        "cumulative_output_tokens": usage_state["output_tokens"],
    }
    if usage_state["cost_ticks"]:
        data["cumulative_cost_usd"] = usage_state["cost_ticks"] / _COST_TICKS_PER_USD
    if usage_state["model"]:
        data["model"] = usage_state["model"]
    resp = await client.post(
        base_url.rstrip("/") + _EVENTS_PATH.format(session_id=session_id),
        headers=headers,
        json={"type": "external_session_usage", "data": data},
    )
    resp.raise_for_status()


def _usage_state_path(bridge_dir: Path) -> Path:
    return Path(bridge_dir) / "usage_state.json"


def _load_usage_state(bridge_dir: Path) -> dict[str, Any]:
    """Cumulative usage mirror state; survives serve re-creates for one session."""
    state = {"input_tokens": 0, "output_tokens": 0, "cost_ticks": 0, "model": ""}
    with contextlib.suppress(OSError, ValueError, KeyError, json.JSONDecodeError):
        state.update(json.loads(_usage_state_path(bridge_dir).read_text()))
    return state


def _save_usage_state(bridge_dir: Path, state: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        _usage_state_path(bridge_dir).write_text(json.dumps(state))


def _accumulate_turn_usage(state: dict[str, Any], usage: dict[str, Any]) -> None:
    """Fold one ``turn_completed`` usage block into cumulative totals."""
    state["input_tokens"] += int(usage.get("inputTokens") or 0)
    state["output_tokens"] += int(usage.get("outputTokens") or 0)
    state["cost_ticks"] += int(usage.get("costUsdTicks") or 0)
    model_usage = usage.get("modelUsage") or {}
    if isinstance(model_usage, dict) and model_usage:
        state["model"] = next(iter(model_usage))


async def follow_updates_log(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    updates_path: Path,
    offset_path: Path,
    bridge_dir: Path,
    agent_name: str = _DEFAULT_AGENT_NAME,
    poll_interval_s: float = 0.5,
) -> None:
    """Tail *updates_path* and mirror each ``session/update`` into Omnigent.

    Resumes at the byte offset persisted in *offset_path* so a recreated serve
    for the same Omnigent session does not re-mirror history; a fresh grok
    session starts at 0. Assistant chunks aggregate per turn into one message
    keyed by the turn's ``prompt_id``; ``turn_completed`` drives the idle edge
    plus a cumulative ``external_session_usage`` post.
    """
    offset = 0
    with contextlib.suppress(OSError, ValueError):
        offset = int(offset_path.read_text().strip())
    usage_state = _load_usage_state(bridge_dir)
    assistant_parts: list[str] = []
    running = False

    async def _flush_turn(update: dict[str, Any]) -> None:
        nonlocal running
        prompt_id = str(update.get("prompt_id") or "")
        if assistant_parts:
            try:
                await post_mirrored_item(
                    client,
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    item=MirroredItem(
                        kind="message",
                        role="assistant",
                        text="".join(assistant_parts),
                        response_id=prompt_id,
                    ),
                    agent_name=agent_name,
                )
            except Exception:
                logger.exception(
                    "grok forwarder failed to mirror assistant turn for %s", session_id
                )
            assistant_parts.clear()
        usage = update.get("usage") or {}
        if isinstance(usage, dict) and usage.get("inputTokens") is not None:
            _accumulate_turn_usage(usage_state, usage)
            _save_usage_state(bridge_dir, usage_state)
            try:
                await _post_usage(
                    client,
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    usage_state=usage_state,
                )
            except Exception:
                logger.exception("grok forwarder usage post failed for %s", session_id)
        if running:
            running = False
            try:
                await post_status(
                    client,
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    status="idle",
                )
            except Exception:
                logger.exception("grok forwarder idle edge failed for %s", session_id)

    while True:
        if not updates_path.exists():
            await asyncio.sleep(poll_interval_s)
            continue
        try:
            with updates_path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
        except OSError:
            await asyncio.sleep(poll_interval_s)
            continue
        for line in chunk.splitlines():
            try:
                record = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            params = record.get("params")
            if not isinstance(params, dict):
                continue
            update = params.get("update") or {}
            if update.get("sessionUpdate") == "turn_completed":
                await _flush_turn(update)
                continue
            if update.get("sessionUpdate") == "agent_message_chunk":
                text = ""
                content = update.get("content")
                if isinstance(content, dict) and content.get("type") == "text":
                    text = str(content.get("text") or "")
                if text:
                    if not running:
                        running = True
                        try:
                            await post_status(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                status="running",
                            )
                        except Exception:
                            logger.exception(
                                "grok forwarder running edge failed for %s", session_id
                            )
                    assistant_parts.append(text)
                continue
            for item in session_update_to_items(params):
                if not running:
                    running = True
                    try:
                        await post_status(
                            client,
                            base_url=base_url,
                            headers=headers,
                            session_id=session_id,
                            status="running",
                        )
                    except Exception:
                        logger.exception("grok forwarder running edge failed for %s", session_id)
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
        with contextlib.suppress(OSError):
            offset_path.write_text(str(offset))
        await asyncio.sleep(poll_interval_s)


def start_updates_follower(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    workspace: str,
    grok_session_id: str,
    bridge_dir: Path,
    agent_name: str = _DEFAULT_AGENT_NAME,
) -> asyncio.Task[None]:
    """Start the updates-log follower as a named background task."""
    return asyncio.create_task(
        follow_updates_log(
            client,
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            updates_path=grok_updates_log_path(workspace, grok_session_id),
            offset_path=Path(bridge_dir) / "updates.offset",
            bridge_dir=bridge_dir,
            agent_name=agent_name,
        ),
        name=f"grok-forwarder-{session_id}",
    )


__all__ = [
    "follow_updates_log",
    "grok_updates_log_path",
    "make_update_sink",
    "post_mirrored_item",
    "post_status",
    "start_updates_follower",
]
