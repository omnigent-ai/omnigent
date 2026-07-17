from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx

# Pure event parsing, DTOs, and the base error live in ``events``; the client
# and pool here build on them. Re-exported below so existing
# ``from omnigent_slack.omnigent import extract_delta`` sites keep working.
from omnigent_slack.events import (
    ElicitationOption,
    ElicitationQuestion,
    ElicitationRequest,
    OmnigentError,
    OutputFile,
    _extract_list,
    _extract_runner_id,
    _extract_session_id,
    _host_id,
    _is_host_online,
    extract_assistant_text,
    extract_delta,
    extract_elicitation_request,
    extract_error_text,
    extract_output_file,
    extract_policy_denied,
    extract_todos,
    is_soft_idle_event,
    is_terminal_event,
    iter_sse_events,
)

__all__ = [
    "AuthRequiredError",
    "AuthResolver",
    "ClientAuth",
    "ElicitationOption",
    "ElicitationQuestion",
    "ElicitationRequest",
    "HostUnavailableError",
    "OmnigentClient",
    "OmnigentClientPool",
    "OmnigentError",
    "OutputFile",
    "RunnerUnavailableError",
    "ServerUnreachableError",
    "ValidatedServer",
    "extract_assistant_text",
    "extract_delta",
    "extract_elicitation_request",
    "extract_error_text",
    "extract_output_file",
    "extract_policy_denied",
    "extract_todos",
    "is_soft_idle_event",
    "is_terminal_event",
    "iter_sse_events",
]

# Sentinels for the idle-grace disambiguation. ``_NO_RESUMPTION``: the grace
# window elapsed and the snapshot confirms the turn is over. ``_RESUMED``: the
# stream produced another event (or ended), so the turn continues.
_NO_RESUMPTION = object()
_RESUMED = object()


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

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        accepted: bool,
        content: dict[str, Any] | None = None,
    ) -> None:
        """Deliver a verdict for a parked elicitation.

        ``accepted`` picks the MCP action (``accept``/``decline``). ``content``
        carries form answers for a form-mode elicitation (e.g. AskUserQuestion's
        ``{question: selected_label}`` map, which the server forwards to the
        agent as the tool result) — omitted for a binary approve/deny.

        Posts to the dedicated resolve endpoint (the id rides in the URL). The
        server returns 202 on delivery and 404/409 when the elicitation is
        already gone (cancel race / already resolved) — all benign, so only an
        unexpected status is surfaced.
        """
        self._logger.info(
            "Resolving Omnigent elicitation session_id=%s elicitation_id=%s accepted=%s "
            "has_content=%s",
            session_id,
            elicitation_id,
            accepted,
            content is not None,
        )
        body: dict[str, Any] = {"action": "accept" if accepted else "decline"}
        if content:
            body["content"] = content
        response = await self._request(
            "POST",
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json=body,
        )
        if response.status_code in (200, 202, 404, 409):
            return
        await _raise_for_status(response)

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
            raise HostUnavailableError(
                "No online Omnigent hosts are available to launch a runner."
            )
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
        idle_grace_seconds: float = 600.0,
        idle_poll_seconds: float = 5.0,
        idle_settle_seconds: float = 2.0,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._run_turn_once(
                session_id, text, idle_grace_seconds, idle_poll_seconds, idle_settle_seconds
            ):
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

        async for event in self._run_turn_once(
            session_id, text, idle_grace_seconds, idle_poll_seconds, idle_settle_seconds
        ):
            yield event

    async def _run_turn_once(
        self,
        session_id: str,
        text: str,
        idle_grace_seconds: float,
        idle_poll_seconds: float,
        idle_settle_seconds: float,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self.stream_session_events(session_id) as events:
            await self.submit_message(session_id, text)
            iterator = events.__aiter__()
            # A single in-flight "next event" task, reused across idle grace
            # windows. Timing it out must NOT cancel the underlying __anext__
            # (that would terminate the async generator), so we keep the task
            # alive with asyncio.wait and only await it again next window.
            pending: asyncio.Task[dict[str, Any]] | None = None
            awaiting_resumption = False
            try:
                while True:
                    if pending is None:
                        pending = asyncio.ensure_future(iterator.__anext__())

                    if awaiting_resumption:
                        # Previous event was a soft `idle`. Disambiguate park vs
                        # end without cancelling the in-flight read (that would
                        # kill the generator): time out the wait, and on a quiet
                        # window consult the rolled-up snapshot — while a
                        # sub-agent child is still running the parent reads
                        # `running`, so keep waiting (a slow child can outlast
                        # the grace window).
                        resumed = await self._await_within_grace(
                            pending,
                            session_id,
                            idle_grace_seconds,
                            idle_poll_seconds,
                            idle_settle_seconds,
                        )
                        if resumed is _NO_RESUMPTION:
                            pending.cancel()
                            self._logger.info(
                                "Omnigent turn settled idle with no resumption session_id=%s",
                                session_id,
                            )
                            break
                        awaiting_resumption = False

                    try:
                        event = await pending
                    except StopAsyncIteration:
                        break
                    pending = None

                    self._logger.debug(
                        "Received Omnigent event session_id=%s type=%s",
                        session_id,
                        event.get("type"),
                    )
                    yield event

                    if is_soft_idle_event(event):
                        awaiting_resumption = True
                        continue
                    if is_terminal_event(event):
                        self._logger.info(
                            "Omnigent turn reached terminal event session_id=%s type=%s",
                            session_id,
                            event.get("type"),
                        )
                        break
            finally:
                # Cancel and AWAIT the in-flight read so the underlying httpx
                # stream isn't still running when the context manager closes it
                # (aclose on a mid-flight async generator raises "already
                # running"). Swallow the cancellation/stop that surfaces here.
                if pending is not None:
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending

    async def _await_within_grace(
        self,
        pending: asyncio.Task[dict[str, Any]],
        session_id: str,
        grace_seconds: float,
        poll_seconds: float,
        settle_seconds: float,
    ) -> object:
        """After a soft ``idle``, wait for the stream to resume or confirm it ended.

        Waits on the in-flight read WITHOUT cancelling it (``asyncio.wait``
        leaves the task pending, so the async generator survives to be awaited
        again). Returns ``_RESUMED`` when the stream produced another event (or
        ended — the caller's ``await`` then surfaces ``StopAsyncIteration``), or
        ``_NO_RESUMPTION`` once the turn is genuinely over.

        Two timescales, because ``idle`` is doubly ambiguous:

        1. **Settle wait** (``settle_seconds``, short): a claude-native turn
           oscillates ``running``/``idle`` *while still streaming* its answer,
           with sub-second gaps between bursts. So EVERY idle first waits a short
           settle window for the next burst — ending here would truncate the
           reply mid-answer. Bounded and small so a genuinely-final idle adds
           only a brief tail.
        2. **Snapshot + poll** (``poll_seconds``, coarse): if still quiet after
           the settle, consult the rolled-up status. A fan-out orchestrator
           parked between wake cycles reads ``running`` (a sub-agent is working),
           so keep polling — a slow child can take many seconds. Only when the
           snapshot is no longer ``running`` is the turn over.

        ``grace_seconds`` caps the total wait so a stuck session can't park the
        turn forever.
        """
        deadline = asyncio.get_running_loop().time() + grace_seconds
        while True:
            # Settle: wait briefly for the next streaming burst. Handles the
            # mid-answer running/idle oscillation without truncating.
            done, _ = await asyncio.wait({pending}, timeout=settle_seconds)
            if done:
                return _RESUMED
            # Still quiet — is the session genuinely done, or a fan-out parent
            # waiting on a sub-agent (rolled-up status still `running`)?
            status = await self.get_session_status(session_id)
            if status != "running":
                return _NO_RESUMPTION
            if asyncio.get_running_loop().time() >= deadline:
                self._logger.info(
                    "Idle grace cap (%ss) elapsed while still running session_id=%s; "
                    "ending turn to avoid parking forever",
                    grace_seconds,
                    session_id,
                )
                return _NO_RESUMPTION
            # Fan-out parent still working — wait a coarser poll for resumption.
            done, _ = await asyncio.wait({pending}, timeout=poll_seconds)
            if done:
                return _RESUMED
            self._logger.debug(
                "Idle poll quiet but session still running (sub-agent "
                "outstanding) session_id=%s; continuing to wait",
                session_id,
            )

    async def get_session_status(self, session_id: str) -> str | None:
        """Fetch the session's rolled-up status from the snapshot.

        The snapshot's ``status`` rolls direct sub-agent child activity into the
        parent: a fan-out orchestrator parked between wake cycles reads
        ``running`` here (a child is still working) even though its own runner
        emitted ``idle`` on the stream. That makes this the authoritative "is the
        turn really over?" check when a stream ``idle`` is ambiguous. Best-effort
        — returns ``None`` on any failure so the caller falls back to the timer.
        """
        try:
            response = await self._request("GET", f"/v1/sessions/{session_id}")
            await _raise_for_status(response)
            status = response.json().get("status")
            return status if isinstance(status, str) else None
        except OmnigentError:
            return None

    async def _pending_elicitations(self, session_id: str) -> list[ElicitationRequest]:
        """Parse the session snapshot's outstanding elicitations.

        Best-effort — returns ``[]`` on any failure. ``None`` snapshot status
        (an auth/transport hiccup) is indistinguishable from "empty" here; both
        yield ``[]``, so callers must treat "no pending" conservatively.
        """
        try:
            response = await self._request("GET", f"/v1/sessions/{session_id}")
            await _raise_for_status(response)
        except OmnigentError:
            return []
        pending = response.json().get("pending_elicitations")
        if not isinstance(pending, list):
            return []
        out: list[ElicitationRequest] = []
        for entry in pending:
            if isinstance(entry, dict):
                request = extract_elicitation_request(entry, session_id)
                if request is not None:
                    out.append(request)
        return out

    async def get_pending_elicitation(self, session_id: str) -> ElicitationRequest | None:
        """Return the session's first outstanding elicitation, if any.

        Used to detect that a session is parked awaiting the user before a new
        message is submitted — the bot tells the user to answer the pending
        request first. Best-effort — ``None`` when nothing is pending or on error.
        """
        pending = await self._pending_elicitations(session_id)
        return pending[0] if pending else None

    async def is_elicitation_pending(self, session_id: str, elicitation_id: str) -> bool:
        """Whether ``elicitation_id`` is still outstanding on the server.

        Lets a Slack-side waiter detect that the elicitation was resolved
        *elsewhere* (the web UI, another client) and stop waiting. Best-effort:
        on a read failure returns ``True`` (assume still pending) so a transient
        hiccup doesn't spuriously abandon the wait.
        """
        try:
            response = await self._request("GET", f"/v1/sessions/{session_id}")
            await _raise_for_status(response)
        except OmnigentError:
            return True
        pending = response.json().get("pending_elicitations")
        if not isinstance(pending, list):
            return False
        return any(
            isinstance(e, dict) and e.get("elicitation_id") == elicitation_id for e in pending
        )

    async def latest_assistant_message(self, session_id: str) -> tuple[str, str] | None:
        """Return ``(item_id, text)`` of the newest assistant message, or None.

        The id lets a caller tell *this* turn's message from a prior turn's — a
        blind "latest text" fetch would otherwise resurrect the previous answer
        when the current turn produced none (e.g. a denied approval).
        """
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
            if not isinstance(item, dict):
                continue
            text = extract_assistant_text(item)
            if text:
                item_id = item.get("id")
                return (item_id if isinstance(item_id, str) else "", text)
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
