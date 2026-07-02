"""Unit tests for runner-side WebSocket tunnel serving helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypedDict

import pytest
from typing_extensions import Unpack
from websockets.exceptions import InvalidStatus, InvalidURI, WebSocketException
from websockets.http11 import Response

from omnigent.runner.identity import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    RUNNER_TUNNEL_TOKEN_HEADER,
)
from omnigent.runner.transports.ws_tunnel import serve as serve_module
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    RequestCancelFrame,
    RequestFrame,
    WSCloseFrame,
    WSFrame,
    WSOpenFrame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import (
    _handle_tunnel_frame,
    _serve_tunnel_once,
    _websocket_auth_redirect_url,
    _websocket_close_code,
    _websocket_http_status,
    serve_tunnel,
)


@dataclass
class _Close:
    """Minimal close object matching websockets' ``rcvd`` shape.

    :param code: WebSocket close code, e.g. ``4002``.
    """

    code: int


class _Closed(WebSocketException):
    """Fake websockets exception carrying an ``rcvd`` close code.

    :param code: WebSocket close code, e.g. ``4002``.
    """

    def __init__(self, code: int) -> None:
        super().__init__("closed")
        self.rcvd = _Close(code)


async def _noop_app(
    scope: dict[str, Any],
    receive: Any,
    send: Any,
) -> None:
    """ASGI app stub for serve-loop tests.

    :param scope: ASGI scope.
    :param receive: ASGI receive callable.
    :param send: ASGI send callable.
    :returns: None.
    """
    del scope, receive, send


def test_websocket_close_code_reads_received_close_code() -> None:
    """Protocol close codes are extracted for fail-loud retry decisions.

    :returns: None.
    """
    assert _websocket_close_code(_Closed(4002)) == 4002


def test_websocket_close_code_returns_none_without_code() -> None:
    """Exceptions without close metadata do not look fatal.

    :returns: None.
    """
    assert _websocket_close_code(WebSocketException("boom")) is None


def test_websocket_http_status_reads_invalid_status_response() -> None:
    """HTTP upgrade rejections expose their status for retry policy.

    :returns: None.
    """
    exc = InvalidStatus(Response(401, "Unauthorized", [], b""))

    assert _websocket_http_status(exc) == 401


@pytest.mark.asyncio
async def test_serve_tunnel_backs_off_after_clean_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean server-side close still sleeps before reconnecting.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    calls: list[str] = []
    sleeps: list[float] = []

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Pretend one tunnel connection closed normally.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Optional bearer token.
        :param tunnel_token: Optional tunnel binding token.
        :returns: None.
        """
        del app, runner_version, auth_token, tunnel_token
        calls.append(f"{tunnel_url}:{runner_id}")

    async def _sleep(delay: float) -> None:
        """Record the reconnect delay and stop the infinite loop.

        :param delay: Delay passed to ``asyncio.sleep``.
        :raises asyncio.CancelledError: Always, to end the test.
        """
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)
    # Pin jitter to 0 so the sleep delay is the unjittered backoff.
    monkeypatch.setattr(serve_module.random, "uniform", lambda *_args, **_kw: 0.0)

    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_clean_close",
            runner_version="0.1.0",
        )

    assert calls == ["ws://127.0.0.1:8000/v1/runners/runner_clean_close/tunnel:runner_clean_close"]
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_serve_tunnel_resets_backoff_after_successful_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later successful connection resets accumulated retry backoff.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    outcomes = iter(["error", "error", "clean", "stop"])
    sleeps: list[float] = []

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Raise transient errors, then close cleanly.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Optional bearer token.
        :param tunnel_token: Optional tunnel binding token.
        :raises ConnectionError: For the first two attempts.
        :raises asyncio.CancelledError: On the final attempt to end
            the test.
        """
        del app, tunnel_url, runner_id, runner_version, auth_token, tunnel_token
        outcome = next(outcomes)
        if outcome == "error":
            raise ConnectionError("temporary outage")
        if outcome == "stop":
            raise asyncio.CancelledError

    async def _sleep(delay: float) -> None:
        """Record reconnect delays without waiting.

        :param delay: Delay passed to ``asyncio.sleep``.
        :returns: None.
        """
        sleeps.append(delay)

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)
    # Pin jitter to 0 so sleep delays are the unjittered backoff curve.
    monkeypatch.setattr(serve_module.random, "uniform", lambda *_args, **_kw: 0.0)

    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_reset_backoff",
            runner_version="0.1.0",
        )

    # After backoff init 0.5s -> 1.0s, then reset to 0.5s after the
    # successful "clean" connection.
    assert sleeps == [0.5, 1.0, 0.5]


@pytest.mark.asyncio
async def test_serve_tunnel_fails_loud_on_protocol_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent server close codes are not retried.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Raise a protocol close from the server.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Optional bearer token.
        :param tunnel_token: Optional tunnel binding token.
        :raises _Closed: Always with close code 4002.
        """
        del app, tunnel_url, runner_id, runner_version, auth_token, tunnel_token
        raise _Closed(4002)

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)

    with pytest.raises(RuntimeError, match="runner tunnel rejected"):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_bad_protocol",
            runner_version="0.1.0",
        )


@pytest.mark.asyncio
async def test_serve_tunnel_fails_loud_on_http_auth_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 401/403 during WS upgrade stops the reconnect loop.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Raise an HTTP auth rejection from the server.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Optional bearer token.
        :param tunnel_token: Optional tunnel binding token.
        :raises InvalidStatus: Always with status 401.
        """
        del app, tunnel_url, runner_id, runner_version, auth_token, tunnel_token
        raise InvalidStatus(Response(401, "Unauthorized", [], b""))

    async def _sleep(delay: float) -> None:
        """Fail if auth rejection tries to reconnect.

        :param delay: Reconnect delay.
        :raises AssertionError: Always.
        """
        raise AssertionError(f"auth rejection should not sleep before retry: {delay}")

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await serve_tunnel(
            _noop_app,
            server_url="https://example.databricksapps.com",
            runner_id="runner_auth_rejected",
            runner_version="0.1.0",
            auth_token="tok-expired",
        )


def test_websocket_auth_redirect_url_detects_https_redirect() -> None:
    """An ``InvalidURI`` carrying an https:// target is classified as auth.

    Mirrors the real failure mode in Databricks Apps deployments:
    an unauthenticated WS handshake is 302'd to the OAuth
    ``/oidc/oauth2/v2.0/authorize?…`` URL, and the websockets
    library surfaces that via :class:`InvalidURI`. We must catch
    that case so the runner can stop retrying.

    :returns: None.
    """
    login_url = (
        "https://example.databricks.com/oidc/oauth2/v2.0/authorize"
        "?client_id=abc&response_type=code"
    )
    exc = InvalidURI(login_url, "scheme isn't ws or wss")
    assert _websocket_auth_redirect_url(exc) == login_url


def test_websocket_auth_redirect_url_ignores_non_redirect_invalid_uri() -> None:
    """A bare malformed ``ws://`` URL is NOT an auth redirect.

    A literal ``ws://`` with bad syntax should fall through to
    the existing retry path — it could be a transient typo from
    the caller and the regular reconnect machinery handles it.
    Only HTTP(S) targets are the "server is redirecting to a
    login page" signal we treat as fatal.

    :returns: None.
    """
    exc = InvalidURI("ws://bad host/path", "invalid")
    assert _websocket_auth_redirect_url(exc) is None


def test_websocket_auth_redirect_url_ignores_non_invalid_uri_exceptions() -> None:
    """Non-``InvalidURI`` exceptions return ``None`` (no false fatals).

    The helper is consulted on the generic ``WebSocketException``
    branch, so it must reject every other exception class even
    if they happen to carry a ``uri`` attribute.

    :returns: None.
    """
    assert _websocket_auth_redirect_url(WebSocketException("boom")) is None
    assert _websocket_auth_redirect_url(RuntimeError("other")) is None


@pytest.mark.asyncio
async def test_serve_tunnel_fails_loud_on_auth_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WS upgrade redirected to an OAuth login URL exits immediately.

    Previously the tunnel loop would keep retrying forever against
    a URL it could never upgrade, flooding the user's terminal
    with ``"isn't a valid URI: scheme isn't ws or wss"`` noise
    until they Ctrl+C. The fix detects the redirect, raises a
    fatal ``RuntimeError`` carrying the login URL, and lets the
    runner exit via ``_entry.main`` so the parent CLI surfaces
    the failure with the log-path hint.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    login_url = (
        "https://example.databricks.com/oidc/oauth2/v2.0/authorize"
        "?client_id=abc&response_type=code"
    )

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Simulate websockets following a redirect into an OAuth URL.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Optional bearer token.
        :param tunnel_token: Optional tunnel binding token.
        :raises InvalidURI: Always, carrying the OAuth target.
        """
        del app, tunnel_url, runner_id, runner_version, auth_token, tunnel_token
        raise InvalidURI(login_url, "scheme isn't ws or wss")

    async def _sleep(delay: float) -> None:
        """Fail the test if the loop ever sleeps for a retry.

        Auth redirects must not back off — they will never succeed
        and the user is staring at a frozen CLI in the meantime.

        :param delay: Reconnect delay.
        :raises AssertionError: Always.
        """
        raise AssertionError(f"auth redirect should not sleep before retry: {delay}")

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)

    with pytest.raises(RuntimeError) as exc_info:
        await serve_tunnel(
            _noop_app,
            server_url="https://example.databricksapps.com",
            runner_id="runner_redirected",
            runner_version="0.1.0",
        )
    message = str(exc_info.value)
    # The standard rejection prefix is preserved so
    # ``_entry.main`` recognizes the failure as the fatal class
    # and exits via ``SystemExit(1)`` instead of re-raising.
    assert "runner tunnel rejected by server" in message
    # The actual redirect target shows up so the user can see
    # what the server is asking for.
    assert login_url in message
    # User-actionable next step.
    assert "omnigent setup" in message


@pytest.mark.asyncio
async def test_serve_tunnel_once_sends_bearer_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated remote tunnels pass the bearer on the WS handshake.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import websockets

    class _ConnectKwargs(TypedDict, total=False):
        """Expected kwargs passed to ``websockets.connect``.

        :param additional_headers: Optional handshake headers.
        :param close_timeout: WebSocket close-handshake timeout.
        :param max_size: Maximum inbound WebSocket message size.
        :param ping_interval: Protocol keepalive ping interval (seconds).
        :param ping_timeout: Protocol keepalive PONG timeout (seconds).
        """

        additional_headers: dict[str, str] | None
        close_timeout: float
        max_size: int
        ping_interval: float
        ping_timeout: float

    captured: dict[str, str | _ConnectKwargs] = {}

    class _FakeWS:
        """WebSocket stub that accepts hello then closes iteration."""

        async def send(self, data: str) -> None:
            """
            Record the hello frame payload.

            :param data: Encoded tunnel frame JSON.
            :returns: None.
            """
            captured["sent"] = data

        def __aiter__(self) -> _FakeWS:
            """
            Return the async iterator.

            :returns: This fake WebSocket iterator.
            """
            return self

        async def __anext__(self) -> str:
            """
            End the fake WebSocket stream immediately.

            :raises StopAsyncIteration: Always.
            """
            raise StopAsyncIteration

    class _ConnectContext:
        """Async context manager returned by fake ``websockets.connect``."""

        async def __aenter__(self) -> _FakeWS:
            """
            Enter and yield the fake WebSocket.

            :returns: The fake WebSocket.
            """
            return _FakeWS()

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            """
            Exit without suppressing exceptions.

            :param exc_type: Exception type from the context, if any.
            :param exc: Exception value from the context, if any.
            :param tb: Traceback from the context, if any.
            :returns: None.
            """
            del exc_type, exc, tb

    def _fake_connect(url: str, **kwargs: Unpack[_ConnectKwargs]) -> _ConnectContext:
        """
        Capture connection arguments.

        :param url: WebSocket URL passed to ``websockets.connect``.
        :param kwargs: Additional connection keyword arguments.
        :returns: Fake async context manager for the connection.
        """
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _ConnectContext()

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    # No recorded ?o= selector, so no workspace-routing header rides the
    # handshake (keeps the asserted header set exact).
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda _server_url: None)

    await _serve_tunnel_once(
        _noop_app,
        tunnel_url="wss://example.databricksapps.com/v1/runners/runner_auth/tunnel",
        server_url="https://example.databricksapps.com",
        runner_id="runner_auth",
        runner_version="0.1.0",
        auth_token="tok-auth",
        tunnel_token="bind-token",
    )

    assert captured["url"] == "wss://example.databricksapps.com/v1/runners/runner_auth/tunnel"
    # The runner also sends the first-party Origin sentinel so the server's
    # CSWSH origin guard admits the tunnel (a non-browser client), in
    # addition to the bearer and tunnel-binding token.
    assert captured["kwargs"] == {
        "additional_headers": {
            "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
            "Authorization": "Bearer tok-auth",
            RUNNER_TUNNEL_TOKEN_HEADER: "bind-token",
        },
        "close_timeout": serve_module._RUNNER_TUNNEL_CLOSE_TIMEOUT_S,
        "max_size": serve_module.RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        "ping_interval": serve_module.TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        "ping_timeout": serve_module.TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    }
    assert isinstance(captured["sent"], str)


async def test_serve_tunnel_once_sends_org_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded ?o= selector rides the tunnel handshake.

    The WS upgrade must name the workspace via ``X-Databricks-Org-Id`` or it
    routes to the account. The selector is keyed by the server URL, not the
    ws tunnel URL, so the handshake resolves it from *server_url*.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import websockets

    captured: dict[str, object] = {}

    class _FakeWS:
        async def send(self, data: str) -> None:
            del data

        def __aiter__(self) -> _FakeWS:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

    class _Ctx:
        async def __aenter__(self) -> _FakeWS:
            return _FakeWS()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def _fake_connect(url: str, **kwargs: object) -> _Ctx:
        captured["headers"] = kwargs.get("additional_headers")
        return _Ctx()

    monkeypatch.setattr(websockets, "connect", _fake_connect)
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_org_id", lambda _server_url: "2850744067564480"
    )

    await _serve_tunnel_once(
        _noop_app,
        tunnel_url="wss://acme.databricks.com/v1/runners/r/tunnel",
        server_url="https://acme.databricks.com/api/2.0/omnigent",
        runner_id="r",
        runner_version="0.1.0",
        auth_token="tok",
    )

    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Databricks-Org-Id"] == "2850744067564480"


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param("not even json", id="bad-json"),
        pytest.param('{"kind": "alien_kind"}', id="unknown-kind"),
        pytest.param('{"kind": "response.head"}', id="missing-required-fields"),
        pytest.param(
            '{"kind":"request","id":"r","method":"GET","path":"/","headers":123}',
            id="bad-optional-field",
        ),
        pytest.param(b"\xff\xfe\xfd", id="bytes-not-utf8"),
    ],
)
@pytest.mark.asyncio
async def test_handle_tunnel_frame_drops_malformed_frame(
    malformed: str | bytes,
) -> None:
    """Malformed frames must be logged and skipped, not raised.

    :param malformed: A raw WebSocket payload that fails to decode.
    :returns: None.
    """
    sent: list[str] = []

    async def _send_text(text: str) -> None:
        """Capture frames the handler would write back.

        :param text: Encoded frame payload.
        :returns: None.
        """
        sent.append(text)

    dispatch_tasks: dict[str, asyncio.Task[None]] = {}

    await _handle_tunnel_frame(_noop_app, malformed, _send_text, dispatch_tasks, {})

    assert dispatch_tasks == {}
    assert sent == []


@pytest.mark.asyncio
async def test_handle_tunnel_frame_dispatches_after_malformed_frame() -> None:
    """A valid request following a malformed frame still dispatches.

    :returns: None.
    """
    sent: list[str] = []

    async def _send_text(text: str) -> None:
        """Capture frames the handler would write back.

        :param text: Encoded frame payload.
        :returns: None.
        """
        sent.append(text)

    dispatch_tasks: dict[str, asyncio.Task[None]] = {}

    await _handle_tunnel_frame(_noop_app, "not json", _send_text, dispatch_tasks, {})
    assert dispatch_tasks == {}

    valid = encode_frame(
        RequestFrame(id="req-after-bad", method="GET", path="/health"),
    )
    await _handle_tunnel_frame(_noop_app, valid, _send_text, dispatch_tasks, {})

    task = dispatch_tasks.get("req-after-bad")
    assert task is not None, (
        "Valid request after malformed frame should spawn a dispatch task. "
        "If None, the malformed frame poisoned the handler state."
    )
    await asyncio.gather(task, return_exceptions=True)
    assert task.exception() is None


@pytest.mark.asyncio
async def test_handle_tunnel_frame_marks_request_activity() -> None:
    """HTTP request frames reset the runner idle timer.

    :returns: None.
    """
    activities: list[str] = []

    async def _send_text(text: str) -> None:
        """Ignore response frames from the dispatched request.

        :param text: Encoded tunnel frame.
        :returns: None.
        """
        del text

    dispatch_tasks: dict[str, asyncio.Task[None]] = {}
    raw = encode_frame(RequestFrame(id="req-activity", method="GET", path="/health"))

    await _handle_tunnel_frame(
        _noop_app,
        raw,
        _send_text,
        dispatch_tasks,
        {},
        on_activity=lambda: activities.append("activity"),
    )

    task = dispatch_tasks.get("req-activity")
    assert task is not None
    await asyncio.gather(task, return_exceptions=True)
    assert activities == ["activity"]


@pytest.mark.asyncio
async def test_handle_tunnel_frame_does_not_mark_ping_activity() -> None:
    """Tunnel pings are keepalives and must not prevent idle shutdown.

    :returns: None.
    """
    activities: list[str] = []
    sent: list[str] = []

    async def _send_text(text: str) -> None:
        """Capture the pong frame.

        :param text: Encoded tunnel frame.
        :returns: None.
        """
        sent.append(text)

    await _handle_tunnel_frame(
        _noop_app,
        encode_frame(PingFrame(ts=123)),
        _send_text,
        {},
        {},
        on_activity=lambda: activities.append("activity"),
    )

    assert activities == []
    assert sent, "ping should still receive a pong even though it is not activity"


@pytest.mark.asyncio
async def test_handle_tunnel_frame_marks_websocket_channel_activity() -> None:
    """Tunneled WebSocket channel frames reset the idle timer.

    :returns: None.
    """
    activities: list[str] = []

    async def _send_text(text: str) -> None:
        """Ignore frames emitted by the ASGI websocket task.

        :param text: Encoded tunnel frame.
        :returns: None.
        """
        del text

    ws_channels: dict[str, serve_module._RunnerWSChannel] = {}
    await _handle_tunnel_frame(
        _noop_app,
        encode_frame(WSOpenFrame(ch_id="ch-1", path="/ws")),
        _send_text,
        {},
        ws_channels,
        on_activity=lambda: activities.append("activity"),
    )
    await _handle_tunnel_frame(
        _noop_app,
        encode_frame(WSFrame(ch_id="missing", data="payload")),
        _send_text,
        {},
        ws_channels,
        on_activity=lambda: activities.append("activity"),
    )
    await _handle_tunnel_frame(
        _noop_app,
        encode_frame(WSCloseFrame(ch_id="missing")),
        _send_text,
        {},
        ws_channels,
        on_activity=lambda: activities.append("activity"),
    )

    for channel in list(ws_channels.values()):
        if channel.task is not None:
            channel.task.cancel()
            await asyncio.gather(channel.task, return_exceptions=True)

    assert activities == ["activity", "activity", "activity"]


@pytest.mark.asyncio
async def test_handle_tunnel_frame_marks_request_cancel_activity() -> None:
    """Request-cancel frames reset the idle timer.

    :returns: None.
    """
    activities: list[str] = []

    async def _send_text(text: str) -> None:
        """Ignore outbound frames.

        :param text: Encoded tunnel frame.
        :returns: None.
        """
        del text

    await _handle_tunnel_frame(
        _noop_app,
        encode_frame(RequestCancelFrame(id="missing")),
        _send_text,
        {},
        {},
        on_activity=lambda: activities.append("activity"),
    )

    assert activities == ["activity"]


# ── Auth token refresh tests ─────────────────────────────


@pytest.mark.asyncio
async def test_serve_tunnel_calls_factory_on_each_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token factory is called before each connection attempt.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    tokens: list[str] = []
    call_count = 0

    def _factory() -> str:
        """Return incrementing tokens.

        :returns: Token string.
        """
        nonlocal call_count
        call_count += 1
        return f"tok-{call_count}"

    iteration = 0

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Record the token used for each connection attempt.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Bearer token for this attempt.
        :param tunnel_token: Tunnel binding token.
        :returns: None.
        """
        del app, tunnel_url, runner_id, runner_version, tunnel_token
        nonlocal iteration
        iteration += 1
        if auth_token is not None:
            tokens.append(auth_token)

    async def _sleep(delay: float) -> None:
        """Stop after the second reconnect.

        :param delay: Reconnect delay.
        :raises asyncio.CancelledError: After two iterations.
        """
        del delay
        if iteration >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_refresh",
            runner_version="0.1.0",
            auth_token="tok-initial",
            auth_token_factory=_factory,
        )

    # Factory is called before each reconnect, so each iteration
    # gets a fresh token. If tokens are all "tok-initial", the
    # factory was never called.
    assert tokens == ["tok-1", "tok-2"]


@pytest.mark.asyncio
async def test_serve_tunnel_401_with_factory_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 401 triggers a factory refresh and retries immediately.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    attempt = 0

    def _factory() -> str:
        """Return a fresh token.

        :returns: Token string.
        """
        return "tok-refreshed"

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """First call raises 401; second succeeds.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Bearer token for this attempt.
        :param tunnel_token: Tunnel binding token.
        :raises InvalidStatus: On first call with 401.
        :returns: None on second call.
        """
        del app, tunnel_url, runner_id, runner_version, tunnel_token
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise InvalidStatus(Response(401, "Unauthorized", [], b""))

    async def _sleep(delay: float) -> None:
        """Stop after the successful retry.

        :param delay: Reconnect delay.
        :raises asyncio.CancelledError: Always.
        """
        del delay
        raise asyncio.CancelledError

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)

    # Should NOT raise RuntimeError — the 401 is retried after
    # refresh. The CancelledError comes from the sleep after the
    # successful second attempt.
    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_401_retry",
            runner_version="0.1.0",
            auth_token="tok-expired",
            auth_token_factory=_factory,
        )

    # Two attempts: first 401 → refresh → second succeeds.
    assert attempt == 2


@pytest.mark.asyncio
async def test_serve_tunnel_401_without_factory_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 401 without a factory remains fatal (existing behavior).

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Raise 401.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Bearer token.
        :param tunnel_token: Tunnel binding token.
        :raises InvalidStatus: Always with 401.
        """
        del app, tunnel_url, runner_id, runner_version, auth_token, tunnel_token
        raise InvalidStatus(Response(401, "Unauthorized", [], b""))

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)

    # No factory → 401 is fatal.
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_no_factory",
            runner_version="0.1.0",
            auth_token="tok-stale",
        )


@pytest.mark.asyncio
async def test_serve_tunnel_403_remains_fatal_with_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 403 stays fatal even when a factory is available.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    factory_calls = 0

    def _factory() -> str:
        """Track calls but return a valid token for proactive refresh.

        :returns: Token string.
        """
        nonlocal factory_calls
        factory_calls += 1
        return "tok-valid"

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Raise 403.

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Bearer token.
        :param tunnel_token: Tunnel binding token.
        :raises InvalidStatus: Always with 403.
        """
        del app, tunnel_url, runner_id, runner_version, auth_token, tunnel_token
        raise InvalidStatus(Response(403, "Forbidden", [], b""))

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)

    # 403 with factory → still fatal. Factory is called once for
    # the proactive refresh before the connection attempt, but the
    # 403 handler must NOT call it again.
    with pytest.raises(RuntimeError, match="HTTP 403"):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_403",
            runner_version="0.1.0",
            auth_token="tok-valid",
            auth_token_factory=_factory,
        )
    # 1 call = proactive refresh only. If 2, the 403 handler
    # incorrectly attempted a refresh for a permissions error.
    assert factory_calls == 1


@pytest.mark.asyncio
async def test_serve_tunnel_reconnect_uses_fresh_token_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a disconnect, reconnect sends the REFRESHED token, not
    the initial one.

    This is the integration test for the 1-hour token expiry fix.
    The old code cached the initial token and never called the
    factory for the WebSocket ``Authorization`` header on reconnect.
    Sessions died after the token expired with no way to recover.

    **What a failure proves:** if ``tokens`` contains
    ``"tok-initial"`` on the second attempt, the factory was not
    called before reconnect — the stale token was reused.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    call_count = 0

    def _factory() -> str:
        """Simulate the Databricks SDK minting a fresh token.

        :returns: Token string with incrementing sequence number.
        """
        nonlocal call_count
        call_count += 1
        return f"tok-fresh-{call_count}"

    tokens_used: list[str | None] = []
    iteration = 0

    async def _serve_once(
        app: Any,
        *,
        tunnel_url: str,
        server_url: str = "",
        runner_id: str,
        runner_version: str,
        auth_token: str | None = None,
        tunnel_token: str | None = None,
    ) -> None:
        """Record the auth token used per connection, then simulate
        a clean close (disconnect).

        :param app: Runner ASGI app.
        :param tunnel_url: WebSocket URL.
        :param runner_id: Stable runner id.
        :param runner_version: Runner version string.
        :param auth_token: Bearer token for this connection attempt.
        :param tunnel_token: Tunnel binding token.
        :returns: None.
        """
        del app, tunnel_url, runner_id, runner_version, tunnel_token
        nonlocal iteration
        iteration += 1
        tokens_used.append(auth_token)

    async def _sleep(delay: float) -> None:
        """Stop after two reconnects so the test terminates.

        :param delay: Reconnect delay.
        :raises asyncio.CancelledError: After two iterations.
        """
        del delay
        if iteration >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://127.0.0.1:8000",
            runner_id="runner_refresh_integration",
            runner_version="0.1.0",
            auth_token="tok-initial",
            auth_token_factory=_factory,
        )

    # Both attempts must use FRESH tokens from the factory, not
    # the stale initial token. "tok-initial" appearing anywhere
    # means the factory was bypassed for that attempt.
    assert len(tokens_used) == 2, f"Expected 2 connection attempts, got {len(tokens_used)}"
    assert "tok-initial" not in tokens_used, (
        f"Stale initial token was reused on reconnect: {tokens_used}. "
        f"The factory should produce fresh tokens for every attempt."
    )
    assert tokens_used == ["tok-fresh-1", "tok-fresh-2"], (
        f"Expected incrementing fresh tokens, got {tokens_used}. "
        f"If both are the same, the factory was cached. If either is "
        f"'tok-initial', the factory was not called before reconnect."
    )
