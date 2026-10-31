"""Host addressing for cross-session runner requests."""

from typing import Any

import httpx

from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, databricks_request_headers
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY
from omnigent.util.uuid import uuid_to_bytes

# Local httpx metadata, never sent as an HTTP header.
REQUEST_HOST_EXTENSION = "omnigent_host_id"


async def post_on_host(
    client: httpx.AsyncClient,
    path: str,
    *,
    host_id: str | None,
    **kwargs: Any,
) -> httpx.Response:
    """Address one POST without changing the shared client's routing or credentials."""
    request = client.build_request("POST", path, **kwargs)
    request.headers.pop(OMNIGENT_SLICE_KEY_HEADER, None)
    request.headers.update(databricks_request_headers(str(client.base_url), host_id=host_id or ""))
    request.extensions[REQUEST_HOST_EXTENSION] = host_id or ""
    return await client.send(request)


def is_wrong_replica(response: httpx.Response) -> bool:
    """Only the explicit pre-delivery routing error permits re-addressing."""
    if response.status_code != 400:
        return False
    try:
        body = response.json()
        return isinstance(body, dict) and body.get("error", {}).get("code") == "wrong_replica"
    except (ValueError, AttributeError):
        return False


async def session_host_id(client: httpx.AsyncClient, session_id: str) -> str | None:
    """Resolve a target's host using authorized snapshots, including child inheritance."""
    from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY

    visited: set[str] = set()
    shared_runner_id: str | None = None
    for _ in range(16):
        session_id = uuid_to_bytes(session_id).hex()
        if session_id in visited:
            break
        visited.add(session_id)
        response = await client.get(
            f"/v1/sessions/{session_id}",
            params={
                "include_items": "false",
                "include_liveness": "false",
                "include_live_status": "false",
                "include_usage": "false",
            },
        )
        response.raise_for_status()
        snapshot = response.json()
        if shared_runner_id is not None and snapshot.get("runner_id") != shared_runner_id:
            return None
        labels = snapshot.get("labels") or {}
        host_id = snapshot.get("host_id") or labels.get(REQUESTED_HOST_ID_LABEL_KEY)
        if host_id:
            return str(host_id)
        parent = snapshot.get("parent_session_id")
        if not parent and labels.get(SIDE_CHAT_LABEL_KEY) == "1":
            parent = labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            shared_runner_id = snapshot.get("runner_id")
        if not parent:
            break
        session_id = parent
    return None
