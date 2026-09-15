"""Shared supersession notice for native-harness session rotation.

The claude-native, codex-native, and antigravity-native harnesses all move the
Omnigent session binding onto a fresh conversation when the user starts a new
vendor conversation in the pane (``/clear`` in Claude/Antigravity, ``/new`` in
Codex). The abandoned OLD conversation must be told, or its web view is
stranded: the "Working…" spinner never clears (its terminal moved to the new
session, so the turn-end edge never arrives), no message links to where work
continued, and a client actively viewing it never redirects.

:func:`post_supersession_notice` is that notification, extracted from the
claude-native forwarder so every rotation path shares one implementation.
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

_logger = logging.getLogger(__name__)


async def post_supersession_notice(
    client: httpx.AsyncClient,
    *,
    old_session_id: str,
    new_session_id: str,
    agent_name: str,
    command: str,
) -> None:
    """
    Notify the superseded session that a new-conversation command rotated it away.

    Posts three best-effort events to the OLD conversation, in order:

    1. An ``external_session_status: idle`` so the old conversation's
       "Working…" spinner stops — its terminal moved to the new session,
       so it will never receive the turn-end edge that would normally
       clear it.
    2. A persisted assistant ``message`` item linking to the new
       conversation, so a later reload of the superseded conversation
       explains what happened and offers the continuation link. This is
       the durable record — it survives reconnects.
    3. A transient ``external_session_superseded`` event the server
       republishes as ``session.superseded``, so a client *actively*
       viewing the old conversation auto-redirects to the new one.

    Each failure is logged and swallowed: the rotation has already
    completed, and a notification error must not disrupt the caller's
    forward/read loop or stop the new session from forwarding.

    :param client: Omnigent HTTP client (``base_url`` = AP server).
    :param old_session_id: Superseded conversation id, e.g. ``"conv_old"``.
    :param new_session_id: Rotated-to conversation id, e.g. ``"conv_new"``.
    :param agent_name: Agent name to stamp on the notice message — an
        assistant ``message`` item requires one.
    :param command: Vendor command that rotated the session, e.g.
        ``"/clear"`` or ``"/new"``. Named in the notice text.
    :returns: None.
    """
    if old_session_id == new_session_id:
        # Defensive: never address the notice/redirect at the live session.
        # If the caller's old id ever collapses to the new id, posting here
        # would dump the supersession banner onto the active chat.
        return
    old_events_url = f"/v1/sessions/{urllib.parse.quote(old_session_id, safe='')}/events"
    try:
        status_resp = await client.post(
            old_events_url,
            json={
                "type": "external_session_status",
                "data": {"status": "idle"},
            },
        )
        status_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post %s supersession idle status; old_session=%s new_session=%s",
            command,
            old_session_id,
            new_session_id,
            exc_info=True,
            extra={"session_id": old_session_id},
        )
    notice = (
        f"This conversation was ended by `{command}`. "
        f"Continue in [the new chat](/c/{new_session_id}). "
        "You can also send a message here to resume this conversation."
    )
    try:
        item_resp = await client.post(
            old_events_url,
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "assistant",
                        "agent": agent_name,
                        "content": [{"type": "output_text", "text": notice}],
                    },
                },
            },
        )
        item_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post %s supersession notice; old_session=%s new_session=%s",
            command,
            old_session_id,
            new_session_id,
            exc_info=True,
            extra={"session_id": old_session_id},
        )
    try:
        event_resp = await client.post(
            old_events_url,
            json={
                "type": "external_session_superseded",
                "data": {"target_conversation_id": new_session_id},
            },
        )
        event_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post %s supersession redirect event; old_session=%s new_session=%s",
            command,
            old_session_id,
            new_session_id,
            exc_info=True,
            extra={"session_id": old_session_id},
        )
