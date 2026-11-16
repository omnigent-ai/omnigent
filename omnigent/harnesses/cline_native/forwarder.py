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
import dataclasses
import logging
from pathlib import Path
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


# Hub restart marks sessions ``aborted``; they refuse further input and only
# a fresh ``session.create`` revives the conversation — terminal for the
# forwarder, which exits so the next terminal ensure can rebuild.
_DEAD_SESSION_STATUSES = {"aborted", "terminated", "deleted", "closed"}

_MAX_BACKOFF_S = 60.0


async def run_cline_forwarder(
    *,
    ws_url: str,
    auth_token: str,
    hub_session_id: str,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    agent_name: str = _DEFAULT_AGENT_NAME,
    cwd: str = "",
    executable: str = "",
    bridge_dir: Path | None = None,
) -> None:
    """Own the forwarder connection for one session's lifetime.

    Connects to the hub, subscribes to the session's event stream, and holds
    the socket open so :func:`make_event_sink` drains events into Omnigent
    posts. On socket loss the loop re-runs ``cline hub start`` (a hub restart
    rotates the auth token), refreshes bridge state, re-subscribes, and keeps
    mirroring; it exits only on cancellation or when the hub session itself
    is gone (``session_not_found`` / dead status), which the next terminal
    ensure rebuilds.

    :param cwd: Workspace root for re-ensuring the hub after a restart.
    :param executable: ``cline`` binary path for the same re-ensure.
    :param bridge_dir: Per-session bridge dir — refreshed coordinates are
        written back so the executor/approval paths pick up the new token.
    """
    from omnigent.harnesses.cline_native.client import ClineHubClient, ClineHubError

    backoff = 1.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        while True:
            try:
                client = await ClineHubClient.connect(
                    ws_url, auth_token, workspace_root=cwd, cwd=cwd
                )
            except (ClineHubError, OSError) as exc:
                logger.warning(
                    "cline forwarder %s: hub connect failed (%s); re-ensuring hub",
                    session_id,
                    exc,
                )
                refreshed = await _refresh_hub_endpoint(
                    cwd=cwd,
                    executable=executable,
                    bridge_dir=bridge_dir,
                    session_id=session_id,
                )
                if refreshed is not None:
                    ws_url, auth_token = refreshed.ws_url, refreshed.auth_token
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue
            try:
                record = await client.session_get(hub_session_id)
            except ClineHubError as exc:
                if "session_not_found" in str(exc):
                    logger.error(
                        "cline forwarder %s: hub session %s no longer exists; exiting",
                        session_id,
                        hub_session_id,
                    )
                    await client.close()
                    return
                record = {}
            status = str(record.get("status") or "")
            if status in _DEAD_SESSION_STATUSES:
                logger.error(
                    "cline forwarder %s: hub session %s is %s; exiting",
                    session_id,
                    hub_session_id,
                    status,
                )
                await client.close()
                return
            _on_event, drain = make_event_sink(
                http,
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                agent_name=agent_name,
            )
            client.set_event_handler(_on_event)
            try:
                await client.subscribe(hub_session_id)
                logger.info(
                    "cline forwarder %s: subscribed to hub session %s at %s",
                    session_id,
                    hub_session_id,
                    ws_url,
                )
                code, reason = await client.wait_closed()
                logger.warning(
                    "cline forwarder %s: hub socket closed (code=%s reason=%r); reconnecting",
                    session_id,
                    code,
                    reason,
                )
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain
                await client.close()
            # A dropped socket usually means the hub restarted and rotated its
            # token — fetch fresh coordinates before the next connect attempt.
            refreshed = await _refresh_hub_endpoint(
                cwd=cwd,
                executable=executable,
                bridge_dir=bridge_dir,
                session_id=session_id,
            )
            if refreshed is not None:
                ws_url, auth_token = refreshed.ws_url, refreshed.auth_token
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)


async def _refresh_hub_endpoint(
    *,
    cwd: str,
    executable: str,
    bridge_dir: Path | None,
    session_id: str,
) -> Any | None:
    """Re-run ``cline hub start`` and publish the fresh token in bridge state."""
    from omnigent.harnesses.cline_native.bridge import (
        read_bridge_state,
        write_bridge_state,
    )
    from omnigent.harnesses.cline_native.serve import ClineHubStartError, ensure_cline_hub

    try:
        endpoint = await ensure_cline_hub(cwd=cwd, executable=executable or None)
    except (ClineHubStartError, OSError) as exc:
        logger.warning(
            "cline forwarder %s: cline hub start failed during refresh: %s",
            session_id,
            exc,
        )
        return None
    if bridge_dir is not None:
        state = read_bridge_state(bridge_dir)
        if state is not None and (
            state.ws_url != endpoint.ws_url or state.auth_token != endpoint.auth_token
        ):
            write_bridge_state(
                bridge_dir,
                dataclasses.replace(
                    state, ws_url=endpoint.ws_url, auth_token=endpoint.auth_token
                ),
            )
            logger.info(
                "cline forwarder %s: refreshed bridge state for hub restart",
                session_id,
            )
    return endpoint


__all__ = [
    "make_event_sink",
    "post_mirrored_item",
    "post_status",
    "post_usage",
    "run_cline_forwarder",
]
