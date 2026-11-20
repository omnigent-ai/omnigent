"""Cache the connected server's startup capabilities until reconnect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

import httpx


@dataclass
class _ServerInfo:
    payload: dict[str, Any]
    stale: bool = False


_server_info_cache: WeakKeyDictionary[httpx.AsyncClient, _ServerInfo] = WeakKeyDictionary()


def invalidate_server_info(server_client: httpx.AsyncClient | None) -> None:
    """Refresh lazily after reconnect, retaining the last success if the probe fails."""
    if server_client is not None and (cached := _server_info_cache.get(server_client)):
        cached.stale = True


async def get_server_info(
    server_client: httpx.AsyncClient | None, *, allow_stale: bool = True
) -> dict[str, Any]:
    """Return the connected server's startup capabilities."""
    if server_client is None:
        return {}
    cached = _server_info_cache.get(server_client)
    if cached is not None and not cached.stale:
        return cached.payload
    previous = cached.payload if allow_stale and cached is not None else {}
    try:
        # Capability discovery must not inherit a streaming client's unlimited read timeout.
        response = await server_client.get("/v1/info", timeout=5.0)
        if response.status_code != 200:
            return previous
        info = response.json()
    except (httpx.HTTPError, ValueError):
        return previous
    if not isinstance(info, dict):
        info = {}
    _server_info_cache[server_client] = _ServerInfo(info)
    return info


async def server_session_set_document_enabled(server_client: httpx.AsyncClient | None) -> bool:
    """Require a server that supports the runner-authorized document write."""
    info = await get_server_info(server_client, allow_stale=False)
    return info.get("session_set_document") is True
