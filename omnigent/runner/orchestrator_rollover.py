"""Ask the server to roll this project orchestrator over through the session event API."""

import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.session_host import normalize_session_id


async def execute_orchestrator_rollover_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """Schedule a rollover of *source_id*, the calling orchestrator, after this turn."""
    if client is None or source_id is None:
        return json.dumps({"error": "Rolling over requires an authenticated runner"})
    notes = args.get("notes")
    if notes is not None and not isinstance(notes, str):
        return json.dumps({"error": "notes must be text"})
    target = normalize_session_id(source_id)
    try:
        response = await client.post(
            f"/v1/sessions/{target}/events",
            headers={ORCHESTRATION_SOURCE_HEADER: source_id},
            json={"type": "rollover_orchestrator", "data": {"notes": (notes or "")[:8000]}},
            timeout=60,
        )
    except httpx.HTTPError:
        return json.dumps({"error": "The rollover request could not be confirmed"})
    if response.is_error:
        return json.dumps({"error": "Rollover refused", "detail": response.text[:1000]})
    return json.dumps(
        {
            "scheduled": True,
            "note": "Finish your reply now. Once this turn ends and nothing is queued, a new "
            "orchestrator takes over with your handoff; this session becomes read-only history.",
        }
    )
