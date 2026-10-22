"""Thin adapter into ordinary session event delivery."""

import json
from typing import Any

import httpx

from omnigent.db.db_models import normalize_uuid


async def execute_message_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    target = (
        normalize_uuid(args.get("session_id")) if isinstance(args.get("session_id"), str) else None
    )
    message = args.get("message")
    if not target or not isinstance(message, str) or not message.strip() or len(message) > 8000:
        return json.dumps({"error": "Provide a session_id and a message of 1–8000 characters"})
    if client is None or source_id is None:
        return json.dumps({"error": "Session messaging requires an authenticated runner"})
    try:
        response = await client.post(
            f"/v1/sessions/{target}/events",
            json={"type": "session_message", "data": {"message": message}},
            timeout=60,
        )
    except httpx.HTTPError:
        return json.dumps(
            {"error": "Delivery could not be confirmed; do not automatically resend"}
        )
    if response.is_error:
        return json.dumps(
            {
                "error": "Session message refused",
                "status": response.status_code,
                "detail": response.text[:1000],
            }
        )
    return json.dumps(response.json())
