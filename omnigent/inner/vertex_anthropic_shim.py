"""Local reverse proxy that lets the Claude CLI reach Claude on Vertex AI.

Google exposes Anthropic's models on Vertex AI through its Model Garden
partner integration, which uses a different wire shape than the plain
Anthropic API the Claude CLI's ``ANTHROPIC_BASE_URL`` normally targets:

- Path: ``.../publishers/anthropic/models/<model>:rawPredict`` (or
  ``:streamRawPredict`` for streaming) instead of ``/v1/messages``. The
  model id is part of the URL, not the request body, and uses Vertex's
  ``@``-dated form (``claude-haiku-4-5@20251001``) rather than Anthropic's
  own dash-dated form (``claude-haiku-4-5-20251001``).
- Body: requires an injected ``anthropic_version`` field (Vertex has no
  equivalent of the plain API's ``anthropic-version`` header).
- Auth: a GCP OAuth2 access token (``Authorization: Bearer ...``), not an
  Anthropic API key — and that token must come from Application Default
  Credentials resolved in this (unsandboxed) process, never from inside
  the sandboxed Claude CLI subprocess the executor spawns.

This shim sits between the CLI and Vertex, translating one Anthropic
Messages API request (whatever the CLI would have sent to
``api.anthropic.com/v1/messages``) into the equivalent Vertex rawPredict
call, and forwarding the response back unchanged — Vertex's response body
and SSE stream shape already match Anthropic's native Messages API, so no
response-side translation is needed.

Request/response shapes and the exact rawPredict path were confirmed
against a real Vertex AI project with Claude enabled, using the Claude
CLI's own (``CLAUDE_CODE_USE_VERTEX=1``) debug log as ground truth for the
path format, and live calls to Vertex to validate the request/response
shape end to end (including tool use and streaming).

See ``ClaudeGatewayShim`` (``claude_gateway_shim.py``) for the sibling
shim used for the Databricks AI Gateway path — that one proxies to an
already-Anthropic-shaped upstream and only patches specific body fields,
so it doesn't need to know about Vertex's different path/body/auth shape
at all. This module is intentionally separate rather than a mode flag on
that class: the two proxy fundamentally different upstream contracts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from typing import TYPE_CHECKING, Any, TypeAlias

import httpx
import uvicorn

if TYPE_CHECKING:
    import google.auth.credentials

logger = logging.getLogger(__name__)

# The Vertex-specific `anthropic_version` value, distinct from the plain
# Anthropic API's `anthropic-version` header values (e.g. "2023-06-01").
# See https://docs.anthropic.com/en/api/claude-on-vertex-ai
ANTHROPIC_VERTEX_VERSION = "vertex-2023-10-16"

# GCP OAuth2 scope required for Vertex AI calls.
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Anthropic's native model id uses a dash before the trailing date
# (claude-haiku-4-5-20251001); Vertex's publisher-model path uses "@"
# (claude-haiku-4-5@20251001).
_DASH_DATE_RE = re.compile(r"^(?P<family>.+)-(?P<date>\d{8})$")
_AT_DATE_RE = re.compile(r"^(?P<family>.+)@(?P<date>\d{8})$")

# Refresh the cached access token this many seconds before it actually
# expires, so a request never races a not-yet-refreshed, about-to-expire
# token.
_TOKEN_REFRESH_MARGIN_SECONDS = 120

# How long to wait for the in-process uvicorn server to bind before
# failing the turn. Mirrors ClaudeGatewayShim's identical constant.
_START_TIMEOUT_SECONDS = 10.0

# Upstream timeout: `read=None` because streamed responses can legitimately
# idle between deltas for minutes on long thinking turns; the CLI applies
# its own request-level timeout.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=60.0, pool=None)

# ASGI callable types — uvicorn's protocol is untyped dicts.
_Scope: TypeAlias = MutableMapping[str, Any]  # type: ignore[explicit-any]  # ASGI scope is a heterogeneous dict by spec
_Receive: TypeAlias = Callable[[], Awaitable[MutableMapping[str, Any]]]  # type: ignore[explicit-any]  # ASGI message dicts
_Send: TypeAlias = Callable[[MutableMapping[str, Any]], Awaitable[None]]  # type: ignore[explicit-any]  # ASGI message dicts


class _NoSignalServer(uvicorn.Server):
    """uvicorn Server that never installs process signal handlers.

    Identical rationale to ``ClaudeGatewayShim``'s copy of this class: a
    second signal-capturing server on the main thread would steal
    SIGINT/SIGTERM from the harness subprocess's own uvicorn server,
    breaking its graceful shutdown path.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """No-op replacement for uvicorn's signal-handler swap."""
        yield


def to_vertex_model_id(model: str) -> str:
    """Convert an Anthropic-style dash-dated model id to Vertex's @-dated form.

    Idempotent — a model already in ``@``-form, or with no trailing
    8-digit date at all, is returned unchanged.

    :param model: Model id, e.g. ``"claude-haiku-4-5-20251001"`` or
        ``"claude-haiku-4-5@20251001"``.
    :returns: The Vertex publisher-model id, e.g.
        ``"claude-haiku-4-5@20251001"``.
    """
    if _AT_DATE_RE.match(model):
        return model
    m = _DASH_DATE_RE.match(model)
    if m:
        return f"{m.group('family')}@{m.group('date')}"
    return model


def build_vertex_anthropic_url(
    *,
    project: str,
    location: str,
    model: str,
    stream: bool,
) -> str:
    """Build the Vertex AI rawPredict URL for a Claude publisher model.

    :param project: GCP project id.
    :param location: Vertex location, e.g. ``"global"`` or ``"us-east5"``.
    :param model: Model id in either dash- or ``@``-dated form; translated
        to the ``@``-dated Vertex form internally.
    :param stream: Use ``:streamRawPredict`` instead of ``:rawPredict``.
    :returns: Full HTTPS URL for the Vertex publisher-model endpoint.
    """
    vertex_model = to_vertex_model_id(model)
    method = "streamRawPredict" if stream else "rawPredict"
    host = (
        "aiplatform.googleapis.com"
        if location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/anthropic/models/{vertex_model}:{method}"
    )


def prepare_vertex_anthropic_body(body: bytes) -> bytes:
    """Translate an Anthropic Messages API request body for Vertex.

    Drops ``model`` (it's part of the URL path on Vertex, not the body)
    and injects ``anthropic_version`` (Vertex expects it in the body; the
    plain Anthropic API takes it as an ``anthropic-version`` header
    instead). Also drops ``context_management`` — confirmed against a
    real Vertex project that ``:rawPredict`` rejects it outright with
    ``400 context_management: Extra inputs are not permitted``, unlike
    ``api.anthropic.com`` which accepts it; Vertex's schema for this
    endpoint appears to be a stricter subset of the plain Messages API
    rather than a true pass-through, so the CLI's auto-compaction context
    editing feature is unavailable on this path. Everything else
    (``messages``, ``system``, ``max_tokens``, ``tools``, ``tool_choice``,
    ``stream``, etc.) is Anthropic Messages API-shaped and passed through
    unchanged.

    Non-object JSON and invalid JSON are returned unchanged rather than
    raising — Vertex owns request validation for malformed bodies, same
    as ``ClaudeGatewayShim.restore_thinking_display``'s pass-through
    behavior for the same class of input.

    :param body: Raw request body bytes a client sent to ``/v1/messages``.
    :returns: The body reshaped for Vertex's ``:rawPredict`` /
        ``:streamRawPredict``, or the original bytes if it doesn't parse
        as a JSON object.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(parsed, dict):
        return body
    parsed.pop("model", None)
    parsed.pop("context_management", None)
    parsed["anthropic_version"] = ANTHROPIC_VERTEX_VERSION
    return json.dumps(parsed).encode("utf-8")


class GoogleADCTokenCache:
    """Caches a short-lived Google Cloud access token, refreshing when stale.

    Generic Google OAuth2 / Application Default Credentials token
    exchange — nothing here is specific to Vertex AI or Claude. Resolves
    ADC once per process (reading
    ``~/.config/gcloud/application_default_credentials.json``, or a
    service account key via ``GOOGLE_APPLICATION_CREDENTIALS``) and
    refreshes the access token as needed.

    Designed to run only in the unsandboxed parent process — the whole
    point of :class:`VertexAnthropicGatewayShim` is that the sandboxed
    Claude CLI subprocess never needs to see the credential this class
    holds.
    """

    def __init__(self) -> None:
        self._credentials: google.auth.credentials.Credentials | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a valid access token, refreshing it first if it's stale.

        :returns: A bearer token suitable for
            ``Authorization: Bearer <token>``.
        """
        async with self._lock:
            await asyncio.to_thread(self._ensure_fresh_sync)
            assert (
                self._credentials is not None
            )  # narrows for the type checker; set by _ensure_fresh_sync
            return str(self._credentials.token)

    def _ensure_fresh_sync(self) -> None:
        """Resolve ADC (once) and refresh the token if it's stale.

        Blocking Google Cloud SDK calls — always run via
        ``asyncio.to_thread`` from :meth:`get_token`, never awaited
        directly.
        """
        import google.auth
        import google.auth.transport.requests

        if self._credentials is None:
            self._credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])

        needs_refresh = not self._credentials.valid or self._expires_within_margin()
        if needs_refresh:
            request = google.auth.transport.requests.Request()
            self._credentials.refresh(request)

    def _expires_within_margin(self) -> bool:
        """Check whether the cached credential is within its refresh margin.

        ``google.auth.credentials.Credentials.expiry`` is documented as a
        naive ``datetime`` that's implicitly UTC — comparing it against
        ``datetime.now(timezone.utc)`` directly (rather than converting
        either side through ``.timestamp()``, which treats a naive
        datetime as *local* time) avoids misjudging the margin on a
        non-UTC host.

        :returns: ``True`` if there's no expiry at all (never observed on
            a real ADC token, but treated as "needs refresh" defensively)
            or the expiry is closer than
            ``_TOKEN_REFRESH_MARGIN_SECONDS`` away.
        """
        import datetime

        expiry = getattr(self._credentials, "expiry", None)
        if expiry is None:
            return True
        now_utc_naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        margin = datetime.timedelta(seconds=_TOKEN_REFRESH_MARGIN_SECONDS)
        return (expiry - now_utc_naive) < margin


# Sent by the CLI as ``ANTHROPIC_API_KEY`` / ``x-api-key`` on the client
# side of the loopback hop. The shim never reads or validates it — real
# auth is the GCP access token it attaches to the *upstream* Vertex
# request (see module docstring) — so this only needs to be a non-empty
# string that satisfies the CLI's own "are we logged in" check.
PLACEHOLDER_API_KEY = "vertex-shim-placeholder-not-a-real-key"


class VertexAnthropicGatewayShim:
    """Reverse proxy between the Claude CLI and Claude on Vertex AI.

    Start with :meth:`start`, then point ``ANTHROPIC_BASE_URL`` at
    :attr:`base_url`. Every ``POST .../v1/messages`` request is
    translated into the equivalent Vertex ``:rawPredict`` /
    ``:streamRawPredict`` call (see module docstring) and the response —
    including SSE streams — is forwarded back to the CLI unbuffered and
    unmodified, since Vertex's response shape already matches Anthropic's
    native Messages API.

    One shim runs per
    :class:`~omnigent.inner.claude_sdk_executor.ClaudeSDKExecutor` using
    the Vertex gateway path, started lazily with the first Vertex client
    and stopped by the executor's ``close()`` (or with the harness
    subprocess, whichever comes first) — the same lifecycle
    ``ClaudeGatewayShim`` uses.

    :param project: GCP project id with Claude enabled on Vertex AI.
    :param location: Vertex location, e.g. ``"global"`` or ``"us-east5"``.
    :param token_cache: Optional pre-built token cache; a fresh
        :class:`GoogleADCTokenCache` is created if omitted. Exposed for
        tests that need to inject a fake.
    """

    def __init__(
        self,
        *,
        project: str,
        location: str = "global",
        token_cache: GoogleADCTokenCache | None = None,
    ) -> None:
        self._project = project
        self._location = location
        self._token_cache = token_cache if token_cache is not None else GoogleADCTokenCache()
        self._client: httpx.AsyncClient | None = None
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._port: int | None = None
        # Serializes start() so two concurrent first turns can't bind two
        # servers / leak a connection pool — same rationale as
        # ClaudeGatewayShim._start_lock.
        self._start_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """The local URL the CLI should use as ``ANTHROPIC_BASE_URL``.

        :returns: Loopback base URL, e.g. ``"http://127.0.0.1:49152"``.
        :raises RuntimeError: If :meth:`start` has not completed.
        """
        if self._port is None:
            raise RuntimeError("VertexAnthropicGatewayShim.start() has not completed")
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        """Bind the local server on an ephemeral loopback port.

        Idempotent — subsequent calls return immediately once the server
        is up.

        :raises OSError: If the server fails to bind within
            ``_START_TIMEOUT_SECONDS``.
        """
        async with self._start_lock:
            if self._port is not None:
                return
            await self._start_locked()

    async def _start_locked(self) -> None:
        """Bind the server; caller must hold ``_start_lock``.

        :raises OSError: If the server fails to bind within
            ``_START_TIMEOUT_SECONDS``.
        """
        if self._client is not None:
            # A prior failed start left a client behind; don't leak it.
            await self._client.aclose()
        self._client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
        config = uvicorn.Config(
            self._asgi_app,
            host="127.0.0.1",
            port=0,  # ephemeral — the bound port is read back below
            log_level="warning",
            lifespan="off",  # plain ASGI callable; no lifespan protocol
            interface="asgi3",  # bound methods defeat uvicorn's auto-detection
        )
        server = _NoSignalServer(config)
        self._server = server
        self._serve_task = asyncio.create_task(
            server.serve(), name="vertex-anthropic-gateway-shim-serve"
        )
        deadline = asyncio.get_running_loop().time() + _START_TIMEOUT_SECONDS
        # uvicorn flips `server.started` from its serve task; there is no
        # event/callback hook to await, so poll at 10ms.
        while not server.started:
            if self._serve_task.done():
                self._serve_task.result()
                raise OSError("Vertex Anthropic gateway shim server exited before startup")
            if asyncio.get_running_loop().time() > deadline:
                raise OSError(
                    f"Vertex Anthropic gateway shim failed to start within "
                    f"{_START_TIMEOUT_SECONDS}s"
                )
            await asyncio.sleep(0.01)
        self._port = server.servers[0].sockets[0].getsockname()[1]
        logger.info(
            "Vertex Anthropic gateway shim listening on %s (project=%s, location=%s)",
            self.base_url,
            self._project,
            self._location,
        )

    async def aclose(self) -> None:
        """Stop the local server and release the upstream connection pool.

        Safe to call multiple times or before :meth:`start`.
        """
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
            self._serve_task = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._server = None
        self._port = None

    async def _asgi_app(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        """Translate and forward one request to Vertex AI.

        Only ``POST .../v1/messages`` is meaningful for this upstream —
        Vertex has no equivalent of the plain Anthropic API's other
        endpoints (``/v1/models`` etc.), so anything else gets a 404
        rather than being forwarded nowhere.

        :param scope: ASGI connection scope; only ``"http"`` is served.
        :param receive: ASGI receive callable for request body chunks.
        :param send: ASGI send callable for response messages.
        """
        if scope["type"] != "http":
            return
        if self._client is None:
            raise RuntimeError("VertexAnthropicGatewayShim served a request before start()")

        method: str = scope["method"]
        path: str = scope["path"]
        if method != "POST" or not path.endswith("/v1/messages"):
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "not_found_error",
                                "message": (
                                    "vertex-anthropic-gateway-shim only serves "
                                    "POST .../v1/messages"
                                ),
                            },
                        }
                    ).encode("utf-8"),
                }
            )
            return

        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        request_body = bytes(body)

        try:
            parsed = json.loads(request_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = None
        model = parsed.get("model") if isinstance(parsed, dict) else None
        if not isinstance(model, str) or not model:
            await self._send_error(send, status=400, message="request body must have a 'model'")
            return
        stream = bool(parsed.get("stream", False)) if isinstance(parsed, dict) else False

        url = build_vertex_anthropic_url(
            project=self._project,
            location=self._location,
            model=model,
            stream=stream,
        )
        vertex_body = prepare_vertex_anthropic_body(request_body)

        try:
            token = await self._token_cache.get_token()
        except Exception as exc:  # noqa: BLE001 — surface any ADC failure as a clean 502, not a crashed turn
            logger.warning("Vertex Anthropic gateway shim failed to resolve a GCP token: %s", exc)
            await self._send_error(send, status=502, message=f"GCP auth error: {exc}")
            return

        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}

        try:
            async with self._client.stream(
                "POST", url, headers=headers, content=vertex_body
            ) as upstream:
                response_headers = [
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in upstream.headers.items()
                    if k.lower() not in {"content-length", "connection", "transfer-encoding"}
                ]
                await send(
                    {
                        "type": "http.response.start",
                        "status": upstream.status_code,
                        "headers": response_headers,
                    }
                )
                # aiter_raw() preserves the wire bytes (no transparent
                # decompression), so SSE chunks flush to the CLI as they
                # arrive rather than being buffered whole.
                async for chunk in upstream.aiter_raw():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})
        except httpx.HTTPError as exc:
            logger.warning("Vertex Anthropic gateway shim upstream error: %s", exc)
            await self._send_error(send, status=502, message=f"gateway shim upstream error: {exc}")

    @staticmethod
    async def _send_error(send: _Send, *, status: int, message: str) -> None:
        """Send a single-shot Anthropic-shaped error response.

        :param send: ASGI send callable.
        :param status: HTTP status code to send.
        :param message: Human-readable error message.
        """
        body = json.dumps({"type": "error", "error": {"type": "api_error", "message": message}})
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body.encode("utf-8")})
