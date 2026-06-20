"""Runner-side adapter: receive ``request`` frames, dispatch via ASGI,
frame responses back (Phase 4).

Per ``designs/RUNNER.md`` §3 "Sketch of the adapters", the runner
accepts incoming ``request`` frames and calls the runner's FastAPI
app via ASGI directly — no TCP listener needed. Responses are framed
back as ``response.head`` + N × ``response.body`` + ``response.end``.

This module ships the runner-side WebSocket client loop and the ASGI
dispatcher it invokes for each incoming ``request`` frame.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from websockets.exceptions import InvalidURI, WebSocketException

from omnigent.runner.identity import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    RUNNER_TUNNEL_TOKEN_HEADER,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    PingFrame,
    PongFrame,
    RequestCancelFrame,
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    WSCloseFrame,
    WSFrame,
    WSOpenFrame,
    decode_body,
    decode_frame,
    encode_body,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES

_logger = logging.getLogger(__name__)

# ASGI app type — async callable with the standard 3-arg shape.
_ASGIApp = Callable[
    [
        dict[str, Any],
        Callable[[], Awaitable[dict[str, Any]]],
        Callable[[dict[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

# Reconnect backoff: 0.5 s initial, 10 s cap, ±50% jitter. The
# jitter spreads simultaneous reconnects from many runners across
# each backoff window so a server restart doesn't see a synchronised
# WS-accept spike.
#
# The cap is tuned against the parent CLI's runner-startup budget
# (``omnigent.chat._wait_for_remote_runner``, currently 60 s).
# An older 30 s cap meant a single bad attempt could eat half the
# budget before the next reconnect even tried, so transient
# disconnects during startup looked like total failure to the
# polling parent. 10 s keeps each retry visible inside the budget
# while still backing off enough not to hammer a slow server.
_INITIAL_RECONNECT_DELAY_S = 0.5
_MAX_RECONNECT_DELAY_S = 10.0
_RECONNECT_JITTER_FRACTION = 0.5
_FATAL_SERVER_CLOSE_CODES = {4001, 4002, 4004, 4500}
_REFRESHABLE_HTTP_STATUSES = {401}
_FATAL_SERVER_HTTP_STATUSES = {403}
# Routine server-initiated recycles, NOT errors: 1012 "service restart" and
# 1001 "going away" (and a 502 upgrade rejection) are how the Databricks Apps
# ingress cycles long-lived WebSockets out from under a healthy app. The
# server *wants* a prompt reconnect, so we reset the backoff to its minimum
# instead of escalating toward the cap — escalating would leave the runner
# unregistered (and messages undeliverable) for seconds on every recycle,
# which is the dominant on-app reliability failure.
_TUNNEL_RECYCLE_CLOSE_CODES = {1001, 1012}
_TUNNEL_RECYCLE_HTTP_STATUSES = {502}
_RUNNER_TUNNEL_CLOSE_TIMEOUT_S = 0.25
RUNNER_TUNNEL_REJECTION_PREFIX = "runner tunnel rejected by server "

# Schemes that, when surfaced through ``InvalidURI.uri``, indicate
# the WebSocket upgrade request was redirected somewhere the
# websockets library cannot follow. The common case on Databricks
# Apps is an unauthenticated request being redirected to the OAuth
# login page (``https://<workspace>/oidc/oauth2/v2.0/authorize?…``).
# We detect that case and exit fatally instead of looping forever
# against an endpoint we can never upgrade.
_AUTH_REDIRECT_SCHEMES = {"http", "https"}


async def dispatch_via_asgi(
    app: _ASGIApp,
    frame: RequestFrame,
    send_text: Callable[[str], Awaitable[None]],
) -> None:
    """Run a tunneled ``request`` frame through the runner's ASGI app
    and stream the response back as frames via ``send_text``.

    :param app: The runner's FastAPI app (which is an ASGI callable).
    :param frame: The incoming ``request`` frame the server sent.
    :param send_text: Async callback that writes a frame onto the
        WebSocket back to the server (typically ``ws.send_text``).
    """
    body_bytes = decode_body(frame.body, frame.encoding) if frame.body is not None else b""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": frame.method,
        "scheme": "http",
        "path": frame.path,
        "raw_path": frame.path.encode("utf-8"),
        "query_string": frame.query_string.encode("utf-8"),
        "headers": [(k.lower().encode("utf-8"), v.encode("utf-8")) for k, v in frame.headers],
        "server": ("runner", 0),
        "client": ("tunnel", 0),
        "root_path": "",
    }

    body_sent = False
    response_status: list[int] = []
    response_headers_raw: list[tuple[bytes, bytes]] = []
    head_sent_to_ws: bool = False

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }
        # After the full request body is delivered, do not synthesize
        # ``http.disconnect``. Starlette's StreamingResponse listens
        # for disconnects concurrently with body iteration; returning
        # disconnect here makes it cancel the stream before the runner
        # can proxy harness SSE chunks. A real tunnel disconnect or
        # request.cancel frame cancels the dispatch task, which also
        # cancels this receive wait.
        disconnect: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        return await disconnect

    async def send(event: dict[str, Any]) -> None:
        nonlocal head_sent_to_ws
        ev_type = event.get("type")
        if ev_type == "http.response.start":
            response_status.append(event["status"])
            response_headers_raw[:] = list(event.get("headers", []))
            await send_text(
                encode_frame(
                    ResponseHeadFrame(
                        id=frame.id,
                        status=event["status"],
                        headers=[
                            [k.decode("latin-1"), v.decode("latin-1")]
                            for k, v in event.get("headers", [])
                        ],
                    )
                )
            )
            head_sent_to_ws = True
        elif ev_type == "http.response.body":
            chunk = event.get("body", b"")
            if chunk:
                # Pick body encoding based on the response's
                # content-type header — utf-8 for text-shaped, base64
                # otherwise (binary file downloads).
                content_type = "application/octet-stream"
                for k, v in response_headers_raw:
                    if k.lower() == b"content-type":
                        content_type = v.decode("latin-1", errors="replace")
                        break
                body_str, encoding = encode_body(chunk, content_type)
                await send_text(
                    encode_frame(
                        ResponseBodyFrame(
                            id=frame.id,
                            body=body_str,
                            encoding=encoding,
                        )
                    )
                )
            if not event.get("more_body", False):
                await send_text(encode_frame(ResponseEndFrame(id=frame.id)))

    try:
        await app(scope, receive, send)
    except Exception:
        # If the app crashed BEFORE sending head, surface a 500 so
        # the server's request-side awaiter doesn't hang. If it
        # crashed AFTER head, it's already streaming — best we can
        # do is end the response so the consumer doesn't wait
        # forever.
        if not head_sent_to_ws:
            await send_text(
                encode_frame(
                    ResponseHeadFrame(
                        id=frame.id,
                        status=500,
                        headers=[["content-type", "application/json"]],
                    )
                )
            )
            await send_text(
                encode_frame(
                    ResponseBodyFrame(
                        id=frame.id,
                        body='{"error": "runner_dispatch_failed"}',
                        encoding="utf-8",
                    )
                )
            )
        await send_text(encode_frame(ResponseEndFrame(id=frame.id)))
        raise


async def serve_tunnel(
    app: _ASGIApp,
    *,
    server_url: str,
    runner_id: str,
    runner_version: str,
    auth_token: str | None = None,
    tunnel_token: str | None = None,
    auth_token_factory: Callable[[], str | None] | None = None,
    on_reconnect: Callable[[], Awaitable[None]] | None = None,
    on_activity: Callable[[], None] | None = None,
) -> None:
    """Keep a runner WebSocket tunnel connected to a server.

    The runner is the WebSocket client: it connects to the server's
    ``/v1/runners/{runner_id}/tunnel`` endpoint, sends a hello frame,
    then dispatches incoming request frames through the runner ASGI
    app. Disconnects retry forever with capped backoff so starting
    the runner before the local server is available is valid.

    When *auth_token_factory* is provided, a fresh bearer token is
    obtained before each reconnect attempt so that expired OAuth
    tokens are transparently refreshed. On HTTP 401, the factory is
    called once more before giving up — this handles the edge case
    where the proactively refreshed token was already near expiry.

    :param app: Runner ASGI application.
    :param server_url: HTTP(S) server base URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param runner_id: Stable runner id, e.g.
        ``"runner_0123456789abcdef"``.
    :param runner_version: Runner version string, e.g. ``"0.1.0"``.
    :param auth_token: Optional bearer token for authenticated
        remote tunnel endpoints. Used as the initial token and as
        a fallback when *auth_token_factory* fails.
    :param tunnel_token: Optional secret token that binds this
        WebSocket tunnel to *runner_id*.
    :param auth_token_factory: Optional sync callable that returns
        a fresh bearer token string (or ``None``). Called via
        ``asyncio.to_thread`` before each reconnect. Typical
        implementation: ``lambda: _read_databrickscfg(profile).token``.
    :param on_reconnect: Optional async callback fired after a
        successful reconnect (not on the initial connect). Used
        by the runner to do a catch-up scan for missed messages
        (Step 8.5 Scenario B).
    :param on_activity: Optional sync callback fired for real
        server-to-runner work frames. Tunnel pings are excluded so
        keepalives do not keep an otherwise idle runner alive.
    :returns: Never returns during normal operation.
    """
    delay_s = _INITIAL_RECONNECT_DELAY_S
    tunnel_url = _tunnel_url(server_url, runner_id)
    _connected_before = False
    while True:
        auth_token = await _refresh_auth_token(auth_token, auth_token_factory)
        if _connected_before and on_reconnect is not None:
            try:
                await on_reconnect()
            except Exception:
                _logger.exception("on_reconnect callback failed")
        retry_reason = "connection closed cleanly"
        recycle = False
        try:
            _connected_before = True
            activity_kwargs = {"on_activity": on_activity} if on_activity is not None else {}
            await _serve_tunnel_once(
                app,
                tunnel_url=tunnel_url,
                runner_id=runner_id,
                runner_version=runner_version,
                auth_token=auth_token,
                tunnel_token=tunnel_token,
                **activity_kwargs,
            )
            delay_s = _INITIAL_RECONNECT_DELAY_S
        except asyncio.CancelledError:
            raise
        except WebSocketException as exc:
            redirect_url = _websocket_auth_redirect_url(exc)
            if redirect_url is not None:
                # The websockets library auto-followed a redirect
                # away from our ws:// endpoint to an http(s):// URL
                # — typically a Databricks App login page when the
                # caller is unauthenticated. Retrying cannot help:
                # every reconnect will land back on the same
                # redirect, so we fail loud with the actual URL so
                # the user sees what the server is asking for
                # ("go log in here").
                raise RuntimeError(
                    f"{RUNNER_TUNNEL_REJECTION_PREFIX}"
                    f"(redirect to non-WebSocket URL {redirect_url}); "
                    "the server likely requires auth — "
                    "run `omnigent setup` to configure credentials"
                ) from exc
            http_status = _websocket_http_status(exc)
            if http_status in _REFRESHABLE_HTTP_STATUSES:
                auth_token = await _handle_refreshable_auth_failure(
                    auth_token_factory, http_status, exc
                )
                delay_s = _INITIAL_RECONNECT_DELAY_S
                continue
            if http_status in _FATAL_SERVER_HTTP_STATUSES:
                raise RuntimeError(
                    f"{RUNNER_TUNNEL_REJECTION_PREFIX}"
                    f"(HTTP {http_status}); check remote server authentication"
                ) from exc
            close_code = _websocket_close_code(exc)
            if close_code in _FATAL_SERVER_CLOSE_CODES:
                raise RuntimeError(
                    f"{RUNNER_TUNNEL_REJECTION_PREFIX}"
                    f"(close code {close_code}); check frame protocol compatibility"
                ) from exc
            if (
                close_code in _TUNNEL_RECYCLE_CLOSE_CODES
                or http_status in _TUNNEL_RECYCLE_HTTP_STATUSES
            ):
                # Routine ingress recycle — reconnect promptly, don't escalate
                # the backoff (which would leave the runner unregistered for
                # seconds each recycle and drop in-flight message delivery).
                delay_s = _INITIAL_RECONNECT_DELAY_S
                recycle = True
                detail = f"close {close_code}" if close_code else f"HTTP {http_status or 0}"
                retry_reason = f"server recycled the tunnel ({detail}); reconnecting promptly"
            else:
                retry_reason = str(exc)
        except (ConnectionError, OSError, ValueError) as exc:
            retry_reason = str(exc)
        jittered = delay_s * (
            1.0 + random.uniform(-_RECONNECT_JITTER_FRACTION, _RECONNECT_JITTER_FRACTION)
        )
        _logger.info(
            "runner tunnel disconnected: %s; retrying in %.2fs (jittered from %.2fs)",
            retry_reason,
            jittered,
            delay_s,
        )
        await asyncio.sleep(jittered)
        # Match the host tunnel (connect.py): escalate the backoff only on
        # non-recycle failures. A routine ingress recycle keeps reconnecting
        # promptly at the base delay instead of doubling toward the cap.
        if not recycle:
            delay_s = min(delay_s * 2, _MAX_RECONNECT_DELAY_S)


async def _refresh_auth_token(
    current_token: str | None,
    factory: Callable[[], str | None] | None,
) -> str | None:
    """
    Call *factory* to obtain a fresh auth token if available.

    Falls back to *current_token* if the factory is ``None`` or
    raises an exception (transient IdP outage should not kill a
    reconnect that might still succeed with the old token).

    :param current_token: The last known bearer token (may be
        expired), e.g. ``"eyJhbGci..."``.
    :param factory: Sync callable returning a fresh token, or
        ``None`` when no refresh mechanism is configured.
    :returns: A fresh token from the factory, or *current_token*
        as fallback.
    """
    if factory is None:
        return current_token
    try:
        fresh = await asyncio.to_thread(factory)
        if fresh is not None:
            return fresh
    except (ValueError, OSError, ImportError):
        _logger.warning(
            "auth token refresh failed; falling back to previous token",
            exc_info=True,
        )
    return current_token


async def _handle_refreshable_auth_failure(
    factory: Callable[[], str | None] | None,
    http_status: int,
    exc: WebSocketException,
) -> str | None:
    """
    Attempt a token refresh after an HTTP 401 rejection.

    If the factory produces a new token, returns it so the caller
    can retry immediately. If no factory is available or the refresh
    fails, raises a fatal ``RuntimeError``.

    :param factory: Sync callable returning a fresh token.
    :param http_status: The HTTP status that triggered this call,
        e.g. ``401``.
    :param exc: The original ``WebSocketException``.
    :returns: A refreshed token string.
    :raises RuntimeError: When no factory is available or refresh
        fails.
    """
    if factory is not None:
        try:
            fresh = await asyncio.to_thread(factory)
            if fresh is not None:
                _logger.info(
                    "auth token refreshed after HTTP %d; retrying",
                    http_status,
                )
                return fresh
        except (ValueError, OSError, ImportError):
            _logger.warning(
                "auth token refresh failed after HTTP %d",
                http_status,
                exc_info=True,
            )
    raise RuntimeError(
        f"{RUNNER_TUNNEL_REJECTION_PREFIX}(HTTP {http_status}); check remote server authentication"
    ) from exc


def _websocket_http_status(exc: BaseException) -> int | None:
    """Extract an HTTP response status from a WebSocket handshake error.

    :param exc: Exception raised while opening the WebSocket.
    :returns: HTTP status code, e.g. ``401``, or ``None`` when the
        exception is not an HTTP upgrade rejection.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _websocket_auth_redirect_url(exc: BaseException) -> str | None:
    """Return the redirect target when the WebSocket upgrade bounced
    to an HTTP(S) URL the library cannot follow.

    The ``websockets`` library raises
    :class:`websockets.exceptions.InvalidURI` when it follows a
    redirect to a URL whose scheme is not ``ws`` / ``wss``. The most
    common cause in this project is an unauthenticated request to a
    Databricks App: the platform redirects to an OAuth login page
    (``https://<workspace>/oidc/oauth2/v2.0/authorize?…``) and the
    library bails out. The originating URL is preserved on the
    exception's ``uri`` attribute.

    Other ``InvalidURI`` cases (a literal ``ws://`` with a bad host,
    say) are intentionally NOT classified as auth redirects — we
    only fire on the redirect-into-http(s) pattern because it is
    the one users keep hitting and the one that benefits from a
    targeted hint instead of a generic retry.

    :param exc: Exception raised while opening the WebSocket.
    :returns: The HTTP(S) URL we were redirected to, e.g.
        ``"https://example.databricks.com/oidc/oauth2/v2.0/authorize?…"``,
        or ``None`` when *exc* is not an auth-style redirect.
    """
    if not isinstance(exc, InvalidURI):
        return None
    uri = getattr(exc, "uri", None)
    if not isinstance(uri, str):
        return None
    scheme = urlsplit(uri).scheme.lower()
    if scheme in _AUTH_REDIRECT_SCHEMES:
        return uri
    return None


async def _serve_tunnel_once(
    app: _ASGIApp,
    *,
    tunnel_url: str,
    runner_id: str,
    runner_version: str,
    auth_token: str | None = None,
    tunnel_token: str | None = None,
    on_activity: Callable[[], None] | None = None,
) -> None:
    """Serve one WebSocket connection until it closes.

    :param app: Runner ASGI application.
    :param tunnel_url: WebSocket URL to connect to, e.g.
        ``"ws://127.0.0.1:6767/v1/runners/runner_abc/tunnel"``.
    :param runner_id: Stable runner id, e.g. ``"runner_abc"``.
    :param runner_version: Runner version string for the hello
        frame, e.g. ``"0.1.0"``.
    :param auth_token: Optional bearer token for the WebSocket
        handshake.
    :param tunnel_token: Optional secret token that binds this
        WebSocket tunnel to *runner_id*.
    :param on_activity: Optional sync callback fired for real work
        frames received from the server. Ping keepalives do not
        trigger it.
    :returns: None.
    """
    import websockets

    dispatch_tasks: dict[str, asyncio.Task[None]] = {}
    ws_channels: dict[str, _RunnerWSChannel] = {}
    # Identify as a first-party client so the server's WebSocket origin
    # guard (CSWSH protection) allows the handshake — this runner is not a
    # browser and would otherwise rely on the permissive missing-origin
    # branch.
    headers: dict[str, str] = {"Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if tunnel_token:
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = tunnel_token
    async with websockets.connect(
        tunnel_url,
        additional_headers=headers,
        close_timeout=_RUNNER_TUNNEL_CLOSE_TIMEOUT_S,
        max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
    ) as ws:
        await _send_hello(ws.send, runner_version)
        _logger.info("runner %s connected to %s", runner_id, tunnel_url)
        try:
            async for raw in ws:
                await _handle_tunnel_frame(
                    app,
                    raw,
                    ws.send,
                    dispatch_tasks,
                    ws_channels,
                    on_activity=on_activity,
                )
        finally:
            await _cancel_dispatch_tasks(dispatch_tasks)
            await _cancel_ws_channels(ws_channels)


async def _send_hello(
    send_text: Callable[[str], Awaitable[None]],
    runner_version: str,
) -> None:
    """Send the runner's opening hello frame.

    :param send_text: Async WebSocket text sender.
    :param runner_version: Runner version string for the hello
        frame, e.g. ``"0.1.0"``.
    :returns: None.
    """
    await send_text(
        encode_frame(
            HelloFrame(
                runner_version=runner_version,
                frame_protocol_version=1,
                harnesses=[
                    "claude-native",
                    "claude-sdk",
                    "codex",
                    "openai-agents",
                    "open-responses",
                    "pi",
                ],
                envs=["os_sandbox"],
            )
        )
    )


async def _handle_tunnel_frame(
    app: _ASGIApp,
    raw: str | bytes,
    send_text: Callable[[str], Awaitable[None]],
    dispatch_tasks: dict[str, asyncio.Task[None]],
    ws_channels: dict[str, _RunnerWSChannel],
    *,
    on_activity: Callable[[], None] | None = None,
) -> None:
    """Handle one server-to-runner tunnel frame.

    Malformed frames (bad JSON, unknown ``kind``, missing required
    fields, non-utf8 bytes) are logged and skipped.

    :param app: Runner ASGI application.
    :param raw: Raw WebSocket message from the server.
    :param send_text: Async WebSocket text sender.
    :param dispatch_tasks: Mutable request-id-to-task map.
    :param ws_channels: Mutable channel-id to runner-side WS channel
        state map.
    :param on_activity: Optional sync callback fired for non-ping
        frames that represent real runner work.
    :returns: None.
    """
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        frame = decode_frame(text)
    except ValueError as exc:
        _logger.warning("runner received malformed tunnel frame; dropping: %s", exc)
        return
    if isinstance(frame, PingFrame):
        await send_text(encode_frame(PongFrame(ts=frame.ts)))
    elif isinstance(frame, RequestFrame):
        if on_activity is not None:
            on_activity()
        task = asyncio.create_task(
            dispatch_via_asgi(app, frame, send_text),
            name=f"ws-tunnel-dispatch:{frame.id}",
        )
        dispatch_tasks[frame.id] = task
        task.add_done_callback(_forget_dispatch_task(dispatch_tasks, frame.id))
    elif isinstance(frame, RequestCancelFrame):
        if on_activity is not None:
            on_activity()
        task = dispatch_tasks.get(frame.id)
        if task is not None:
            task.cancel()
    elif isinstance(frame, WSOpenFrame):
        if on_activity is not None:
            on_activity()
        channel = _RunnerWSChannel(ch_id=frame.ch_id, send_text=send_text)
        ws_channels[frame.ch_id] = channel
        channel.task = asyncio.create_task(
            _dispatch_ws_via_asgi(app, frame, channel),
            name=f"ws-tunnel-attach:{frame.ch_id}",
        )
        channel.task.add_done_callback(_forget_ws_channel(ws_channels, frame.ch_id))
    elif isinstance(frame, WSFrame):
        if on_activity is not None:
            on_activity()
        channel = ws_channels.get(frame.ch_id)
        if channel is None:
            _logger.debug("runner: dropping ws.frame for unknown ch_id %r", frame.ch_id)
            return
        if frame.encoding == "utf-8":
            channel.inbound.put_nowait(("text", frame.data))
        elif frame.encoding == "base64":
            try:
                decoded = base64.b64decode(frame.data, validate=True)
            except (binascii.Error, ValueError):
                _logger.warning(
                    "runner: dropping ws.frame with malformed base64 on ch_id %r",
                    frame.ch_id,
                )
                return
            channel.inbound.put_nowait(("bytes", decoded))
        else:
            _logger.warning("runner: dropping ws.frame with unknown encoding %r", frame.encoding)
    elif isinstance(frame, WSCloseFrame):
        if on_activity is not None:
            on_activity()
        channel = ws_channels.get(frame.ch_id)
        if channel is None:
            return
        channel.inbound.put_nowait(("close", (frame.code, frame.reason)))


async def _cancel_dispatch_tasks(dispatch_tasks: dict[str, asyncio.Task[None]]) -> None:
    """Cancel all active request dispatch tasks.

    :param dispatch_tasks: Mutable request-id-to-task map.
    :returns: None.
    """
    for task in dispatch_tasks.values():
        task.cancel()
    await asyncio.gather(*dispatch_tasks.values(), return_exceptions=True)


class _RunnerWSChannel:
    """Per-channel state on the runner side of a tunneled WS attach.

    Holds the inbound queue that the tunnel-receive task pushes
    onto, plus a back-pointer to the dispatch task so the cancel
    path can stop the ASGI WS dispatch when the tunnel disconnects.
    """

    def __init__(
        self,
        *,
        ch_id: str,
        send_text: Callable[[str], Awaitable[None]],
    ) -> None:
        self.ch_id = ch_id
        self.send_text = send_text
        # Items pushed by ``_handle_tunnel_frame``:
        #   ("text", str)             — server-to-runner text frame
        #   ("bytes", bytes)          — server-to-runner binary frame
        #   ("close", (code, reason)) — server-to-runner ws.close
        self.inbound: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        # Set once the runner-side ASGI app has accepted the WS
        # handshake. Used to know whether we still need to surface
        # a close as 1006-style abort or whether a clean close is
        # appropriate.
        self.accepted = False


async def _dispatch_ws_via_asgi(
    app: _ASGIApp,
    frame: WSOpenFrame,
    channel: _RunnerWSChannel,
) -> None:
    """Run a tunneled WS attach through the runner's ASGI app.

    Translates between channel inbound items and ASGI websocket
    receive/send events, and frames runner-side WS sends back as
    ``ws.frame`` / ``ws.close`` over the tunnel.

    :param app: Runner ASGI application.
    :param frame: The ``ws.open`` that triggered this dispatch.
    :param channel: Per-channel state for the receive side.
    """
    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": frame.path,
        "raw_path": frame.path.encode("utf-8"),
        "query_string": frame.query_string.encode("utf-8"),
        "headers": [],
        "client": ("tunnel", 0),
        "server": ("runner", 0),
        "root_path": "",
        "subprotocols": [],
    }

    # First receive() must return websocket.connect per ASGI WS spec.
    connect_event_consumed = False
    close_seen: tuple[int, str] | None = None

    async def receive() -> dict[str, Any]:
        nonlocal connect_event_consumed, close_seen
        if not connect_event_consumed:
            connect_event_consumed = True
            return {"type": "websocket.connect"}
        if close_seen is not None:
            # The peer already closed; surface a disconnect to the app.
            return {"type": "websocket.disconnect", "code": close_seen[0]}
        item = await channel.inbound.get()
        tag = item[0]
        if tag == "close":
            payload = item[1]
            assert isinstance(payload, tuple)
            code, _reason = payload
            close_seen = (code, _reason)
            return {"type": "websocket.disconnect", "code": code}
        if tag == "text":
            text_payload = item[1]
            assert isinstance(text_payload, str)
            return {"type": "websocket.receive", "text": text_payload}
        if tag == "bytes":
            bytes_payload = item[1]
            assert isinstance(bytes_payload, (bytes, bytearray))
            return {"type": "websocket.receive", "bytes": bytes(bytes_payload)}
        raise RuntimeError(f"runner ws-channel {channel.ch_id!r}: unknown tag {tag!r}")

    async def send(event: dict[str, Any]) -> None:
        ev_type = event.get("type")
        if ev_type == "websocket.accept":
            channel.accepted = True
            return
        if ev_type == "websocket.send":
            text = event.get("text")
            data = event.get("bytes")
            if text is not None:
                await channel.send_text(
                    encode_frame(WSFrame(ch_id=channel.ch_id, data=text, encoding="utf-8"))
                )
            elif data is not None:
                await channel.send_text(
                    encode_frame(
                        WSFrame(
                            ch_id=channel.ch_id,
                            data=base64.b64encode(bytes(data)).decode("ascii"),
                            encoding="base64",
                        )
                    )
                )
            return
        if ev_type == "websocket.close":
            code = event.get("code", 1000)
            reason = event.get("reason", "") or ""
            await channel.send_text(
                encode_frame(WSCloseFrame(ch_id=channel.ch_id, code=code, reason=reason))
            )
            return

    try:
        await app(scope, receive, send)
    except asyncio.CancelledError:
        # Tunnel teardown: try to inform the peer once, then re-raise.
        with contextlib.suppress(Exception):
            await channel.send_text(
                encode_frame(
                    WSCloseFrame(ch_id=channel.ch_id, code=1001, reason="runner shutdown")
                )
            )
        raise
    except Exception as exc:  # noqa: BLE001 -- log + surface as close
        _logger.warning("runner ws-attach dispatch %s failed: %r", channel.ch_id, exc)
        with contextlib.suppress(Exception):
            await channel.send_text(
                encode_frame(
                    WSCloseFrame(
                        ch_id=channel.ch_id,
                        code=1011,
                        reason="runner dispatch failed",
                    )
                )
            )


async def _cancel_ws_channels(ws_channels: dict[str, _RunnerWSChannel]) -> None:
    """Cancel every in-flight WS-channel dispatch task."""
    tasks = [ch.task for ch in ws_channels.values() if ch.task is not None]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _forget_ws_channel(
    ws_channels: dict[str, _RunnerWSChannel],
    ch_id: str,
) -> Callable[[asyncio.Task[None]], None]:
    """Drop a completed WS-channel dispatch task from the table."""

    def _callback(task: asyncio.Task[None]) -> None:
        ws_channels.pop(ch_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.warning("runner ws-attach dispatch %s failed: %r", ch_id, exc)

    return _callback


def _forget_dispatch_task(
    dispatch_tasks: dict[str, asyncio.Task[None]],
    req_id: str,
) -> Callable[[asyncio.Task[None]], None]:
    """Build a callback that forgets a completed dispatch task.

    :param dispatch_tasks: Mutable request-id-to-task map.
    :param req_id: Request id to remove, e.g.
        ``"7a0f7f7cb90f4a5fb5a8071fd0b77568"``.
    :returns: Callback suitable for
        :meth:`asyncio.Task.add_done_callback`.
    """

    def _callback(task: asyncio.Task[None]) -> None:
        """Forget the completed task and log unexpected failures.

        :param task: Completed dispatch task.
        :returns: None.
        """
        dispatch_tasks.pop(req_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _logger.warning("runner tunnel dispatch %s failed: %s", req_id, exc)

    return _callback


def _tunnel_url(server_url: str, runner_id: str) -> str:
    """Build the WebSocket tunnel URL for a server and runner.

    :param server_url: HTTP(S) server base URL, e.g.
        ``"http://127.0.0.1:6767"``.
    :param runner_id: Stable runner id, e.g.
        ``"runner_0123456789abcdef"``.
    :returns: WebSocket URL for the tunnel endpoint.
    :raises ValueError: If *server_url* is not HTTP(S).
    """
    parsed = urlsplit(server_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"server_url must use http or https, got {parsed.scheme!r}")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/v1/runners/{quote(runner_id, safe='')}/tunnel"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _websocket_close_code(exc: WebSocketException) -> int | None:
    """Return a close code from a websockets exception when present.

    :param exc: Exception raised by the ``websockets`` package.
    :returns: Close code such as ``4002``, or ``None`` when the
        exception does not carry one.
    """
    direct = getattr(exc, "code", None)
    if isinstance(direct, int):
        return direct
    for attr in ("rcvd", "sent"):
        close = getattr(exc, attr, None)
        code = getattr(close, "code", None)
        if isinstance(code, int):
            return code
    return None
