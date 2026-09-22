"""
Guard tests for the session MCP-server management SSRF / multi-tenant-RCE check
(:func:`omnigent.server.routes.session_mcp_servers.assert_mcp_server_request_safe`).

The create/update routes accepted an http ``url`` validated only by a
scheme-prefix check and a stdio ``command`` spawned unsandboxed, so a caller
with edit access could reach cloud metadata / internal hosts (SSRF) or register
an arbitrary local command (RCE) on shared multi-tenant runner infrastructure.
These tests exercise the guard directly and are hermetic: DNS resolution and the
single-user server-mode flag are stubbed, so no network or real config is
touched.
"""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import session_mcp_servers as mod
from omnigent.server.schemas import UpsertMCPServerRequest


def _http(url: str) -> UpsertMCPServerRequest:
    return UpsertMCPServerRequest(name="srv", transport="http", url=url)


def _stdio() -> UpsertMCPServerRequest:
    return UpsertMCPServerRequest(
        name="srv", transport="stdio", command="/bin/sh", args=["-c", "x"]
    )


def _stub_dns(monkeypatch: pytest.MonkeyPatch, ip: str | None) -> None:
    """Point getaddrinfo at *ip*, or raise OSError when *ip* is None."""

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list:
        if ip is None:
            raise OSError("name or service not known")
        return [(None, None, None, "", (ip, 0))]

    monkeypatch.setattr(mod.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://127.0.0.1:8080/",  # loopback
        "http://10.1.2.3/",  # private RFC1918
        "http://[::1]/",  # IPv6 loopback
        "http://192.168.0.5/",  # private
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


def test_http_hostname_resolving_to_metadata_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to an internal address is rejected."""
    _stub_dns(monkeypatch, "169.254.169.254")
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


def test_stdio_blocked_on_multi_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio transport is forbidden when the server is not single-user (RCE)."""
    monkeypatch.setattr(mod, "local_single_user_enabled", lambda: False)
    with pytest.raises(OmnigentError) as exc:
        mod.assert_mcp_server_request_safe(_stdio())
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_stdio_allowed_single_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio transport is still allowed on a single-user / local server."""
    monkeypatch.setattr(mod, "local_single_user_enabled", lambda: True)
    mod.assert_mcp_server_request_safe(_stdio())
