"""Adapt peer messages to the existing session event API."""

import json
from typing import Any, Literal

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.session_host import normalize_session_id
from omnigent.tools.builtins.session_message import (
    SESSION_MESSAGE_MAX_CHARS,
    peer_session_messaging_enabled,
)

# A failed send's ``delivery``: "refused" when nothing was queued, "unconfirmed" when it
# may have been. Accepted sends return only the server's acknowledgement.
_Delivery = Literal["refused", "unconfirmed"]
_UNCONFIRMED_ERROR = "Delivery could not be confirmed; do not automatically resend"


def _failed_send(error: str, delivery: _Delivery, **fields: str) -> str:
    """
    Build a failed send's tool result.

    :param error: What went wrong, e.g. ``"Session message refused"``.
    :param delivery: ``"refused"`` (nothing was queued) or ``"unconfirmed"`` (it may have been).
    :param fields: Extra string fields, e.g. ``detail`` with the server's refusal text.
    :returns: JSON such as ``{"error": "Session message refused", "delivery": "refused"}``.
    """
    return json.dumps({"error": error, "delivery": delivery, **fields})


async def execute_message_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    Send ``args["message"]`` to session ``args["session_id"]`` as session ``source_id``.

    :returns: The server's acknowledgement JSON (a boolean ``queued``) when it accepted
        the message, else an ``error`` with ``delivery`` saying whether it may be queued.
    """
    try:
        target = normalize_session_id(args.get("session_id"))
    except ValueError:
        return _failed_send("Provide a valid session_id (UUID or numeric managed ID)", "refused")
    message = args.get("message")
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > SESSION_MESSAGE_MAX_CHARS
    ):
        return _failed_send(
            f"Provide a non-blank message of 1–{SESSION_MESSAGE_MAX_CHARS} characters", "refused"
        )
    if client is None or source_id is None:
        return _failed_send("Session messaging requires an authenticated runner", "refused")
    if not peer_session_messaging_enabled():
        return _failed_send("Messaging other sessions is disabled on this runner", "refused")
    try:
        path = f"/v1/sessions/{target}/events"
        payload = {"type": "session_message", "data": {"message": message}}
        headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
        response = await client.post(path, headers=headers, json=payload, timeout=60)
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise httpx.HTTPError("Delivery acknowledgement unavailable")
    except (httpx.HTTPError, ValueError):
        return _failed_send(_UNCONFIRMED_ERROR, "unconfirmed")
    if response.is_error:
        return _failed_send("Session message refused", "refused", detail=response.text[:1000])
    try:
        acknowledgement = response.json()
        if not isinstance(acknowledgement, dict) or not isinstance(
            acknowledgement.get("queued"), bool
        ):
            raise ValueError("Expected a delivery acknowledgement with boolean queued")
        if acknowledgement.get("denied") is True:
            acknowledgement["error"] = (
                f"Session message denied by policy: {acknowledgement.get('reason', 'Denied')}"
            )
            acknowledgement["delivery"] = "refused"
        return json.dumps(acknowledgement)
    except ValueError:
        return _failed_send(_UNCONFIRMED_ERROR, "unconfirmed")
