"""
Guard tests for the session MCP-server SSRF / multi-tenant-RCE protections.

Two enforcement points share one host classifier (:mod:`omnigent.util.ssrf`):

- **registration time** —
  :func:`omnigent.server.routes.session_mcp_servers.assert_mcp_server_request_safe`
  rejects an internal http ``url`` or stdio transport on a multi-tenant server
  before the declaration is persisted;
- **connect time** —
  :meth:`omnigent.tools.mcp.McpServerConnection._reject_internal_redirect`
  refuses an HTTP redirect that pivots from a public host to an internal one,
  closing the follow-redirects bypass of the registration-time check.

The tests are hermetic: DNS resolution, the single-user server-mode flag, and
the shared classifier are stubbed, so no network or real config is touched. The
default mode is multi-tenant (single-user disabled); the single-user carve-out
is exercised explicitly.
"""

from __future__ import annotations

import asyncio
import types

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import session_mcp_servers as mod
from omnigent.server.schemas import UpsertMCPServerRequest
from omnigent.tools import mcp as mcp_mod
from omnigent.util import ssrf


@pytest.fixture(autouse=True)
def _multi_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to multi-tenant mode (single-user disabled)."""
    monkeypatch.setattr(mod, "local_single_user_enabled", lambda: False)


def _http(url: str) -> UpsertMCPServerRequest:
    return UpsertMCPServerRequest(name="srv", transport="http", url=url)


def _stdio() -> UpsertMCPServerRequest:
    return UpsertMCPServerRequest(
        name="srv", transport="stdio", command="/bin/sh", args=["-c", "x"]
    )


def _stub_dns(monkeypatch: pytest.MonkeyPatch, ip: str | None) -> None:
    """Point the classifier's getaddrinfo at *ip*, or raise OSError when None."""

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list:
        if ip is None:
            raise OSError("name or service not known")
        return [(None, None, None, "", (ip, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


# ── registration-time guard ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://127.0.0.1:8080/",  # loopback
        "http://10.1.2.3/",  # private RFC1918
        "http://192.168.0.5/",  # private
        "http://100.64.0.1/",  # shared / CGNAT (RFC 6598) — not is_private
        "http://[::1]/",  # IPv6 loopback
    ],
)
def test_http_ip_literal_internal_blocked(url: str) -> None:
    """An http url whose host is an internal IP literal is rejected (SSRF)."""
    with pytest.raises(OmnigentError) as exc:
        mod.assert_mcp_server_request_safe(_http(url))
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_http_public_ip_literal_allowed() -> None:
    """A public IP literal is permitted (no DNS needed for a literal)."""
    mod.assert_mcp_server_request_safe(_http("http://8.8.8.8/"))


@pytest.mark.parametrize("resolved_ip", ["169.254.169.254", "100.64.0.1", "10.0.0.9"])
def test_http_hostname_resolving_to_internal_blocked(
    monkeypatch: pytest.MonkeyPatch, resolved_ip: str
) -> None:
    """A hostname that resolves to an internal / shared address is rejected."""
    _stub_dns(monkeypatch, resolved_ip)
    with pytest.raises(OmnigentError) as exc:
        mod.assert_mcp_server_request_safe(_http("http://metadata.example/"))
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_http_public_hostname_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to a public address is permitted."""
    _stub_dns(monkeypatch, "93.184.216.34")
    mod.assert_mcp_server_request_safe(_http("https://example.com/mcp"))


def test_http_unresolvable_host_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable host is treated as internal and rejected (fail closed)."""
    _stub_dns(monkeypatch, None)
    with pytest.raises(OmnigentError) as exc:
        mod.assert_mcp_server_request_safe(_http("http://nope.invalid/"))
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_http_malformed_url_rejected() -> None:
    """A malformed authority (unclosed IPv6 literal) fails closed, not with a 500.

    ``urlsplit(...).hostname`` raises ``ValueError`` for such input; the guard
    must convert that to a controlled FORBIDDEN rather than let it surface as an
    unhandled 500.
    """
    with pytest.raises(OmnigentError) as exc:
        mod.assert_mcp_server_request_safe(_http("http://[::1"))
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_stdio_blocked_on_multi_tenant() -> None:
    """stdio transport is forbidden when the server is not single-user (RCE)."""
    with pytest.raises(OmnigentError) as exc:
        mod.assert_mcp_server_request_safe(_stdio())
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_stdio_allowed_single_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio transport is still allowed on a single-user / local server."""
    monkeypatch.setattr(mod, "local_single_user_enabled", lambda: True)
    mod.assert_mcp_server_request_safe(_stdio())


def test_http_internal_allowed_single_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-user / local server may register a loopback http MCP endpoint.

    There is no other tenant to protect and reaching a local service is the
    common local-dev case, so the internal-host check is skipped — mirroring the
    stdio carve-out.
    """
    monkeypatch.setattr(mod, "local_single_user_enabled", lambda: True)
    mod.assert_mcp_server_request_safe(_http("http://127.0.0.1:3000/mcp"))


# ── host classifier: IPv6 transition forms that embed an internal IPv4 ────────


@pytest.mark.parametrize(
    "literal",
    [
        "::ffff:169.254.169.254",  # IPv4-mapped
        "2002:a9fe:a9fe::",  # 6to4 of 169.254.169.254
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "::a9fe:a9fe",  # deprecated IPv4-compatible of 169.254.169.254
    ],
)
def test_host_is_internal_decodes_ipv6_transition_forms(literal: str) -> None:
    """An IPv6 literal that embeds an internal IPv4 destination is blocked.

    These transition forms are the exact vectors the embedded-IPv4 decode was
    written to close: the outer IPv6 flags do not reflect the smuggled
    169.254.169.254 metadata address, so each must be decoded and judged.
    """
    assert ssrf.host_is_internal(literal) is True


@pytest.mark.parametrize("literal", ["::ffff:8.8.8.8", "64:ff9b::8.8.8.8"])
def test_host_is_internal_allows_public_embedded_ipv4(literal: str) -> None:
    """An IPv6 transition form embedding a public IPv4 stays allowed."""
    assert ssrf.host_is_internal(literal) is False


# ── connect-time guard: HTTP redirect re-validation ──────────────────────────


def _conn() -> types.SimpleNamespace:
    """A minimal stand-in carrying only what the redirect hook reads."""
    return types.SimpleNamespace(config=types.SimpleNamespace(name="srv"))


def _redirect(from_url: str, to_url: str, status: int = 302) -> httpx.Response:
    return httpx.Response(
        status, headers={"location": to_url}, request=httpx.Request("POST", from_url)
    )


def test_redirect_public_to_internal_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public endpoint redirecting to an internal host is refused."""
    monkeypatch.setattr(mcp_mod, "host_is_internal", lambda h: h == "169.254.169.254")
    resp = _redirect("https://93.184.216.34/mcp", "http://169.254.169.254/latest/")
    with pytest.raises(mcp_mod._InternalRedirectBlocked):
        asyncio.run(mcp_mod.McpServerConnection._reject_internal_redirect(_conn(), resp))


def test_redirect_public_to_public_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public→public redirect (incl. trailing-slash / scheme upgrade) is fine."""
    monkeypatch.setattr(mcp_mod, "host_is_internal", lambda h: False)
    resp = _redirect("https://93.184.216.34/mcp", "https://93.184.216.34/mcp/")
    asyncio.run(mcp_mod.McpServerConnection._reject_internal_redirect(_conn(), resp))


def test_redirect_internal_origin_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect that starts on an internal origin (local dev) is not our concern."""
    monkeypatch.setattr(mcp_mod, "host_is_internal", lambda h: True)
    resp = _redirect("http://127.0.0.1:3000/mcp", "http://127.0.0.1:3000/mcp/")
    asyncio.run(mcp_mod.McpServerConnection._reject_internal_redirect(_conn(), resp))


def test_non_redirect_response_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal (non-3xx) response is passed through untouched."""
    monkeypatch.setattr(mcp_mod, "host_is_internal", lambda h: True)
    resp = httpx.Response(200, request=httpx.Request("POST", "https://93.184.216.34/mcp"))
    asyncio.run(mcp_mod.McpServerConnection._reject_internal_redirect(_conn(), resp))
