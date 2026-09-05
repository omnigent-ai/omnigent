"""Local HTTP shim that restores request fields the Claude CLI strips.

Claude Code CLIs from 2.1.168 (and likely earlier 2.1.16x) drop the
``thinking.display`` field from ``POST /v1/messages`` bodies whenever
experimental betas are disabled — the request builder gates ``display``
on the same internal flag as the experimental beta headers. The
Databricks AI gateway path must set
``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`` (the gateway's beta
allowlist rejects several of the CLI's experimental betas with HTTP
400 "invalid beta flag"), so gateway Opus requests lose ``display``
and fall back to the Opus 4.7+ model default of ``display="omitted"``:
the thinking block opens, every delta is empty, and no thoughts ever
stream. Sonnet's default keeps thinking visible, which is why only
Opus appeared broken. The Messages API itself accepts ``display``
with no beta header at all, so re-applying it at the HTTP boundary is
safe and changes nothing else about the request.

The shim binds an ephemeral port on ``127.0.0.1``; the executor points
``ANTHROPIC_BASE_URL`` at it. Qualifying ``/v1/messages`` bodies get
``display="summarized"`` re-injected; every other request — and every
response, including SSE streams — is forwarded verbatim and unbuffered.

One shim runs per
:class:`~omnigent.inner.claude_sdk_executor.ClaudeSDKExecutor`,
started lazily with the first gateway client and stopped by the
executor's ``close()`` (or with the harness subprocess, whichever
comes first).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from typing import Any, TypeAlias

import httpx
import uvicorn

logger = logging.getLogger(__name__)


class _NoSignalServer(uvicorn.Server):
    """
    uvicorn Server that never installs process signal handlers.

    ``uvicorn.Server.serve()`` replaces the process's SIGINT/SIGTERM
    handlers for its whole lifetime when run on the main thread. The
    harness subprocess already runs its own uvicorn server whose
    graceful shutdown is driven by SIGTERM (see
    ``omnigent/runtime/harnesses/_runner.py``); a second
    signal-capturing server would steal those handlers and break the
    harness's shutdown path. The shim is stopped explicitly via
    :meth:`ClaudeGatewayShim.aclose` (or dies with the process), so it
    needs no signal handling of its own.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """No-op replacement for uvicorn's signal-handler swap."""
        yield


# Databricks-hosted Claude model ids that default to
# ``thinking.display="omitted"`` — Opus 4.7+ (e.g.
# ``databricks-claude-opus-4-8``) and Fable, which shares Opus's
# adaptive-only thinking surface. Sonnet/Haiku stream visible thinking
# without a display field, so the shim leaves them untouched.
DATABRICKS_CLAUDE_ADAPTIVE_THINKING_PREFIXES: tuple[str, ...] = (
    "databricks-claude-opus-",
    "databricks-claude-fable-",
)

# Hop-by-hop headers (RFC 9110 §7.6.1) must not be forwarded by an
# intermediary; httpx/uvicorn manage their own connection framing.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Request headers never forwarded upstream: hop-by-hop framing plus
# ``host`` (belongs to the upstream) and ``content-length`` (recomputed
# by httpx after the body patch changes its size).
_REQUEST_HEADER_EXCLUDES = _HOP_BY_HOP_HEADERS | {"host", "content-length"}

# How long to wait for the in-process uvicorn server to bind before
# failing the turn. Binding a localhost ephemeral port is near-instant;
# 10s only guards against a pathologically wedged event loop.
_START_TIMEOUT_SECONDS = 10.0

# Upstream timeout: ``read=None`` because /v1/messages SSE streams can
# legitimately idle between deltas for minutes on long thinking turns;
# the CLI applies its own request-level timeout.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=60.0, pool=None)

# ASGI callable types — uvicorn's protocol is untyped dicts.
_Scope: TypeAlias = MutableMapping[str, Any]  # type: ignore[explicit-any]  # ASGI scope is a heterogeneous dict by spec
_Receive: TypeAlias = Callable[[], Awaitable[MutableMapping[str, Any]]]  # type: ignore[explicit-any]  # ASGI message dicts
_Send: TypeAlias = Callable[[MutableMapping[str, Any]], Awaitable[None]]  # type: ignore[explicit-any]  # ASGI message dicts


def restore_thinking_display(body: bytes) -> bytes:
    """
    Re-inject ``thinking.display="summarized"`` into a Messages API body.

    No-op (returns ``body`` unchanged) unless the body is a JSON object
    whose ``model`` is a Databricks Claude Opus/Fable id, whose ``thinking``
    is a dict with a non-``"disabled"`` type, and whose ``thinking``
    lacks a ``display`` key. A present ``display`` is always respected
    so a future CLI that forwards the field again makes this a no-op.

    :param body: Raw request body bytes, e.g.
        ``b'{"model": "databricks-claude-opus-4-8", "thinking":
        {"type": "adaptive"}, ...}'``.
    :returns: The body with ``display`` injected, or the original
        bytes when the request doesn't qualify.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(parsed, dict):
        return body
    model = parsed.get("model")
    thinking = parsed.get("thinking")
    if (
        not isinstance(model, str)
        or not model.startswith(DATABRICKS_CLAUDE_ADAPTIVE_THINKING_PREFIXES)
        or not isinstance(thinking, dict)
        or thinking.get("type") == "disabled"
        or "display" in thinking
    ):
        return body
    thinking["display"] = "summarized"
    logger.debug("Restored thinking.display=summarized for model %s", model)
    return json.dumps(parsed).encode("utf-8")


class ClaudeGatewayShim:
    """
    Reverse proxy between the Claude CLI and an Anthropic-compatible
    gateway that patches request bodies the CLI mis-builds.

    Start with :meth:`start`, then point ``ANTHROPIC_BASE_URL`` at
    :attr:`base_url`. All traffic is forwarded to
    ``upstream_base_url`` with headers preserved (minus hop-by-hop)
    and responses streamed back chunk-by-chunk, so SSE passes through
    unbuffered.

    :param upstream_base_url: The real gateway base URL the CLI would
        otherwise talk to, e.g.
        ``"https://example.databricks.com/ai-gateway/anthropic"``.
    """

    def __init__(self, upstream_base_url: str) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._port: int | None = None
        # Serializes start() so two concurrent first turns can't bind
        # two servers / leak a connection pool.
        self._start_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """
        The local URL the CLI should use as ``ANTHROPIC_BASE_URL``.

        :returns: Loopback base URL, e.g. ``"http://127.0.0.1:49152"``.
        :raises RuntimeError: If :meth:`start` has not completed.
        """
        if self._port is None:
            raise RuntimeError("ClaudeGatewayShim.start() has not completed")
        return f"http://127.0.0.1:{self._port}"

    async def start(self) -> None:
        """
        Bind the local server on an ephemeral loopback port.

        Idempotent — subsequent calls return immediately once the
        server is up.

        :raises OSError: If the server fails to bind within
            ``_START_TIMEOUT_SECONDS``.
        """
        async with self._start_lock:
            if self._port is not None:
                return
            await self._start_locked()

    async def _start_locked(self) -> None:
        """
        Bind the server; caller must hold ``_start_lock``.

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
        self._serve_task = asyncio.create_task(server.serve(), name="claude-gateway-shim-serve")
        deadline = asyncio.get_running_loop().time() + _START_TIMEOUT_SECONDS
        # uvicorn flips ``server.started`` from its serve task; there is
        # no event/callback hook to await, so poll at 10ms.
        while not server.started:
            if self._serve_task.done():
                # Surface bind/startup errors (e.g. port exhaustion)
                # instead of timing out silently.
                self._serve_task.result()
                raise OSError("Claude gateway shim server exited before startup")
            if asyncio.get_running_loop().time() > deadline:
                raise OSError(
                    f"Claude gateway shim failed to start within {_START_TIMEOUT_SECONDS}s"
                )
            await asyncio.sleep(0.01)
        self._port = server.servers[0].sockets[0].getsockname()[1]
        logger.info(
            "Claude gateway shim listening on %s → %s",
            self.base_url,
            self._upstream_base_url,
        )

    async def aclose(self) -> None:
        """
        Stop the local server and release the upstream connection pool.

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
        """
        Forward one request to the upstream gateway.

        :param scope: ASGI connection scope; only ``"http"`` is served.
        :param receive: ASGI receive callable for request body chunks.
        :param send: ASGI send callable for response messages.
        """
        if scope["type"] != "http":
            return
        if self._client is None:
            # Serving begins only after start() set the client; reaching
            # here means the shim's invariants were violated upstream.
            raise RuntimeError("ClaudeGatewayShim served a request before start()")

        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        method: str = scope["method"]
        path: str = scope["path"]
        request_body = bytes(body)
        if method == "POST" and path.endswith("/v1/messages"):
            request_body = restore_thinking_display(request_body)

        headers: list[tuple[str, str]] = []
        for raw_name, raw_value in scope["headers"]:
            name = raw_name.decode("latin-1")
            if name.lower() not in _REQUEST_HEADER_EXCLUDES:
                headers.append((name, raw_value.decode("latin-1")))
        query = scope.get("query_string", b"").decode("latin-1")
        url = f"{self._upstream_base_url}{path}" + (f"?{query}" if query else "")

        try:
            async with self._client.stream(
                method, url, headers=headers, content=request_body
            ) as upstream:
                response_headers = [
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in upstream.headers.items()
                    if k.lower() not in _HOP_BY_HOP_HEADERS
                ]
                await send(
                    {
                        "type": "http.response.start",
                        "status": upstream.status_code,
                        "headers": response_headers,
                    }
                )
                # aiter_raw() preserves the wire bytes (no transparent
                # decompression), so content-encoding/content-length
                # response headers stay truthful and SSE chunks flush
                # to the CLI as they arrive.
                async for chunk in upstream.aiter_raw():
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    )
                await send({"type": "http.response.body", "body": b""})
        except httpx.HTTPError as exc:
            logger.exception("Claude gateway shim upstream error: %s", exc)
            error_body = json.dumps(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"gateway shim upstream error: {exc}",
                    },
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": error_body})
