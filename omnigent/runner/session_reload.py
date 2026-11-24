"""Ask the server to reload a session's tools through the session event API."""

import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.session_host import normalize_session_id


async def execute_reload_tools_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    Schedule a tool reload for *source_id* or a session it created.

    :param args: Tool arguments, e.g. ``{"session_id": "<id>"}`` or ``{}`` for itself.
    :param client: Authenticated server client for this runner.
    :param source_id: The calling session's ID.
    :returns: JSON acknowledgement or error.
    """
    if client is None or source_id is None:
        return json.dumps({"error": "Reloading tools requires an authenticated runner"})
    raw_target = args.get("session_id") or source_id
    try:
        target = normalize_session_id(raw_target)
    except ValueError:
        return json.dumps({"error": "Provide a valid session_id, or omit it for this session"})
    try:
        response = await client.post(
            f"/v1/sessions/{target}/events",
            headers={ORCHESTRATION_SOURCE_HEADER: source_id},
            json={"type": "reload_tools", "data": {}},
            timeout=60,
        )
    except httpx.HTTPError:
        return json.dumps({"error": "The reload request could not be confirmed"})
    if response.is_error:
        return json.dumps({"error": "Tool reload refused", "detail": response.text[:1000]})
    note = (
        "Scheduled. The runner restarts with the current tools after this turn ends and "
        "nothing is queued; the conversation is kept."
        if target == normalize_session_id(source_id)
        else "Scheduled. The session's runner restarts with the current tools once it is idle."
    )
    return json.dumps({"scheduled": True, "session_id": target, "note": note})
