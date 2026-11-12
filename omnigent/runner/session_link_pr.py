"""Link a session's PR through the authenticated resource API."""

import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import (
    is_wrong_replica,
    normalize_session_id,
    post_on_host,
    session_host_id,
)


async def execute_link_pr_tool(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    if client is None or source_id is None:
        return json.dumps({"error": "PR linking requires an authenticated runner"})
    try:
        target = normalize_session_id(args.get("session_id", source_id))
    except ValueError:
        return json.dumps({"error": "Provide a valid session_id"})
    url = args.get("url")
    action = args.get("action", "attach")
    if not isinstance(url, str) or not url.strip() or action not in ("attach", "remove"):
        return json.dumps({"error": "Provide a PR url and action attach or remove"})
    requested = False
    try:
        info = await client.get("/v1/info", timeout=30)
        info.raise_for_status()
        if info.json().get("scripted_sessions") is not True:
            return json.dumps(
                {"error": "Scripted session PR linking is unavailable on this server"}
            )
        host_id = await session_host_id(client, target)
        path = f"/v1/sessions/{target}/resources/github/prs/orchestrated"
        headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
        payload = {"url": url, "action": action}
        requested = True
        if host_id:
            response = await post_on_host(
                client, path, host_id=host_id, headers=headers, json=payload, timeout=60
            )
        else:
            # Hostless runners use the client's existing replica affinity.
            response = await client.post(path, headers=headers, json=payload, timeout=60)
        if host_id and is_wrong_replica(response):
            # Older hosts can be registered on the unkeyed replica. This explicit
            # routing refusal precedes mutation; ambiguous outcomes are never replayed.
            response = await post_on_host(
                client, path, host_id=None, headers=headers, json=payload, timeout=60
            )
        if response.is_error:
            if response.status_code >= 500 or response.status_code in (408, 429):
                raise httpx.HTTPError("Missing acknowledgement")
            return json.dumps({"error": "PR link update refused", "detail": response.text[:1000]})
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Expected PR panel state")
        return json.dumps(result)
    except (httpx.HTTPError, ValueError):
        if requested:
            return json.dumps(
                {
                    "error": "PR link update could not be confirmed; "
                    "inspect the session's PR panel before retrying"
                }
            )
        return json.dumps(
            {"error": "Could not resolve PR linking capability or target host; no update was sent"}
        )
