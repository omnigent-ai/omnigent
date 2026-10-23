"""Adapt peer messages to the existing session event API."""

import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
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
        response = await client.post(
            f"/v1/sessions/{target}/events",
            headers={ORCHESTRATION_SOURCE_HEADER: source_id},
            json={"type": "session_message", "data": {"message": message}},
            timeout=60,
        )
    except httpx.HTTPError:
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )
    if response.is_error:
        return json.dumps({"error": "Session message refused", "detail": response.text[:1000]})
    try:
        return json.dumps(response.json())
    except ValueError:
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )
