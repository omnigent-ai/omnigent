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
    SessionActivity,
    SessionInfo,
    _extract_list,
    _extract_runner_id,
    _extract_session_id,
    _is_host_online,
    extract_assistant_text,
    extract_delta,
    extract_elicitation_request,
    extract_elicitation_resolved,
    extract_error_text,
    extract_output_file,
    extract_policy_denied,
    extract_todos,
    host_id_of,
    is_hard_terminal_event,
    iter_sse_events,
    session_status,
)

__all__ = [
    "AuthRequiredError",
    "AuthResolver",
    "ClientAuth",
    "ElicitationOption",
    "ElicitationQuestion",
    "ElicitationRequest",
    "HarnessNotConfiguredError",
    "HostUnavailableError",
    "OmnigentClient",
    "OmnigentClientPool",
    "OmnigentError",
    "OutputFile",
    "RunnerUnavailableError",
    "ServerUnreachableError",
    "SessionActivity",
    "SessionInfo",
    "StreamInterruptedError",
    "ValidatedServer",
    "extract_assistant_text",
    "extract_delta",
    "extract_elicitation_request",
    "extract_elicitation_resolved",
    "extract_error_text",
    "extract_output_file",
    "extract_policy_denied",
    "extract_todos",
    "is_hard_terminal_event",
    "iter_sse_events",
    "session_status",
]

_logger = logging.getLogger(__name__)


class RunnerUnavailableError(OmnigentError):
    pass


class AuthRequiredError(OmnigentError):
    """The Omnigent server rejected an unauthenticated request (HTTP 401).

    The Slack bot has no way to authenticate yet, so callers surface this as a
    "not supported" message during setup rather than retrying.
    """


class ServerUnreachableError(OmnigentError):
    """The Omnigent server could not be reached at all (transport failure)."""


class TokenRefreshTransientError(OmnigentError):
    """A token refresh failed transiently (network blip / 5xx).

    The stored refresh token is still valid, so the current access token is kept
    and this attempt fails without re-prompting login — a later turn retries.
    Distinct from a dead grant, which drops the token and surfaces
    :class:`AuthRequiredError` so the user re-enrolls.
    """


class StreamInterruptedError(OmnigentError):
    """A live turn stream dropped mid-tail while the server stayed reachable.

    Distinct from ``ServerUnreachableError``: the ``GET .../stream`` response
    connected (``200 OK``) and then the connection was severed mid-body — the
    signature of a proxy max-duration cap on a long-lived chunked response
    (e.g. the ~5-minute cutoff on Databricks-App-hosted servers), not a server
    that is actually down. The turn keeps running server-side, so the client
    reconnects transparently rather than reporting the server unreachable.
    """


class HostUnavailableError(OmnigentError):
    """No online host could serve the session.

    Raised when the server reports no online hosts, the user's preferred host is
    offline/missing, or a launched runner never comes online — cases the user
    resolves by starting a host with ``omni host --server <url>``.
    """


class HarnessNotConfiguredError(OmnigentError):
    """The selected harness isn't configured on the host (HTTP 412).

    A precondition failure the user resolves by running ``omnigent setup`` on the
    host machine — a retry can't succeed without that. Carries the server's
    curated ``error.message`` (safe to show for this specific code).
    """


# A long-lived turn stream is severed by a proxy max-duration cap (e.g. the
# ~5-minute cutoff fronting Databricks-App-hosted servers) while the turn keeps
# running server-side. Reconnect transparently rather than reporting the server
# unreachable. Bounded so a genuinely dead server can't spin forever: once these
# are exhausted the drop surfaces as ``StreamInterruptedError``.
#
# The attempt counter bounds *consecutive* reconnects that make no new progress —
# a leg that forwards a genuinely new (non-replay) event resets it, so a long,
# healthy turn riding through many proxy caps is never abandoned. A separate hard
# cap on *total* reconnects backstops a pathological "replay one byte then drop"
# loop, which would otherwise reset the consecutive counter forever.
_STREAM_RECONNECT_MAX_ATTEMPTS = 6
_STREAM_RECONNECT_MAX_TOTAL = 200
_STREAM_RECONNECT_BACKOFF_S = 1.0

# Terminal ``response.*`` lifecycle events the in-process harness scaffold emits
# at a turn's end (completed/failed/cancelled/incomplete). Their presence marks
# a turn as having PRODUCED output — an id-less idle that follows one is a real
# end, not a claude-native cold-start PTY flap (claude-native emits no
# ``response.*`` lifecycle events at all). ``response.failed``/``cancelled`` are
# handled as hard-terminal separately; they are listed here too so the
# production signal is set even on the paths that don't reach that branch.
_RESPONSE_TERMINAL_EVENT_TYPES = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    }
)

# Path fragments that mark a redirect Location as an auth/login bounce (the
# Databricks Apps proxy 302s an unauthenticated request to its OAuth authorize
# endpoint). Used to tell an auth wall apart from a benign canonical redirect.
_AUTH_REDIRECT_MARKERS = ("/oidc/", "/oauth", "/authorize", "/login", "/.auth/")


def _is_auth_redirect(location: str) -> bool:
    """Whether a redirect ``Location`` points at an auth/login endpoint."""
    from urllib.parse import urlsplit

    if not location:
        return False
    # Match on the path (+query) only, so an unrelated host in the URL can't
    # smuggle a marker; the proxy's login path is what identifies the bounce.
    parts = urlsplit(location)
    hay = f"{parts.path}?{parts.query}".lower()
    return any(marker in hay for marker in _AUTH_REDIRECT_MARKERS)


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
            try:
                token = await self._refresh()
            except TokenRefreshTransientError:
                # Transient failure — the stored refresh token is still valid.
                # Keep the current access token (don't blank it) and re-raise so
                # the caller fails this attempt WITHOUT prompting re-login.
                raise
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
        # Bounded read timeout for ordinary requests so a stalled server can't
        # hang a call indefinitely and wedge the per-thread turn queue. The
        # long-lived SSE stream overrides this with ``read=None`` at its call
        # site (see ``stream_session_events``), since a live tail legitimately
        # blocks between events.
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout),
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

    @staticmethod
    def _is_auth_wall(response: httpx.Response) -> bool:
        """Whether a response means "the token was rejected, refresh it".

        A 401 is the direct signal. A Databricks-App proxy instead returns a 3xx
        redirect to its OAuth login for an expired/invalid token (the client has
        ``follow_redirects=False``, so it surfaces as a raw 3xx) — but only a
        redirect whose ``Location`` points at an auth/login endpoint counts, not a
        benign canonical-URL/trailing-slash 3xx. Narrowing this avoids
        double-submitting a non-idempotent request (and burning a single-use
        refresh token) on a legitimate redirect, while still catching the
        proxy login bounce that would otherwise strand the session at token expiry.
        """
        if response.status_code == 401:
            return True
        if not response.is_redirect:
            return False
        location = response.headers.get("location", "")
        return _is_auth_redirect(location)

    def _unreachable(self, exc: httpx.HTTPError) -> ServerUnreachableError:
        # A transport failure (DNS, refused connection, timeout) means the server
        # itself is unreachable — distinct from an HTTP error response, which
        # ``_raise_for_status`` classifies.
        return ServerUnreachableError(
            f"Could not reach Omnigent server at {self._client.base_url}: {exc}"
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        used_token = self._auth.access_token if self._auth is not None else None
        # Pop caller headers once — a second pop would return None and silently
        # drop them on the 401 retry below.
        custom_headers = kwargs.pop("headers", None) or {}
        headers = {**self._auth_headers(), **custom_headers}
        try:
            response = await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        # A delegated token expires within the hour; on an auth wall (401, or a
        # Databricks proxy 3xx→login) refresh once and retry so long-lived threads
        # keep working without re-login.
        if self._auth is not None and self._is_auth_wall(response):
            try:
                new_token = await self._auth.refresh(used_token)
            except TokenRefreshTransientError as exc:
                # The refresh endpoint blipped, but the grant is still good — the
                # token wasn't dropped. Fail this attempt as "unreachable" (the
                # retryable class), NOT as a 401 → no spurious re-login prompt.
                raise ServerUnreachableError(
                    f"Token refresh failed transiently for {self._client.base_url}: {exc}"
                ) from exc
            if new_token:
                retry_headers = {**self._auth_headers(), **custom_headers}
                try:
                    response = await self._client.request(
                        method, url, headers=retry_headers, **kwargs
                    )
                except httpx.HTTPError as exc:
                    raise self._unreachable(exc) from exc
        return response

    async def _get_list(self, url: str, *keys: str) -> list[dict[str, Any]]:
        """GET ``url`` and return its list payload as dicts.

        Tries each of ``keys`` in order (the server wraps the list under a
        top-level key that varies by endpoint), falling back to a bare list body.
        Shared by the agent/host listing endpoints so the wrap-key fallback logic
        lives in one place.
        """
        response = await self._request("GET", url)
        await _raise_for_status(response)
        payload = response.json()
        data = next(
            (lst for key in keys if (lst := _extract_list(payload, key)) is not None), None
        )
        if data is None:
            data = payload if isinstance(payload, list) else []
        return [item for item in data if isinstance(item, dict)]

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
        # Don't log the title — it embeds the user's message text; log only the
        # agent id (everywhere else we log lengths, not content).
        self._logger.info("Creating Omnigent session agent_id=%s", agent_id)
        response = await self._request(
            "POST",
            "/v1/sessions",
            json={"agent_id": agent_id, "title": title},
        )
        await _raise_for_status(response)
        payload = response.json()
        session_id = _extract_session_id(payload)
        if session_id is None:
            # Log the raw body for operators, but keep it out of the exception —
            # it surfaces to the Slack thread, and a server body can carry
            # internal detail (matches the discipline in _raise_for_status).
            self._logger.warning("Create session response had no id: %r", payload)
            raise OmnigentError("Omnigent server returned no session id.")
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
            self._logger.warning(
                "Omnigent host unavailable host=%s status=%s body=%r",
                target_host,
                response.status_code,
                response.text,
            )
            raise HostUnavailableError(f"Omnigent host {target_host} is not available.")
        await _raise_for_status(response)
        payload = response.json()
        runner_id = _extract_runner_id(payload)
        if runner_id is None:
            # Log the raw body for operators; keep it out of the thread-facing
            # exception (see create_session / _raise_for_status).
            self._logger.warning("Launch runner response had no id: %r", payload)
            raise OmnigentError("Omnigent server returned no runner id.")

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
        agents = await self._get_list("/v1/agents", "data", "agents")
        self._logger.info("Found built-in Omnigent agents count=%s", len(agents))
        return agents

    async def list_hosts(self) -> list[dict[str, Any]]:
        self._logger.debug("Listing Omnigent hosts")
        hosts = await self._get_list("/v1/hosts", "hosts", "data")
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
            if _is_host_online(host) and (host_id := host_id_of(host)) is not None
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
            if self._is_auth_wall(probe):
                # A transient refresh failure keeps the (still-valid) token; open
                # the stream with it rather than aborting the turn on a blip.
                with contextlib.suppress(TokenRefreshTransientError):
                    await self._auth.refresh(used_token)
        # A transport error BEFORE the stream connects means the server is
        # unreachable; one AFTER the ``200 OK`` (thrown back in when the caller's
        # tail iteration fails) is a mid-stream drop — a proxy severing a
        # long-lived chunked response, not a down server. The caller reconnects
        # on the latter, so the two are classified distinctly.
        connected = False
        try:
            async with self._client.stream(
                "GET",
                f"/v1/sessions/{session_id}/stream",
                params={"idle": "false"},
                headers=self._auth_headers(),
                # A live tail blocks between events — disable the read timeout
                # for the stream only (ordinary requests keep the bounded one).
                timeout=httpx.Timeout(self._timeout, read=None),
            ) as response:
                await _raise_for_status(response)
                connected = True
                self._logger.debug("Connected to Omnigent SSE stream session_id=%s", session_id)
                yield iter_sse_events(response.aiter_lines())
        except httpx.HTTPError as exc:
            if connected:
                raise StreamInterruptedError(
                    f"Omnigent stream to {self._client.base_url} dropped mid-turn: {exc}"
                ) from exc
            raise self._unreachable(exc) from exc

    async def run_turn(
        self,
        session_id: str,
        text: str,
        *,
        workspace: str | None = None,
        host_id: str | None = None,
        idle_grace_seconds: float = 600.0,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._run_turn_once(session_id, text, idle_grace_seconds):
                yield event
            return
        except RunnerUnavailableError:
            # No runner bound to the session — launch one and retry the turn once.
            if not workspace:
                raise
            self._logger.info(
                "Session has no available runner; "
                "launching a fresh runner and retrying session_id=%s",
                session_id,
            )
            await self.launch_runner(session_id, workspace=workspace, host_id=host_id)

        async for event in self._run_turn_once(session_id, text, idle_grace_seconds):
            yield event

    async def _run_turn_once(
        self,
        session_id: str,
        text: str,
        idle_grace_seconds: float,
    ) -> AsyncIterator[dict[str, Any]]:
        # Turn-end detection is SERVER-AUTHORITATIVE and HARNESS-AGNOSTIC,
        # mirroring the web UI's reducer. The discriminator is "is a response
        # currently OPEN?", NOT the harness name — because `session.status`
        # carries a `response_id` only for terminal-backed harnesses
        # (claude-native/codex) and is id-LESS for the in-process runtime
        # (debby/claude-sdk); the schema documents this as intentional.
        #
        # A response is OPEN once we see an id-bearing `running`/`waiting`
        # (claude-native's Stop-hook edge). The turn ENDS on `idle`/`failed` when:
        #   (a) it is id-bearing and matches the open response, OR
        #   (b) it is id-LESS and NO id-bearing response is open — this covers the
        #       in-process harness, whose running/waiting are all id-less so
        #       nothing is ever "open", and whose id-less `idle` is the real end.
        # An id-less `idle` while an id-bearing response IS open is a claude-native
        # PTY-activity flap (mid-answer generation lull) — IGNORED, else the reply
        # truncates at the first pause. `waiting` NEVER ends the turn (both
        # harnesses use it for "parked on sub-agents / async work").
        #
        # The stream never sends `[DONE]` and never closes; heartbeats fire every
        # ~15s. So the ONLY non-event case is a dead SOCKET (half-open) — treat a
        # read that produces nothing for `idle_grace_seconds` as dead and end.
        # Turn-end state persists ACROSS reconnects: a proxy max-duration cap can
        # sever the stream mid-turn (``StreamInterruptedError``) while the turn
        # keeps running server-side, so we re-open the stream and continue rather
        # than surfacing a false "server unreachable". On re-open the server
        # replays the in-flight assistant text (one cumulative delta per
        # message_id) before resuming the live tail; ``_emitted`` tracks what we
        # have already forwarded per bucket so ``_reconcile_delta`` yields only
        # the unseen suffix and the reply never double-renders.
        open_response_id: str | None = None
        saw_open_running = False
        # Whether THIS turn has actually started producing on the stream. A
        # freshly-resumed session's stream (``idle=false``) replays the session's
        # CURRENT status first — which, hours after the last turn, is a stale
        # ``idle`` (no response_id) that arrives before our just-submitted
        # message's ``running`` edge. Without this guard the ``id_bearing_match``
        # branch below (``not saw_open_running``) would treat that pre-turn idle as
        # the end and return 0 events. We only honor such an idle once we've seen
        # the turn begin: a running/waiting edge, a forwarded answer delta, or a
        # hard-terminal event.
        turn_started = False
        # Whether THIS turn has PRODUCED anything on the stream — an answer delta or
        # a terminal ``response.*`` lifecycle event. Stricter than ``turn_started``,
        # which a bare id-less ``running`` edge also sets. The id-less end branch
        # keys off this, not ``turn_started``: claude-native's PTY-activity watcher
        # emits an id-less ``running`` then an id-less ``idle`` flap during cold
        # start (runner booted, message submitted, but the LLM hasn't returned its
        # first token). That pair looks identical on the wire to the in-process
        # harness's real id-less end — EXCEPT the in-process harness always produces
        # first (a delta, or a terminal ``response.*`` envelope from the scaffold;
        # even a policy-deny notice emits a bare ``output_text.delta``). Gating on
        # production lets the flap fall through (kept reading until the real
        # id-bearing Stop idle, or the idle-grace backstop) while a genuinely-empty
        # in-process turn still ends promptly on its id-less idle.
        turn_produced = False
        emitted: dict[str | None, str] = {}
        attempt = 0
        total_reconnects = 0
        submitted = False
        while True:
            # After a reconnect the first delta per message_id is a cumulative
            # replay (de-dup it); on the first connection nothing is replayed, so
            # this stays inert and the happy path is unchanged.
            resyncing = attempt > 0
            resynced_buckets: set[str | None] = set()
            # Whether THIS connection leg forwarded a genuinely NEW (non-replay)
            # event. Only real progress resets the consecutive-reconnect budget —
            # a leg that merely replays already-seen text then drops does NOT
            # count, so a "replay one byte then drop" loop still hits the cap.
            progressed_this_leg = False
            try:
                async with self.stream_session_events(session_id) as events:
                    if not submitted:
                        # Submit ONCE: the server keeps running the turn across a
                        # reconnect, so re-submitting would start a second turn.
                        await self.submit_message(session_id, text)
                        submitted = True
                    iterator = events.__aiter__()
                    # A single in-flight "next event" task. A liveness timeout must
                    # NOT cancel it (that would terminate the async generator); we
                    # keep it alive with asyncio.wait and await it again next window.
                    pending: asyncio.Task[dict[str, Any]] | None = None
                    try:
                        while True:
                            if pending is None:
                                pending = asyncio.ensure_future(iterator.__anext__())

                            done, _ = await asyncio.wait({pending}, timeout=idle_grace_seconds)
                            if not done:
                                # No event for the whole liveness window — with 15s
                                # heartbeats on a live connection, this means the
                                # socket is dead (half-open). End rather than hang.
                                pending.cancel()
                                self._logger.info(
                                    "Omnigent stream silent for %ss (no heartbeat) — "
                                    "ending turn session_id=%s",
                                    idle_grace_seconds,
                                    session_id,
                                )
                                return

                            try:
                                event = await pending
                            except StopAsyncIteration:
                                return
                            pending = None

                            self._logger.debug(
                                "Received Omnigent event session_id=%s type=%s",
                                session_id,
                                event.get("type"),
                            )
                            reconciled = self._reconcile_delta(
                                event, emitted, resyncing, resynced_buckets
                            )
                            if reconciled is not None:
                                # A forwarded, non-replay event is real progress —
                                # this leg advanced the turn, so it resets the
                                # consecutive-reconnect budget below.
                                progressed_this_leg = True
                                # Actual answer TEXT means the turn is producing, so
                                # a later idle is a real end. Do NOT count a passed-
                                # through ``session.status`` here — a stale pre-turn
                                # idle also flows through reconcile, and counting it
                                # would defeat the stale-idle guard below.
                                if event.get("type") == "response.output_text.delta":
                                    turn_started = True
                                    turn_produced = True
                                yield reconciled

                            # A terminal ``response.*`` lifecycle envelope also counts
                            # as production: the in-process harness always emits one
                            # (the scaffold's completed/failed/cancelled) before its
                            # id-less idle, so an id-less idle that follows one is a
                            # real end, not a claude-native cold-start flap (which
                            # emits no ``response.*`` at all).
                            if event.get("type") in _RESPONSE_TERMINAL_EVENT_TYPES:
                                turn_produced = True

                            if is_hard_terminal_event(event):
                                self._logger.info(
                                    "Omnigent turn reached hard-terminal event "
                                    "session_id=%s type=%s",
                                    session_id,
                                    event.get("type"),
                                )
                                return

                            parsed = session_status(event)
                            if parsed is None:
                                continue
                            status, response_id = parsed
                            if status in ("running", "waiting"):
                                # The turn is now producing — any subsequent idle is
                                # a real end, not a stale pre-turn one replayed on
                                # resume.
                                turn_started = True
                            if status in ("running", "waiting") and response_id is not None:
                                # An id-bearing open edge (claude-native Stop hook).
                                # Mark a response OPEN so a later matching terminal
                                # ends the turn and a bare id-less idle is treated as
                                # a mid-answer flap.
                                open_response_id = response_id
                                saw_open_running = True
                            elif status in ("idle", "failed") and not turn_started:
                                # A terminal BEFORE this turn started producing is
                                # stale — the session's pre-existing status replayed
                                # on connect (common when resuming an idle session
                                # hours later). Ignore it and keep reading; the
                                # idle-grace timeout is the backstop if nothing ever
                                # comes. Ending here would return 0 events and post
                                # "completed without returning response text".
                                self._logger.info(
                                    "Ignoring stale pre-turn %s (no prior activity) "
                                    "session_id=%s response_id=%s",
                                    status,
                                    session_id,
                                    response_id,
                                )
                            elif status in ("idle", "failed"):
                                # Terminal edge (turn has started). End when:
                                #  (a) id-bearing and matches the open response (or we
                                #      saw no id-bearing open — some paths only stamp
                                #      the end);
                                #  (b) id-less AND no id-bearing response is open — the
                                #      in-process (debby/claude-sdk) real end. `waiting`
                                #      would have kept us going; only `idle`/`failed`.
                                # An id-less idle WHILE an id-bearing response is open
                                # is a claude-native PTY flap → ignored (falls through).
                                # An id-less IDLE with NOTHING produced is a
                                # claude-native cold-start flap (id-less PTY
                                # running→idle before the first token) → ignored; the
                                # real end is the later id-bearing Stop idle, with the
                                # idle-grace timeout as the backstop. This
                                # produced-gate applies to `idle` ONLY: `failed` is
                                # never a PTY flap (it comes solely from the
                                # authoritative StopFailure hook / a setup-phase
                                # failure — the PTY watcher emits only `idle`), so a
                                # bare id-less `failed` must end the turn promptly even
                                # with nothing produced.
                                id_bearing_match = response_id is not None and (
                                    not saw_open_running or response_id == open_response_id
                                )
                                produced_or_failed = turn_produced or status == "failed"
                                id_less_end = (
                                    response_id is None
                                    and not saw_open_running
                                    and produced_or_failed
                                )
                                if id_bearing_match or id_less_end:
                                    self._logger.info(
                                        "Omnigent turn ended session_id=%s status=%s "
                                        "response_id=%s",
                                        session_id,
                                        status,
                                        response_id,
                                    )
                                    return
                    finally:
                        # Cancel and AWAIT the in-flight read so the underlying httpx
                        # stream isn't still running when the context manager closes
                        # it (aclose on a mid-flight async generator raises "already
                        # running"). Swallow the cancellation/stop that surfaces here.
                        if pending is not None:
                            pending.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await pending
                # The stream ended without a terminal event or a drop (the server
                # closed it cleanly) — the turn is over from this client's view.
                return
            except StreamInterruptedError as exc:
                total_reconnects += 1
                # A leg that forwarded a NEW event before dropping is progress, not
                # a failing reconnect — reset the consecutive budget so the cap
                # counts only CONSECUTIVE no-progress reconnects (a long healthy
                # turn is never abandoned). The total cap backstops a pathological
                # replay-then-drop loop that would reset the consecutive counter
                # forever.
                if progressed_this_leg:
                    attempt = 0
                attempt += 1
                if (
                    attempt >= _STREAM_RECONNECT_MAX_ATTEMPTS
                    or total_reconnects >= _STREAM_RECONNECT_MAX_TOTAL
                ):
                    # Give up reconnecting — surface as a stream interruption (a
                    # non-alarming "lost the live connection", not "server down").
                    self._logger.info(
                        "Omnigent stream dropped and reconnect exhausted "
                        "(%s attempts) session_id=%s",
                        attempt,
                        session_id,
                    )
                    raise
                # The turn may have finished during the drop. If the server reports
                # it no longer running, stop cleanly — the caller's end-of-turn
                # reconcile recovers the committed final text. Unknown status means
                # reconnect (a truly-down server re-fails as ServerUnreachableError).
                activity = await self.get_session_activity(session_id)
                if activity.status in ("idle", "failed"):
                    self._logger.info(
                        "Omnigent stream dropped; server reports turn ended "
                        "status=%s session_id=%s",
                        activity.status,
                        session_id,
                    )
                    return
                self._logger.info(
                    "Omnigent stream dropped mid-turn; reconnecting "
                    "(attempt %s) session_id=%s: %s",
                    attempt,
                    session_id,
                    exc,
                )
                await asyncio.sleep(_STREAM_RECONNECT_BACKOFF_S * attempt)

    def _reconcile_delta(
        self,
        event: dict[str, Any],
        emitted: dict[str | None, str],
        resyncing: bool,
        resynced_buckets: set[str | None],
    ) -> dict[str, Any] | None:
        """De-dup replayed in-flight text so a reconnect never double-renders.

        Only ``response.output_text.delta`` events carry accumulating text; every
        other event passes through untouched. Deltas are tracked per
        ``message_id`` bucket (``None`` for the single-bucket non-native shape).

        On a live connection each delta is an incremental chunk — appended to the
        bucket and forwarded verbatim. On the FIRST delta of a bucket after a
        reconnect (``resyncing``), the server replays the whole streamed-so-far
        text as one cumulative delta; we forward only the suffix past what we
        already emitted (empty when nothing new streamed during the drop) and
        drop the rest, so the reply resumes exactly where it left off. A cumulative
        replay that is NOT a superset of what we showed (the server rescoped the
        message) resets the bucket to the replayed value.

        :returns: The event to yield, or ``None`` to swallow a fully-seen replay.
        """
        if event.get("type") != "response.output_text.delta":
            return event
        delta = event.get("delta")
        if not isinstance(delta, str):
            return event
        message_id = event.get("message_id")
        bucket = message_id if isinstance(message_id, str) else None
        seen = emitted.get(bucket, "")

        if resyncing and bucket not in resynced_buckets:
            # First post-reconnect delta for this bucket: it carries the cumulative
            # streamed-so-far text, not an increment.
            resynced_buckets.add(bucket)
            if delta.startswith(seen):
                suffix = delta[len(seen) :]
            else:
                # Server rescoped this message — re-render it from scratch.
                suffix = delta
            emitted[bucket] = delta
            if not suffix:
                return None
            return {**event, "delta": suffix}

        emitted[bucket] = seen + delta
        return event

    async def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        """Best-effort GET returning the JSON body as a dict, else ``None``.

        Shared by the read-only status/elicitation/items probes, all of which
        must degrade gracefully (a transient failure must never abort or wedge a
        turn). Swallows transport/HTTP errors AND a non-JSON body — callers get
        ``None`` and apply their own conservative default.
        """
        try:
            response = await self._request("GET", url, **kwargs)
            await _raise_for_status(response)
            payload = response.json()
        except (OmnigentError, ValueError):
            # ValueError covers json.JSONDecodeError (non-JSON 200 body).
            return None
        return payload if isinstance(payload, dict) else None

    async def get_session_activity(self, session_id: str) -> SessionActivity:
        """Snapshot of whether the SERVER considers this session busy.

        Mirrors the web UI's send-gating (``computeIsWorking`` +
        pending-elicitation): a session is busy when its rolled-up ``status`` is
        ``running``/``waiting``, and needs user action when it has a pending
        elicitation. Both are SERVER-derived — the authoritative "can I submit a
        new prompt now?" signal — unlike any local connection bookkeeping. One
        GET. Best-effort: an unreadable snapshot returns ``unknown`` so the caller
        can decide conservatively (we treat unknown as "go ahead", since the
        server itself safely buffers a message that races a turn).
        """
        snapshot = await self._get_json(f"/v1/sessions/{session_id}")
        if snapshot is None:
            return SessionActivity(status=None, pending_elicitation=False)
        status = snapshot.get("status")
        return SessionActivity(
            status=status if isinstance(status, str) else None,
            pending_elicitation=bool(self._parse_pending(snapshot)),
        )

    async def get_session_info(self, session_id: str) -> SessionInfo:
        """Read the session's harness + agent name from the snapshot.

        For the first-message config summary. Best-effort: fields default to
        ``None`` if the snapshot is unreadable or omits them.
        """
        snapshot = await self._get_json(f"/v1/sessions/{session_id}")
        if snapshot is None:
            return SessionInfo(harness=None, agent_name=None)
        harness = snapshot.get("harness")
        agent_name = snapshot.get("agent_name")
        return SessionInfo(
            harness=harness if isinstance(harness, str) and harness else None,
            agent_name=agent_name if isinstance(agent_name, str) and agent_name else None,
        )

    @staticmethod
    def _parse_pending(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
        pending = snapshot.get("pending_elicitations") if snapshot else None
        return [e for e in pending if isinstance(e, dict)] if isinstance(pending, list) else []

    async def latest_assistant_message(self, session_id: str) -> tuple[str | None, str] | None:
        """Return ``(item_id, text)`` of the newest assistant message, or None.

        The id lets a caller tell *this* turn's message from a prior turn's — a
        blind "latest text" fetch would otherwise resurrect the previous answer
        when the current turn produced none (e.g. a denied approval). ``item_id``
        is ``None`` when the message carries no id, so a caller can't mistake two
        id-less messages for the same one. Best-effort: the outer ``None`` on any
        read failure (the caller must not be left mid-turn if the snapshot fetch
        fails).
        """
        self._logger.debug("Fetching latest Omnigent assistant item session_id=%s", session_id)
        payload = await self._get_json(
            f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "desc"}
        )
        items = payload.get("data") if payload else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            text = extract_assistant_text(item)
            if text:
                item_id = item.get("id")
                return (item_id if isinstance(item_id, str) and item_id else None, text)
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
        # A streaming response (the SSE tail) hasn't had its body read, so the
        # ``.text``/``.json()`` inspection below would raise ``ResponseNotRead``
        # and mask the real status. Pull the (small) error body in first; the
        # classification then works the same as for an ordinary request — so a
        # 401 on the stream still becomes AuthRequiredError, not a raw httpx error.
        if not response.is_closed:
            with contextlib.suppress(Exception):
                await response.aread()
        error_code, error_message = _extract_error(response)
        # The raw server body can carry internal paths/stack traces; log it for
        # operators but keep it out of the exception message, which surfaces to
        # the Slack channel (visible to everyone in the thread). Guard the body
        # access: if the stream couldn't be read, classify on status alone.
        body = "<unread>"
        with contextlib.suppress(Exception):
            body = response.text
        # Log whether a bearer was actually attached (not its value). On an auth
        # wall (302→login / 401) this is the fact that separates "the bot didn't
        # send a token" from "the server rejected the token it was sent".
        had_bearer = "authorization" in response.request.headers
        _logger.warning(
            "Omnigent request failed status=%s url=%s had_bearer=%s body=%r",
            response.status_code,
            response.request.url,
            had_bearer,
            body,
        )
        if response.status_code == 503 and error_code == "runner_unavailable":
            raise RunnerUnavailableError("Omnigent runner is unavailable.") from exc
        # A 3xx redirect means an auth proxy in front of the server is bouncing
        # an unauthenticated request to its login page — the omnigent API itself
        # never redirects its own endpoints. This is how a Databricks-App-hosted
        # server signals "no credentials" (its proxy 302s to /oidc/... rather
        # than returning 401), so treat it the same as 401 → the setup flow then
        # starts the per-user login/enrollment instead of reporting "unreachable".
        if response.is_redirect:
            raise AuthRequiredError(
                f"Omnigent server requires authentication for {response.request.url}"
            ) from exc
        if response.status_code == 401:
            raise AuthRequiredError(
                f"Omnigent server requires authentication for {response.request.url}"
            ) from exc
        if response.status_code == 412 and error_code == "harness_not_configured":
            # A precondition failure the user CAN act on (the harness isn't set up
            # on the host — run `omnigent setup` there). The server's structured
            # error.message is curated actionable guidance for this code, so it's
            # safe to surface (unlike a raw body); fall back to a generic hint.
            raise HarnessNotConfiguredError(
                error_message or "The selected harness isn't configured on the host."
            ) from exc
        raise OmnigentError(
            f"Omnigent request failed with status {response.status_code}."
        ) from exc


def _extract_error(response: httpx.Response) -> tuple[str | None, str | None]:
    """Return ``(code, message)`` from a server error body, or ``(None, None)``.

    The server wraps failures as ``{"error": {"code": ..., "message": ...}}``.
    The message is only surfaced to users for specific, curated codes (see
    ``_raise_for_status``) — never blindly, since a raw body can leak internals.
    """
    try:
        payload = response.json()
    except (json.JSONDecodeError, httpx.StreamError):
        # StreamError (e.g. ResponseNotRead) when a streaming body couldn't be
        # read — classify on status alone rather than masking it.
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) and message else None,
    )
