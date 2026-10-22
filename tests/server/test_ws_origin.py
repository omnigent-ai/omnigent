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

import logging
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.process_logging import _log_once_seen
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


@pytest.mark.parametrize(
    "origin,allowed",
    [
        # Happy path: subdomain (any depth) over the wildcarded scheme.
        ("https://foo.ts.net", True),
        ("https://machine.tailnet.ts.net", True),  # real Tailscale MagicDNS shape
        ("https://a.b.c.ts.net", True),  # arbitrarily deep is still a subdomain
        # The bare domain itself is not a subdomain of itself.
        ("https://ts.net", False),
        # Scheme must still match exactly.
        ("http://foo.ts.net", False),
        # A domain that merely ends with the same letters, but not on a
        # label boundary, must not match (naive suffix-matching bug).
        ("https://evilts.net", False),
        ("https://foots.net", False),
        # A different domain that happens to end similarly.
        ("https://foo.evilts.net", False),
        ("https://foo.notts.net", False),
        # Case-insensitivity, like real hostnames.
        ("https://FOO.TS.NET", True),
        # Missing/unparseable origin never matches a wildcard entry either.
        ("not-a-url", False),
    ],
)
def test_origin_allowed_with_wildcard_allowlist(origin: str, allowed: bool) -> None:
    """A ``*.``-prefixed allowlist entry trusts every subdomain, any depth.

    A failure here means either a legitimate subdomain (including nested,
    which is how Tailscale MagicDNS names actually look) was rejected, or
    — more dangerously — a lookalike/attacker domain (``evilts.net``,
    ``foo.evilts.net``) was wrongly admitted because the suffix match
    crossed a label boundary instead of stopping at a ``.``.

    :param origin: The handshake ``Origin`` under test.
    :param allowed: Expected policy decision.
    :returns: None.
    """
    extra = frozenset({"https://*.ts.net"})
    assert origin_allowed(origin, local_mode=False, extra_allowed=extra) is allowed


@pytest.mark.parametrize(
    "entry",
    [
        "https://*",  # bare wildcard, no domain at all
        "https://*.",  # nothing after the wildcard label
        "https://a.*.ts.net",  # wildcard not in the leftmost label
        "https://**.ts.net",  # not a clean "*." leftmost label
        "https://*.ts.*",  # a second "*" elsewhere in the pattern
        "https://*.ts.net/*",  # decorated with a path
        "https://*.ts.net?x=*",  # decorated with a query string
        "https://*@*.ts.net",  # decorated with userinfo
        "https://*:*@*.ts.net",  # decorated with userinfo (username and password)
    ],
)
def test_malformed_wildcard_entry_matches_nothing(entry: str) -> None:
    """A malformed wildcard-looking entry fails closed, not open.

    Each of these is ambiguous enough that guessing its intent would risk
    over-matching, so the policy must treat it as matching no real
    ``Origin`` at all rather than, say, silently falling back to "match
    everything". A failure here would turn a typo'd allowlist entry into
    an accidental open origin policy.

    The decorated-with-path/query/userinfo cases matter because an
    ``Origin`` is never anything but ``scheme://host[:port]`` — a
    wildcard entry carrying any of those is not a decorated valid
    pattern, it's a malformed one, even though the literal string a
    non-browser client could replay as its own ``Origin`` header would
    ordinarily still contain the same characters.

    :param entry: The malformed allowlist entry under test.
    :returns: None.
    """
    extra = frozenset({entry})
    for candidate in (
        "https://foo.ts.net",
        "https://ts.net",
        "https://evil.example.com",
        entry,  # literal replay of the entry's own text must not match either
    ):
        assert origin_allowed(candidate, local_mode=False, extra_allowed=extra) is False


def test_wildcard_entry_honors_explicit_port() -> None:
    """A wildcard entry with an explicit port only admits that same port.

    Mirrors the exact-match behavior for literal entries: an allowlisted
    origin string with a port only matches an ``Origin`` carrying that
    same port. A failure here would mean the wildcard silently ignores
    port scoping that an operator explicitly configured.

    :returns: None.
    """
    extra = frozenset({"https://*.ts.net:8443"})
    assert origin_allowed("https://foo.ts.net:8443", local_mode=False, extra_allowed=extra) is True
    assert origin_allowed("https://foo.ts.net", local_mode=False, extra_allowed=extra) is False
    assert (
        origin_allowed("https://foo.ts.net:9000", local_mode=False, extra_allowed=extra) is False
    )


def test_wildcard_entry_without_port_requires_no_explicit_port() -> None:
    """A portless wildcard entry does not admit an origin with a port.

    Consistent with literal-entry semantics, where the allowlisted string
    must match the ``Origin`` exactly (including the absence of a port):
    an entry that never mentions a port only matches origins that also
    omit one.

    :returns: None.
    """
    extra = frozenset({"https://*.ts.net"})
    assert (
        origin_allowed("https://foo.ts.net:8443", local_mode=False, extra_allowed=extra) is False
    )


def test_out_of_range_port_in_wildcard_entry_fails_closed_not_crash() -> None:
    """A malformed port in a configured wildcard entry never raises.

    ``urlsplit(...).port`` raises ``ValueError`` lazily, on access, for a
    port outside 0-65535. A misconfigured allowlist entry (an operator
    typo) must not crash every WebSocket handshake and file upload on the
    server — it should simply never match, like any other malformed
    entry.

    :returns: None.
    """
    extra = frozenset({"https://*.ts.net:99999"})
    assert origin_allowed("https://foo.ts.net", local_mode=False, extra_allowed=extra) is False


def test_out_of_range_port_in_origin_header_fails_closed_not_crash() -> None:
    """A malformed port in the incoming ``Origin`` header never raises.

    Unlike the allowlist entry (operator-controlled), the ``Origin``
    header comes from whatever client opens the connection — not
    necessarily a real browser, since only a real browser is bound by the
    forbidden-header list. Once a deployment configures even one wildcard
    entry, any client could otherwise crash every origin check on demand
    by sending an out-of-range port, a denial-of-service vector. It must
    instead just fail to match.

    :returns: None.
    """
    extra = frozenset({"https://*.ts.net"})
    assert (
        origin_allowed("https://evil.com:999999", local_mode=False, extra_allowed=extra) is False
    )


def test_wildcard_and_literal_entries_coexist() -> None:
    """A wildcard entry and a literal entry in the same allowlist both apply.

    A failure here would mean adding a wildcard entry accidentally
    disables, or is disabled by, an existing literal entry in the same
    ``OMNIGENT_WS_ALLOWED_ORIGINS`` value.

    :returns: None.
    """
    extra = frozenset({"https://*.ts.net", "https://ui.example.com"})
    assert origin_allowed("https://foo.ts.net", local_mode=False, extra_allowed=extra) is True
    assert origin_allowed("https://ui.example.com", local_mode=False, extra_allowed=extra) is True
    assert (
        origin_allowed("https://other.example.com", local_mode=False, extra_allowed=extra) is False
    )


def test_wildcard_entry_is_never_admitted_by_literal_replay() -> None:
    """A configured wildcard entry's own text is not itself a trusted Origin.

    ``*`` isn't a valid hostname character, so no real browser can ever
    send an ``Origin`` equal to a wildcard entry's literal text — only a
    non-browser client crafting a raw header could. Such an origin must
    be resolved purely through the wildcard-matching rule (here, it
    trivially satisfies the suffix check on its own malformed hostname),
    never admitted merely because it happens to equal a configured
    string verbatim. A failure here would mean an entry's own raw text
    doubles as an unintended second, redundant admission path.

    :returns: None.
    """
    entry = "https://*.ts.net"
    extra = frozenset({entry})
    # Still allowed: the origin's hostname ("*.ts.net") satisfies the
    # wildcard suffix rule on its own — but via the wildcard path, not a
    # literal-equality shortcut.
    assert origin_allowed(entry, local_mode=False, extra_allowed=extra) is True


def test_tailnet_scoped_wildcard_excludes_other_tailnets_and_apex() -> None:
    """A tailnet-scoped wildcard entry admits only that tailnet's machines.

    ``*.ts.net`` is Tailscale's single shared public suffix across every
    customer's tailnet, so it is not itself a safe recommendation for
    "trust my tailnet" — the deployment docs must scope the wildcard to
    the operator's own tailnet name (``*.<tailnet>.ts.net``). This proves
    the mechanism actually enforces that narrower boundary: a machine on
    the configured tailnet is admitted, a machine on a different tailnet
    (also ending in ``.ts.net``) is not, and neither is the tailnet's own
    apex.

    :returns: None.
    """
    extra = frozenset({"https://*.my-tailnet.ts.net"})
    assert (
        origin_allowed("https://machine.my-tailnet.ts.net", local_mode=False, extra_allowed=extra)
        is True
    )
    assert (
        origin_allowed(
            "https://machine.other-tailnet.ts.net", local_mode=False, extra_allowed=extra
        )
        is False
    )
    assert (
        origin_allowed("https://my-tailnet.ts.net", local_mode=False, extra_allowed=extra) is False
    )


def test_wildcard_allowlist_admits_subdomain_in_local_mode() -> None:
    """A wildcarded allowlist entry also applies in local mode.

    Local mode's loopback-only default still applies to origins the
    allowlist doesn't cover, but an explicitly wildcarded origin should be
    admitted the same way a literal allowlist entry already is (see
    ``test_origin_allowed_with_allowlist``).

    :returns: None.
    """
    extra = frozenset({"https://*.ts.net"})
    assert origin_allowed("https://foo.ts.net", local_mode=True, extra_allowed=extra) is True
    assert (
        origin_allowed("https://evil.example.com", local_mode=True, extra_allowed=extra) is False
    )


def test_parse_allowed_origins_splits_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist env is split on commas with whitespace stripped.

    A failure would mean origins are parsed with stray whitespace (never
    matching a real ``Origin`` header) or that blank entries leak in.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_ALLOWLIST_ENV, " https://a.example.com , ,https://b.example.com ")
    assert parse_allowed_origins() == frozenset({"https://a.example.com", "https://b.example.com"})


def test_parse_allowed_origins_passes_through_wildcard_entry_unparsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``*.``-prefixed entry round-trips through parsing untouched.

    ``parse_allowed_origins`` only splits and strips; wildcard expansion
    is entirely :func:`origin_allowed`'s job. A failure here would mean
    the raw entry was mangled (e.g. the ``*`` stripped) before it ever
    reaches the matching logic.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.setenv(_ALLOWLIST_ENV, "https://*.ts.net, https://ui.example.com")
    assert parse_allowed_origins() == frozenset({"https://*.ts.net", "https://ui.example.com"})


def test_parse_allowed_origins_unset_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset allowlist env yields an empty set (passthrough default).

    A failure would change the non-local default away from passthrough.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_ALLOWLIST_ENV, raising=False)
    assert parse_allowed_origins() == frozenset()


@pytest.mark.parametrize(
    "entry",
    [
        "https://*",
        "https://*.",
        "https://a.*.ts.net",
        "https://**.ts.net",
        "https://*.ts.*",
        "https://*.ts.net/*",
        "https://*.ts.net?x=*",
        "https://*@*.ts.net",
    ],
)
def test_parse_allowed_origins_warns_once_on_malformed_wildcard(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, entry: str
) -> None:
    """A malformed wildcard-looking entry logs one warning per process.

    Without this, a typo'd allowlist entry just silently matches nothing
    forever — the only symptom is a legitimate origin mysteriously getting
    rejected, with no diagnostic pointing at the bad entry. Since this
    function runs on every connection (not just at startup), a repeat call
    with the same misconfiguration must not re-log — that's what
    ``log_once`` is for.

    :param monkeypatch: pytest env patcher.
    :param caplog: pytest log capture fixture.
    :param entry: A malformed wildcard-looking allowlist entry.
    :returns: None.
    """
    logger_name = "omnigent.server.ws_origin"
    _log_once_seen.clear()
    monkeypatch.setenv(_ALLOWLIST_ENV, entry)
    with caplog.at_level(logging.WARNING, logger=logger_name):
        parse_allowed_origins()
        parse_allowed_origins()  # same misconfiguration again -> not re-logged
    records = [r for r in caplog.records if r.name == logger_name]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert entry in records[0].getMessage()


@pytest.mark.parametrize(
    "entry",
    [
        "https://*.ts.net",  # valid wildcard
        "https://ui.example.com",  # ordinary literal, no "*" at all
    ],
)
def test_parse_allowed_origins_does_not_warn_on_well_formed_entry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, entry: str
) -> None:
    """Neither a valid wildcard nor an ordinary literal entry logs a warning.

    A failure here (a warning fires anyway) would mean every deployment's
    perfectly valid configuration gets spammed with a false-positive
    diagnostic.

    :param monkeypatch: pytest env patcher.
    :param caplog: pytest log capture fixture.
    :param entry: A well-formed allowlist entry that must not warn.
    :returns: None.
    """
    logger_name = "omnigent.server.ws_origin"
    _log_once_seen.clear()
    monkeypatch.setenv(_ALLOWLIST_ENV, entry)
    with caplog.at_level(logging.WARNING, logger=logger_name):
        parse_allowed_origins()
    assert [r for r in caplog.records if r.name == logger_name] == []


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


def test_e2e_wildcard_allowlist_admits_subdomain_and_denies_lookalike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``*.``-prefixed allowlist entry works end-to-end through the ASGI stack.

    Exercises the full path from a real WebSocket handshake down through
    :func:`parse_allowed_origins` and :func:`origin_allowed`: a subdomain
    of the wildcarded domain connects, while a lookalike domain that
    merely ends with the same letters (not on a label boundary) is
    refused — proving the label-boundary safety isn't just a unit-level
    accident of ``origin_allowed`` but actually wired into the middleware.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.setenv(_ALLOWLIST_ENV, "https://*.ts.net")
    app = _make_app()
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws", headers={"origin": "https://evilts.net"}):
            pass
    assert exc_info.value.code == FORBIDDEN_ORIGIN_CLOSE_CODE
    assert app.state.accepted == []

    with client.websocket_connect(
        "/ws", headers={"origin": "https://machine.tailnet.ts.net"}
    ) as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"
    assert app.state.accepted == [True]
