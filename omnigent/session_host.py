"""Session host affinity shared by CLI requests and the interactive REPL."""

from __future__ import annotations

import json
from urllib.parse import unquote, urlsplit

import httpx

from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, databricks_request_headers

_session_hosts: dict[str, str] = {}


def set_session_host(session_id: str, host_id: str | None) -> None:
    """Record a session's host; an absent host clears stale routing."""
    if host_id:
        _session_hosts[session_id] = host_id
    else:
        _session_hosts.pop(session_id, None)


def get_session_host(session_id: str) -> str | None:
    """Return the last observed host, or None for an unknown or hostless session."""
    return _session_hosts.get(session_id)


def session_routing_headers(url: str) -> dict[str, str]:
    """Return the observed session owner's routing headers for HTTP or WS."""
    parsed = urlsplit(url)
    base_path, separator, route = parsed.path.partition("/v1/sessions/")
    host_id = get_session_host(unquote(route.split("/")[0])) if separator else None
    if host_id is None:
        return {}
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    base_url = parsed._replace(scheme=scheme, path=base_path, query="", fragment="").geturl()
    return databricks_request_headers(base_url, host_id=host_id)


async def route_known_session_request(request: httpx.Request) -> None:
    """Prefer an observed session owner over the caller's default host."""
    request.headers.update(session_routing_headers(str(request.url)))


async def route_session_request(request: httpx.Request) -> None:
    """Route a session request by its host without changing shared client headers."""
    path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    base_path, separator, route = path.partition("/v1/sessions")
    if not separator or (route and not route.startswith("/")):
        return
    host_id = None
    if (
        not route
        and request.method == "POST"
        and "application/json" in request.headers.get("content-type", "")
    ):
        try:
            body = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("host_id"), str):
            host_id = body["host_id"]
    elif route.startswith("/"):
        host_id = get_session_host(unquote(route.split("/")[1]))
    request.headers.pop(OMNIGENT_SLICE_KEY_HEADER, None)
    request.headers.update(
        databricks_request_headers(
            str(request.url.copy_with(path=unquote(base_path), query=None)), host_id=host_id
        )
    )
