"""Extended unit tests for runner-side WS tunnel serve helpers.

Covers dispatch_via_asgi, _tunnel_url construction, _refresh_auth_token,
tunnel recycle backoff, and on_reconnect callback.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner.transports.ws_tunnel import serve as serve_module
from omnigent.runner.transports.ws_tunnel.frames import (
    RequestFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    decode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import (
    _refresh_auth_token,
    _tunnel_url,
    _websocket_auth_redirect_url,
    _websocket_http_status,
    dispatch_via_asgi,
    serve_tunnel,
)

# ── _tunnel_url ─────────────────────────────────────────


def test_tunnel_url_http_to_ws() -> None:
    """http:// base URL produces ws:// tunnel URL."""
    url = _tunnel_url("http://127.0.0.1:6767", "runner_abc")
    assert url == "ws://127.0.0.1:6767/v1/runners/runner_abc/tunnel"


def test_tunnel_url_https_to_wss() -> None:
    """https:// base URL produces wss:// tunnel URL."""
    url = _tunnel_url("https://example.com", "runner_abc")
    assert url == "wss://example.com/v1/runners/runner_abc/tunnel"


def test_tunnel_url_with_base_path() -> None:
    """A base URL with a path prefix preserves it."""
    url = _tunnel_url("http://example.com/api", "runner_abc")
    assert url == "ws://example.com/api/v1/runners/runner_abc/tunnel"


def test_tunnel_url_with_trailing_slash() -> None:
    """Trailing slash on base URL is stripped."""
    url = _tunnel_url("http://example.com/", "runner_abc")
    assert url == "ws://example.com/v1/runners/runner_abc/tunnel"


def test_tunnel_url_percent_encodes_runner_id() -> None:
    """Special characters in runner_id are percent-encoded."""
    url = _tunnel_url("http://localhost:8000", "runner/with spaces")
    assert "runner%2Fwith%20spaces" in url


def test_tunnel_url_rejects_non_http_scheme() -> None:
    """Only http/https schemes are valid."""
    with pytest.raises(ValueError, match="http or https"):
        _tunnel_url("ftp://example.com", "runner_abc")

    with pytest.raises(ValueError, match="http or https"):
        _tunnel_url("ws://example.com", "runner_abc")


# ── _refresh_auth_token ─────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_returns_current_when_no_factory() -> None:
    """No factory means the current token is passed through."""
    result = await _refresh_auth_token("tok-current", None)
    assert result == "tok-current"


@pytest.mark.asyncio
async def test_refresh_returns_fresh_token_from_factory() -> None:
    """A working factory replaces the current token."""
    result = await _refresh_auth_token("tok-old", lambda: "tok-fresh")
    assert result == "tok-fresh"


@pytest.mark.asyncio
async def test_refresh_falls_back_on_factory_error() -> None:
    """Factory exceptions fall back to the current token."""

    def _broken_factory() -> str:
        raise OSError("IdP unreachable")

    result = await _refresh_auth_token("tok-fallback", _broken_factory)
    assert result == "tok-fallback"


@pytest.mark.asyncio
async def test_refresh_falls_back_when_factory_returns_none() -> None:
    """Factory returning None falls back to the current token."""
    result = await _refresh_auth_token("tok-current", lambda: None)
    assert result == "tok-current"


# ── dispatch_via_asgi ───────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_via_asgi_simple_get() -> None:
    """A GET request through a minimal ASGI app produces head + end frames."""

    async def _app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"status":"ok"}',
                "more_body": False,
            }
        )

    sent: list[str] = []
    frame = RequestFrame(id="req1", method="GET", path="/health")

    await dispatch_via_asgi(_app, frame, lambda t: _async_append(sent, t))

    assert len(sent) == 3  # head + body + end
    head = decode_frame(sent[0])
    assert isinstance(head, ResponseHeadFrame)
    assert head.status == 200
    assert head.id == "req1"

    body = decode_frame(sent[1])
    assert isinstance(body, ResponseBodyFrame)
    assert body.encoding == "utf-8"

    end = decode_frame(sent[2])
    assert isinstance(end, ResponseEndFrame)
    assert end.id == "req1"


@pytest.mark.asyncio
async def test_dispatch_via_asgi_post_with_body() -> None:
    """A POST request delivers the body to the ASGI receive callable."""
    received_body: list[bytes] = []

    async def _app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        msg = await receive()
        received_body.append(msg["body"])
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[str] = []
    frame = RequestFrame(
        id="req2",
        method="POST",
        path="/v1/sessions/s1/events",
        headers=[["content-type", "application/json"]],
        body='{"role":"user"}',
        encoding="utf-8",
    )

    await dispatch_via_asgi(_app, frame, lambda t: _async_append(sent, t))

    assert received_body == [b'{"role":"user"}']
    head = decode_frame(sent[0])
    assert isinstance(head, ResponseHeadFrame)
    assert head.status == 201


@pytest.mark.asyncio
async def test_dispatch_via_asgi_app_crash_sends_500() -> None:
    """An app crash before sending head produces a 500 error frame."""

    async def _crash_app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        raise RuntimeError("app crashed")

    sent: list[str] = []
    frame = RequestFrame(id="req3", method="GET", path="/crash")

    with pytest.raises(RuntimeError, match="app crashed"):
        await dispatch_via_asgi(_crash_app, frame, lambda t: _async_append(sent, t))

    # Should have sent head(500) + body + end.
    assert len(sent) == 3
    head = decode_frame(sent[0])
    assert isinstance(head, ResponseHeadFrame)
    assert head.status == 500

    body = decode_frame(sent[1])
    assert isinstance(body, ResponseBodyFrame)
    assert "runner_dispatch_failed" in body.body

    end = decode_frame(sent[2])
    assert isinstance(end, ResponseEndFrame)


@pytest.mark.asyncio
async def test_dispatch_via_asgi_app_crash_after_head_sends_end() -> None:
    """An app crash after sending head still sends end frame."""

    async def _late_crash_app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("crash after head")

    sent: list[str] = []
    frame = RequestFrame(id="req4", method="GET", path="/late-crash")

    with pytest.raises(RuntimeError, match="crash after head"):
        await dispatch_via_asgi(_late_crash_app, frame, lambda t: _async_append(sent, t))

    # head + end (no extra 500 head since head was already sent).
    frames = [decode_frame(s) for s in sent]
    assert isinstance(frames[0], ResponseHeadFrame)
    assert frames[0].status == 200
    assert isinstance(frames[-1], ResponseEndFrame)


@pytest.mark.asyncio
async def test_dispatch_via_asgi_streaming_body() -> None:
    """Multi-chunk streaming body produces multiple body frames."""

    async def _streaming_app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"chunk1", "more_body": True})
        await send({"type": "http.response.body", "body": b"chunk2", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[str] = []
    frame = RequestFrame(id="req5", method="GET", path="/stream")

    await dispatch_via_asgi(_streaming_app, frame, lambda t: _async_append(sent, t))

    frames = [decode_frame(s) for s in sent]
    head = frames[0]
    assert isinstance(head, ResponseHeadFrame)
    assert head.status == 200

    body_frames = [f for f in frames if isinstance(f, ResponseBodyFrame)]
    assert len(body_frames) == 2
    assert body_frames[0].body == "chunk1"
    assert body_frames[1].body == "chunk2"

    assert isinstance(frames[-1], ResponseEndFrame)


@pytest.mark.asyncio
async def test_dispatch_via_asgi_binary_body_uses_base64() -> None:
    """Binary content-type body is encoded as base64."""

    async def _binary_app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/octet-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"\x89PNG\r\n", "more_body": False})

    sent: list[str] = []
    frame = RequestFrame(id="req6", method="GET", path="/file")

    await dispatch_via_asgi(_binary_app, frame, lambda t: _async_append(sent, t))

    body = decode_frame(sent[1])
    assert isinstance(body, ResponseBodyFrame)
    assert body.encoding == "base64"


@pytest.mark.asyncio
async def test_dispatch_via_asgi_query_string() -> None:
    """Query string from the request frame reaches the ASGI scope."""
    captured_scope: list[dict[str, Any]] = []

    async def _scope_app(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        captured_scope.append(dict(scope))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[str] = []
    frame = RequestFrame(
        id="req7",
        method="GET",
        path="/search",
        query_string="q=hello&limit=10",
    )

    await dispatch_via_asgi(_scope_app, frame, lambda t: _async_append(sent, t))

    assert captured_scope[0]["query_string"] == b"q=hello&limit=10"
    assert captured_scope[0]["path"] == "/search"
    assert captured_scope[0]["method"] == "GET"


# ── serve_tunnel recycle backoff ────────────────────────


@pytest.mark.asyncio
async def test_serve_tunnel_recycle_close_code_resets_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1012 'service restart' close code resets backoff instead of escalating."""
    from websockets.exceptions import WebSocketException

    class _Recycled(WebSocketException):
        def __init__(self) -> None:
            super().__init__("recycle")

        @property
        def code(self) -> int:
            return 1012

    sleeps: list[float] = []
    call_count = 0

    async def _serve_once(app: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise _Recycled()

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)
    monkeypatch.setattr(serve_module.random, "uniform", lambda *_a, **_k: 0.0)

    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://localhost:8000",
            runner_id="r1",
            runner_version="0.1.0",
        )

    # Both sleeps should be at the initial delay (0.5), not escalating.
    assert sleeps == [0.5, 0.5]


@pytest.mark.asyncio
async def test_serve_tunnel_on_reconnect_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_reconnect is called after successful reconnect (not initial connect)."""
    reconnects: list[str] = []
    call_count = 0

    async def _serve_once(app: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1

    async def _sleep(delay: float) -> None:
        if call_count >= 2:
            raise asyncio.CancelledError

    async def _on_reconnect() -> None:
        reconnects.append("reconnected")

    monkeypatch.setattr(serve_module, "_serve_tunnel_once", _serve_once)
    monkeypatch.setattr(serve_module.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await serve_tunnel(
            _noop_app,
            server_url="http://localhost:8000",
            runner_id="r1",
            runner_version="0.1.0",
            on_reconnect=_on_reconnect,
        )

    # on_reconnect is called before the second _serve_once, not the first.
    assert reconnects == ["reconnected"]


# ── websocket_http_status edge cases ────────────────────


def test_websocket_http_status_none_for_no_response() -> None:
    """An exception without a response attribute returns None."""
    assert _websocket_http_status(ValueError("no response")) is None


def test_websocket_http_status_none_for_non_int_status() -> None:
    """A non-integer status code returns None."""

    class _FakeExc(Exception):
        class response:
            status_code = "not_an_int"

    assert _websocket_http_status(_FakeExc()) is None


# ── websocket_auth_redirect_url edge cases ──────────────


def test_websocket_auth_redirect_url_none_for_ws_scheme() -> None:
    """A ws:// InvalidURI is not an auth redirect."""
    from websockets.exceptions import InvalidURI

    exc = InvalidURI("ws://valid-but-down.example.com/tunnel", "connection refused")
    assert _websocket_auth_redirect_url(exc) is None


# ── Helper ──────────────────────────────────────────────


async def _noop_app(
    scope: dict[str, Any],
    receive: Any,
    send: Any,
) -> None:
    del scope, receive, send


async def _async_append(lst: list[str], item: str) -> None:
    lst.append(item)
