"""Adapt peer messages to the existing session event API."""

import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import is_wrong_replica, post_on_host, session_host_id
from omnigent.util.uuid import uuid_to_bytes


async def execute_message_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    target = args.get("session_id")
    try:
        target = uuid_to_bytes(target).hex() if isinstance(target, str) else None
    except ValueError:
        target = None
    message = args.get("message")
    if (
        not target
        or len(target) != 32
        or not isinstance(message, str)
        or not message.strip()
        or len(message) > 8000
    ):
        return json.dumps({"error": "Provide a session_id and a message of 1–8000 characters"})
    if client is None or source_id is None:
        return json.dumps({"error": "Session messaging requires an authenticated runner"})
    try:
        path = f"/v1/sessions/{target}/events"
        payload = {"type": "session_message", "data": {"message": message}}
        headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
        response = await client.post(path, headers=headers, json=payload, timeout=60)
        if is_wrong_replica(response):
            try:
                host_id = await session_host_id(client, target)
            except (httpx.HTTPError, ValueError):
                return json.dumps(
                    {"error": "Message not delivered: target host could not be resolved"}
                )
            response = await post_on_host(
                client, path, host_id=host_id, headers=headers, json=payload, timeout=60
            )
            if host_id and is_wrong_replica(response):
                # Older hosts may have registered on the default, unkeyed replica.
                response = await post_on_host(
                    client, path, host_id=None, headers=headers, json=payload, timeout=60
                )
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise httpx.HTTPError("Delivery acknowledgement unavailable")
    except (httpx.HTTPError, ValueError):
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )
    if response.is_error:
        return json.dumps({"error": "Session message refused", "detail": response.text[:1000]})
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
        return json.dumps(acknowledgement)
    except ValueError:
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )
