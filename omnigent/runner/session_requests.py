"""Runner-side session addressing: create placement and host hints for known sessions."""

from typing import Any

import httpx

from omnigent.session_host import (
    address_request,
    is_wrong_replica,  # noqa: F401 (chat.py imports it from here)
    normalize_session_id,
    set_session_host,
)


async def post_on_host(
    client: httpx.AsyncClient,
    path: str,
    *,
    host_id: str | None,
    **kwargs: Any,
) -> httpx.Response:
    """Address one POST to a host; an empty host sends it keyless, not with the runner's key."""
    request = client.build_request("POST", path, **kwargs)
    address_request(request, str(client.base_url), host_id or "")
    return await client.send(request)


def remember_session_host(session_id: str | None, host_id: str | None) -> None:
    """Cache the host a session is known to live on, so its first request is keyed correctly."""
    if not session_id or not host_id:
        return
    try:
        key = normalize_session_id(session_id)
    except ValueError:
        return
    set_session_host(key, host_id)
