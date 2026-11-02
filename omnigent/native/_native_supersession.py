"""Create a native ``/clear`` replacement session and tell the old one it moved.

Native ``/clear`` (Claude Code's hook, Codex's new app-server thread) rotates
the terminal onto a fresh Omnigent session. The replacement keeps the old
session's title and project; the old session never sees another turn, so its
viewers need an explicit notice and redirect.
"""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Mapping

import httpx

from omnigent.entities.conversation import USER_SESSION_TITLE_MAX_CHARS

_logger = logging.getLogger(__name__)

_PROJECT_CREATE_FIELDS = ("project_id", "workspace", "git")


def clear_replacement_create_body(
    old_session: Mapping[str, object],
    *,
    agent_id: str,
    labels: dict[str, str],
) -> dict[str, object]:
    """
    Build the ``POST /v1/sessions`` body for a native ``/clear`` replacement.

    ``/clear`` resets only the conversation, so the replacement inherits the
    old session's title and first-class project. Explicit ``workspace`` and
    ``git`` nulls stop project defaults from starting a new workspace or
    worktree for the terminal that is being transferred.

    :param old_session: ``GET /v1/sessions/{id}`` snapshot of the session
        being cleared, e.g. ``{"title": "Fix login", "project_id": "proj_1"}``.
    :param agent_id: Agent for the replacement, e.g. ``"agent_claude"``.
    :param labels: Labels for the replacement, including its bridge id.
    :returns: JSON body for the create request.
    """
    body: dict[str, object] = {"agent_id": agent_id, "labels": labels}
    title = old_session.get("title")
    if isinstance(title, str) and title:
        body["title"] = title[:USER_SESSION_TITLE_MAX_CHARS]
    project_id = old_session.get("project_id")
    if isinstance(project_id, str) and project_id:
        body |= {"project_id": project_id, "workspace": None, "git": None}
    return body


def unfiled_clear_replacement_body(
    create_body: Mapping[str, object],
    response: httpx.Response,
) -> dict[str, object] | None:
    """
    Return the create body without its project after a project 404.

    The server answers 404 when the project was deleted or belongs to another
    user (a guest on a shared host). ``/clear`` still needs a continuation
    session, so the caller retries it unfiled.

    :param create_body: Body from :func:`clear_replacement_create_body`.
    :param response: Response to the first create request.
    :returns: Body to retry without project fields, or ``None`` when the
        first response needs no retry.
    """
    if response.status_code != httpx.codes.NOT_FOUND or "project_id" not in create_body:
        return None
    _logger.warning(
        "Could not file /clear replacement session in project %s; creating it unfiled",
        create_body["project_id"],
    )
    return {key: value for key, value in create_body.items() if key not in _PROJECT_CREATE_FIELDS}


async def post_clear_supersession(
    client: httpx.AsyncClient,
    *,
    old_session_id: str,
    new_session_id: str,
    agent_name: str,
) -> None:
    """
    Notify the superseded session that a ``/clear`` rotated it away.

    Posts three best-effort events to the OLD conversation, in order:

    1. An ``external_session_status: idle`` so the old conversation's
       "Working…" spinner stops — its terminal moved to the new session,
       so it will never receive the turn-end edge that would normally
       clear it.
    2. A persisted assistant ``message`` item linking to the new
       conversation, so a later reload of the cleared conversation
       explains what happened and offers the continuation link. This is
       the durable record — it survives reconnects.
    3. A transient ``external_session_superseded`` event the server
       republishes as ``session.superseded``, so a client *actively*
       viewing the old conversation auto-redirects to the new one.

    Each failure is logged and swallowed: the rotation has already
    completed and reset forwarder state, and a notification error must
    not disrupt the poll loop or stop the new session from forwarding.

    :param client: Omnigent HTTP client (``base_url`` = AP server).
    :param old_session_id: Superseded conversation id, e.g. ``"conv_old"``.
    :param new_session_id: Rotated-to conversation id, e.g. ``"conv_new"``.
    :param agent_name: Agent name to stamp on the notice message — an
        assistant ``message`` item requires one.
    :returns: None.
    """
    if old_session_id == new_session_id:
        # Defensive: never address the notice/redirect at the live session.
        # The caller resolves the old id from the pre-rotation forwarder
        # state, but if that ever collapses to the new id, posting here
        # would dump the "you were cleared" banner onto the active chat.
        return
    events_path = f"/v1/sessions/{urllib.parse.quote(old_session_id, safe='')}/events"
    try:
        status_resp = await client.post(
            events_path,
            json={
                "type": "external_session_status",
                "data": {"status": "idle"},
            },
        )
        status_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post /clear supersession idle status; old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
            extra={"session_id": old_session_id},
        )
    notice = (
        "This conversation was ended by `/clear`. "
        f"Continue in [the new chat](/c/{new_session_id}). "
        "You can also send a message here to resume this conversation."
    )
    try:
        item_resp = await client.post(
            events_path,
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
            "Failed to post /clear supersession notice; old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
            extra={"session_id": old_session_id},
        )
    try:
        event_resp = await client.post(
            events_path,
            json={
                "type": "external_session_superseded",
                "data": {"target_conversation_id": new_session_id},
            },
        )
        event_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "Failed to post /clear supersession redirect event; old_session=%s new_session=%s",
            old_session_id,
            new_session_id,
            exc_info=True,
            extra={"session_id": old_session_id},
        )
