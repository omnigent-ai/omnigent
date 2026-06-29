"""Tokenmaxx dispatch (#11) — run a work item as an in-process agent turn.

Mirrors :mod:`omnigent.runtime.schedule_dispatch`: posts a ``user`` message to
the work item's conversation via the real ``/v1/sessions/{id}/events`` endpoint
(in-process ASGI), so the turn reuses the full dispatch path (host relaunch,
policy, runner forward). Returns whether the turn was accepted, so the engine
only marks an item ``in_progress`` when a runner actually took it.

A work item with no linked conversation can't be dispatched here (spawning a
fresh sub-session for raw intake items is a follow-up); such items are skipped
so a later tick can retry once they're planned into a session.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from omnigent.entities.work_item import WorkItem

_logger = logging.getLogger(__name__)

# "No live runner / not dispatchable right now" — expected off-hours when a
# host is offline; logged at INFO and reported as not-accepted so the item is
# left for a later tick rather than marked in_progress.
_SOFT_STATUSES = frozenset({404, 409, 410, 503})


def _work_item_prompt(item: WorkItem) -> str:
    """Build the turn prompt for a work item from its title/body/plan."""
    parts = [f"[Tokenmaxx off-hours] Please make progress on this task:\n\n{item.title}"]
    if item.body:
        parts.append(item.body)
    if item.plan:
        parts.append(f"Plan:\n{item.plan}")
    return "\n\n".join(parts)


def build_work_item_dispatch(
    app: Any,
    *,
    identity_header: str,
    reserved_identities: frozenset[str] = frozenset(),
    timeout_s: float = 60.0,
) -> Callable[[WorkItem], Awaitable[bool]]:
    """Build a tokenmaxx dispatch callback that runs a work item as a turn.

    :param app: The ASGI application to dispatch into (the FastAPI server).
    :param identity_header: Trusted identity header to attribute the turn with.
    :param reserved_identities: Creator ids never sent as an explicit header
        (the reserved ``"local"`` sentinel — header-auth rejects it).
    :param timeout_s: Per-dispatch request timeout.
    :returns: An awaitable ``dispatch(item) -> bool`` for
        :class:`~omnigent.runtime.tokenmaxx.TokenmaxxService`.
    """

    async def dispatch(item: WorkItem) -> bool:
        if not item.conversation_id:
            _logger.info(
                "tokenmaxx: work item %s has no linked conversation; skipping (needs planning)",
                item.id,
            )
            return False

        headers: dict[str, str] = {}
        creator = item.assignee_user_id or item.created_by
        if creator and creator not in reserved_identities:
            headers[identity_header] = creator
        payload = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": _work_item_prompt(item)}],
            },
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://omnigent.internal",
                timeout=timeout_s,
            ) as client:
                resp = await client.post(
                    f"/v1/sessions/{item.conversation_id}/events",
                    json=payload,
                    headers=headers,
                )
        except Exception:
            _logger.exception("tokenmaxx: dispatch POST errored for work item %s", item.id)
            return False

        if resp.status_code in _SOFT_STATUSES:
            _logger.info(
                "tokenmaxx: no live runner for work item %s (conversation %s, HTTP %s) — "
                "leaving for a later tick",
                item.id,
                item.conversation_id,
                resp.status_code,
            )
            return False
        if resp.status_code >= 400:
            _logger.warning(
                "tokenmaxx: dispatch for work item %s → HTTP %s: %s",
                item.id,
                resp.status_code,
                resp.text[:200],
            )
            return False
        _logger.info(
            "tokenmaxx: dispatched work item %s into conversation %s",
            item.id,
            item.conversation_id,
        )
        return True

    return dispatch
