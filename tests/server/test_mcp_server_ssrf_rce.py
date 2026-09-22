"""
Guard tests for the session MCP-server SSRF / multi-tenant-RCE registration
check (:func:`omnigent.server.routes.session_mcp_servers.assert_mcp_server_request_safe`),
which rejects an internal http ``url`` or stdio transport on a multi-tenant
server before the declaration is persisted, and the shared host classifier it
uses (:mod:`omnigent.util.ssrf`).

The tests are hermetic: DNS resolution and the single-user server-mode flag are
stubbed, so no network or real config is touched. The default mode is
multi-tenant (single-user disabled); the single-user carve-out is exercised
explicitly.
"""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import session_mcp_servers as mod
from omnigent.server.schemas import UpsertMCPServerRequest
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
