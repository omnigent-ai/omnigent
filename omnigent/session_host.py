"""Session host affinity shared by the runner, the CLI and the interactive REPL."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import unquote, urlsplit

import httpx

from omnigent.cli_auth import (
    OMNIGENT_SLICE_KEY_HEADER,
    UNKEYED_REQUEST_EXTENSION,
    databricks_request_headers,
)
from omnigent.util.uuid import uuid_to_bytes

# Local httpx metadata naming the host a request was addressed to; never sent as a header.
REQUEST_HOST_EXTENSION = "omnigent_host_id"
# The wrapped client has no read timeout, so the host lookup bounds its own wait.
SESSION_HOST_LOOKUP_TIMEOUT_S = 3.0
_MAX_PARENT_HOPS = 16
# Collection creates: a body host_id keys the create to that host.
_CREATE_ROUTES = frozenset({"", "/orchestrated"})
_LOOKUP_PARAMS = {
    "include_items": "false",
    "include_liveness": "false",
    "include_live_status": "false",
    "include_usage": "false",
}
_LOOKUP_TIMEOUT = dict.fromkeys(
    ("connect", "read", "write", "pool"), SESSION_HOST_LOOKUP_TIMEOUT_S
)
_BODY_HEADERS = frozenset({"content-length", "content-type", "transfer-encoding"})

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
    if not request.extensions.get(UNKEYED_REQUEST_EXTENSION):
        request.headers.update(session_routing_headers(str(request.url)))


async def route_session_request(request: httpx.Request) -> None:
    """Route a session request by its host without changing shared client headers."""
    path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    base_path, separator, route = path.partition("/v1/sessions")
    if not separator or (route and not route.startswith("/")):
        return
    if request.extensions.get(UNKEYED_REQUEST_EXTENSION):
        request.headers.pop(OMNIGENT_SLICE_KEY_HEADER, None)
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


def normalize_session_id(value: object) -> str:
    """Preserve managed numeric IDs and canonicalize OSS UUIDs for session routes."""
    if not isinstance(value, str):
        raise ValueError("Session ID must be a string")
    if value.isascii() and value.isdecimal():
        return value
    uuid_bytes = uuid_to_bytes(value)
    if len(uuid_bytes) != 16:
        raise ValueError("Session UUID must decode to exactly 16 bytes")
    return uuid_bytes.hex()


def is_wrong_replica(response: httpx.Response) -> bool:
    """Only the explicit pre-delivery routing error permits re-addressing."""
    if response.status_code != 400:
        return False
    try:
        body = response.json()
        return isinstance(body, dict) and body.get("error", {}).get("code") == "wrong_replica"
    except (ValueError, AttributeError):
        return False


def address_request(request: httpx.Request, base_url: str, host_id: str) -> None:
    """Key one request to a host, or send it keyless when host_id is empty."""
    request.headers.pop(OMNIGENT_SLICE_KEY_HEADER, None)
    request.headers.update(databricks_request_headers(base_url, host_id=host_id))
    request.extensions[REQUEST_HOST_EXTENSION] = host_id


def _split_session_path(request: httpx.Request) -> tuple[str, str] | None:
    """Return the path before ``/v1/sessions`` and the route after it, or None."""
    try:
        path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    base_path, separator, route = path.partition("/v1/sessions")
    if not separator or (route and not route.startswith("/")):
        return None
    return unquote(base_path), route


def _session_key(route: str) -> str | None:
    """The canonical id of the session a route names, or None for collection routes."""
    segment = route.split("/")[1] if route.startswith("/") else ""
    if not segment:
        return None
    try:
        return normalize_session_id(unquote(segment))
    except (ValueError, TypeError):
        # Collection routes such as /orchestrated and /projects are not session ids.
        return None


def _create_body_host(request: httpx.Request) -> str | None:
    """The host_id a JSON create body names, or None."""
    if request.method != "POST" or "application/json" not in request.headers.get(
        "content-type", ""
    ):
        return None
    try:
        body = json.loads(request.content)
    except ValueError:
        return None
    host_id = body.get("host_id") if isinstance(body, dict) else None
    return host_id if isinstance(host_id, str) else None


async def _wrong_replica_refusal(response: httpx.Response) -> dict | None:
    """The error object of a wrong_replica refusal, or None. Event streams are never read."""
    if response.status_code != 400:
        return None
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return None
    await response.aread()
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return None
    if isinstance(error, dict) and error.get("code") == "wrong_replica":
        return error
    return None


class SessionRoutingTransport(httpx.AsyncBaseTransport):
    """Send each session-scoped request to its session's host, recovering once from wrong_replica.

    For a request whose path is ``/v1/sessions/{id}...`` with a real session id:

    - A cached host keys the request.
    - Otherwise the caller's own key is sent unchanged, with no lookup.
    - On ``wrong_replica`` that names a slice key in its body, that key is cached and
      retried once; a null key means resend keyless. Otherwise the host is looked up
      and cached, and retried once if it differs from the cached host. Whatever
      remains is one keyless attempt, and nothing else is retried.

    A create whose JSON body names a ``host_id`` is keyed by it and never retried.
    Timeouts and other failures propagate unchanged, because delivery is uncertain.
    Established event streams are never inspected or replayed; JSON routing
    refusals before stream establishment may recover like other requests.

    Wraps the network transport, so clients stay plain ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = True,
    ) -> None:
        # The inner client owns the network path, so env proxies apply as in a plain client.
        self._network = httpx.AsyncClient(transport=transport, trust_env=trust_env)

    async def aclose(self) -> None:
        await self._network.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parts = _split_session_path(request)
        if parts is None:
            return await self._send(request)
        # Read the body once so a retry can resend it; only session routes need that.
        await request.aread()
        base_path, route = parts
        base_url = str(request.url.copy_with(path=base_path, query=None))
        if request.extensions.get(UNKEYED_REQUEST_EXTENSION):
            address_request(request, base_url, "")
            return await self._send(request)
        if route in _CREATE_ROUTES:
            create_host = _create_body_host(request)
            if create_host is not None:
                address_request(request, base_url, create_host)
            return await self._send(request)
        session_id = _session_key(route)
        if session_id is None:
            return await self._send(request)

        caller_headers = request.headers.copy()
        cached = get_session_host(session_id)
        if cached is not None:
            address_request(request, base_url, cached)
        response = await self._send(request)
        refusal = await _wrong_replica_refusal(response)
        if refusal is None:
            return response
        await response.aclose()

        if "slice_key" in refusal:
            # The refusal names the host's key, or null when the request must go keyless.
            named = refusal["slice_key"]
            if isinstance(named, str):
                set_session_host(session_id, named)
                address_request(request, base_url, named)
                response = await self._send(request)
                if await _wrong_replica_refusal(response) is None:
                    return response
                await response.aclose()
        else:
            resolved, current = await self._resolve_after_refusal(
                base_url, caller_headers, session_id
            )
            if resolved and current is not None and current != cached:
                address_request(request, base_url, current)
                response = await self._send(request)
                if await _wrong_replica_refusal(response) is None:
                    return response
                await response.aclose()
        address_request(request, base_url, "")
        return await self._send(request)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        return await self._network.send(request, stream=True)

    async def _resolve_after_refusal(
        self, base_url: str, caller_headers: httpx.Headers, session_id: str
    ) -> tuple[bool, str | None]:
        """Look up and cache the host; (False, None) when the lookup fails."""
        try:
            async with asyncio.timeout(SESSION_HOST_LOOKUP_TIMEOUT_S):
                host_id = await self._lookup_host(base_url, caller_headers, session_id)
        except (httpx.HTTPError, ValueError, TimeoutError):
            return False, None
        set_session_host(session_id, host_id)
        return True, host_id

    async def _lookup_host(
        self, base_url: str, caller_headers: httpx.Headers, session_id: str
    ) -> str | None:
        """Resolve a session's host from authorized snapshots, following child inheritance."""
        from omnigent.server.managed_hosts import REQUESTED_HOST_ID_LABEL_KEY
        from omnigent.stores.conversation_store import (
            SIDE_CHAT_LABEL_KEY,
            SIDE_CHAT_SOURCE_LABEL_KEY,
        )

        headers = httpx.Headers(
            [(k, v) for k, v in caller_headers.multi_items() if k.lower() not in _BODY_HEADERS]
        )
        visited: set[str] = set()
        shared_runner_id: str | None = None
        current = session_id
        for _ in range(_MAX_PARENT_HOPS):
            current = normalize_session_id(current)
            if current in visited:
                break
            visited.add(current)
            request = httpx.Request(
                "GET",
                f"{base_url}/v1/sessions/{current}",
                params=_LOOKUP_PARAMS,
                headers=headers,
                extensions={"timeout": _LOOKUP_TIMEOUT},
            )
            response = await self._network.send(request)
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
            current = parent
        return None
