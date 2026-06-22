"""Tests for WebSocket ``Origin`` enforcement (CSWSH protection).

Three layers, each catching a distinct breakage:

- pure-policy tests for :func:`origin_allowed` and
  :func:`origin_hostname_is_loopback` (the decision table);
- ASGI-level tests of :class:`WebSocketOriginMiddleware` that prove a
  rejected handshake is closed with the forbidden-origin code and the
  downstream app is never invoked;
- end-to-end tests through Starlette's ``TestClient`` against a real
  FastAPI app, proving the handshake is refused *before* the route's
  ``websocket.accept()`` runs and that allowed origins round-trip.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.server.ws_origin import (
    FORBIDDEN_ORIGIN_CLOSE_CODE,
    WebSocketOriginMiddleware,
    origin_allowed,
    origin_hostname_is_loopback,
    parse_allowed_origins,
)

_LOCAL_ENV = "OMNIGENT_LOCAL_SINGLE_USER"
_ALLOWLIST_ENV = "OMNIGENT_WS_ALLOWED_ORIGINS"


@pytest.fixture(autouse=True)
def _clean_origin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a known WS-origin env baseline.

    The middleware reads ``OMNIGENT_LOCAL_SINGLE_USER`` and
    ``OMNIGENT_WS_ALLOWED_ORIGINS`` per connection; a value inherited
    from the developer's shell would flip the policy and make these
    tests pass or fail for the wrong reason. Each test sets only what it
    needs on top of this cleared baseline.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.delenv(_ALLOWLIST_ENV, raising=False)


# --------------------------------------------------------------------------
# origin_hostname_is_loopback
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin,expected",
    [
        ("http://localhost", True),
        ("http://localhost:8000", True),
        ("https://localhost:443", True),
        ("http://127.0.0.1:6767", True),
        ("http://127.5.5.5:1", True),  # all of 127.0.0.0/8 is loopback
        ("http://[::1]:8000", True),
        ("http://[::ffff:127.0.0.1]:8000", True),  # IPv4-mapped loopback
        ("https://app.example.com", False),
        ("https://localhost.evil.com", False),  # not a loopback host
        ("http://10.0.0.5:8000", False),
        ("http://169.254.0.1", False),  # link-local, not loopback
        ("", False),  # no host
        ("not-a-url", False),  # unparseable host
    ],
)
def test_origin_hostname_is_loopback(origin: str, expected: bool) -> None:
    """The loopback check accepts only genuine loopback hosts.

    A failure here means a non-loopback origin (a CSWSH attacker) was
    classified as loopback, or a real local UI origin was rejected.

    :param origin: The ``Origin`` header under test.
    :param expected: Whether it should be classified as loopback.
    :returns: None.
    """
    assert origin_hostname_is_loopback(origin) is expected


# --------------------------------------------------------------------------
# origin_allowed (the decision table)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin,local_mode,allowed",
    [
        # Missing Origin (non-browser client) is allowed in any mode.
        (None, True, True),
        (None, False, True),
        # The first-party sentinel is always allowed.
        (OMNIGENT_INTERNAL_WS_ORIGIN, True, True),
        (OMNIGENT_INTERNAL_WS_ORIGIN, False, True),
        # Local mode: only loopback browser origins pass.
        ("http://localhost:8000", True, True),
        ("http://127.0.0.1:6767", True, True),
        ("https://evil.example.com", True, False),
        # Non-local mode: cookie/proxy auth guards, so any origin passes
        # when no allowlist is configured.
        ("https://evil.example.com", False, True),
    ],
)
def test_origin_allowed_no_allowlist(origin: str | None, local_mode: bool, allowed: bool) -> None:
    """Policy decisions with no explicit allowlist configured.

    A failure means the core CSWSH guard is wrong: e.g. a cross-origin
    browser handshake admitted in local mode (the attack), or the local
    UI / runner sentinel wrongly refused (breaking the product).

    :param origin: The handshake ``Origin``, or ``None``.
    :param local_mode: Whether the single-user local marker is set.
    :param allowed: Expected policy decision.
    :returns: None.
    """
    assert origin_allowed(origin, local_mode=local_mode, extra_allowed=frozenset()) is allowed


@pytest.mark.parametrize(
    "origin,local_mode,allowed",
    [
        # Allowlisted origin passes in either mode.
        ("https://ui.example.com", False, True),
        ("https://ui.example.com", True, True),
        # In non-local mode a configured allowlist flips the default to
        # deny: an origin not on the list is refused even though cookies
        # would otherwise guard it.
        ("https://evil.example.com", False, False),
        # Local mode still admits loopback even alongside an allowlist.
        ("http://localhost:3000", True, True),
        # Missing Origin still passes (non-browser clients) despite the
        # allowlist.
        (None, False, True),
    ],
)
def test_origin_allowed_with_allowlist(
    origin: str | None, local_mode: bool, allowed: bool
) -> None:
    """Policy decisions when ``OMNIGENT_WS_ALLOWED_ORIGINS`` is set.

    A failure means the deployment allowlist either failed to admit a
    configured origin or failed to deny an unlisted one in non-local
    mode.

    :param origin: The handshake ``Origin``, or ``None``.
    :param local_mode: Whether the single-user local marker is set.
    :param allowed: Expected policy decision.
    :returns: None.
    """
    extra = frozenset({"https://ui.example.com"})
    assert origin_allowed(origin, local_mode=local_mode, extra_allowed=extra) is allowed


def test_parse_allowed_origins_splits_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist env is split on commas with whitespace stripped.

    A failure would mean origins are parsed with stray whitespace (never
    matching a real ``Origin`` header) or that blank entries leak in.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_ALLOWLIST_ENV, " https://a.example.com , ,https://b.example.com ")
    assert parse_allowed_origins() == frozenset({"https://a.example.com", "https://b.example.com"})


def test_parse_allowed_origins_unset_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset allowlist env yields an empty set (passthrough default).

    A failure would change the non-local default away from passthrough.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_ALLOWLIST_ENV, raising=False)
    assert parse_allowed_origins() == frozenset()


# --------------------------------------------------------------------------
# WebSocketOriginMiddleware — ASGI level
# --------------------------------------------------------------------------


class _RecordingASGIApp:
    """Downstream ASGI app that records whether it was invoked.

    Stands in for the real route stack so a middleware test can prove
    whether a handshake reached the route (was admitted) or was rejected
    by the middleware before reaching it.
    """

    def __init__(self) -> None:
        """Initialize with no invocations recorded.

        :returns: None.
        """
        self.called = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Record the call and accept the (websocket) handshake.

        :param scope: ASGI connection scope.
        :param receive: ASGI receive callable.
        :param send: ASGI send callable.
        :returns: None.
        """
        self.called = True
        await send({"type": "websocket.accept"})


def _ws_scope(origin: str | None) -> dict[str, Any]:
    """Build a minimal ASGI websocket scope with an optional ``Origin``.

    :param origin: Origin header value to include, or ``None`` to omit.
    :returns: An ASGI scope dict with ``type == "websocket"``.
    """
    headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    return {"type": "websocket", "headers": headers}


async def _drive_middleware(
    middleware: WebSocketOriginMiddleware, scope: dict[str, Any]
) -> list[dict[str, Any]]:
    """Run the middleware against a scope, capturing sent ASGI messages.

    :param middleware: The middleware instance under test.
    :param scope: ASGI scope to dispatch.
    :returns: The list of ASGI messages the middleware sent downstream.
    """
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        """Yield the initial websocket connect event.

        :returns: A ``websocket.connect`` ASGI event.
        """
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        """Capture an ASGI message emitted upstream.

        :param message: The ASGI message to record.
        :returns: None.
        """
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


async def test_middleware_rejects_forbidden_origin_without_calling_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forbidden origin is closed and the downstream app never runs.

    Proves the two security guarantees at once: the connection is closed
    with :data:`FORBIDDEN_ORIGIN_CLOSE_CODE`, and the route (which would
    otherwise ``accept()`` and bridge I/O) is never invoked. If the
    middleware accepted-then-checked, ``downstream.called`` would be
    ``True``.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    downstream = _RecordingASGIApp()
    middleware = WebSocketOriginMiddleware(downstream)

    sent = await _drive_middleware(middleware, _ws_scope("https://evil.example.com"))

    assert downstream.called is False  # route never reached → rejected pre-accept
    assert sent == [
        {
            "type": "websocket.close",
            "code": FORBIDDEN_ORIGIN_CLOSE_CODE,
            "reason": "forbidden origin",
        }
    ]


async def test_middleware_admits_loopback_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback origin in local mode reaches the downstream app.

    A failure (downstream not called) would mean the guard rejects the
    user's own local UI.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    downstream = _RecordingASGIApp()
    middleware = WebSocketOriginMiddleware(downstream)

    sent = await _drive_middleware(middleware, _ws_scope("http://localhost:8000"))

    assert downstream.called is True  # admitted → route handled the handshake
    assert sent == [{"type": "websocket.accept"}]


async def test_middleware_ignores_non_websocket_scope() -> None:
    """Non-websocket scopes pass straight through, untouched.

    A failure would mean the WS guard interferes with ordinary HTTP
    traffic.

    :returns: None.
    """
    downstream = _RecordingASGIApp()
    middleware = WebSocketOriginMiddleware(downstream)

    await _drive_middleware(middleware, {"type": "http", "headers": []})

    assert downstream.called is True  # HTTP scope delegated unconditionally


# --------------------------------------------------------------------------
# End-to-end through Starlette TestClient + a real FastAPI route
# --------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """Build a FastAPI app guarded by the origin middleware.

    The single websocket route appends to ``app.state.accepted`` *before*
    accepting, so a test can prove whether the route ran at all — the
    list stays empty when the middleware rejects pre-accept.

    :returns: The configured FastAPI app.
    """
    app = FastAPI()
    app.state.accepted = []

    @app.websocket("/ws")
    async def echo(websocket: WebSocket) -> None:
        """Echo one text frame back to the client, prefixed.

        :param websocket: The incoming connection.
        :returns: None.
        """
        websocket.app.state.accepted.append(True)
        await websocket.accept()
        msg = await websocket.receive_text()
        await websocket.send_text(f"echo:{msg}")
        await websocket.close()

    app.add_middleware(WebSocketOriginMiddleware)
    return app


def test_e2e_local_mode_rejects_cross_origin_before_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-origin browser handshake is refused before the route runs.

    This is the core CSWSH defense end-to-end: with the local marker set,
    a connection carrying a hostile ``Origin`` is closed at the handshake
    with code 4403, and the route's pre-accept marker is never appended —
    proving the rejection happened before ``websocket.accept()``.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    app = _make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example.com"}):
            pass

    # 4403 = forbidden origin (our private close code), distinct from the
    # 1008 auth-failure code used elsewhere.
    assert exc_info.value.code == FORBIDDEN_ORIGIN_CLOSE_CODE
    # The route never ran: rejection happened before websocket.accept().
    assert app.state.accepted == []


def test_e2e_local_mode_admits_loopback_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loopback-origin handshake connects and round-trips a message.

    Proves the user's own local UI (which sends a loopback ``Origin``)
    still works under the guard.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect("/ws", headers={"origin": "http://localhost:5173"}) as ws:
        ws.send_text("hi")
        # The echo proves the route accepted and processed the frame.
        assert ws.receive_text() == "echo:hi"
    assert app.state.accepted == [True]


def test_e2e_local_mode_admits_missing_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handshake with no ``Origin`` connects (non-browser client path).

    The CLI runner / host clients send no ``Origin``; rejecting them
    would break local operation. TestClient sends no ``Origin`` unless
    asked, so this mirrors that path.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
    assert app.state.accepted == [True]


def test_e2e_local_mode_admits_internal_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handshake bearing the first-party sentinel origin connects.

    This mirrors what the runner / host / terminal-attach clients send;
    a failure would mean our own tunnels are rejected in local mode.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect("/ws", headers={"origin": OMNIGENT_INTERNAL_WS_ORIGIN}) as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
    assert app.state.accepted == [True]


def test_e2e_non_local_mode_admits_cross_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the local marker, cross-origin handshakes pass through.

    Non-local modes authenticate via cookie/proxy, so the middleware
    must not block them. A failure would break deployed multi-user
    servers.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    app = _make_app()
    client = TestClient(app)

    with client.websocket_connect("/ws", headers={"origin": "https://app.example.com"}) as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
    assert app.state.accepted == [True]


def test_e2e_allowlist_denies_unlisted_origin_in_non_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured allowlist denies unlisted origins even in non-local mode.

    Proves the opt-in defense-in-depth knob: setting
    ``OMNIGENT_WS_ALLOWED_ORIGINS`` flips the non-local default from
    passthrough to deny-by-default, rejecting an unlisted origin with the
    forbidden-origin code while the listed origin still connects.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, "https://ui.example.com")
    app = _make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", headers={"origin": "https://other.example.com"}):
            pass
    assert exc_info.value.code == FORBIDDEN_ORIGIN_CLOSE_CODE
    assert app.state.accepted == []

    with client.websocket_connect("/ws", headers={"origin": "https://ui.example.com"}) as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
    # Only the allowlisted connection reached the route.
    assert app.state.accepted == [True]
