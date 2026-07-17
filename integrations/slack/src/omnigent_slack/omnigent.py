from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx


class OmnigentError(RuntimeError):
    pass


class RunnerUnavailableError(OmnigentError):
    pass


class AuthRequiredError(OmnigentError):
    """The Omnigent server rejected an unauthenticated request (HTTP 401).

    The Slack bot has no way to authenticate yet, so callers surface this as a
    "not supported" message during setup rather than retrying.
    """


class ServerUnreachableError(OmnigentError):
    """The Omnigent server could not be reached at all (transport failure)."""


class HostUnavailableError(OmnigentError):
    """No online host could serve the session.

    Raised when the server reports no online hosts, the user's preferred host is
    offline/missing, or a launched runner never comes online — cases the user
    resolves by starting a host with ``omni host --server <url>``.
    """


@dataclass(frozen=True, slots=True)
class ValidatedServer:
    """Outcome of probing an Omnigent server during Slack setup."""

    agents: list[dict[str, Any]]
    online_hosts: list[dict[str, Any]]


class ClientAuth:
    """Holds a Slack user's delegated bearer token for one server.

    Supplies the current access token on every request and knows how to
    refresh it. ``refresh`` returns the new access token, or ``None`` if
    the grant is gone (revoked / expired) — the caller then surfaces a
    re-login prompt.
    """

    def __init__(
        self,
        access_token: str,
        refresh: Callable[[], Awaitable[str | None]],
    ) -> None:
        self.access_token: str | None = access_token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    async def refresh(self, used_token: str | None) -> str | None:
        """Rotate the token, single-flighting concurrent callers.

        Turns for one user run in different threads but share this
        instance, so an expired token 401s several of them at once. Rotating
        refresh tokens are single-use, so a second rotation would consume the
        just-minted refresh token and revoke the whole grant — logging the
        user out mid-session. ``used_token`` is the access token the failed
        request actually sent; if the live token no longer matches it, another
        caller already rotated, so we adopt that result instead of rotating
        again.
        """
        async with self._lock:
            if self.access_token != used_token:
                return self.access_token
            token = await self._refresh()
            self.access_token = token
            return token


class OmnigentClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        runner_launch_timeout_seconds: float = 60.0,
        auth: ClientAuth | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, read=None),
        )
        self._runner_launch_timeout_seconds = runner_launch_timeout_seconds
        self._auth = auth
        self._logger = logging.getLogger(__name__)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _auth_headers(self) -> dict[str, str]:
        if self._auth is not None and self._auth.access_token:
            return {"Authorization": f"Bearer {self._auth.access_token}"}
        return {}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # A transport failure (DNS, refused connection, timeout) means the
        # server itself is unreachable — distinct from an HTTP error response,
        # which ``_raise_for_status`` classifies.
        used_token = self._auth.access_token if self._auth is not None else None
        headers = {**self._auth_headers(), **(kwargs.pop("headers", None) or {})}
        try:
            response = await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ServerUnreachableError(
                f"Could not reach Omnigent server at {self._client.base_url}: {exc}"
            ) from exc
        # A delegated token expires within the hour; on a 401 refresh once
        # and retry so long-lived threads keep working without re-login.
        if response.status_code == 401 and self._auth is not None:
            new_token = await self._auth.refresh(used_token)
            if new_token:
                retry_headers = {**self._auth_headers(), **(kwargs.pop("headers", None) or {})}
                try:
                    response = await self._client.request(
                        method, url, headers=retry_headers, **kwargs
                    )
                except httpx.HTTPError as exc:
                    raise ServerUnreachableError(
                        f"Could not reach Omnigent server at {self._client.base_url}: {exc}"
                    ) from exc
        return response

    async def check_health(self) -> None:
        # Liveness probe against the public ``/health`` endpoint, confirming the
        # server is reachable before setup lists its agents and hosts.
        self._logger.debug("Probing Omnigent server health")
        response = await self._request("GET", "/health")
        await _raise_for_status(response)

    async def validate(self) -> ValidatedServer:
        # Setup-time probe. Confirms the server is reachable (``/health``) and
        # that unauthenticated access works — ``list_agents`` hits an
        # auth-gated endpoint, so a server with auth enabled raises
        # ``AuthRequiredError`` here. Returns the agents and online hosts that
        # populate the setup select menus.
        await self.check_health()
        agents = await self.list_agents()
        hosts = await self.list_hosts()
        online_hosts = [host for host in hosts if _is_host_online(host)]
        return ValidatedServer(agents=agents, online_hosts=online_hosts)

    async def create_session(self, agent_id: str, title: str) -> str:
        self._logger.info("Creating Omnigent session agent_id=%s title=%r", agent_id, title)
        response = await self._request(
            "POST",
            "/v1/sessions",
            json={"agent_id": agent_id, "title": title},
        )
        await _raise_for_status(response)
        payload = response.json()
        session_id = _extract_session_id(payload)
        if session_id is None:
            raise OmnigentError(f"Create session response did not include an id: {payload!r}")
        self._logger.info("Created Omnigent session session_id=%s", session_id)
        return session_id

    async def submit_message(self, session_id: str, text: str) -> None:
        self._logger.info(
            "Submitting Slack message to Omnigent session_id=%s chars=%s",
            session_id,
            len(text),
        )
        payload = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        response = await self._request("POST", f"/v1/sessions/{session_id}/events", json=payload)
        await _raise_for_status(response)
        self._logger.debug("Submitted Omnigent message session_id=%s", session_id)

    async def launch_runner(
        self,
        session_id: str,
        *,
        workspace: str,
        host_id: str | None = None,
    ) -> str:
        # This server keeps no standing runners — each session spawns one on
        # demand. ``POST /v1/hosts/{host_id}/runners`` is the only primitive
        # that makes a session live, and it requires an absolute ``workspace``
        # path on the host.
        if not workspace:
            raise OmnigentError(
                "A workspace path is required to launch an Omnigent runner. "
                "Re-run setup and set a workspace."
            )
        target_host = host_id or await self._select_random_online_host()
        self._logger.info(
            "Launching Omnigent runner session_id=%s host_id=%s workspace=%s",
            session_id,
            target_host,
            workspace,
        )
        response = await self._request(
            "POST",
            f"/v1/hosts/{target_host}/runners",
            json={"session_id": session_id, "workspace": workspace},
        )
        # A 404 (unknown host) or 409 (host offline / connection replaced) means
        # the chosen host can't serve the session — surface it as host-unavailable
        # so the caller can tell the user to start a host.
        if response.status_code in (404, 409):
            raise HostUnavailableError(
                f"Omnigent host {target_host} is not available: {response.text}"
            )
        await _raise_for_status(response)
        payload = response.json()
        runner_id = _extract_runner_id(payload)
        if runner_id is None:
            raise OmnigentError(f"Launch runner response did not include a runner id: {payload!r}")

        await self.wait_for_runner_online(runner_id)
        self._logger.info(
            "Launched Omnigent runner session_id=%s runner_id=%s host_id=%s",
            session_id,
            runner_id,
            target_host,
        )
        return runner_id

    async def list_agents(self) -> list[dict[str, Any]]:
        self._logger.debug("Listing built-in Omnigent agents")
        response = await self._request("GET", "/v1/agents")
        await _raise_for_status(response)
        payload = response.json()
        data = _extract_list(payload, "data") or _extract_list(payload, "agents")
        if data is None:
            data = payload if isinstance(payload, list) else []
        agents = [item for item in data if isinstance(item, dict)]
        self._logger.info("Found built-in Omnigent agents count=%s", len(agents))
        return agents

    async def list_hosts(self) -> list[dict[str, Any]]:
        self._logger.debug("Listing Omnigent hosts")
        response = await self._request("GET", "/v1/hosts")
        await _raise_for_status(response)
        payload = response.json()
        data = _extract_list(payload, "hosts") or _extract_list(payload, "data")
        if data is None:
            data = payload if isinstance(payload, list) else []
        hosts = [item for item in data if isinstance(item, dict)]
        self._logger.info("Found Omnigent hosts count=%s", len(hosts))
        return hosts

    async def wait_for_runner_online(self, runner_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._runner_launch_timeout_seconds
        while True:
            response = await self._request("GET", f"/v1/runners/{runner_id}/status")
            await _raise_for_status(response)
            payload = response.json()
            if isinstance(payload, dict) and payload.get("online") is True:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise HostUnavailableError(
                    f"Timed out waiting for launched Omnigent runner to come online: {runner_id}"
                )
            await asyncio.sleep(1)

    async def _select_random_online_host(self) -> str:
        hosts = await self.list_hosts()
        host_ids = [
            host_id
            for host in hosts
            if _is_host_online(host) and (host_id := _host_id(host)) is not None
        ]
        if not host_ids:
            raise HostUnavailableError("No online Omnigent hosts are available to launch a runner.")
        host_id = random.choice(host_ids)
        self._logger.info(
            "Selected random Omnigent host host_id=%s candidates=%s",
            host_id,
            len(host_ids),
        )
        return host_id

    async def get_host_home(self, host_id: str) -> str | None:
        # The host does not advertise its working directory, but listing its
        # filesystem with no path makes the host expand ``~`` and return entries
        # with absolute paths. The home directory is the parent of any entry —
        # the same derivation the web UI uses to seed the workspace field.
        self._logger.debug("Resolving host home host_id=%s", host_id)
        response = await self._request("GET", f"/v1/hosts/{host_id}/filesystem")
        await _raise_for_status(response)
        payload = response.json()
        entries = _extract_list(payload, "data") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str) and path.startswith("/"):
                parent = path.rsplit("/", 1)[0]
                return parent or "/"
        return None

    @asynccontextmanager
    async def stream_session_events(
        self,
        session_id: str,
    ) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        # Refresh a stale delegated token before opening the long-lived
        # stream: a 401 mid-stream can't be retried cleanly, so probe and
        # refresh here where the connection hasn't started yet.
        if self._auth is not None and self._auth.access_token:
            used_token = self._auth.access_token
            probe = await self._request("GET", "/health")
            if probe.status_code == 401:
                await self._auth.refresh(used_token)
        try:
            async with self._client.stream(
                "GET",
                f"/v1/sessions/{session_id}/stream",
                params={"idle": "false"},
                headers=self._auth_headers(),
            ) as response:
                await _raise_for_status(response)
                self._logger.debug("Connected to Omnigent SSE stream session_id=%s", session_id)
                yield iter_sse_events(response.aiter_lines())
        except httpx.HTTPError as exc:
            raise ServerUnreachableError(
                f"Could not reach Omnigent server at {self._client.base_url}: {exc}"
            ) from exc

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._run_turn_once(session_id, text):
                yield event
            return
        except RunnerUnavailableError:
            if not workspace:
                raise
            self._logger.info(
                "Session has no available runner; "
                "launching a fresh runner and retrying session_id=%s",
                session_id,
            )
            await self.launch_runner(session_id, workspace=workspace, host_id=host_id)

        async for event in self._run_turn_once(session_id, text):
            yield event

    async def _run_turn_once(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        async with self.stream_session_events(session_id) as events:
            await self.submit_message(session_id, text)
            async for event in events:
                self._logger.debug(
                    "Received Omnigent event session_id=%s type=%s",
                    session_id,
                    event.get("type"),
                )
                yield event
                if is_terminal_event(event):
                    self._logger.info(
                        "Omnigent turn reached terminal event session_id=%s type=%s",
                        session_id,
                        event.get("type"),
                    )
                    break

    async def latest_assistant_text(self, session_id: str) -> str | None:
        self._logger.debug("Fetching latest Omnigent assistant item session_id=%s", session_id)
        response = await self._request(
            "GET",
            f"/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "desc"},
        )
        await _raise_for_status(response)
        payload = response.json()
        items = payload.get("data", [])
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                text = extract_assistant_text(item)
                if text:
                    return text
        return None


# Builds the per-user ``ClientAuth`` for a (server_url, user_id), or None
# when the user has no delegated token (unauthenticated — setup / login).
AuthResolver = Callable[[str, str], Awaitable["ClientAuth | None"]]


class OmnigentClientPool:
    """Caches one client per ``(server_url, slack_user_id)``.

    The bot targets one operator-fixed server, but each Slack user carries
    their own delegated token, so clients are keyed per user (the server_url
    is part of the key mainly so cached clients are dropped cleanly if the
    operator repoints the bot). An optional ``auth_resolver`` supplies each
    user's bearer token; when it is absent (or returns ``None``) the client
    is unauthenticated — used by the setup/login probes before a token
    exists.
    """

    def __init__(
        self,
        timeout: float = 30.0,
        auth_resolver: AuthResolver | None = None,
    ) -> None:
        self._timeout = timeout
        self._auth_resolver = auth_resolver
        self._clients: dict[tuple[str, str], OmnigentClient] = {}
        self._lock = asyncio.Lock()

    def set_auth_resolver(self, resolver: AuthResolver) -> None:
        """Wire the per-user auth resolver after construction.

        Lets the pool be created before the auth manager (which needs a
        reference back to the pool to invalidate cached clients on
        login/logout), then have its resolver attached.
        """
        self._auth_resolver = resolver

    async def get(self, server_url: str, user_id: str = "") -> OmnigentClient:
        key = (server_url.rstrip("/"), user_id)
        async with self._lock:
            client = self._clients.get(key)
            if client is not None:
                return client
        # Resolve auth outside the lock (it may hit the DB / refresh).
        auth: ClientAuth | None = None
        if user_id and self._auth_resolver is not None:
            auth = await self._auth_resolver(server_url.rstrip("/"), user_id)
        async with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = OmnigentClient(key[0], timeout=self._timeout, auth=auth)
                self._clients[key] = client
            return client

    async def invalidate(self, server_url: str, user_id: str) -> None:
        """Drop a cached client (e.g. after logout) and close it."""
        key = (server_url.rstrip("/"), user_id)
        async with self._lock:
            client = self._clients.pop(key, None)
        if client is not None:
            await client.aclose()

    async def invalidate_user(self, user_id: str) -> None:
        """Drop every cached client for a user.

        Backs a full logout, dropping any client holding the user's
        now-revoked token.
        """
        async with self._lock:
            keys = [k for k in self._clients if k[1] == user_id]
            clients = [self._clients.pop(k) for k in keys]
        for client in clients:
            await client.aclose()

    async def aclose_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            event = _decode_sse_event(event_name, data_lines)
            event_name = None
            data_lines = []
            if event is None:
                continue
            if event == "[DONE]":
                break
            if isinstance(event, str):
                continue
            yield event
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    event = _decode_sse_event(event_name, data_lines)
    if isinstance(event, dict):
        yield event


def is_terminal_event(event: dict[str, Any]) -> bool:
    # A turn ends at the SESSION level, not the response level. Orchestrator
    # agents emit a `response.completed`/`turn.completed` every time they end a
    # turn to wait on a background sub-agent, then resume with more responses in
    # the same turn — so treating those as terminal cuts the stream off at the
    # first sub-agent dispatch. `session.status` is the authoritative signal:
    # `running` -> `waiting` (parked on async work) -> `running` -> `idle`, and
    # only `idle`/`failed` mean the turn is truly over.
    event_type = str(event.get("type"))
    if event_type == "session.status":
        return str(event.get("status")) in {"idle", "failed"}
    # Explicit turn/response failure and cancellation still end the turn; keep
    # them as a fallback in case the session settles without an `idle` edge.
    return event_type in {
        "response.failed",
        "response.cancelled",
        "turn.failed",
        "turn.cancelled",
    }


def extract_delta(event: dict[str, Any]) -> str | None:
    if event.get("type") != "response.output_text.delta":
        return None
    delta = event.get("delta")
    return delta if isinstance(delta, str) else None


def extract_error_text(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type"))
    if event_type == "response.error":
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        message = event.get("message")
        if isinstance(message, str):
            return message
    if event_type in {"response.failed", "turn.failed"}:
        response = event.get("response")
        if isinstance(response, dict):
            last_error = response.get("error") or response.get("last_error")
            if isinstance(last_error, dict):
                message = last_error.get("message")
                if isinstance(message, str):
                    return message
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        if isinstance(error, str):
            return error
    return None


def extract_assistant_text(event_or_item: dict[str, Any]) -> str | None:
    if event_or_item.get("type") == "response.output_item.done":
        item = event_or_item.get("item")
        return extract_assistant_text(item) if isinstance(item, dict) else None

    item_type = event_or_item.get("type")
    if item_type != "message":
        return None

    data = event_or_item.get("data")
    message = data if isinstance(data, dict) else event_or_item
    if message.get("role") != "assistant":
        return None

    content = message.get("content")
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip() or None


def _decode_sse_event(event_name: str | None, data_lines: list[str]) -> dict[str, Any] | str | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return data
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise OmnigentError(f"Invalid SSE JSON payload: {data}") from exc
    if not isinstance(payload, dict):
        return None
    if event_name and "type" not in payload:
        payload["type"] = event_name
    return payload


def _extract_session_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "session_id", "conversation_id"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        for key in ("session", "data"):
            value = _extract_session_id(payload.get(key))
            if value:
                return value
    return None


def _extract_list(payload: Any, key: str) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, list) else None


def _runner_id(runner: dict[str, Any]) -> str | None:
    for key in ("id", "runner_id"):
        value = runner.get(key)
        if isinstance(value, str):
            return value
    return None


def _extract_runner_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = _runner_id(payload)
        if value:
            return value
        for key in ("runner", "data"):
            value = _extract_runner_id(payload.get(key))
            if value:
                return value
    return None


def _host_id(host: dict[str, Any]) -> str | None:
    for key in ("id", "host_id"):
        value = host.get(key)
        if isinstance(value, str):
            return value
    return None


def _is_host_online(host: dict[str, Any]) -> bool:
    if host.get("online") is True or host.get("host_online") is True:
        return True
    status = host.get("status")
    return isinstance(status, str) and status.lower() == "online"


async def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        error_code = _extract_error_code(response)
        if response.status_code == 503 and error_code == "runner_unavailable":
            raise RunnerUnavailableError(
                f"Omnigent runner unavailable for {response.request.url}: {response.text}"
            ) from exc
        if response.status_code == 401:
            raise AuthRequiredError(
                f"Omnigent server requires authentication for {response.request.url}"
            ) from exc
        raise OmnigentError(
            f"Omnigent request failed with {response.status_code}: {response.text}"
        ) from exc


def _extract_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None
